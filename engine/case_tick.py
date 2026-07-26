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
    "Tier1": 7,      # ladder.md: Tier 1 window = 7 business days, Day-3 nudge
    "Tier2": 7,
    "Tier3": 7,
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
AFFIRMATIVE = {"yes", "true", "y", "1", "client", "client case", "on", "checked", "✓"}

# ---- substantive-reply detection (M44) -------------------------------------------------
# Description blocks in which a vendor reply is logged. Advancement means "the window lapsed
# in silence"; a reply logged AFTER the current phase's outbound means it did not.
REPLY_BLOCKS = ("REPLIES", "REPLY", "RECEIVED", "VENDOR REPLY", "INBOUND")
SENT_BLOCK = "SENT"
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    if (issue.get("description") or "").lstrip().upper().startswith(CLIENT_DESC_MARKER):
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


def vendor_reply_hold(desc, mr=None):
    """(hold: bool, why: str) — did the vendor answer this phase's outbound?

    An advancement asserts the counterparty went silent for the whole window. If the case
    record logs a reply NEWER than the most recent outbound, it did not, and a human decides
    what the reply means before a new channel is opened.

    Signals, in order:
      * an affirmative `MR Vendor Replied`-style property (opt-in; absent from the default
        schema, so this costs nothing on a stock board);
      * a REPLIES/RECEIVED/… block whose newest date is >= the newest date in the SENT block.
    A reply block with dates but a SENT block with none is held (fail-safe): we cannot show
    the reply is stale, and holding only costs a manual escalation.
    Clearing a hold is a human action: log the newer outbound in SENT, or clear the block.
    """
    for name, val in (mr or {}).items():
        n = str(name).lower()
        if "replied" in n or "reply received" in n:
            if _affirmative(val):
                return True, "%s is affirmative — a vendor reply is on record" % name
    reply_txt = " ".join(_block(desc, lab) for lab in REPLY_BLOCKS).strip()
    if not reply_txt:
        return False, ""
    reply_at = _latest_date(reply_txt)
    if not reply_at:
        return False, ""
    sent_at = _latest_date(_block(desc, SENT_BLOCK))
    if sent_at is None:
        return True, ("a vendor reply is logged (%s) and the record has no dated %s line to "
                      "show it is stale — holding" % (reply_at, SENT_BLOCK))
    if reply_at >= sent_at:
        return True, ("a vendor reply dated %s is newer than this phase's outbound (%s) — the "
                      "window did not lapse in silence; a human reads the reply" % (reply_at, sent_at))
    return False, ""


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
        return "NO-DEADLINE", "%s with no MR Phase Deadline set — arm the clock" % phase
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
    today = today or date.today()
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

        # GATE 3b — the window must have lapsed IN SILENCE. A substantive vendor reply logged
        # after this phase's outbound means the counterparty engaged; escalating to a new
        # channel over the top of a live conversation is a human's call, not a timer's.
        held, why = vendor_reply_hold(case.get("description"), case.get("mr"))
        if held:
            return False, None, None, "substantive vendor reply on record — %s" % why

    # GATE 4 — court is structurally unreachable until the remedy map is exhausted.
    if target in COURT_PHASES:
        rmap = case.get("remedy_map") or []
        if not rmap:
            return False, None, None, (
                "entering %s requires a Tier-0 remedy map; %s is empty — failing CLOSED "
                "(no map means the levers were never enumerated, not that none apply)"
                % (target, REMEDY_MAP_PROP))
        r = remedy_gate.remedy_complete(rmap, case.get("remedy_attempted") or [])
        if not r["ready_for_court"]:
            return False, None, None, "remedy_gate: %d lever(s) still owed before %s: %s" % (
                len(r["missing"]), target, ", ".join(r["missing"]))

    sla = PHASE_SLA_DAYS.get(target)
    new_deadline = businessday.business_day_deadline(today, sla) if sla else None
    return True, target, new_deadline, "gates cleared — %s -> %s" % (phase, target)


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
    today = date.today()
    print("=== case_tick (VPS/API)  %s  (%s) ===" % (today.isoformat(), "LIVE" if live else "DRY-RUN"))
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
    tracked, untracked, advanced, mapped = [], [], [], []
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
        if phase:
            v = case_view(it)
            code, action = decide(phase, v["deadline"], v["intake_done"], v["awaiting_yes"], today)
            note = ""
            # --- TIER 0: a case sitting at RemedyMap with an empty map gets one built ---
            # Runs BEFORE advancement so the map exists before the case leaves the phase that
            # is supposed to produce it. Dry-run writes NOTHING.
            if phase == REMEDY_MAP_PHASE and not v["remedy_map"]:
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
    if live and any(code == "NUDGE" for _, _, code, _, _ in tracked):
        try:
            import nudge  # noqa: E402
            nudge.enqueue_due(today)
        except Exception as e:
            print("  (nudge enqueue failed: %s)" % e)

    print("\nsummary: %d tracked, %d need onboarding, %d advanced, %d remedy map(s) built. %s" %
          (len(tracked), len(untracked), len(advanced), len(mapped),
           "dry-run — nothing written." if not live else "live."))
    for ident, frm, to, dl in advanced:
        print("  advanced %-8s %s -> %s (deadline %s)" % (ident, frm, to, dl))
    for ident, levers in mapped:
        print("  Tier-0 map %-8s %s" % (ident, ", ".join(levers)))


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
    for fname in ("advance", "build_remedy_map"):
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
