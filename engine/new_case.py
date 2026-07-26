#!/usr/bin/env python3
"""new_case.py — the CASE CREATE path for the merchandise-returns engine (M43).

WHY THIS FILE EXISTS
--------------------
Until now nothing in this engine opened a case. `multica_api.py` had no create call at
all; `new_claim_draft.py` says in its own docstring "THIS MODULE NEVER OPENS A CASE"; every
MER-* issue on the board was typed in by hand. That is why `dup_guard.py` — built, tested
and documented as the pre-open duplicate gate — was wired to nothing: there was no "open"
for it to sit in front of. A guard with no code path is a guarantee on paper only
(BLUEPRINT §10).

This module is that code path, and the guard is IN it, not beside it:

    dup_guard.guard(...)  ->  refuse and return          # BEFORE any POST
    multica_api.create_issue(...)                         # only reached if allowed
    set_properties(MR Phase=Intake, MR Intake Complete=…)  # loud on failure

The order is the point. `dup_guard` runs BEFORE creation and its verdict RETURNS — a
duplicate is refused, never created-then-flagged. Two issues for one return means split
history and two escalation clocks pointed at the same vendor.

FAIL CLOSED. If the duplicate check itself cannot run (no token, API down, bad board), the
creation is REFUSED, not waved through. A gate that fails open is not a gate.

DRY RUN BY DEFAULT. Nothing is written without `--live`. The dry run prints the exact
title, description and properties that would be posted.

TITLE CONVENTION (matches the existing board):
    "Case: <Vendor> / <Item>"                        — the user's own case
    "CLIENT: <Name> - <Item> via <Vendor>"           — operator mode, --client-name
    ... plus " (INTAKE INCOMPLETE)" while any 🔴 BLOCKING intake field is unanswered.
The suffix is not decoration: SKILL.md §1 forbids a case leaving CASE FILE until every
blocking field is answered, and `MR Intake Complete` is the boolean the engine reads.

Run:
    python3 new_case.py --selftest                      # offline, stubbed, no network
    python3 new_case.py --intake-file intake.json       # dry run (prints the plan)
    python3 new_case.py --intake-file intake.json --live
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")

import dup_guard  # noqa: E402
import refund_landed  # noqa: E402  (M44 — normalize_remedy is the single remedy vocabulary)

# refund_landed's internal keys -> the `MR Remedy Type` select OPTION names (onboard.py).
_REMEDY_OPTION = {"refund": "Refund", "replacement": "Replacement",
                  "repair": "Repair", "store_credit": "StoreCredit"}

# --------------------------------------------------------------------------------------
# THE INTAKE STRUCTURE — references/intake-questionnaire.md, by question number.
# 🔴 BLOCKING fields are the ones that must be answered before a case may leave CASE FILE.
# A case may still be OPENED with them missing (that is what "(INTAKE INCOMPLETE)" is for);
# what it may not do is proceed. Anything absent is left absent — never invented. The same
# rule new_claim_draft enforces: a blank field is safe, a fabricated one seeds a case with
# a lie.
# --------------------------------------------------------------------------------------
BLOCKING_FIELDS = [
    ("vendor",              "1.1 Where did you buy it (retailer / marketplace / carrier / maker)"),
    ("purchase_date",       "1.2 Purchase date (approximate OK)"),
    ("amount_paid",         "1.3 Exact amount paid (incl. tax)"),
    ("payment_method",      "1.4 How did you pay (card+issuer / PayPal / financing / cash / gift card)"),
    ("item",                "2.1 Brand + exact model (size / capacity / variant)"),
    ("serial_or_imei",      "2.2 Serial number / IMEI / VIN"),
    ("condition_at_sale",   "2.3 New, open-box, refurbished or used"),
    ("defect_summary",      "3.1 What is wrong, in your own words"),
    ("defective_from_start", "3.2 Defective from the start, or failed later"),
    ("first_noticed",       "3.3 When you first noticed it"),
    ("damage_history",      "3.5 Ever dropped, liquid-exposed, repaired or modified"),
    ("troubleshooting",     "4.1 What troubleshooting you have done"),
    ("prior_contact",       "4.3 Already contacted the seller / manufacturer"),
    ("existing_case_numbers", "4.4 Existing case / ticket / RMA / claim numbers"),
    ("contemporaneous_notice", "4.6 Did you report it to anyone at the time it failed"),
    ("delay_explanation",   "4.7 If time has passed — what happened in between (§1.6)"),
    ("desired_outcome",     "5.1 What you want: refund / replacement / repair / credit (ranked)"),
    ("willing_to_escalate", "5.3 Willing to escalate to regulators and, if needed, small claims"),
    ("evidence_photos",     "6.1 Photos or video of the defect"),
    ("discrimination_check", "7.1 Treated differently because of a protected characteristic (facts only)"),
]

PARALLEL_FIELDS = [
    ("proof_of_purchase",   "1.5 Receipt / order email / account order history"),
    ("in_store_or_online",  "1.6 In-store or online"),
    ("protection_plan",     "2.4 Extended warranty or protection plan, and who issued it"),
    ("intermittent",        "3.4 Constant, intermittent, or specific conditions"),
    ("currently_usable",    "3.6 Currently usable at all"),
    ("ruled_out_external",  "4.2 Ruled out external causes"),
    ("reps_spoken_to",      "4.5 Names of anyone you spoke with + what they promised"),
    ("unacceptable_outcome", "5.2 Any outcome you would NOT accept"),
    ("user_deadline",       "5.4 A deadline or event that matters to you"),
    ("prior_correspondence", "6.2 Screenshots of prior correspondence"),
    ("receipt_file",        "6.3 The receipt / order confirmation"),
]

# Identity / routing, from the user profile (§0) rather than the questionnaire.
CONTEXT_FIELDS = [
    ("user_email",   "Your email (the case's mail scope)"),
    ("user_name",    "Your legal name as on the purchase"),
    ("jurisdiction", "Your state AND county for THIS purchase"),
]

ALL_FIELDS = BLOCKING_FIELDS + PARALLEL_FIELDS + CONTEXT_FIELDS

# The MR properties a newly opened case must carry. A case with no `MR Phase` is invisible
# to case_tick / mer_engine, which select by phase — see multica_api.PropertiesNotSetError.
NEW_CASE_PHASE = "Intake"


def _blank(v):
    if v is None:
        return True
    s = str(v).strip()
    return not s or s.lower() in ("null", "none", "n/a", "na", "unknown", "not stated",
                                  "not provided", "not specified", "-", "--", "?")


def missing_blocking(intake):
    """The 🔴 field keys still unanswered. Computed here, never trusted from a caller."""
    return [k for k, _ in BLOCKING_FIELDS if _blank((intake or {}).get(k))]


def build_title(intake, client_name=None):
    """Board title convention + the (INTAKE INCOMPLETE) suffix while any 🔴 is open."""
    vendor = (intake.get("vendor") or "").strip() or "UNKNOWN VENDOR"
    item = (intake.get("item") or "").strip() or "UNKNOWN ITEM"
    if client_name:
        title = "CLIENT: %s - %s via %s" % (client_name.strip(), item, vendor)
    else:
        title = "Case: %s / %s" % (vendor, item)
    if missing_blocking(intake):
        title += " (INTAKE INCOMPLETE)"
    return title


def build_description(intake, client_name=None):
    """The case body: every intake answer that exists, and an explicit list of the ones
    that do not. The missing list is part of the record on purpose — an unanswered
    blocking field must be visible on the board, not implied by its absence."""
    lines = []
    vendor = (intake.get("vendor") or "").strip() or "UNKNOWN"
    item = (intake.get("item") or "").strip() or "UNKNOWN"
    lines.append("VENDOR/ITEM: %s - %s" % (vendor, item))
    if client_name:
        lines.append("CLIENT CASE. Client: %s" % client_name.strip())
    for k, label in CONTEXT_FIELDS:
        if not _blank(intake.get(k)):
            lines.append("%s: %s" % (label.split(" (")[0], intake[k]))
    lines.append("")

    def section(name, fields):
        rows = [(lab, intake.get(k)) for k, lab in fields if not _blank(intake.get(k))]
        if not rows:
            return
        lines.append("== %s ==" % name)
        for lab, val in rows:
            lines.append("  %s: %s" % (lab, val))
        lines.append("")

    section("INTAKE — blocking (SKILL.md §1)", BLOCKING_FIELDS)
    section("INTAKE — parallel", PARALLEL_FIELDS)

    miss = missing_blocking(intake)
    lines.append("== STILL MISSING (BLOCKING — case cannot leave CASE FILE) ==")
    if miss:
        by_key = dict(BLOCKING_FIELDS)
        for k in miss:
            lines.append("  [ ] %s" % by_key[k])
    else:
        lines.append("  (none — every blocking field is answered)")
    lines.append("")
    lines.append("Opened by new_case.py. MR Phase=%s. Duplicate check ran BEFORE creation "
                 "(dup_guard)." % NEW_CASE_PHASE)
    return "\n".join(lines)


# =========================================================================================
# THE CREATE PATH — the guard is the first thing in it.
# =========================================================================================
def open_case(intake, *, api=None, issues=None, dry_run=True, project_id=None,
              client_name=None, ws=None, dup_threshold=dup_guard.DUP_THRESHOLD):
    """Open a merchandise-return case, refusing outright if a live case already covers it.

    Returns a verdict dict:
        {"created": bool, "refused": bool, "reason": str, "title": str,
         "description": str, "properties": {...}, "duplicates": [...], "issue": {...}|None,
         "missing_blocking": [...], "dry_run": bool}

    ORDER OF OPERATIONS (this is the enforcement, not the docstring):
      1. dup_guard.guard(vendor, item, user_email)   <- BEFORE anything is written
      2. refused? RETURN. No POST happens. The caller merges into duplicates[0].
      3. only then build + create + set MR properties.

    `api` defaults to multica_api and is injectable so the self-test can prove step 1
    blocks step 3 without touching a live board. `issues` is an optional pre-fetched board
    (the self-test's fixture); when omitted dup_guard reads the live board itself.
    """
    intake = dict(intake or {})
    verdict = {
        "created": False, "refused": False, "reason": "", "title": None,
        "description": None, "properties": None, "duplicates": [], "issue": None,
        "missing_blocking": missing_blocking(intake), "dry_run": bool(dry_run),
    }

    # ---- STEP 1: the duplicate gate, before any write ----------------------------------
    if _blank(intake.get("item")) and _blank(intake.get("user_email")):
        verdict["refused"] = True
        verdict["reason"] = ("REFUSED: cannot run the duplicate check with neither an item "
                             "nor a user email. A gate with nothing to match on passes "
                             "everything, which is worse than no gate.")
        return verdict
    try:
        g = dup_guard.guard(intake.get("vendor"), intake.get("item"),
                            intake.get("user_email"), issues=issues,
                            threshold=dup_threshold)
    except Exception as exc:
        # FAIL CLOSED. A duplicate check that cannot run must not let a case be opened.
        verdict["refused"] = True
        verdict["reason"] = ("REFUSED: the duplicate check could not run (%s: %s). Verify "
                             "by hand before opening — a gate that fails open is not a gate."
                             % (type(exc).__name__, exc))
        return verdict
    if not g["allowed"]:
        verdict["refused"] = True
        verdict["duplicates"] = g["duplicates"]
        verdict["reason"] = ("REFUSED — %s. Merge into / flag that case instead of opening "
                             "a second one: two issues for one return means split history "
                             "and two escalation clocks on one vendor." % g["reason"])
        return verdict

    # ---- STEP 2: build what would be written -------------------------------------------
    title = build_title(intake, client_name)
    description = build_description(intake, client_name)
    props = {
        "MR Phase": NEW_CASE_PHASE,
        "MR Intake Complete": not verdict["missing_blocking"],
    }
    if not _blank(intake.get("jurisdiction")):
        props["MR Jurisdiction"] = intake["jurisdiction"]
    # M44 — seed `MR Remedy Type` ONLY when intake 5.1 names exactly one remedy family.
    # 5.1 asks for a RANKED list ("refund / replacement / repair / credit"), which names
    # four; normalize_remedy returns None for that and the property is left blank. It is
    # then set to whatever the vendor actually GRANTS. A blank value is safe — close_case
    # holds — while a guessed one decides how the case closes.
    rt = refund_landed.normalize_remedy(intake.get("desired_outcome"))
    if rt:
        props["MR Remedy Type"] = _REMEDY_OPTION[rt]
    verdict.update(title=title, description=description, properties=props)

    if dry_run:
        verdict["reason"] = ("DRY RUN — clear to open (%s). Nothing was written. Re-run with "
                             "--live to create." % g["reason"])
        return verdict

    # ---- STEP 3: create, then set the MR properties (loud if step 3b fails) ------------
    if api is None:
        import multica_api as api  # noqa: F811  (deferred so --selftest needs no token)
    issue = api.create_issue(title, description=description, project_id=project_id,
                             properties=props, ws=ws)
    verdict["created"] = True
    verdict["issue"] = issue
    verdict["reason"] = "CREATED %s — %s" % (issue.get("identifier") or issue.get("id"),
                                             g["reason"])
    return verdict


def _print_verdict(v):
    print(v["reason"])
    for d in v["duplicates"]:
        print("   %-8s %.2f  %s" % (d["identifier"], d["score"], d["title"]))
    if v["title"]:
        print("\n--- TITLE ---\n%s" % v["title"])
        print("\n--- PROPERTIES ---")
        for k, val in sorted(v["properties"].items()):
            print("  %-22s %s" % (k, val))
        print("\n--- DESCRIPTION ---\n%s" % v["description"])
    if v["missing_blocking"]:
        print("\n%d blocking intake field(s) still open — the case opens as "
              "(INTAKE INCOMPLETE) and MR Intake Complete=false." % len(v["missing_blocking"]))


# =========================================================================================
# SELF-TEST — offline, fabricated data, stubbed API. No network, no live board, no writes.
# =========================================================================================
# A wholly INVENTED board. No real person, email, ticket, account or case id appears in
# this file: the skill is packaged and handed to strangers, and a self-test fixture ships
# with it onto every recipient's machine.
_FIXTURE_BOARD = [
    {"identifier": "CASE-21", "status": "todo",
     "title": "Case: Vantage Mobile / Northstar Fold X3 (INTAKE INCOMPLETE)",
     "description": "VENDOR/ITEM: Vantage Mobile - Northstar Fold X3. "
                    "Contact robin.vale@example.com. Phase 1 intake."},
    {"identifier": "CASE-4", "status": "todo",
     "title": "Case: Vantage Mobile / Northstar Slate 11 tablet (INTAKE INCOMPLETE)",
     "description": "VENDOR/ITEM: Vantage Mobile - Northstar Slate 11 tablet, defective. "
                    "Contact alex.marsh@example.com."},
    {"identifier": "CASE-2", "status": "in_review",
     "title": "Case: Ridgeway Athletic / Ridgeway Trail 8 shoes - exchange shipped",
     "description": "VENDOR/ITEM: Ridgeway Athletic - Ridgeway Trail 8 TR Mesh, Men's 12."},
    {"identifier": "CASE-51", "status": "cancelled",
     "title": "Cancelled test case - Northstar Fold X3 via Vantage Mobile",
     "description": "Cancelled. Northstar Fold X3, Vantage Mobile."},
]


class _StubAPI(object):
    """Records what WOULD have gone over the wire. Never opens a socket."""

    def __init__(self, fail_properties=False):
        self.calls = []
        self.fail_properties = fail_properties
        self._n = 0

    def create_issue(self, title, description=None, project_id=None, status=None,
                     priority=None, due_date=None, properties=None, ws=None, defs=None):
        self._n += 1
        issue = {"id": "stub-%d" % self._n, "identifier": "STUB-%d" % self._n,
                 "title": title, "description": description}
        self.calls.append(("create_issue", title, properties))
        if properties and self.fail_properties:
            import multica_api
            raise multica_api.PropertiesNotSetError(
                "stubbed property write failure", issue=issue, cause=RuntimeError("boom"))
        return issue


def _selftest():
    ok = True
    results = []

    def check(name, cond, detail=""):
        nonlocal ok
        if not cond:
            ok = False
        results.append(("PASS" if cond else "FAIL", name, detail))

    print("=== new_case self-test (fixture board, stubbed API, no network) ===\n")

    clean = {
        "vendor": "Best Buy", "item": "Dell XPS 13 laptop", "user_email": "someone@example.com",
        "purchase_date": "2026-03-02", "amount_paid": "$1,249.99",
        "payment_method": "Visa ending 4412 (Chase)", "serial_or_imei": "5CD1234ABC",
        "condition_at_sale": "new", "defect_summary": "screen flickers and goes black",
        "defective_from_start": "failed later, about 6 weeks in", "first_noticed": "2026-04-15",
        "damage_history": "never dropped, no liquid, never opened",
        "troubleshooting": "reinstalled display driver, external monitor works",
        "prior_contact": "yes, Geek Squad 2026-04-20", "existing_case_numbers": "BBY-99120",
        "contemporaneous_notice": "told the store the week it started",
        "delay_explanation": "was travelling for work through May",
        "desired_outcome": "refund, replacement second", "willing_to_escalate": "yes",
        "evidence_photos": "3 phone videos of the flicker",
        "discrimination_check": "no",
        "jurisdiction": "Texas / Travis County", "user_name": "Jordan A. Rivera",
    }

    # 1) A DUPLICATE IS REFUSED **BEFORE** CREATION — the whole point of this module.
    dup_intake = dict(clean, vendor="Vantage Mobile", item="Northstar Fold X3",
                      user_email="robin.vale@example.com")
    stub = _StubAPI()
    v = open_case(dup_intake, api=stub, issues=_FIXTURE_BOARD, dry_run=False)
    print("Test 1 (duplicate):\n   %s" % v["reason"])
    check("duplicate is REFUSED", v["refused"] is True, v["reason"][:80])
    check("duplicate was NOT created", v["created"] is False)
    check("NO create call reached the API", stub.calls == [], "calls=%r" % (stub.calls,))
    check("the case to merge into is named", bool(v["duplicates"]) and
          v["duplicates"][0]["identifier"] == "CASE-21",
          str([d["identifier"] for d in v["duplicates"]]))
    check("the OTHER item on the same vendor is not a duplicate",
          "CASE-4" not in [d["identifier"] for d in v["duplicates"]])
    check("a cancelled case is never offered to merge into",
          "CASE-51" not in [d["identifier"] for d in v["duplicates"]])

    # 2) A CLEAN, COMPLETE CASE CREATES, with MR Phase=Intake.
    stub2 = _StubAPI()
    v2 = open_case(clean, api=stub2, issues=_FIXTURE_BOARD, dry_run=False)
    print("\nTest 2 (clean case):\n   %s\n   title: %s" % (v2["reason"], v2["title"]))
    check("clean case is CREATED", v2["created"] is True, v2["reason"][:80])
    check("exactly one create call", len(stub2.calls) == 1, "calls=%d" % len(stub2.calls))
    check("MR Phase=Intake written at creation",
          stub2.calls[0][2].get("MR Phase") == "Intake", str(stub2.calls[0][2]))
    check("MR Intake Complete=True (all blocking answered)",
          stub2.calls[0][2].get("MR Intake Complete") is True, str(stub2.calls[0][2]))
    check("no (INTAKE INCOMPLETE) suffix on a complete intake",
          "(INTAKE INCOMPLETE)" not in v2["title"], v2["title"])

    # 3) An INCOMPLETE intake still opens, but marked — title suffix + boolean false.
    thin = {"vendor": "Acme Tools", "item": "Acme 18V drill",
            "user_email": "someone@example.com", "defect_summary": "chuck seized"}
    stub3 = _StubAPI()
    v3 = open_case(thin, api=stub3, issues=_FIXTURE_BOARD, dry_run=False)
    print("\nTest 3 (incomplete intake):\n   title: %s" % v3["title"])
    check("incomplete intake still creates", v3["created"] is True)
    check("(INTAKE INCOMPLETE) in title", "(INTAKE INCOMPLETE)" in v3["title"], v3["title"])
    check("MR Intake Complete=False", stub3.calls[0][2].get("MR Intake Complete") is False)
    check("missing blocking fields are listed", len(v3["missing_blocking"]) >= 15,
          "%d missing" % len(v3["missing_blocking"]))
    check("the missing list is in the description",
          "STILL MISSING (BLOCKING" in v3["description"])

    # 4) DRY RUN is the default and writes nothing.
    stub4 = _StubAPI()
    v4 = open_case(clean, api=stub4, issues=_FIXTURE_BOARD)
    print("\nTest 4 (dry run default):\n   %s" % v4["reason"])
    check("dry run does not create", v4["created"] is False and stub4.calls == [])
    check("dry run still shows the exact title", v4["title"] == v2["title"])

    # 5) FAIL CLOSED — if the duplicate check itself explodes, nothing is created.
    class _Boom(list):
        def __iter__(self):
            raise RuntimeError("board unreachable")

    stub5 = _StubAPI()
    v5 = open_case(clean, api=stub5, issues=_Boom(), dry_run=False)
    print("\nTest 5 (dup check fails):\n   %s" % v5["reason"])
    check("an unusable duplicate check REFUSES (fails closed)", v5["refused"] is True)
    check("nothing created when the gate cannot run", stub5.calls == [])

    # 6) A half-created case (properties failed) surfaces LOUDLY, never as success.
    stub6 = _StubAPI(fail_properties=True)
    try:
        open_case(clean, api=stub6, issues=_FIXTURE_BOARD, dry_run=False)
        raised = False
        detail = "no exception"
    except Exception as exc:
        raised = type(exc).__name__ == "PropertiesNotSetError"
        detail = "%s: %s" % (type(exc).__name__, str(exc)[:70])
    print("\nTest 6 (property write fails):\n   %s" % detail)
    check("a failed property write raises PropertiesNotSetError", raised, detail)

    # 7) A gate with nothing to match on refuses rather than passing everything.
    stub7 = _StubAPI()
    v7 = open_case({"vendor": "Acme"}, api=stub7, issues=_FIXTURE_BOARD, dry_run=False)
    print("\nTest 7 (no item, no email):\n   %s" % v7["reason"])
    check("refuses when there is nothing to dedupe on", v7["refused"] is True)
    check("nothing created", stub7.calls == [])

    print("")
    for status, name, detail in results:
        print("[%s] %s%s" % (status, name, ("  -> %s" % detail) if detail and status == "FAIL" else ""))
    print("\n%s" % ("ALL PASS" if ok else "SOME FAILED"))
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Open a merchandise-return case (dup_guard runs BEFORE creation).")
    ap.add_argument("--selftest", action="store_true",
                    help="offline self-test: stubbed API, fixture board, no network")
    ap.add_argument("--intake-file", help="JSON file of intake answers (keys: %s ...)"
                                          % ", ".join(k for k, _ in BLOCKING_FIELDS[:4]))
    ap.add_argument("--vendor", help="1.1 vendor (overrides the intake file)")
    ap.add_argument("--item", help="2.1 brand + exact model (overrides the intake file)")
    ap.add_argument("--email", help="the user's email (case mail scope)")
    ap.add_argument("--client-name", help="operator mode: use the CLIENT: title convention")
    ap.add_argument("--project", help="Multica project id to file the case under")
    ap.add_argument("--live", action="store_true",
                    help="actually create the case. Without this nothing is written.")
    ap.add_argument("--fields", action="store_true", help="print the intake field keys and exit")
    a = ap.parse_args(argv)

    if a.selftest:
        return 0 if _selftest() else 1
    if a.fields:
        print("BLOCKING (case cannot leave CASE FILE until all are answered):")
        for k, lab in BLOCKING_FIELDS:
            print("  %-24s %s" % (k, lab))
        print("\nPARALLEL:")
        for k, lab in PARALLEL_FIELDS:
            print("  %-24s %s" % (k, lab))
        print("\nCONTEXT (from the user profile):")
        for k, lab in CONTEXT_FIELDS:
            print("  %-24s %s" % (k, lab))
        return 0

    intake = {}
    if a.intake_file:
        with open(a.intake_file, encoding="utf-8") as fh:
            intake = json.load(fh)
    for flag, key in (("vendor", "vendor"), ("item", "item"), ("email", "user_email")):
        val = getattr(a, flag)
        if val:
            intake[key] = val
    if not intake:
        print("Nothing to do: supply --intake-file (or at least --item/--email). "
              "`--fields` lists every key.")
        return 3

    try:
        v = open_case(intake, dry_run=not a.live, project_id=a.project,
                      client_name=a.client_name)
    except Exception as exc:
        print("FAILED — %s: %s" % (type(exc).__name__, exc))
        return 3
    _print_verdict(v)
    if v["refused"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
