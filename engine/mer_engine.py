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
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
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


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"seen": {}}


def main():
    commit = "--commit" in sys.argv
    notify = "--notify" in sys.argv
    first_run = not os.path.exists(STATE)
    st = load_state()
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
    print("discovery: %d case(s) watchable from the board, %d skipped."
          % (len(queries), len(unwatchable)))
    if unwatchable:
        case_queries.log_skips(unwatchable)

    tg_high = []
    tg_client = []
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
            print("  %s query failed: %s" % (ident, e)); continue

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

            note = ("RECORD ONLY - NO ACTION REQUIRED [re %s]. Inbound classified: %s (%s). "
                    "Resolved=%s: %s. Subject: %s" %
                    (ident, cat, lane, res.get("resolved"), res.get("reason", "")[:120], subj))
            if commit:
                # Wake-agent safe: a comment on an issue with a LIVE AGENT assigned wakes that
                # agent and is read as an instruction (the 2026-07-18 bank-double-email lesson), so
                # a status note goes to the activity-log issue instead. With no such issue on the
                # board we fall back to the case itself — which is correct for the single-user
                # product, where cases have no agent assigned and nothing can be woken.
                target = activity_log if (issue.get("assignee_type") == "agent" and activity_log) \
                    else issue["id"]
                try:
                    mc.add_comment(target, note)
                except Exception as e:
                    print("     (board write failed: %s)" % e)
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

        seen[ident] = current

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
        json.dump(st, open(STATE, "w"), indent=2)
    print("done. %d HIGH item(s), %d client item(s)%s."
          % (len(tg_high), len(tg_client), "" if commit else " (dry-run — no writes)"))


def _telegram(text):
    env = {}
    try:
        for line in open("/opt/data/.env"):
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass                      # no shared .env (a non-VPS install) — fall back to the environment
    # M32: WHO gets notified is profile data; the bot TOKEN stays a secret in the environment and
    # never enters a profile. Same precedence as inbox_watcher and king_nag — those two already
    # read the profile here and this one did not, so a new user's HIGH-priority case alerts would
    # have gone to King's chat id or nowhere at all.
    try:
        uid = mer_config.notify_telegram_chat_id()
    except Exception:
        uid = None
    uid = uid or env.get("TELEGRAM_USER_ID") or os.environ.get("TELEGRAM_USER_ID")
    tok = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not (tok and uid):
        return
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok,
            data=json.dumps({"chat_id": uid, "text": text}).encode(),
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception:
        pass


if __name__ == "__main__":
    main()
