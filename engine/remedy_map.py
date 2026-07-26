#!/usr/bin/env python3
"""
remedy_map.py — TIER 0: the remedy-map WRITER for the AMBS Merchandise Returns Engine.

THE GAP THIS CLOSES (confirmed 2026-07-26)
------------------------------------------
`remedy_gate.remedy_complete()` gates Tier 4 (court) on a case's remedy map, and
`case_tick.gate_check()` fails CLOSED when that map is empty. But NOTHING in the system
ever WROTE one. `MR Remedy Map` / `MR Remedy Attempted` exist on the board and are empty
on every case. Consequence: **Tier 4 was unreachable by construction, not by policy** —
court was structurally blocked for every case, forever. Tier 0 was described in SKILL.md
§2 and ladder.md phase 3, and implemented nowhere. This module is Tier 0.

WHAT IT DOES / DOES NOT DO
--------------------------
Tier 0 is **research and record-keeping only**. Nothing here sends, files, or contacts
anyone. It reads a case and writes two text properties.

THE CENTRAL DESIGN RULE — a wrongly-INCLUDED lever is worse than a missing one
------------------------------------------------------------------------------
`remedy_gate` treats every key in the map as OWED before court opens. So a lever that can
never be attempted for this case (an FCC complaint against a paint store; a chargeback on a
cash purchase) does not merely add noise — it makes Tier 4 **permanently unreachable**,
which is the exact bug this file exists to fix. The inclusion test is therefore not
"is this lever plausible?" but:

    Can this lever be ATTEMPTED and LOGGED for THIS case, by this user, today?

If the answer is not a confident yes, the lever is EXCLUDED and recorded in `excluded`
with the reason, so the omission is a documented decision and not an oversight.

KEY AGREEMENT WITH THE GATE (non-negotiable)
--------------------------------------------
Every key this module can emit is a member of `remedy_gate.LEVER_LABELS`, and no key is a
member of `remedy_gate.COURT_LEVERS`. The self-test asserts both. If the two files ever
drift, the gate becomes meaningless — it would sit waiting on a lever nothing can satisfy.

WHY THE COURT / CHARGEBACK LEVERS ARE NOT IN THE MAP
-----------------------------------------------------
Court is the thing being gated, not a prerequisite for itself (remedy_gate strips
COURT_LEVERS anyway). Chargeback is a *component of Tier 4*, runs on the card network's own
~120-day clock in parallel, and is dead on a cash purchase — putting it in the blocking map
would let a cash case block itself out of court forever. It is reported as a **money lever**
(non-blocking advisory) instead, with its window status.

§1.6 COMPLIANCE — this module never polices the user out of their own claim. Purchase age
is used ONLY to mark the *chargeback* window open/closed (an advisory), never to drop a
lever, close a case, or decline an escalation.

DATA SOURCE: skills/merchandise-return/references/jurisdiction-lookup.md (50 states + DC),
references/money-trail.md, references/ladder.md, SKILL.md §2. Statutes below are
transcribed from that table — nothing is invented. VOLATILE values (small-claims caps,
exact notice day-counts, portal URLs) are emitted as VERIFY items, never as settled fact.

Usage:
    python3 remedy_map.py --selftest        # offline, fabricated cases, exits nonzero on failure

    import remedy_map
    plan = remedy_map.build(remedy_map.case_view(issue))
    remedy_map.write(issue, plan)                       # LIVE ONLY — callers gate on --live
    remedy_map.mark_attempted(issue, "state_ag")        # idempotent append

stdlib only. multica_api is imported LAZILY (inside the write paths) so build() and the
self-test are fully offline and cannot touch a board.
"""
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remedy_gate  # noqa: E402  — the single source of truth for legal lever keys

REMEDY_MAP_PROP = "MR Remedy Map"
REMEDY_DONE_PROP = "MR Remedy Attempted"

# Card-network chargeback window (money-trail.md). Networks vary and some run from the
# expected delivery date — this is the conservative default and is always emitted as VERIFY.
CHARGEBACK_WINDOW_DAYS = 120

# Deterministic emit order = ladder order (ladder.md phase table). Sorting by this rather
# than alphabetically keeps the written property readable as an escalation sequence.
LEVER_ORDER = [
    "tier1_vendor",
    "tier2_exec",
    "industry_regulator",
    "state_statute",
    "state_ag",
    "bbb",
    "ftc",
    "class_action",
    "arbitration",
    "nonprofit_mediation",
    "civil_rights",
    "pre_suit_notice",
]

# ---------------------------------------------------------------------------------------
# Jurisdiction data — transcribed from references/jurisdiction-lookup.md (50 states + DC).
#   statute        : the primary consumer/UDAP statute (STABLE layer)
#   presuit        : mandatory pre-suit notice — the STABLE flag. "Yes"/"Often" are written
#                    in the source table; everything else is "Verify" (a prompt to confirm
#                    live, not a statement that none exists).
#   cap            : small-claims cap — VOLATILE, always emitted with a VERIFY warning.
#   civil_rights   : the state civil-rights / public-accommodation avenue.
# ---------------------------------------------------------------------------------------
STATE_DATA = {
    "AL": ("Deceptive Trade Practices Act", "Verify", "~$6,000",
           "AL has limited state public-accommodation law — use federal §1981 / Title II"),
    "AK": ("Unfair Trade Practices & Consumer Protection Act", "Verify", "~$10,000",
           "AK Human Rights Commission"),
    "AZ": ("Consumer Fraud Act", "Verify", "~$3,500", "AZ Civil Rights Division (AG)"),
    "AR": ("Deceptive Trade Practices Act", "Verify", "~$5,000", "AR — federal primary"),
    "CA": ("CLRA (Civ. §1750) + UCL (B&P §17200) + Song-Beverly implied warranty",
           "Yes — CLRA 30-day written notice before damages (Civ. §1782)", "~$12,500 (individual)",
           "Unruh Civil Rights Act (Civ. §51) — statutory minimum $4,000/violation; CA Civil Rights Dept"),
    "CO": ("Consumer Protection Act", "Verify", "~$7,500", "CO Civil Rights Division"),
    "CT": ("Unfair Trade Practices Act (CUTPA)", "Verify", "~$5,000",
           "CT Commission on Human Rights & Opportunities"),
    "DE": ("Consumer Fraud Act", "Verify", "~$25,000 (JP court)",
           "DE Division of Human & Civil Rights"),
    "DC": ("Consumer Protection Procedures Act (CPPA)", "Verify", "~$10,000",
           "DC Office of Human Rights"),
    "FL": ("FDUTPA (Deceptive & Unfair Trade Practices Act)", "Verify", "~$8,000",
           "FL Commission on Human Relations"),
    "GA": ("Fair Business Practices Act", "Yes — 30-day ante litem demand", "~$15,000 (magistrate)",
           "GA — federal primary"),
    "HI": ("UDAP (HRS ch. 480)", "Verify", "~$5,000", "HI Civil Rights Commission"),
    "ID": ("Consumer Protection Act", "Verify", "~$5,000", "ID Human Rights Commission"),
    "IL": ("Consumer Fraud & Deceptive Business Practices Act", "Verify", "~$10,000",
           "IL Dept. of Human Rights"),
    "IN": ("Deceptive Consumer Sales Act", "Often — cure/notice offer", "~$10,000",
           "IN Civil Rights Commission"),
    "IA": ("Consumer Fraud Act", "Verify", "~$6,500", "IA Civil Rights Commission"),
    "KS": ("Consumer Protection Act", "Verify", "~$4,000", "KS Human Rights Commission"),
    "KY": ("Consumer Protection Act", "Verify", "~$2,500", "KY Commission on Human Rights"),
    "LA": ("Unfair Trade Practices & Consumer Protection Law", "Verify", "~$5,000",
           "LA Commission on Human Rights"),
    "ME": ("Unfair Trade Practices Act", "Yes — written demand 30 days pre-suit", "~$6,000",
           "ME Human Rights Commission"),
    "MD": ("Consumer Protection Act", "Verify", "~$5,000", "MD Commission on Civil Rights"),
    "MA": ("Chapter 93A (up to treble damages + fees)", "Yes — 30-day demand letter (93A §9)",
           "~$7,000", "MA Commission Against Discrimination (MCAD)"),
    "MI": ("Consumer Protection Act", "Verify", "~$7,000", "MI Dept. of Civil Rights"),
    "MN": ("Prevention of Consumer Fraud Act / UDTPA", "Verify", "~$15,000",
           "MN Dept. of Human Rights"),
    "MS": ("Consumer Protection Act", "Often — must offer informal dispute resolution first",
           "~$3,500", "MS — federal primary"),
    "MO": ("Merchandising Practices Act (MMPA)", "Verify", "~$5,000",
           "MO Commission on Human Rights"),
    "MT": ("Unfair Trade Practices & Consumer Protection Act", "Verify", "~$7,000",
           "MT Human Rights Bureau"),
    "NE": ("Consumer Protection Act / UDTPA", "Verify", "~$3,900",
           "NE Equal Opportunity Commission"),
    "NV": ("Deceptive Trade Practices Act", "Verify", "~$10,000", "NV Equal Rights Commission"),
    "NH": ("Consumer Protection Act (RSA 358-A)", "Verify", "~$10,000",
           "NH Commission for Human Rights"),
    "NJ": ("Consumer Fraud Act (treble damages + fees)", "Verify", "~$5,000",
           "NJ Division on Civil Rights"),
    "NM": ("Unfair Practices Act", "Verify", "~$10,000", "NM Human Rights Bureau"),
    "NY": ("GBL §349 (deceptive acts) + §350 (false advertising)", "Verify",
           "~$10,000 (NYC) / $3–5k towns", "NY Division of Human Rights"),
    "NC": ("Unfair & Deceptive Trade Practices Act (Ch. 75) — treble", "Verify", "~$10,000",
           "NC Human Relations Commission"),
    "ND": ("Consumer fraud / UDTPA", "Verify", "~$15,000", "ND Dept. of Labor & Human Rights"),
    "OH": ("Consumer Sales Practices Act", "Verify", "~$6,000", "OH Civil Rights Commission"),
    "OK": ("Consumer Protection Act", "Verify", "~$10,000", "OK — federal primary"),
    "OR": ("Unlawful Trade Practices Act", "Verify", "~$10,000",
           "OR Bureau of Labor & Industries (BOLI)"),
    "PA": ("Unfair Trade Practices & Consumer Protection Law (UTPCPL)", "Verify", "~$12,000",
           "PA Human Relations Commission"),
    "RI": ("Deceptive Trade Practices Act", "Verify", "~$5,000", "RI Commission for Human Rights"),
    "SC": ("Unfair Trade Practices Act", "Verify", "~$7,500", "SC Human Affairs Commission"),
    "SD": ("Deceptive Trade Practices statute", "Verify", "~$12,000", "SD Division of Human Rights"),
    "TN": ("Consumer Protection Act", "Verify", "~$25,000", "TN Human Rights Commission"),
    "TX": ("DTPA (Bus. & Com. Code §17.41 et seq.)",
           "Yes — 60-day written notice (§17.505)", "~$20,000 (JP court)",
           "TX has no broad state public-accommodation retail law — use federal §1981 / Title II"),
    "UT": ("Consumer Sales Practices Act", "Verify", "~$15,000",
           "UT Antidiscrimination & Labor Division"),
    "VT": ("Consumer Protection Act", "Verify", "~$5,000", "VT Human Rights Commission"),
    "VA": ("Consumer Protection Act", "Verify", "~$5,000", "VA Office of Civil Rights (AG)"),
    "WA": ("Consumer Protection Act (Ch. 19.86)", "Verify", "~$10,000",
           "WA Human Rights Commission"),
    "WV": ("Consumer Credit & Protection Act", "Yes — 20-day notice for some claims", "~$10,000",
           "WV Human Rights Commission"),
    "WI": ("Deceptive Trade Practices Act", "Verify", "~$10,000", "WI Equal Rights Division"),
    "WY": ("Consumer Protection Act", "Verify", "~$6,000",
           "WY Dept. of Workforce — Labor Standards"),
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# ---------------------------------------------------------------------------------------
# Industry -> regulator (SKILL.md §2). Only a CONFIDENT match adds `industry_regulator`.
# Patterns are regex fragments matched with word boundaries against the case text. Bare
# ambiguous names ("frontier" = a telecom AND an airline) are deliberately absent — an
# ambiguous guess here becomes a permanently-owed lever.
# ---------------------------------------------------------------------------------------
INDUSTRY_RULES = [
    ("carrier/telecom", "FCC — consumercomplaints.fcc.gov (carrier must respond on the record)", [
        r"t-?mobile", r"verizon", r"at&t", r"sprint", r"mint mobile", r"cricket wireless",
        r"boost mobile", r"us cellular", r"spectrum", r"comcast", r"xfinity", r"cox communications",
        r"centurylink", r"frontier communications", r"optimum", r"dish network", r"directv",
        r"wireless (?:carrier|plan|service|account)", r"cell(?:ular)? (?:plan|service|carrier)",
        r"broadband", r"internet service provider", r"\bisp\b", r"landline",
    ]),
    ("airline", "DOT — aviation consumer protection", [
        r"american airlines", r"delta air", r"united airlines", r"southwest airlines",
        r"spirit airlines", r"frontier airlines", r"jetblue", r"alaska airlines",
        r"allegiant air", r"\bairline\b", r"air(?:fare|line) refund", r"denied boarding",
    ]),
    ("auto", "NHTSA (safety/defect) + the state's lemon law + state DMV/motor-vehicle board", [
        r"\bvehicle\b", r"\bautomobile\b", r"car dealership", r"auto dealer(?:ship)?",
        r"\bdealership\b", r"\bvin\b", r"lemon law", r"used car", r"new car",
        r"transmission", r"powertrain",
    ]),
    ("utility", "the state Public Utility Commission (PUC/PSC)", [
        r"electric utility", r"gas utility", r"water utility", r"power company",
        r"utility (?:bill|company|provider)", r"public utility",
    ]),
    ("insurance", "the state Department of Insurance / Insurance Commissioner", [
        r"insurance (?:policy|claim|company|carrier)", r"\binsurer\b", r"claim denial",
        r"homeowners policy", r"auto policy",
    ]),
]

# Financing / installment credit -> CFPB is the PRIMARY regulator (money-trail.md).
FINANCING_PATTERNS = [
    r"\beip\b", r"equipment installment", r"installment plan", r"installment agreement",
    r"\bbnpl\b", r"buy now,? pay later", r"\baffirm\b", r"\bklarna\b", r"afterpay",
    r"\bfinanc(?:ed|ing)\b", r"store card", r"auto loan", r"\bloan\b", r"monthly payments",
]

CARD_PATTERNS = [
    r"\bvisa\b", r"\bmastercard\b", r"\bamex\b", r"american express", r"\bdiscover card\b",
    r"credit card", r"debit card", r"card ending", r"card \*+", r"charged? to (?:my|the) card",
    r"\bchargeback\b", r"\barn\b",
]

PROCESSOR_PATTERNS = [
    (r"\bpaypal\b", "PayPal (buyer protection, its own ~180-day clock)"),
    (r"\bklarna\b", "Klarna (own dispute portal)"),
    (r"\baffirm\b", "Affirm (own dispute portal)"),
    (r"afterpay", "Afterpay (own dispute portal)"),
    (r"shop ?pay", "Shop Pay / Shopify (own dispute flow)"),
    (r"apple pay", "Apple Pay (routes to the underlying card issuer)"),
    (r"cash ?app", "Cash App (limited protection — verify)"),
    (r"\bvenmo\b", "Venmo (limited protection — verify)"),
]

CASH_PATTERNS = [
    r"\bpaid cash\b", r"\bin cash\b", r"\bcash purchase\b", r"\bcash only\b",
    r"money order", r"cashier'?s check", r"\bgift card\b", r"store credit",
]

ARBITRATION_PATTERNS = [
    r"arbitration clause", r"binding arbitration", r"notice of dispute",
    r"arbitration agreement", r"class action waiver",
]


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------

def _hay(*parts):
    """One lowercase haystack from the case's free text."""
    return " ".join(str(p) for p in parts if p).lower()


def _hit(hay, patterns):
    for p in patterns:
        if re.search(p, hay):
            return p
    return None


def resolve_state(text):
    """Free-text 'MR Jurisdiction' -> (2-letter code|None, confident_bool).

    Mirrors sol_watchdog.resolve_state deliberately (same property, same hedge words) but
    is reimplemented locally so this module stays stdlib-only and importable offline.
    """
    if not text:
        return None, False
    s = str(text)
    low = s.lower()
    confident = not any(w in low for w in ("unconfirm", "likely", "unknown", "assume", "?", "tbd"))
    for tok in re.findall(r"\b([A-Z]{2})\b", s):
        if tok in STATE_DATA:
            return tok, confident
    for name, code in STATE_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return code, confident
    return None, False


def resolve_county(text):
    """Pull a county/parish out of 'TX / Dallas County (Mesquite)'."""
    if not text:
        return None
    m = re.search(r"([A-Z][A-Za-z .'-]+?)\s+(County|Parish)\b", str(text))
    if m:
        return "%s %s" % (m.group(1).strip(), m.group(2))
    return None


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not v:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v).strip())
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"~?\s*(\d{4})-(\d{2})\b", str(v).strip())
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            return None
    return None


def _parse_amount(text):
    if not text:
        return None
    m = re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", str(text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_levers(v):
    """Property text -> [keys]. Same tolerant parse as case_tick._levers."""
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


def serialize(levers):
    """[keys] -> the property's text form (comma-separated, ladder order, deduped)."""
    return ", ".join(order_levers(levers))


def order_levers(levers):
    seen, out = set(), []
    for k in LEVER_ORDER:
        if k in levers and k not in seen:
            seen.add(k)
            out.append(k)
    for k in levers:                      # anything unrecognized keeps its own order, last
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ---------------------------------------------------------------------------------------
# case_view — Multica issue -> the plain dict build() consumes (no network; testable)
# ---------------------------------------------------------------------------------------

def case_view(issue):
    mr = issue.get("mr", {}) or {}
    desc = issue.get("description") or ""
    title = issue.get("title") or ""
    juris = mr.get("MR Jurisdiction") or ""
    state, confident = resolve_state(juris)
    county = resolve_county(juris)

    purchase = _to_date(mr.get("MR Purchase Date")) or _to_date(mr.get("MR Discovery Date"))
    if not purchase:
        m = re.search(r"PURCHASE[/ ]?DATES?\s*:?(.{0,80})", desc, re.I)
        if m:
            purchase = _to_date(re.sub(r"^[^\d~]*", "", m.group(1)).strip())

    amount = None
    m = re.search(r"AMOUNT[^\n:]*:(.{0,120})", desc, re.I)
    if m:
        amount = _parse_amount(m.group(1))
    if amount is None:
        amount = _parse_amount(title)

    flag = str(mr.get("MR Discrimination Flag", "")).lower() in ("true", "1", "yes", "✓")
    facts = ""
    m = re.search(r"DISCRIMINATION[^\n:]*:(.{0,400})", desc, re.I)
    if m:
        facts = m.group(1).strip()

    return {
        "identifier": issue.get("identifier"),
        "id": issue.get("id"),
        "title": title,
        "description": desc,
        "jurisdiction": juris,
        "state": state,
        "state_confident": confident,
        "county": county,
        "vendor": mr.get("MR Vendor") or "",
        "industry": mr.get("MR Industry") or "",
        "amount": amount,
        "payment_method": mr.get("MR Payment Method") or "",
        "money_parties": mr.get("MR Money Parties") or "",
        "purchase_date": purchase,
        "discrimination_flag": flag,
        "discrimination_facts": facts,
        "arbitration_clause": None,          # unknown unless the case text says so
        "local_consumer_office": mr.get("MR Local Consumer Office") or "",
        "existing_map": parse_levers(mr.get(REMEDY_MAP_PROP)),
        "existing_attempted": parse_levers(mr.get(REMEDY_DONE_PROP)),
    }


# ---------------------------------------------------------------------------------------
# the money trail (money-trail.md) — ADVISORY, never a blocking lever
# ---------------------------------------------------------------------------------------

def money_trail(case, hay, today=None):
    """Classify how the money moved. Returns (kind, money_levers[], notes[]).

    kind ∈ {"card", "financing", "processor", "cash", "unknown"} — the *dominant* path.
    money_levers are non-blocking: chargeback and processor disputes run on their own
    clocks in parallel with the ladder and are components of Tier 4, so they are reported,
    not owed. Putting a chargeback in the blocking map would let a cash case block itself
    out of court permanently.
    """
    today = today or date.today()
    levers, notes = [], []
    explicit = str(case.get("payment_method") or "").lower()
    text = _hay(hay, explicit)

    is_cash = bool(_hit(text, CASH_PATTERNS))
    is_fin = bool(_hit(text, FINANCING_PATTERNS))
    proc = [label for pat, label in PROCESSOR_PATTERNS if re.search(pat, text)]
    is_card = bool(_hit(text, CARD_PATTERNS))

    # Cash is only decisive when nothing else is on the money path.
    if is_cash and not (is_fin or proc or is_card):
        notes.append("Paid CASH / gift card / money order — NO card issuer, NO processor, NO "
                     "lender. Chargeback and CFPB-against-the-issuer are DEAD paths; record it "
                     "so nobody chases them (money-trail.md rule 1).")
        return "cash", [], notes

    purchase = case.get("purchase_date")
    if is_card or is_fin or proc:
        if purchase:
            age = (today - purchase).days
            if age <= CHARGEBACK_WINDOW_DAYS:
                status = ("window OPEN — ~%d of %d days used, ~%d left (VERIFY the exact window "
                          "with the issuer; some networks run it from expected delivery)"
                          % (age, CHARGEBACK_WINDOW_DAYS, CHARGEBACK_WINDOW_DAYS - age))
            else:
                status = ("window ~%d days PASSED (purchase %s) — treat chargeback as DEAD unless "
                          "the issuer says otherwise; do not build strategy on it"
                          % (age - CHARGEBACK_WINDOW_DAYS, purchase))
        else:
            status = ("window UNKNOWN — no purchase date on the case; confirm with the issuer "
                      "before relying on or writing off this path")
        if is_card:
            levers.append({"party": "card issuer", "lever": "chargeback (Tier 4 component, parallel)",
                           "status": status})
    for label in proc:
        levers.append({"party": "payment processor", "lever": "processor dispute — %s" % label,
                       "status": "own clock, separate from the card network's — VERIFY days left"})
    if is_fin:
        levers.append({"party": "financing / installment lender",
                       "lever": "CFPB complaint against the lender (primary regulator) + payoff "
                                "status as leverage",
                       "status": "record open / paid-off / in-dispute in MR Money Parties"})
        return "financing", levers, notes
    if proc:
        return "processor", levers, notes
    if is_card:
        notes.append("Card purchase with no financing: a CFPB complaint against the ISSUER is a "
                     "conditional sibling lever (strongest after a chargeback is denied or the "
                     "window is tight), not an owed Tier-0 lever — see `excluded`.")
        return "card", levers, notes

    notes.append("Payment method NOT recorded on this case. The money parties are unknown, so no "
                 "issuer/processor/lender lever could be assessed. Fill MR Money Parties at intake "
                 "— a blank reads as 'not yet investigated' (money-trail.md rule 1).")
    return "unknown", levers, notes


# ---------------------------------------------------------------------------------------
# build — THE TIER-0 STEP
# ---------------------------------------------------------------------------------------

def build(case, today=None):
    """Case dict -> the remedy plan.

    Returns:
        {
          "levers":       [keys]        the BLOCKING map remedy_gate will hold court on
          "rationale":    {key: why}    why each included lever applies AND is attemptable
          "excluded":     {key: why}    levers deliberately left out — a wrongly-included
                                        one makes Tier 4 permanently unreachable
          "regulators":   [str]         concrete targets behind `industry_regulator`
          "money_levers": [dict]        chargeback / processor / lender — ADVISORY, not owed
          "notes":        [str]         everything else worth recording
          "verify":       [str]         VOLATILE values that must be confirmed live
          "state", "county", "statute", "presuit", "civil_rights_avenue", "cap"
        }
    """
    today = today or date.today()
    levers, why, excl, notes, verify = [], {}, {}, [], []

    hay = _hay(case.get("title"), case.get("description"), case.get("vendor"),
               case.get("industry"), case.get("money_parties"), case.get("payment_method"),
               case.get("jurisdiction"))

    state = case.get("state")
    if not state:
        state, _c = resolve_state(case.get("jurisdiction"))
    county = case.get("county") or resolve_county(case.get("jurisdiction"))
    sd = STATE_DATA.get(state)

    def add(key, reason):
        if key not in levers:
            levers.append(key)
        why[key] = reason

    def drop(key, reason):
        excl[key] = reason

    # --- 1/2. The two vendor tiers. Every case has a vendor; both are always attemptable.
    vendor = case.get("vendor") or "the vendor"
    add("tier1_vendor", "Every case has a seller (and often a manufacturer). Tier 1 direct "
                        "demand to %s is always available and always loggable." % vendor)
    add("tier2_exec", "Executive/counsel escalation exists for every business — no gatekeeper "
                      "can make it unattemptable. Subsumes the public/media and elected-official "
                      "channels of ladder.md phase 8.")

    # --- 3. Industry regulator — CONDITIONAL. No confident industry match => NO lever.
    regulators = []
    explicit_ind = str(case.get("industry") or "").strip().lower()
    for name, reg, pats in INDUSTRY_RULES:
        matched = _hit(hay, pats) or (explicit_ind and explicit_ind in name)
        if matched:
            regulators.append("%s -> %s" % (name, reg))
    kind, money_levers, money_notes = money_trail(case, hay, today)
    notes.extend(money_notes)
    if kind == "financing":
        regulators.append("financing / installment credit -> CFPB — consumerfinance.gov/complaint "
                          "(the lender MUST respond, typically ~15 days)")
    if regulators:
        add("industry_regulator",
            "A specific regulator with a real intake exists for this case: %s. Filing is a "
            "single online complaint the user can make today." % "; ".join(regulators))
        verify.append("the industry regulator's CURRENT complaint portal (they move)")
    else:
        drop("industry_regulator",
             "No industry-specific regulator applies. This vendor is not a carrier (FCC), "
             "airline (DOT), auto (NHTSA/lemon law), utility (PUC), insurer (DOI), and there is "
             "no financing/installment credit making CFPB the primary regulator. Including it "
             "would leave a lever nobody could ever attempt — and remedy_gate would hold court "
             "shut forever waiting on it.")

    # --- 4/5. State statute + state AG. Every state has both (jurisdiction-lookup.md 50/50).
    if sd:
        statute, presuit, cap, cr_avenue = sd
        add("state_statute", "%s governs this purchase under %s law. Asserting it in the demand "
                             "is a drafting step, always performable." % (statute, state))
        add("state_ag", "Every state AG runs a consumer-protection complaint intake; the %s AG "
                        "accepts a complaint from a %s resident on this transaction." %
                        (state, state))
        verify.append("the %s AG consumer-complaint intake URL and whether it issues a case number"
                      % state)
    else:
        drop("state_statute",
             "No state resolved from MR Jurisdiction (%r). The governing statute cannot be named, "
             "so it cannot be asserted or attempted. FIX THE JURISDICTION, then rebuild — do not "
             "guess a state (per-case jurisdiction is a hard rule; King's TX defaults are not a "
             "fallback)." % (case.get("jurisdiction") or ""))
        drop("state_ag", "No state resolved — there is no AG to complain to until MR Jurisdiction "
                         "names a state.")
        notes.append("BLOCKING GAP: MR Jurisdiction does not resolve to a state. The map is "
                     "incomplete by construction; capture state + county at intake and rebuild.")

    # --- 6. BBB — national, accepts a complaint against any business, always attemptable.
    add("bbb", "BBB accepts a complaint against any US business regardless of accreditation, "
               "in any state. Always attemptable and always loggable.")

    # --- 7. FTC — reportfraud.ftc.gov, always open, no eligibility test.
    add("ftc", "reportfraud.ftc.gov takes any consumer report nationwide. No individual remedy, "
               "but it is a one-form step that is never unavailable.")

    # --- 8. Class-action / known-defect check — RESEARCH ONLY, never unavailable.
    add("class_action", "A documented-defect / open-class-action search is pure research: it can "
                        "always be performed and logged, and a hit is major leverage. It is never "
                        "a dead path — a negative result is still a completed attempt.")

    # --- 9. Arbitration / Notice-of-Dispute — ONLY on a recorded clause.
    arb_flag = case.get("arbitration_clause")
    arb_hit = _hit(hay, ARBITRATION_PATTERNS)
    if arb_flag is True or arb_hit:
        add("arbitration", "The case records an arbitration / Notice-of-Dispute clause%s. The "
                           "contractual pre-arbitration notice is a real, attemptable step and "
                           "often the cheapest lever in the file."
                           % ("" if arb_flag is True else " (matched %r in the case text)" % arb_hit))
    else:
        drop("arbitration",
             "No arbitration or Notice-of-Dispute clause is recorded for this vendor. Guessing "
             "one exists would create an owed lever with no clause to invoke — permanently "
             "blocking court. Pull the vendor's terms during Discovery; if a clause is found, "
             "rebuild the map and it will be added.")
        verify.append("whether %s's terms contain an arbitration / Notice-of-Dispute clause"
                      % vendor)

    # --- 10. Local county consumer office / nonprofit mediation — ONLY if one is known.
    office = str(case.get("local_consumer_office") or "").strip()
    if office:
        add("nonprofit_mediation", "A local consumer/mediation body is recorded for this case "
                                   "(%s), so it is a real, contactable option." % office)
    else:
        drop("nonprofit_mediation",
             "No county consumer-affairs office or local mediation program is recorded%s. Most "
             "US counties have none, so assuming one exists would create a lever with no office "
             "to call — court would wait on it forever. Research it at Tier 0 and add it if it "
             "is real." % (" for %s" % county if county else ""))
        if county:
            verify.append("whether %s runs a consumer-affairs office or free mediation program"
                          % county)

    # --- 11. Civil rights — ONLY on flag AND supporting facts (SKILL.md §6.4).
    flag = bool(case.get("discrimination_flag"))
    facts = str(case.get("discrimination_facts") or "").strip()
    if flag and facts:
        cr = sd[3] if sd else "federal only"
        add("civil_rights",
            "The intake discrimination flag is set AND the case records supporting facts. "
            "Tier 3-D runs in PARALLEL from the moment it is flagged (ladder.md gate rule 5): "
            "federal 42 U.S.C. §1981 (contracts) + Title II where it fits, plus %s." % cr)
        notes.append("CIVIL-RIGHTS TRACK ACTIVE — does not wait for the consumer ladder to fail.")
        if state == "CA":
            notes.append("CA: Unruh (Civ. §51) carries a $4,000 statutory minimum per violation, "
                         "which can dwarf the item's value and materially changes leverage.")
    elif flag:
        drop("civil_rights",
             "The discrimination flag is set but NO supporting facts are recorded on the case. "
             "The engine never manufactures a discrimination claim (SKILL.md §6.4) — and an "
             "unsupported lever would block court forever. Capture what was said or done, by "
             "whom, and when; then rebuild and the track opens.")
        notes.append("ACTION FOR INTAKE: the discrimination flag is set with no facts. Ask the "
                     "user what specifically was said or done, by whom, and when.")
    else:
        drop("civil_rights", "Discrimination was not flagged at intake. Never added speculatively.")

    # --- 12. Statutory pre-suit demand — ALWAYS (ladder.md phase 12 is unconditional).
    if sd:
        presuit = sd[1]
        mandatory = presuit.lower().startswith(("yes", "often"))
        add("pre_suit_notice",
            "Phase 12 is unconditional in ladder.md and a demand letter is always sendable. %s "
            % (("%s law makes it MANDATORY: %s — missing it can bar or abate the suit entirely."
                % (state, presuit))
               if mandatory else
               ("%s's requirement is not in the stable layer ('%s') — send the notice anyway: it "
                "costs nothing, it often settles, and it protects the claim if a requirement does "
                "exist." % (state, presuit))))
        verify.append("the EXACT current pre-suit notice requirement + day count for %s "
                      "(statutes change; a missed mandatory notice can bar the lawsuit)" % state)
    else:
        add("pre_suit_notice",
            "A written pre-suit demand is always sendable even before the state is known. The "
            "STATUTORY form of it cannot be drafted until MR Jurisdiction resolves.")

    # --- money-path + amount notes -----------------------------------------------------
    amount = case.get("amount")
    if sd and amount:
        verify.append("the CURRENT small-claims cap and filing fee for %s%s (listed %s — VOLATILE)"
                      % (state, " / %s" % county if county else "", sd[2]))
        capnum = _parse_amount(sd[2])
        if capnum and amount > capnum:
            notes.append("Claim $%.2f EXCEEDS the listed %s small-claims cap (%s). Small claims "
                         "may not be the right venue — check the next court up. VERIFY the cap "
                         "live; it is raised periodically." % (amount, state, sd[2]))
    if not case.get("state_confident", True) and state:
        notes.append("Jurisdiction is HEDGED (%r) — the state was inferred, not confirmed. Every "
                     "state-specific lever above is provisional until the user confirms state + "
                     "county." % case.get("jurisdiction"))

    notes.append("§1.6: purchase age was used ONLY to status the chargeback window. No lever was "
                 "dropped, and no case closed, because time has passed.")
    notes.append("Manufacturer and seller are independent targets and may be pursued in parallel; "
                 "Magnuson-Moss (15 U.S.C. §2301) reaches the manufacturer on any written warranty.")

    # levers explicitly NOT modelled as blocking, recorded so the omission is a decision
    drop("tier4_court", "Court is the destination being gated, not a prerequisite for itself "
                        "(remedy_gate.COURT_LEVERS strips it). Never emitted.")
    drop("small_claims", "Same as tier4_court — the gated destination, never an owed lever.")
    drop("online_reviews", "Public review / reputation pressure is already inside tier2_exec "
                           "(ladder.md phase 8 = exec + counsel + public/media + elected "
                           "official). Listing it separately would double-count one phase and "
                           "make court wait on a duplicate.")
    if kind == "cash":
        drop("chargeback", "Paid cash — there is no issuer to claw money back. A chargeback lever "
                           "here could NEVER be attempted, so it would block Tier 4 permanently.")
    else:
        drop("chargeback", "Chargeback is a COMPONENT of Tier 4 and runs in parallel on the card "
                           "network's own ~120-day clock (ladder.md gate rule 4). It is reported "
                           "as a money lever, not owed before court — otherwise a case whose "
                           "window has closed could never reach court.")
    if kind == "card":
        drop("cfpb_issuer", "CFPB-against-the-issuer on a plain card purchase is conditional "
                            "(strongest after a denied chargeback), not a Tier-0 certainty. "
                            "Recorded as a note, not an owed lever.")

    return {
        "levers": order_levers(levers),
        "rationale": why,
        "excluded": excl,
        "regulators": regulators,
        "money_path": kind,
        "money_levers": money_levers,
        "notes": notes,
        "verify": verify,
        "state": state,
        "county": county,
        "statute": sd[0] if sd else None,
        "presuit": sd[1] if sd else None,
        "cap": sd[2] if sd else None,
        "civil_rights_avenue": sd[3] if sd else None,
    }


def render(plan, case=None):
    """The RECORD-ONLY text written to the board. Observations, never imperatives."""
    ident = (case or {}).get("identifier") or "case"
    L = ["RECORD ONLY - NO ACTION REQUIRED [re %s]. TIER 0 REMEDY MAP built by remedy_map.py "
         "(research + record-keeping only; nothing was sent, filed, or contacted)." % ident,
         "",
         "JURISDICTION: %s%s" % (plan.get("state") or "UNRESOLVED",
                                 " / %s" % plan["county"] if plan.get("county") else ""),
         "STATUTE:      %s" % (plan.get("statute") or "(unknown — jurisdiction unresolved)"),
         "PRE-SUIT:     %s" % (plan.get("presuit") or "(unknown)"),
         "MONEY PATH:   %s" % plan.get("money_path"),
         "",
         "APPLICABLE LEVERS (%d) — this is what remedy_gate holds Tier 4 on:" % len(plan["levers"])]
    for k in plan["levers"]:
        L.append("  [ ] %-20s %s" % (k, remedy_gate.LEVER_LABELS.get(k, k)))
        L.append("        why: %s" % plan["rationale"].get(k, ""))
    if plan.get("money_levers"):
        L += ["", "MONEY LEVERS (parallel, NOT owed before court):"]
        for m in plan["money_levers"]:
            L.append("  - %s: %s — %s" % (m["party"], m["lever"], m["status"]))
    L += ["", "DELIBERATELY EXCLUDED (an inapplicable lever would make Tier 4 permanently "
              "unreachable):"]
    for k, v in sorted(plan["excluded"].items()):
        L.append("  x %-20s %s" % (k, v))
    if plan.get("verify"):
        L += ["", "VERIFY LIVE before relying on (VOLATILE layer):"]
        for v in plan["verify"]:
            L.append("  ! %s" % v)
    if plan.get("notes"):
        L += ["", "NOTES:"]
        for n in plan["notes"]:
            L.append("  - %s" % n)
    return "\n".join(L)


# ---------------------------------------------------------------------------------------
# board writes — LIVE ONLY. Callers MUST gate on --live.
# ---------------------------------------------------------------------------------------

def _api():
    import multica_api as mc  # lazy: build() and the self-test stay fully offline
    return mc


def write(issue, levers, defs=None, ws=None):
    """Persist the map to `MR Remedy Map`. Accepts a lever list OR a build() plan.

    Written through multica_api.set_properties, which uses
        PUT /api/issues/<id>/properties/<property_id> {"value": ...}
    NOT `update_issue` with a {"properties": ...} body — that returns 200 OK and SILENTLY
    DISCARDS the write (BLUEPRINT §12 defect 4). A silently-dropped remedy map would be the
    worst possible failure here: the log would say the map was written and court would stay
    shut forever.
    """
    if isinstance(levers, dict):
        levers = levers.get("levers") or []
    levers = order_levers([str(x).strip() for x in levers if str(x).strip()])
    if not levers:
        raise ValueError("refusing to write an EMPTY remedy map — a blank map fails the court "
                         "gate closed and reads as 'never investigated'")
    unknown = [k for k in levers if k not in remedy_gate.LEVER_LABELS]
    if unknown:
        raise ValueError("lever key(s) %s are not known to remedy_gate — the gate would hold "
                         "court on a lever nothing can satisfy" % unknown)
    court = [k for k in levers if k in remedy_gate.COURT_LEVERS]
    if court:
        raise ValueError("refusing to write court lever(s) %s into the map — court is the "
                         "destination, not a prerequisite" % court)
    _api().set_properties(issue, {REMEDY_MAP_PROP: serialize(levers)}, defs=defs, ws=ws)
    return levers


def mark_attempted(issue, lever, defs=None, ws=None, api=None):
    """Append `lever` to `MR Remedy Attempted`, idempotently.

    Never duplicates a lever and never loses an existing one: the current value is read from
    the issue, merged as a set, and rewritten in ladder order. Returns (changed, [levers]).
    Accepts a single key or an iterable of keys.

    `api` is injectable so a caller's self-test can prove the write path without a token —
    before M44 the only way to exercise this function was against the live board.
    """
    new = [lever] if isinstance(lever, str) else list(lever or [])
    new = [str(x).strip() for x in new if str(x).strip()]
    if not new:
        return False, parse_levers((issue.get("mr", {}) or {}).get(REMEDY_DONE_PROP))
    mc = api if api is not None else _api()
    if not isinstance(issue, dict) or "mr" not in issue:
        # A bare id / raw issue: re-read so we can never clobber an existing value.
        got = mc.get_issue(issue if not isinstance(issue, dict) else issue["id"], ws)
        existing = parse_levers((got.get("mr", {}) or {}).get(REMEDY_DONE_PROP))
        issue = got
    else:
        existing = parse_levers((issue.get("mr", {}) or {}).get(REMEDY_DONE_PROP))
    merged = list(existing)
    for k in new:
        if k not in merged:
            merged.append(k)
    if merged == existing:
        return False, existing            # already logged — no write, no duplicate
    merged = order_levers(merged)
    mc.set_properties(issue, {REMEDY_DONE_PROP: serialize(merged)}, defs=defs, ws=ws)
    if isinstance(issue.get("mr"), dict):
        issue["mr"][REMEDY_DONE_PROP] = serialize(merged)
    return True, merged


# The lever an outbound in a given MR Phase IS. Used by callers that want to pass
# `lever=` to send_queue.enqueue() so a successful send logs itself.
#
# Tier3 is DELIBERATELY ABSENT. It is not one lever but several distinct filings
# (industry_regulator / state_ag / bbb / ftc), each on its own portal, and only the person
# who filed knows which one went. Guessing would mark three levers done off one action and
# open court early — the exact 2026-07-17 gate-jump this system exists to prevent. Those
# are logged by hand: `remedy_map.py --mark-attempted --case X --lever bbb --live`.
PHASE_LEVER = {
    "Tier1": "tier1_vendor",
    "Tier2": "tier2_exec",
    "PreSuit": "pre_suit_notice",
}


# =========================================================================================
# INTEGRATION NOTE — the two call sites this module cannot add for itself (M44)
# =========================================================================================
# `case_tick.py` and `nudge.py` are owned elsewhere and a live vendor send runs off them, so
# nothing below was applied. Both are one-line, additive, and no-ops until used.
#
# 1. nudge.py, function enqueue_due(), ~line 269 — and the identical call in main(), ~line
#    311. Currently:
#        rid = send_queue.enqueue(n["case"], n["to"], n["subject"], n["body"],
#                                 action=n["action"], window_hours=0)
#    Add ONE keyword:
#                                 lever=remedy_map.PHASE_LEVER.get(n.get("phase")),
#    (plus `import remedy_map` beside the existing `import send_queue`). n["phase"] must be
#    the case's MR Phase; if the nudge dict does not carry it, pass None and leave the
#    automatic path to Tier 1/2/PreSuit sends only. With lever=None — every phase not in
#    PHASE_LEVER, and every existing caller — behaviour is byte-identical to today.
#
# 2. case_tick.py — NO EDIT IS NEEDED OR WANTED. The tick advances phases; it does not send,
#    and a phase advance is not an attempt. Marking a lever attempted when the phase changes
#    would log Tier 2 as done at the moment the case ENTERS Tier 2, before a single letter
#    goes out, and would hand court a cleared gate for work nobody did. The attempt is the
#    send, and the send happens in send_queue.process(), which is already wired.
#
# Until (1) is applied, every lever is logged through the human CLI. That is not a
# degradation: the majority of levers in a real map (state_ag, bbb, ftc,
# industry_regulator, arbitration, class_action) are portal filings this engine never
# sends anyway, so a person was always going to have to say they happened.
def log_attempt(case, lever, *, live=False, issues=None, ws=None, defs=None, api=None):
    """M44 — the CALL SITE for mark_attempted(). Log that ONE lever was actually attempted.

    THE GAP THIS CLOSES
    -------------------
    `write()` had a caller (case_tick.build_remedy_map) from the day it was written, so
    `MR Remedy Map` gets populated. `mark_attempted()` had NONE. The consequence is the
    mirror image of the bug this file was created to fix: the map fills up, nothing ever
    marks a lever done, `remedy_gate.remedy_complete()` therefore returns
    ready_for_court=False on every case forever, and Tier 4 is unreachable — again by
    construction rather than by policy. Two halves of one gate; only one had a hand on it.

    WHAT COUNTS AS AN ATTEMPT
    -------------------------
    Done AND logged. Not intended, not drafted, not queued — SENT / FILED. That is why the
    automatic call site is send_queue.process(), after the send primitive actually reports
    sent=True, and not at enqueue time: a queued letter can still be vetoed, and a vetoed
    letter that had been marked "attempted" would open court on a lever nobody ever pulled.

    FAILS CLOSED, in the direction that matters here. Marking a lever attempted OPENS a
    gate, so every doubt refuses the mark:
      * an unknown lever key is refused (a typo would silently leave the real lever owed);
      * a COURT lever is refused (court is the destination, not a prerequisite);
      * a lever that is not in this case's `MR Remedy Map` is refused, because either the
        map is wrong or the lever is — both need a human, and neither should be papered
        over by a write;
      * an EMPTY map is refused: Tier 0 has not run, so nothing is owed yet and nothing can
        be satisfied.
    DRY RUN BY DEFAULT — nothing is written without live=True.

    Returns {"logged", "refused", "reason", "case", "lever", "attempted": [...],
             "dry_run": bool}.
    """
    v = {"logged": False, "refused": False, "reason": "", "case": None, "lever": lever,
         "attempted": [], "dry_run": not live}

    key = str(lever or "").strip()
    if not key:
        v["refused"] = True
        v["reason"] = "REFUSED: no lever key given."
        return v
    if key in remedy_gate.COURT_LEVERS:
        v["refused"] = True
        v["reason"] = ("REFUSED: %r is a COURT lever. Court is the destination the gate "
                       "protects, not a prerequisite for itself." % key)
        return v
    if key not in remedy_gate.LEVER_LABELS:
        v["refused"] = True
        v["reason"] = ("REFUSED: %r is not a lever remedy_gate knows (%s). A typo here "
                       "would leave the REAL lever owed while the log claims it is done."
                       % (key, ", ".join(sorted(k for k in remedy_gate.LEVER_LABELS
                                                if k not in remedy_gate.COURT_LEVERS))))
        return v

    # Resolve the case to a real issue, without inventing one.
    issue = case
    if not (isinstance(case, dict) and isinstance(case.get("mr"), dict)):
        ident = case.get("identifier") if isinstance(case, dict) else case
        try:
            mc = api if api is not None else _api()
            board = issues if issues is not None else mc.list_issues()
            issue = next((it for it in board
                          if it.get("identifier") == ident or it.get("id") == ident), None)
        except Exception as exc:
            v["refused"] = True
            v["reason"] = ("REFUSED: could not read the board to resolve %r (%s: %s). A "
                           "gate-opening write on an unread case is not something to guess "
                           "at." % (ident, type(exc).__name__, exc))
            return v
        if issue is None:
            v["refused"] = True
            v["reason"] = "REFUSED: no issue on the board with identifier/id %r." % ident
            return v

    v["case"] = issue.get("identifier") or issue.get("id")
    props = issue.get("mr", {}) or {}
    rmap = parse_levers(props.get(REMEDY_MAP_PROP))
    already = parse_levers(props.get(REMEDY_DONE_PROP))
    v["attempted"] = already

    if not rmap:
        v["refused"] = True
        v["reason"] = ("REFUSED: %s has an EMPTY %s. Tier 0 has not run, so no lever is "
                       "owed yet and none can be satisfied. Run the remedy map first."
                       % (v["case"], REMEDY_MAP_PROP))
        return v
    if key not in rmap:
        v["refused"] = True
        v["reason"] = ("REFUSED: %r is not in %s's %s (%s). Either the map is wrong or the "
                       "lever is — both need a person, not a write."
                       % (key, v["case"], REMEDY_MAP_PROP, serialize(rmap)))
        return v
    if key in already:
        v["reason"] = ("Already logged: %r is in %s. No write, no duplicate."
                       % (key, REMEDY_DONE_PROP))
        return v

    would = merge_attempted(already, key)
    if not live:
        v["reason"] = ("DRY RUN — would set %s = %s on %s. Nothing was written; re-run "
                       "with --live." % (REMEDY_DONE_PROP, serialize(would), v["case"]))
        v["attempted"] = would
        return v

    changed, merged = mark_attempted(issue, key, defs=defs, ws=ws, api=api)
    v["logged"] = bool(changed)
    v["attempted"] = merged
    r = remedy_gate.remedy_complete(rmap, merged)
    v["reason"] = ("LOGGED %r on %s. %s = %s. %s"
                   % (key, v["case"], REMEDY_DONE_PROP, serialize(merged),
                      ("Every applicable lever is now attempted — Tier 4 may open."
                       if r["ready_for_court"] else
                       "%d lever(s) still owed before Tier 4: %s"
                       % (len(r["missing"]), ", ".join(r["missing"])))))
    return v


def merge_attempted(existing, new):
    """Pure, offline version of the merge in mark_attempted (used by the self-test)."""
    merged = list(parse_levers(existing))
    for k in (parse_levers(new) if not isinstance(new, str) else [new]):
        if k not in merged:
            merged.append(k)
    return order_levers(merged)


# ---------------------------------------------------------------------------------------
# self-test — offline, fabricated cases, no network. PASS/FAIL style per idempotency.py.
# ---------------------------------------------------------------------------------------

def _selftest():
    today = date(2026, 7, 26)
    fails = []

    def check(label, ok, detail=""):
        print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", label, detail))
        if not ok:
            fails.append(label)

    print("=== remedy_map Tier-0 self-test (offline, fabricated cases) ===\n")

    # ---------------------------------------------------------------- CASE A: TX cash
    tx_cash = {
        "identifier": "T-TXCASH", "vendor": "Riverside Hardware",
        "title": "Case: Riverside Hardware — pressure washer, $380",
        "description": ("VENDOR/ITEM: Riverside Hardware (Mesquite) sold a pressure washer that "
                        "failed after 4 uses. AMOUNT/CARD: $380 paid cash at the register — no "
                        "receipt card, paid cash. PURCHASE/DATES: 2025-03-04."),
        "jurisdiction": "TX / Dallas County (Mesquite)", "state": "TX",
        "county": "Dallas County", "amount": 380.0, "purchase_date": date(2025, 3, 4),
        "discrimination_flag": False,
    }
    a = build(tx_cash, today)
    print("A. TX cash purchase, hardware store")
    print("   levers: %s" % serialize(a["levers"]))
    check("A: money path detected as cash", a["money_path"] == "cash", a["money_path"])
    check("A: NO chargeback lever on a cash purchase",
          not any("chargeback" in str(m).lower() for m in a["money_levers"])
          and "chargeback" not in a["levers"],
          "money_levers=%s" % a["money_levers"])
    check("A: chargeback exclusion is recorded with a reason",
          "chargeback" in a["excluded"] and "no issuer" in a["excluded"]["chargeback"])
    check("A: NO industry_regulator for a hardware store",
          "industry_regulator" not in a["levers"], a["excluded"].get("industry_regulator", "")[:40])
    check("A: NO civil_rights (flag not set)", "civil_rights" not in a["levers"])
    check("A: NO arbitration (no clause recorded)", "arbitration" not in a["levers"])
    check("A: NO nonprofit_mediation (no office recorded)", "nonprofit_mediation" not in a["levers"])
    check("A: TX DTPA named + 60-day notice is MANDATORY",
          "DTPA" in a["statute"] and "60-day" in a["presuit"] and "pre_suit_notice" in a["levers"])
    check("A: TX civil-rights avenue routes to federal §1981",
          "§1981" in a["civil_rights_avenue"])
    print()

    # ---------------------------------------------------------- CASE B: CA card, carrier
    ca_carrier = {
        "identifier": "T-CACARR", "vendor": "T-Mobile",
        "title": "Case: T-Mobile — Samsung Galaxy Z Fold, $1,099",
        "description": ("VENDOR/ITEM: T-Mobile (seller) / Samsung (mfr) — Galaxy Z Fold screen "
                        "failed. AMOUNT/CARD: $1,099 charged to Visa credit card ending 4321. "
                        "PURCHASE/DATES: 2026-06-01. Wireless carrier account."),
        "jurisdiction": "CA / Los Angeles County", "state": "CA", "county": "Los Angeles County",
        "money_parties": "T-Mobile (seller); Samsung (mfr); Chase Visa (card issuer)",
        "amount": 1099.0, "purchase_date": date(2026, 6, 1), "discrimination_flag": False,
    }
    b = build(ca_carrier, today)
    print("B. CA card purchase from a phone carrier")
    print("   levers: %s" % serialize(b["levers"]))
    check("B: industry_regulator IS included for a carrier", "industry_regulator" in b["levers"])
    check("B: the regulator resolved is the FCC",
          any("FCC" in r for r in b["regulators"]), "; ".join(b["regulators"]))
    check("B: chargeback IS a money lever on a card purchase",
          any("chargeback" in m["lever"] for m in b["money_levers"]))
    check("B: chargeback window computed OPEN (55d old, 120d window)",
          any("window OPEN" in m["status"] for m in b["money_levers"]),
          [m["status"][:46] for m in b["money_levers"]])
    check("B: chargeback is NOT in the blocking map", "chargeback" not in b["levers"])
    check("B: CA CLRA/UCL named + 30-day CLRA notice mandatory",
          "CLRA" in b["statute"] and "30-day" in b["presuit"])
    check("B: NO civil_rights (flag not set, CA Unruh notwithstanding)",
          "civil_rights" not in b["levers"])
    print()

    # ------------------------------------------------------------------- CASE C: auto
    auto = {
        "identifier": "T-AUTO", "vendor": "Northside Motors",
        "title": "Case: Northside Motors — used vehicle transmission failure, $12,400",
        "description": ("VENDOR/ITEM: Northside Motors auto dealership sold a used car (VIN "
                        "1HGCM82633A004352); the transmission failed. AMOUNT/CARD: $12,400 "
                        "financed — auto loan, monthly payments, 9 of 60 paid. "
                        "PURCHASE/DATES: 2026-01-15."),
        "jurisdiction": "FL / Broward County", "state": "FL", "county": "Broward County",
        "amount": 12400.0, "purchase_date": date(2026, 1, 15), "discrimination_flag": False,
    }
    c = build(auto, today)
    print("C. Auto purchase, financed")
    print("   levers: %s" % serialize(c["levers"]))
    check("C: industry_regulator IS included for an auto case", "industry_regulator" in c["levers"])
    check("C: NHTSA + lemon law resolved",
          any("NHTSA" in r for r in c["regulators"]), "; ".join(c["regulators"]))
    check("C: financing puts CFPB in the regulator set",
          any("CFPB" in r for r in c["regulators"]))
    check("C: money path = financing", c["money_path"] == "financing", c["money_path"])
    check("C: NO FCC for a car dealer", not any("FCC" in r for r in c["regulators"]))
    check("C: NO chargeback lever — the loan is not a card, so there is no issuer to claw back",
          not any("chargeback" in m["lever"] for m in c["money_levers"]),
          [m["lever"][:38] for m in c["money_levers"]])
    check("C: the lender's CFPB lever + payoff status IS recorded",
          any("CFPB complaint against the lender" in m["lever"] for m in c["money_levers"]))
    check("C: $12,400 flagged as over the FL small-claims cap",
          any("EXCEEDS" in n for n in c["notes"]))

    # C2 — a CARD purchase whose 120-day window has run: the chargeback must be marked DEAD,
    # and — §1.6 — that must NOT remove a single lever from the blocking map.
    c2 = build(dict(ca_carrier, identifier="T-OLDCARD", purchase_date=date(2025, 8, 1)), today)
    check("C2: a stale card purchase marks the chargeback window PASSED",
          any("PASSED" in m["status"] for m in c2["money_levers"]),
          [m["status"][:40] for m in c2["money_levers"]])
    check("C2: §1.6 — an expired window drops NO lever from the map",
          c2["levers"] == b["levers"], "%d vs %d" % (len(c2["levers"]), len(b["levers"])))
    print()

    # ------------------------------------------------- CASE D: discrimination flag + facts
    disc = dict(ca_carrier, identifier="T-DISC", discrimination_flag=True,
                discrimination_facts="Store manager refused the exchange and told her 'people "
                                     "like you always try this', 2026-06-14, in front of two staff.")
    d = build(disc, today)
    print("D. Discrimination flag SET with supporting facts (CA)")
    print("   levers: %s" % serialize(d["levers"]))
    check("D: civil_rights IS included when flagged AND facts exist", "civil_rights" in d["levers"])
    check("D: §1981 + the CA Unruh avenue are both named",
          "§1981" in d["rationale"]["civil_rights"] and "Unruh" in d["rationale"]["civil_rights"])
    check("D: Unruh $4,000 statutory minimum surfaced",
          any("$4,000" in n for n in d["notes"]))

    # D2 — flag set, NO facts: the track must stay CLOSED (never manufacture a claim).
    d2 = build(dict(disc, discrimination_facts=""), today)
    check("D2: flag WITHOUT facts does NOT add civil_rights",
          "civil_rights" not in d2["levers"] and "civil_rights" in d2["excluded"])
    check("D2: the omission is surfaced as an intake action",
          any("discrimination flag is set with no facts" in n for n in d2["notes"]))
    print()

    # ------------------------------------------------ KEY AGREEMENT WITH remedy_gate
    print("E. Key agreement with remedy_gate (the two must agree or the gate is meaningless)")
    emitted = set()
    for plan in (a, b, c, c2, d, d2):
        emitted |= set(plan["levers"])
    unknown = sorted(emitted - set(remedy_gate.LEVER_LABELS))
    check("E: every emitted key is in remedy_gate.LEVER_LABELS", not unknown, "stray=%s" % unknown)
    check("E: LEVER_ORDER itself is a subset of remedy_gate.LEVER_LABELS",
          not set(LEVER_ORDER) - set(remedy_gate.LEVER_LABELS))
    court = sorted(emitted & remedy_gate.COURT_LEVERS)
    check("E: no COURT lever is ever emitted (court never gates on itself)", not court, court)
    print("   emitted taxonomy: %s" % ", ".join(order_levers(emitted)))

    # the gate must actually behave on a real map
    r_none = remedy_gate.remedy_complete(a["levers"], [])
    r_part = remedy_gate.remedy_complete(a["levers"], a["levers"][:-1])
    r_all = remedy_gate.remedy_complete(a["levers"], a["levers"])
    check("E: a fresh map leaves court SHUT", r_none["ready_for_court"] is False,
          "%d owed" % len(r_none["missing"]))
    check("E: one lever short still leaves court SHUT", r_part["ready_for_court"] is False,
          "missing=%s" % r_part["missing"])
    check("E: exhausting the map OPENS court (Tier 4 is now reachable)",
          r_all["ready_for_court"] is True)
    print()

    # ------------------------------------------------------ F. write / mark_attempted guards
    print("F. Write guards and idempotent attempt-logging (offline)")
    try:
        write({"id": "x"}, [])
        ok_empty = False
    except ValueError as e:
        ok_empty = "EMPTY remedy map" in str(e)
    check("F: write() refuses an EMPTY map", ok_empty)
    try:
        write({"id": "x"}, ["bbb", "made_up_lever"])
        ok_unknown = False
    except ValueError as e:
        ok_unknown = "not known to remedy_gate" in str(e)
    check("F: write() refuses a key remedy_gate does not know", ok_unknown)
    try:
        write({"id": "x"}, ["bbb", "tier4_court"])
        ok_court = False
    except ValueError as e:
        ok_court = "destination" in str(e)
    check("F: write() refuses a court lever in the map", ok_court)

    m1 = merge_attempted("bbb, state_ag", "bbb")
    m2 = merge_attempted("bbb, state_ag", "ftc")
    m3 = merge_attempted("", "tier1_vendor")
    check("F: re-logging an existing lever never duplicates it", m1 == ["state_ag", "bbb"], m1)
    check("F: a new lever is appended and the existing ones survive",
          m2 == ["state_ag", "bbb", "ftc"], m2)
    check("F: logging onto an empty property works", m3 == ["tier1_vendor"], m3)
    check("F: serialize/parse round-trips", parse_levers(serialize(a["levers"])) == a["levers"])

    # F3 — log_attempt(): the call site mark_attempted never had. Offline, stubbed board.
    class _StubMC(object):
        def __init__(self):
            self.writes = []

        def list_issues(self, ws=None):
            return _ATTEMPT_BOARD

        def set_properties(self, issue, values, ws=None, defs=None):
            self.writes.append((issue.get("identifier"), dict(values)))
            if isinstance(issue.get("mr"), dict):
                issue["mr"].update(values)

        def get_issue(self, issue_id, ws=None):
            return next(i for i in _ATTEMPT_BOARD if i["id"] == issue_id)

    def _board():
        return [
            {"identifier": "CASE-A", "id": "a",
             "mr": {"MR Remedy Map": "tier1_vendor, tier2_exec, bbb",
                    "MR Remedy Attempted": "tier1_vendor"}},
            {"identifier": "CASE-B", "id": "b",
             "mr": {"MR Remedy Map": "", "MR Remedy Attempted": ""}},
        ]

    _ATTEMPT_BOARD = _board()
    stub = _StubMC()
    v = log_attempt("CASE-A", "made_up_lever", live=True, api=stub)
    check("F3: an unknown lever key is REFUSED", v["refused"] and not stub.writes, v["reason"][:60])

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-A", "tier4_court", live=True, api=stub)
    check("F3: a COURT lever is REFUSED", v["refused"] and not stub.writes)

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-A", "state_ag", live=True, api=stub)
    check("F3: a lever outside this case's map is REFUSED",
          v["refused"] and not stub.writes, v["reason"][:70])

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-B", "bbb", live=True, api=stub)
    check("F3: an EMPTY remedy map REFUSES (Tier 0 has not run)",
          v["refused"] and not stub.writes)

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-A", "bbb", api=stub)
    check("F3: DRY RUN writes nothing", not v["logged"] and not stub.writes, str(stub.writes))

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-A", "tier1_vendor", live=True, api=stub)
    check("F3: re-logging an already-attempted lever writes nothing",
          not v["logged"] and not stub.writes)

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-A", "bbb", live=True, api=stub)
    check("F3: a valid, owed lever IS logged", v["logged"] and len(stub.writes) == 1,
          str(stub.writes))
    check("F3: the existing attempt survives the merge",
          parse_levers(stub.writes[0][1][REMEDY_DONE_PROP]) == ["tier1_vendor", "bbb"],
          str(stub.writes[0][1]))

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-A", "tier2_exec", live=True, api=stub)
    v = log_attempt("CASE-A", "bbb", live=True, api=stub)
    check("F3: once every owed lever is logged, the gate reports court open",
          remedy_gate.remedy_complete(["tier1_vendor", "tier2_exec", "bbb"],
                                      v["attempted"])["ready_for_court"], str(v["attempted"]))

    _ATTEMPT_BOARD = _board(); stub = _StubMC()
    v = log_attempt("CASE-NOPE", "bbb", live=True, api=stub)
    check("F3: an unknown case is REFUSED, not created", v["refused"] and not stub.writes)

    check("F3: every PHASE_LEVER value is a lever remedy_gate consumes",
          set(PHASE_LEVER.values()) <= set(remedy_gate.LEVER_LABELS),
          str(sorted(set(PHASE_LEVER.values()) - set(remedy_gate.LEVER_LABELS))))
    check("F3: no PHASE_LEVER maps a phase to a COURT lever",
          not (set(PHASE_LEVER.values()) & remedy_gate.COURT_LEVERS))
    check("F3: Tier3 is NOT auto-mapped (it is four separate filings)",
          "Tier3" not in PHASE_LEVER)
    print()

    # F2 — nothing in this module can send. Structural assertion, not a promise.
    #
    # Asserted on the AST, not on the source TEXT. The regex this replaces matched the words
    # "import send_queue" inside the integration-note COMMENT above log_attempt(), so a module
    # that cannot send reported that it could — and the whole self-test exited 1 while printing
    # PASS. A structural claim has to be checked against structure; prose defeats a substring
    # match, which is the same trap case_tick's --live gate hit.
    import ast as _ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    _send_mods = {"mer_send", "send_queue", "smtplib"}
    _imported = set()
    for _node in _ast.walk(_ast.parse(src)):
        if isinstance(_node, _ast.Import):
            _imported.update(a.name.split(".")[0] for a in _node.names)
        elif isinstance(_node, _ast.ImportFrom) and _node.module:
            _imported.add(_node.module.split(".")[0])
    check("F: Tier 0 has no send path (no smtp/mer_send/send_queue import)",
          not (_imported & _send_mods),
          "real imports checked: %d" % len(_imported))
    print()

    print("--- sample RECORD-ONLY output (case B) " + "-" * 32)
    print(render(b, ca_carrier)[:1400])
    print("-" * 70)

    if fails:
        print("\nFAIL — %d assertion(s) wrong: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("\nPASS — Tier 0 emits only levers this case can actually attempt, every key is one "
          "remedy_gate consumes, and court is reachable exactly when the map is exhausted.")
    return 0


def _mark_cli(argv):
    """M44: log an attempted lever from the command line. DRY RUN unless --live.

        python3 remedy_map.py --mark-attempted --case CASE-1 --lever state_ag
        python3 remedy_map.py --mark-attempted --case CASE-1 --lever state_ag --live

    This is the HUMAN path — the levers a person pulls by hand (an AG portal submission, a
    BBB complaint, a regulator form) leave no trace the engine can see, so somebody has to
    say so. Levers the ENGINE pulls are logged automatically by send_queue.process() once
    the send actually succeeds.

    Exit 0 = logged (or already logged). Exit 2 = refused. Exit 3 = could not run.
    """
    import argparse
    ap = argparse.ArgumentParser(description="M14/M44 — log an attempted remedy lever")
    ap.add_argument("--mark-attempted", action="store_true")
    ap.add_argument("--case", required=True, help="case identifier, e.g. CASE-1")
    ap.add_argument("--lever", required=True,
                    help="lever key, e.g. state_ag / bbb / ftc / pre_suit_notice")
    ap.add_argument("--live", action="store_true",
                    help="actually write. Without this nothing is written.")
    a = ap.parse_args(argv)
    try:
        v = log_attempt(a.case, a.lever, live=a.live)
    except Exception as exc:
        print("COULD NOT RUN (%s: %s). Nothing was written." % (type(exc).__name__, exc))
        return 3
    print(v["reason"])
    if v["attempted"]:
        print("  %s = %s" % (REMEDY_DONE_PROP, serialize(v["attempted"])))
    return 2 if v["refused"] else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--mark-attempted" in sys.argv:
        sys.exit(_mark_cli(sys.argv[1:]))
    # Bare execution is READ-ONLY: build maps for the live board and PRINT them. No writes.
    mc = _api()
    for it in mc.list_issues():
        if (it.get("mr", {}) or {}).get("MR Phase"):
            v = case_view(it)
            plan = build(v)
            print("\n" + "=" * 78)
            print("%s  %s" % (v["identifier"], v["title"][:60]))
            print("  existing map: %s" % (v["existing_map"] or "(EMPTY)"))
            print("  would build : %s" % serialize(plan["levers"]))
    print("\n(read-only preview — nothing was written)")
