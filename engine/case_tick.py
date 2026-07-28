#!/usr/bin/env python3
"""
case_tick.py (VPS / API edition) — the generalized daily case-tick engine, running on the 24/7 VPS.

This is the laptop-independent version (Blueprint M20). Instead of shelling out to the Windows
`multica` CLI, it uses multica_api.py to talk to https://api.multica.ai directly. Same decision
logic as the laptop version: for every open return case it reads the MR phase-state and computes
the next action (advance / nudge / wait / needs-YES / onboard).

M37 — THE LADDER NOW ACTUALLY CLIMBS. Until now this script PRINTED "ADVANCE"/"DUE"/"OVERDUE"
and wrote nothing, so a case parked at Tier 1 was nudged at Tier 1 forever and never escalated.
In --live, a case whose gate has genuinely cleared now has its `MR Phase` advanced per NEXT_PHASE
and its `MR Phase Deadline` re-armed with businessday math (never hand-counted calendar days —
that is a documented, repeated bug class here).

What advancing does NOT do: it does not send anything. Entering a RED-lane phase (any new
channel: Tier1 first contact, Tier2 executives, Tier3 regulators, PreSuit statutory notice,
Tier4) also sets `MR Awaiting User YES`, so the case parks for the user's explicit YES before
the outbound for that phase goes anywhere. The ladder climbs; the letters still wait for a human.

M44 — THE WRITE IS NOW PROVEN, AND CLIENT CASES ARE FENCED OUT. Two holes were left open by
M37 and are closed here:

  1. NO READ-BACK. `PUT /api/issues/<id>` with a {"properties": …} body returns 200 OK and
     SILENTLY DISCARDS them (see multica_api.set_properties). set_properties already uses the
     correct per-property endpoint, but "the correct endpoint" is a belief, not a proof: any
     future schema change, permission change, or option-id drift would make the write a no-op
     while the tick logged "ADVANCED" and idempotency recorded the advance as done — a ladder
     that reports climbing while standing still. advance() now RE-READS the issue after every
     write and compares the stored value to what it asked for. A mismatch is a failure, the
     idempotency reservation is RELEASED so the next tick can retry, and nothing is claimed.
  2. CLIENT CASES COULD AUTO-ADVANCE. gate_check had no notion of a client case, so a
     `CLIENT:`-titled case with an elapsed Tier1 deadline would climb — and entering a tier is
     what queues third-party contact. Client cases need written authorization first (and a
     separate authorization before any regulatory filing), so they are now refused outright:
     the engine reports them and a human advances them by hand.

Also added: a substantive-vendor-reply hold. Advancement means "the counterparty went silent
through the whole window"; if the case record logs a reply NEWER than the current phase's
outbound, the window did not lapse in silence and a human decides what the reply means.

Gates that must hold (non-negotiable product rules, enforced structurally below):
  * never auto-advance a CLIENT case — third-party contact needs written authorization;
  * never advance a case with `MR Awaiting User YES` true;
  * never advance past Intake/CaseFile unless `MR Intake Complete` is true;
  * never advance INTO Tier4/court unless remedy_gate.remedy_complete() clears (and it
    fails CLOSED when the case has no remedy map at all — no Tier 0 output, no court);
  * never advance a Closed/done/cancelled case, and never auto-CLOSE one (closing is a RED
    action needing the user's explicit YES — "escalated internally" never closes a case);
  * advancement is idempotent — re-running the tick the same day cannot double-advance
    (idempotency.reserve on a phase_advance key, plus the freshly-armed future deadline,
    which puts the case straight back into WAIT);
  * an advancement that did not verifiably land on the board is NOT an advancement — the
    reservation is released and the case is reported as not advanced.

Default --dry-run (prints the plan, writes NOTHING). --live advances + arms deadlines + logs.
Deployed to /opt/data/scripts/ ; run by the container's hourly-business / daily cron.
"""
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import multica_api as mc          # noqa: E402
import businessday                # noqa: E402
import remedy_gate                # noqa: E402
import remedy_map                 # noqa: E402
import idempotency                # noqa: E402
# resolution_check is the module that exists PRECISELY to reject brush-offs ("we've escalated
# internally"). Until 2026-07-28 case_tick never imported it, so the reply-hold treated every
# inbound alike and a brush-off held the ladder shut exactly as hard as a real resolution.
import resolution_check           # noqa: E402

BUILD_PROJECT = "13805886-7b4d-45bf-b985-32128f91b288"  # engine-build milestones — skip
CASE_TITLE_HINTS = ("Case:", "CLIENT:", "SELF:")
NEXT_PHASE = {
    "Intake": "CaseFile", "CaseFile": "RemedyMap", "RemedyMap": "Tier1",
    "Tier1": "Tier2", "Tier2": "Tier3", "Tier3": "PreSuit",
    "PreSuit": "Tier4", "Tier4": "(user files)",
}

# Property names (resolved BY NAME by multica_api — that portability is deliberate).
PHASE_PROP = "MR Phase"
DEADLINE_PROP = "MR Phase Deadline"
INTAKE_PROP = "MR Intake Complete"
YES_PROP = "MR Awaiting User YES"
REMEDY_MAP_PROP = "MR Remedy Map"            # Tier 0 output: applicable lever keys
REMEDY_DONE_PROP = "MR Remedy Attempted"     # levers actually done AND logged

# The phase at which Tier 0 runs (ladder.md phase 3). A case sitting here with an empty
# MR Remedy Map has its map BUILT by remedy_map.py — the step that never existed, and
# without which remedy_gate holds Tier 4 shut for every case forever.
REMEDY_MAP_PHASE = "RemedyMap"

# Business-day SLA armed on ENTERING a phase. Business days only — see businessday.py and the
# MER-3 PPG incident ("7 business days from a Friday" is not "+7 calendar days").
PHASE_SLA_DAYS = {
    "CaseFile": 2,
    "RemedyMap": 3,
    # Tier 1/2 shortened 7 -> 5 business days (King, 2026-07-26). These are OUR policy, not law:
    # nothing statutory sets a vendor's reply window. 5 business days is ~7-9 calendar days, still
    # a reasonable opportunity to respond if the demand is ever tested in court, while cutting a
    # full week out of the ladder. Anything shorter starts to read as manufactured.
    # NOTE: this does NOT move an already-armed deadline. `MR Phase Deadline` is written on
    # ENTERING a phase, so a case that was promised 7 business days in writing keeps them -- moving
    # a goalpost we already put in a letter is the gate-jump this system exists to prevent.
    "Tier1": 5,      # ladder.md: Tier 1 window = 5 business days, Day-3 nudge
    "Tier2": 5,
    "Tier3": 7,      # unchanged: Tier 3 waits on REGULATORS, and we do not set their clock
    # PreSuit is a STATUTORY notice period that varies by state (TX DTPA 60 calendar days,
    # CA CLRA 30). We never shorten a promised/required window, so the default clock is a
    # deliberately conservative 45 business days (~9 weeks) — longer than any state's notice
    # period. The real per-case period comes from the remedy map and should override this.
    "PreSuit": 45,
    "Tier4": 10,
}

# Phases whose ENTRY is a new channel / filing — the 🔴 lane. Advancing into one parks the case
# on the user's explicit YES so nothing is fired at a new counterparty automatically.
RED_ENTRY_PHASES = {"Tier1", "Tier2", "Tier3", "PreSuit", "Tier4"}

# Phase names the engine may NEVER write. "(user files)" is the court filing (the principal
# files, never the engine); "Closed" is the user's explicit close phrase only.
NEVER_WRITE_PHASES = {"(user files)", "Closed", "closed"}

# Entering these requires remedy_gate to clear.
COURT_PHASES = {"Tier4"}

CLOSED_STATUSES = {"done", "cancelled", "closed"}
ADVANCING_CODES = {"ADVANCE", "DUE", "OVERDUE"}

# ---- client-case detection (M44) -------------------------------------------------------
# Same CONVENTION mer_engine.is_client_case() uses, reimplemented here as a pure function so
# the gate is unit-testable offline and case_tick does not have to import mer_engine's whole
# send stack (gmail_transport, classify_llm, …) just to answer "is this the operator's client?".
# mer_engine's answer is OR-ed in when it happens to be importable — the test is deliberately
# FAIL-SAFE / one-way: any single signal marks a case as a client case. A false positive costs
# a human advancing one case by hand; a false negative opens a new channel against a third
# party on someone else's behalf without their written authorization. Widen, never narrow.
CLIENT_TITLE_PREFIX = "CLIENT:"
CLIENT_DESC_MARKER = "CLIENT CASE"
# How much of the description the marker may hide in. Bounded so a stray mention deep in a
# long thread transcript cannot flip an ordinary case; the marker is a header convention and
# lives in the opening block.
CLIENT_MARKER_SCAN_CHARS = 500
AFFIRMATIVE = {"yes", "true", "y", "1", "client", "client case", "on", "checked", "✓"}

# ---- substantive-reply detection (M44; rebuilt 2026-07-28) -----------------------------
# THE GATE THAT WAS A NO-OP. This block used to be read ONLY out of the issue description
# (a REPLIES:/RECEIVED:/… block) or an "MR …Replied" property. Nothing in the tree ever WROTE
# either one, and no such property existed on the board — so vendor_reply_hold() returned
# (False, "") on 100% of live cases and the gate had never once fired. MER-2 (Nike) would have
# auto-escalated on 2026-08-05 with a physical exchange in transit, and MER-76 on 2026-08-03
# mid-thread with the vendor.
#
# The primary signal is now the board property `MR Last Vendor Reply` (date), written by the
# inbound classifier for SUBSTANTIVE inbound only — autoresponders and delivery receipts do
# not count. The description blocks remain as a fallback so a hand-kept case record still works.
LAST_REPLY_PROP = "MR Last Vendor Reply"
REPLY_BLOCKS = ("REPLIES", "REPLY", "RECEIVED", "VENDOR REPLY", "INBOUND")
SENT_BLOCK = "SENT"
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# The remedy the case is demanding, used to judge whether a reply actually DELIVERED it.
REMEDY_TYPE_PROP = "MR Remedy Type"

# NO HOLD MAY BE PERMANENT. A hold with no timeout is just a different way to strand a case:
# the ladder stops, nothing escalates, and nobody is told. After this many business days the
# hold either lapses (unclassifiable) or converts into a named human action (resolved).
REPLY_HOLD_MAX_BUSINESS_DAYS = 10


def truthy(v):
    return str(v).lower() in ("true", "1", "yes", "✓")


def _affirmative(val):
    if val is True:
        return True
    if val is False or val is None:
        return False
    return str(val).strip().lower() in AFFIRMATIVE


def _block(desc, label):
    """Text of a 'LABEL: …' block, up to the next blank line. Mirrors case_queries._block."""
    if not desc:
        return ""
    m = re.search(r"(?ms)^\s*%s\s*:\s*(.+?)(?:\n\s*\n|\Z)" % re.escape(label), desc)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def is_client_case(issue):
    """True if this case is run on behalf of the operator's CLIENT rather than the user.

    `issue` is a raw board issue dict OR the plain case_view dict — both carry title,
    description and mr/properties under the same keys.
    """
    issue = issue or {}
    if (issue.get("title") or "").strip().upper().startswith(CLIENT_TITLE_PREFIX):
        return True
    # CONTAINMENT, not startswith (fixed 2026-07-28). The old test only looked at the FIRST
    # characters of the description, and MER-4's description opens "NEEDS-KING: re-send the
    # intake questions to Kim…" with "CLIENT CASE —" on line 3 — so the marker signal missed
    # entirely and the case was caught only by its title prefix. One convention short of a
    # third-party contact being opened without written authorization. The docstring above says
    # "widen, never narrow"; a prefix test is the narrowest possible reading of it.
    if CLIENT_DESC_MARKER in (issue.get("description") or "")[:CLIENT_MARKER_SCAN_CHARS].upper():
        return True
    for name, val in (issue.get("mr") or {}).items():
        if "client" in str(name).lower() and _affirmative(val):
            return True
    # OR in the engine's own detector, but ONLY if mer_engine is ALREADY loaded. Importing it
    # here would drag gmail_transport / classify_llm into a pure predicate that runs on every
    # issue of every board walk — a heavy, credential-touching import on the send path, to
    # re-derive a rule this function already implements identically. One-way widening only.
    me = sys.modules.get("mer_engine")
    if me is not None:
        try:
            if me.is_client_case(issue.get("identifier"), issue):
                return True
        except Exception:
            pass
    return False


def _latest_date(text):
    """The newest ISO date appearing in a chunk of record text, or None."""
    best = None
    for raw in _DATE_RE.findall(text or ""):
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    return best


def _to_date(v):
    """ISO date / datetime string -> date, else None."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def vendor_reply_hold(desc, mr=None, today=None):
    """Did the vendor answer this phase's outbound, and does that answer hold the ladder?

    Returns a dict:
        {"hold": bool, "why": str, "disposition": str, "flag_human": bool,
         "reply_at": date|None, "resolved": bool}
    `disposition` ∈ {"none", "stale", "resolved", "brush_off", "unclassifiable", "expired"}.

    THE LOGIC IS NOT "ANY REPLY HOLDS" — that was the second half of the defect. The
    documented ladder rule is that "escalated internally" is NOT resolution (workspace rule
    #4). A brush-off is precisely the reply that SHOULD escalate; holding on it hands the
    vendor a free, unlimited stall by return of post. So:

        resolved       -> HOLD, and route toward close_case (never toward a new tier)
        brush-off      -> DO NOT HOLD; the ladder climbs, which is the whole point of it
        unclassifiable -> HOLD and FLAG A HUMAN (we cannot read it; a person must)
        stale reply    -> no hold (it predates this phase's outbound)

    Signals for "there is a reply", in order of authority:
      1. the `MR Last Vendor Reply` DATE property — written by the inbound classifier for
         SUBSTANTIVE inbound only (autoresponders and delivery receipts are excluded there);
      2. an affirmative `MR …Replied`-style property (opt-in legacy signal);
      3. a REPLIES/RECEIVED/… description block whose newest date is >= the SENT block's.

    NO HOLD IS PERMANENT. Past REPLY_HOLD_MAX_BUSINESS_DAYS an unclassifiable hold LAPSES
    (disposition "expired") so a case cannot be parked forever by one unreadable email, and a
    resolved hold stops being silent — it keeps holding (advancing a resolved case into a new
    escalation tier is never right) but flags a named human action: close it or reopen it.
    """
    today = today or businessday.today()
    mr = mr or {}
    out = {"hold": False, "why": "", "disposition": "none", "flag_human": False,
           "reply_at": None, "resolved": False}

    # ---- 1. the authoritative property -------------------------------------------------
    reply_at = _to_date(mr.get(LAST_REPLY_PROP))
    source = "%s = %s" % (LAST_REPLY_PROP, reply_at) if reply_at else ""

    # ---- 2. legacy affirmative flag ----------------------------------------------------
    flag_prop = None
    for name, val in mr.items():
        n = str(name).lower()
        if ("replied" in n or "reply received" in n) and _affirmative(val):
            flag_prop = name
            break

    # ---- 3. description fallback -------------------------------------------------------
    reply_txt = " ".join(_block(desc, lab) for lab in REPLY_BLOCKS).strip()
    desc_at = _latest_date(reply_txt) if reply_txt else None
    sent_at = _latest_date(_block(desc, SENT_BLOCK))

    if desc_at and (reply_at is None or desc_at > reply_at):
        # A description-logged reply only counts if it is not demonstrably older than this
        # phase's outbound. Unknown SENT date -> fail safe and treat it as current.
        if sent_at is None or desc_at >= sent_at:
            reply_at = desc_at
            source = ("%s block dated %s%s"
                      % ("/".join(REPLY_BLOCKS[:2]), desc_at,
                         "" if sent_at is None else " (outbound %s)" % sent_at))
        elif reply_at is None and not flag_prop:
            out["disposition"] = "stale"
            out["why"] = ("the only logged reply (%s) predates this phase's outbound (%s) — "
                          "the window did lapse in silence" % (desc_at, sent_at))
            return out

    if reply_at is None and not flag_prop:
        return out

    out["reply_at"] = reply_at
    if flag_prop and not source:
        source = "%s is affirmative" % flag_prop

    # ---- classify the reply: resolution, brush-off, or unreadable ----------------------
    demanded = str(mr.get(REMEDY_TYPE_PROP) or "refund")
    verdict = resolution_check.rule_based(demanded, reply_txt)
    reason = verdict.get("reason") or ""
    if reply_txt and verdict.get("resolved"):
        disposition = "resolved"
    elif reply_txt and (reason.startswith("Brush-off:") or reason.startswith("DODGE:")):
        disposition = "brush_off"
    else:
        disposition = "unclassifiable"

    age_ok = True
    if reply_at is not None:
        age_ok = businessday.business_day_deadline(reply_at, REPLY_HOLD_MAX_BUSINESS_DAYS) >= today

    if disposition == "brush_off":
        out.update({
            "hold": False, "disposition": "brush_off",
            "why": ("a vendor reply is on record (%s) but it is a BRUSH-OFF, not a "
                    "resolution — %s. 'Escalated internally' is not resolution; a brush-off "
                    "does not hold the ladder." % (source, reason)),
        })
        return out

    if disposition == "resolved":
        out.update({
            "hold": True, "resolved": True, "disposition": "resolved",
            "flag_human": not age_ok,
            "why": ("the vendor reply on record (%s) DELIVERS the demanded %s — %s. The case "
                    "routes toward close_case, never toward a new escalation tier.%s"
                    % (source, demanded, reason,
                       "" if age_ok else
                       " HUMAN ACTION REQUIRED: this resolution is more than %d business days old and "
                       "the case is still open — close it (close_case.py --confirm-close) or "
                       "reopen it; it must not sit here silently."
                       % REPLY_HOLD_MAX_BUSINESS_DAYS)),
        })
        return out

    # unclassifiable — we cannot read it, so a human must, but not forever.
    if not age_ok:
        out.update({
            "hold": False, "disposition": "expired", "flag_human": True,
            "why": ("a vendor reply was on record (%s) that could not be classified, and the "
                    "hold has now exceeded %d business days — the hold LAPSES rather than "
                    "stranding the case. A human should read that reply."
                    % (source, REPLY_HOLD_MAX_BUSINESS_DAYS)),
        })
        return out
    out.update({
        "hold": True, "disposition": "unclassifiable", "flag_human": True,
        "why": ("a vendor reply is on record (%s) that the classifier could not call a "
                "resolution OR a brush-off (%s) — holding, and FLAGGING FOR A HUMAN. "
                "Escalating to a new channel over the top of a live conversation is a "
                "person's decision, not a timer's. Hold lapses after %d business days."
                % (source, reason or "no reply text available to classify",
                   REPLY_HOLD_MAX_BUSINESS_DAYS)),
    })
    return out


def _levers(v):
    """Parse a lever list property ('bbb, state_ag' / newline separated) -> [keys]."""
    if not v:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    out = []
    for chunk in str(v).replace("\n", ",").replace(";", ",").split(","):
        c = chunk.strip()
        if c:
            out.append(c)
    return out


def decide(phase, deadline, intake_done, awaiting_yes, today):
    if awaiting_yes:
        return "NEEDS-YES", "a %s action is queued for the user's explicit approval" % phase
    if phase == "Closed":
        return "CLOSED", "closed — skip"
    if phase in ("Intake", "CaseFile"):
        if not intake_done:
            return "INTAKE", "awaiting the user's answers; no auto-advance until MR Intake Complete=true"
        return "ADVANCE", "intake complete — advance %s -> %s" % (phase, NEXT_PHASE.get(phase, "?"))
    if not deadline:
        return "NO-DEADLINE", ("%s with no MR Phase Deadline set — arming the clock "
                               "(nothing used to; see arm_deadline)" % phase)
    d = (deadline - today).days
    nxt = NEXT_PHASE.get(phase, "?")
    if d > 3:
        return "WAIT", "%dd until %s (%s); silent" % (d, deadline, phase)
    if 1 <= d <= 3:
        return "NUDGE", "%dd until %s (%s); Day-3 follow-up nudge window (auto-send on veto)" % (d, deadline, phase)
    if d == 0:
        return "DUE", "DUE TODAY — advance %s -> %s (queue next outbound per lane)" % (phase, nxt)
    return "OVERDUE", "OVERDUE by %dd — advance %s -> %s (was due %s)" % (-d, phase, nxt, deadline)


# ---------------------------------------------------------------- the M37 gate

def gate_check(case, today=None):
    """Decide whether THIS case may have its MR Phase advanced right now.

    `case` is a plain dict (see case_view) — no network, no board object — so the whole gate
    is unit-testable offline:
        {identifier, title, description, status, phase, deadline(date|None), intake_done,
         awaiting_yes, remedy_map[], remedy_attempted[], mr{}}

    Returns (allowed: bool, target_phase: str|None, deadline: date|None, reason: str).
    Every refusal names the rule it is enforcing.
    """
    today = today or businessday.today()
    phase = case.get("phase")
    status = (case.get("status") or "").lower()

    if status in CLOSED_STATUSES:
        return False, None, None, "case status is %r — closed cases never advance" % status
    if not phase:
        return False, None, None, "no MR Phase set — not in the engine"
    if phase in NEVER_WRITE_PHASES or phase == "Closed":
        return False, None, None, "phase %r is terminal — the engine never advances or closes it" % phase

    # GATE 0 — CLIENT CASES NEVER AUTO-ADVANCE. Entering a tier is what queues contact with a
    # third party on someone else's behalf, and that needs the client's WRITTEN authorization
    # (and a separate authorization before any regulatory filing). No timer can supply consent,
    # so no timer may move a client case. The engine reports it; a human advances it by hand.
    if is_client_case(case):
        return False, None, None, ("client case (CLIENT: convention) — client cases NEVER "
                                   "auto-advance; third-party contact needs the client's written "
                                   "authorization, which no elapsed timer can supply")

    # GATE 1 — an action is parked on the user's explicit YES. Nothing moves.
    if case.get("awaiting_yes"):
        return False, None, None, "MR Awaiting User YES is true — the user must answer first"

    # GATE 2 — intake must be complete before leaving Intake/CaseFile.
    if phase in ("Intake", "CaseFile") and not case.get("intake_done"):
        return False, None, None, "MR Intake Complete is not true — cannot advance past %s" % phase

    target = NEXT_PHASE.get(phase)
    if not target:
        return False, None, None, "no NEXT_PHASE mapping for %r" % phase
    if target in NEVER_WRITE_PHASES:
        return False, None, None, (
            "next phase is %r — the engine never writes it (the principal files in court; "
            "closing needs the user's explicit YES)" % target)

    # GATE 3 — the clock. Intake/CaseFile advance on the intake flag; every timed phase needs
    # its deadline to have actually elapsed. A gate is never opened early.
    if phase not in ("Intake", "CaseFile"):
        deadline = case.get("deadline")
        if not deadline:
            return False, None, None, "%s has no MR Phase Deadline — clock not armed, nothing elapsed" % phase
        if (deadline - today).days > 0:
            return False, None, None, "%s deadline %s has not elapsed (%dd left)" % (
                phase, deadline, (deadline - today).days)

        # GATE 3b — the window must have lapsed IN SILENCE, and "silence" is judged by what
        # the reply actually SAID. A resolution holds (and routes to close); a brush-off does
        # NOT hold (that is what the ladder is for); an unreadable reply holds AND flags a
        # human, with a timeout so no hold can be permanent.
        h = vendor_reply_hold(case.get("description"), case.get("mr"), today)
        if h["hold"]:
            return False, None, None, "vendor reply on record (%s) — %s%s" % (
                h["disposition"], h["why"], "  [FLAG FOR HUMAN]" if h["flag_human"] else "")

    # GATE 3c — A CASE MAY NOT LEAVE RemedyMap WITH AN EMPTY MAP.
    # Tier 0 is the phase whose entire job is producing the map, and nothing stopped a case
    # walking straight out of it with the property blank. GATE 4 only ever fired on the last
    # hop (target == Tier4), and nothing re-ran Tier 0, so MER-1 (Tier3), MER-2 (Tier1) and
    # MER-3 (Tier2) all left RemedyMap empty and became PERMANENTLY unable to reach court —
    # each of them due to burn 45 business days at PreSuit before anything said so.
    rmap = case.get("remedy_map") or []
    if phase == REMEDY_MAP_PHASE and not rmap:
        return False, None, None, (
            "%s is empty and this case is AT %s — the phase whose only job is to produce it. "
            "Leaving here with a blank map makes Tier 4 structurally unreachable for the rest "
            "of the case's life, so the hop is refused until Tier 0 runs (case_tick --live "
            "builds it, or remedy_map.py by hand)." % (REMEDY_MAP_PROP, REMEDY_MAP_PHASE))

    # GATE 3d — THE MAP IS VALIDATED ON READ, EVERY HOP.
    # remedy_map.write() validated its own writes, but most live maps were typed in by hand
    # and this side read the property as free text. MER-76's map contained four keys unknown
    # to remedy_gate (log_attempt refuses them by design, so they could NEVER be satisfied)
    # plus `small_claims_dallas` — court as an owed prerequisite for court. An unsatisfiable
    # map is a LOUD refusal, never a silent forever-hold: the problem and its fix are named.
    if rmap:
        ok_map, problems = remedy_map.validate_map(rmap)
        if not ok_map:
            suggested, _ = remedy_map.normalize_map(rmap)
            return False, None, None, (
                "UNSATISFIABLE %s -- REFUSING TO ADVANCE. %s || The map as written can never "
                "clear the court gate, so advancing would walk this case toward a Tier 4 it "
                "can never reach. Suggested corrected map: %s"
                % (REMEDY_MAP_PROP, remedy_map.describe_problems(problems),
                   remedy_map.serialize(suggested) or "(rebuild with Tier 0)"))

    # GATE 4 — court is structurally unreachable until the remedy map is exhausted.
    # remedy_complete() is now evaluated on EVERY advance, not only on the last hop, so a
    # case learns what it still owes from Tier 1 onward instead of discovering it after
    # 45 business days at PreSuit. It only BLOCKS when the target is court.
    owed = ""
    if phase not in ("Intake", "CaseFile", REMEDY_MAP_PHASE):
        r = remedy_gate.remedy_complete(rmap, case.get("remedy_attempted") or [])
        if r.get("no_map"):
            owed = "  [remedy map EMPTY — Tier 4 unreachable until Tier 0 runs]"
        elif r["missing"]:
            owed = "  [still owed before Tier 4: %s]" % ", ".join(r["missing"])
        if target in COURT_PHASES and not r["ready_for_court"]:
            return False, None, None, (
                "remedy_gate holds %s: %s" % (target, r["reason"]))

    sla = PHASE_SLA_DAYS.get(target)
    new_deadline = businessday.business_day_deadline(today, sla) if sla else None
    return True, target, new_deadline, "gates cleared — %s -> %s%s" % (phase, target, owed)


def tracked_cases(issues):
    """The subset of `issues` this engine treats as live merchandise-return cases.

    Factored out of main()'s loop so a second consumer (mer_dashboard.py) filters the exact same
    way case_tick itself does, instead of re-implementing the rule and risking the two drifting —
    the same class of bug M33 killed for Gmail queries (two hand-synced tables, one wrong).
    A case is tracked iff: not done/cancelled, not a build-project housekeeping issue, not a
    sweep/system/milestone title, and it carries a non-empty MR Phase.
    """
    out = []
    for it in issues:
        status = it.get("status") or ""
        title = it.get("title") or ""
        if status in ("done", "cancelled"):
            continue
        if it.get("project_id") == BUILD_PROJECT:
            continue
        if title.startswith("Daily case sweep") or title.startswith(("KING:", "SYSTEM:", "M1 ", "M2 ")):
            continue
        if (it.get("mr", {}) or {}).get(PHASE_PROP):
            out.append(it)
    return out


def case_view(issue):
    """Multica issue dict -> the plain dict gate_check consumes."""
    p = issue.get("mr", {}) or {}
    deadline = None
    if p.get(DEADLINE_PROP):
        try:
            deadline = datetime.strptime(str(p[DEADLINE_PROP])[:10], "%Y-%m-%d").date()
        except Exception:
            deadline = None
    return {
        "identifier": issue.get("identifier"),
        "id": issue.get("id"),
        "title": issue.get("title") or "",
        # M44: description + the raw mr map travel with the view so the client-case gate and
        # the substantive-reply gate stay pure functions of a plain dict (offline-testable).
        "description": issue.get("description") or "",
        "mr": p,
        "status": (issue.get("status") or "").lower(),
        "phase": p.get(PHASE_PROP),
        "deadline": deadline,
        "intake_done": truthy(p.get(INTAKE_PROP, "")),
        "awaiting_yes": truthy(p.get(YES_PROP, "")),
        "remedy_map": _levers(p.get(REMEDY_MAP_PROP)),
        "remedy_attempted": _levers(p.get(REMEDY_DONE_PROP)),
    }


def _release_reservation(key):
    """Undo an idempotency reservation whose write did not land.

    reserve() records BEFORE the write — that ordering is correct (it is what makes a
    concurrent second tick unable to double-write), but it means a FAILED or SILENTLY
    DISCARDED write would be remembered forever as "already advanced" and the case could
    never be retried. So a write we could not verify releases its own key.

    Implemented against idempotency's own primitives rather than by editing that module:
    the ledger is a plain dict keyed by send_key, atomically rewritten.
    """
    try:
        data = idempotency._load()
        if key in data:
            del data[key]
            idempotency._atomic_save(data)
            return True
    except Exception:
        pass
    return False


def _same_value(want, got, pdef=None):
    """Did the board store what we asked for? Compared as normalized strings.

    Dates come back as '2026-08-05' or '2026-08-05T00:00:00Z'; selects come back either as the
    option label (list_issues resolves them) or the raw option id (get_issue does not), so an
    id that maps to the wanted label also counts as a match.
    """
    if isinstance(want, bool):
        return truthy(got) == want
    w = str(want).strip().lower()
    g = str(got).strip().lower()
    if w == g:
        return True
    if len(w) == 10 and w[:4].isdigit() and g.startswith(w):     # date vs datetime
        return True
    if pdef and (pdef.get("options") or {}):
        label = pdef["options"].get(got)
        if label is not None and str(label).strip().lower() == w:
            return True
    return False


def verify_written(issue_id, values_by_name, defs):
    """Re-read the issue and prove every value actually landed. Returns (ok, detail).

    ⚠️ THIS IS THE WHOLE POINT OF M44. `PUT /api/issues/<id>` with a {"properties": …} body
    returns 200 OK and silently discards them — a success response for a write that never
    happened. set_properties uses the correct per-property endpoint, but no HTTP status can
    prove a value is on the board; only reading it back can. Without this check a broken
    write would make case_tick log "ADVANCED" for a case that never moved.
    """
    raw = mc.get_issue(issue_id)
    props = (raw or {}).get("properties") or {}
    resolved = (raw or {}).get("mr") or {}      # present when get_issue fell back to list_issues
    bad = []
    for name, want in values_by_name.items():
        d = (defs or {}).get(name) or {}
        got = props.get(d.get("id")) if d.get("id") in props else resolved.get(name)
        if not _same_value(want, got, d):
            bad.append("%s: wanted %r, board has %r" % (name, want, got))
    if bad:
        return False, "; ".join(bad)
    return True, "read-back confirms " + ", ".join("%s=%s" % (k, v)
                                                   for k, v in values_by_name.items())


def advance(issue, view, target, new_deadline, mer16_id=None, defs=None):
    """Write the advancement to the board. LIVE ONLY — callers must gate on --live.

    Idempotent: idempotency.reserve() keys the write on (case, phase_advance:from->to,
    target+deadline). A second run the same day is refused before any HTTP call. The
    freshly-armed future deadline is the second, structural guard: the case reads WAIT.

    VERIFIED: the write is read back off the board before this returns success. A write that
    raised, or that returned 200 and changed nothing, releases its idempotency reservation and
    is reported as NOT advanced — so the next tick retries instead of the case being silently
    stranded with the ledger insisting it already climbed.
    """
    ident = view["identifier"]
    key_body = "%s|%s" % (target, new_deadline)
    ok, key = idempotency.reserve(ident, "phase_advance:%s->%s" % (view["phase"], target),
                                  "board", key_body,
                                  meta={"from": view["phase"], "to": target,
                                        "deadline": str(new_deadline)})
    if not ok:
        return False, "already advanced (idempotency key %s) — no double-advance" % key

    values = {PHASE_PROP: target}
    if new_deadline:
        values[DEADLINE_PROP] = new_deadline.isoformat()
    red = target in RED_ENTRY_PHASES
    if red:
        # Entering a new channel is a 🔴 action: the phase climbs, the outbound waits for YES.
        values[YES_PROP] = True
    try:
        mc.set_properties(issue, values, defs=defs)
    except Exception as e:
        _release_reservation(key)
        return False, ("the property write FAILED (%s: %s) — reservation released, the next "
                       "tick will retry. NOTHING was advanced." % (type(e).__name__, e))

    # --- READ-BACK. No success is claimed on the strength of an HTTP 200 alone. ---
    try:
        verified, detail = verify_written(issue["id"], values, defs)
    except Exception as e:
        verified, detail = False, "read-back itself failed (%s: %s)" % (type(e).__name__, e)
    if not verified:
        _release_reservation(key)
        return False, ("WRITE DID NOT LAND — %s. The API accepted the call but the board does "
                       "not show the new value (this is the 200-OK-and-silently-discards trap; "
                       "see multica_api.set_properties). Reservation released; %s was NOT "
                       "advanced and the next tick will retry." % (detail, ident))

    note = ("RECORD ONLY - NO ACTION REQUIRED [re %s]. Phase advanced %s -> %s by the daily tick "
            "(gate elapsed/cleared). New %s = %s (%s business days, businessday.py). %s"
            % (ident, view["phase"], target, DEADLINE_PROP, new_deadline,
               PHASE_SLA_DAYS.get(target, "n/a"),
               ("Entering %s is a new channel — %s set true; no outbound is sent until the user "
                "says YES." % (target, YES_PROP)) if red else "Green-lane phase; no new channel opened."))
    # WAKE-AGENT SAFETY: a comment on an issue with a live agent assigned WAKES that agent and is
    # read as an instruction (2026-07-18 bank double-email). Log to MER-16 instead in that case.
    target_issue = (mer16_id if (issue.get("assignee_type") == "agent" and mer16_id) else issue["id"])
    try:
        mc.add_comment(target_issue, note)
    except Exception as e:
        return True, "advanced, but the RECORD-ONLY log failed: %s" % e
    return True, "advanced %s -> %s; deadline %s%s (%s)" % (
        view["phase"], target, new_deadline, "; awaiting-YES set" if red else "", detail)


def arm_deadline(issue, view, today=None, mer16_id=None, defs=None):
    """Arm `MR Phase Deadline` on a tier phase that has none. LIVE ONLY.

    THE TERMINAL STALL THIS FIXES (2026-07-28). decide() has always returned
    NO-DEADLINE "…arm the clock" — and nothing armed one. advance() is the ONLY writer of
    MR Phase Deadline anywhere in the tree, and it is unreachable from this code path, so a
    tier phase with no deadline sat in NO-DEADLINE forever: GATE 3 refuses to advance
    ("clock not armed, nothing elapsed"), no nudge fires (nudge.due_nudges skips a case with
    no parseable deadline), and no report says the case is stuck. Telling evidence that this
    is the normal state and not an edge case: `MR Awaiting User YES` had usage_count 0
    workspace-wide before 2026-07-28, meaning case_tick has never actually advanced a case
    into a tier in production — every tier phase on the board was set BY HAND, which is
    exactly how a phase ends up with no deadline.

    The clock is armed from the phase's own outbound where the record shows one (the SENT
    block's newest date), else from today. Arming from SENT is the honest reading: the window
    started when the letter went, not when the engine noticed. That can produce an
    already-elapsed deadline — which is correct, and the case advances on the NEXT tick
    through the normal gates rather than jumping two states at once here.

    Writes ONLY the deadline. It never touches MR Phase and never sets awaiting-YES, so this
    can never be a disguised advancement. Verified by read-back, idempotent, RECORD-ONLY log.
    """
    today = today or businessday.today()
    ident = view["identifier"]
    phase = view["phase"]
    sla = PHASE_SLA_DAYS.get(phase)
    if not sla:
        return False, "no SLA defined for phase %r — nothing to arm" % phase
    sent_at = _latest_date(_block(view.get("description"), SENT_BLOCK))
    base = sent_at or today
    new_deadline = businessday.business_day_deadline(base, sla)

    ok, key = idempotency.reserve(ident, "arm_deadline:%s" % phase, "board",
                                  str(new_deadline),
                                  meta={"phase": phase, "deadline": str(new_deadline),
                                        "anchored_on": str(base)})
    if not ok:
        return False, "deadline already armed (idempotency key %s) — no re-arm" % key

    values = {DEADLINE_PROP: new_deadline.isoformat()}
    try:
        mc.set_properties(issue, values, defs=defs)
    except Exception as e:
        _release_reservation(key)
        return False, ("arming the clock FAILED (%s: %s) — reservation released, the next "
                       "tick retries. Nothing was written." % (type(e).__name__, e))
    try:
        verified, detail = verify_written(issue["id"], values, defs)
    except Exception as e:
        verified, detail = False, "read-back itself failed (%s: %s)" % (type(e).__name__, e)
    if not verified:
        _release_reservation(key)
        return False, ("CLOCK NOT ARMED — %s. Reservation released; the next tick retries."
                       % detail)

    view["deadline"] = new_deadline
    note = ("RECORD ONLY - NO ACTION REQUIRED [re %s]. %s was blank at phase %s, which is a "
            "terminal stall: no gate can elapse, no nudge fires, and nothing reports it. The "
            "clock is now armed to %s = %d business days from %s (%s). No phase change, no "
            "outbound, no approval flag was touched."
            % (ident, DEADLINE_PROP, phase, new_deadline, sla, base,
               "the newest dated %s line on the record" % SENT_BLOCK if sent_at else "today"))
    target_issue = (mer16_id if (issue.get("assignee_type") == "agent" and mer16_id) else issue["id"])
    try:
        mc.add_comment(target_issue, note)
    except Exception as e:
        return True, "clock armed to %s, but the RECORD-ONLY log failed: %s" % (new_deadline, e)
    return True, "clock armed: %s = %s (%d business days from %s; %s)" % (
        DEADLINE_PROP, new_deadline, sla, base,
        "anchored on the SENT record" if sent_at else "anchored on today")


def build_remedy_map(issue, view, mer16_id=None, defs=None):
    """TIER 0 — build and persist this case's remedy map. LIVE ONLY; callers gate on --live.

    Why this lives in the tick: `remedy_gate` (and gate_check GATE 4) hold Tier 4 shut until
    the case's remedy map is exhausted, and fail CLOSED when the map is empty. Nothing in the
    engine ever WROTE one, so court was unreachable by construction rather than by policy.
    A case sitting at RemedyMap with an empty map now gets a real, case-specific lever list.

    Sends nothing, files nothing, contacts nobody — Tier 0 is research and record-keeping.
    Idempotent: idempotency.reserve keys the write on the case + the lever set, so a second
    tick the same day cannot re-write or re-comment.
    """
    ident = view["identifier"]
    plan = remedy_map.build(remedy_map.case_view(issue))
    levers = plan["levers"]
    if not levers:
        return False, "remedy_map produced no levers — refusing to write an empty map"

    ok, key = idempotency.reserve(ident, "remedy_map_build", "board",
                                  remedy_map.serialize(levers),
                                  meta={"levers": levers, "state": plan.get("state")})
    if not ok:
        return False, "map already built (idempotency key %s) — no re-write" % key

    remedy_map.write(issue, levers, defs=defs)
    view["remedy_map"] = levers          # the rest of this tick sees the fresh map

    # WAKE-AGENT SAFETY: a comment on an issue with a live agent assigned WAKES that agent and
    # is read as an instruction (2026-07-18 bank double-email). Log to MER-16 in that case.
    target_issue = (mer16_id if (issue.get("assignee_type") == "agent" and mer16_id) else issue["id"])
    try:
        mc.add_comment(target_issue, remedy_map.render(plan, view))
    except Exception as e:
        return True, "map written (%d levers), but the RECORD-ONLY log failed: %s" % (len(levers), e)
    return True, "%d applicable lever(s) written to %s: %s" % (
        len(levers), REMEDY_MAP_PROP, remedy_map.serialize(levers))


def main():
    live = "--live" in sys.argv
    # USER-TIMEZONE TODAY, not the container's. The VPS container runs UTC, where date.today()
    # is already TOMORROW from 19:00 America/Chicago onward — every evening tick would judge
    # deadlines a day early and could fire an escalation a full day before it was due.
    today = businessday.today()
    print("=== case_tick (VPS/API)  %s  (%s) ===" % (today.isoformat(), "LIVE" if live else "DRY-RUN"))
    print("today resolved in %s (container clock is UTC — never date.today() here)"
          % businessday.profile_timezone())
    print("runtime: 24/7 VPS via api.multica.ai (laptop-independent, M20)\n")

    issues = mc.list_issues()
    # M39: resolved by CONVENTION, not by the hardcoded identifier "MER-16" — that was one issue on
    # ONE person's board, and a fresh user's board has none, so the sink silently became None. Same
    # helper mer_engine uses, so both agree on where a status note goes. Falls back to the case
    # itself, which is correct for the single-user product: no agent is assigned, so nothing can be
    # woken. Override with MER_ACTIVITY_LOG_ISSUE.
    try:
        import mer_engine
        mer16 = mer_engine._activity_log_issue({i.get("identifier") or i.get("id"): i
                                                for i in issues})
    except Exception:
        mer16 = None
    defs = mc.name_to_defs() if live else None
    tracked_issues = tracked_cases(issues)
    tracked_ids = {it.get("id") for it in tracked_issues}
    tracked, untracked, advanced, mapped, armed, flagged = [], [], [], [], [], []
    for it in issues:
        status = it.get("status") or ""
        title = it.get("title") or ""
        if status in ("done", "cancelled"):
            continue
        if it.get("project_id") == BUILD_PROJECT:
            continue
        if title.startswith("Daily case sweep") or title.startswith(("KING:", "SYSTEM:", "M1 ", "M2 ")):
            continue
        p = it.get("mr", {})
        phase = p.get(PHASE_PROP)
        if it.get("id") in tracked_ids:
            v = case_view(it)
            code, action = decide(phase, v["deadline"], v["intake_done"], v["awaiting_yes"], today)
            note = ""
            # --- TIER 0: ANY tracked case with an empty map gets one built ---
            # This used to fire ONLY at phase == RemedyMap. Nothing blocked a case leaving
            # RemedyMap on a blank map and nothing ever re-ran Tier 0, so MER-1 (Tier3),
            # MER-2 (Tier1) and MER-3 (Tier2) sat past the phase with empty maps and Tier 4
            # permanently unreachable — and the tick had no code path that could ever notice.
            # Runs BEFORE advancement so the map exists before the case moves. Dry-run writes
            # NOTHING. build() itself refuses to emit an empty map, so a case it cannot map
            # is reported rather than papered over.
            if not v["remedy_map"] and phase not in NEVER_WRITE_PHASES:
                if not live:
                    note = ("would BUILD the Tier-0 remedy map (%s is empty) "
                            "— dry-run, nothing written" % REMEDY_MAP_PROP)
                else:
                    try:
                        built, msg = build_remedy_map(it, v, mer16_id=mer16, defs=defs)
                    except Exception as e:
                        built, msg = False, "remedy map build FAILED: %s" % e
                    note = ("REMEDY MAP: " if built else "remedy map not written: ") + msg
                    if built:
                        mapped.append((v["identifier"], v["remedy_map"]))
            # --- NO-DEADLINE is a terminal stall: arm the clock ---
            # decide() has always SAID "arm the clock" and nothing ever did. See arm_deadline().
            if code == "NO-DEADLINE":
                pre = note + " | " if note else ""
                if not live:
                    note = pre + ("would ARM %s from the %s record (or today) — dry-run, "
                                  "nothing written" % (DEADLINE_PROP, SENT_BLOCK))
                else:
                    try:
                        done, msg = arm_deadline(it, v, today, mer16_id=mer16, defs=defs)
                    except Exception as e:
                        done, msg = False, "arm FAILED: %s" % e
                    note = pre + ("CLOCK ARMED: " if done else "clock not armed: ") + msg
                    if done:
                        armed.append((v["identifier"], v["phase"], v["deadline"]))
            # --- a reply we cannot classify is surfaced even when nothing else happens ---
            if v["phase"] not in ("Intake", "CaseFile"):
                _h = vendor_reply_hold(v.get("description"), v.get("mr"), today)
                if _h["flag_human"]:
                    flagged.append((v["identifier"], _h["disposition"], _h["why"]))
            # --- M37: the ladder climbs ---
            if code in ADVANCING_CODES:
                pre = note + " | " if note else ""   # keep the Tier-0 line; never overwrite it
                allowed, tgt, new_deadline, reason = gate_check(v, today)
                if not allowed:
                    note = pre + "GATE HELD: %s" % reason
                elif not live:
                    note = pre + "would ADVANCE -> %s, %s -> %s (dry-run — nothing written)" % (
                        tgt, DEADLINE_PROP, new_deadline)
                else:
                    try:
                        done, msg = advance(it, v, tgt, new_deadline, mer16_id=mer16, defs=defs)
                    except Exception as e:
                        done, msg = False, "advance FAILED: %s" % e
                    note = pre + ("ADVANCED: " if done else "not advanced: ") + msg
                    if done:
                        advanced.append((v["identifier"], v["phase"], tgt, new_deadline))
            tracked.append((it["identifier"], title, code, action, note))
        elif title.startswith(CASE_TITLE_HINTS):
            untracked.append((it["identifier"], title))

    print("-- ENGINE-TRACKED CASES --")
    for key, title, code, action, note in tracked:
        print("  [%-9s] %-7s %s" % (code, key, action))
        print("            %s" % title[:74])
        if note:
            print("            >> %s" % note)
    if not tracked:
        print("  (none)")
    if untracked:
        print("\n-- RETURN CASES NOT YET IN THE ENGINE (onboard: set MR Phase) --")
        for key, title in untracked:
            print("  ! %-7s %s" % (key, title[:76]))
    # M7 — hand every case sitting in the Day-3 nudge window to the general nudge engine. This is
    # what replaces the per-case crons (stride-trace-nudge, ppg-tier2-escalation); it only enqueues
    # in --live, and every enqueue still passes the idempotency guard + the send-mode gate.
    # Runs AFTER advancement so a case that just climbed carries its fresh deadline and is not
    # also nudged on the phase it has already left.
    # CALLED UNCONDITIONALLY under --live (fixed 2026-07-28). It used to run only if SOME
    # case in this walk emitted NUDGE — but enqueue_due() re-walks the whole board itself, so
    # whether case X got nudged depended on whether an unrelated case Y happened to be in its
    # Day-3 window on the same day. That coupling is invisible and produces missed follow-ups
    # that look like nothing at all. enqueue_due is independently idempotent (queue record +
    # send ledger), so calling it every tick is safe and is the only honest wiring.
    if live:
        try:
            import nudge  # noqa: E402
            nudge.enqueue_due(today)
        except Exception as e:
            print("  (nudge enqueue failed: %s)" % e)

    print("\nsummary: %d tracked, %d need onboarding, %d advanced, %d remedy map(s) built, "
          "%d clock(s) armed, %d reply flag(s). %s" %
          (len(tracked), len(untracked), len(advanced), len(mapped), len(armed), len(flagged),
           "dry-run — nothing written." if not live else "live."))
    for ident, frm, to, dl in advanced:
        print("  advanced %-8s %s -> %s (deadline %s)" % (ident, frm, to, dl))
    for ident, levers in mapped:
        print("  Tier-0 map %-8s %s" % (ident, ", ".join(levers)))
    for ident, ph, dl in armed:
        print("  clock armed %-8s %s -> %s" % (ident, ph, dl))
    if flagged:
        print("\n-- VENDOR REPLIES NEEDING A HUMAN (RECORD ONLY - NO ACTION REQUIRED) --")
        for ident, disp, why in flagged:
            print("  ! %-8s [%s] %s" % (ident, disp, why))


# ---------------------------------------------------------------- self-test

def _case(**kw):
    base = {"identifier": "MER-T", "id": "t", "title": "t", "status": "open", "phase": "Tier1",
            "deadline": None, "intake_done": True, "awaiting_yes": False,
            "remedy_map": [], "remedy_attempted": []}
    base.update(kw)
    return base


def _selftest():
    """Offline proof of the gate logic. No network, fabricated in-memory cases."""
    from datetime import timedelta
    today = date(2026, 7, 27)          # a Monday
    yday = today - timedelta(days=1)
    fails = []

    def check(label, got_allowed, want_allowed, reason, extra=True):
        ok = (got_allowed == want_allowed) and extra
        print("  %-4s %-52s allowed=%-5s  %s" % ("PASS" if ok else "FAIL", label, got_allowed, reason))
        if not ok:
            fails.append(label)

    print("=== case_tick M37 gate self-test (offline, fabricated cases) ===")

    # 1. awaiting-YES blocks, even on a long-overdue Tier1.
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday, awaiting_yes=True), today)
    check("awaiting-YES blocks an overdue Tier1", a, False, r, "Awaiting User YES" in r)

    # 2. intake incomplete blocks leaving Intake / CaseFile.
    a, t, d, r = gate_check(_case(phase="Intake", intake_done=False), today)
    check("intake incomplete blocks Intake", a, False, r, "Intake Complete" in r)
    a, t, d, r = gate_check(_case(phase="CaseFile", intake_done=False), today)
    check("intake incomplete blocks CaseFile", a, False, r, "Intake Complete" in r)

    # 3. intake complete DOES advance Intake -> CaseFile (green lane, no clock needed).
    a, t, d, r = gate_check(_case(phase="Intake", intake_done=True), today)
    check("Intake -> CaseFile on intake complete", a, True, r, t == "CaseFile" and d is not None)

    # 4. court needs remedy_gate: no map at all -> fail CLOSED.
    a, t, d, r = gate_check(_case(phase="PreSuit", deadline=yday), today)
    check("PreSuit -> Tier4 blocked with no remedy map", a, False, r, "remedy map" in r)

    # 5. court needs remedy_gate: map with an owed lever -> blocked.
    a, t, d, r = gate_check(_case(phase="PreSuit", deadline=yday,
                                  remedy_map=["tier1_vendor", "state_ag", "bbb"],
                                  remedy_attempted=["tier1_vendor", "state_ag"]), today)
    check("PreSuit -> Tier4 blocked with a lever owed", a, False, r, "bbb" in r)

    # 6. court opens only when every applicable lever is attempted + logged.
    a, t, d, r = gate_check(_case(phase="PreSuit", deadline=yday,
                                  remedy_map=["tier1_vendor", "state_ag", "bbb"],
                                  remedy_attempted=["tier1_vendor", "state_ag", "bbb"]), today)
    check("PreSuit -> Tier4 opens when remedy map exhausted", a, True, r, t == "Tier4")

    # 7. the clean case: an elapsed Tier1 climbs to Tier2 with a business-day deadline.
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday), today)
    want = businessday.business_day_deadline(today, PHASE_SLA_DAYS["Tier2"])
    check("Tier1 -> Tier2 on an elapsed deadline", a, True, r,
          t == "Tier2" and d == want and d.weekday() < 5)
    print("       (new deadline %s = %d business days from %s; calendar +%d would be %s)"
          % (d, PHASE_SLA_DAYS["Tier2"], today, PHASE_SLA_DAYS["Tier2"],
             today + timedelta(days=PHASE_SLA_DAYS["Tier2"])))

    # 8. an un-elapsed deadline does NOT advance (no early gate-jump).
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=today + timedelta(days=5)), today)
    check("Tier1 with 5d left does not advance", a, False, r, "not elapsed" in r)

    # 9. a Tier1 with no clock armed does not advance on vibes.
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=None), today)
    check("Tier1 with no deadline does not advance", a, False, r, "clock not armed" in r)

    # 10. never advance a closed case; never auto-close.
    a, t, d, r = gate_check(_case(phase="Tier2", deadline=yday, status="done"), today)
    check("closed (status=done) case never advances", a, False, r, "closed cases never advance" in r)
    a, t, d, r = gate_check(_case(phase="Closed", deadline=yday), today)
    check("phase=Closed never advances", a, False, r, "terminal" in r)

    # 11. Tier4 never auto-advances into the court filing — the principal files.
    a, t, d, r = gate_check(_case(phase="Tier4", deadline=yday), today)
    check("Tier4 never advances into '(user files)'", a, False, r, "never writes it" in r)

    # 12. RED-lane entry is flagged so the outbound waits for the user's YES.
    check("Tier2/Tier3/PreSuit/Tier4 entry is RED-lane",
          RED_ENTRY_PHASES >= {"Tier2", "Tier3", "PreSuit", "Tier4"}, True,
          "entering these sets %s=true" % YES_PROP)

    # 13. idempotency: the same advancement cannot be reserved twice (structural, not a flag file).
    import tempfile
    ledger = os.path.join(tempfile.gettempdir(), "mer_ledger_case_tick_test.json")
    if os.path.exists(ledger):
        os.remove(ledger)
    idempotency.LEDGER = ledger
    ok1, _ = idempotency.reserve("MER-T", "phase_advance:Tier1->Tier2", "board", "Tier2|%s" % want)
    ok2, _ = idempotency.reserve("MER-T", "phase_advance:Tier1->Tier2", "board", "Tier2|%s" % want)
    check("re-running the tick cannot double-advance", ok1 and not ok2, True,
          "1st reserve=%s, 2nd reserve=%s (expect True/False)" % (ok1, ok2))
    os.remove(ledger)

    # 14. dry-run writes nothing. Proved on the AST, not by grepping for a substring: every
    #     call to a writing function in main() must sit inside an `if`/`elif` whose test
    #     mentions `live`. A rearranged branch that lost the gate would now FAIL here.
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    parents = {}
    for nd in ast.walk(tree):
        for kid in ast.iter_child_nodes(nd):
            parents[kid] = nd

    def _live_gated(call_node):
        nd = call_node
        while nd in parents:
            nd = parents[nd]
            if isinstance(nd, ast.If) and "live" in {n.id for n in ast.walk(nd.test)
                                                     if isinstance(n, ast.Name)}:
                return True
            if isinstance(nd, ast.FunctionDef) and nd.name == "main":
                return False
        return False

    main_fn = next(nd for nd in tree.body if isinstance(nd, ast.FunctionDef) and nd.name == "main")
    for fname in ("advance", "build_remedy_map", "arm_deadline"):
        calls = [nd for nd in ast.walk(main_fn)
                 if isinstance(nd, ast.Call) and getattr(nd.func, "id", None) == fname]
        check("%s() is called at all (a self-test is not a wired module)" % fname,
              bool(calls), True, "%d call site(s) in main()" % len(calls))
        check("%s() is unreachable without --live" % fname,
              bool(calls) and all(_live_gated(c) for c in calls), True,
              "every call site sits inside an `if live` branch (AST-verified)")
    check("--live is the only thing that sets live",
          "live = \"--live\" in sys.argv" in src, True, "main() reads the flag from argv")

    # 14b. M44 — CLIENT CASES NEVER AUTO-ADVANCE, by any of the three convention signals, and
    #     not even when every other gate is wide open (intake done, deadline long elapsed).
    for label, kw in (
        ("CLIENT: title prefix", {"title": "CLIENT: Jordan Rivera - tablet via Acme"}),
        ("'CLIENT CASE' description marker", {"description": "CLIENT CASE. Bought a tablet."}),
        ("affirmative MR Client Case property", {"mr": {"MR Client Case": "yes"}}),
        ("MR Client Case = true (bool)", {"mr": {"MR Client Case": True}}),
    ):
        a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday, **kw), today)
        check("client case (%s) never advances" % label, a, False, r, "client case" in r)
    # ...at every rung of the ladder, including the green-lane intake hop.
    for ph in ("Intake", "CaseFile", "RemedyMap", "Tier1", "Tier2", "Tier3", "PreSuit"):
        a, t, d, r = gate_check(_case(phase=ph, deadline=yday,
                                      title="CLIENT: Jordan Rivera - tablet"), today)
        check("client case at %s never advances" % ph, a, False, r, "client case" in r)
    # ...and the identical NON-client case at Tier1 does advance, so the gate is the cause.
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday,
                                  title="Case: Acme Tools / tablet"), today)
    check("the same case titled 'Case:' DOES advance", a, True, r, t == "Tier2")

    # 14c. M44 — a substantive vendor reply newer than this phase's outbound holds the gate.
    rec = ("VENDOR/ITEM: Acme Tools — a tablet.\n\nSENT:\nTier 1 letter on 2026-07-10\n"
           "\nREPLIES:\nAcme wrote back 2026-07-20 offering a partial credit\n")
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday, description=rec), today)
    check("a reply newer than the outbound holds the gate", a, False, r, "vendor reply" in r)
    stale = ("VENDOR/ITEM: Acme Tools — a tablet.\n\nSENT:\nTier 1 letter on 2026-07-22\n"
             "\nREPLIES:\nAcme auto-ack 2026-07-02\n")
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday, description=stale), today)
    check("a reply OLDER than the outbound does not hold", a, True, r, t == "Tier2")
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday,
                                  mr={"MR Vendor Replied": "yes"}), today)
    check("an affirmative MR Vendor Replied property holds the gate", a, False, r,
          "vendor reply" in r)

    # 14d. M44 — THE WRITE IS VERIFIED, NOT ASSUMED. The documented API trap is that
    #     PUT /api/issues/<id> {"properties": …} answers 200 OK and discards them. Simulate
    #     exactly that: a set_properties that succeeds and changes nothing. advance() must
    #     report FAILURE and must RELEASE its idempotency reservation so the next tick retries.
    idempotency.LEDGER = ledger = os.path.join(tempfile.gettempdir(), "mer_ledger_m44_test.json")
    if os.path.exists(ledger):
        os.remove(ledger)
    board_state = {PHASE_PROP: "Tier1", DEADLINE_PROP: "2026-07-26"}
    real_set, real_get, real_comment = mc.set_properties, mc.get_issue, mc.add_comment
    comments = []

    def _silently_discard(issue, values, ws=None, defs=None):
        return {"ok": True}                       # 200 OK, board unchanged — the trap

    def _honest_set(issue, values, ws=None, defs=None):
        board_state.update({k: (v.isoformat() if hasattr(v, "isoformat") else v)
                            for k, v in values.items()})
        return {"ok": True}

    def _fake_get(issue_id, ws=None):
        return {"id": issue_id, "properties": {}, "mr": dict(board_state)}

    try:
        mc.set_properties, mc.get_issue = _silently_discard, _fake_get
        mc.add_comment = lambda iid, content, ws=None: comments.append((iid, content))
        v = _case(phase="Tier1", deadline=yday)
        want_dl = businessday.business_day_deadline(today, PHASE_SLA_DAYS["Tier2"])
        done, msg = advance({"id": "t"}, v, "Tier2", want_dl, defs={})
        check("a silently-discarded write is reported as NOT advanced", done, False, msg,
              "WRITE DID NOT LAND" in msg)
        check("...and its idempotency reservation is RELEASED (the tick can retry)",
              idempotency._load() == {}, True, "ledger is empty after the failed write")

        # the retry, against a board that honours the write, must now succeed.
        mc.set_properties = _honest_set
        done, msg = advance({"id": "t"}, v, "Tier2", want_dl, defs={})
        check("the retry advances once the write lands", done, True, msg,
              "read-back confirms" in msg)
        check("read-back saw the NEW phase on the board", board_state[PHASE_PROP], "Tier2",
              "board now: %s" % board_state)
        check("entering a RED-lane phase set awaiting-YES on the board",
              truthy(board_state.get(YES_PROP)), True, "%s=%r" % (YES_PROP, board_state.get(YES_PROP)))

        # a write that RAISES must also release, not strand the case.
        if os.path.exists(ledger):
            os.remove(ledger)

        def _boom(issue, values, ws=None, defs=None):
            raise RuntimeError("Multica API PUT -> 503")
        mc.set_properties = _boom
        done, msg = advance({"id": "t"}, v, "Tier2", want_dl, defs={})
        check("a raising write is reported as NOT advanced", done, False, msg, "FAILED" in msg)
        check("...and releases its reservation too", idempotency._load() == {}, True,
              "ledger is empty")
        check("a FAILED advance logs nothing to the board",
              len(comments), 1, "%d comment(s) — only the one successful advance logged"
              % len(comments))
    finally:
        mc.set_properties, mc.get_issue, mc.add_comment = real_set, real_get, real_comment
        if os.path.exists(ledger):
            os.remove(ledger)

    check("advance() reads the value back before claiming success",
          "verify_written(" in src and "_release_reservation(key)" in src, True,
          "no success is claimed on an HTTP 200 alone")

    # 15. TIER 0 IS WIRED. Court was unreachable by construction because nothing built a
    #     remedy map; a RemedyMap case with an empty map must now produce a real lever list.
    plan = remedy_map.build(remedy_map.case_view({
        "identifier": "MER-T0", "id": "t0", "title": "Case: Test Vendor - widget $250",
        "description": "VENDOR/ITEM: Test Vendor. AMOUNT/CARD: $250 paid cash.",
        "mr": {PHASE_PROP: "RemedyMap", "MR Jurisdiction": "TX / Dallas County"},
    }))
    check("Tier 0 builds a non-empty, case-specific map", bool(plan["levers"]), True,
          "levers: %s" % ", ".join(plan["levers"]))
    check("every lever Tier 0 emits is one remedy_gate consumes",
          not (set(plan["levers"]) - set(remedy_gate.LEVER_LABELS)), True,
          "no key the gate cannot resolve")
    check("Tier 0 never emits a court lever (court cannot gate on itself)",
          not (set(plan["levers"]) & remedy_gate.COURT_LEVERS), True, "COURT_LEVERS excluded")
    rc = remedy_gate.remedy_complete(plan["levers"], plan["levers"])
    check("a fully-attempted Tier-0 map makes Tier 4 REACHABLE",
          rc["ready_for_court"], True, "court opens once the map is exhausted")

    # 16. dry-run still writes nothing on the Tier-0 path either.
    check("build_remedy_map() is only reachable under --live",
          "if not live:" in src and "build_remedy_map(it, v," in src, True,
          "dry-run branch precedes the write branch in main()")

    # =====================================================================================
    # 2026-07-28 — the state-machine gate fixes. Every case below is the REAL live shape.
    # =====================================================================================

    # 17. THE REPLY-HOLD GATE IS NO LONGER A NO-OP, and it is no longer inverted.
    print("\n-- 17. reply hold: reads the board property, and judges what the reply SAID --")
    # 17a. the property alone holds — this is the signal the old code had no way to see.
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday,
                                  mr={LAST_REPLY_PROP: "2026-07-27"}), today)
    check("MR Last Vendor Reply (the new property) HOLDS the gate", a, False, r,
          "vendor reply on record" in r)
    # 17b. a BRUSH-OFF does NOT hold. This is the inversion fix: "escalated internally is not
    #      resolution", so the reply that most deserves escalation must not block it.
    brush = ("VENDOR/ITEM: Acme.\n\nSENT:\nTier 1 letter 2026-07-10\n\nREPLIES:\n"
             "2026-07-24 Acme: thank you for your patience, we have escalated this "
             "internally and our team will get back to you.\n")
    h = vendor_reply_hold(brush, {LAST_REPLY_PROP: "2026-07-24"}, today)
    check("a BRUSH-OFF does not hold the ladder", h["hold"], False,
          h["why"], h["disposition"] == "brush_off")
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday, description=brush,
                                  mr={LAST_REPLY_PROP: "2026-07-24"}), today)
    check("...so the case still climbs Tier1 -> Tier2 over a brush-off", a, True, r, t == "Tier2")
    # 17c. a REAL resolution holds and routes toward close, never toward a new tier.
    good = ("VENDOR/ITEM: Acme.\n\nSENT:\nTier 1 letter 2026-07-10\n\nREPLIES:\n"
            "2026-07-24 Acme: a refund of $249.00 has been issued to your card.\n")
    h = vendor_reply_hold(good, {LAST_REPLY_PROP: "2026-07-24",
                                 REMEDY_TYPE_PROP: "refund"}, today)
    check("a REAL resolution holds and routes toward close_case", h["hold"], True,
          h["why"], h["resolved"] is True and "close_case" in h["why"])
    # 17d. an unreadable reply holds AND flags a human.
    murky = ("SENT:\nTier 1 letter 2026-07-10\n\nREPLIES:\n"
             "2026-07-24 Acme sent a message about the order.\n")
    h = vendor_reply_hold(murky, {LAST_REPLY_PROP: "2026-07-24"}, today)
    check("an UNCLASSIFIABLE reply holds AND flags a human", h["hold"], True, h["why"],
          h["flag_human"] is True and h["disposition"] == "unclassifiable")
    # 17e. NO HOLD IS PERMANENT — the unclassifiable hold lapses on a timeout.
    old_reply = today - timedelta(days=60)
    h = vendor_reply_hold(murky.replace("2026-07-24", old_reply.isoformat()),
                          {LAST_REPLY_PROP: old_reply.isoformat()}, today)
    check("an unclassifiable hold LAPSES after the timeout (no permanent hold)",
          h["hold"], False, h["why"],
          h["disposition"] == "expired" and h["flag_human"] is True)
    # 17f. resolution_check is actually imported and used — the module that exists to reject
    #      brush-offs was never imported by this file before today.
    check("resolution_check is wired into the hold (not merely documented)",
          "resolution_check" in src and "resolution_check.rule_based(" in src, True,
          "the brush-off detector is on the code path")

    # 18. A CASE CANNOT LEAVE RemedyMap WITH AN EMPTY MAP.
    print("\n-- 18. RemedyMap may not be left empty; court stays reachable --")
    a, t, d, r = gate_check(_case(phase="RemedyMap", deadline=yday, remedy_map=[]), today)
    check("RemedyMap -> Tier1 REFUSED on an empty map", a, False, r,
          "structurally unreachable" in r)
    a, t, d, r = gate_check(_case(phase="RemedyMap", deadline=yday,
                                  remedy_map=["tier1_vendor", "bbb"]), today)
    check("RemedyMap -> Tier1 allowed once the map exists", a, True, r, t == "Tier1")
    # ...and `missing` is surfaced from Tier1 onward, not only at the last hop.
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday,
                                  remedy_map=["tier1_vendor", "state_ag", "bbb"],
                                  remedy_attempted=["tier1_vendor"]), today)
    check("owed levers are surfaced at Tier1, not discovered at PreSuit", a, True, r,
          "still owed before Tier 4" in r and "bbb" in r)
    a, t, d, r = gate_check(_case(phase="Tier2", deadline=yday, remedy_map=[]), today)
    check("an empty map is CALLED OUT on every hop past RemedyMap", a, True, r,
          "remedy map EMPTY" in r)
    # the Tier-0 build is no longer restricted to cases sitting at RemedyMap.
    check("Tier 0 is triggered by an EMPTY MAP, not by the phase",
          'if not v["remedy_map"] and phase not in NEVER_WRITE_PHASES:' in src, True,
          "MER-1/2/3 were all past RemedyMap with empty maps and unreachable")

    # 19. HAND-WRITTEN MAPS ARE VALIDATED ON READ — the real MER-76 map.
    print("\n-- 19. an unsatisfiable hand-written map is a LOUD refusal, not a silent hold --")
    mer76 = ["tier1_vendor", "tier2_exec", "state_ag", "texas_dtpa", "bbb_southwest",
             "magnuson_moss", "small_claims_dallas"]
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday, remedy_map=mer76), today)
    check("MER-76's live map REFUSES to advance", a, False, r, "UNSATISFIABLE" in r)
    check("...and the refusal names the circular court key", "small_claims_dallas" in r, True,
          "court cannot be a prerequisite for court")
    check("...and names the unsatisfiable keys", "texas_dtpa" in r and "bbb_southwest" in r,
          True, "log_attempt() refuses unknown keys by design")
    check("...and prints the corrected map so the refusal is actionable",
          "Suggested corrected map" in r, True, "a loud refusal, never a silent forever-hold")
    a, t, d, r = gate_check(_case(phase="Tier1", deadline=yday,
                                  remedy_map=remedy_map.normalize_map(mer76)[0]), today)
    check("the normalised map advances cleanly", a, True, r, t == "Tier2")

    # 20. remedy_gate FAILS CLOSED ON AN EMPTY MAP IN THE LIBRARY (not just in callers).
    check("remedy_gate.remedy_complete([], []) does NOT open court",
          remedy_gate.remedy_complete([], [])["ready_for_court"], False,
          "the guard lives in the library now")

    # 21. NO-DEADLINE IS NO LONGER A TERMINAL STALL.
    print("\n-- 21. a tier phase with no clock gets one armed --")
    code, action = decide("Tier1", None, True, False, today)
    check("a tier phase with no deadline reports NO-DEADLINE", code, "NO-DEADLINE", action)
    idempotency.LEDGER = ledger = os.path.join(tempfile.gettempdir(), "mer_ledger_arm_test.json")
    if os.path.exists(ledger):
        os.remove(ledger)
    board2 = {PHASE_PROP: "Tier1"}
    real_set, real_get, real_comment = mc.set_properties, mc.get_issue, mc.add_comment
    logged = []
    try:
        mc.set_properties = lambda issue, values, ws=None, defs=None: board2.update(values)
        mc.get_issue = lambda iid, ws=None: {"id": iid, "properties": {}, "mr": dict(board2)}
        mc.add_comment = lambda iid, content, ws=None: logged.append(content)
        v = _case(phase="Tier1", deadline=None,
                  description="SENT:\nTier 1 letter on 2026-07-20\n")
        done, msg = arm_deadline({"id": "t"}, v, today, defs={})
        want_arm = businessday.business_day_deadline(date(2026, 7, 20), PHASE_SLA_DAYS["Tier1"])
        check("the clock is armed from the SENT record", done, True, msg,
              board2.get(DEADLINE_PROP) == want_arm.isoformat())
        check("...arming NEVER changes the phase or sets awaiting-YES",
              board2.get(PHASE_PROP) == "Tier1" and YES_PROP not in board2, True,
              "board now: %s" % board2)
        check("...and it is logged RECORD-ONLY",
              bool(logged) and "RECORD ONLY - NO ACTION REQUIRED" in logged[0], True,
              "a comment on a live-agent issue is read as an instruction (workspace rule #1)")
        done2, msg2 = arm_deadline({"id": "t"}, v, today, defs={})
        check("...and re-arming the same clock is refused (idempotent)", done2, False, msg2,
              "already armed" in msg2)
        # with no SENT record it anchors on today
        if os.path.exists(ledger):
            os.remove(ledger)
        board2.pop(DEADLINE_PROP, None)
        v2 = _case(phase="Tier2", deadline=None, description="no outbound logged")
        done3, msg3 = arm_deadline({"id": "t2"}, v2, today, defs={})
        check("with no SENT line the clock anchors on today", done3, True, msg3,
              board2.get(DEADLINE_PROP) ==
              businessday.business_day_deadline(today, PHASE_SLA_DAYS["Tier2"]).isoformat())
    finally:
        mc.set_properties, mc.get_issue, mc.add_comment = real_set, real_get, real_comment
        if os.path.exists(ledger):
            os.remove(ledger)

    # 22. THE CLIENT-CASE MARKER IS FOUND ANYWHERE IN THE OPENING BLOCK (MER-4's real shape).
    # Fixture shape is real; the name is not. A self-test that carries a real client's
    # name ships that name to every recipient of this package — the publish gate caught
    # exactly this on 2026-07-28. Fixtures use fictional people.
    mer4_desc = ('NEEDS-OWNER: re-send the intake questions to the client. Their 2026-07-18 '
                 'email arrived EMPTY.\n\nCLIENT CASE — first external client of the '
                 'merchandise-return service.\n\nCLIENT: Jordan Rivera')
    check("the 'CLIENT CASE' marker is found on line 3, not just at offset 0",
          is_client_case({"title": "Jordan Rivera - tablet", "description": mer4_desc}), True,
          "MER-4's description opens with NEEDS-KING:; startswith() missed it entirely")
    check("...while an ordinary case is still not a client case",
          is_client_case({"title": "Case: Acme", "description": "VENDOR/ITEM: Acme."}), False,
          "the widening does not swallow self cases")

    # 23. TIMEZONE — no deadline decision may use the container's UTC date.
    # AST, not a substring, so this check cannot be fooled by its own message.
    _utc_today_calls = [nd for nd in ast.walk(tree)
                        if isinstance(nd, ast.Call)
                        and isinstance(nd.func, ast.Attribute) and nd.func.attr == "today"
                        and isinstance(nd.func.value, ast.Name) and nd.func.value.id == "date"]
    check("case_tick never calls the container-clock today() for a decision",
          len(_utc_today_calls), 0,
          "businessday.today() resolves the USER's today (UTC rolls over at 19:00 CT)")
    check("gate_check defaults 'today' to the profile timezone",
          "today = today or businessday.today()" in src, True, "one source of 'today'")

    # 24. NUDGE COUPLING — enqueue_due() re-walks the whole board, so it must not be
    #     conditional on some OTHER case happening to emit NUDGE in this walk.
    check("nudge.enqueue_due is called unconditionally under --live",
          'if live:\n        try:\n            import nudge' in src, True,
          "whether case X is nudged no longer depends on case Y")

    if fails:
        print("\nFAIL — %d gate(s) wrong: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("\nPASS — the ladder climbs only when every gate clears; Tier 0 writes the remedy map "
          "that makes court reachable; dry-run writes nothing.")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    main()
