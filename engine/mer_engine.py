#!/usr/bin/env python3
"""
mer_engine.py — the live inbound->action orchestrator (Blueprint M6).

Ties the tested pieces into one loop, run hourly on the 24/7 VPS:
  detect new inbound on a live case  (Gmail, per-case query DERIVED FROM THE BOARD (M33), dedup)
   -> read body + PDF attachments      (gmail_fetch + pdf_text)
   -> classify                         (classify_llm, heuristic fallback)
   -> resolution-check                 (resolution_check — is it a real resolution or a dodge?)
   -> log a RECORD-ONLY classification to the board (wake-agent safe) + correct case state
   -> surface HIGH items to Telegram; queue routine follow-ups
   -> [SEND is GATED] draft is prepared; actual vendor send only when MER_ENGINE_SEND is
      'veto'/'live' AND the idempotency guard passes AND the lane permits. Default 'off'.

Modes:
  (default)        DRY-RUN — detect+classify+print a plan. No board writes, no Telegram, no send.
  --commit         write the RECORD-ONLY classification + state flags to Multica.
  --notify         push HIGH items to King's Telegram.
  MER_ENGINE_SEND  env: off (default) | veto | live  — controls the outbound send path only.

The SEND path is deliberately off by default: this is the one irreversible step. Flipping it is a
config change (MER_ENGINE_SEND=veto) once the loop is proven — not a code change.
"""
import base64
import email.utils
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import heartbeat                           # noqa: E402  (M46 — liveness ledger + alarm bell)
import gmail_transport                     # noqa: E402  send path (gated)
import gmail_fetch                         # noqa: E402
import pdf_text                            # noqa: E402
import classify_llm                        # noqa: E402
import resolution_check                    # noqa: E402
import idempotency                         # noqa: E402
import multica_api as mc                   # noqa: E402
import draft_reply                         # noqa: E402
import send_queue                          # noqa: E402
import case_queries                        # noqa: E402  (M33 — discovery comes from the board)
import mer_config                          # noqa: E402  (M32 — identity from the profile)
import re as _re                           # noqa: E402

STATE = os.environ.get("MER_ENGINE_STATE", "/opt/data/mer_engine_state.json")
SEND_MODE = os.environ.get("MER_ENGINE_SEND", "off").lower()   # off | test | veto | live
WINDOW_H = int(os.environ.get("MER_ENGINE_WINDOW_HOURS", "3"))
LOOKBACK = "newer_than:14d"
MAX_PER_CASE = 8

# M46 — how many message ids to remember PER CASE. `seen` used to be REPLACED every run with
# the <=8 ids the current `newer_than:14d` query returned, so an id that fell out of the
# window was forgotten and, if the thread later surfaced it again, was reprocessed: a second
# classification comment on the board and, in the send lanes, a second drafted reply to the
# vendor. It is now a bounded UNION — freshest first, capped — so a busy thread cannot push
# an already-handled message back into "new", and the file still cannot grow without limit.
SEEN_MAX = 250

# M46 — the board property that records that a vendor actually answered. Nothing wrote this,
# which meant the escalation hold gate was blind: MER-76's vendor replied 2026-07-27 and the
# board recorded nothing, so the case would have auto-escalated over a live conversation.
# The property is a DATE. Autoresponders and delivery receipts must never set it; a brush-off
# ("we've escalated internally") is a real reply and DOES set it — deciding whether that
# holds the gate is the READING side's job, not this one's.
VENDOR_REPLY_PROP = os.environ.get("MER_VENDOR_REPLY_PROPERTY", "MR Last Vendor Reply")

# M46 — how long a tracked case may sit with no monitoring query before it becomes an ALARM
# rather than a log line. Three of six live cases were silently unmonitored for days because
# no vendor address was on the record; case_queries computed a perfectly good reason string
# and the engine printed it into a log nobody reads.
SKIP_ALARM_SECONDS = float(os.environ.get("MER_SKIP_ALARM_SECONDS", 24 * 3600))

# M33 — case discovery. The hand-maintained identifier->query table that used to live here (and
# again, verbatim, in inbox_watcher.py) is DELETED. Every case's Gmail query is derived at runtime
# from that case's own board record by case_queries.resolve(), so a brand-new user's cases are
# discovered with zero code edits and the two copies can no longer drift apart.
#
# CASE_QUERIES survives only as a lazy, board-derived VIEW: unmatched_review.py (M17) imports this
# name to build its "matched set". It resolves on first access, not on import, and degrades to {}
# if the board is unreachable — exactly the failure mode unmatched_review already handles.
CASE_QUERIES = case_queries.live_case_queries()

HIGH = {"refused", "legal_threat", "discrimination_signal", "refund"}
YELLOW = {"needs_info", "partial"}

# Cases whose inbound mail comes from the USER'S OWN CLIENT, not from the vendor. The draft/send
# path assumes the sender is the vendor being pursued; on a client-intake case that assumption
# inverts and the engine aims a vendor-style letter back at the client (caught in the 2026-07-26
# test pass: a vendor-style reply signed in the CLIENT's own name was queued TO that client).
# Client mail is therefore NEVER
# auto-replied — it is surfaced to the operator, who answers in their own voice.
#
# M39: the hardcoded {"MER-4": ..., "MER-21": ...} roster that used to live here is DELETED. It was
# operator scaffolding — one particular person's two client-intake cases — and in the shipped
# single-user product every case is the user's own, so a shipped roster of someone else's case IDs is
# both useless and a privacy leak. Detection is now pure CONVENTION and carries no per-user data:
#
#   1. the `CLIENT:` title prefix   (the convention case_tick / sol_watchdog already key off)
#   2. a `CLIENT CASE` description marker (how the case records themselves already open)
#   3. any board property whose NAME mentions "client" and whose value is affirmative
#      (e.g. an "MR Client Case" checkbox/select) — opt-in, absent by default, no schema required
#
# The test is deliberately FAIL-SAFE / one-way: any single signal is enough to mark a case as a
# client case. A false positive costs only a human writing one reply by hand; a false negative aims
# a vendor letter at the user's own client. Widen this before you ever narrow it.
_CLIENT_TITLE_PREFIX = "CLIENT:"
_CLIENT_DESC_MARKER = "CLIENT CASE"
_AFFIRMATIVE = {"yes", "true", "y", "1", "client", "client case", "on", "checked"}


def _affirmative(val):
    if val is True:
        return True
    if val is False or val is None:
        return False
    return str(val).strip().lower() in _AFFIRMATIVE


def is_client_case(ident, issue):
    """True if this case's inbound mail comes from the operator's client rather than the vendor.

    `ident` is retained for call-site compatibility and logging; it is deliberately NOT consulted —
    no case identifier is special-cased any more (M39).
    """
    issue = issue or {}
    if (issue.get("title") or "").strip().upper().startswith(_CLIENT_TITLE_PREFIX):
        return True
    if (issue.get("description") or "").lstrip().upper().startswith(_CLIENT_DESC_MARKER):
        return True
    for name, val in (issue.get("mr") or {}).items():
        if "client" in str(name).lower() and _affirmative(val):
            return True
    return False


# Where a status note goes when the case itself has a live agent assigned (see the call site).
# M39: this used to be the hardcoded identifier "MER-16" — one issue on ONE person's board. A
# fresh user's board has no MER-16, so the sink silently resolved to None and every note fell back
# to the case. Harmless there (a single-user board has no agents to wake) but it is exactly the
# kind of literal that makes a "portable" product quietly behave differently for its author than
# for everyone else. Resolved by CONVENTION instead, with an env override for an odd board.
ACTIVITY_LOG_TITLES = ("activity log", "case activity log", "engine activity log", "case log")


def _activity_log_issue(issues):
    """Issue id to use as the wake-agent-safe log sink, or None if the board has no such issue."""
    want = os.environ.get("MER_ACTIVITY_LOG_ISSUE")
    if want:
        for it in issues.values():
            if want in (it.get("identifier"), it.get("id")):
                return it["id"]
    for it in issues.values():
        title = (it.get("title") or "").strip().lower()
        if any(title.startswith(t) or t in title for t in ACTIVITY_LOG_TITLES):
            return it["id"]
    return None


def _walk(payload):
    stack = [payload]
    while stack:
        p = stack.pop()
        yield p
        for c in (p.get("parts") or []):
            stack.append(c)


def body_text(msg):
    out = []
    for p in _walk(msg.get("payload", {})):
        if p.get("mimeType") == "text/plain":
            data = (p.get("body") or {}).get("data")
            if data:
                out.append(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace"))
    return "\n".join(out).strip()


def attachment_text(mid, token):
    txt = []
    try:
        for a in gmail_fetch.list_pdf_attachments(mid, token=token):
            try:
                raw = gmail_fetch.fetch_attachment(mid, a["attachment_id"], token=token)
                t = pdf_text.extract_text(raw)
                if t:
                    txt.append("[PDF %s]\n%s" % (a.get("filename", "?"), t))
            except Exception:
                pass
    except Exception:
        pass
    return "\n".join(txt)


class StateCorrupt(RuntimeError):
    """The state file exists but cannot be trusted.

    THE BUG THIS TYPE EXISTS TO KILL (M46): load_state() returned {"seen": {}} on ANY error,
    while `first_run` was computed separately from whether the file EXISTED. So a corrupt
    state file produced first_run=False (the file is there) with an empty seen-set — and
    first_run=False is precisely the flag that says "act on everything you find". Every
    message in the 14-day window on every case would have been reprocessed, re-commented,
    and in the send lanes re-drafted to every vendor at once.

    Absent is fine and means "establish a baseline". Corrupt is a refusal.
    """


def load_state(path=None):
    """-> (state, status) where status is 'absent' (fine) or 'ok'. Raises on corrupt."""
    path = path or STATE
    if not os.path.exists(path):
        return {"seen": {}}, "absent"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise StateCorrupt("%s is unreadable (%s)" % (path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("seen", {}), dict):
        raise StateCorrupt("%s is not the expected shape (want {'seen': {case: [ids]}})" % path)
    data.setdefault("seen", {})
    return data, "ok"


def merge_seen(prior, current, cap=SEEN_MAX):
    """Bounded union: everything in `current`, then anything from `prior` not already there.

    Order matters — freshest first — because the cap truncates the TAIL, and the ids most
    likely to reappear in the next 14-day window are the newest ones.
    """
    out = []
    for mid in list(current) + list(prior or []):
        if mid not in out:
            out.append(mid)
    return out[:cap]


# --------------------------------------------------- is this a REAL reply from a human?

# Headers a well-behaved autoresponder or a mail system sets on itself. RFC 3834's
# Auto-Submitted is the standards-track one; the X-* headers are what the field actually
# uses. Any of these means "no human composed this".
_AUTO_HEADERS = {
    "auto-submitted": lambda v: v.strip().lower() != "no",
    "x-autoreply": lambda v: True,
    "x-autorespond": lambda v: True,
    "x-auto-response-suppress": lambda v: True,
    "precedence": lambda v: v.strip().lower() in ("auto_reply", "bulk", "list", "junk"),
}

# Local-parts that never belong to a person who is answering you.
_ROBOT_LOCALPARTS = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
                     "do_not_reply", "mailer-daemon", "postmaster", "bounce", "bounces")

# Subject shapes that are a machine talking, not a vendor answering. Kept SHORT and specific:
# every entry here is a way to LOSE a real reply, so the bar for adding one is high.
_AUTO_SUBJECTS = (
    "out of office", "out-of-office", "automatic reply", "auto-reply", "autoreply",
    "auto: ", "delivery status notification", "undeliverable", "undelivered mail",
    "returned mail", "mail delivery failed", "mail delivery subsystem",
    "read receipt", "read: ", "delivery receipt", "message delivery notification",
)


def is_autoresponder(hdr, body="", content_type=""):
    """(True, why) if this inbound was generated by a machine rather than composed by a person.

    Deliberately asymmetric to the client-case test above it: a FALSE POSITIVE here throws
    away the evidence that a vendor replied, which is exactly the signal the escalation hold
    gate needs. So this only fires on unambiguous machine markers — headers, robot senders,
    and bounce/out-of-office subjects — and never on the CONTENT of a reply. A brush-off is
    a reply.
    """
    low = {str(k).lower(): str(v) for k, v in (hdr or {}).items()}
    for name, test in _AUTO_HEADERS.items():
        v = low.get(name)
        if v is not None and test(v):
            return True, "%s: %s" % (name, v.strip()[:40])
    frm = low.get("from", "").lower()
    for lp in _ROBOT_LOCALPARTS:
        if lp + "@" in frm:
            return True, "sender is a no-reply/daemon address"
    subj = low.get("subject", "").lower()
    for pat in _AUTO_SUBJECTS:
        if pat in subj:
            return True, "subject looks automated (%r)" % pat
    ct = (content_type or low.get("content-type") or "").lower()
    if "report-type=delivery-status" in ct or "message/delivery-status" in ct:
        return True, "delivery status report (DSN)"
    if low.get("x-failed-recipients"):
        return True, "bounce (X-Failed-Recipients)"
    return False, ""


def reply_date(hdr, now=None):
    """The date to stamp on the case, as YYYY-MM-DD, from the message's own Date header.

    Falls back to today (UTC) rather than to nothing: a reply whose Date header is missing or
    unparseable still happened, and 'we know a vendor replied but not when' is a worse state
    for the hold gate than 'today'.
    """
    raw = (hdr or {}).get("Date") or (hdr or {}).get("date")
    if raw:
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).date().isoformat()
        except Exception:
            pass
    return (now or datetime.now(timezone.utc)).date().isoformat()


def _alert(text, key, cooldown=None):
    kw = {"key": key}
    if cooldown is not None:
        kw["cooldown"] = cooldown
    try:
        return heartbeat.alert(text, **kw)
    except Exception as exc:                    # the alarm bell must never be the crash
        print("  (ALERT FAILED: %s) %s" % (exc, text[:200]))
        return False, str(exc)


def check_skips(st, unwatchable, tracked, monitored, now=None, alerter=None):
    """The alarm for defect 5. Returns a list of human-readable problems (also alerted).

    Two independent checks, because they fail differently:
      * a per-case timer — a case that has produced no monitoring query for over 24h is a
        case nobody is watching, and it names the case AND the missing field;
      * a COUNTER INVARIANT — tracked == monitored, asserted every single tick. The timer
        can be defeated by a case that is skipped, fixed, and skipped again; the invariant
        cannot, because it is a statement about right now.
    """
    now = now or datetime.now(timezone.utc)
    alerter = alerter or _alert
    problems = []
    skips = st.setdefault("skips", {})
    still = set()

    for cq in unwatchable:
        ident = getattr(cq, "identifier", None) or str(cq)
        reason = getattr(cq, "reason", "") or "no reason recorded"
        still.add(ident)
        rec = skips.get(ident)
        if not isinstance(rec, dict) or not rec.get("first_seen"):
            rec = {"first_seen": now.isoformat(), "reason": reason, "alerted": False}
            skips[ident] = rec
        rec["reason"] = reason
        rec["last_seen"] = now.isoformat()
        try:
            first = datetime.fromisoformat(str(rec["first_seen"]).replace("Z", "+00:00"))
        except Exception:
            first = now
            rec["first_seen"] = now.isoformat()
        age = (now - first).total_seconds()
        if age >= SKIP_ALARM_SECONDS:
            msg = ("%s has produced NO monitoring query for %.0f hours. Nothing is watching "
                   "for a vendor reply on that case. Missing on the record: %s"
                   % (ident, age / 3600.0, reason))
            problems.append(msg)
            alerter("\U0001f6a8 Unmonitored case\n" + msg +
                    "\nFix: add the vendor's email address or domain to the case record on "
                    "the board, then this clears itself on the next tick.",
                    "unmonitored:%s" % ident, 12 * 3600)

    for ident in [k for k in skips if k not in still]:
        skips.pop(ident, None)                 # fixed — stop counting, stop alerting

    if monitored != tracked:
        msg = ("COUNTER INVARIANT BROKEN: %d case(s) tracked, %d monitored — %d case(s) are "
               "open on the board with nothing watching their mail: %s"
               % (tracked, monitored, tracked - monitored,
                  ", ".join(sorted(still)) or "(unnamed)"))
        problems.append(msg)
        alerter("⚠️ merchandise-return: " + msg, "invariant:tracked-vs-monitored", 6 * 3600)
    return problems


def main(note=None):
    note = note or (lambda text: None)
    commit = "--commit" in sys.argv
    notify = "--notify" in sys.argv
    st, status = load_state()
    first_run = status == "absent"
    seen = st.get("seen", {})

    try:
        _self_email = (mer_config.email() or "").strip().lower()
    except Exception:
        _self_email = ""

    token = gmail_transport.access_token()
    issues = {it["identifier"]: it for it in mc.list_issues()}
    activity_log = _activity_log_issue(issues)

    print("=== mer_engine  %s  (%s%s, send=%s) ==="
          % (datetime.now(timezone.utc).isoformat(),
             "COMMIT" if commit else "DRY-RUN",
             "+NOTIFY" if notify else "", SEND_MODE))
    if first_run:
        print("first run — establishing baseline (no actions).")

    # M33 — derive each live case's Gmail query from its own board record. A case whose record
    # carries no vendor address/domain is SKIPPED with a visible line: it is never given a broad
    # fallback query, because an over-broad query pulls unrelated mail into a case and gets it
    # classified (and, in the send lanes, replied to) as if it belonged there.
    queries, unwatchable = case_queries.resolve_all(issues.values())
    tracked = len(queries) + len(unwatchable)
    print("discovery: %d case(s) tracked, %d monitored, %d skipped."
          % (tracked, len(queries), len(unwatchable)))
    if unwatchable:
        case_queries.log_skips(unwatchable)
    # M46 — a skipped case is no longer only a log line. Over 24h it is an ALARM naming the
    # case and the missing field, and tracked != monitored is an alarm every single tick.
    for problem in check_skips(st, unwatchable, tracked, len(queries)):
        print("  !! %s" % problem)
        note(problem)

    tg_high = []
    tg_client = []
    query_failures = []
    board_write_failures = []
    vendor_replies = {}          # ident -> the latest substantive reply date seen this run
    for ident, query in queries.items():
        issue = issues.get(ident)
        if not issue or (issue.get("status") in ("done", "cancelled")):
            continue
        prior = set(seen.get(ident, []))
        current = []
        try:
            q = "%s %s" % (query, LOOKBACK)
            import urllib.parse
            url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages?q=%s&maxResults=%d"
                   % (urllib.parse.quote(q), MAX_PER_CASE))
            import urllib.request
            req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
            msgs = json.loads(urllib.request.urlopen(req, timeout=25).read()).get("messages", []) or []
        except Exception as e:
            # A per-case query failure used to be print + continue, which is how a dead Gmail
            # token turned into a run that finished and printed "done. 0 HIGH item(s)". It is
            # now a recorded failure: the run cannot report itself green (see the guard in
            # __main__), and `seen` for this case is deliberately left untouched below.
            print("  %s query failed: %s" % (ident, e))
            query_failures.append("%s: %s" % (ident, e))
            continue

        for m in msgs:
            mid = m["id"]
            current.append(mid)
            if mid in prior or first_run:
                continue
            try:
                full = gmail_fetch.get_message(mid, token=token)
            except Exception:
                continue
            hdr = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            frm = hdr.get("From", "")
            # M32/M33: the self-address filter is identity, so it comes from the profile.
            if _self_email and _self_email in frm.lower():
                continue  # our own sent copy
            auto, auto_why = is_autoresponder(hdr)
            body = body_text(full)
            atext = attachment_text(mid, token)
            cls = classify_llm.classify(body, atext)
            cat = cls.get("category", "other")
            remedy = "the refund/replacement/exchange demanded on %s" % (issue.get("title") or ident)
            res = resolution_check.is_resolved(remedy, body + "\n" + atext, use_llm=True)
            lane = "RED" if cat in HIGH else ("YELLOW" if cat in YELLOW else "LOW")
            subj = hdr.get("Subject", "(no subject)")[:80]

            print("  %s  [%s / %s]  from %s" % (ident, cat, lane, frm[:40]))
            print("     subj: %s" % subj)
            print("     resolved=%s — %s" % (res.get("resolved"), res.get("reason", "")[:90]))
            if atext:
                print("     (read %d chars of PDF attachment)" % len(atext))

            # ---- M46: RECORD THAT A VENDOR ACTUALLY REPLIED --------------------------------
            # The one thing the escalation hold gate needs and nothing was writing. A machine
            # -generated message (out-of-office, DSN, read receipt, no-reply sender) is NOT a
            # reply and must not hold the gate; a brush-off is, and does.
            if auto:
                print("     (autoresponder — %s; NOT recorded as a vendor reply)" % auto_why)
            elif not is_client_case(ident, issue):
                d = reply_date(hdr)
                if d > vendor_replies.get(ident, ""):
                    vendor_replies[ident] = d

            board_note = ("RECORD ONLY - NO ACTION REQUIRED [re %s]. Inbound classified: %s (%s). "
                          "Resolved=%s: %s. Subject: %s" %
                          (ident, cat, lane, res.get("resolved"),
                           res.get("reason", "")[:120], subj))
            if commit:
                # Wake-agent safe: a comment on an issue with a LIVE AGENT assigned wakes that
                # agent and is read as an instruction (the 2026-07-18 bank-double-email lesson), so
                # a status note goes to the activity-log issue instead. With no such issue on the
                # board we fall back to the case itself — which is correct for the single-user
                # product, where cases have no agent assigned and nothing can be woken.
                target = activity_log if (issue.get("assignee_type") == "agent" and activity_log) \
                    else issue["id"]
                try:
                    mc.add_comment(target, board_note)
                except Exception as e:
                    # Was a bare print. A board that is not accepting writes means the case
                    # record is drifting away from what actually happened, silently — the
                    # MER-76 failure mode exactly.
                    print("     (board write failed: %s)" % e)
                    board_write_failures.append("%s comment: %s" % (ident, e))
            if cat in HIGH:
                tg_high.append("%s: %s — %s" % (ident, cat, subj))

            # --- draft + queue (yellow, veto-window auto-send) / surface (red, King confirms) ---
            if is_client_case(ident, issue):
                # The sender is our client, not a vendor. Never auto-reply in the client's own name;
                # surface so King answers personally.
                print("     -> CLIENT MAIL — surfaced for King, never auto-replied")
                tg_client.append("%s: client wrote — %s" % (ident, subj))
            elif res.get("resolved"):
                print("     -> RESOLVED — surface for close (no reply queued)")
            elif SEND_MODE in ("test", "veto", "live"):
                mm = _re.search(r"[\w.+-]+@[\w.-]+", frm)
                vendor_email = mm.group(0) if mm else None
                if lane == "YELLOW" and vendor_email:
                    # Give the drafter the case's OWN record as context — its deadline, prior-send
                    # dates, vendor and item. draft_reply feeds `context` into BOTH the prompt and
                    # the grounding corpus (M38), so this widens what the reply may legitimately
                    # cite without ever widening what counts as an invented fact. Without it the
                    # drafter can only echo the vendor's own words back and cannot reference the
                    # deadline we actually set.
                    _p = issue.get("mr", {}) or {}
                    case_ctx = "\n".join(x for x in (
                        (issue.get("description") or "")[:2000],
                        "MR Phase: %s" % _p.get("MR Phase", ""),
                        "MR Phase Deadline: %s" % _p.get("MR Phase Deadline", ""),
                    ) if x and not x.endswith(": "))
                    d = draft_reply.draft(issue.get("title") or ident, cat,
                                          body + "\n" + atext, context=case_ctx)
                    if d:
                        rid = send_queue.enqueue(
                            ident, vendor_email, "Re: " + subj, d["body"],
                            action="reply_%s" % cat, window_hours=WINDOW_H,
                            in_reply_to=hdr.get("Message-ID"))
                        print("     -> DRAFTED + queued (%s); auto-sends in %sh unless vetoed [id %s]"
                              % (cat, WINDOW_H, rid))
                    else:
                        print("     -> draft unavailable; surfacing for a manual reply")
                elif lane == "RED":
                    print("     -> RED (%s): needs your review — surfaced, NOT auto-sent" % cat)

        # M46: bounded UNION, not replacement. See SEEN_MAX.
        seen[ident] = merge_seen(seen.get(ident, []), current)

    # ---- M46: write "MR Last Vendor Reply" ------------------------------------------------
    # One write per case per run, after the loop, so a thread with four new messages produces
    # one board write carrying the newest date rather than four.
    if commit and vendor_replies:
        for ident, when in sorted(vendor_replies.items()):
            issue = issues.get(ident)
            if not issue:
                continue
            existing = str((issue.get("mr") or {}).get(VENDOR_REPLY_PROP) or "")[:10]
            if existing and existing >= when:
                print("  %s vendor reply %s already recorded (board has %s)"
                      % (ident, when, existing))
                continue
            try:
                mc.set_properties(issue, {VENDOR_REPLY_PROP: when})
                print("  %s -> %s = %s" % (ident, VENDOR_REPLY_PROP, when))
            except ValueError as e:
                # The board has no such property. Not fatal — but it means the hold gate is
                # blind on this board, so say so once rather than every run forever.
                print("  (%s not written: %s)" % (VENDOR_REPLY_PROP, e))
                _alert("⚠️ merchandise-return: cannot record vendor replies.\n%s\n"
                       "Add a DATE property named %r to the workspace, or set "
                       "MER_VENDOR_REPLY_PROPERTY to the name you use. Until then the "
                       "escalation hold gate cannot see that a vendor answered."
                       % (e, VENDOR_REPLY_PROP), "vendor-reply-prop-missing", 24 * 3600)
            except Exception as e:
                print("  (%s write failed for %s: %s)" % (VENDOR_REPLY_PROP, ident, e))
                board_write_failures.append("%s %s: %s" % (ident, VENDOR_REPLY_PROP, e))
    elif vendor_replies:
        print("  (dry-run) would record %s on: %s"
              % (VENDOR_REPLY_PROP,
                 ", ".join("%s=%s" % kv for kv in sorted(vendor_replies.items()))))

    if notify and not first_run:
        if tg_high:
            _telegram("\U0001f6a8 Case mail needs you:\n" + "\n".join("- " + x for x in tg_high))
        if tg_client:
            # A client waiting on a reply is the failure mode that let one sit unanswered for 2 days
            # (2026-07-26) because nothing alerted on client mail.
            _telegram("\U0001f4ec A client is waiting on you (no auto-reply is ever sent on client "
                      "cases):\n" + "\n".join("- " + x for x in tg_client))

    st["seen"] = seen
    if commit or first_run:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=2)
        os.replace(tmp, STATE)      # atomic: a crash mid-write is what produces a corrupt file

    # ---- M46: the run cannot report itself green if anything actually failed ---------------
    if query_failures:
        total = len(queries)
        allfail = total and len(query_failures) == total
        head = ("EVERY per-case mailbox query failed (%d/%d). No vendor reply is being seen "
                "on ANY case." % (len(query_failures), total)) if allfail else \
               ("%d of %d per-case mailbox queries failed." % (len(query_failures), total))
        note(head)
        print("!! " + head)
        auth = any(("401" in f or "403" in f or "invalid_grant" in f.lower()
                    or "unauthorized" in f.lower()) for f in query_failures)
        _alert("\U0001f6a8 merchandise-return engine: %s%s\n\n%s"
               % (head,
                  "\nThis is the signature of an EXPIRED GMAIL OAUTH TOKEN — re-authorise the "
                  "mailbox." if (auth or allfail) else "",
                  "\n".join(query_failures[:8])),
               "gmail-query-fail:%s" % ("all" if allfail else "partial"), 3 * 3600)
    if board_write_failures:
        note("%d board write(s) failed" % len(board_write_failures))
        _alert("\U0001f6a8 merchandise-return engine: %d board write(s) FAILED. The case "
               "records no longer match what actually happened.\n\n%s"
               % (len(board_write_failures), "\n".join(board_write_failures[:8])),
               "board-write-fail", 3 * 3600)

    print("done. %d HIGH item(s), %d client item(s), %d vendor reply date(s)%s."
          % (len(tg_high), len(tg_client), len(vendor_replies),
             "" if commit else " (dry-run — no writes)"))
    return 0


def _telegram(text):
    """Operator-facing notification.

    Was: build the message, try to POST it, `except Exception: pass`. A failed alert was
    therefore indistinguishable from a delivered one — the alarm bell could be broken and the
    engine would never know. It now goes through heartbeat.alert(), which falls back to a
    second transport, prints the failure, writes it to the unbounded error log, and records a
    failed beat for `heartbeat-alert` so `heartbeat.py --check` reports "the alerter itself
    is down" on its next pass.

    force=True: these are per-item case notifications, not repeated health warnings, so they
    must never be swallowed by the de-duplication cooldown.
    """
    sent, why = heartbeat.alert(text, key="mer-engine-notify", force=True)
    if not sent:
        print("  (telegram NOT delivered: %s)" % why)
    return sent


# ------------------------------------------------------------------------------- selftest

def _selftest():
    """Offline. No mailbox, no board, no LLM, no send — the pure decision functions only."""
    import shutil
    import tempfile

    fails = []

    def ck(name, cond, detail=""):
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               "" if cond else "  <- " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="mer_engine_selftest_")
    print("mer_engine --selftest  (offline)")
    print("-" * 70)

    print("A. seen-state is a BOUNDED UNION, not a replacement (M46 / defect 7)")
    ck("ids that fell out of the query window are still remembered",
       merge_seen(["old1", "old2"], ["new1"]) == ["new1", "old1", "old2"],
       merge_seen(["old1", "old2"], ["new1"]))
    ck("no duplicates", merge_seen(["a", "b"], ["b", "c"]) == ["b", "c", "a"],
       merge_seen(["a", "b"], ["b", "c"]))
    big = merge_seen([str(i) for i in range(1000)], ["x"], cap=5)
    ck("the union is capped so the file cannot grow forever", len(big) == 5, big)
    ck("the cap keeps the FRESHEST ids", big[0] == "x", big)
    ck("an id already seen never becomes new again",
       "b" in merge_seen(merge_seen([], ["a", "b"]), ["c"]))

    print("B. absent state vs CORRUPT state (M46 / defect 7)")
    p = os.path.join(tmp, "state.json")
    stt, status = load_state(p)
    ck("an absent state file is 'absent' and gives an empty seen-set",
       status == "absent" and stt == {"seen": {}}, (status, stt))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"seen": {"MER-X": ["m1"]}}, fh)
    stt, status = load_state(p)
    ck("a good state file loads", status == "ok" and stt["seen"]["MER-X"] == ["m1"], stt)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    raised = False
    try:
        load_state(p)
    except StateCorrupt:
        raised = True
    ck("a CORRUPT state file raises instead of silently yielding an empty seen-set", raised)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"seen": ["not", "a", "dict"]}, fh)
    raised = False
    try:
        load_state(p)
    except StateCorrupt:
        raised = True
    ck("a wrong-shaped state file also raises", raised)

    print("C. autoresponders and receipts are not vendor replies (M46 / defect 3)")
    for hdr, why in (
            ({"From": "svc@vendor.com", "Subject": "Automatic reply: your case"}, "OOO subject"),
            ({"From": "svc@vendor.com", "Subject": "Out of Office"}, "out of office"),
            ({"From": "noreply@vendor.com", "Subject": "Ticket 55 updated"}, "no-reply sender"),
            ({"From": "MAILER-DAEMON@vendor.com", "Subject": "hi"}, "daemon"),
            ({"From": "a@v.com", "Subject": "hi", "Auto-Submitted": "auto-replied"}, "RFC 3834"),
            ({"From": "a@v.com", "Subject": "hi", "Precedence": "bulk"}, "bulk"),
            ({"From": "a@v.com", "Subject": "Delivery Status Notification (Failure)"}, "DSN"),
            ({"From": "a@v.com", "Subject": "Read: our demand letter"}, "read receipt"),
            ({"From": "a@v.com", "Subject": "hi", "X-Failed-Recipients": "x@y.com"}, "bounce")):
        ck("machine mail is rejected (%s)" % why, is_autoresponder(hdr)[0] is True, hdr)
    for hdr, why in (
            ({"From": "Jane <jane@vendor.com>", "Subject": "Re: your refund request"}, "real"),
            ({"From": "esc@vendor.com", "Subject": "Re: refund"}, "brush-off"),
            ({"From": "a@v.com", "Subject": "hi", "Auto-Submitted": "no"}, "explicit no")):
        ck("a human reply is NOT rejected (%s)" % why, is_autoresponder(hdr)[0] is False, hdr)
    ck("a brush-off body is still a reply",
       is_autoresponder({"From": "e@v.com", "Subject": "Re: refund"},
                        "We have escalated this internally.")[0] is False)

    print("D. the reply date comes from the message, not from the clock")
    ck("a Date header is used",
       reply_date({"Date": "Mon, 27 Jul 2026 14:03:11 -0500"}) == "2026-07-27",
       reply_date({"Date": "Mon, 27 Jul 2026 14:03:11 -0500"}))
    ck("a missing Date falls back to today, never to nothing",
       reply_date({}, now=datetime(2026, 7, 28, tzinfo=timezone.utc)) == "2026-07-28")
    ck("an unparseable Date falls back to today",
       reply_date({"Date": "yesterday-ish"},
                  now=datetime(2026, 7, 28, tzinfo=timezone.utc)) == "2026-07-28")

    print("E. an unmonitored case is an ALARM, not a log line (M46 / defect 5)")

    class FakeCQ(object):
        def __init__(self, ident, reason):
            self.identifier, self.reason = ident, reason

    alerts = []

    def cap(text, key, cooldown=None):
        alerts.append(key)
        return True, "captured"

    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    st = {}
    skipped = [FakeCQ("MER-A", "no vendor address or domain on the record")]
    probs = check_skips(st, skipped, tracked=3, monitored=2, now=now, alerter=cap)
    ck("a brand-new skip does not alarm yet (it may be minutes old)",
       not any(k.startswith("unmonitored:") for k in alerts), alerts)
    ck("but the COUNTER INVARIANT alarms immediately",
       "invariant:tracked-vs-monitored" in alerts and len(probs) == 1, (alerts, probs))
    del alerts[:]
    later = now + timedelta(hours=25)
    probs = check_skips(st, skipped, tracked=3, monitored=2, now=later, alerter=cap)
    ck("after 24h the case itself alarms, by name",
       "unmonitored:MER-A" in alerts, alerts)
    ck("the alarm names the missing field",
       any("no vendor address or domain" in p for p in probs), probs)
    del alerts[:]
    probs = check_skips(st, [], tracked=3, monitored=3, now=later, alerter=cap)
    ck("a fixed case stops alarming and is forgotten",
       not alerts and not probs and st.get("skips") == {}, (alerts, probs, st))
    st2 = {}
    ck("tracked == monitored with zero skips is silent",
       check_skips(st2, [], tracked=0, monitored=0, now=now, alerter=cap) == [])

    print("F. structural — the notifier can no longer fail silently")
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    body = src.split("def _selftest")[0]
    tg = body.split("def _telegram")[1].split("\ndef ")[0]
    # Its docstring QUOTES the old `except Exception: pass` to explain what was wrong, so the
    # check must look at code, not prose — a comment describing a bug is not the bug.
    tg_code = tg.split('"""')[-1]
    ck("_telegram no longer swallows its own failure",
       "except Exception" not in tg_code, tg_code)
    ck("_telegram routes through the heartbeat alerter", "heartbeat.alert" in tg)
    ck("_telegram reports an undelivered alert to its caller", "not sent" in tg)
    ck("the entry point is wrapped in a heartbeat guard", "heartbeat.guard(" in src)
    ck("state is written atomically", "os.replace(tmp, STATE)" in body)

    shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 70)
    if fails:
        print("SELF-TEST FAILED: %s" % ", ".join(fails))
        return 1
    print("PASS — mer_engine self-test green (offline; no mailbox, no board, no send).")
    return 0


def _run(note):
    try:
        return main(note)
    except StateCorrupt as exc:
        # REFUSE. Running with an empty seen-set and first_run=False would reprocess every
        # message in the 14-day window on every case and re-draft to every vendor at once.
        msg = ("\U0001f6a8 merchandise-return engine REFUSED TO RUN: its state file is "
               "corrupt.\n%s\n\nNothing was classified, commented or queued. Inspect the file; "
               "if it is unrecoverable, DELETE it — an absent state file is handled correctly "
               "(it establishes a fresh baseline and acts on nothing)." % exc)
        print(msg)
        heartbeat.log_error("mer-engine", msg)
        _alert(msg, "state-corrupt", 3600)
        note("state file corrupt — refused to run")
        return 2


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    # M46 — every tick writes {name, ts, ok, err}, whether it succeeded, failed, or crashed.
    # A crash also alerts with the traceback and a named diagnosis (Multica 401, dead Google
    # refresh token, network). This is what makes "the engine has not run since Tuesday" and
    # "the engine ran and did nothing" two different, both-detectable states.
    sys.exit(heartbeat.guard(
        "mer-engine", _run, expect_seconds=5400.0,
        window={"hours": [13, 24], "dows": [0, 1, 2, 3, 4]}))
