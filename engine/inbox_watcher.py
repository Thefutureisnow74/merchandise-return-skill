#!/usr/bin/env python3
"""
inbox_watcher.py - report-only Gmail watcher for live Multica cases.

WHY THIS EXISTS
Multica has no email watcher. A case can wait on a vendor reply forever because
nothing reads the inbox - proven 2026-07-20, when Stride Bank replied and
declined to research the trace, and NOTHING surfaced it; MER-1 still said
"result due ~Jul 31" while the counterparty had actually stopped and asked a
question. This closes that gap.

HOW
Runs on the VPS (always-on, unlike a laptop). Reads the profile owner's mailbox
with the gmail.readonly scope ALREADY present on the profile's OAuth token file
(mer_config.google_token_file(); scope + live read both verified 2026-07-20 on
King's instance), reusing gmail_transport.access_token() - the same self-refreshing
credential the send path uses. Writes new-inbound findings to
/opt/data/multica_inbound.json, which king_nag.py folds into the daily nag.

REPORT-ONLY - HARD BOUNDARY
It never sends, replies, labels, archives, or touches a Multica issue. Reading
vendor mail is one step from replying to it; that is how Stride got emailed
twice (2026-07-18). This script has no send path, by design. Keep it that way.

DEDUP
Per-case set of seen Gmail message IDs in /opt/data/inbox_watcher_state.json.
The very first run establishes a silent baseline (records what is already there,
reports nothing) so it does not flood the nag with two weeks of back-traffic.
After that it reports only genuinely new inbound mail, once each.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# import the working send-path module purely for its self-refreshing access_token()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_transport as g  # noqa: E402
import mer_config            # noqa: E402  (M32 — identity comes from the profile, not a literal)
import multica_api as mc     # noqa: E402  (M33 — the board is the case list)
import case_queries          # noqa: E402  (M33 — one shared query resolver, no local table)

STATE_FILE = "/opt/data/inbox_watcher_state.json"
OUT_FILE = "/opt/data/multica_inbound.json"
LOOKBACK = "newer_than:14d"     # bound the query so seen-sets stay small
MAX_RESULTS = 10                # per case, per run
MAX_REPORT = 15                 # cap total surfaced items so a chatty case can't flood

# M33 — CASE DISCOVERY IS NO LONGER HAND-MAINTAINED HERE.
# This file used to carry its own copy of the identifier->Gmail-query table, with a comment
# instructing whoever edited it to keep it in sync with mer_engine's copy. That is a drift bug
# with a countdown on it (the two copies had ALREADY diverged), and it meant a brand-new user's
# cases were invisible to this watcher. Both tables are deleted. The open cases and their queries
# now come from the board, through the single shared resolver in case_queries.py.
#
# The precision lessons the old table encoded are preserved IN THE RESOLVER, not in a literal:
#   * a free-mail address is watched as a full address, never widened to `from:gmail.com`;
#   * subject-scoping (which is what stopped Kim's trading mail — "4-Hour Futures Backtest
#     Results" — from leaking into her tablet case on 2026-07-20) is read from that case's own
#     `MAIL SUBJECT:` block on the board, so it is now per-case DATA that the case owner can
#     widen without a code change;
#   * a case with no derivable vendor address/domain is SKIPPED and logged, never given a broad
#     fallback query.
#
# STILL REPORT-ONLY. case_queries imports nudge for its description parser (nudge in turn imports
# the send queue), but nothing on this path is ever CALLED here: this file has no send, no reply,
# no label, no board write. Keep it that way.


def load_case_queries():
    """{display label -> Gmail query} for every open case on the board, plus the skipped ones.

    Returns (queries, skipped). `queries` is keyed by identifier so the dedup state file stays
    stable across title/vendor edits; the human-readable label is carried separately.
    """
    from collections import OrderedDict
    queries, labels, skipped = OrderedDict(), {}, []
    for cq in case_queries.resolve_cases(mc.list_issues()):
        if cq.skipped:
            skipped.append(cq)
            continue
        queries[cq.identifier] = cq.query
        labels[cq.identifier] = cq.label
    return queries, labels, skipped


def _migrate_state_keys(seen, identifiers):
    """Adopt pre-M33 state keys ('MER-1 (GHL / Stride)') onto their bare identifier.

    Without this, the first run after the refactor would see an empty seen-set for every case and
    report two weeks of already-known mail as new — a Telegram flood and a false 'new reply' alarm.
    """
    for ident in identifiers:
        if ident in seen:
            continue
        for old in list(seen):
            if old == ident or old.startswith(ident + " ") or old.startswith(ident + "("):
                seen[ident] = seen.pop(old)
                break
    return seen


def _api(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


def list_messages(query, token):
    q = urllib.parse.quote("%s %s" % (query, LOOKBACK))
    url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages"
           "?q=%s&maxResults=%d" % (q, MAX_RESULTS))
    return _api(url, token).get("messages", []) or []


def headers_of(mid, token):
    url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages/%s"
           "?format=metadata&metadataHeaders=Subject&metadataHeaders=Date"
           "&metadataHeaders=From" % mid)
    m = _api(url, token)
    return {x["name"]: x["value"] for x in m["payload"]["headers"]}


def _notify(items):
    """Ping King on Telegram the moment a vendor/client reply lands on a live case,
    so the 3-hourly run surfaces new mail within hours instead of at the next nag."""
    env = {}
    try:
        for line in open("/opt/data/.env"):
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return
    tok = env.get("TELEGRAM_BOT_TOKEN")
    # M32: the chat id is identity — the profile wins, .env is the fallback. The bot TOKEN
    # stays in .env and never goes in a profile.
    if mer_config.notify_channel() == "none":
        return
    uid = mer_config.notify_telegram_chat_id() or env.get("TELEGRAM_USER_ID")
    if not tok or not uid:
        return
    lines = ["\U0001f4e8 New mail on live cases:"]
    for m in items:
        lines.append("- %s: %s" % (m.get("case", "?"), (m.get("subject") or "(no subject)")))
    payload = json.dumps({"chat_id": uid, "text": "\n".join(lines)}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok, data=payload,
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception:
        pass


def main():
    first_run = not os.path.exists(STATE_FILE)
    state = {}
    if not first_run:
        try:
            with open(STATE_FILE) as fh:
                state = json.load(fh)
        except Exception:
            state = {}
    seen = state.get("seen", {})  # case identifier -> [gmail message ids seen in-window]

    queries, labels, skipped = load_case_queries()
    seen = _migrate_state_keys(seen, list(queries))
    if skipped:
        # Visible, never silent: these cases are open on the board but cannot be watched.
        print("inbox_watcher: %d open case(s) NOT watchable from board data:" % len(skipped))
        case_queries.log_skips(skipped)

    token = g.access_token()
    new_items = []

    for ident, query in queries.items():
        case = labels.get(ident, ident)      # human label for the report/nag
        prior = set(seen.get(ident, []))
        current_ids = []
        try:
            msgs = list_messages(query, token)
        except Exception as exc:
            print("query FAILED for %s: %s" % (case, exc))
            # keep whatever we knew before so we don't lose the baseline
            current_ids = list(prior)
            seen[ident] = current_ids
            continue
        for msg in msgs:
            mid = msg["id"]
            current_ids.append(mid)
            if mid in prior or first_run:
                continue
            try:
                h = headers_of(mid, token)
            except Exception:
                continue
            frm = h.get("From", "")
            if mer_config.email().lower() in frm.lower():
                continue  # our own sent copy in the thread - not an inbound reply
            new_items.append({
                "case": case,
                "from": frm,
                "date": h.get("Date"),
                "subject": (h.get("Subject") or "")[:140],
                "gmail_id": mid,
            })
        seen[ident] = current_ids

    truncated = max(0, len(new_items) - MAX_REPORT)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "first_run_baseline": first_run,
        "new_mail": new_items[:MAX_REPORT],
        "truncated": truncated,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    state["seen"] = seen
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)

    if not first_run and new_items:
        _notify(new_items[:MAX_REPORT])
    if first_run:
        print("inbox_watcher: first run - baseline recorded, nothing reported.")
    else:
        print("inbox_watcher: %d new inbound message(s) across %d cases -> %s"
              % (len(new_items), len(queries), OUT_FILE))


if __name__ == "__main__":
    main()
