#!/usr/bin/env python3
"""case_digest.py — the cross-runtime status digest (M50).

WHY THIS FILE EXISTS
--------------------
Three things went wrong on 2026-07-28 and all three were the same thing: **nobody could see the
whole picture from one place.**

1. Vendor replies on two live cases sat unanswered for ~27 hours. They were found only because a
   session happened to run an ad-hoc Gmail search while working an unrelated case.
2. A session reported a case as neglected when another runtime had in fact answered it within 90
   minutes — because the session could see the board and its own sends, and nothing else.
3. A store manager received three copies of one letter, because two runtimes each knew only what
   they themselves had sent.

The board says what the *state* is. The local ledger says what *this runtime* did. Neither answers
the question that actually matters: **what has happened on my cases, by anyone, and what is now
waiting on me?**

This module answers that, from the two sources that are shared by every actor:
  * the Multica board  — the case state, phase and deadline;
  * the Gmail mailbox  — every message IN and OUT, whoever sent it.

It reads the MAILBOX for outbound, deliberately, not the ledger. The ledger is per-runtime and
that is exactly the blindness being fixed (see idempotency M48).

STRICTLY READ-ONLY. It never writes to the board, never sends, never touches the ledger. A
reporting tool that can act is a reporting tool that can cause an incident.

CLI
    case_digest.py --selftest        offline, stubbed, no network
    case_digest.py                   print the digest
    case_digest.py --markdown        markdown (for a handoff file or an email body)
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")

LOOKBACK_DAYS = int(os.environ.get("MER_DIGEST_LOOKBACK_DAYS", "7"))

# ---------------------------------------------------------------------------------------------
# M49/E20 — the digest must never tell the user to make a phone call.
#
# The user handles every counterparty in writing. On 2026-07-28 an assistant put "call Branch,
# 833-548-0319" in front of him three times as *the* urgent next step, which parked the
# fastest-decaying item on that case behind the one thing he does not do. E20 recorded the rule
# and honestly marked it unenforced. Here it is enforced, in the one place that emits next-actions.
# ---------------------------------------------------------------------------------------------
_PHONE_ACTION_RE = re.compile(
    r"\b(?:call|phone|ring|dial|telephone)\b(?!\s+(?:log|record|notes?|history|centre|center))"
    r"|\bgive\s+(?:them|him|her|us)\s+a\s+call\b|\bon\s+the\s+phone\b", re.I)


class PhoneActionError(RuntimeError):
    """An action line told the user to phone somebody. E20: everything is done in writing."""


def assert_no_phone_action(line):
    """Raise if an action line asks the user to make a call. Returns the line unchanged."""
    if _PHONE_ACTION_RE.search(line or ""):
        raise PhoneActionError(
            "action line asks the user to telephone somebody, which E20 forbids "
            "(everything is handled in writing): %r" % line)
    return line


# ---------------------------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def _epoch_ms(v):
    try:
        return datetime.fromtimestamp(int(v) / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def gather(issues=None, fetch_inbound=None, fetch_outbound=None, now=None):
    """-> a list of per-case dicts. Injectable fetchers keep the self-test offline.

    fetch_inbound(query, days)  -> [{"from","subject","at"}]   messages FROM the counterparty
    fetch_outbound(query, days) -> [{"to","subject","at"}]     messages the MAILBOX sent
    """
    import case_queries
    now = now or _now()
    if issues is None:
        import multica_api as mc
        issues = mc.list_issues()

    if fetch_inbound is None or fetch_outbound is None:
        fetch_inbound, fetch_outbound = _gmail_fetchers()

    # M50 BUGFIX 2026-07-29 — `CaseQuery` has no `.issue` attribute and never did, so the old
    # `cq.issue if hasattr(...) else None` silently resolved to None for EVERY case. Result: every
    # phase and deadline rendered blank, and the digest then printed "0 past deadline · 0 awaiting
    # a reply" — a FALSE ALL-CLEAR, on a board that had a deadline two days out.
    #
    # This is the worst failure a status tool can have: it did not crash, it did not warn, it
    # reported health it had not checked. Caught only by reading real output against a board known
    # to have deadlines. The lesson is the one this whole engine keeps relearning — an absent value
    # must never render as a reassuring one.
    by_ident = {it.get("identifier"): it for it in (issues or []) if isinstance(it, dict)}

    rows = []
    for cq in case_queries.resolve_cases(issues):
        issue = by_ident.get(cq.identifier)
        row = {
            "case": cq.identifier,
            # M52 — the human name leads. The identifier is a lookup key, not a label.
            "label": case_queries.case_label({"identifier": cq.identifier, "title": cq.title}),
            "title": (cq.title or "")[:80],
            "unwatched": bool(cq.skipped),
            "skip_reason": cq.reason if cq.skipped else "",
            "phase": _prop(issue, "MR Phase"),
            "deadline": _prop(issue, "MR Phase Deadline"),
            "awaiting_user": _prop(issue, "MR Awaiting User YES"),
            "inbound": [],
            "outbound": [],
        }
        if not cq.skipped:
            try:
                row["inbound"] = fetch_inbound(str(cq.query), LOOKBACK_DAYS) or []
            except Exception as e:
                row["inbound_error"] = "%s: %s" % (type(e).__name__, e)
            try:
                row["outbound"] = fetch_outbound(str(cq.query), LOOKBACK_DAYS) or []
            except Exception as e:
                row["outbound_error"] = "%s: %s" % (type(e).__name__, e)
        row["needs_reply"] = _needs_reply(row)
        row["overdue"] = _overdue(row["deadline"], now)
        # M56 — measured independently of the phase deadline, which is what missed the Nike case.
        row["days_silent"] = days_silent(row, now)
        row["stale"] = is_stale(row, now)
        unconf, ship_date, ship_days = shipment_unconfirmed(
            (issue or {}).get("description"), now)
        row["shipment_unconfirmed"] = unconf
        row["ship_date"] = ship_date
        row["ship_days"] = ship_days
        rows.append(row)
    return rows


def _needs_reply(row):
    """An inbound with no outbound AFTER it — by ANY runtime — is unanswered.

    This is the whole reason the digest reads the mailbox rather than a ledger: 'answered' means
    somebody replied, not 'I replied'."""
    ins = [m for m in row.get("inbound") or [] if m.get("at")]
    outs = [m for m in row.get("outbound") or [] if m.get("at")]
    if not ins:
        return False
    last_in = max(m["at"] for m in ins)
    last_out = max([m["at"] for m in outs], default=None)
    return last_out is None or last_out < last_in


def _overdue(deadline, now):
    if not deadline:
        return False
    try:
        d = datetime.strptime(str(deadline)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return d.date() < now.date()


# =========================================================================================
# M56 — TWO TIMERS THAT DO NOT DEPEND ON THE PHASE DEADLINE.
#
# 2026-07-30, the Nike shoes. The case had a deadline of 2026-08-05 sitting comfortably in the
# future, so nothing anywhere reported a problem. Underneath that reassuring date:
#
#   * Nike's last contact was 2026-07-17 — THIRTEEN DAYS of silence.
#   * The shoes had been posted on 07-24 and NOBODY HAD CONFIRMED THEY ARRIVED. The deadline was
#     computed at ship time from an ASSUMED receipt. They had in fact been delivered on 07-29, so
#     the real SLA had barely started — but that was luck. Had the parcel been lost, the case would
#     have waited forever on a receipt scan that could never happen, with the goods already gone
#     from the user's possession.
#
# A phase deadline answers "when is the vendor's window up". It cannot answer "has this case gone
# quiet" or "did the thing we posted actually arrive", and those are the two ways a case dies
# without anyone noticing. Both are measured here, independently, and neither can be satisfied by
# a date that has not yet arrived.
# =========================================================================================
#: Days of total vendor silence before a case is called stale. Vendors reply on business
#: timescales, so a week of nothing is a real signal rather than impatience.
SILENCE_DAYS = int(os.environ.get("MER_SILENCE_DAYS", "7"))

#: Days after a shipment with no confirmed delivery before it is an alarm. UPS/FedEx ground is
#: 1-5 days domestically; beyond a week without confirmation, something is wrong.
SHIPMENT_GRACE_DAYS = int(os.environ.get("MER_SHIPMENT_GRACE_DAYS", "5"))

_SHIP_RE = re.compile(r"EVENT:\s*shipment\b.*?(\d{4}-\d{2}-\d{2})", re.I | re.S)
_DELIVERY_RE = re.compile(r"EVENT:\s*delivery\b", re.I)


def days_silent(row, now):
    """Days since ANY inbound from the counterparty. None when nothing has ever arrived."""
    ins = [m["at"] for m in (row.get("inbound") or []) if m.get("at")]
    if not ins:
        return None
    return (now - max(ins)).days


def is_stale(row, now, threshold=None):
    """A case nobody has heard from in a while, regardless of what its deadline says."""
    n = days_silent(row, now)
    return n is not None and n >= (SILENCE_DAYS if threshold is None else threshold)


def shipment_unconfirmed(description, now, grace=None):
    """-> (unconfirmed: bool, ship_date, days_since). Reads the EVENT lines on the case.

    A shipment with no matching `EVENT: delivery` line is unconfirmed. This is deliberately
    pessimistic: an unlogged delivery reads the same as a lost parcel, and being told to go and
    check is a far cheaper error than a case waiting silently on goods that never arrived."""
    grace = SHIPMENT_GRACE_DAYS if grace is None else grace
    text = str(description or "")
    m = _SHIP_RE.search(text)
    if not m:
        return False, None, None
    if _DELIVERY_RE.search(text):
        return False, m.group(1), None
    try:
        shipped = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return False, m.group(1), None
    days = (now - shipped).days
    return days >= grace, m.group(1), days


def _prop(issue, name):
    if not isinstance(issue, dict):
        return ""
    mr = issue.get("mr")
    if isinstance(mr, dict) and name in mr:
        return mr[name]
    return ""


def _gmail_fetchers():
    """The real Gmail readers. `in:sent` is what makes other runtimes' work visible."""
    import inbox_watcher
    import gmail_fetch
    token = gmail_fetch.access_token()

    def _hydrate(ids):
        out = []
        for mid in (ids or [])[:20]:
            try:
                msg = gmail_fetch.get_message(mid, token=token)
            except Exception:
                continue
            hdrs = {h.get("name", "").lower(): h.get("value", "")
                    for h in (msg.get("payload", {}).get("headers") or [])}
            out.append({"from": hdrs.get("from", ""), "to": hdrs.get("to", ""),
                        "subject": hdrs.get("subject", "")[:70],
                        "at": _epoch_ms(msg.get("internalDate"))})
        return out

    def inbound(query, days):
        return _hydrate(inbox_watcher.list_messages(
            "%s newer_than:%dd -in:sent" % (query, days), token))

    def outbound(query, days):
        # Same vendor scope, but the SENT side. from: becomes to: so the scope still applies.
        q = query.replace("from:", "to:")
        return _hydrate(inbox_watcher.list_messages(
            "in:sent (%s) newer_than:%dd" % (q, days), token))

    return inbound, outbound


# ---------------------------------------------------------------------------------------------
def render(rows, markdown=False, now=None):
    """-> (text, action_count). Every action line passes assert_no_phone_action()."""
    now = now or _now()
    L = []
    h1 = "# " if markdown else ""
    L.append("%sMERCHANDISE-RETURN DIGEST — %s UTC" % (h1, _iso(now)))
    L.append("")

    unwatched = [r for r in rows if r["unwatched"]]
    unanswered = [r for r in rows if r["needs_reply"]]
    overdue = [r for r in rows if r["overdue"]]
    awaiting = [r for r in rows if r.get("awaiting_user")]

    L.append("%s%d case(s) · %d unwatched · %d awaiting a reply · %d past deadline"
             % ("**" if markdown else "", len(rows), len(unwatched), len(unanswered),
                len(overdue)) + ("**" if markdown else ""))
    # An absent value must never read as a healthy one. If the board fields did not resolve, say
    # so loudly instead of printing "0 past deadline" over data we never actually read.
    no_state = [r for r in rows if not r.get("phase") and not r.get("deadline")]
    if no_state:
        L.append("")
        L.append("  !! %d of %d case(s) returned NO phase and NO deadline. The counts above are "
                 "NOT trustworthy for those cases — the board fields did not resolve."
                 % (len(no_state), len(rows)))
        L.append("     affected: %s" % ", ".join(r["case"] for r in no_state))
    L.append("")

    if unwatched:
        L.append("## UNWATCHED — no reply on these will ever be detected" if markdown
                 else "UNWATCHED — no reply on these will ever be detected")
        for r in unwatched:
            L.append("  %s  (%s)" % (r.get("label") or r["title"], r["case"]))
            L.append("        %s" % r["skip_reason"])
        L.append("")

    if unanswered:
        L.append("## UNANSWERED — a counterparty wrote and nobody has replied since" if markdown
                 else "UNANSWERED — a counterparty wrote and nobody has replied since")
        for r in unanswered:
            last = max(m["at"] for m in r["inbound"] if m.get("at"))
            age = now - last
            L.append("  %s  (%s)" % (r.get("label") or r["title"], r["case"]))
            L.append("        last inbound %s (%dh ago) from %s"
                     % (_iso(last), age.total_seconds() // 3600,
                        (r["inbound"][0].get("from") or "?")[:44]))
        L.append("")

    stale = [r for r in rows if r.get("stale")]
    unconfirmed = [r for r in rows if r.get("shipment_unconfirmed")]

    if unconfirmed:
        L.append("## SHIPPED, DELIVERY NOT CONFIRMED" if markdown
                 else "SHIPPED, DELIVERY NOT CONFIRMED")
        for r in unconfirmed:
            L.append("  %s  (%s)" % (r.get("label") or r["title"], r["case"]))
            L.append("        posted %s, %s days ago, and no delivery has been logged."
                     % (r.get("ship_date"), r.get("ship_days")))
            L.append("        CHECK THE CARRIER. The goods have left the user's possession and the")
            L.append("        deadline on this case assumes a receipt nobody has verified.")
        L.append("")

    if stale:
        L.append("## GONE QUIET" if markdown else "GONE QUIET")
        for r in stale:
            L.append("  %s  (%s)" % (r.get("label") or r["title"], r["case"]))
            L.append("        no word from the other side in %d days. Its deadline (%s) says"
                     % (r["days_silent"], r.get("deadline") or "none"))
            L.append("        nothing about this - a future deadline and a silent vendor look")
            L.append("        identical until the day it expires.")
        L.append("")

    L.append("## EVERY CASE" if markdown else "EVERY CASE")
    for r in sorted(rows, key=lambda x: str(x["deadline"] or "9999")):
        flags = []
        if r["unwatched"]:
            flags.append("UNWATCHED")
        if r["needs_reply"]:
            flags.append("NEEDS REPLY")
        if r["overdue"]:
            flags.append("OVERDUE")
        if r.get("awaiting_user"):
            flags.append("AWAITING USER")
        L.append("  %-52s %-10s %-12s %s%s"
                 % ((r.get("label") or r["title"])[:52], r["phase"] or "-",
                    r["deadline"] or "no deadline", r["case"],
                    ("  [" + ", ".join(flags) + "]") if flags else ""))
        # Cross-runtime visibility: say who sent what, so no session mistakes another's work
        # for neglect, and no session sends a duplicate on top of it.
        for m in (r["outbound"] or [])[:3]:
            L.append("             sent %s -> %s  %s"
                     % (_iso(m["at"]) if m.get("at") else "?",
                        (m.get("to") or "?")[:34], (m.get("subject") or "")[:40]))
    L.append("")

    actions = []
    for r in unwatched:
        actions.append("%s: add a `MAIL FROM: vendor.com` line to the issue description — "
                       "nothing is watching this case." % (r.get("label") or r["case"]))
    for r in unanswered:
        actions.append("%s: a reply is waiting and unanswered — draft a response." % (r.get("label") or r["case"]))
    for r in overdue:
        actions.append("%s: the phase deadline has passed — advance the tier or extend it."
                       % (r.get("label") or r["case"]))
    for r in unconfirmed:
        actions.append("%s: posted %s days ago with no delivery confirmed - check the carrier "
                       "tracking before trusting this case's deadline."
                       % ((r.get("label") or r["case"]), r.get("ship_days")))
    for r in stale:
        actions.append("%s: %d days of silence - chase it or advance the tier."
                       % ((r.get("label") or r["case"]), r["days_silent"]))
    for r in awaiting:
        actions.append("%s: flagged AWAITING USER — needs a written decision before it moves."
                       % (r.get("label") or r["case"]))

    for a in actions:
        assert_no_phone_action(a)          # E20, enforced not documented

    L.append("## NEXT ACTIONS" if markdown else "NEXT ACTIONS")
    if actions:
        for a in actions:
            L.append("  - %s" % a)
    else:
        L.append("  none — every case is watched, answered and inside its deadline.")
    return "\n".join(L), len(actions)


# ---------------------------------------------------------------------------------------------
def _selftest():
    ok = [True]

    def check(name, cond, detail=""):
        ok[0] = ok[0] and bool(cond)
        print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                             ("  -> %s" % detail) if detail and not cond else ""))

    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    # the bug this module exists for: an inbound answered by ANOTHER runtime is NOT unanswered
    answered = {"inbound": [{"at": now - timedelta(hours=6), "from": "v@x.com"}],
                "outbound": [{"at": now - timedelta(hours=4), "to": "v@x.com"}]}
    check("an inbound answered by another runtime is NOT flagged",
          _needs_reply(answered) is False)
    stale = {"inbound": [{"at": now - timedelta(hours=27), "from": "v@x.com"}], "outbound": []}
    check("an inbound with no reply at all IS flagged", _needs_reply(stale) is True)
    older_out = {"inbound": [{"at": now - timedelta(hours=2), "from": "v@x.com"}],
                 "outbound": [{"at": now - timedelta(hours=30), "to": "v@x.com"}]}
    check("an outbound OLDER than the inbound does not count as an answer",
          _needs_reply(older_out) is True)
    check("no inbound at all is not 'needs reply'",
          _needs_reply({"inbound": [], "outbound": []}) is False)

    check("a past deadline is overdue", _overdue("2026-07-01", now) is True)
    check("a future deadline is not", _overdue("2026-08-30", now) is False)
    check("a missing deadline is not", _overdue("", now) is False)
    check("an unparseable deadline is not", _overdue("soon", now) is False)

    # E20 enforcement
    check("a phone action RAISES", _raises(lambda: assert_no_phone_action(
        "MER-79: call Branch on 833-548-0319 to open the dispute."), PhoneActionError))
    check("'give them a call' also raises", _raises(lambda: assert_no_phone_action(
        "give them a call tomorrow"), PhoneActionError))
    check("a written action passes",
          assert_no_phone_action("MER-79: email the dispute to support@branchapp.com"))
    check("'call log' is not an instruction to call",
          assert_no_phone_action("MER-1: attach the call log to the complaint"))

    rows = [
        {"case": "MER-1", "title": "Stride", "unwatched": False, "skip_reason": "",
         "phase": "Tier3", "deadline": "2026-07-01", "awaiting_user": False,
         "inbound": [{"at": now - timedelta(hours=27), "from": "interact@stridebank.com"}],
         "outbound": [], "needs_reply": True, "overdue": True},
        {"case": "MER-9", "title": "Blind case", "unwatched": True,
         "skip_reason": "no address in the record", "phase": "Intake", "deadline": "",
         "awaiting_user": False, "inbound": [], "outbound": [],
         "needs_reply": False, "overdue": False},
    ]
    text, n = render(rows, now=now)
    check("the digest names the unwatched case", "MER-9" in text and "UNWATCHED" in text)
    check("the digest names the unanswered case", "UNANSWERED" in text and "MER-1" in text)
    check("the digest counts actions", n == 3, str(n))
    check("the digest reports the inbound age", "27h ago" in text, text[:400])

    md, _ = render(rows, markdown=True, now=now)
    check("markdown mode emits headings", md.startswith("# ") and "## EVERY CASE" in md)

    # ---- 2026-07-29 BUGFIX REGRESSION: the false all-clear.
    # gather() read a `cq.issue` attribute that CaseQuery has never had, so every phase and
    # deadline came back blank and the digest cheerfully printed "0 past deadline" over a board
    # with a deadline two days out. It did not crash and did not warn. These are its tests.
    class _CQ(object):
        def __init__(self, ident, title):
            self.identifier, self.title, self.query = ident, title, "from:v.com"
            self.skipped, self.reason = False, ""

    import case_queries as _cqmod
    _real = _cqmod.resolve_cases
    _cqmod.resolve_cases = lambda issues: [_CQ("MER-5", "Case: Acme / widget")]
    try:
        got = gather(issues=[{"identifier": "MER-5", "title": "Case: Acme / widget",
                              "mr": {"MR Phase": "Tier2", "MR Phase Deadline": "2026-01-01"}}],
                     fetch_inbound=lambda q, d: [], fetch_outbound=lambda q, d: [], now=now)
        check("gather reads MR props off the issue (THE false-all-clear bug)",
              bool(got) and got[0]["phase"] == "Tier2"
              and got[0]["deadline"] == "2026-01-01", str(got[:1])[:160])
        check("...so a past deadline is correctly flagged overdue",
              bool(got) and got[0]["overdue"] is True)
        check("...and the human label rides along",
              bool(got) and got[0]["label"] == "Acme / widget",
              str(got[0].get("label") if got else None))
    finally:
        _cqmod.resolve_cases = _real

    blind = [{"case": "MER-8", "label": "Acme / thing", "title": "t", "unwatched": False,
              "skip_reason": "", "phase": "", "deadline": "", "awaiting_user": False,
              "inbound": [], "outbound": [], "needs_reply": False, "overdue": False}]
    txt_blind, _ = render(blind, now=now)
    check("no phase AND no deadline triggers a loud untrustworthy warning",
          "NOT trustworthy" in txt_blind, txt_blind[:300])
    check("a case WITH state does not trigger that warning",
          "NOT trustworthy" not in render(rows, now=now)[0])

    # ---- M56 REGRESSION: the two timers a phase deadline cannot express.
    # The Nike case sat 13 days silent with a posted parcel nobody had confirmed, and reported
    # clean the whole time because its deadline was still in the future.
    quiet = {"inbound": [{"at": now - timedelta(days=13), "from": "v@x.com"}], "outbound": []}
    check("13 days of silence is stale", is_stale(quiet, now) is True)
    check("...and the exact day count is reported", days_silent(quiet, now) == 13)
    fresh = {"inbound": [{"at": now - timedelta(days=2), "from": "v@x.com"}], "outbound": []}
    check("2 days of silence is not stale", is_stale(fresh, now) is False)
    check("a case that has never heard from anyone is not 'stale'",
          is_stale({"inbound": [], "outbound": []}, now) is False,
          "no inbound is a different problem; days_silent returns None")
    check("days_silent is None when nothing ever arrived",
          days_silent({"inbound": [], "outbound": []}, now) is None)

    shipped_only = "EVENT: shipment | MER-2 | UPS 1Z492 -> Rebound | 2026-07-24 | next: SLA"
    unconf, sd, sdays = shipment_unconfirmed(shipped_only, now)
    check("a shipment with no delivery logged is unconfirmed", unconf is True, str((sd, sdays)))
    # 5, not 6: the self-test clock is 2026-07-29 12:00 UTC and the parcel went on 07-24.
    check("...and it reports the ship date and its age",
          sd == "2026-07-24" and sdays == 5, str((sd, sdays)))
    check("a logged delivery clears it",
          shipment_unconfirmed(shipped_only + "\nEVENT: delivery | MER-2 | DELIVERED | 2026-07-29",
                               now)[0] is False)
    check("a case with no shipment at all is not flagged",
          shipment_unconfirmed("no events here", now)[0] is False)
    check("a shipment inside the grace window is not yet an alarm",
          shipment_unconfirmed("EVENT: shipment | X | posted | 2026-07-29 | next: SLA",
                               now)[0] is False, "posted yesterday")

    ship_row = [{"case": "MER-2", "label": "Nike shoes", "title": "t", "unwatched": False,
                 "skip_reason": "", "phase": "Tier1", "deadline": "2026-08-30",
                 "awaiting_user": False, "inbound": [], "outbound": [],
                 "needs_reply": False, "overdue": False, "stale": False,
                 "shipment_unconfirmed": True, "ship_date": "2026-07-24", "ship_days": 6,
                 "days_silent": None}]
    txt_ship, n_ship = render(ship_row, now=now)
    check("an unconfirmed shipment surfaces DESPITE a comfortable future deadline",
          "SHIPPED, DELIVERY NOT CONFIRMED" in txt_ship and n_ship >= 1, txt_ship[:300])
    check("...and it says to check the carrier", "CHECK THE CARRIER" in txt_ship)

    txt_quiet, _ = render(
        [dict(ship_row[0], shipment_unconfirmed=False, stale=True, days_silent=13)], now=now)
    check("a quiet case surfaces DESPITE a comfortable future deadline",
          "GONE QUIET" in txt_quiet and "13 days" in txt_quiet, txt_quiet[:300])

    print("\n%s" % ("ALL PASS" if ok[0] else "SOME FAILED"))
    return ok[0]


def _raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


def email_digest(text, n, to=None):
    """Send the digest to the OWNER. Never to a vendor — this is the one outbound this module has.

    Routed through mer_send so the send-path gate holds (only mer_send may reach the transport).
    The idempotency action carries the DATE, so each day is a distinct logical send and a rerun on
    the same day is correctly suppressed. The mailbox guard is disabled for this one path on
    purpose: it exists to stop a second letter reaching a VENDOR, and every prior digest was sent
    to the owner's own address, so it would block the daily report by design.
    """
    import mer_send
    import mer_config
    to = to or (mer_config.profile() or {}).get("email")
    if not to:
        return {"sent": False, "reason": "no owner email in the profile — refusing to guess"}
    day = _now().strftime("%Y-%m-%d")
    return mer_send.send(
        to, "Case digest %s — %d item(s) need attention" % (day, n), text,
        case="DIGEST", action="daily_digest_%s" % day, override=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="cross-runtime status digest (read-only to vendors)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--email", action="store_true",
                    help="also email the digest to the profile owner (never to a vendor)")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if _selftest() else 1
    rows = gather()
    text, n = render(rows, markdown=a.markdown)
    print(text)
    if a.email:
        r = email_digest(text, n)
        print("")
        print("-- email: %s"
              % ("sent to the owner" if r.get("sent") else r.get("reason")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
