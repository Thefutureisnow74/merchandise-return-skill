#!/usr/bin/env python3
"""
test_send_path.py — adversarial tests for the M45 send-path hardening.

Run standalone:   python test_send_path.py        (prints PASS/FAIL lines, exits non-zero on FAIL)

Each test here corresponds to a defect that was CONFIRMED against real evidence, not imagined:

  1  send_mime refuses without a reservation token       (07-18 Stride, 07-25 PPG, 07-28 RTB)
  2  the coarse (case, recipient) cooldown blocks a differently-worded second letter   (07-28)
  3  a failed transport RELEASES the reservation so the letter stays sendable
  4  the queue RETAINS an errored record instead of deleting it
  5  a veto persists across a simulated 09:00 tick
  6  reserve under a CORRUPT ledger refuses instead of silently resetting history
  7  reserve under a READ-ONLY ledger fails loudly instead of pretending it recorded
  8  two concurrent PROCESSES cannot both reserve the same send
  9  the atomic save does not reset ledger permissions to 0600
 10  MER_ENGINE_SEND rejects an unknown mode instead of degrading to "sending disabled"

SAFETY. This file cannot send mail and cannot touch live state:
  * every test runs in its own tempdir with MER_SENT_LEDGER / MER_SEND_QUEUE / MER_SEND_DEAD /
    MER_VETO_LEDGER pointed into it;
  * a module-level guard replaces smtplib.SMTP with a raiser, so even a logic bug that got past
    the token gate would blow up rather than open a socket;
  * gmail_transport.send_mime is only ever exercised for its REFUSAL, never past the gate;
  * MER_ENGINE_SEND is forced to "off"/"test" and the Multica board is never imported.
"""
import json
import os
import shutil
import smtplib
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# --- HARD SAFETY: nothing in this process may open an SMTP socket. -----------------------------
os.environ.setdefault("MER_ENGINE_SEND", "off")


class _SmtpBlocked(AssertionError):
    pass


def _no_smtp(*a, **k):
    raise _SmtpBlocked("TEST TRIED TO OPEN AN SMTP CONNECTION — the send guard leaked")


smtplib.SMTP = _no_smtp
smtplib.SMTP_SSL = _no_smtp

import idempotency  # noqa: E402


# ------------------------------------------------------------------ harness

_RESULTS = []


def check(label, cond, detail=""):
    ok = bool(cond)
    print("%-4s %s%s" % ("PASS" if ok else "FAIL", label, ("   [%s]" % detail) if detail else ""))
    _RESULTS.append((ok, label))
    return ok


class Sandbox(object):
    """A disposable directory with every engine state file redirected into it."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="mer_sendpath_test_")
        self.saved_env = {k: os.environ.get(k) for k in
                          ("MER_SENT_LEDGER", "MER_SEND_QUEUE", "MER_SEND_DEAD",
                           "MER_VETO_LEDGER", "MER_ENGINE_SEND")}
        self.ledger = os.path.join(self.dir, "ledger.json")
        self.queue = os.path.join(self.dir, "queue.json")
        os.environ["MER_SENT_LEDGER"] = self.ledger
        os.environ["MER_SEND_QUEUE"] = self.queue
        os.environ["MER_SEND_DEAD"] = os.path.join(self.dir, "dead.json")
        os.environ["MER_VETO_LEDGER"] = os.path.join(self.dir, "vetoed.json")
        self.saved_ledger_global = idempotency.LEDGER
        idempotency.LEDGER = self.ledger
        return self

    def __exit__(self, *exc):
        idempotency.LEDGER = self.saved_ledger_global
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            os.chmod(self.dir, 0o755)
            for f in os.listdir(self.dir):
                try:
                    os.chmod(os.path.join(self.dir, f), 0o644)
                except OSError:
                    pass
        except OSError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def _fresh_send_queue(sb):
    """Import send_queue with its module globals bound into this sandbox."""
    import send_queue
    send_queue.QUEUE = sb.queue
    return send_queue


# ------------------------------------------------------------------ 1. token gate


def test_send_mime_refuses_without_token():
    print("\n--- 1. gmail_transport.send_mime refuses without a reservation token ---")
    import email.mime.text

    import gmail_transport
    with Sandbox():
        msg = email.mime.text.MIMEText("body")
        msg["To"] = "vendor@example.com"
        msg["Subject"] = "s"

        # LAYER ONE: with no guard registered at all, the transport sends nothing, ever.
        prev = gmail_transport.register_send_guard(None)
        try:
            gmail_transport.send_mime(msg, to_addrs=["vendor@example.com"], token="anything")
            check("with NO guard registered, send_mime refuses outright", False, "it did not raise")
        except gmail_transport.UnguardedSendError:
            check("with NO guard registered, send_mime refuses outright", True)
        except _SmtpBlocked:
            check("with NO guard registered, send_mime refuses outright", False, "reached socket")
        finally:
            gmail_transport.register_send_guard(prev)

        import mer_send  # noqa: F401  — importing the policy layer registers the real guard
        check("importing mer_send arms the transport's guard",
              gmail_transport._SEND_GUARD is idempotency.consume_send_token)

        # LAYER TWO: with the real guard registered, only a real token gets through.
        try:
            gmail_transport.send_mime(msg, to_addrs=["vendor@example.com"])
            check("send_mime() with NO token refuses", False, "it did not raise")
        except idempotency.SendGuardError as e:
            check("send_mime() with NO token refuses", True, str(e)[:52] + "...")
        except _SmtpBlocked:
            check("send_mime() with NO token refuses", False, "it reached the socket")

        try:
            gmail_transport.send_mime(msg, to_addrs=["vendor@example.com"], token="deadbeef")
            check("send_mime() with a FORGED token refuses", False, "it did not raise")
        except idempotency.SendGuardError:
            check("send_mime() with a FORGED token refuses", True)
        except _SmtpBlocked:
            check("send_mime() with a FORGED token refuses", False, "it reached the socket")

        # A real token works exactly once. We let it through the gate and then let the SMTP
        # guard stop it, which proves the gate passed and nothing else did.
        r = idempotency.reserve_send("T", "a", "vendor@example.com", "body")
        try:
            gmail_transport.send_mime(msg, to_addrs=["vendor@example.com"], token=r["token"])
            check("a VALID token passes the gate", False, "expected the smtp guard to fire")
        except _SmtpBlocked:
            check("a VALID token passes the gate", True, "stopped at the smtp guard, as designed")
        except idempotency.SendGuardError as e:
            check("a VALID token passes the gate", False, str(e)[:60])
        except Exception as e:                      # identity/config errors are fine here too
            check("a VALID token passes the gate", "SendGuard" not in type(e).__name__,
                  type(e).__name__)

        try:
            gmail_transport.send_mime(msg, to_addrs=["vendor@example.com"], token=r["token"])
            check("the SAME token cannot be used twice", False, "it did not raise")
        except idempotency.SendGuardError:
            check("the SAME token cannot be used twice", True)
        except _SmtpBlocked:
            check("the SAME token cannot be used twice", False, "it reached the socket AGAIN")

        check("send_mime's docstring no longer advertises itself as the API",
              "Do not call this" in (gmail_transport.send_mime.__doc__ or ""))
        check("the module docstring points callers at mer_send.send()",
              "mer_send.send()" in (gmail_transport.__doc__ or ""))


# ------------------------------------------------------------------ 2. cooldown


def test_cooldown_blocks_reworded_second_letter():
    print("\n--- 2. the 48h cooldown blocks a differently-worded second letter (the 07-28 hole) ---")
    with Sandbox():
        a = "Please refund $482. Respond by August 4, 2026."
        b = "Please refund $482. Respond by August 11, 2026."   # ONE date differs
        k1 = idempotency.send_key("MER-7", "tier1", "care@relaxtheback.com", a)
        k2 = idempotency.send_key("MER-7", "tier1", "care@relaxtheback.com", b)
        check("the two letters really do have DIFFERENT exact keys (the old guard's blind spot)",
              k1 != k2)

        r1 = idempotency.reserve_send("MER-7", "tier1", "care@relaxtheback.com", a)
        idempotency.consume_send_token(r1["token"])
        idempotency.commit(r1["key"], gmail_id="g1")

        r2 = idempotency.reserve_send("MER-7", "tier1", "care@relaxtheback.com", b)
        check("the reworded second letter is BLOCKED by the cooldown", r2["ok"] is False,
              r2["reason"][:60])
        check("...and the block names the cooldown, not the exact key",
              "cooldown" in r2["reason"] and r2.get("cooldown"))
        check("...and no token was minted for it", r2["token"] is None)

        # a DIFFERENT recipient on the same case is unaffected
        r3 = idempotency.reserve_send("MER-7", "tier2", "ceo@relaxtheback.com", b)
        check("a different recipient on the same case is NOT blocked", r3["ok"] is True)
        idempotency.release(r3["key"])

        # a different CASE to the same recipient is unaffected
        r4 = idempotency.reserve_send("MER-9", "tier1", "care@relaxtheback.com", b)
        check("a different case to the same recipient is NOT blocked", r4["ok"] is True)
        idempotency.release(r4["key"])

        # override is allowed but must leave a trace
        r5 = idempotency.reserve_send("MER-7", "tier1", "care@relaxtheback.com", b, override=True)
        check("override=True is honoured", r5["ok"] is True)
        entry = idempotency._load()[r5["key"]]
        check("...and the override is RECORDED in the ledger entry",
              entry.get("cooldown_override") is True and entry.get("cooldown_override_of"),
              json.dumps({k: entry[k] for k in entry if "cooldown" in k}))
        idempotency.release(r5["key"])

        # a window of 0 disables the cooldown for callers who ask explicitly
        r6 = idempotency.reserve_send("MER-7", "tier1", "care@relaxtheback.com", b,
                                      cooldown_hours=0)
        check("cooldown_hours=0 disables the coarse guard (exact key still applies)",
              r6["ok"] is True)
        idempotency.release(r6["key"])


# ------------------------------------------------------------------ 3. release on failure


def test_reservation_released_on_transport_failure():
    print("\n--- 3. a failed transport RELEASES the reservation (the letter stays sendable) ---")
    with Sandbox():
        os.environ["MER_ENGINE_SEND"] = "test"
        import gmail_transport
        import mer_send
        real = gmail_transport.send_mime
        calls = []

        def _boom(msg, to_addrs=None, token=None, **k):
            calls.append(token)
            idempotency.consume_send_token(token)     # the real transport consumes, then fails
            raise RuntimeError("SMTP 421 service not available")

        mer_send.gmail_transport.send_mime = _boom
        mer_send.self_mailbox = lambda: "owner@example.com"
        try:
            res = mer_send.send("vendor@example.com", "subj", "the body",
                                case="MER-1", action="tier1")
            check("a raising transport reports sent=False", res.get("sent") is False,
                  str(res.get("reason"))[:50])
            check("...and says it released the reservation", res.get("released") is True)
            check("...and the ledger no longer claims the letter went",
                  idempotency._load() == {}, str(idempotency._load())[:60])
            check("...so was_sent() is False and a retry is possible",
                  idempotency.was_sent("MER-1", "tier1", "vendor@example.com", "the body")
                  is False)

            res2 = mer_send.send("vendor@example.com", "subj", "the body",
                                 case="MER-1", action="tier1")
            check("the retry gets a FRESH reservation (not 'already sent')",
                  "already went out" not in str(res2.get("reason")), str(res2.get("reason"))[:50])
            check("...and a different single-use token", len(calls) == 2 and calls[0] != calls[1])
        finally:
            mer_send.gmail_transport.send_mime = real


# ------------------------------------------------------------------ 4. queue retains errors


def test_queue_retains_errored_record():
    print("\n--- 4. the queue RETAINS an errored record (it used to vanish entirely) ---")
    with Sandbox() as sb:
        import mer_send
        sq = _fresh_send_queue(sb)
        real = mer_send.send

        def _err(*a, **k):
            return {"sent": False, "reason": "send error: SMTP 550 mailbox unavailable"}
        mer_send.send = _err
        try:
            rid = sq.enqueue("MER-4", "vendor@example.com", "s", "b",
                             action="tier1", window_hours=0)
            sq.process()
            live = [r for r in sq._load() if r["id"] == rid]
            check("the errored record is still in the queue", len(live) == 1)
            check("...with attempts=1", live and live[0]["attempts"] == 1)
            check("...and last_error recorded", live and "550" in (live[0]["last_error"] or ""))
            check("...and a backoff scheduled", live and live[0]["next_attempt_at"])

            # inside the backoff it is not retried at all
            rows = sq.process()
            row = [r for r in rows if r[0] == rid]
            check("inside the backoff window it is not retried",
                  row and "backing off" in str(row[0][2]), str(row[:1])[:60])

            # burn the attempts: it must end in dead letters, never in nothing
            for _ in range(sq.MAX_ATTEMPTS + 2):
                sq._save([dict(r, next_attempt_at=None) for r in sq._load()])
                sq.process()
            check("after MAX_ATTEMPTS it leaves the live queue",
                  all(r["id"] != rid for r in sq._load()))
            dead = sq.dead_letters()
            check("...and lands in the DEAD-LETTER file", any(r["id"] == rid for r in dead))
            check("...with the body intact so it can be requeued",
                  any(r["id"] == rid and r.get("body") == "b" for r in dead))
            check("requeue_dead() puts it back", sq.requeue_dead(rid) is True)
            check("...into the live queue with a clean slate",
                  any(r["id"] == rid and not r.get("attempts") for r in sq._load()))

            # an idempotency/cooldown refusal is also preserved, not discarded
            def _blocked(*a, **k):
                return {"sent": False, "reason": "idempotency: cooldown: already wrote them"}
            mer_send.send = _blocked
            bid = sq.enqueue("MER-5", "vendor@example.com", "s", "b2",
                             action="tier1", window_hours=0)
            sq.process()
            check("a guard-blocked record is NOT silently deleted",
                  any(r["id"] == bid for r in sq.dead_letters()))
            check("...and is not left looping in the live queue",
                  all(r["id"] != bid for r in sq._load()))
        finally:
            mer_send.send = real


# ------------------------------------------------------------------ 5. veto persists


def test_veto_persists_across_a_tick():
    print("\n--- 5. a veto survives the record being dropped (the 09:00 re-enqueue loop) ---")
    with Sandbox() as sb:
        import mer_send
        sq = _fresh_send_queue(sb)
        real = mer_send.send
        mer_send.send = lambda *a, **k: {"sent": False,
                                         "reason": "MER_ENGINE_SEND=off (sending disabled)"}
        try:
            rid = sq.enqueue("MER-2", "vendor@example.com", "Tier 2 escalation", "the body",
                             action="tier2_followup", window_hours=0)
            sq.veto(rid)
            sq.process()
            check("the vetoed record is gone from the queue",
                  all(r["id"] != rid for r in sq._load()))

            # SIMULATE THE NEXT MORNING'S TICK: a brand-new process state, nothing in memory.
            check("was_vetoed(case, action) is True the next morning",
                  idempotency.was_vetoed("MER-2", "tier2_followup") is True)
            check("was_vetoed(case) with no action answers for the whole case",
                  idempotency.was_vetoed("MER-2") is True)
            check("an un-vetoed action on the same case is False",
                  idempotency.was_vetoed("MER-2", "tier1_followup") is False)
            check("an un-vetoed case is False", idempotency.was_vetoed("MER-77") is False)

            rec = idempotency.veto_record("MER-2", "tier2_followup")
            check("the veto record keeps enough context to explain itself",
                  rec and rec.get("to") == "vendor@example.com" and rec.get("at"),
                  json.dumps(rec)[:70] if rec else "None")
            check("clear_veto() can lift it deliberately",
                  idempotency.clear_veto("MER-2", "tier2_followup") is True
                  and idempotency.was_vetoed("MER-2", "tier2_followup") is False)
            check("clear_veto() on an unknown veto returns False",
                  idempotency.clear_veto("MER-2", "nope") is False)
        finally:
            mer_send.send = real


# ------------------------------------------------------------------ 6. corrupt ledger


def test_reserve_under_a_corrupt_ledger():
    print("\n--- 6. a CORRUPT ledger refuses to proceed (it used to reset the whole history) ---")
    with Sandbox() as sb:
        r = idempotency.reserve_send("MER-6", "tier1", "vendor@example.com", "b")
        idempotency.commit(r["key"], gmail_id="g")
        check("a real send is on the books", len(idempotency._load()) == 1)

        with open(sb.ledger, "w") as fh:
            fh.write('{"half a record": ')          # truncated mid-write, the classic crash
        try:
            idempotency._load()
            check("reserve/_load refuses a corrupt ledger", False, "it returned silently")
        except idempotency.LedgerCorrupt as e:
            check("reserve/_load refuses a corrupt ledger", True, str(e)[:44] + "...")

        try:
            idempotency.reserve_send("MER-6", "tier1", "vendor@example.com", "b2")
            check("reserve_send() refuses rather than re-sending blind", False, "it returned")
        except idempotency.LedgerCorrupt:
            check("reserve_send() refuses rather than re-sending blind", True)

        check("the last good ledger is preserved at .bak", os.path.exists(sb.ledger + ".bak"))
        bak = json.load(open(sb.ledger + ".bak"))
        check("...and the .bak still contains the real send", len(bak) == 1, str(list(bak))[:40])

        # a MISSING ledger is a legitimate cold start, not corruption
        os.remove(sb.ledger)
        check("a MISSING ledger is an empty ledger, not an error", idempotency._load() == {})
        check("...and a send can be reserved on a cold install",
              idempotency.reserve_send("MER-6", "tier1", "vendor@example.com", "b3")["ok"])

        # a wrong-shaped-but-valid-JSON ledger is also corruption
        with open(sb.ledger, "w") as fh:
            fh.write("[1, 2, 3]")
        try:
            idempotency._load()
            check("a wrong-TYPE ledger is refused too", False, "it returned")
        except idempotency.LedgerCorrupt:
            check("a wrong-TYPE ledger is refused too", True)


# ------------------------------------------------------------------ 7. read-only ledger


def test_reserve_under_a_read_only_ledger():
    print("\n--- 7. a READ-ONLY ledger fails loudly (a silent failure would allow a re-send) ---")
    with Sandbox() as sb:
        idempotency._atomic_save({})
        if os.name == "nt":
            check("read-only ledger test skipped on Windows (POSIX perms not enforced)", True)
            return
        if os.geteuid() == 0:
            check("read-only ledger test skipped as root (root ignores the mode bits)", True)
            return
        os.chmod(sb.dir, 0o555)             # the DIRECTORY is what mkstemp needs
        try:
            try:
                idempotency.reserve_send("MER-8", "tier1", "vendor@example.com", "b")
                check("reserve_send() on an unwritable ledger raises", False,
                      "it returned ok — the caller would have sent believing it was recorded")
            except (OSError, IOError, idempotency.LedgerCorrupt) as e:
                check("reserve_send() on an unwritable ledger raises", True,
                      "%s: %s" % (type(e).__name__, str(e)[:34]))
            check("...and nothing was recorded", idempotency._load() == {})
        finally:
            os.chmod(sb.dir, 0o755)


# ------------------------------------------------------------------ 8. concurrency


# WHY THE CHILD PATCHES os.replace ON WINDOWS (and only on Windows)
# ------------------------------------------------------------------
# idempotency._atomic_save() writes a temp file and os.replace()s it over the ledger. On POSIX
# — the deployment target — that rename is genuinely atomic and never fails because of another
# process. On Win32 it CANNOT rename over a file another process holds open, so with two
# children deliberately racing the same ledger, roughly one run in five died with
# PermissionError inside _atomic_save; the child produced no stdout and the test failed at
# "both children ran". That is a filesystem artifact of the dev box, not a defect in the
# invariant under test.
#
# So the child retries the rename briefly, IN THE TEST HARNESS, on Windows only. Nothing in
# idempotency.py changes and no assertion is relaxed: "exactly ONE of two racing processes may
# send" still runs unmodified wherever fcntl exists. This only ensures both racers survive long
# enough to be judged. (Chosen over skipping test 8 on Windows the way tests 7 and 9 do,
# because a whole-test skip would also drop the platform-independent file_lock re-entrancy and
# read-check-write-inside-one-lock assertions at the end of this test, which are real coverage
# and do pass here.)
_WIN_REPLACE_RETRY = '''
if os.name == "nt":
    _real_replace = os.replace
    def _replace_retry(src, dst, *a, **k):
        for _i in range(200):
            try:
                return _real_replace(src, dst, *a, **k)
            except PermissionError:
                time.sleep(0.005)
        return _real_replace(src, dst, *a, **k)
    os.replace = _replace_retry
'''

CHILD = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, %(here)r)
    os.environ["MER_SENT_LEDGER"] = %(ledger)r
""") + _WIN_REPLACE_RETRY + textwrap.dedent("""
    import idempotency
    idempotency.LEDGER = %(ledger)r
    barrier = %(barrier)r
    while not os.path.exists(barrier):      # start both children as close together as possible
        time.sleep(0.005)
    r = idempotency.reserve_send("RACE", "tier1", "vendor@example.com", "identical body")
    print(json.dumps({"ok": r["ok"], "reason": r["reason"]}))
""")


def test_concurrent_reserve_from_two_processes():
    print("\n--- 8. two PROCESSES racing the same reservation: exactly one wins ---")
    with Sandbox() as sb:
        barrier = os.path.join(sb.dir, "go")
        script = os.path.join(sb.dir, "child.py")
        with open(script, "w") as fh:
            fh.write(CHILD % {"here": HERE, "ledger": sb.ledger, "barrier": barrier})
        env = dict(os.environ, MER_SENT_LEDGER=sb.ledger, MER_ENGINE_SEND="off")
        procs = [subprocess.Popen([sys.executable, script], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env) for _ in range(2)]
        open(barrier, "w").close()
        outs = []
        for p in procs:
            so, se = p.communicate(timeout=60)
            outs.append((so.decode().strip(), se.decode().strip()))
        oks = []
        for so, se in outs:
            if not so:
                check("both children ran", False, se[-120:])
                return
            oks.append(json.loads(so.splitlines()[-1])["ok"])

        if idempotency.fcntl is None:
            # HONEST REPORTING, NOT A GREEN TICK. flock is POSIX-only and this repo is edited on
            # Windows; the module degrades to no-lock there BY DESIGN (crashing the whole engine
            # on a dev box would be worse). So on Windows we assert only what is actually
            # guaranteed — that the degraded path still does not crash and still does not
            # scribble over the ledger — and we say plainly that the mutual-exclusion property
            # itself is unverified here. It IS verified when this file runs on the VPS.
            check("[windows] the no-lock fallback does not crash either racer", len(oks) == 2,
                  str(oks))
            check("[windows] the ledger is still valid JSON with one reservation",
                  len(idempotency._load()) == 1, str(len(idempotency._load())))
            print("     NOTE: fcntl is unavailable on this platform, so BOTH racers were allowed "
                  "(%s). Mutual exclusion is NOT verified here — run this file on the POSIX VPS "
                  "to verify it." % oks)
        else:
            check("exactly ONE of two racing processes may send", oks.count(True) == 1, str(oks))
            check("...and the loser is told why", oks.count(False) == 1, str(oks))
            check("...and the ledger holds exactly one reservation",
                  len(idempotency._load()) == 1, str(len(idempotency._load())))

        # Platform-independent: the properties the lock RELIES on.
        depth_seen = []
        with idempotency.file_lock(sb.ledger):
            with idempotency.file_lock(sb.ledger):          # re-entrant: must not self-deadlock
                with idempotency.file_lock(sb.queue):       # a DIFFERENT path: must not no-op
                    depth_seen.append(True)
                idempotency._atomic_save(idempotency._load())   # nested lock via the public API
        check("file_lock is re-entrant per path and never self-deadlocks", depth_seen == [True])
        check("...and releases cleanly (a later lock still works)",
              _lock_roundtrip(sb.ledger))
        src = open(os.path.join(HERE, "idempotency.py"), encoding="utf-8").read()
        body = src.split("def _reserve(", 1)[1].split("\ndef ", 1)[0]
        check("_reserve's read-check-write is INSIDE one lock (not read-then-write)",
              "with _lock():" in body
              and body.index("with _lock():") < body.index("_read_json(LEDGER")
              and body.index("with _lock():") < body.index("_write_json(LEDGER"))


def _lock_roundtrip(path):
    try:
        with idempotency.file_lock(path):
            pass
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 9. permissions


def test_atomic_save_keeps_group_writable_permissions():
    print("\n--- 9. the atomic save does not reset the ledger to 0600 (root vs hermes lockout) ---")
    with Sandbox() as sb:
        if os.name == "nt":
            check("permission test skipped on Windows (POSIX mode bits not enforced)", True)
            return
        idempotency._atomic_save({"a": {"case": "x"}})
        mode = os.stat(sb.ledger).st_mode & 0o777
        check("the ledger is group-writable after a save (0664, not mkstemp's 0600)",
              mode & 0o060 == 0o060, oct(mode))
        idempotency.reserve_send("MER-P", "tier1", "vendor@example.com", "b")
        mode2 = os.stat(sb.ledger).st_mode & 0o777
        check("...and stays group-writable after every subsequent send", mode2 & 0o060 == 0o060,
              oct(mode2))

        import send_queue
        send_queue.QUEUE = sb.queue
        send_queue._save([{"id": "x"}])
        qmode = os.stat(sb.queue).st_mode & 0o777
        check("the send QUEUE is group-writable too", qmode & 0o060 == 0o060, oct(qmode))


# ------------------------------------------------------------------ 10. modes


def test_mode_validation_and_veto_mode():
    print("\n--- 10. MER_ENGINE_SEND: veto is implemented, an unknown value is rejected ---")
    with Sandbox() as sb:
        import mer_send
        saved = os.environ.get("MER_ENGINE_SEND")
        try:
            os.environ["MER_ENGINE_SEND"] = "veto"
            check("'veto' is a recognised mode", mer_send.mode() == "veto")
            res = mer_send.send("vendor@example.com", "s", "b", case="MER-V", action="a")
            check("in veto mode a DIRECT send is refused", res.get("sent") is False,
                  str(res.get("reason"))[:52])
            check("...and NOT with the 'sending disabled' reason that made the queue hoard "
                  "letters forever", "sending disabled" not in str(res.get("reason")))
            check("...and nothing was reserved", idempotency._load() == {})

            os.environ["MER_ENGINE_SEND"] = "lvie"        # a typo
            try:
                mer_send.mode()
                check("an unknown mode is REJECTED", False, "it was accepted")
            except mer_send.ModeError as e:
                check("an unknown mode is REJECTED", True, str(e)[:44] + "...")

            os.environ["MER_ENGINE_SEND"] = "off"
            res = mer_send.send("vendor@example.com", "s", "b", case="MER-O", action="a")
            check("'off' still refuses with the historic reason string",
                  res.get("sent") is False and "sending disabled" in res.get("reason", ""))
        finally:
            if saved is None:
                os.environ.pop("MER_ENGINE_SEND", None)
            else:
                os.environ["MER_ENGINE_SEND"] = saved

        # the stale-backlog guard: an old letter must never fire
        os.environ["MER_ENGINE_SEND"] = "off"
        sq = _fresh_send_queue(sb)
        rid = sq.enqueue("MER-OLD", "vendor@example.com", "ancient", "b",
                         action="a", window_hours=0)
        q = sq._load()
        for r in q:
            if r["id"] == rid:
                r["send_after"] = (datetime.now(timezone.utc)
                                   - timedelta(hours=sq.MAX_SEND_AGE_HOURS + 5)).isoformat()
        sq._save(q)
        rows = sq.process()
        row = [r for r in rows if r[0] == rid]
        check("a letter far past its window is flagged STALE and never sent",
              row and "STALE" in str(row[0][2]), str(row[:1])[:60])
        check("...and is left visible for redrafting, not deleted",
              any(r["id"] == rid and r.get("needs_redraft") for r in sq._load()))


# ------------------------------------------------------------------ main

TESTS = [
    test_send_mime_refuses_without_token,
    test_cooldown_blocks_reworded_second_letter,
    test_reservation_released_on_transport_failure,
    test_queue_retains_errored_record,
    test_veto_persists_across_a_tick,
    test_reserve_under_a_corrupt_ledger,
    test_reserve_under_a_read_only_ledger,
    test_concurrent_reserve_from_two_processes,
    test_atomic_save_keeps_group_writable_permissions,
    test_mode_validation_and_veto_mode,
]


def main():
    print("=== test_send_path.py — M45 send-path hardening (offline; cannot send mail) ===")
    for t in TESTS:
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check("%s completed without crashing" % t.__name__, False,
                  "%s: %s" % (type(e).__name__, e))
    bad = [lbl for ok, lbl in _RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(_RESULTS), len(bad)))
    if bad:
        print("FAIL — %s" % "; ".join(bad))
        return 1
    print("PASS — the send path refuses to send twice, refuses to send unguarded, refuses to "
          "lose a letter, and refuses to proceed on corrupt state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
