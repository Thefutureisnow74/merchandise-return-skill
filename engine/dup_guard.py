#!/usr/bin/env python3
"""
dup_guard.py — M30 duplicate-case guard for the AMBS Merchandise Returns engine.

The problem this closes: two paths can open a case for one return.
  (1) the new-claim auto-draft skeleton (M16, mer_engine) fires when inbound mail looks
      like a fresh claim, and
  (2) a human (or a client re-ping) manually opens a case.
Nothing today asks "is there already a live case for this same item + vendor + user?"
before opening. When both paths fire on the same return you get two MER-* issues, split
history, and — worse — two parallel escalation clocks against the same vendor.

This module is the pre-open check. Before anything opens a new case it calls
find_duplicates(vendor, item, user_email); if that comes back non-empty (or the boolean
is_probable_dup(...) is True) the caller should MERGE into / FLAG the existing case
instead of opening a second one.

Hard boundary — READ ONLY. This file never opens, edits, comments on, or closes an
issue. It reads the live board (multica_api.list_issues) and returns candidates. The
decision to merge or flag belongs to the caller, so the guard can never itself become the
thing that double-writes.

How the match works (fuzzy, normalized, no external deps):
  - Everything is lower-cased and split on non-alphanumerics into token sets, with a small
    stop-list of return-domain noise ("case", "claim", "client", "via", "exchange", ...)
    removed. So "T-Mobile" -> {t, mobile}, "Samsung Galaxy Z Fold 3" -> {samsung, galaxy,
    z, fold, 3}. Model numbers and digits are KEPT — "Fold 3" vs "Fold 4" must differ.
  - A case issue's searchable text is title + description (the working files put the
    "VENDOR/ITEM:" line and the client's email in there).
  - vendor score = fraction of the query's vendor tokens found in the issue text.
  - item score   = fraction of the query's item tokens found in the issue text (token
    overlap). This is the backbone signal: a duplicate is fundamentally the same item.
  - user score   = 1.0 if the user's email appears verbatim in the issue text (the
    strongest single signal); else name-token overlap.
  - Only the components the caller actually supplied are weighted; weights renormalize over
    what's present, so calling with item alone still works.
  - A candidate must clear a gate (real item overlap OR an exact email hit) AND a combined
    score threshold. The gate stops a vendor-only coincidence (two different Samsung items)
    from reading as a duplicate; the threshold ranks how confident we are.

Only LIVE cases count. Anything already done/cancelled cannot be duplicated into.

stdlib only.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")

# --- tunables -------------------------------------------------------------------------
# Statuses that mean the case is no longer live. You cannot create a "duplicate" of a
# closed case, so these are never candidates.
CLOSED_STATUSES = frozenset({"done", "cancelled", "canceled", "closed", "archived"})

# Weights for the three signals when all are supplied. Renormalized over whichever the
# caller actually passed (empty vendor / empty user_email drop out).
W_VENDOR = 0.35
W_ITEM = 0.45
W_USER = 0.20

# A candidate must share at least this fraction of the query's item tokens (unless the
# user's email matches verbatim, which is decisive on its own).
MIN_ITEM_OVERLAP = 0.34
# Combined-score bar for calling something a probable duplicate.
DUP_THRESHOLD = 0.50

# Return-domain noise + generic English glue. Removed before matching so they can't
# manufacture overlap. Product nouns (laptop, tablet, sprayer, ...) are deliberately NOT
# here — they are real disambiguators.
STOP = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "to", "via", "with", "in", "on", "at",
    "by", "from", "case", "cases", "claim", "claims", "client", "clients", "exchange",
    "return", "returns", "refund", "order", "incomplete", "intake", "new", "re", "mer",
    "inc", "llc", "co", "corp", "vendor", "item",
})

_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")


def _tokens(text):
    """Lower-case, split on non-alphanumerics, drop stop-words. Returns a set."""
    if not text:
        return set()
    return {t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t and t not in STOP}


def _issue_text(issue):
    """The searchable blob for an issue: title + description."""
    return "%s\n%s" % (issue.get("title") or "", issue.get("description") or "")


def _is_live(issue):
    return str(issue.get("status") or "").lower() not in CLOSED_STATUSES


def _overlap_fraction(query_tokens, hay_tokens):
    """Fraction of query tokens present in the haystack (0..1). Empty query -> 0."""
    if not query_tokens:
        return 0.0
    return len(query_tokens & hay_tokens) / len(query_tokens)


def _score_issue(issue, vendor, item, user_email):
    """
    Score one issue against the query. Returns (score, why_match, gated_in) where
    gated_in is whether the hard gate (item overlap or exact email) was satisfied.
    """
    hay_text = _issue_text(issue)
    hay_low = hay_text.lower()
    hay_tokens = _tokens(hay_text)

    reasons = []
    parts = []  # (weight, component_score)

    # --- vendor ---
    v_tokens = _tokens(vendor)
    if v_tokens:
        v_hit = v_tokens & hay_tokens
        v_score = len(v_hit) / len(v_tokens)
        parts.append((W_VENDOR, v_score))
        if v_hit:
            reasons.append("vendor~%d/%d [%s]" % (len(v_hit), len(v_tokens), ",".join(sorted(v_hit))))

    # --- item (the backbone signal) ---
    i_tokens = _tokens(item)
    i_score = 0.0
    if i_tokens:
        i_hit = i_tokens & hay_tokens
        i_score = len(i_hit) / len(i_tokens)
        parts.append((W_ITEM, i_score))
        if i_hit:
            reasons.append("item~%d/%d [%s]" % (len(i_hit), len(i_tokens), ",".join(sorted(i_hit))))

    # --- user ---
    email_exact = False
    if user_email:
        ue = user_email.strip().lower()
        if ue and ue in hay_low:
            email_exact = True
            u_score = 1.0
            reasons.append("email exact [%s]" % ue)
        else:
            # fall back to name-token overlap (e.g. "firstname.lastname@…")
            name_q = _tokens(re.sub(r"@.*", "", ue)) | _tokens(ue.replace("@", " ").replace(".", " "))
            u_hit = name_q & hay_tokens
            u_score = _overlap_fraction(name_q, hay_tokens)
            if u_hit:
                reasons.append("user~[%s]" % ",".join(sorted(u_hit)))
        parts.append((W_USER, u_score))

    # weighted, renormalized over supplied components
    total_w = sum(w for w, _ in parts)
    score = (sum(w * s for w, s in parts) / total_w) if total_w else 0.0

    # gate: must share the item, OR match the email verbatim
    gated_in = email_exact or (i_score >= MIN_ITEM_OVERLAP)

    return round(score, 3), "; ".join(reasons), gated_in


def find_duplicates(vendor, item, user_email, issues=None, threshold=DUP_THRESHOLD):
    """
    Report live cases that likely already cover this vendor + item + user.

    Args:
        vendor:      vendor / retailer name (may be "" / None).
        item:        the product — the primary signal (may be "" / None, but supply it).
        user_email:  the affected user's email (may be "" / None).
        issues:      optional pre-fetched issue list (for testing / a caller that already
                     has the board). Defaults to a fresh multica_api.list_issues().
        threshold:   minimum combined score to report. Defaults to DUP_THRESHOLD.

    Returns:
        A list of {identifier, title, why_match, score} for live cases at or above
        threshold that also pass the item/email gate, sorted by score descending.

    READ ONLY: never opens/edits/closes anything.
    """
    if issues is None:
        import multica_api as mc
        issues = mc.list_issues()

    out = []
    for it in issues:
        if not _is_live(it):
            continue
        score, why, gated_in = _score_issue(it, vendor or "", item or "", user_email or "")
        if gated_in and score >= threshold:
            out.append({
                "identifier": it.get("identifier"),
                "title": it.get("title"),
                "why_match": why,
                "score": score,
            })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def is_probable_dup(vendor, item, user_email, issues=None, threshold=DUP_THRESHOLD):
    """True if at least one live case is a probable duplicate of this return."""
    return bool(find_duplicates(vendor, item, user_email, issues=issues, threshold=threshold))


# =========================================================================================
# M42/M43 — THE GATE, AND WHERE IT NOW SITS
#
# HISTORY (kept because it explains the shape of this file): until 2026-07-26 this guard
# was called by nothing, for a structural reason — there was no code path in the engine
# that created a case. multica_api.py had no create at all (its only POST was
# /issues/{id}/comments) and new_claim_draft.py says in its own docstring "THIS MODULE
# NEVER OPENS A CASE". A guard cannot stand in front of a door that does not exist.
#
# M43 built the door: `multica_api.create_issue` and `new_case.py`. This gate is the FIRST
# thing in that path — see new_case.open_case(), STEP 1, which runs BEFORE any POST and
# RETURNS on a duplicate:
#
#     g = dup_guard.guard(vendor, item, user_email)
#     if not g["allowed"]:
#         return refused-verdict     # merge into / flag g["duplicates"][0]; no POST happens
#
# Not a log line after the fact — a return before the write (BLUEPRINT §10). new_case's
# self-test asserts the stubbed API received ZERO create calls on the duplicate path, so
# the ordering is proven, not asserted. It also fails CLOSED: if this guard raises,
# new_case refuses the creation rather than waving it through.
#
# The command form remains, for the human/skill path that opens a case by hand:
#
#     python3 dup_guard.py --check --vendor "T-Mobile" --item "Galaxy Z Fold 3" \
#                          --email user@example.com
#     exit 0 -> clear to open      exit 2 -> probable duplicate, do not open
# =========================================================================================
def guard(vendor, item, user_email, issues=None, threshold=DUP_THRESHOLD):
    """The pre-open gate. Returns a verdict; never opens, edits, or closes anything.

    {
      "allowed":    False when a live case probably already covers this return,
      "reason":     one line fit to show a human or log,
      "duplicates": [{identifier, title, why_match, score}, ...]  (highest score first)
    }

    allowed=False means MERGE INTO or FLAG duplicates[0] — it does not mean "warn and
    continue". The caller enforces it; this module only judges.
    """
    dups = find_duplicates(vendor, item, user_email, issues=issues, threshold=threshold)
    if not dups:
        return {
            "allowed": True,
            "reason": "no live case matches vendor=%r item=%r user=%r — clear to open"
                      % (vendor or "", item or "", user_email or ""),
            "duplicates": [],
        }
    top = dups[0]
    return {
        "allowed": False,
        "reason": "probable duplicate of %s (score %.2f: %s) — %s"
                  % (top["identifier"], top["score"], top["why_match"], top["title"]),
        "duplicates": dups,
    }


# --- fixtures / self-test -------------------------------------------------------------
# A trimmed mirror of the live board (2026-07-25) so the self-test is deterministic and
# runs anywhere with no network / token. The live check (--live) hits the real board.
_FIXTURE = [
    {"identifier": "CASE-21", "status": "todo",
     "title": "CLIENT: Robin Vale - Samsung Galaxy Z Fold 3 via T-Mobile (INTAKE INCOMPLETE)",
     "description": "CLIENT CASE. Contact robin.vale@example.com. VENDOR/ITEM: T-Mobile / "
                    "Samsung Galaxy Z Fold 3. Phase 1 intake."},
    {"identifier": "CASE-4", "status": "todo",
     "title": "CLIENT: Alex Marsh - Samsung Galaxy Tablet, defective from purchase (INTAKE INCOMPLETE)",
     "description": "CLIENT: Alex Marsh - alex.marsh@example.com. Samsung Galaxy Tablet defective."},
    {"identifier": "CASE-2", "status": "in_review",
     "title": "Case: Nike Reax 8 exchange - Claim #100000001 (SHIPPED 2026-07-24)",
     "description": "VENDOR/ITEM: Nike Inc. - Nike Reax 8 TR Mesh (Style 000000-000), Men's 12."},
    {"identifier": "CASE-3", "status": "in_review",
     "title": "Case: PPG Paints / Graco sprayer - $499 est, Tier 1 sent",
     "description": "VENDOR/ITEM: PPG Paints - Graco Ultra Cordless Handheld Airless Paint Sprayer."},
    {"identifier": "CASE-1", "status": "in_review",
     "title": "Case: HighLevel (GHL) #9000001 - $129.99 confirmed AT Stride",
     "description": "VENDOR/ITEM: HighLevel Inc. (GoHighLevel) - SaaS subscription chargebacks."},
    {"identifier": "CASE-51", "status": "cancelled",
     "title": "TEST-M8 proof [CANCELLED] - CA phone return",
     "description": "Cancelled test case. Samsung phone, T-Mobile."},
]


def _selftest():
    ok = True

    def check(name, got, cond):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print("[%s] %s -> %s" % (status, name, got))

    print("=== dup_guard self-test (fixture board) ===\n")

    # 1) Z Fold 3 / T-Mobile / the case owner -> CASE-21, NOT the other Samsung item.
    r1 = find_duplicates("T-Mobile", "Samsung Galaxy Z Fold 3", "robin.vale@example.com", issues=_FIXTURE)
    print("Test 1  (Z Fold 3 / T-Mobile / case owner):")
    for r in r1:
        print("   ", r)
    ids1 = [r["identifier"] for r in r1]
    check("top match is CASE-21", ids1[:1], ids1[:1] == ["CASE-21"])
    check("CASE-4 (the other Samsung item) not a dup", ids1, "CASE-4" not in ids1)
    check("is_probable_dup True", True, is_probable_dup("T-Mobile", "Samsung Galaxy Z Fold 3",
                                                        "robin.vale@example.com", issues=_FIXTURE))

    # 2) item-only "Nike Reax 8" -> CASE-2.
    r2 = find_duplicates("", "Nike Reax 8", "", issues=_FIXTURE)
    print("\nTest 2  (item-only 'Nike Reax 8'):")
    for r in r2:
        print("   ", r)
    check("matches CASE-2", [r["identifier"] for r in r2], [r["identifier"] for r in r2] == ["CASE-2"])

    # 3) unrelated "Dell laptop / Best Buy" -> nothing.
    r3 = find_duplicates("Best Buy", "Dell laptop", "someone@example.com", issues=_FIXTURE)
    print("\nTest 3  (unrelated 'Dell laptop / Best Buy'):")
    print("   ", r3)
    check("no matches", r3, r3 == [])
    check("is_probable_dup False", False, is_probable_dup("Best Buy", "Dell laptop",
                                                          "someone@example.com", issues=_FIXTURE) is False)

    # 4) a cancelled case must never be reported (Samsung phone / T-Mobile is CASE-51 cancelled).
    r4 = find_duplicates("T-Mobile", "Samsung phone", "", issues=_FIXTURE)
    print("\nTest 4  (cancelled cases excluded):")
    print("   ", r4)
    check("CASE-51 (cancelled) never returned", [r["identifier"] for r in r4],
          "CASE-51" not in [r["identifier"] for r in r4])

    # 5) M42 gate: the verdict a caller must obey, in both directions.
    g_block = guard("T-Mobile", "Samsung Galaxy Z Fold 3", "robin.vale@example.com", issues=_FIXTURE)
    print("\nTest 5  (M42 gate — duplicate):")
    print("   ", g_block["reason"])
    check("gate BLOCKS a duplicate", g_block["allowed"], g_block["allowed"] is False)
    check("gate names the case to merge into", g_block["duplicates"][0]["identifier"],
          g_block["duplicates"][0]["identifier"] == "CASE-21")

    g_ok = guard("Best Buy", "Dell laptop", "someone@example.com", issues=_FIXTURE)
    print("\nTest 6  (M42 gate — genuinely new):")
    print("   ", g_ok["reason"])
    check("gate ALLOWS a genuinely new case", g_ok["allowed"], g_ok["allowed"] is True)
    check("no duplicates reported", g_ok["duplicates"], g_ok["duplicates"] == [])

    print("\n%s" % ("ALL PASS" if ok else "SOME FAILED"))
    return ok


if __name__ == "__main__":
    if "--check" in sys.argv:
        # M42 gate as a command. Exit 0 = clear to open, 2 = probable duplicate, 3 = error.
        import argparse
        ap = argparse.ArgumentParser(description="M30 pre-open duplicate gate")
        ap.add_argument("--check", action="store_true")
        ap.add_argument("--vendor", default="")
        ap.add_argument("--item", default="", help="the product — the primary signal")
        ap.add_argument("--email", default="", help="the affected user's email")
        a = ap.parse_args()
        if not (a.item or a.email):
            print("REFUSING to judge: supply at least --item or --email. A gate with nothing "
                  "to match on would pass everything, which is worse than no gate.")
            sys.exit(3)
        try:
            v = guard(a.vendor, a.item, a.email)
        except Exception as exc:
            # A gate that cannot check must FAIL CLOSED — never wave a case through on an error.
            print("BLOCKED — duplicate check could not run (%s: %s). Verify by hand before "
                  "opening." % (type(exc).__name__, exc))
            sys.exit(3)
        if v["allowed"]:
            print("CLEAR — %s" % v["reason"])
            sys.exit(0)
        print("DUPLICATE — %s" % v["reason"])
        for d in v["duplicates"]:
            print("   %-8s %.2f  %s" % (d["identifier"], d["score"], d["title"]))
        print("\nMerge into / flag the case above rather than opening a second one: two issues "
              "for one return means split history and two escalation clocks on one vendor.")
        sys.exit(2)
    elif "--live" in sys.argv:
        # Hit the real board. Prints the three task test cases against live data.
        cases = [
            ("T-Mobile", "Samsung Galaxy Z Fold 3", "robin.vale@example.com"),
            ("", "Nike Reax 8", ""),
            ("Best Buy", "Dell laptop", ""),
        ]
        for v, i, u in cases:
            print("QUERY vendor=%r item=%r user=%r" % (v, i, u))
            for r in find_duplicates(v, i, u):
                print("   ", r)
            print("   is_probable_dup:", is_probable_dup(v, i, u))
            print()
    else:
        sys.exit(0 if _selftest() else 1)
