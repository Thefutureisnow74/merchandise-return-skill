#!/usr/bin/env python3
"""recall_research.py — the recall check, made unskippable (M54).

WHY THIS FILE EXISTS
--------------------
M53 added `recall_check` as a blocking lever in the remedy map. That was the right vocabulary and
the wrong amount of enforcement: a lever in a map is satisfied by somebody *choosing* to run it,
and this project's entire lesson register is a list of things that were supposed to happen because
a document said so.

The first time the check was actually run, on 2026-07-29, it found a **CPSC recall with an exact
model match on a $3,000 chair, whose mandated remedy included a full refund** — on a case that had
already been through Tier 1 and Tier 2 without anyone looking. It also found that a recall remedy
keys to **model and serial, not proof of purchase**, which is the precise wall two other live cases
were stuck against.

So the most valuable single lever in the ladder had been sitting unused, on the strongest case on
the board, for days. That is not a lever. That is a suggestion.

WHAT THIS MODULE DOES
  * `brief(case)`     -> the exact research brief for one case: the product identifiers, the
                        authoritative sources to search in order, and the anti-overstatement rules.
                        This is what gets handed to a research agent, so the quality of the check
                        does not depend on who is running it that day.
  * `needs_recall_check(case)` -> is this a product case that has not yet been checked?
  * `sweep(issues)`   -> every case still owing a check. Non-zero exit, so it can sit on the clock
                        and FAIL rather than log.

The check is satisfied by HAVING LOOKED. "No recall found" is a real, loggable result — three of
the four cases checked on 2026-07-29 came back negative and the negatives were valuable, because
each one closed off a false lead someone would otherwise have reached for later.

WHY A SERVICE CASE IS EXEMPT
A subscription, a membership, a bank fee or a service dispute has no manufactured article, so there
is nothing for CPSC or NHTSA to have recalled. Exempting those is not a loophole — asking the
question would produce a guaranteed "no recall found" on every one of them and train everybody to
ignore the sweep. A gate that fires on cases it cannot possibly apply to is a gate people mute.

CLI
    recall_research.py --selftest
    recall_research.py --sweep            every case still owing a check (exit 1 if any)
    recall_research.py --brief MER-76     print the research brief for one case
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")

LEVER = "recall_check"

#: Authoritative first, always. Every one of these is a regulator or the maker itself; a blog, a
#: forum or an AI-written FAQ is never a source for a claim that goes in a letter. On 2026-07-29 a
#: widely-repeated "Hisense recalled 519,000 dehumidifiers" claim traced back to an AI-generated
#: FAQ page and was contradicted by Hisense's own recall page. It would have been demolished in one
#: search by a vendor's counsel, taking every true statement in the letter with it.
SOURCES = [
    ("CPSC", "https://www.cpsc.gov/Recalls and https://www.saferproducts.gov/ — consumer products. "
             "WARNING: CPSC manufacturer pages are keyed on BRAND NAME and two unrelated companies "
             "can share one. cpsc.gov/manufacturer/graco returns only Graco Children's Products "
             "(Newell, car seats) and nothing from Graco Inc. (Minneapolis, paint sprayers)."),
    ("NHTSA", "https://www.nhtsa.gov/recalls — vehicles, tyres, child seats."),
    ("FDA", "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts — food, drugs, "
            "devices, cosmetics, supplements."),
    ("USDA FSIS", "https://www.fsis.usda.gov/recalls — meat, poultry, egg products."),
    ("the maker itself", "the manufacturer's own recall / safety / service-bulletin pages. Often "
                         "more current than CPSC and sometimes the only place a service bulletin "
                         "appears. Also the best refutation of a false third-party claim."),
]

#: A service case has no manufactured article. See the module docstring.
_SERVICE_HINTS = (
    "subscription", "membership", "monthly fee", "recurring charge", "auto-renew",
    "credit monitoring", "credit report", "insurance policy", "airfare", "flight",
    "hotel", "booking", "wireless plan", "cell plan", "broadband", "streaming",
    "bank fee", "overdraft", "financing agreement", "saas",
)
#: Dropped deliberately, each having caused or risked a false "service" classification:
#:   "policy"   — appears in any case that mentions a return policy. Flipped the Graco sprayer.
#:   "licence"/"license" — appears in warranty text and in "licensed dealer".
#:   "loan", "software" — too generic to carry on their own.
#: Retained only as multi-word phrases where the pairing is what makes them diagnostic.


#: Placeholders that LOOK like a value and are not one. A record saying
#: "2.2 Serial number: N/A - subscription service" was read as a serial and flipped a subscription
#: case into a product case. An intake field that says "there is nothing here" must read as empty.
_NULLISH = re.compile(
    r"^\s*(?:"
    r"n/?a\b"
    r"|none\b"
    r"|not\s+(?:provided|stated|recorded|applicable|yet|available|known)"
    r"|unknown\b|tbd\b|no\s+serial\b|lost\b"
    r"|-+\s*$"
    r"|\(.*\)\s*$"
    r")", re.I)


def _val(case, key):
    """A field's value, with placeholder text treated as absent."""
    v = str(case.get(key) or "").strip()
    return "" if (not v or _NULLISH.match(v)) else v


def _text(case):
    """Service detection reads the TITLE, ITEM and VENDOR only — never the whole description.

    Scanning the description matched 'policy' inside the Graco sprayer's case notes and classified
    a paint sprayer as a service, exempting the very case that most needed checking. A full case
    record incidentally contains 'policy', 'licence', 'financing' and 'insurance' as ordinary
    English; only the name of the thing is evidence of what the thing is."""
    return " ".join(str(case.get(k) or "") for k in ("title", "item", "vendor")).lower()


#: An explicit board escape hatch. A service case that really does involve hardware — a leased
#: router, a handset on a wireless plan — sets this in its description. It is deliberately an
#: explicit marker rather than an inference: see is_product_case().
_FORCE_PRODUCT_MARKER = "RECALL CHECK APPLIES"


def is_product_case(case):
    """A case with a physical article to recall. Services are exempt, and say why.

    NOTE — an earlier version tried to be clever here: if a service case had a model or serial
    recorded, it was promoted back to a product case on the theory that the identifier proved
    hardware. That inference was wrong twice over. The intake template *always* fills
    "2.1 Brand + exact model", so a subscription case had "Experian paid membership (CreditWorks
    tier)" sitting in the model field and was classified as a product — and the fields it read were
    full of placeholder text like "N/A - subscription service", which read as values.

    A guess that can promote a case in the wrong direction is worse than no guess. The name of the
    thing decides, and anything genuinely mixed declares itself with an explicit marker."""
    if _FORCE_PRODUCT_MARKER in str(case.get("description") or "").upper():
        return True
    return not any(h in _text(case) for h in _SERVICE_HINTS)


def already_checked(case):
    attempted = str(case.get("attempted") or "")
    return LEVER in attempted


def needs_recall_check(case):
    return is_product_case(case) and not already_checked(case)


def brief(case):
    """The research brief for one case. Deterministic, so the check does not vary by operator."""
    ident = case.get("identifier") or "(case)"
    label = case.get("label") or case.get("title") or "(unnamed)"
    item = case.get("item") or "(item not recorded)"
    model = case.get("model") or "(no model recorded)"
    serial = case.get("serial") or "(no serial recorded)"
    vendor = case.get("vendor") or "(vendor not recorded)"
    symptom = case.get("defect_summary") or "(symptom not recorded)"

    L = []
    L.append("=" * 78)
    L.append("RECALL RESEARCH BRIEF — %s  (%s)" % (label, ident))
    L.append("=" * 78)
    L.append("")
    L.append("PRODUCT")
    L.append("  item    : %s" % item)
    L.append("  model   : %s" % model)
    L.append("  serial  : %s" % serial)
    L.append("  sold by : %s" % vendor)
    L.append("  symptom : %s" % symptom)
    L.append("")
    L.append("SEARCH THESE, IN THIS ORDER. Authoritative sources only.")
    for name, note in SOURCES:
        L.append("  * %-16s %s" % (name, note))
    L.append("")
    L.append("FOR ANY RECALL FOUND, REPORT")
    L.append("  - recall number and date")
    L.append("  - the exact models AND serial ranges covered")
    L.append("  - the hazard, and the remedy the maker was obligated to offer")
    L.append("  - whether THIS unit's model and serial fall inside the covered range")
    L.append("  - the source URL for every claim")
    L.append("")
    L.append("THE RULES THAT MATTER MORE THAN FINDING SOMETHING")
    L.append("  1. \"No recall found\" is a REAL AND USEFUL RESULT. Say it plainly. The lever is")
    L.append("     satisfied by having looked, not by finding a hit.")
    L.append("  2. NEVER stretch a different model, a different brand, or a loosely similar")
    L.append("     product into a match. Distinguish, in words: \"this exact model is recalled\" /")
    L.append("     \"a related model is recalled\" / \"no recall found\".")
    L.append("  3. Watch for the two traps that have already been hit:")
    L.append("     - SHARED BRAND NAMES. Two unrelated companies can share one name, and the")
    L.append("       regulator's own page will happily show you the wrong company's recalls.")
    L.append("     - AI-GENERATED RECALL CLAIMS. A confident, widely-repeated recall that traces")
    L.append("       back to an FAQ-farm or a blog, and that the maker's own recall page")
    L.append("       contradicts. Check the maker's page before believing any third party.")
    L.append("  4. A NEAR MISS IS A LANDMINE, not a bonus. Where a recall matches the SYMPTOM but")
    L.append("     not the model, say so loudly and record it as unusable — that is exactly the")
    L.append("     one somebody cites later without re-reading it.")
    L.append("  5. If the hazard found is NOT the symptom on this case, say so. A recall about a")
    L.append("     reclining mechanism does not support a claim about wiring.")
    L.append("")
    L.append("IF A HAZARD EXISTS AND IS UNREPORTED")
    L.append("  A symptom with no matching recall may be an unreported hazard. Filing at")
    L.append("  saferproducts.gov is truthful, creates a federal record, and carries more weight")
    L.append("  with a vendor than an overstated recall claim ever would.")
    L.append("")
    L.append("WHY A RECALL IS WORTH THIS MUCH CARE")
    L.append("  It moves the case from \"will this vendor help me\" to \"this product was declared")
    L.append("  defective by its own maker or by a federal regulator\" — which no retention script,")
    L.append("  expired warranty or lost receipt can answer. Recall remedies are keyed to MODEL AND")
    L.append("  SERIAL rather than proof of purchase, which is the exact wall a lost-receipt case")
    L.append("  hits. And an OVERSTATED recall destroys a legitimate claim, so the care is not")
    L.append("  bureaucracy — it is what makes the lever usable at all.")
    return "\n".join(L)


def _cases_from_board(issues=None):
    import case_queries
    if issues is None:
        import multica_api as mc
        issues = mc.list_issues()
    out = []
    for it in case_queries.eligible_issues(issues):
        mr = it.get("mr") or {}
        desc = it.get("description") or ""
        out.append({
            "identifier": it.get("identifier"),
            "title": it.get("title") or "",
            "label": case_queries.case_label(it),
            "description": desc,
            "vendor": case_queries.parse_vendor(desc),
            "item": _from_block(desc, "2.1 Brand + exact model"),
            "model": _from_block(desc, "2.1 Brand + exact model"),
            "serial": _from_block(desc, "2.2 Serial number"),
            "defect_summary": _from_block(desc, "3.1 What is wrong"),
            "attempted": mr.get("MR Remedy Attempted") or "",
        })
    return out


def _from_block(desc, label):
    m = re.search(r"^\s*%s[^:]*:\s*(.+)$" % re.escape(label), desc or "", re.M)
    return m.group(1).strip() if m else ""


def sweep(issues=None, out=None):
    """-> count of cases still owing a recall check. Prints one line each."""
    p = out or (lambda s: sys.stdout.write(s + "\n"))
    cases = _cases_from_board(issues)
    owing = [c for c in cases if needs_recall_check(c)]
    exempt = [c for c in cases if not is_product_case(c)]
    p("=" * 78)
    p("RECALL-CHECK SWEEP — %d product case(s) owing a check, %d checked, %d service-exempt"
      % (len(owing), len([c for c in cases if is_product_case(c) and already_checked(c)]),
         len(exempt)))
    p("=" * 78)
    for c in owing:
        p("  OWED   %-46s (%s)" % ((c.get("label") or "")[:46], c.get("identifier")))
        p("         run: recall_research.py --brief %s" % c.get("identifier"))
    for c in exempt:
        p("  exempt %-46s (%s)  no manufactured article"
          % ((c.get("label") or "")[:46], c.get("identifier")))
    if not owing:
        p("")
        p("  every product case has had its recall check run.")
    return len(owing)


def _selftest():
    ok = [True]

    def check(name, cond, detail=""):
        ok[0] = ok[0] and bool(cond)
        print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                             ("  -> %s" % detail) if detail and not cond else ""))

    prod = {"identifier": "MER-9", "title": "Case: Acme / widget", "item": "Acme widget",
            "attempted": ""}
    check("a product case owes a check", needs_recall_check(prod) is True)

    done = dict(prod, attempted="tier1_vendor, recall_check")
    check("a checked case no longer owes one", needs_recall_check(done) is False)
    check("...and partial-name matching does not false-positive",
          needs_recall_check(dict(prod, attempted="tier1_vendor")) is True)

    svc = {"identifier": "MER-8", "title": "Case: Experian / membership subscription",
           "item": "paid membership", "attempted": ""}
    check("a subscription case is service-exempt", is_product_case(svc) is False)
    check("...so it does not appear as owing", needs_recall_check(svc) is False)

    check("a service case with a placeholder model stays a service",
          is_product_case(dict(svc, model="Experian paid membership (CreditWorks tier)",
                               serial="N/A - subscription service")) is False,
          "the intake template always fills the model field; that is not evidence of hardware")
    check("only an EXPLICIT marker promotes a service case to a product case",
          is_product_case(dict(svc, description="... RECALL CHECK APPLIES: leased router ...")) is True)
    check("placeholder text reads as absent",
          _val({"serial": "N/A - subscription service"}, "serial") == ""
          and _val({"serial": "not provided"}, "serial") == ""
          and _val({"serial": "X99Z00Y111222333"}, "serial") == "X99Z00Y111222333")
    check("the nullish pattern carries no corrupted escapes",
          chr(8) not in _NULLISH.pattern,
          "a non-raw patch turned \b into a literal backspace and the regex matched nothing")
    check("a sprayer is NOT misread as a service by the word 'policy'",
          is_product_case({"title": "Case: PPG Paints / Graco sprayer",
                           "item": "Graco Ultra Cordless Handheld Airless sprayer",
                           "description": "PPG return policy required a receipt"}) is True)

    b = brief({"identifier": "MER-9", "label": "Acme / widget", "item": "Acme widget",
               "model": "W-1", "serial": "S-1", "vendor": "Acme",
               "defect_summary": "stopped working"})
    for must in ("CPSC", "NHTSA", "saferproducts.gov", "No recall found",
                 "NEVER stretch", "SHARED BRAND NAMES", "AI-GENERATED",
                 "NEAR MISS IS A LANDMINE", "MODEL AND"):
        check("brief carries the %r rule" % must[:26], must in b)
    check("brief names the actual product", "Acme widget" in b and "S-1" in b)

    check("_from_block pulls a value",
          _from_block("  2.2 Serial number / IMEI / VIN: X99Z00Y111222333\n",
                      "2.2 Serial number") == "X99Z00Y111222333")
    check("_from_block on a missing label returns empty",
          _from_block("nothing here", "2.2 Serial number") == "")

    lines = []
    n = sweep(issues=[], out=lines.append)
    check("an empty board owes nothing", n == 0)

    print("\n%s" % ("ALL PASS" if ok[0] else "SOME FAILED"))
    return ok[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description="the recall check, made unskippable")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--brief", metavar="IDENTIFIER")
    a = ap.parse_args(argv)
    if a.selftest:
        return 0 if _selftest() else 1
    if a.brief:
        for c in _cases_from_board():
            if c.get("identifier") == a.brief:
                print(brief(c))
                return 0
        print("no such case on the board: %s" % a.brief)
        return 1
    if a.sweep:
        return 1 if sweep() else 0
    return 1 if sweep() else 0


if __name__ == "__main__":
    sys.exit(main())
