#!/usr/bin/env python3
"""
sol_watchdog.py — M15 statute-of-limitations watchdog for the AMBS Merchandise Returns Engine.

The engine's own SLAs (MR Phase Deadline, business-day windows) govern *our* internal
clock — when to nudge a vendor, when to escalate a tier. None of them protect the case
against the OUTER legal clock: the statute of limitations. A case can be perfectly
"on-schedule" against every internal deadline and still quietly cross the date after
which no lawsuit is possible — at which point every downstream lever (small-claims,
DTPA/UCL suit, the credible *threat* of suit that makes vendors settle) is dead.

M15 captures/computes that outer deadline per case and surfaces it BEFORE it lands, so a
case can never age out in silence.

    sol_status(purchase_or_incident_date, state) -> {
        limitations_years, deadline_date, days_left, urgency, ...
    }
    scan(issues=None) -> per-case SoL findings over the open board
    render_scan_report(results) -> a plain-text, RECORD-ONLY report

------------------------------------------------------------------------------------------
HONESTY / LEGAL DISCLAIMER (read before trusting any number this file prints)

Statute-of-limitations periods are NOT uniform and NOT something to assert from memory.
They vary by state, by claim type, and by *accrual rule* (does the clock run from the
purchase/tender of delivery, or from when the defect was reasonably discovered?). Tolling,
warranty-of-future-performance carve-outs, and periodic legislative changes all move the
real date. This module is a WATCHDOG, not legal advice:

  * It computes CONSERVATIVELY — it drives its alert off the EARLIEST-expiring applicable
    claim, so it warns before *any* viable lever dies, not just the last one.
  * Every period below carries verify=True and a cited basis string. The two anchors it
    leans on are the ones that are actually close to uniform:
      - UCC §2-725 breach-of-warranty on a sale of goods = 4 years from tender of
        delivery, adopted at 4y in nearly every state (a handful differ — VERIFY).
      - A conservative 2-year floor for the state UDAP / consumer-protection claim, since
        those range ~2–4y nationwide; 2y is the safe lower bound to alert against.
  * A small KNOWN table holds only the few state periods that are well-established
    (e.g. TX DTPA = 2y, CA CLRA = 3y / UCL = 4y). Even those are marked verify=True.
  * DO NOT file or represent a hard deadline to a client or a court from this file's
    output without confirming the current statute live for that state and claim.

stdlib only — runs laptop-independent on the 24/7 VPS.
"""
import os
import re
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------------------
# SoL data (conservative; every entry is verify=True). See disclaimer above.
# ---------------------------------------------------------------------------------------

# Claim types that apply to a typical merchandise / goods purchase. Both are computed and
# the SOONER-expiring one drives the alert (see _governing()).
CLAIM_DEFAULTS = {
    "ucc_warranty": {
        "years": 4,
        "runs_from": "tender of delivery (UCC §2-725; NOT discovery, absent a future-performance warranty)",
        "basis": "UCC §2-725 breach of warranty on a sale of goods — adopted at 4y in nearly all states",
        "verify": True,
    },
    "udap": {
        "years": 2,
        "runs_from": "the deceptive act or its discovery (accrual rule varies by state)",
        "basis": "state UDAP / consumer-protection act — conservative 2y FLOOR (actual range ~2-4y)",
        "verify": True,
    },
}

# State-specific overrides for the few periods that are well-established. Still verify=True.
# Keyed by 2-letter code; each maps claim -> partial override merged over CLAIM_DEFAULTS.
KNOWN_STATES = {
    "TX": {
        "udap": {
            "years": 2,
            "runs_from": "the deceptive act or when it reasonably should have been discovered",
            "basis": "Texas DTPA limitations, Bus. & Com. Code §17.565 — 2 years",
        },
    },
    "CA": {
        "udap": {
            "years": 3,
            "runs_from": "accrual/discovery (claim-specific)",
            "basis": "CA CLRA = 3y (Civ. §1783); CA UCL = 4y (B&P §17208) — using the shorter CLRA as the driver",
        },
    },
}

# Urgency bands by days remaining to the governing deadline.
URGENCY_BANDS = [
    (0, "EXPIRED"),      # days_left < 0
    (30, "CRITICAL"),    # 0..30
    (90, "WARNING"),     # 31..90
    (180, "WATCH"),      # 91..180
]
SURFACE_WITHIN_DAYS = 180  # a case is "surfaced" once its governing deadline is <=180d out

# The CRON alert threshold (M42). Deliberately TIGHTER than SURFACE_WITHIN_DAYS: the full
# report surfaces anything inside 180d, but a weekly cron that re-prints a 179-days-out case
# every week for six months trains the operator to ignore it, and an ignored watchdog is the
# same as no watchdog. The cron therefore speaks only for EXPIRED / CRITICAL / WARNING
# (<=90d) plus cases whose SoL date is missing entirely. Override with SOL_ALERT_DAYS.
ALERT_WITHIN_DAYS = int(os.environ.get("SOL_ALERT_DAYS", "90"))

# ---------------------------------------------------------------------------------------
# US state code resolution
# ---------------------------------------------------------------------------------------
STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE",
    "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
    "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
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


def resolve_state(text):
    """Extract a 2-letter US state code from a free-text 'MR Jurisdiction' value.

    Handles 'TX / Dallas County', 'CA / Los Angeles', 'TX (Texas Riverside...)',
    'Unconfirmed - likely CA (310 area code)', 'California', etc.

    Returns (code|None, confident_bool). confident is False when the text hedges
    ('unconfirmed', 'likely', 'unknown', 'assume') — the state may be wrong.
    """
    if not text:
        return None, False
    s = str(text)
    low = s.lower()
    confident = not any(w in low for w in ("unconfirm", "likely", "unknown", "assume", "?", "tbd"))
    # 1) explicit 2-letter code as a standalone token (prefer the first that is real)
    for tok in re.findall(r"\b([A-Z]{2})\b", s):
        if tok in STATE_CODES:
            return tok, confident
    # 2) full state name
    for name, code in STATE_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return code, confident
    return None, False


# ---------------------------------------------------------------------------------------
# Date parsing (structured property first, then description free-text — always approx)
# ---------------------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _to_date(v):
    """Coerce a date/datetime/ISO-string into a date, else None."""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_date_from_text(text, today=None):
    """Best-effort extraction of a purchase/incident date from free text.

    Returns (date, precise_bool, matched_str) or None. precise=False means the value is
    APPROXIMATE (year-only, year-month, or 'N years ago') and rounded to the EARLIEST
    plausible day so the SoL alert fires conservatively (earlier rather than later).

    IMPORTANT: a case description often names several dates on one line (e.g. Nike's
    'PURCHASE/DATES: ~2025-01 ... verified in chat 2026-07-17'). The purchase date is
    stated FIRST, so we pick the LEFTMOST date match — never the most-specific-anywhere,
    which would wrongly latch onto a later chat/refund date. Ties at the same position go
    to the more specific pattern (a real month/day beats a bare year at that spot).
    """
    if not text:
        return None
    today = today or date.today()
    t = str(text)
    cands = []  # (start_pos, -specificity, date, precise, matched)

    def add(mo, d, precise, spec):
        if d is not None:
            cands.append((mo.start(), -spec, d, precise, mo.group(0)))

    # full ISO  YYYY-MM-DD (specific, precise)
    for mo in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            add(mo, date(int(mo[1]), int(mo[2]), int(mo[3])), True, 4)
        except ValueError:
            pass
    # 'Month DD, YYYY' / '~May 11 2026' (specific, precise)
    for mo in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", t):
        if mo[1][:3].lower() in _MONTHS:
            try:
                add(mo, date(int(mo[3]), _MONTHS[mo[1][:3].lower()], int(mo[2])), True, 4)
            except ValueError:
                pass
    # 'Month YYYY' (approx -> 1st)
    for mo in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{4})\b", t):
        if mo[1][:3].lower() in _MONTHS:
            try:
                add(mo, date(int(mo[2]), _MONTHS[mo[1][:3].lower()], 1), False, 2)
            except ValueError:
                pass
    # year-month ISO  YYYY-MM (approx -> 1st)
    for mo in re.finditer(r"\b(\d{4})-(\d{2})\b", t):
        try:
            add(mo, date(int(mo[1]), int(mo[2]), 1), False, 2)
        except ValueError:
            pass
    # 'N years ago/old' — digits (approx). Relative dates are meaningful, rank above bare year.
    for mo in re.finditer(r"\b(\d{1,2})\s*(?:year|yr)s?\s*(?:ago|old)\b", t, re.I):
        add(mo, _add_years(today, -int(mo[1])), False, 3)
    # spelled-out 'two years ago' (approx)
    for mo in re.finditer(r"\b(" + "|".join(_WORD_NUM) + r")\s*(?:year|yr)s?\s*(?:ago|old)\b", t, re.I):
        add(mo, _add_years(today, -_WORD_NUM[mo[1].lower()]), False, 3)
    # bare year (approx -> Jan 1 = earliest day = conservative)
    for mo in re.finditer(r"\b(?:19|20)\d{2}\b", t):
        add(mo, date(int(mo[0]), 1, 1), False, 1)

    if not cands:
        return None
    cands.sort()  # leftmost start first; then higher specificity (via -spec)
    _pos, _spec, d, precise, matched = cands[0]
    return d, precise, matched


def extract_case_date(issue, today=None):
    """Find the purchase/incident date for a case.

    Priority: a structured MR property, then the description's 'PURCHASE/DATES:' line, then any
    relative/loose date in the description. Returns (date|None, precise_bool, source_str).

    `MR Purchase Date` was CREATED on the board 2026-07-26, after this watchdog's first scheduled
    run showed why it matters: MER-3's anchor was a guess parsed from the phrase "approximately
    three years ago" (and the case still flagged EXPIRED on it), and MER-4 had no derivable date
    at all. A date parsed from prose is returned with precise=False on purpose — an approximate
    anchor must never masquerade as a known one when the output is "your claim may be time-barred."
    Populate the property at intake; do not backfill it with a guess, because writing a guess into
    the structured field is exactly how a precise=False estimate becomes a precise=True fact.
    """
    mr = issue.get("mr", {}) or {}

    # 1a) THE DISCOVERY RULE COMES FIRST. Under many state consumer statutes (TX DTPA among them)
    # the limitations clock can run from when the consumer discovered — or reasonably should have
    # discovered — the defect, not from purchase. Anchoring only on purchase systematically
    # UNDER-states how much time a claimant has, and this module's output is "your claim may be
    # time-barred", so erring that way tells someone to give up on a claim that is still live.
    # Real example: MER-3's sprayer was bought ~2023 but "failed after only 6 uses", so discovery
    # is plausibly recent even though purchase is three years back.
    d = _to_date(mr.get("MR Discovery Date"))
    if d:
        return d, True, "property 'MR Discovery Date' (discovery-rule anchor)"

    # 1b) otherwise the purchase/incident anchor — the conservative reading.
    for key in ("MR Purchase Date", "MR Incident Date", "MR SoL Date", "MR Purchase/Incident Date"):
        d = _to_date(mr.get(key))
        if d:
            return d, True, "property '%s' (purchase/incident anchor)" % key

    desc = issue.get("description") or ""

    # 2) the 'PURCHASE/DATES:' line specifically (avoids matching 'sent 2026-07-17' etc.)
    mline = re.search(r"PURCHASE[/ ]?DATES?\s*:?(.+)", desc, re.I)
    if mline:
        got = parse_date_from_text(mline[1][:160], today)
        if got:
            d, precise, matched = got
            return d, precise, "description PURCHASE line ('%s')" % matched

    # 3) 'bought/purchased ... N years ago' anywhere (client verbatim quotes, etc.)
    mrel = re.search(r"(?:bought|purchas\w*)[^.]{0,60}?"
                     r"((?:\d{1,2}|" + "|".join(_WORD_NUM) + r")\s*(?:year|yr)s?\s*(?:ago|old))",
                     desc, re.I)
    if mrel:
        got = parse_date_from_text(mrel[1], today)
        if got:
            d, precise, matched = got
            return d, precise, "description relative date ('%s')" % matched

    return None, False, ""


# ---------------------------------------------------------------------------------------
# Core SoL computation
# ---------------------------------------------------------------------------------------
def _add_years(d, n):
    """Add n years to date d (n may be negative). Feb-29 -> Feb-28 on non-leap years."""
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def _claims_for(state):
    """Applicable claim configs for a state, KNOWN merged over CLAIM_DEFAULTS."""
    out = {}
    for claim, base in CLAIM_DEFAULTS.items():
        cfg = dict(base)
        override = (KNOWN_STATES.get(state or "", {}) or {}).get(claim)
        if override:
            cfg.update(override)
        out[claim] = cfg
    return out


def _urgency(days_left):
    if days_left < 0:
        return "EXPIRED"
    for limit, label in URGENCY_BANDS[1:]:
        if days_left <= limit:
            return label
    return "OK"


def sol_status(purchase_or_incident_date, state, today=None):
    """Compute a case's outer legal deadline (statute of limitations).

    Args:
        purchase_or_incident_date: date | datetime | 'YYYY-MM-DD'. The accrual anchor.
        state: 2-letter code, full state name, or None (falls back to conservative defaults).
        today: override for testing.

    Returns dict:
        limitations_years : years of the GOVERNING (earliest-expiring) claim
        deadline_date     : ISO date of that governing deadline  (the alert driver)
        days_left         : days from today to the governing deadline (negative = passed)
        urgency           : EXPIRED | CRITICAL | WARNING | WATCH | OK
        governing_claim   : which claim expires first
        outer_deadline    : ISO date of the LATEST-expiring applicable claim (full age-out)
        state / state_known : resolved state code and whether we had state-specific data
        claims            : per-claim breakdown [{claim, years, deadline, days_left,
                                                  runs_from, basis, verify}, ...]
        assumptions       : human-readable caveats
        verify            : always True — confirm live before relying on any date
    """
    today = today or date.today()
    code, _conf = (resolve_state(state) if isinstance(state, str) else (state, True))
    anchor = _to_date(purchase_or_incident_date)
    if anchor is None:
        raise ValueError("purchase_or_incident_date could not be parsed: %r" % (purchase_or_incident_date,))

    claims_cfg = _claims_for(code)
    claims = []
    for name, cfg in claims_cfg.items():
        dl = _add_years(anchor, cfg["years"])
        claims.append({
            "claim": name,
            "years": cfg["years"],
            "deadline": dl.isoformat(),
            "days_left": (dl - today).days,
            "runs_from": cfg["runs_from"],
            "basis": cfg["basis"],
            "verify": cfg.get("verify", True),
        })
    claims.sort(key=lambda c: c["deadline"])
    governing = claims[0]          # earliest to expire = the conservative alert driver
    outer = claims[-1]             # latest to expire = full age-out of the case

    assumptions = [
        "Accrual anchor assumed = %s (%s)." % (anchor.isoformat(),
                                               "purchase/incident date supplied"),
        "Alert is driven by the EARLIEST-expiring claim (%s, %dy) so no lever dies silently."
        % (governing["claim"], governing["years"]),
        "Every period is verify=True — confirm the live statute for %s before relying on it."
        % (code or "the applicable state"),
    ]
    if not code:
        assumptions.append("STATE UNKNOWN — used conservative nationwide defaults; resolve the "
                           "state to refine (some states' UDAP is 3-4y, a few UCC periods differ).")
    elif code not in KNOWN_STATES:
        assumptions.append("No state-specific period on file for %s — used the UDAP 2y floor; "
                           "the real period may be longer (2-4y). VERIFY." % code)

    return {
        "limitations_years": governing["years"],
        "deadline_date": governing["deadline"],
        "days_left": governing["days_left"],
        "urgency": _urgency(governing["days_left"]),
        "governing_claim": governing["claim"],
        "outer_deadline": outer["deadline"],
        "state": code,
        "state_known": code in KNOWN_STATES,
        "claims": claims,
        "assumptions": assumptions,
        "verify": True,
    }


# ---------------------------------------------------------------------------------------
# Board scan
# ---------------------------------------------------------------------------------------
BUILD_PROJECT = "13805886-7b4d-45bf-b985-32128f91b288"  # engine-build milestones — skip
CASE_TITLE_HINTS = ("Case:", "CLIENT:", "SELF:")
SKIP_TITLE_PREfIX = ("Daily case sweep", "KING:", "SYSTEM:", "M1 ", "M2 ")
CLOSED = {"done", "cancelled", "closed"}


def _is_real_case(issue):
    """A real merchandise-return case (not a build milestone / system / sweep task)."""
    if issue.get("project_id") == BUILD_PROJECT:
        return False
    title = issue.get("title") or ""
    if title.startswith(SKIP_TITLE_PREfIX):
        return False
    mr = issue.get("mr", {}) or {}
    return bool(mr.get("MR Phase")) or title.startswith(CASE_TITLE_HINTS)


def scan(issues=None, today=None):
    """Walk the open board and compute SoL posture for every real case.

    issues: inject a list for testing; None -> live via multica_api.list_issues().

    Returns a list of finding dicts:
        {identifier, title, state, phase, has_date, date_source, date_precise,
         missing_reason?, sol? (the sol_status dict), surfaced (bool)}
    """
    today = today or date.today()
    if issues is None:
        import multica_api as mc  # deferred so the self-test needs no network
        issues = mc.list_issues()

    findings = []
    for it in issues:
        if (it.get("status") or "").lower() in CLOSED:
            continue
        if not _is_real_case(it):
            continue
        mr = it.get("mr", {}) or {}
        code, conf = resolve_state(mr.get("MR Jurisdiction"))
        f = {
            "identifier": it.get("identifier", it.get("id", "?")),
            "title": (it.get("title") or "")[:70],
            "state": code,
            "state_confident": conf,
            "phase": mr.get("MR Phase"),
        }
        anchor, precise, source = extract_case_date(it, today)
        if anchor is None:
            f.update({
                "has_date": False,
                "missing_reason": "SoL date missing — capture the purchase/incident date",
                "surfaced": True,   # a missing SoL date is itself a thing to surface
            })
        else:
            s = sol_status(anchor, code or mr.get("MR Jurisdiction"), today=today)
            f.update({
                "has_date": True,
                "date": anchor.isoformat(),
                "date_precise": precise,
                "date_source": source,
                "sol": s,
                "surfaced": s["days_left"] <= SURFACE_WITHIN_DAYS,
            })
        findings.append(f)
    return findings


def render_scan_report(findings, today=None):
    """RECORD-ONLY plain-text report of the scan (safe to paste as an observation).

    Deliberately imperative-free per the workspace's hard-won rule #1 (a comment on a
    live-agent issue is read as an instruction). This states posture; it asks for nothing.
    """
    today = today or date.today()
    lines = ["=== M15 SoL watchdog - %s (RECORD ONLY, NO ACTION REQUIRED) ===" % today.isoformat()]
    lines.append("Every period below is a CONSERVATIVE estimate, verify=True. Not legal advice.\n")

    missing = [f for f in findings if not f.get("has_date")]
    surfaced = [f for f in findings if f.get("has_date") and f.get("surfaced")]
    ok = [f for f in findings if f.get("has_date") and not f.get("surfaced")]

    order = {"EXPIRED": 0, "CRITICAL": 1, "WARNING": 2, "WATCH": 3, "OK": 4}
    surfaced.sort(key=lambda f: order.get(f["sol"]["urgency"], 9))

    lines.append("-- SoL DATE MISSING (%d) - cannot compute the outer deadline --" % len(missing))
    for f in missing:
        st = f["state"] or "state?"
        lines.append("  [ ] %-7s %-8s %s" % (f["identifier"], st, f["title"]))
    if not missing:
        lines.append("  (none)")

    lines.append("\n-- WITHIN %dd OF SoL DEADLINE (%d) --" % (SURFACE_WITHIN_DAYS, len(surfaced)))
    for f in surfaced:
        s = f["sol"]
        approx = "" if f["date_precise"] else " ~approx"
        lines.append("  %-9s %-7s %-8s gov=%s %dy  deadline %s (%dd left)%s"
                     % (s["urgency"], f["identifier"], s["state"] or "??",
                        s["governing_claim"], s["limitations_years"],
                        s["deadline_date"], s["days_left"], approx))
        lines.append("            %s" % f["title"])
        lines.append("            anchor %s from %s | outer age-out %s"
                     % (f.get("date"), f.get("date_source"), s["outer_deadline"]))
    if not surfaced:
        lines.append("  (none within window)")

    lines.append("\n-- COMFORTABLY OPEN (>%dd) (%d) --" % (SURFACE_WITHIN_DAYS, len(ok)))
    for f in ok:
        s = f["sol"]
        lines.append("  OK        %-7s %-8s deadline %s (%dd left)"
                     % (f["identifier"], s["state"] or "??", s["deadline_date"], s["days_left"]))
    if not ok:
        lines.append("  (none)")

    lines.append("\nHOOK: report-only. Import scan()+render_scan_report() from the daily sweep or")
    lines.append("mer_engine and print/log the output (or post it prefixed 'RECORD ONLY' to a")
    lines.append("NON-live-agent log issue such as MER-16). It writes nothing and advances nothing.")
    return "\n".join(lines)


def alerting(findings, within_days=None):
    """The subset of findings a human must actually see this run.

    A finding alerts when its SoL date is MISSING (the deadline cannot even be computed)
    or when its governing deadline is within `within_days`. Everything else is healthy
    and stays silent.
    """
    within = ALERT_WITHIN_DAYS if within_days is None else within_days
    out = []
    for f in findings:
        if not f.get("has_date"):
            out.append(f)
        elif f.get("sol") and f["sol"]["days_left"] <= within:
            out.append(f)
    order = {"EXPIRED": 0, "CRITICAL": 1, "WARNING": 2, "WATCH": 3, "OK": 4}
    out.sort(key=lambda f: (0 if not f.get("has_date") else 1,
                            order.get((f.get("sol") or {}).get("urgency"), 9)))
    return out


def render_alert(findings, within_days=None, today=None):
    """Alert-only render for the cron. Returns "" when NOTHING needs a human.

    An empty string is the healthy case and MUST stay empty — the cron wrapper prints
    nothing, so the cron delivers no notification. Silence is the signal that every open
    case still has legal runway.

    Deliberately imperative-free (workspace rule #1): it states posture, asks for nothing.
    """
    within = ALERT_WITHIN_DAYS if within_days is None else within_days
    today = today or date.today()
    hits = alerting(findings, within)
    if not hits:
        return ""
    lines = ["⏳ SoL watchdog (M15) — %s — RECORD ONLY, NO ACTION REQUIRED" % today.isoformat(),
             "Conservative estimates, every period verify=True. Not legal advice."]
    for f in hits:
        if not f.get("has_date"):
            lines.append("  DATE-MISSING  %-8s %-6s %s"
                         % (f["identifier"], f["state"] or "??", f["title"]))
            continue
        s = f["sol"]
        approx = " ~approx" if not f.get("date_precise") else ""
        lines.append("  %-9s %-8s %-6s %s in %dd (gov=%s %dy, anchor %s%s)"
                     % (s["urgency"], f["identifier"], s["state"] or "??",
                        s["deadline_date"], s["days_left"], s["governing_claim"],
                        s["limitations_years"], f.get("date"), approx))
    lines.append("(full report: /opt/data/scripts/sol_watchdog.log)")
    return "\n".join(lines)


def cron_main(within_days=None):
    """M42 cron entry point.

    stdout = the ALERT only, empty when healthy (so cron delivers nothing).
    stderr = the full report, always (so the wrapper can bank it in the log).
    Exit 0 on success; 2 if the board could not be read (a scan that cannot run must
    NOT be mistaken for a clean board — silence has to mean 'checked and healthy').
    """
    try:
        findings = scan()
    except Exception as exc:
        sys.stderr.write("sol_watchdog scan failed: %s: %s\n" % (type(exc).__name__, exc))
        print("⚠️ SoL watchdog could not read the board: %s: %s" % (type(exc).__name__, exc))
        return 2
    sys.stderr.write(render_scan_report(findings) + "\n")
    alert = render_alert(findings, within_days)
    if alert:
        print(alert)
    return 0


# ---------------------------------------------------------------------------------------
# Self-test + optional live scan
# ---------------------------------------------------------------------------------------
def _selftest():
    print("=== sol_watchdog.py self-test ===\n")
    T = date(2026, 7, 25)  # fixed 'today' so assertions are stable

    # --- sol_status: TX DTPA (2y) governs over UCC (4y) ---
    tx = sol_status("2025-01-15", "TX", today=T)
    print("TX purchase 2025-01-15:", tx["governing_claim"], tx["limitations_years"], "y ->",
          tx["deadline_date"], "(%dd, %s)" % (tx["days_left"], tx["urgency"]))
    assert tx["governing_claim"] == "udap" and tx["limitations_years"] == 2, "TX UDAP=2y must govern"
    assert tx["deadline_date"] == "2027-01-15", "TX DTPA deadline = purchase + 2y"
    assert tx["outer_deadline"] == "2029-01-15", "TX UCC outer = purchase + 4y"
    assert tx["verify"] is True

    # --- sol_status: CA (CLRA 3y governs over UCC 4y) ---
    ca = sol_status("2024-03-01", "California", today=T)
    print("CA purchase 2024-03-01:", ca["governing_claim"], ca["limitations_years"], "y ->",
          ca["deadline_date"], "(%dd, %s)" % (ca["days_left"], ca["urgency"]))
    assert ca["limitations_years"] == 3 and ca["deadline_date"] == "2027-03-01", "CA CLRA=3y governs"

    # --- unknown state -> conservative 2y UDAP floor, flagged ---
    unk = sol_status("2025-06-01", None, today=T)
    assert unk["limitations_years"] == 2 and unk["state_known"] is False
    assert any("STATE UNKNOWN" in a for a in unk["assumptions"]), "must flag unknown state"
    print("state=None purchase 2025-06-01:", unk["deadline_date"], unk["urgency"],
          "(defaults, VERIFY)")

    # --- urgency bands ---
    assert sol_status(_add_years(T, -2) - timedelta(days=1), "TX", today=T)["urgency"] == "EXPIRED"  # >2y ago -> passed
    assert sol_status(_add_years(T, -2), "TX", today=T)["days_left"] == 0                             # exactly 2y ago -> 0d today
    assert sol_status("2024-09-01", "TX", today=T)["urgency"] == "WARNING"   # +2y=2026-09-01, ~38d out
    # a fresh purchase is comfortably OK
    assert sol_status("2026-07-01", "TX", today=T)["urgency"] == "OK"
    print("urgency bands OK")

    # --- date parsing (approx handling) ---
    d, precise, m = parse_date_from_text("charges ~May 11 2026.", T)
    assert (d, precise) == (date(2026, 5, 11), True), "month-day-year is precise"
    d, precise, m = parse_date_from_text("purchased in 2023, warranty expired", T)
    assert d == date(2023, 1, 1) and precise is False, "bare year -> Jan 1, approx"
    d, precise, m = parse_date_from_text("failed after 3 yrs ago", T)
    assert d == date(2023, 7, 25) and precise is False, "'3 yrs ago' -> today-3y, approx"
    d, precise, m = parse_date_from_text("bought this phone two years ago", T)
    assert d == date(2024, 7, 25) and precise is False, "'two years ago' -> today-2y, approx"
    d, precise, m = parse_date_from_text("~2025-01, within warranty", T)
    assert d == date(2025, 1, 1) and precise is False, "YYYY-MM -> 1st, approx"
    print("date parsing OK")

    # --- state resolution ---
    assert resolve_state("TX / Dallas County") == ("TX", True)
    assert resolve_state("Unconfirmed - likely CA (310 area code)") == ("CA", False)
    assert resolve_state("California") == ("CA", True)
    assert resolve_state("") == (None, False)
    print("state resolution OK")

    # --- scan() over two synthetic cases: one datable+near, one missing ---
    synthetic = [
        {   # PPG-shaped: TX, ~2023 purchase in a PURCHASE/DATES line -> UDAP already expired
            "identifier": "CASE-T1", "status": "in_review", "project_id": None,
            "title": "Case: Test PPG sprayer TX ~2023",
            "mr": {"MR Phase": "Tier1", "MR Jurisdiction": "TX (Test Riverside LLC)"},
            "description": "VENDOR/ITEM: sprayer.\nPURCHASE/DATES: ~2023 (~3 yrs ago).",
        },
        {   # intake-shaped: no date anywhere -> flagged missing
            "identifier": "CASE-T2", "status": "todo", "project_id": None,
            "title": "CLIENT: Test client tablet - defective from purchase",
            "mr": {"MR Phase": "Intake", "MR Jurisdiction": None},
            "description": "Purchase date NOT provided. Blocking intake field.",
        },
        {   # a build milestone -> must be ignored by the scan
            "identifier": "CASE-T9", "status": "todo", "project_id": BUILD_PROJECT,
            "title": "M99 — some engine build task", "mr": {}, "description": "not a case",
        },
    ]
    findings = scan(issues=synthetic, today=T)
    ids = {f["identifier"] for f in findings}
    assert ids == {"CASE-T1", "CASE-T2"}, "build milestone must be excluded; got %s" % ids
    t1 = next(f for f in findings if f["identifier"] == "CASE-T1")
    t2 = next(f for f in findings if f["identifier"] == "CASE-T2")
    assert t1["has_date"] and t1["sol"]["urgency"] == "EXPIRED", "TX ~2023 UDAP should read EXPIRED"
    assert not t2["has_date"] and t2["surfaced"], "missing-date case must be surfaced"
    print("scan() OK\n")

    print(render_scan_report(findings, today=T))

    # --- M42 cron alert: speaks only when something needs a human, silent otherwise ---
    alert = render_alert(findings, today=T)
    assert alert, "EXPIRED + missing-date cases must produce an alert"
    assert "CASE-T1" in alert and "CASE-T2" in alert, "both alerting cases must appear"
    assert "NO ACTION REQUIRED" in alert, "alert must stay record-only (workspace rule #1)"

    # a comfortably-open, fully-dated case alone => EMPTY alert => cron says nothing
    healthy = scan(issues=[{
        "identifier": "CASE-T3", "status": "in_review", "project_id": None,
        "title": "Case: Fresh purchase, plenty of runway",
        "mr": {"MR Phase": "Tier1", "MR Jurisdiction": "TX / Dallas County"},
        "description": "PURCHASE/DATES: 2026-07-01.",
    }], today=T)
    assert render_alert(healthy, today=T) == "", \
        "a healthy board MUST render an empty alert (silence-when-healthy)"
    # ...while the full report still describes it, for the log
    assert "CASE-T3" in render_scan_report(healthy, today=T)
    print("\nM42 cron alert OK (alerts on trouble, silent when healthy)")

    print("\nPASS - all sol_watchdog.py self-tests passed.")


if __name__ == "__main__":
    if "--cron" in sys.argv:
        # M42 scheduled sweep: alert-only on stdout, full report on stderr.
        sys.exit(cron_main())
    elif "--scan" in sys.argv:
        # live board scan (needs MULTICA_TOKEN in env); report-only, writes nothing
        print(render_scan_report(scan()))
    else:
        _selftest()
