#!/usr/bin/env python3
"""
remedy_gate.py — M14 remedy-completeness gate before Tier 4 / small-claims court.

The skill's goal is to *exhaust every avenue before small-claims court*. Tier 0
(remedy mapping) builds a case-specific list of applicable levers — the ones that
actually apply to THIS case (its industry regulator, its state statute + pre-suit
notice, an arbitration clause if the contract has one, a civil-rights avenue if the
facts support it, a class-action check, a local nonprofit / mediation option, plus
the standard tiers). Court (Tier 4) must be *structurally unreachable* until every
applicable lever in that map was actually attempted and logged.

This is the same lesson as the 2026-07-17 gate-jump: a written "do X before Y" rule
is not enforcement. remedy_complete() is the enforcement — the court task must call
it and refuse to open while it returns ready_for_court=False. A markdown protocol
cannot stop an agent; a False return can.

Design notes:
  - remedy_map is the *applicable* levers for the case (Tier 0's output). Levers that
    do not apply to a case are simply never in the map, so they are never "owed."
  - The court lever itself is excluded from the completeness check — court is the
    thing being gated, not a prerequisite for itself. It is recognized by name so a
    case whose map lists it (as the destination) is not blocked on itself.
  - "attempted" means done AND logged. A lever the agent merely intended, or fired
    without recording, does not count — pass in only what the case ledger confirms.
  - Order-independent and duplicate-safe: sets, not sequence. Ordering between tiers
    is enforced elsewhere (the per-phase Multica task dependencies); this gate only
    answers "is the whole remedy map exhausted yet?"

stdlib only.
"""

# Lever keys that ARE the court destination, not a prerequisite. Excluded from the
# completeness check so a case never blocks on reaching court as a condition of
# reaching court.
COURT_LEVERS = frozenset({
    "tier4_court",
    "small_claims",
    "court",
})

# Human-readable labels for the summary. Any key not listed falls back to a
# de-underscored title-case rendering, so unknown/new lever keys still read cleanly.
LEVER_LABELS = {
    "tier1_vendor": "Tier 1 — vendor / retention contact",
    "tier2_exec": "Tier 2 — executive escalation",
    "industry_regulator": "Industry regulator complaint",
    "state_ag": "State Attorney General complaint",
    "bbb": "BBB complaint",
    "ftc": "FTC report",
    "pre_suit_notice": "Statutory pre-suit demand notice",
    "state_statute": "State consumer-protection statute remedy",
    "arbitration": "Contractual arbitration clause",
    "civil_rights": "Civil-rights / discrimination avenue",
    "class_action": "Class-action check",
    "nonprofit_mediation": "Local nonprofit / mediation",
    "online_reviews": "Public review / reputation channel",
    "tier4_court": "Tier 4 — small-claims court",
    "small_claims": "Tier 4 — small-claims court",
    "court": "Tier 4 — small-claims court",
}


def _label(lever):
    return LEVER_LABELS.get(lever, lever.replace("_", " ").strip().title())


def remedy_complete(remedy_map, attempted):
    """
    Decide whether court (Tier 4) may open.

    Args:
        remedy_map: iterable of lever keys applicable to THIS case (Tier 0 output).
        attempted:  iterable of lever keys actually done AND logged for the case.

    Returns dict:
        {
          "ready_for_court": bool,   # True only when every non-court applicable
                                     #   lever is in attempted
          "missing":  [levers],      # applicable levers still owed (sorted, deduped)
          "attempted":[levers],      # applicable levers confirmed done (sorted, deduped)
        }

    Levers in attempted that are not in remedy_map are ignored — they cannot make an
    inapplicable avenue "count," and they cannot fabricate readiness.
    """
    required = {str(x) for x in remedy_map} - COURT_LEVERS
    done = {str(x) for x in attempted}

    missing = required - done
    attempted_applicable = required & done

    return {
        "ready_for_court": len(missing) == 0,
        "missing": sorted(missing),
        "attempted": sorted(attempted_applicable),
    }


def summarize(remedy_map, attempted, case=None):
    """Short human summary of what is still owed before court."""
    r = remedy_complete(remedy_map, attempted)
    head = "Case %s — " % case if case else ""
    if r["ready_for_court"]:
        return (
            "%sREADY FOR TIER 4 (court). All %d applicable remedy levers attempted + logged: %s."
            % (head, len(r["attempted"]), ", ".join(_label(x) for x in r["attempted"]))
        )
    lines = [
        "%sNOT READY FOR COURT — %d of %d remedy levers still owed:"
        % (head, len(r["missing"]), len(r["missing"]) + len(r["attempted"]))
    ]
    for lever in r["missing"]:
        lines.append("  [ ] %s (%s)" % (_label(lever), lever))
    if r["attempted"]:
        lines.append("  Already attempted + logged: %s." % ", ".join(_label(x) for x in r["attempted"]))
    return "\n".join(lines)


if __name__ == "__main__":
    # --- Self-test 1: 6-lever map, 5 attempted -> not ready, 6th is missing ---
    remedy_map = [
        "tier1_vendor", "tier2_exec", "industry_regulator",
        "state_ag", "bbb", "pre_suit_notice",
    ]
    attempted_5 = [
        "tier1_vendor", "tier2_exec", "industry_regulator",
        "state_ag", "bbb",
    ]  # pre_suit_notice deliberately not done
    r1 = remedy_complete(remedy_map, attempted_5)
    print("=== Test 1: 6-lever map, 5 attempted ===")
    print(r1)
    print(summarize(remedy_map, attempted_5, case="MER-TEST"))
    assert r1["ready_for_court"] is False, "T1: should NOT be ready with a lever outstanding"
    assert r1["missing"] == ["pre_suit_notice"], "T1: missing must be exactly the 6th lever"
    assert len(r1["attempted"]) == 5, "T1: five levers should be counted attempted"

    # --- Self-test 2: fully attempted -> ready ---
    attempted_all = list(remedy_map)
    r2 = remedy_complete(remedy_map, attempted_all)
    print("\n=== Test 2: 6-lever map, all 6 attempted ===")
    print(r2)
    print(summarize(remedy_map, attempted_all, case="MER-TEST"))
    assert r2["ready_for_court"] is True, "T2: should be ready when all levers attempted"
    assert r2["missing"] == [], "T2: nothing should be missing"

    # --- Test 3: court lever in the map must not block on itself, nor grant readiness ---
    map_with_court = remedy_map + ["tier4_court"]
    r3a = remedy_complete(map_with_court, attempted_all)          # all non-court done
    r3b = remedy_complete(map_with_court, attempted_5)            # one still owed
    print("\n=== Test 3: court lever excluded from its own gate ===")
    print("all non-court attempted ->", r3a)
    print("one non-court owed      ->", r3b)
    assert r3a["ready_for_court"] is True, "T3a: court in map must not block when others done"
    assert "tier4_court" not in r3a["attempted"], "T3: court lever is not a prerequisite"
    assert r3b["ready_for_court"] is False, "T3b: still gated when a real lever is owed"

    # --- Test 4: extra attempted levers not in the map cannot fabricate readiness ---
    r4 = remedy_complete(remedy_map, attempted_5 + ["some_unrelated_thing"])
    print("\n=== Test 4: stray attempted lever cannot fake completeness ===")
    print(r4)
    assert r4["ready_for_court"] is False, "T4: stray attempt must not satisfy an owed lever"
    assert r4["missing"] == ["pre_suit_notice"], "T4: real lever still owed"

    print("\nPASS — court is structurally unreachable until every applicable remedy lever is attempted + logged.")
