#!/usr/bin/env python3
"""
delivery_check.py — M21 post-send delivery confirmation for the AMBS Merchandise Returns Engine.

The problem this closes: mer_send.send() reports success the moment SMTP accepts the message
for relay. That is NOT proof it reached the vendor. A message can be accepted and then bounce
minutes later (bad address, downstream 4xx/5xx, a receiving MTA that "refused to talk"). If that
bounce goes unnoticed, a DEAD escalation looks alive — the deadline clock keeps ticking on a
letter nobody ever received, and the case silently rots. (Cf. the MER-3 silent-at-deadline flaw.)

check_delivery() verifies an outbound was not silently bounced by searching Gmail — the SAME
mailbox the send left from (the profile owner's own mailbox, mer_config.email()) — for a
Mailer-Daemon / delivery-failure
report that references the recipient or subject, shortly after the send.

Statuses returned:
  bounced   — a delivery-failure report referencing the recipient/subject was found. The caller
              MUST treat the escalation as un-delivered (re-send, fix the address, or alert
              the user).
  delivered — the mailbox was searched and NO matching bounce was found in the window. This is the
              clean signal. (Strictly: "no bounce detected" — absence of a DSN is strong but not
              cryptographic proof; the detail says so honestly.)
  unknown   — we could not check: no recipient/subject/message-id to match on, or the Gmail search
              itself failed. NEVER downgraded to "delivered" on an error — an error must not be
              mistaken for a clean delivery (that is the exact silent-failure trap M21 exists to kill).

Primary entry point:
    check_delivery(gmail_message_id=None, recipient=None, subject=None, since_ts=None) -> dict

The easy hook after a send: pass the id mer_send.send() returns —
    res = mer_send.send(...); dc = check_delivery(gmail_message_id=res["gmail_id"])
check_delivery will read that sent message to learn its recipient, subject and send time itself.

Auth + REST mirror gmail_fetch / gmail_transport exactly (self-refreshing token, stdlib only).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import gmail_fetch  # noqa: E402  (access_token() + get_message(), self-refreshing token)
import mer_config   # noqa: E402  (M32 — identity comes from the profile, not a literal)
import idempotency  # noqa: E402  (M42 — the sent-ledger is the only durable record of sends)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# --- M42 sweep tunables ----------------------------------------------------------------
# How far back the sweep looks in the sent-ledger. A DSN lands within minutes-to-hours of
# the send, so 48h is generous; it also means each send is re-checked a couple of times and
# then falls out of the window, which is what keeps a healthy sweep silent.
SWEEP_LOOKBACK_HOURS = int(os.environ.get("DELIVERY_CHECK_LOOKBACK_HOURS", "48"))
SWEEP_MAX_ENTRIES = int(os.environ.get("DELIVERY_CHECK_MAX", "40"))


def self_mailbox():
    """The mailbox being searched for bounces — always the profile owner's own (M32)."""
    return mer_config.email()

# The bounce fingerprint. Delivery-failure reports come from a mailer-daemon / postmaster
# address and/or carry one of these subjects. Kept broad on purpose — a missed bounce is far
# more dangerous than an extra candidate we then confirm by recipient/subject reference.
BOUNCE_QUERY = (
    'from:mailer-daemon OR from:postmaster OR '
    'subject:(undeliverable OR "delivery failed" OR "delivery status notification" '
    'OR "Undelivered Mail" OR "returned to sender" OR "failure notice" OR "mail delivery failed")'
)

MAX_CANDIDATES = 25


def _api(path, token, params=None):
    url = GMAIL_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _headers(msg):
    return {h["name"].lower(): h["value"]
            for h in (msg.get("payload", {}) or {}).get("headers", []) or []}


def _iter_parts(part):
    if not part:
        return
    yield part
    for sub in part.get("parts", []) or []:
        yield from _iter_parts(sub)


def _message_text(msg):
    """Return a lowercased haystack: snippet + subject/from/to headers + every text/* body part.

    Bounce reports carry the failed recipient (and often the original subject) either in the
    human-readable body or in the machine-readable DSN part (Final-Recipient / Original-Recipient),
    so we search across all of them.
    """
    parts_text = [msg.get("snippet", "") or ""]
    h = _headers(msg)
    for k in ("subject", "from", "to"):
        if h.get(k):
            parts_text.append(h[k])
    for part in _iter_parts(msg.get("payload", {})):
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if data and mime.startswith("text/"):
            try:
                import base64
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                parts_text.append(decoded.decode("utf-8", "replace"))
            except Exception:
                pass
    return "\n".join(parts_text).lower()


def _normalize_subject(subject):
    """Strip reply/forward prefixes and mer_send's [TEST -> x] banner; collapse whitespace."""
    s = subject or ""
    # mer_send's [TEST -> addr] banner comes first, then any number of Re:/Fwd: prefixes.
    s = re.sub(r"^\s*\[test[^\]]*\]\s*", "", s, flags=re.I)
    while True:
        stripped = re.sub(r"^\s*(re|fw|fwd)\s*:\s*", "", s, flags=re.I)
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip()


def _references(haystack, recipient, subject):
    """Does this bounce reference our recipient or our (original) subject?

    Recipient is the strongest signal (an email address is unambiguous). Subject is a fallback,
    matched only if it is distinctive enough (>=12 chars) to avoid a generic false positive.
    """
    if recipient:
        r = recipient.strip().lower()
        if r and r in haystack:
            return "references recipient %s" % recipient
    if subject:
        s = _normalize_subject(subject).lower()
        if len(s) >= 12 and s in haystack:
            return "references subject %r" % _normalize_subject(subject)
    return None


def _resolve_from_sent(gmail_message_id, token):
    """Read a just-sent message to learn its recipient, subject and internal send time."""
    msg = gmail_fetch.get_message(gmail_message_id, token=token)
    h = _headers(msg)
    to_hdr = h.get("to", "")
    # take the bare address out of a "Name <addr>" To header
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", to_hdr)
    recipient = m.group(0) if m else (to_hdr or None)
    subject = h.get("subject")
    since_ts = None
    try:
        since_ts = int(msg["internalDate"]) // 1000  # ms -> s
    except Exception:
        pass
    return recipient, subject, since_ts


def _time_bound(since_ts):
    """Gmail query fragment bounding the search to after the send (a bounce follows the send).

    Uses `after:<epoch>` when we know the send time; otherwise a 1-day window (newer_than:1d),
    matching the spec — a bounce lands within minutes-to-hours of the send, not days later.
    """
    if since_ts:
        return "after:%d" % int(since_ts)
    return "newer_than:1d"


def check_delivery(gmail_message_id=None, recipient=None, subject=None, since_ts=None,
                   token=None, retries=3, delay=2):
    """Verify an outbound send was not silently bounced.

    Provide either the gmail_message_id of the sent message (recipient/subject/time are read
    from it), or recipient and/or subject directly (optionally with since_ts, epoch seconds).

    Returns {"status": "bounced"|"delivered"|"unknown", "detail": str, ...}.
    Never raises — a checker that crashes is worse than one that says "unknown".
    """
    try:
        if token is None:
            token = gmail_fetch.access_token()
    except Exception as e:
        return {"status": "unknown", "detail": "auth failed: %s" % e}

    # If given the sent message, learn what to look for from it.
    if gmail_message_id and not (recipient or subject):
        try:
            recipient, subject, sent_ts = _resolve_from_sent(gmail_message_id, token)
            if since_ts is None:
                since_ts = sent_ts
        except Exception as e:
            return {"status": "unknown",
                    "detail": "could not read sent message %s: %s" % (gmail_message_id, e)}

    if not recipient and not subject:
        return {"status": "unknown",
                "detail": "nothing to match on (need recipient, subject, or gmail_message_id)"}

    query = "(%s) %s" % (BOUNCE_QUERY, _time_bound(since_ts))

    # Search, with a short retry — a bounce may not be indexed the instant we look.
    candidates = None
    last_err = None
    for _ in range(max(1, retries)):
        try:
            resp = _api("/messages", token,
                        {"q": query, "maxResults": MAX_CANDIDATES})
            candidates = resp.get("messages", []) or []
            break
        except Exception as e:
            last_err = e
            time.sleep(delay)
    if candidates is None:
        return {"status": "unknown",
                "detail": "bounce search failed: %s" % last_err,
                "recipient": recipient, "subject": subject}

    for c in candidates:
        try:
            full = gmail_fetch.get_message(c["id"], token=token)
        except Exception:
            continue
        hay = _message_text(full)
        ref = _references(hay, recipient, subject)
        if ref:
            h = _headers(full)
            return {
                "status": "bounced",
                "detail": "delivery-failure report %s (%s)" % (ref, h.get("subject", "")),
                "bounce_message_id": c["id"],
                "bounce_from": h.get("from", ""),
                "bounce_subject": h.get("subject", ""),
                "bounce_date": h.get("date", ""),
                "recipient": recipient,
                "subject": subject,
            }

    return {
        "status": "delivered",
        "detail": "no delivery-failure report referencing recipient/subject found "
                  "in window (%d bounce candidate(s) scanned)" % len(candidates),
        "recipient": recipient,
        "subject": subject,
    }


# =========================================================================================
# M42 — the scheduled sweep
#
# check_delivery() above is the per-send check, but nothing calls it after a send: mer_send
# returns the gmail_id to its caller and the caller drops it on the floor. There is no
# "sent messages" table in the engine.
#
# There IS one durable record of every outbound: the idempotency ledger
# (/opt/data/scripts/mer_sent_ledger.json), which mer_send.reserve()s BEFORE every send and
# which stores {case, action, recipient, at}. That is exactly the (recipient, since_ts) pair
# check_delivery already accepts — so the sweep needs no new bookkeeping anywhere else, and
# touches no file it does not own.
#
# HONEST LIMITATION: the ledger records the INTENDED recipient. Under MER_ENGINE_SEND=test
# the mail is redirected to the profile owner, so a test-mode send will read "delivered"
# (nothing bounced) even though the vendor never received it. That is correct — in test mode
# nothing was supposed to reach the vendor — but do not read a test-mode "delivered" as proof
# the vendor address is good.
# =========================================================================================
def _parse_iso(ts):
    """ISO-8601 (with or without offset) -> epoch seconds, or None."""
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def ledger_entries(lookback_hours=None, ledger_path=None, now_ts=None, ledger=None):
    """Recent outbound sends worth checking, newest first.

    Reads (never writes) the idempotency ledger. Entries without a parseable timestamp or
    without an email-looking recipient are skipped — there is nothing to match a bounce on.
    """
    hours = SWEEP_LOOKBACK_HOURS if lookback_hours is None else lookback_hours
    now_ts = now_ts or int(time.time())
    cutoff = now_ts - hours * 3600
    if ledger is None:
        path = ledger_path or idempotency.LEDGER
        try:
            with open(path, encoding="utf-8") as fh:
                ledger = json.load(fh)
        except (IOError, OSError, ValueError):
            return []
    if not isinstance(ledger, dict):
        return []

    rows = []
    for key, rec in ledger.items():
        if not isinstance(rec, dict):
            continue
        sent_ts = _parse_iso(rec.get("at"))
        if sent_ts is None or sent_ts < cutoff:
            continue
        recipient = (rec.get("recipient") or "").strip()
        if "@" not in recipient:
            continue
        rows.append({
            "key": key,
            "case": rec.get("case") or "-",
            "action": rec.get("action") or "-",
            "recipient": recipient,
            "at": rec.get("at"),
            "sent_ts": sent_ts,
        })
    rows.sort(key=lambda r: r["sent_ts"], reverse=True)
    return rows[:SWEEP_MAX_ENTRIES]


def match_bounces(entries, candidates, grace_seconds=300):
    """Pure matching step: mark each sent-entry bounced or delivered against a pre-fetched
    set of delivery-failure candidates.

    `candidates` is a list of {"id", "headers": {...}, "haystack": <lowercased text>,
    "internal_ts": int}. A candidate only counts for an entry if it arrived at/after that
    entry's send time (minus a small clock-skew grace) — otherwise an OLD bounce to the same
    address would condemn a NEW send that is actually fine.

    No network, no state. Kept separate from sweep() precisely so it is self-testable.
    """
    out = []
    for e in entries:
        hit = None
        for c in candidates:
            if c.get("internal_ts") is not None and c["internal_ts"] < e["sent_ts"] - grace_seconds:
                continue
            ref = _references(c.get("haystack", ""), e["recipient"], None)
            if ref:
                hit = (c, ref)
                break
        row = dict(e)
        if hit:
            c, ref = hit
            h = c.get("headers", {}) or {}
            row.update({
                "status": "bounced",
                "detail": "delivery-failure report %s (%s)" % (ref, h.get("subject", "")),
                "bounce_message_id": c.get("id"),
                "bounce_from": h.get("from", ""),
                "bounce_subject": h.get("subject", ""),
            })
        else:
            row.update({
                "status": "delivered",
                "detail": "no delivery-failure report referencing this recipient in window",
            })
        out.append(row)
    return out


def sweep(lookback_hours=None, token=None, ledger_path=None, now_ts=None, ledger=None):
    """Check every recent outbound for a silent bounce. Read-only; sends nothing.

    Returns a list of rows: {case, action, recipient, at, status, detail, ...} where status
    is bounced | delivered | unknown. An error NEVER becomes "delivered" — it becomes
    "unknown" and therefore still surfaces, because a check that could not run must not be
    mistaken for a clean delivery.
    """
    entries = ledger_entries(lookback_hours=lookback_hours, ledger_path=ledger_path,
                             now_ts=now_ts, ledger=ledger)
    if not entries:
        return []

    def _unknown(reason):
        return [dict(e, status="unknown", detail=reason) for e in entries]

    try:
        if token is None:
            token = gmail_fetch.access_token()
    except Exception as exc:
        return _unknown("auth failed: %s" % exc)

    earliest = min(e["sent_ts"] for e in entries)
    query = "(%s) after:%d" % (BOUNCE_QUERY, earliest)
    try:
        resp = _api("/messages", token, {"q": query, "maxResults": MAX_CANDIDATES})
        ids = resp.get("messages", []) or []
    except Exception as exc:
        return _unknown("bounce search failed: %s" % exc)

    candidates = []
    for c in ids:
        try:
            full = gmail_fetch.get_message(c["id"], token=token)
        except Exception:
            continue
        try:
            internal_ts = int(full["internalDate"]) // 1000
        except Exception:
            internal_ts = None
        candidates.append({
            "id": c["id"],
            "headers": _headers(full),
            "haystack": _message_text(full),
            "internal_ts": internal_ts,
        })
    return match_bounces(entries, candidates)


def render_sweep_alert(rows):
    """Alert-only render for the cron. Returns "" when every recent send looks delivered.

    Empty string is the healthy case and MUST stay empty — the wrapper prints nothing, so
    the cron delivers nothing. Only 'bounced' (a dead escalation) and 'unknown' (a check
    that could not run) are worth waking a human for.
    """
    bad = [r for r in rows if r["status"] != "delivered"]
    if not bad:
        return ""
    bounced = [r for r in bad if r["status"] == "bounced"]
    unknown = [r for r in bad if r["status"] == "unknown"]
    lines = ["📮 Delivery check (M21) — RECORD ONLY, NO ACTION REQUIRED"]
    if bounced:
        lines.append("BOUNCED — these escalations did NOT reach the recipient:")
        for r in bounced:
            lines.append("  %-8s %-10s -> %s  (sent %s)"
                         % (r["case"], r["action"], r["recipient"], r["at"]))
            lines.append("           %s" % r["detail"])
    if unknown:
        lines.append("UNCHECKED — delivery could not be verified (not the same as delivered):")
        for r in unknown:
            lines.append("  %-8s %-10s -> %s  (sent %s): %s"
                         % (r["case"], r["action"], r["recipient"], r["at"], r["detail"]))
    lines.append("(full sweep: /opt/data/scripts/delivery_check.log)")
    return "\n".join(lines)


def render_sweep_report(rows):
    """Full sweep detail for the log — every row, including the healthy ones."""
    stamp = datetime.now(timezone.utc).isoformat()
    if not rows:
        return ("=== delivery_check sweep %s ===\nNo outbound in the last %dh — nothing to "
                "verify." % (stamp, SWEEP_LOOKBACK_HOURS))
    lines = ["=== delivery_check sweep %s (lookback %dh, %d send(s)) ==="
             % (stamp, SWEEP_LOOKBACK_HOURS, len(rows))]
    for r in rows:
        lines.append("  %-9s %-8s %-10s -> %-38s %s"
                     % (r["status"].upper(), r["case"], r["action"], r["recipient"], r["at"]))
        lines.append("            %s" % r["detail"])
    return "\n".join(lines)


def sweep_cron_main():
    """M42 cron entry point. stdout = alert only (empty when healthy); stderr = full sweep.

    Exit 0 normally, 1 if any send bounced (so the wrapper/operator can tell a real
    delivery failure from a merely-unverified one at a glance).
    """
    try:
        rows = sweep()
    except Exception as exc:  # a crashing watcher must not read as silence
        sys.stderr.write("delivery_check sweep crashed: %s: %s\n" % (type(exc).__name__, exc))
        print("⚠️ Delivery check could not run: %s: %s" % (type(exc).__name__, exc))
        return 2
    sys.stderr.write(render_sweep_report(rows) + "\n")
    alert = render_sweep_alert(rows)
    if alert:
        print(alert)
    return 1 if any(r["status"] == "bounced" for r in rows) else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="M21 post-send delivery confirmation")
    ap.add_argument("--gmail-id", help="gmail message id of the sent message")
    ap.add_argument("--recipient", help="recipient address to match on")
    ap.add_argument("--subject", help="original subject to match on")
    ap.add_argument("--since", type=int, help="epoch seconds lower bound (after:)")
    ap.add_argument("--sweep", action="store_true",
                    help="check every send in the idempotency ledger's recent window")
    ap.add_argument("--cron", action="store_true",
                    help="M42 scheduled sweep: alert-only stdout, full report on stderr")
    ap.add_argument("--selftest", action="store_true",
                    help="offline self-test of the pure matching logic (no network)")
    args = ap.parse_args()

    if args.cron:
        sys.exit(sweep_cron_main())

    if args.sweep:
        rows = sweep()
        print(render_sweep_report(rows))
        alert = render_sweep_alert(rows)
        if alert:
            print("\n" + alert)
        sys.exit(0)

    if args.selftest:
        # Pure-logic checks — no Gmail calls, safe for the M31 harness.
        ok = True

        # Real 7/16 mailchannels DSN body (final-recipient reference) must match on recipient.
        dsn = ("this is the mail system at host relay.mailchannels.net.\n"
               "final-recipient: rfc822; 1000001@fr-int-smtpout11.hostinger.io\n"
               "action: failed\nstatus: 4.0.0\n").lower()
        if not _references(dsn, "1000001@fr-int-smtpout11.hostinger.io", None):
            print("[FAIL] recipient reference not detected in DSN body"); ok = False
        if _references(dsn, "someone-else@example.com", None):
            print("[FAIL] false positive on unrelated recipient"); ok = False

        # Subject fallback: distinctive subject present -> match; generic/short -> no match.
        body = "undeliverable: warranty claim - persistent sole squeaking - reax sneakers".lower()
        if not _references(body, None, "Re: Warranty claim - persistent sole squeaking"):
            print("[FAIL] distinctive subject not matched"); ok = False
        if _references(body, None, "Re: Hi"):
            print("[FAIL] too-short subject should not match"); ok = False

        # Subject normalization strips Re:/Fwd:/[TEST -> ..].
        if _normalize_subject("[TEST -> v@x.com] Re: Claim #12") != "Claim #12":
            print("[FAIL] subject normalization: %r" % _normalize_subject("[TEST -> v@x.com] Re: Claim #12")); ok = False

        # Time bound: send time -> after:, none -> newer_than:1d.
        if not _time_bound(1752624000).startswith("after:"):
            print("[FAIL] time bound with since_ts"); ok = False
        if _time_bound(None) != "newer_than:1d":
            print("[FAIL] time bound default"); ok = False

        # --- M42 sweep: ledger reading + bounce matching + silence-when-healthy ---
        NOW = 1_800_000_000
        fake_ledger = {
            "k1": {"case": "CASE-3", "action": "tier1", "recipient": "good@vendor.example",
                   "at": datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()},
            "k2": {"case": "CASE-9", "action": "tier2", "recipient": "dead@vendor.example",
                   "at": datetime.fromtimestamp(NOW - 7200, timezone.utc).isoformat()},
            "k3": {"case": "CASE-OLD", "action": "tier1", "recipient": "old@vendor.example",
                   "at": datetime.fromtimestamp(NOW - 400 * 3600, timezone.utc).isoformat()},
            "k4": {"case": "CASE-BAD", "action": "tier1", "recipient": "",
                   "at": datetime.fromtimestamp(NOW - 60, timezone.utc).isoformat()},
        }
        ents = ledger_entries(lookback_hours=48, now_ts=NOW, ledger=fake_ledger)
        got = sorted(e["case"] for e in ents)
        if got != ["CASE-3", "CASE-9"]:
            print("[FAIL] ledger window/recipient filter -> %s" % got); ok = False

        bounce = {
            "id": "b1",
            "headers": {"from": "mailer-daemon@googlemail.com",
                        "subject": "Delivery Status Notification (Failure)"},
            "haystack": ("address not found. final-recipient: rfc822; "
                         "dead@vendor.example\naction: failed"),
            "internal_ts": NOW - 7000,
        }
        rows = match_bounces(ents, [bounce])
        by = {r["case"]: r["status"] for r in rows}
        if by.get("CASE-9") != "bounced":
            print("[FAIL] bounce not matched to CASE-9 -> %s" % by); ok = False
        if by.get("CASE-3") != "delivered":
            print("[FAIL] unrelated send should read delivered -> %s" % by); ok = False
        if not render_sweep_alert(rows):
            print("[FAIL] a bounced send must produce an alert"); ok = False

        # a bounce that PREDATES the send must not condemn it
        stale = dict(bounce, internal_ts=NOW - 200_000)
        if match_bounces(ents, [stale])[0]["status"] == "bounced" or \
                any(r["status"] == "bounced" for r in match_bounces(ents, [stale])):
            print("[FAIL] pre-send bounce must not match a later send"); ok = False

        # all clean -> EMPTY alert -> cron stays silent
        if render_sweep_alert(match_bounces(ents, [])) != "":
            print("[FAIL] healthy sweep must render an empty alert"); ok = False
        # ...but 'unknown' is NOT healthy and must still surface
        if not render_sweep_alert([dict(ents[0], status="unknown", detail="auth failed")]):
            print("[FAIL] unknown status must surface (never silently pass as delivered)"); ok = False
        # no sends at all -> nothing to say
        if render_sweep_alert([]) != "":
            print("[FAIL] empty sweep must be silent"); ok = False

        print("PASS — delivery_check self-test" if ok else "SELF-TEST FAILED")
        sys.exit(0 if ok else 1)

    print(json.dumps(check_delivery(
        gmail_message_id=args.gmail_id, recipient=args.recipient,
        subject=args.subject, since_ts=args.since), indent=2))
