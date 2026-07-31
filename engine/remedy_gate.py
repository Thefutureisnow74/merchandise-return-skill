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

# Court keys are also written BY HAND, with a venue glued on: MER-76's map (2026-07-28)
# contained `small_claims_dallas`, which is not in COURT_LEVERS, so remedy_complete counted
# it as an ordinary owed lever — court became a prerequisite for court, the exact circularity
# the module docstring claims to prevent, and the case could never reach Tier 4.
# is_court_lever() recognises the FAMILY, not just the three canonical spellings.
COURT_PREFIXES = ("small_claims", "smallclaims", "tier4", "court", "justice_court",
                  "district_court", "magistrate")


def is_court_lever(lever):
    """True if `lever` names the court destination in any plausible hand-written form.

    Recognises the canonical keys plus venue-suffixed variants (small_claims_dallas,
    tier4_court_tx, court_jp_precinct_3, ...). Deliberately one-way/greedy: a false positive
    means one lever is not required before court; a false negative means COURT IS A
    PREREQUISITE FOR COURT and the case is barred from the courthouse forever. The second
    failure is unrecoverable without a human noticing, so we widen.
    """
    k = str(lever or "").strip().lower()
    if not k:
        return False
    if k in COURT_LEVERS:
        return True
    return any(k == p or k.startswith(p + "_") for p in COURT_PREFIXES)

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
    # Federal warranty claim. Added 2026-07-28 because it appeared in a hand-written live map
    # (MER-76) where, being unknown to this table, log_attempt() refused it BY DESIGN — the
    # lever could never be satisfied and the case could never reach Tier 4. A real, nameable
    # avenue belongs in the vocabulary rather than being silently unsatisfiable.
    "magnuson_moss": "Magnuson-Moss Warranty Act (federal warranty claim)",
    "arbitration": "Contractual arbitration clause",
    "civil_rights": "Civil-rights / discrimination avenue",
    "class_action": "Class-action check",
    "nonprofit_mediation": "Local nonprofit / mediation",
    "online_reviews": "Public review / reputation channel",
    # M53 — the RECALL CHECK, run at Tier 0 before a single letter goes out.
    #
    # A recall is the single most powerful fact a consumer can hold, and it is the one nobody
    # thinks to look for. It moves the argument off "is this vendor willing to help me" and onto
    # "this product was declared defective by its own maker or by a federal regulator" — which no
    # retention script, no expired warranty and no lost receipt can answer. Where a recall exists,
    # a receipt is usually irrelevant: recall remedies are keyed to the model and serial, not to
    # proof of purchase, which is exactly the wall a lost-receipt case hits.
    #
    # It is deliberately its own lever rather than a note inside class_action: a recall is a
    # regulator-or-manufacturer FINDING, while a class action is other people's litigation. They
    # carry different weight and one does not imply the other.
    #
    # Sources: CPSC / saferproducts.gov (consumer goods), NHTSA (vehicles and child seats),
    # FDA (food, drugs, devices, cosmetics), USDA FSIS (meat and poultry), plus the
    # manufacturer's own recall and service-bulletin pages.
    "recall_check": "Manufacturer / regulator recall + documented-defect check (CPSC, NHTSA, FDA)",
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
          "no_map":   bool,          # there was nothing to exhaust — fails CLOSED
          "reason":   str,           # why the verdict is what it is
        }

    Levers in attempted that are not in remedy_map are ignored — they cannot make an
    inapplicable avenue "count," and they cannot fabricate readiness.

    ⚠️ FAILS CLOSED ON AN EMPTY MAP (2026-07-28). Set arithmetic used to make an empty map
    the MOST permissive input there is: empty required -> empty missing -> ready_for_court
    True. Every caller therefore had to remember to test `if not remedy_map` FIRST, and only
    two of them did. The guard now lives in the library, where it cannot be forgotten: an
    empty map (or a map containing nothing but court levers) means the levers were never
    ENUMERATED, not that none apply, and court stays shut.
    """
    raw = [str(x).strip() for x in remedy_map if str(x).strip()]
    required = {x for x in raw if not is_court_lever(x)}
    done = {str(x).strip() for x in attempted if str(x).strip()}

    if not required:
        return {
            "ready_for_court": False,
            "missing": [],
            "attempted": [],
            "no_map": True,
            "reason": ("EMPTY remedy map (%d key(s) given, 0 of them non-court) — Tier 0 never "
                       "enumerated this case's levers, so there is nothing to have exhausted. "
                       "An empty remedy map is not 'no avenues apply'; it is 'nobody looked'. "
                       "Court stays shut. Run remedy_map.py to build the map." % len(raw)),
        }

    missing = required - done
    attempted_applicable = required & done

    return {
        "ready_for_court": len(missing) == 0,
        "missing": sorted(missing),
        "attempted": sorted(attempted_applicable),
        "no_map": False,
        "reason": ("all %d applicable lever(s) attempted + logged" % len(attempted_applicable)
                   if not missing else
                   "%d of %d lever(s) still owed: %s"
                   % (len(missing), len(required), ", ".join(sorted(missing)))),
    }


def summarize(remedy_map, attempted, case=None):
    """Short human summary of what is still owed before court."""
    r = remedy_complete(remedy_map, attempted)
    head = "Case %s — " % case if case else ""
    if r.get("no_map"):
        return "%sNOT READY FOR COURT — %s" % (head, r["reason"])
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


def _gate_cli(argv):
    """M44: the Tier-4 gate as a command, for the human path.

    The gate already runs inside case_tick.gate_check() (GATE 4) on every tick, but the
    person who is about to walk into a courthouse cannot read a cron log. This answers the
    question directly, for one case, from the case's REAL board properties.

        python3 remedy_gate.py --check --case CASE-1
        python3 remedy_gate.py --check --map tier1_vendor,bbb --attempted tier1_vendor

    Exit 0 = court may open. Exit 2 = HELD, levers still owed. Exit 3 = the gate could not
    run (board unreachable, no map on the case). Exit 3 is a HOLD too: an unrunnable gate
    must never read as permission.
    """
    import argparse
    import os
    import sys as _sys
    ap = argparse.ArgumentParser(description="M14 Tier-4 (court) remedy gate")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--case", help="case identifier on the board, e.g. CASE-1")
    ap.add_argument("--map", dest="rmap", default=None,
                    help="comma-separated lever keys applicable to this case "
                         "(offline mode; omit to read `MR Remedy Map` off the board)")
    ap.add_argument("--attempted", default=None,
                    help="comma-separated lever keys done AND logged (offline mode)")
    a = ap.parse_args(argv)

    def _split(v):
        return [x.strip() for x in str(v or "").replace("\n", ",").replace(";", ",").split(",")
                if x.strip()]

    if a.rmap is not None:
        rmap, done, label = _split(a.rmap), _split(a.attempted), a.case or "(offline)"
    else:
        if not a.case:
            print("HOLD — give either --case (read the board) or --map/--attempted "
                  "(offline). With neither there is nothing to check, and nothing checked "
                  "is never a clearance.")
            return 3
        try:
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import multica_api as mc
            hit = next((it for it in mc.list_issues()
                        if it.get("identifier") == a.case or it.get("id") == a.case), None)
            if hit is None:
                raise LookupError("no issue with identifier/id %r" % a.case)
            p = hit.get("mr", {}) or {}
            rmap, done = _split(p.get("MR Remedy Map")), _split(p.get("MR Remedy Attempted"))
            label = a.case
        except Exception as exc:
            print("HOLD — could not read %s off the board (%s: %s). A gate that cannot read "
                  "the case must not open court." % (a.case, type(exc).__name__, exc))
            return 3

    if not rmap:
        print("HOLD — %s has an EMPTY MR Remedy Map. An empty map means the levers were "
              "never enumerated, NOT that none apply. Run Tier 0 (remedy_map.py) first."
              % label)
        return 3

    print(summarize(rmap, done, case=label))
    return 0 if remedy_complete(rmap, done)["ready_for_court"] else 2


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        sys.exit(_gate_cli(sys.argv[1:]))

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

    # --- Test 5 (2026-07-28): AN EMPTY MAP FAILS CLOSED, IN THE LIBRARY ---------------
    # The original set arithmetic made an empty map the most permissive input possible:
    # empty required -> empty missing -> ready_for_court True. Nothing in this module's own
    # self-tests exercised it, and only two of its callers remembered to guard for it.
    for empty in ([], (), set(), ["", "  "], None or []):
        r5 = remedy_complete(empty, [])
        assert r5["ready_for_court"] is False, \
            "T5: an EMPTY remedy map must NEVER open court (got %r)" % r5
        assert r5["no_map"] is True, "T5: an empty map must be reported as no_map"
    r5b = remedy_complete([], ["tier1_vendor", "state_ag", "bbb"])
    assert r5b["ready_for_court"] is False, \
        "T5: attempted levers cannot open court when the map itself is empty"
    print("\n=== Test 5: empty map fails CLOSED in the library ===")
    print(summarize([], [], case="MER-EMPTY"))

    # --- Test 6: a map made of NOTHING BUT court levers is also an empty map ----------
    r6 = remedy_complete(["tier4_court", "small_claims"], [])
    assert r6["ready_for_court"] is False and r6["no_map"] is True, \
        "T6: court-only map has no prerequisites enumerated — fail closed"

    # --- Test 7: hand-written court keys are recognised as COURT, not as owed levers --
    # MER-76's live map said `small_claims_dallas`. Unrecognised, it became an ordinary
    # required lever: court owed before court, permanently unreachable.
    assert is_court_lever("small_claims_dallas"), "small_claims_dallas is the court destination"
    assert is_court_lever("tier4_court_tx") and is_court_lever("court")
    assert not is_court_lever("state_ag") and not is_court_lever("bbb")
    assert not is_court_lever(""), "an empty key is not a court lever"
    r7 = remedy_complete(["tier1_vendor", "small_claims_dallas"], ["tier1_vendor"])
    assert r7["ready_for_court"] is True, \
        "T7: court must not be a prerequisite for itself, however the key is spelled"
    assert "small_claims_dallas" not in r7["missing"], "T7: court key never appears as owed"
    print("\n=== Test 7: venue-suffixed court keys cannot gate court on itself ===")
    print(r7)

    # --- Test 8: magnuson_moss is a lever the vocabulary knows (so it is satisfiable) --
    assert "magnuson_moss" in LEVER_LABELS, \
        "T8: a lever appearing in real maps must be known, or it can never be satisfied"

    print("\nPASS — court is structurally unreachable until every applicable remedy lever is attempted + logged, "
          "an EMPTY map fails closed, and court is never a prerequisite for itself.")
