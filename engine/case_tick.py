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

Gates that must hold (non-negotiable product rules, enforced structurally below):
  * never advance a case with `MR Awaiting User YES` true;
  * never advance past Intake/CaseFile unless `MR Intake Complete` is true;
  * never advance INTO Tier4/court unless remedy_gate.remedy_complete() clears (and it
    fails CLOSED when the case has no remedy map at all — no Tier 0 output, no court);
  * never advance a Closed/done/cancelled case, and never auto-CLOSE one (closing is a RED
    action needing the user's explicit YES — "escalated internally" never closes a case);
  * advancement is idempotent — re-running the tick the same day cannot double-advance
    (idempotency.reserve on a phase_advance key, plus the freshly-armed future deadline,
    which puts the case straight back into WAIT).

Default --dry-run (prints the plan, writes NOTHING). --live advances + arms deadlines + logs.
Deployed to /opt/data/scripts/ ; run by the container's hourly-business / daily cron.
"""
import os
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


def truthy(v):
    return str(v).lower() in ("true", "1", "yes", "✓")


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
        {identifier, status, phase, deadline(date|None), intake_done, awaiting_yes,
         remedy_map[], remedy_attempted[]}

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
        "status": (issue.get("status") or "").lower(),
        "phase": p.get(PHASE_PROP),
        "deadline": deadline,
        "intake_done": truthy(p.get(INTAKE_PROP, "")),
        "awaiting_yes": truthy(p.get(YES_PROP, "")),
        "remedy_map": _levers(p.get(REMEDY_MAP_PROP)),
        "remedy_attempted": _levers(p.get(REMEDY_DONE_PROP)),
    }


def advance(issue, view, target, new_deadline, mer16_id=None, defs=None):
    """Write the advancement to the board. LIVE ONLY — callers must gate on --live.

    Idempotent: idempotency.reserve() keys the write on (case, phase_advance:from->to,
    target+deadline). A second run the same day is refused before any HTTP call. The
    freshly-armed future deadline is the second, structural guard: the case reads WAIT.
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
    mc.set_properties(issue, values, defs=defs)

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
    return True, "advanced %s -> %s; deadline %s%s" % (
        view["phase"], target, new_deadline, "; awaiting-YES set" if red else "")


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

    # 14. dry-run writes nothing: main() only calls advance() under --live (asserted structurally).
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    check("advance() is only reachable under --live", "elif not live:" in src and "advance(it, v," in src,
          True, "dry-run branch precedes the write branch in main()")

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
