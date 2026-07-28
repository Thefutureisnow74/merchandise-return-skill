#!/usr/bin/env python3
"""
idempotency.py — the skill-level send guard (Blueprint M22, hardened M45).

Makes a double-send structurally impossible, regardless of which code path triggers a send.
This is the generalized fix for the 2026-07-18 bank-double-email incident: instead of relying on
a per-cron flag file (which only protects one script), EVERY outbound in the engine must pass
through reserve_send() first, and gmail_transport.send_mime() now REFUSES to put anything on the
wire without a token this module minted.

--------------------------------------------------------------------------------------------
WHAT M45 ADDED, AND WHY (each item is a real incident, not a hypothetical)
--------------------------------------------------------------------------------------------
1. TOKENS.  send_mime() was a plain public function; any module could import and call it with
   zero guard, and three did (2026-07-18 Stride, 07-25 PPG, 07-28 Relax The Back). reserve_send()
   now mints a one-shot opaque token; send_mime() validates and consumes it or raises.

2. COOLDOWN.  The exact key is (case, action, recipient, sha256(body)). On 2026-07-28 two letters
   to the same vendor differed only in the deadline date -> different hash -> different key ->
   duplicate. A coarse (case, recipient) cooldown now sits ABOVE the exact key: default 48h,
   overridable only with an explicit override=True that is RECORDED in the ledger entry.

3. TWO-PHASE.  reserve() used to record the send BEFORE the transport ran and never released it,
   so a failed send burned the key forever: the letter became permanently un-sendable while the
   ledger claimed it went. Send reservations are now written "pending", then commit()ed or
   release()d. A "pending" older than PENDING_TTL_MINUTES is treated as a crashed run and is
   re-reservable.

4. LOCKING.  reserve() was documented "atomic check-and-set" and was in fact read-then-write with
   no lock, while mer-hotpath (every minute) can race the hourly mer-engine. Every mutation now
   runs under an fcntl.flock on a sibling lockfile. fcntl is POSIX-only and this repo is EDITED
   ON WINDOWS, so the import is guarded and degrades to no-lock rather than crashing.

5. FAIL CLOSED.  _load() used to swallow JSONDecodeError and return {} — a corrupt ledger silently
   reset the entire duplicate-send history and the next save made that permanent. A MISSING file
   is fine (empty ledger); a PRESENT-but-unreadable file now raises LedgerCorrupt. Every save
   keeps the previous good version at <ledger>.bak.

6. PERMISSIONS.  tempfile.mkstemp() always creates 0600 owned by the writing uid, and os.replace
   carried that onto the ledger — so a root-run send locked out the hermes-run send with
   PermissionError. Every atomic save now chmods the temp file to 0664 before the replace.

7. VETOES.  A vetoed letter was dropped from disk and recorded NOWHERE, so case_tick at 09:00 the
   next morning found no queue record and no ledger entry and re-enqueued the identical letter,
   daily, forever. Vetoes are now persisted in a sibling mer_vetoed.json and are queryable via
   was_vetoed(case, action).

--------------------------------------------------------------------------------------------
A "logical send" is keyed by (case, action, recipient, body-fingerprint) so that:
  - re-running the tick / a retry / a second code path with the same message -> blocked
  - a genuinely different message (new tier, new recipient, changed body) -> allowed exact-key,
    but still subject to the (case, recipient) cooldown above it.

Ledger: a single JSON file, atomically updated, locked. Persists across runs and restarts.

NOTE FOR NON-SEND CALLERS (case_tick's phase_advance / remedy_map_build board writes):
reserve() is unchanged — same (ok, key) tuple, same immediate commit, no cooldown, no token.
Only reserve_send() opts into the send-path machinery.
"""
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

try:                                    # POSIX only. Windows dev boxes edit this file.
    import fcntl                        # noqa: F401
except ImportError:                     # pragma: no cover - platform dependent
    fcntl = None

LEDGER = os.environ.get("MER_SENT_LEDGER", "/opt/data/scripts/mer_sent_ledger.json")

#: A send reservation left "pending" longer than this is assumed to belong to a crashed run
#: and may be re-reserved. Short enough that a real retry is not blocked for long, long enough
#: that a slow SMTP handshake is not mistaken for a crash.
PENDING_TTL_MINUTES = int(os.environ.get("MER_PENDING_TTL_MIN", "15"))

#: Coarse semantic cooldown: no second letter to the same (case, recipient) inside this window,
#: no matter how differently it is worded. THE 2026-07-28 GUARD.
DEFAULT_COOLDOWN_HOURS = float(os.environ.get("MER_SEND_COOLDOWN_HOURS", "48"))


class LedgerCorrupt(RuntimeError):
    """The ledger file exists but could not be read. NEVER treated as 'nothing happened'."""


class SendGuardError(RuntimeError):
    """A send was attempted without a valid, unconsumed reservation token."""


# ------------------------------------------------------------------ paths


def _veto_path():
    """Sibling of the ledger. Derived at CALL time so tests that rebind LEDGER get a temp veto
    file too (case_tick / nudge / send_queue all rebind idempotency.LEDGER)."""
    env = os.environ.get("MER_VETO_LEDGER")
    if env:
        return env
    return os.path.join(os.path.dirname(LEDGER) or ".", "mer_vetoed.json")


# ------------------------------------------------------------------ locking

_LOCK_DEPTH = {}          # abs lockfile path -> [open file handle, recursion depth]


@contextlib.contextmanager
def file_lock(path):
    """Exclusive advisory lock around one state file. Shared by every engine state store.

    Re-entrant WITHIN a process, PER PATH: flock() is per-file-descriptor, so a naive second
    lock from the same process on a second fd would deadlock against itself (reserve() calls
    _load() and _write_json(), both of which lock). The depth map is keyed by path rather than
    global so that holding the send-queue lock does not silently turn the ledger lock into a
    no-op — send_queue.process() legitimately touches both.

    On Windows fcntl does not exist: we yield UNLOCKED rather than crash, because this repo is
    edited on Windows. The VPS — where mer-hotpath (every minute) genuinely races the hourly
    mer-engine over the same ledger — is POSIX, so the lock is real where it matters.
    """
    if fcntl is None:
        yield
        return
    lockfile = os.path.abspath(path) + ".lock"
    slot = _LOCK_DEPTH.get(lockfile)
    if slot and slot[1] > 0:
        slot[1] += 1
        try:
            yield
        finally:
            slot[1] -= 1
        return
    d = os.path.dirname(lockfile) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    fh = open(lockfile, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            os.chmod(lockfile, 0o664)
        except OSError:
            pass
        _LOCK_DEPTH[lockfile] = [fh, 1]
        yield
    finally:
        _LOCK_DEPTH.pop(lockfile, None)
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _lock(path=None):
    """The ledger lock (or another path's). Kept private-looking for existing callers."""
    return file_lock(path or LEDGER)


# ------------------------------------------------------------------ state I/O


def _read_json(path, empty):
    """Absent -> `empty` (that is a legitimate cold start). Present-but-unreadable -> raise.

    The old code returned {} on JSONDecodeError, so one corrupt byte erased the entire
    duplicate-send history and the very next save made the erasure permanent.
    """
    if not os.path.exists(path):
        return empty
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        raise LedgerCorrupt(
            "%s exists but is not readable JSON (%s). REFUSING to proceed: treating it as empty "
            "would erase the duplicate-send history and the next write would make that permanent. "
            "Inspect it, or restore %s.bak, then retry." % (path, e, path))
    except OSError as e:
        raise LedgerCorrupt("%s exists but could not be opened (%s)." % (path, e))
    if not isinstance(data, type(empty)):
        raise LedgerCorrupt("%s holds %s, expected %s — refusing to proceed."
                            % (path, type(data).__name__, type(empty).__name__))
    return data


def _write_json(path, data):
    """Atomic, permission-stable write with a .bak of the last good version.

    mkstemp() creates 0600 owned by the writing uid; os.replace() then stamped that onto the
    ledger, so whichever uid sent last locked the other one out (root vs hermes — hit live
    2026-07-28). chmod before the replace fixes it.
    """
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    if os.path.exists(path):
        try:
            # Back up the current file ONLY if it is still readable. Backing up a corrupt file
            # would overwrite the last good copy with the very garbage the .bak exists to undo.
            with open(path, encoding="utf-8") as chk:
                json.load(chk)
            shutil.copy2(path, path + ".bak")
        except (OSError, ValueError, UnicodeDecodeError):
            pass
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o664)         # NOT 0600 — mixed-uid jobs must both stay able to write
        except OSError:
            pass
        os.replace(tmp, path)            # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _load():
    """dict — the ledger. Absent file is an empty ledger; corrupt file raises LedgerCorrupt."""
    with _lock():
        return _read_json(LEDGER, {})


def _atomic_save(data):
    with _lock():
        _write_json(LEDGER, data)


# ------------------------------------------------------------------ keys


def send_key(case, action, recipient, body):
    """Stable key for a logical send. Body is fingerprinted (whitespace-normalized)."""
    body_fp = hashlib.sha256(" ".join((body or "").split()).encode("utf-8")).hexdigest()[:12]
    raw = "|".join([str(case), str(action), (recipient or "").lower().strip(), body_fp])
    return "%s|%s|%s" % (case, action, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])


def _now():
    return datetime.now(timezone.utc)


def _parse(ts):
    try:
        d = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _is_stale_pending(entry, now=None):
    """A reservation whose process died mid-send. Re-reservable."""
    if not isinstance(entry, dict) or entry.get("state") != "pending":
        return False
    at = _parse(entry.get("at"))
    if at is None:
        return True
    return (now or _now()) - at > timedelta(minutes=PENDING_TTL_MINUTES)


def _is_live(entry, now=None):
    """True if this entry still blocks a re-send (committed, or pending and not stale)."""
    if not isinstance(entry, dict):
        return True                      # unparseable legacy entry: fail CLOSED, never re-send
    if entry.get("state") == "pending":
        return not _is_stale_pending(entry, now)
    return True


# ------------------------------------------------------------------ cooldown


def cooldown_conflict(case, recipient, hours=None, now=None, data=None):
    """The blocking entry if a letter already went to this (case, recipient) recently, else None.

    Deliberately COARSE — it ignores the body entirely. That is the whole point: the 07-28
    duplicate had a different body hash (one word of the deadline differed) and sailed straight
    through the exact-match key. Only kind="send" entries count; case_tick's board-write
    reservations (recipient "board") are not sends and never trip this.
    """
    hours = DEFAULT_COOLDOWN_HOURS if hours is None else hours
    if not hours or hours <= 0:
        return None
    now = now or _now()
    rcpt = (recipient or "").lower().strip()
    if not rcpt:
        return None
    cutoff = now - timedelta(hours=hours)
    data = _read_json(LEDGER, {}) if data is None else data
    newest = None
    for key, e in data.items():
        if not isinstance(e, dict) or e.get("kind") != "send":
            continue
        if str(e.get("case")) != str(case):
            continue
        if (e.get("recipient") or "").lower().strip() != rcpt:
            continue
        if not _is_live(e, now):
            continue
        at = _parse(e.get("at"))
        if at is None or at < cutoff:
            continue
        if newest is None or at > _parse(newest[1].get("at")):
            newest = (key, e)
    if newest is None:
        return None
    return {"key": newest[0], "at": newest[1].get("at"), "action": newest[1].get("action"),
            "recipient": newest[1].get("recipient"), "hours": hours}


# ------------------------------------------------------------------ reserve / commit / release


def _reserve(case, action, recipient, body, meta, kind, two_phase,
             cooldown_hours, override):
    key = send_key(case, action, recipient, body)
    with _lock():
        data = _read_json(LEDGER, {})
        now = _now()

        existing = data.get(key)
        if existing is not None and _is_live(existing, now):
            return {"ok": False, "key": key, "token": None,
                    "reason": "idempotency: this exact send already went out"}

        cool = None
        if kind == "send":
            cool = cooldown_conflict(case, recipient, cooldown_hours, now, data)
            if cool and not override:
                return {"ok": False, "key": key, "token": None, "cooldown": cool,
                        "reason": ("cooldown: a letter already went to %s on case %s at %s "
                                   "(within %sh). Different wording does NOT make it a "
                                   "different letter. Pass override=True to send anyway."
                                   % (cool["recipient"], case, cool["at"], cool["hours"]))}

        token = secrets.token_hex(16) if kind in ("send", "manual") else None
        entry = {
            "case": case, "action": action, "recipient": recipient,
            "at": now.isoformat(),
            "kind": kind,
            "state": "pending" if two_phase else "committed",
            **(meta or {}),
        }
        if token:
            entry["token"] = token
        if cool and override:
            # REQUIRED by the design: an override is worthless if it is not auditable.
            entry["cooldown_override"] = True
            entry["cooldown_override_of"] = cool["key"]
            entry["cooldown_override_at"] = cool["at"]
        data[key] = entry
        _write_json(LEDGER, data)
    return {"ok": True, "key": key, "token": token, "reason": "reserved"}


def reserve(case, action, recipient, body, meta=None):
    """LEGACY / non-send reservation. Returns (ok_to_proceed, key) exactly as before.

    Still the right call for the engine's non-mail idempotency (case_tick's phase_advance and
    remedy_map_build board writes): one phase, committed immediately, no cooldown, no token.
    Anything that puts mail on the wire must use reserve_send() instead.
    """
    r = _reserve(case, action, recipient, body, meta,
                 kind="board", two_phase=False, cooldown_hours=None, override=False)
    return r["ok"], r["key"]


def reserve_send(case, action, recipient, body, meta=None,
                 cooldown_hours=None, override=False):
    """Reserve an OUTBOUND EMAIL. Two-phase, cooled-down, tokenised.

    Returns {"ok", "key", "token", "reason"[, "cooldown"]}.
      ok=True   -> caller may hand `token` to gmail_transport.send_mime(), then MUST call
                   commit(key, gmail_id) on success or release(key) on failure.
      ok=False  -> caller MUST NOT send. `reason` says whether it was the exact-match key or
                   the coarse (case, recipient) cooldown.
    """
    return _reserve(case, action, recipient, body, meta,
                    kind="send", two_phase=True,
                    cooldown_hours=cooldown_hours, override=override)


def commit(key, gmail_id=None, meta=None):
    """Phase two: the transport confirmed. Freeze the reservation permanently."""
    with _lock():
        data = _read_json(LEDGER, {})
        e = data.get(key)
        if not isinstance(e, dict):
            return False
        e["state"] = "sent"
        e["committed_at"] = _now().isoformat()
        if gmail_id:
            e["gmail_id"] = gmail_id
        if meta:
            e.update(meta)
        data[key] = e
        _write_json(LEDGER, data)
    return True


def release(key):
    """Phase two, sad path: the transport failed, so give the key back.

    Without this, one SMTP hiccup made a letter permanently un-sendable — every retry with the
    same body was answered "already sent" while the ledger claimed it had gone out.
    Only a PENDING reservation can be released; a committed send is never un-sent.
    """
    with _lock():
        data = _read_json(LEDGER, {})
        e = data.get(key)
        if not isinstance(e, dict):
            return False
        if e.get("state") not in (None, "pending"):
            return False
        del data[key]
        _write_json(LEDGER, data)
    return True


def was_sent(case, action, recipient, body):
    """True if this exact logical send is on the books (committed, or pending and not stale)."""
    key = send_key(case, action, recipient, body)
    data = _load()
    e = data.get(key)
    return e is not None and _is_live(e)


# ------------------------------------------------------------------ tokens


def _find_token(data, token):
    for key, e in data.items():
        if isinstance(e, dict) and e.get("token") and e["token"] == token:
            return key, e
    return None, None


def consume_send_token(token, recipient=None):
    """Validate a reservation token and burn it. Returns the ledger key.

    Called by gmail_transport.send_mime() as the LAST gate before SMTP. Raises SendGuardError
    for a missing, unknown, already-used, or stale token. This is what makes the guard
    non-optional: there is no longer a public function that sends without one.
    """
    if not token:
        raise SendGuardError(
            "REFUSING TO SEND: no reservation token. gmail_transport.send_mime() is not a public "
            "API — it is the last mile of mer_send.send(), which reserves the send first. "
            "Call mer_send.send(...). Three duplicate vendor emails (2026-07-18, 07-25, 07-28) "
            "came from calling send_mime directly.")
    with _lock():
        data = _read_json(LEDGER, {})
        key, e = _find_token(data, token)
        if e is None:
            raise SendGuardError(
                "REFUSING TO SEND: reservation token is not in the ledger. It was never minted "
                "by idempotency.reserve_send(), or its reservation was already released.")
        if e.get("token_used_at"):
            raise SendGuardError(
                "REFUSING TO SEND: reservation token %s was already used at %s (key %s). A token "
                "is single-use — this is a duplicate send attempt."
                % (token[:8] + "...", e["token_used_at"], key))
        if e.get("state") == "pending" and _is_stale_pending(e):
            raise SendGuardError(
                "REFUSING TO SEND: reservation %s is stale (pending since %s, TTL %sm). Re-reserve."
                % (key, e.get("at"), PENDING_TTL_MINUTES))
        if recipient and e.get("kind") == "send":
            want = (e.get("recipient") or "").lower().strip()
            # test mode legitimately redirects to the profile owner's own mailbox, so a mismatch
            # is recorded rather than refused; the reservation still names the INTENDED vendor.
            if want and want != (recipient or "").lower().strip():
                e["delivered_to"] = recipient
        e["token_used_at"] = _now().isoformat()
        data[key] = e
        _write_json(LEDGER, data)
    return key


def mint_manual_token(reason, operator=None):
    """An explicit, audited escape hatch for a genuine one-off human send.

    Deliberately awkward and deliberately logged: it writes a real ledger entry with kind
    "manual", so an operator send is as visible in the ledger (and to delivery_check) as an
    engine send. It exists so that "I just need to send one thing" never becomes a reason to
    weaken send_mime's token check. Returns the token string.
    """
    r = _reserve("MANUAL", "manual_send", operator or "operator", str(reason),
                 meta={"reason": str(reason), "operator": operator},
                 kind="manual", two_phase=False, cooldown_hours=None, override=False)
    return r["token"]


# ------------------------------------------------------------------ vetoes


def record_veto(case, action, meta=None):
    """Persist a veto so it survives the record being dropped from the queue.

    THE 09:00 LOOP: a vetoed letter used to be deleted from the queue and written nowhere else,
    so the next morning's case_tick found no queue record and no ledger entry and re-enqueued the
    identical letter. Every day. Forever.
    """
    path = _veto_path()
    with _lock(path):
        data = _read_json(path, {})
        k = "%s|%s" % (case, action)
        data[k] = {"case": case, "action": action,
                   "at": _now().isoformat(), **(meta or {})}
        _write_json(path, data)
    return True


def was_vetoed(case, action=None):
    """True if this (case, action) was vetoed and the veto still stands.

    THE FUNCTION nudge.py / case_tick.py MUST CONSULT:
        idempotency.was_vetoed(case, action=None) -> bool
      case   : the case identifier, e.g. "MER-3"  (str, compared as str)
      action : the action slug, e.g. "tier2_followup". Omit / None to ask "was ANY action on
               this case vetoed?"
    Never raises for a missing veto file (a cold install has vetoed nothing). Raises
    LedgerCorrupt only if the veto file exists and is unreadable — same fail-closed rule as
    the ledger.
    """
    data = _read_json(_veto_path(), {})
    if action is not None:
        return ("%s|%s" % (case, action)) in data
    return any(isinstance(e, dict) and str(e.get("case")) == str(case) for e in data.values())


def veto_record(case, action):
    """The stored veto dict for (case, action), or None. For reporting/dashboards."""
    return _read_json(_veto_path(), {}).get("%s|%s" % (case, action))


def clear_veto(case, action):
    """Lift a veto deliberately (a human changed their mind). Returns True if one was lifted."""
    path = _veto_path()
    with _lock(path):
        data = _read_json(path, {})
        k = "%s|%s" % (case, action)
        if k not in data:
            return False
        del data[k]
        _write_json(path, data)
    return True


# ------------------------------------------------------------------ self-test

if __name__ == "__main__":
    # self-test: the same logical send is allowed once, blocked the second time
    os.environ.setdefault("MER_SENT_LEDGER",
                          os.path.join(tempfile.gettempdir(), "mer_ledger_test.json"))
    LEDGER = os.environ["MER_SENT_LEDGER"]
    for p in (LEDGER, LEDGER + ".bak", _veto_path()):
        if os.path.exists(p):
            os.remove(p)
    ok1, k1 = reserve("CASE-3", "tier1", "consumer.affairs@ppg.com", "Dear PPG, please refund...")
    ok2, k2 = reserve("CASE-3", "tier1", "consumer.affairs@ppg.com", "Dear PPG, please refund...")  # dup
    ok3, k3 = reserve("CASE-3", "tier2", "contact@ppgac.com", "Escalating to your executives...")   # new
    print("1st identical send  -> ok_to_send =", ok1, "(expect True)")
    print("2nd identical send  -> ok_to_send =", ok2, "(expect False — blocked)")
    print("different send      -> ok_to_send =", ok3, "(expect True)")
    assert ok1 and not ok2 and ok3, "IDEMPOTENCY SELF-TEST FAILED"

    # M45 — the send path: two-phase, cooldown, tokens, vetoes.
    for p in (LEDGER, LEDGER + ".bak", _veto_path()):
        if os.path.exists(p):
            os.remove(p)
    r1 = reserve_send("CASE-9", "tier1", "vendor@example.com", "Refund by Aug 4.")
    assert r1["ok"] and r1["token"], "reserve_send should mint a token"
    assert _load()[r1["key"]]["state"] == "pending", "a send reservation starts PENDING"
    release(r1["key"])
    assert _load() == {}, "release() must give the key back after a transport failure"
    print("failed send        -> reservation released =", True, "(expect True)")

    r2 = reserve_send("CASE-9", "tier1", "vendor@example.com", "Refund by Aug 4.")
    consume_send_token(r2["token"])
    commit(r2["key"], gmail_id="gid123")
    assert _load()[r2["key"]]["state"] == "sent"
    dup = reserve_send("CASE-9", "tier1", "vendor@example.com", "Refund by Aug 11.")  # reworded!
    print("reworded 2nd letter -> ok_to_send =", dup["ok"], "(expect False — 48h cooldown)")
    assert not dup["ok"] and "cooldown" in dup["reason"], "COOLDOWN SELF-TEST FAILED"
    ovr = reserve_send("CASE-9", "tier1", "vendor@example.com", "Refund by Aug 11.", override=True)
    assert ovr["ok"] and _load()[ovr["key"]]["cooldown_override"] is True, \
        "an override must be recorded in the ledger entry"
    release(ovr["key"])

    try:
        consume_send_token(None)
        raise AssertionError("send with no token must raise")
    except SendGuardError:
        pass
    print("send without token  -> refused with SendGuardError (expect refused)")

    record_veto("CASE-9", "tier2_followup")
    assert was_vetoed("CASE-9", "tier2_followup") and was_vetoed("CASE-9")
    assert not was_vetoed("CASE-9", "tier1_followup")
    print("veto                -> persists across a tick =", True, "(expect True)")

    for p in (LEDGER, LEDGER + ".bak", _veto_path()):
        if os.path.exists(p):
            os.remove(p)
    print("PASS — double-send is structurally blocked; distinct sends pass.")
