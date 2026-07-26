#!/usr/bin/env python3
"""refund_landed.py — M28/M44 remedy-landed verification before CLOSE.

M44 SUMMARY (read this first)
-----------------------------
This file began as a MONEY check and is now the landing check for every remedy the ladder
can win. The public surface:

    refund_landed(case, amount, since, ...)     # money — unchanged, still the M28 contract
    remedy_landed(case, since, remedy_type=..., amount=..., tokens=...)   # any remedy
    close_gate(case, amount, since, verdict, remedy_type=..., tokens=...) # the CLOSE gate

    refund       money posted, per a bank/issuer notice or the human-confirm log
    replacement  parcel DELIVERED per the CARRIER, matched to a tracking/RMA identifier
    repair       human confirmation ONLY — the unit is back AND verified working
    store_credit the amount named AND a real credit code/card/certificate handed over

The remedy type is read from `MR Remedy Type` on the issue (onboard.MR_SCHEMA). It is never
inferred from prose and never defaulted: absent or ambiguous means the gate cannot run, and
a gate that cannot run holds the case open. The rest of this docstring describes the money
path, which is unchanged.

WHY THIS EXISTS
---------------
M19 (resolution_check.py) catches a vendor REPLY that only *looks* like a
resolution. But a genuine-sounding promise is still just a promise: "a refund of
$129.99 has been issued, allow 3-5 business days" can be true, false, partial, or
silently reversed. CASE-1 is the canonical proof — the bank and the vendor discussed the two
credits ($120.00 + $65.00) but the money NEVER posted; that non-arrival is the
entire case. Closing on the promise would have buried a live loss.

M28 is the last gate before CLOSE: it demands real-world confirmation that the
money ACTUALLY landed. A case may not move to CLOSE on a vendor's word alone.

WHAT COUNTS AS "LANDED"
-----------------------
Two honest, realistic sources for v1 (no bank API on this machine):

  1. EMAIL CONFIRMATION — a BANK / CARD-ISSUER notice, dated AFTER the resolution
     date, that confirms a CREDIT / REFUND / DEPOSIT of the expected amount POSTED
     to the account. We search the profile owner's own Gmail for the amount alongside
     credit-back language, require an affirmative POSTING verb (posted/credited/...)
     with NO negation/future/dispute wording ("missing", "will be credited",
     "pending"), strip quoted reply text, and require the money to be moving the
     RIGHT way (a credit back, not the original charge).

     A key hard-won rule: the confirming email must come from a FINANCIAL
     INSTITUTION / ISSUER, not from the VENDOR being pursued. A vendor's own email
     asserting "we credited you $X" is a PROMISE dressed in past tense — the exact
     trap M28 exists to reject (CASE-1: GHL claimed the $120.00 was "credited to
     that account"; it never reached the customer). Such vendor claims are logged
     as weak evidence but never establish landing.

  2. HUMAN CONFIRMATION (documented manual step) — the profile owner looks at the
     account and logs "I saw $X post on <date>" into /opt/data/refund_confirmations.json.
     This is the highest-confidence source and the intended fallback whenever the bank
     does not email (many issuers don't). See _human_confirm_howto() below.

HONEST LIMITATION
-----------------
Fully-automatic bank verification is impossible here without bank-API / Plaid
access. Email-confirmation + human-confirm is the realistic v1. Email confirmation
alone is graded "medium" confidence (an email can be a pending-notice or spoof);
human confirmation is "high". Absent both, landed=False — and the gate holds.

PUBLIC API
----------
    refund_landed(case, amount, since, *, token=None,
                  human_confirm_file=..., gmail_search=None)
        -> {"landed": bool, "evidence": [...], "confidence": "high|medium|low|none"}

    close_gate(case, amount, since, resolution_verdict, ...) -> dict
        The report-only CLOSE hook (see bottom of file). NOT wired into
        case_tick.py by this module — wiring is a separate, deliberate step.

`amount` may be a single number/str ("$129.99", 129.99) OR a list of them
([120.00, 65.00]); with a list, EVERY amount must be confirmed for landed=True.
`since` may be a date, datetime, or "YYYY-MM-DD" string (the resolution date;
only confirmations at/after it count).

Run directly:
    python3 refund_landed.py            # offline self-tests (synthetic strings)
    python3 refund_landed.py --live-check --case CASE-1 --amount 120.00 \
            --amount 65.00 --since 2026-06-13   # live Gmail check for one case
"""
import json
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mer_config  # noqa: E402  (M32 — identity comes from the profile, not a literal)

DEFAULT_CONFIRM_FILE = "/opt/data/refund_confirmations.json"


def _human_confirm_howto(confirm_file=DEFAULT_CONFIRM_FILE):
    """The manual-confirm instructions, addressed to whoever owns the profile (M32).

    Degrades rather than raising: this text is printed on the HOLD path, and a case that is
    already being held must not turn into a traceback because no profile is configured. The
    instructions are still correct without a name.
    """
    try:
        who = mer_config.legal_name()
    except Exception:
        who = "the profile owner"
    return """\
Manual human-confirm step (when the bank does not email a credit notice):
  %s verifies the refund posted in their account, then appends an entry to
  %s:

    {
      "CASE-1": [
        {"amount": 120.00, "date": "2026-08-02", "note": "saw it on the bank app", "by": "%s"},
        {"amount": 65.00,  "date": "2026-08-02", "note": "posted same day",        "by": "%s"}
      ]
    }

  Keyed by case identifier. Any amount whose 'date' is on/after the resolution
  date counts as landed=True at HIGH confidence. This is the intended fallback and
  is a deliberate human action — no agent may write this file on its own.

Non-money remedies (M44) use the SAME file with an explicit kind + confirmed flag:

    {
      "CASE-2": [
        {"kind": "replacement", "date": "2026-08-02", "confirmed": true,
         "token": "1Z999AA10123456784", "note": "box arrived, right model", "by": "%s"},
        {"kind": "repair",      "date": "2026-08-09", "confirmed": true,
         "token": "RMA-88213", "note": "unit back and powers on, tested 20 min", "by": "%s"}
      ]
    }

  "confirmed": true is the attestation and is never inferred. For a REPLACEMENT it means
  the item is in %s's hands; for a REPAIR it means the unit is back AND was verified
  working; for STORE CREDIT it means the balance is visible and usable. A repair has no
  other accepted proof at all — a carrier can say the box came back, but only its owner
  can say it works.""" % (
        who, confirm_file, who, who, who, who, who)

# A credit noun — money coming BACK to the customer (not a charge going out).
CREDIT_NOUN = re.compile(
    r"refund|reimburs\w*|credit|deposit|money back", re.I)

# An affirmative POSTING verb — the money actually LANDED (past/perfect, not future).
POSTING_VERB = re.compile(
    r"posted|credited|deposited|reimbursed|cleared|"
    r"applied to your|available in your|back in your account|shows in your account",
    re.I)

# Negation / not-yet / future / dispute language. Its presence in the window means
# the amount is being DISCUSSED, not confirmed landed. This is the guard that stops
# "the two refunds MISSING on your account for $120.00" from reading as posted.
NEGATION_OR_FUTURE = re.compile(
    r"missing|outstanding|unresolved|dispute|reversed|declined|denied|"
    r"failed|not (?:yet )?(?:posted|been|credited|received|show|reflect)|"
    r"no refund|never (?:posted|credited|received)|"
    r"have not|haven'?t|has not|hasn'?t|awaiting|still (?:missing|pending|waiting)|"
    r"\bpending\b|will (?:be|post|appear|credit)|would|should (?:appear|post|be)|"
    r"expect|within \d+", re.I)

# Money moving the WRONG way — original charge/debit. Same guard family as M19.
CHARGE_LANGUAGE = re.compile(
    r"charge|charged|debit|purchase|subscription|\btrial\b|billed|"
    r"payment (?:of|posted|received)|was debited|has been charged",
    re.I)

# Sender domains we trust as financial/vendor confirmation (raises confidence).
FINANCIAL_HINTS = (
    "bank", "chase", "wellsfargo", "wells fargo", "capitalone", "capital one",
    "amex", "americanexpress", "discover", "citi", "usbank", "pnc",
    "visa", "mastercard", "issuer", "stride", "paypal", "stripe", "venmo",
    "cashapp", "square", "no-reply@", "noreply@", "alerts@", "notification",
)


def _norm_amount(a):
    """A single amount -> a canonical float rounded to cents. Accepts 129.99,
    '$129.99', '129.99', '1,129.99'."""
    if isinstance(a, (int, float)):
        return round(float(a), 2)
    s = re.sub(r"[^\d.]", "", str(a))
    return round(float(s), 2) if s else None


def _amount_variants(cents_float):
    """Regex-ready string variants for a dollar amount, e.g. 120.00 ->
    (?:\\$\\s?)?120\\.00 with optional thousands separators."""
    whole = int(round(cents_float * 100))
    dollars, cents = divmod(whole, 100)
    # 1234.50 -> "1,234" and "1234"
    grouped = "{:,}".format(dollars)
    plain = str(dollars)
    dollar_alt = re.escape(grouped) if grouped != plain else re.escape(plain)
    if grouped != plain:
        dollar_alt = "(?:%s|%s)" % (re.escape(grouped), re.escape(plain))
    return re.compile(r"\$?\s?%s\.%02d\b" % (dollar_alt, cents))


def _amount_confirmed_in_text(amount_float, text, window=110):
    """Return (confirmed: bool, snippet: str|None).

    An amount is confirmed to have LANDED only when, in the text window around it:
      (a) a credit noun (refund/credit/deposit/reimbursement) is present, AND
      (b) an affirmative POSTING verb is present (posted/credited/deposited/...),
      (c) NO negation / not-yet / future / dispute language is present
          ("missing", "not posted", "will be credited", "pending", ...), AND
      (d) it is not framed purely as the original charge going out.

    (b)+(c) are what separate a real "your refund has posted" from a vendor merely
    *talking about* the refund ("the two refunds missing on your account for $X").
    """
    if not text:
        return False, None
    pat = _amount_variants(amount_float)
    low = text.lower()
    for m in pat.finditer(text):
        lo = max(0, m.start() - window)
        hi = min(len(text), m.end() + window)
        ctx_low = low[lo:hi]
        has_credit = bool(CREDIT_NOUN.search(ctx_low))
        has_posting = bool(POSTING_VERB.search(ctx_low))
        blocked = bool(NEGATION_OR_FUTURE.search(ctx_low))
        charge_only = (bool(CHARGE_LANGUAGE.search(ctx_low)) and not has_posting)
        if has_credit and has_posting and not blocked and not charge_only:
            snippet = re.sub(r"\s+", " ", text[lo:hi]).strip()
            return True, snippet
    return False, None


def _to_date(since):
    if isinstance(since, datetime):
        return since.date()
    if isinstance(since, date):
        return since
    s = str(since).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # last resort: today (fails safe toward "nothing counts before now")
    return date.today()


def _case_query(case):
    """Extract a Gmail query fragment + a display id from a case (dict or str)."""
    if isinstance(case, dict):
        q = case.get("query") or case.get("gmail_query") or ""
        mr = case.get("mr") if isinstance(case.get("mr"), dict) else {}
        ident = (case.get("identifier") or case.get("id") or case.get("name")
                 or case.get("title") or mr.get("Title") or "case")
        return q, str(ident)
    return "", str(case)


# --------------------------------------------------------------------------- #
# Human confirmation (documented manual step)
# --------------------------------------------------------------------------- #
def _human_confirmations(case_ident, amounts, since_d, path):
    """Return list of matched human-confirm evidence dicts (amount+date on/after
    since). Reads path if present; never writes it."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (IOError, ValueError):
        return {}
    entries = data.get(case_ident) or data.get(str(case_ident)) or []
    matched = {}
    for e in entries:
        amt = _norm_amount(e.get("amount"))
        if amt is None or amt not in amounts:
            continue
        try:
            ed = _to_date(e.get("date"))
        except Exception:
            continue
        if ed >= since_d:
            matched[amt] = {
                "source": "human-confirm",
                "amount": amt,
                "date": e.get("date"),
                "note": e.get("note", ""),
                "by": e.get("by", "human"),
            }
    return matched


# --------------------------------------------------------------------------- #
# Gmail search (default, live) — injectable for tests
# --------------------------------------------------------------------------- #
def _decode_body(payload):
    """Extract readable text from a Gmail message payload (text/plain preferred)."""
    import base64

    def _walk(p):
        out = []
        if not p:
            return out
        mime = p.get("mimeType", "")
        body = p.get("body", {}) or {}
        data = body.get("data")
        if data and mime.startswith("text/"):
            try:
                raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                txt = raw.decode("utf-8", "replace")
                if mime == "text/html":
                    txt = re.sub(r"<[^>]+>", " ", txt)
                out.append((mime, txt))
            except Exception:
                pass
        for sub in p.get("parts", []) or []:
            out.extend(_walk(sub))
        return out

    parts = _walk(payload)
    plains = [t for m, t in parts if m == "text/plain"]
    body = "\n".join(plains) if plains else "\n".join(t for _, t in parts)
    return _strip_quotes(body)


def _strip_quotes(text):
    """Drop quoted reply lines (">"/">>"-prefixed) and forwarded-header echoes.

    Quoted text is a copy of an EARLIER message in the thread — never a fresh
    confirmation. On CASE-1 a GHL email quoted the user's own "$120.00 refund ...
    credited to that account" claim back inside ">>" lines; without stripping,
    that echo read as a landing confirmation. We only trust the message's own new
    text."""
    if not text:
        return text
    kept = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith(">"):
            continue
        # common quoted-reply header ("On <date>, <name> wrote:") — stop there.
        if re.match(r"on .{0,80}wrote:$", s.strip(), re.I):
            break
        kept.append(line)
    return "\n".join(kept)


def _default_gmail_search(case_query, since_d, amounts, token=None, max_results=15):
    """Live Gmail search: messages after since_d that mention any expected amount +
    credit language. Returns [{id, from, subject, date, body}]. Stdlib + gmail_fetch."""
    import urllib.parse
    import urllib.request
    import gmail_fetch  # stdlib-only companion; self-refreshing token

    tok = token or gmail_fetch.access_token()
    amt_terms = " OR ".join(
        '"$%.2f" OR "%.2f"' % (a, a) for a in amounts)
    lang = "(refund OR credit OR credited OR deposit OR deposited OR posted OR reimbursed)"
    q = "(%s) %s after:%s" % (amt_terms, lang, since_d.strftime("%Y/%m/%d"))
    if case_query:
        q = "(%s) %s" % (case_query, q)
    url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages?q=%s&maxResults=%d"
           % (urllib.parse.quote(q), max_results))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % tok})
    ids = (json.loads(urllib.request.urlopen(req, timeout=25).read()).get("messages") or [])
    out = []
    for m in ids:
        try:
            msg = gmail_fetch.get_message(m["id"], token=tok)
        except Exception:
            continue
        hdrs = {h["name"]: h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
        out.append({
            "id": m["id"],
            "from": hdrs.get("From", ""),
            "subject": hdrs.get("Subject", ""),
            "date": hdrs.get("Date", ""),
            "body": _decode_body(msg.get("payload", {})),
        })
    return out


def _confidence_for_sender(frm):
    low = (frm or "").lower()
    return "medium" if any(h in low for h in FINANCIAL_HINTS) else "low"


# =========================================================================================
# M44 — EVERY REMEDY, NOT JUST MONEY
# =========================================================================================
# Until M44 this module could only reason about a refund. A case whose remedy was a
# REPLACEMENT, a REPAIR or STORE CREDIT had no automated close path at all: close_case
# demanded an amount, no amount existed, and the case failed closed forever. Safe, but it
# meant three of the four remedies the ladder can win were invisible to the engine.
#
# The generalization keeps the same shape of proof it always demanded for money:
#   * a THIRD PARTY, not the vendor being pursued, says the thing actually arrived; or
#   * the person themselves logs that they have it, in the human-confirm file.
# The vendor's own word is never sufficient for any remedy — that is the whole thesis of
# this file, and it does not weaken because the remedy stopped being cash.
#
# THE BOARD PROPERTY
# ------------------
# The remedy type is read from `MR Remedy Type` on the issue — a select added to
# onboard.MR_SCHEMA in M44 (Refund / Replacement / Repair / StoreCredit). It is NOT
# inferred from prose, and it is NOT defaulted: an absent or unreadable value means the
# gate cannot run, and a gate that cannot run holds the case open.
#
# WHY IT IS SET AT RESOLUTION, NOT AT INTAKE
# ------------------------------------------
# Intake question 5.1 asks for a RANKED list ("refund / replacement / repair / credit").
# A ranked list names several families and is therefore ambiguous — normalize_remedy()
# deliberately returns None for it. The remedy that decides how a case CLOSES is the one
# the vendor actually granted, which is known at resolution time. new_case.py seeds the
# property only when the intake names exactly one family; otherwise it is left blank and
# set when the grant arrives.
REMEDY_TYPE_PROP = "MR Remedy Type"
REMEDY_TYPES = ("refund", "replacement", "repair", "store_credit")
# Remedies denominated in money — these carry an amount and use the original M28 logic.
MONEY_REMEDIES = ("refund", "store_credit")

_REMEDY_FAMILY_WORDS = {
    "refund": ("refund", "refunded", "reimburse", "reimbursement", "money back",
               "chargeback", "credit back", "trace"),
    "replacement": ("replacement", "replace", "exchange", "reship", "new unit",
                    "send another", "swap"),
    "repair": ("repair", "repaired", "fix", "fixed", "service", "servicing"),
    "store_credit": ("store credit", "storecredit", "gift card", "giftcard",
                     "gift certificate", "voucher", "account credit",
                     "merchandise credit", "e gift", "egift"),
}

# Senders that are INDEPENDENT of the vendor for a shipment: the carrier. The carrier is to
# a replacement what the bank is to a refund — a third party with no stake in the dispute
# saying the thing physically arrived.
CARRIER_HINTS = (
    "ups.com", "fedex", "usps", "dhl", "ontrac", "lasership", "purolator",
    "canadapost", "auspost", "royalmail", "shipment-tracking", "tracking@",
    "shipping@", "no-reply@", "noreply@", "notification",
)

# A parcel actually ARRIVED. "Shipped" is not "delivered" — see SHIPPING_FUTURE.
DELIVERED_VERB = re.compile(
    r"delivered|left at|handed to|signed for|front door|front desk|"
    r"picked up at|available for pickup|received at", re.I)

# Everything that means the parcel is still MOVING. Its presence blocks a landing exactly
# the way "will be credited" blocks a refund: a promise about the future, not an arrival.
SHIPPING_FUTURE = re.compile(
    r"label (?:created|printed)|pre-?shipment|shipment created|in transit|"
    r"out for delivery|on (?:its|the) way|has shipped|have shipped|is shipping|"
    r"dispatched|expected|estimated|scheduled|will (?:arrive|ship|be delivered)|"
    r"tracking (?:number|info)", re.I)

# The nouns that name each remedy's deliverable inside a message.
REMEDY_NOUN = {
    "replacement": re.compile(
        r"replacement|new unit|exchange|your (?:package|parcel|shipment|order)|"
        r"shipment|parcel|package", re.I),
    "repair": re.compile(
        r"repair|repaired|serviced|service order|work order|\brma\b", re.I),
    "store_credit": re.compile(
        r"store credit|gift card|gift certificate|merchandise credit|account credit|"
        r"voucher|e-?gift|credit balance", re.I),
}

# A repair is "landed" only when the unit is back AND WORKS. No email can attest the second
# half, which is why REQUIRES_HUMAN below names repair and nothing else.
REPAIR_DONE_VERB = re.compile(
    r"repair (?:is |has been |was )?(?:complete|completed|finished)|"
    r"completed the repair|repaired and (?:returned|shipped)|"
    r"returned to you|delivered back|passed (?:testing|inspection)", re.I)

STORE_CREDIT_VERB = re.compile(
    r"issued|applied to your|added to your|credited to your|is (?:now )?available|"
    r"has been added|now shows|reflects", re.I)

# A store credit's deliverable is an ARTIFACT — a code, a card number, a certificate id.
# A message that names one is handing over the thing itself; a message that only promises
# one ("a credit will be issued") is the same promise this module has always rejected.
CREDIT_ARTIFACT = re.compile(
    r"(?:code|certificate|voucher|card|credit)\s*(?:#|no\.?|number|id)?\s*[:\-]?\s*"
    r"([A-Z0-9][A-Z0-9-]{5,})")

# Remedies no email can ever settle, because the decisive fact is physical and private to
# the owner. For these, ONLY a human confirmation establishes landing.
REQUIRES_HUMAN = ("repair",)


def normalize_remedy(value):
    """A remedy string from the board (or a CLI flag) -> one of REMEDY_TYPES, or None.

    None is returned for anything blank, unrecognised, OR AMBIGUOUS. Ambiguity is the
    common case and the important one: intake 5.1 collects a RANKED preference list
    ("refund / replacement / repair / credit"), which names several families at once. A
    guess between them would decide how a case closes, so it is refused — the caller gets
    None and the gate holds.
    """
    if value is None:
        return None
    s = re.sub(r"[\s_/|,;-]+", " ", str(value).strip().lower())
    if not s:
        return None
    direct = s.replace(" ", "_")
    if direct in REMEDY_TYPES:
        return direct
    hits = set()
    for fam, words in _REMEDY_FAMILY_WORDS.items():
        for w in words:
            if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(w), s):
                hits.add(fam)
                break
    return hits.pop() if len(hits) == 1 else None


def remedy_type_from_case(case):
    """Read the remedy type off the case's REAL board data. Never invents one.

    Looks at `case["mr"]["MR Remedy Type"]` first (the property multica_api resolves by
    name), then a top-level `remedy_type` / `remedy` key so a plain dict from a CLI or a
    test can carry it. Returns None when the property is absent, blank or ambiguous —
    which every caller must treat as "cannot check", not as "refund".
    """
    if not isinstance(case, dict):
        return None
    sources = []
    if isinstance(case.get("mr"), dict):
        sources.append(case["mr"])
    sources.append(case)
    for src in sources:
        for key in (REMEDY_TYPE_PROP, "remedy_type", "remedy"):
            if key in src:
                t = normalize_remedy(src.get(key))
                if t:
                    return t
    return None


def _human_remedy_confirmations(case_ident, remedy_type, since_d, path, anchors=None):
    """Human confirmations for a NON-money remedy. Reads `path`; never writes it.

    An entry counts only when ALL of these hold — anything less is not a confirmation:
        {"kind": "<remedy_type>", "date": "YYYY-MM-DD", "confirmed": true, ...}
      * kind matches the remedy being checked,
      * date is on/after the resolution date,
      * "confirmed" is explicitly true. The flag is the whole point: for a repair it means
        "the unit is back AND I verified it works", for a replacement "I have it in my
        hands", for a store credit "I can see the balance and it is usable". A dated entry
        without it is a note, not an attestation.
      * if `anchors` are supplied (tracking / RMA / credit code), the entry's own
        "token"/"note" must mention one of them, so a confirmation cannot be recycled from
        a different shipment on the same case.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (IOError, ValueError):
        return []
    entries = data.get(case_ident) or data.get(str(case_ident)) or []
    anchors = [str(a).strip().lower() for a in (anchors or []) if str(a).strip()]
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        kind = normalize_remedy(e.get("kind") or e.get("remedy") or e.get("remedy_type"))
        if kind != remedy_type:
            continue
        if e.get("confirmed") is not True:
            continue
        try:
            ed = _to_date(e.get("date"))
        except Exception:
            continue
        if ed < since_d:
            continue
        if anchors:
            hay = ("%s %s" % (e.get("token", ""), e.get("note", ""))).lower()
            if not any(a in hay for a in anchors):
                continue
        out.append({
            "source": "human-confirm", "remedy": remedy_type, "date": e.get("date"),
            "note": e.get("note", ""), "by": e.get("by", "human"),
            "token": e.get("token", ""),
        })
    return out


def _remedy_evidence_in_text(remedy_type, text, anchors, window=160):
    """(confirmed, snippet) for a non-money remedy in one message body.

    Same three-part test the money path uses, transposed:
      (a) the deliverable is NAMED (REMEDY_NOUN),
      (b) an affirmative ARRIVAL/COMPLETION verb is present,
      (c) no negation / not-yet / still-in-transit language sits in the same window.
    When anchors (tracking number, RMA, credit code) are supplied the match must sit near
    one of them, so an unrelated parcel on the same thread cannot close the case.
    """
    if not text:
        return False, None
    noun = REMEDY_NOUN.get(remedy_type)
    if noun is None:
        return False, None
    if remedy_type == "replacement":
        verb, extra_block = DELIVERED_VERB, SHIPPING_FUTURE
    elif remedy_type == "repair":
        verb, extra_block = REPAIR_DONE_VERB, SHIPPING_FUTURE
    elif remedy_type == "store_credit":
        verb, extra_block = STORE_CREDIT_VERB, None
    else:
        return False, None

    spans = []
    if anchors:
        low = text.lower()
        for a in anchors:
            a = str(a).strip().lower()
            if not a:
                continue
            i = low.find(a)
            while i != -1:
                spans.append((i, i + len(a)))
                i = low.find(a, i + 1)
        if not spans:
            return False, None
    else:
        spans = [(m.start(), m.end()) for m in verb.finditer(text)]

    for start, end in spans:
        lo = max(0, start - window)
        hi = min(len(text), end + window)
        ctx = text[lo:hi]
        if not noun.search(ctx) or not verb.search(ctx):
            continue
        if NEGATION_OR_FUTURE.search(ctx):
            continue
        if extra_block is not None and extra_block.search(ctx):
            continue
        return True, re.sub(r"\s+", " ", ctx).strip()
    return False, None


def _remedy_sender_confidence(remedy_type, frm):
    """How much a sender is worth for THIS remedy.

    replacement -> the CARRIER is the independent witness (the bank's analogue).
    repair      -> nothing an email says can prove the unit works; email is never enough.
    store_credit-> the vendor IS the ledger, so there is no independent third party at all;
                   see _store_credit_ok for what has to make up the difference.
    """
    low = (frm or "").lower()
    if remedy_type == "replacement":
        return "medium" if any(h in low for h in CARRIER_HINTS) else "low"
    if remedy_type == "store_credit":
        return "medium"
    return "low"


def _non_money_landed(case, remedy_type, since, *, tokens=None, token=None,
                      human_confirm_file=DEFAULT_CONFIRM_FILE, gmail_search=None):
    """Landing check for replacement / repair / store_credit. Fails closed everywhere."""
    since_d = _to_date(since)
    query, ident = _case_query(case)
    anchors = [str(t).strip() for t in (tokens or []) if str(t).strip()]
    evidence = []

    # 1) Human confirmation — always sufficient, and for a repair the ONLY thing that is.
    human = _human_remedy_confirmations(ident, remedy_type, since_d, human_confirm_file,
                                        anchors)
    if human:
        evidence.extend(human)
        return {"landed": True, "confidence": "high", "evidence": evidence,
                "remedy_type": remedy_type, "anchors": anchors,
                "since": since_d.isoformat(), "case": ident, "amounts": []}

    if remedy_type in REQUIRES_HUMAN:
        evidence.append({
            "source": "gap",
            "detail": ("a %s can only be confirmed by the owner: the unit has to be BACK "
                       "and verified WORKING, and no email can attest the second half. "
                       "No human confirmation on/after %s — holding."
                       % (remedy_type, since_d.isoformat())),
            "missing": [remedy_type]})
        return {"landed": False, "confidence": "none", "evidence": evidence,
                "remedy_type": remedy_type, "anchors": anchors,
                "since": since_d.isoformat(), "case": ident, "amounts": []}

    if not anchors:
        # No tracking number, RMA or credit code to match on. A search with no anchor would
        # match any parcel on the thread — a gate that matches anything is not a gate.
        evidence.append({
            "source": "gap",
            "detail": ("cannot check: no tracking / RMA / credit identifier supplied for a "
                       "%s, so there is nothing to tie an arrival to THIS remedy"
                       % remedy_type),
            "missing": [remedy_type]})
        return {"landed": False, "confidence": "none", "evidence": evidence,
                "remedy_type": remedy_type, "anchors": anchors,
                "since": since_d.isoformat(), "case": ident, "amounts": []}

    # 2) Third-party email confirmation.
    search = gmail_search or _default_remedy_search
    try:
        msgs = search(query, since_d, anchors, token=token) or []
    except TypeError:
        msgs = search(query, since_d, anchors) or []
    except Exception as exc:
        evidence.append({"source": "gmail-error", "detail": str(exc)[:180]})
        msgs = []

    for msg in msgs:
        raw = _strip_quotes(msg.get("body") or "")
        body = raw + "\n" + (msg.get("subject") or "")
        ok, snippet = _remedy_evidence_in_text(remedy_type, body, anchors)
        if not ok:
            continue
        conf = _remedy_sender_confidence(remedy_type, msg.get("from", ""))
        base = {"remedy": remedy_type, "from": msg.get("from", ""),
                "subject": (msg.get("subject") or "")[:120], "date": msg.get("date", ""),
                "gmail_id": msg.get("id", ""), "snippet": (snippet or "")[:220]}
        if remedy_type == "store_credit" and not CREDIT_ARTIFACT.search(body):
            # The vendor is the issuer, so its word is all there will ever be. A credit
            # CODE is the deliverable itself; without one this is still just a promise.
            evidence.append(dict(base, source="email-weak", confidence="low",
                                 note="store-credit claim with no credit code / card / "
                                      "certificate identifier — an issuance promise, not "
                                      "the credit itself"))
            continue
        if conf != "medium":
            evidence.append(dict(base, source="email-weak", confidence="low",
                                 note="not an independent third party (carrier/issuer) — "
                                      "the vendor being pursued cannot confirm its own "
                                      "delivery"))
            continue
        evidence.append(dict(base, source="email", confidence="medium"))
        return {"landed": True, "confidence": "medium", "evidence": evidence,
                "remedy_type": remedy_type, "anchors": anchors,
                "since": since_d.isoformat(), "case": ident, "amounts": []}

    evidence.append({
        "source": "gap",
        "detail": "no independent confirmation that the %s arrived at/after %s (anchors: %s)"
                  % (remedy_type, since_d.isoformat(), ", ".join(anchors) or "none"),
        "missing": [remedy_type]})
    return {"landed": False, "confidence": "none" if not evidence else "low",
            "evidence": evidence, "remedy_type": remedy_type, "anchors": anchors,
            "since": since_d.isoformat(), "case": ident, "amounts": []}


def _default_remedy_search(case_query, since_d, anchors, token=None, max_results=15):
    """Live Gmail search for a non-money remedy: messages after since_d mentioning any
    supplied tracking / RMA / credit identifier. Same transport as the money search."""
    import urllib.parse
    import urllib.request
    import gmail_fetch

    tok = token or gmail_fetch.access_token()
    terms = " OR ".join('"%s"' % a for a in anchors)
    q = "(%s) after:%s" % (terms, since_d.strftime("%Y/%m/%d"))
    if case_query:
        q = "(%s) %s" % (case_query, q)
    url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages?q=%s&maxResults=%d"
           % (urllib.parse.quote(q), max_results))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % tok})
    ids = (json.loads(urllib.request.urlopen(req, timeout=25).read()).get("messages") or [])
    out = []
    for m in ids:
        try:
            msg = gmail_fetch.get_message(m["id"], token=tok)
        except Exception:
            continue
        hdrs = {h["name"]: h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
        out.append({"id": m["id"], "from": hdrs.get("From", ""),
                    "subject": hdrs.get("Subject", ""), "date": hdrs.get("Date", ""),
                    "body": _decode_body(msg.get("payload", {}))})
    return out


def remedy_landed(case, since, *, remedy_type=None, amount=None, tokens=None, token=None,
                  human_confirm_file=DEFAULT_CONFIRM_FILE, gmail_search=None):
    """Did the remedy this case actually won ACTUALLY land? Any remedy type.

    Returns the same shape refund_landed() returns, plus "remedy_type".

        refund       -> money posted (the original M28 logic, unchanged)
        store_credit -> a credit for the amount, issued WITH a credit code/card/certificate
        replacement  -> the parcel DELIVERED per the carrier, matched to a tracking number
        repair       -> human confirmation only: unit back AND verified working

    FAILS CLOSED on every unknown. No remedy type, an ambiguous one, a money remedy with no
    amount, a shipped remedy with no tracking identifier — each of these returns
    landed=False with a `gap`, never a guess.
    """
    rt = normalize_remedy(remedy_type) or remedy_type_from_case(case)
    _, ident = _case_query(case)
    if rt is None:
        return {"landed": False, "confidence": "none", "case": ident, "amounts": [],
                "remedy_type": None, "since": _to_date(since).isoformat(),
                "evidence": [{"source": "gap",
                              "detail": ("cannot check: no unambiguous remedy type. Set `%s` "
                                         "on the issue to one of %s. An absent or ranked "
                                         "value is not guessed — the case stays open."
                                         % (REMEDY_TYPE_PROP, "/".join(REMEDY_TYPES))),
                              "missing": [REMEDY_TYPE_PROP]}]}
    if rt in MONEY_REMEDIES:
        if not amount or (isinstance(amount, (list, tuple)) and not list(amount)):
            return {"landed": False, "confidence": "none", "case": ident, "amounts": [],
                    "remedy_type": rt, "since": _to_date(since).isoformat(),
                    "evidence": [{"source": "gap",
                                  "detail": "cannot check: %s is a money remedy and no "
                                            "amount was supplied" % rt,
                                  "missing": ["amount"]}]}
        if rt == "refund":
            r = refund_landed(case, amount, since, token=token,
                              human_confirm_file=human_confirm_file,
                              gmail_search=gmail_search)
            r["remedy_type"] = "refund"
            return r
        # store_credit: the amount must be named AND a credit artifact handed over.
        r = _non_money_landed(case, "store_credit", since, tokens=(tokens or []) or
                              ["$%.2f" % a for a in
                               [_norm_amount(x) for x in
                                (amount if isinstance(amount, (list, tuple, set))
                                 else [amount])] if a is not None],
                              token=token, human_confirm_file=human_confirm_file,
                              gmail_search=gmail_search)
        r["amounts"] = ["$%.2f" % a for a in
                        [_norm_amount(x) for x in
                         (amount if isinstance(amount, (list, tuple, set)) else [amount])]
                        if a is not None]
        return r
    return _non_money_landed(case, rt, since, tokens=tokens, token=token,
                             human_confirm_file=human_confirm_file,
                             gmail_search=gmail_search)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def refund_landed(case, amount, since, *, token=None,
                  human_confirm_file=DEFAULT_CONFIRM_FILE, gmail_search=None):
    """Verify the refund/credit ACTUALLY posted before a case may CLOSE.

    Returns {"landed": bool, "evidence": [dict...], "confidence": str}.
    landed=True only if EVERY expected amount is confirmed by a human-confirm log
    entry or a real credit-back email dated at/after the resolution date.
    """
    # Normalize amounts -> set of cents-floats.
    raw = amount if isinstance(amount, (list, tuple, set)) else [amount]
    amounts = []
    for a in raw:
        n = _norm_amount(a)
        if n is not None and n not in amounts:
            amounts.append(n)
    if not amounts:
        return {"landed": False, "confidence": "none",
                "evidence": [{"source": "error", "detail": "no valid amount given"}]}

    since_d = _to_date(since)
    query, ident = _case_query(case)
    evidence = []
    confirmed = {}          # amount -> best confidence for that amount
    per_amount_conf = {}    # amount -> "high"/"medium"/"low"

    # 1) Human confirmation (highest confidence).
    human = _human_confirmations(ident, amounts, since_d, human_confirm_file)
    for amt, ev in human.items():
        evidence.append(ev)
        confirmed[amt] = True
        per_amount_conf[amt] = "high"

    # 2) Email confirmation for amounts not already human-confirmed.
    remaining = [a for a in amounts if a not in confirmed]
    if remaining:
        search = gmail_search or _default_gmail_search
        try:
            msgs = search(query, since_d, remaining, token=token) or []
        except TypeError:
            # injected test doubles may not accept token=
            msgs = search(query, since_d, remaining) or []
        except Exception as exc:
            evidence.append({"source": "gmail-error", "detail": str(exc)[:180]})
            msgs = []
        for msg in msgs:
            body = _strip_quotes(msg.get("body") or "") + "\n" + (msg.get("subject") or "")
            conf = _confidence_for_sender(msg.get("from", ""))
            for amt in remaining:
                if amt in confirmed:
                    continue
                ok, snippet = _amount_confirmed_in_text(amt, body)
                if not ok:
                    continue
                base = {
                    "amount": amt,
                    "from": msg.get("from", ""),
                    "subject": (msg.get("subject") or "")[:120],
                    "date": msg.get("date", ""),
                    "gmail_id": msg.get("id", ""),
                    "snippet": (snippet or "")[:220],
                }
                if conf == "medium":
                    # A financial-institution / card-issuer notice about the account.
                    confirmed[amt] = True
                    per_amount_conf[amt] = conf
                    evidence.append(dict(base, source="email", confidence="medium"))
                else:
                    # A non-financial sender (typically the vendor being pursued)
                    # ASSERTING it credited the money is a promise, not real-world
                    # confirmation — the exact trap M28 exists to reject. Record it,
                    # but it does NOT establish landing.
                    evidence.append(dict(base, source="email-weak", confidence="low",
                                         note="vendor/unknown-sender claim — not a "
                                              "bank/issuer confirmation; does not "
                                              "prove the money landed"))

    missing = [a for a in amounts if a not in confirmed]
    landed = not missing

    if missing:
        evidence.append({
            "source": "gap",
            "detail": "no confirmation that %s posted at/after %s"
                      % (", ".join("$%.2f" % a for a in missing), since_d.isoformat()),
            "missing": ["$%.2f" % a for a in missing],
        })

    # Overall confidence = weakest link across confirmed amounts (or none if any gap).
    if not landed:
        confidence = "none" if not confirmed else "low"
    else:
        order = {"low": 0, "medium": 1, "high": 2}
        confidence = min((per_amount_conf[a] for a in amounts),
                         key=lambda c: order[c])

    return {"landed": landed, "confidence": confidence, "evidence": evidence,
            "amounts": ["$%.2f" % a for a in amounts],
            "since": since_d.isoformat(), "case": ident}


# --------------------------------------------------------------------------- #
# CLOSE gate — REPORT-ONLY hook (not wired into case_tick.py by this module)
# --------------------------------------------------------------------------- #
def close_gate(case, amount, since, resolution_verdict, *, remedy_type=None, tokens=None,
               **kw):
    """The gate that guards the move to CLOSE. Two independent conditions BOTH
    required:

        (A) M19 says the reply granted the remedy   -> resolution_verdict["resolved"]
        (B) M28/M44 says the remedy actually landed  -> remedy_landed(...)["landed"]

    A case may CLOSE only when A AND B. A vendor's promise (A alone) is NOT enough.

    M44: (B) now covers every remedy the ladder can win, not only money. The remedy type
    comes from `MR Remedy Type` on the issue (or an explicit remedy_type=). When the
    property is absent the gate falls back to the money path ONLY if an amount was
    explicitly supplied — that is the historical M28 contract and the strictest of the four
    checks, so the fallback cannot loosen anything. With neither a remedy type nor an
    amount, the gate holds.

    Returns {"may_close": bool, "reason": str, "refund": <refund_landed dict>}.

    WIRING STATUS (M43, 2026-07-26): WIRED. `close_case.py` is the engine's only
    close path and this gate is inside it, before any write.

      HISTORY: until M43 this gate was called by nothing for a structural reason —
      nothing closed a case. case_tick.py refuses to by design ("Closed" is in its
      NEVER_WRITE_PHASES set), and multica_api.py had no call that set a status at
      all. There was no `if phase would become Closed` branch for the gate to guard.

      NOW: close_case.evaluate() computes (A) with resolution_check.is_resolved on
      the vendor's actual reply text — the caller does not get to assert it — and
      passes that verdict straight into close_gate() for (B). If may_close is False
      the function RETURNS a hold; the two writes (MR Phase=Closed, then
      status=done) are unreachable. A third condition, an explicit --confirm-close,
      is required on top because closing is RED-lane (SKILL.md §5). And a gate that
      cannot RUN (no reply text, no amount, mailbox unreachable) is also a hold —
      fail closed. close_case's self-test asserts the stubbed API received ZERO
      writes on every refusal path, so the ordering is proven, not asserted.

    case_tick.py still never auto-closes, and should not: closing stays a
    deliberate, confirmed action. This gate makes it an action that cannot be taken
    on a promise.

    The human/skill path can still run the gate alone as a command:
        python3 refund_landed.py --gate --case CASE-1 --amount 120.00 --amount 65.00 \
                                 --since 2026-06-13 --resolved
        exit 0 -> CLOSE permitted     exit 2 -> HOLD (do not close)
    """
    rt = normalize_remedy(remedy_type) or remedy_type_from_case(case)
    if rt is None and amount:
        rt = "refund"          # historical M28 contract; the strictest of the four checks
    r = remedy_landed(case, since, remedy_type=rt, amount=amount, tokens=tokens, **kw)
    resolved = bool((resolution_verdict or {}).get("resolved"))
    may_close = resolved and r["landed"]
    if may_close:
        reason = ("CLOSE permitted: remedy granted (M19) AND %s landed (M28/M44, "
                  "confidence=%s)." % (r.get("remedy_type") or "remedy", r["confidence"]))
    elif not resolved:
        reason = "HOLD: M19 says the remedy was not actually granted."
    else:
        reason = ("HOLD: M19 granted but there is NO landed confirmation for the %s — %s. "
                  "A promise is not a delivered remedy." %
                  (r.get("remedy_type") or "remedy",
                   next((e.get("detail") for e in r["evidence"]
                         if e.get("source") == "gap"), "remedy not confirmed")))
    return {"may_close": may_close, "reason": reason, "refund": r}


# --------------------------------------------------------------------------- #
# Self-tests + live CASE-1 check
# --------------------------------------------------------------------------- #
def _fake_search(messages):
    """Build an injectable gmail_search that ignores the query and returns messages."""
    def _search(query, since_d, amounts, token=None):
        return messages
    return _search


def _selftests():
    ok = True
    cases = []

    # 1) Synthetic bank email: refund posted -> landed True.
    posted = [{
        "id": "synthetic1", "from": "alerts@stridebank.com",
        "subject": "Credit posted", "date": "Sat, 02 Aug 2026 10:00:00 -0500",
        "body": "Good news. Your refund of $129.99 has posted to your account "
                "ending 4321 and is now available.",
    }]
    r = refund_landed({"query": "from:stridebank.com", "identifier": "TEST-1"},
                      129.99, "2026-07-20", gmail_search=_fake_search(posted))
    cases.append(("synthetic refund posted -> landed", r["landed"] is True, r))

    # 2) Synthetic charge dodge (money OUT, not a refund) -> landed False.
    dodge = [{
        "id": "synthetic2", "from": "alerts@stridebank.com",
        "subject": "Transaction posted", "date": "Sat, 02 Aug 2026 10:00:00 -0500",
        "body": "The transaction posted: Jun 13 $120.00 HIGHLEVEL AGENCY SUB was "
                "charged to your card. The card is active.",
    }]
    r = refund_landed({"query": "x", "identifier": "TEST-2"},
                      120.00, "2026-06-10", gmail_search=_fake_search(dodge))
    cases.append(("charge-dodge -> NOT landed", r["landed"] is False, r))

    # 3) Two-credit case, only one posts -> landed False (all-or-nothing).
    partial = [{
        "id": "synthetic3", "from": "alerts@stridebank.com",
        "subject": "Credit", "date": "Sat, 02 Aug 2026 10:00:00 -0500",
        "body": "A credit of $120.00 has been posted to your account.",
    }]
    r = refund_landed({"query": "x", "identifier": "TEST-3"},
                      [120.00, 65.00], "2026-07-20", gmail_search=_fake_search(partial))
    cases.append(("partial (1 of 2 credits) -> NOT landed",
                  r["landed"] is False and r["confidence"] == "low", r))

    # 4) Both credits post -> landed True.
    both = [{
        "id": "synthetic4", "from": "alerts@stridebank.com", "subject": "Credits",
        "date": "Sat, 02 Aug 2026 10:00:00 -0500",
        "body": "A credit of $120.00 has been posted, and a refund of $65.00 was "
                "credited to your account.",
    }]
    r = refund_landed({"query": "x", "identifier": "TEST-4"},
                      [120.00, 65.00], "2026-07-20", gmail_search=_fake_search(both))
    cases.append(("both credits post -> landed", r["landed"] is True, r))

    # 4b) Vendor's OWN claim it credited you -> NOT landed (promise, not proof).
    vendor_claim = [{
        "id": "synthetic4b", "from": "Support Rep <support@gohighlevel.com>",
        "subject": "Re: GHL Refund", "date": "Fri, 17 Jul 2026 11:48:44 -0400",
        "body": "My records show a $120.00 refund credited to that account before "
                "it was closed.",
    }]
    r = refund_landed({"query": "x", "identifier": "TEST-4b"},
                      120.00, "2026-06-13", gmail_search=_fake_search(vendor_claim))
    cases.append(("vendor self-claim -> NOT landed", r["landed"] is False, r))

    # 4c) 'refunds MISSING for $X' (negation) -> NOT landed even from the bank.
    missing = [{
        "id": "synthetic4c", "from": "interact@stridebank.com",
        "subject": "Complaint Resolution", "date": "Fri, 24 Jul 2026 19:23:21 +0000",
        "body": "We reviewed the two refunds missing on your account for $120.00 "
                "and $65.00. The account has been active since 2024.",
    }]
    r = refund_landed({"query": "x", "identifier": "TEST-4c"},
                      [120.00, 65.00], "2026-06-13", gmail_search=_fake_search(missing))
    cases.append(("bank says refunds MISSING -> NOT landed", r["landed"] is False, r))

    # 4d) Quoted (">>") echo of a credited claim -> ignored -> NOT landed.
    quoted = [{
        "id": "synthetic4d", "from": "interact@stridebank.com", "subject": "Re: trace",
        "date": "Fri, 24 Jul 2026 19:23:21 +0000",
        "body": "We have no record of these credits.\n>> $120.00 refund credited to "
                "that account before it was closed\n>> $65.00 refund credited too",
    }]
    r = refund_landed({"query": "x", "identifier": "TEST-4d"},
                      [120.00, 65.00], "2026-06-13", gmail_search=_fake_search(quoted))
    cases.append(("quoted-echo of credit -> NOT landed", r["landed"] is False, r))

    # 4e) Human confirmation log -> landed True at HIGH confidence.
    import tempfile
    hc = os.path.join(tempfile.gettempdir(), "rl_selftest_confirm.json")
    with open(hc, "w") as fh:
        json.dump({"TEST-4e": [
            {"amount": 129.99, "date": "2026-08-02", "note": "saw it", "by": "self-test"}]}, fh)
    r = refund_landed({"query": "x", "identifier": "TEST-4e"}, 129.99, "2026-07-20",
                      human_confirm_file=hc, gmail_search=_fake_search([]))
    cases.append(("human-confirm log -> landed (high)",
                  r["landed"] is True and r["confidence"] == "high", r))
    try:
        os.remove(hc)
    except OSError:
        pass

    # 5) close_gate: M19 resolved but M28 not landed -> may_close False.
    g = close_gate({"query": "x", "identifier": "TEST-5"}, 129.99, "2026-07-20",
                   {"resolved": True}, gmail_search=_fake_search([]))
    cases.append(("close_gate: promise w/o landing -> may_close False",
                  g["may_close"] is False, g))

    # 6) close_gate: resolved AND landed -> may_close True.
    g = close_gate({"query": "x", "identifier": "TEST-6"}, 129.99, "2026-07-20",
                   {"resolved": True}, gmail_search=_fake_search(posted))
    cases.append(("close_gate: granted AND landed -> may_close True",
                  g["may_close"] is True, g))

    # ---------------------------------------------------------------------------------
    # M44 — the other three remedies. Same thesis, non-money proof.
    # ---------------------------------------------------------------------------------
    import tempfile as _tf

    def _hc(payload):
        p = os.path.join(_tf.gettempdir(), "rl_m44_confirm.json")
        with open(p, "w") as fh:
            json.dump(payload, fh)
        return p

    # 7) remedy type must be UNAMBIGUOUS. A ranked intake answer is not a decision.
    cases.append(("ranked intake answer is ambiguous -> no remedy type",
                  normalize_remedy("refund / replacement / repair / credit (ranked)") is None,
                  {"normalize": None}))
    cases.append(("'StoreCredit' select option normalizes",
                  normalize_remedy("StoreCredit") == "store_credit", {}))
    cases.append(("'Exchange' reads as a replacement",
                  normalize_remedy("Exchange") == "replacement", {}))
    cases.append(("remedy type is read off the board property",
                  remedy_type_from_case({"mr": {REMEDY_TYPE_PROP: "Repair"}}) == "repair", {}))

    # 8) No remedy type at all -> cannot check -> NOT landed.
    r = remedy_landed({"identifier": "TEST-8", "query": "x"}, "2026-07-20",
                      gmail_search=_fake_search([]))
    cases.append(("no MR Remedy Type -> NOT landed (fails closed)",
                  r["landed"] is False and r["remedy_type"] is None, r))

    # 9) REPLACEMENT: the carrier says DELIVERED, matched to the tracking number -> landed.
    delivered = [{
        "id": "m44a", "from": "auto-notify@ups.com", "subject": "Your package was delivered",
        "date": "Sun, 03 Aug 2026 14:02:00 -0500",
        "body": "Your replacement shipment 1Z999AA10123456784 was delivered and left at "
                "the front door.",
    }]
    r = remedy_landed({"identifier": "TEST-9", "query": "x",
                       "mr": {REMEDY_TYPE_PROP: "Replacement"}}, "2026-07-20",
                      tokens=["1Z999AA10123456784"], gmail_search=_fake_search(delivered))
    cases.append(("replacement delivered by carrier -> landed (medium)",
                  r["landed"] is True and r["confidence"] == "medium", r))

    # 9b) 'has shipped' is NOT 'delivered'.
    shipped = [{
        "id": "m44b", "from": "auto-notify@ups.com", "subject": "Shipment created",
        "date": "Fri, 01 Aug 2026 09:00:00 -0500",
        "body": "Your replacement 1Z999AA10123456784 has shipped and is in transit; "
                "estimated delivery Monday.",
    }]
    r = remedy_landed({"identifier": "TEST-9b", "mr": {REMEDY_TYPE_PROP: "Replacement"}},
                      "2026-07-20", tokens=["1Z999AA10123456784"],
                      gmail_search=_fake_search(shipped))
    cases.append(("replacement only SHIPPED -> NOT landed", r["landed"] is False, r))

    # 9c) The VENDOR saying it delivered is not proof — same trap as the money path.
    vendor_deliv = [{
        "id": "m44c", "from": "support@vendorbeingpursued.example",
        "subject": "Re: replacement", "date": "Sun, 03 Aug 2026 14:02:00 -0500",
        "body": "Our system shows the replacement 1Z999AA10123456784 was delivered to you.",
    }]
    r = remedy_landed({"identifier": "TEST-9c", "mr": {REMEDY_TYPE_PROP: "Replacement"}},
                      "2026-07-20", tokens=["1Z999AA10123456784"],
                      gmail_search=_fake_search(vendor_deliv))
    cases.append(("vendor claims delivery -> NOT landed", r["landed"] is False, r))

    # 9d) No tracking identifier -> nothing ties an arrival to this remedy -> hold.
    r = remedy_landed({"identifier": "TEST-9d", "mr": {REMEDY_TYPE_PROP: "Replacement"}},
                      "2026-07-20", gmail_search=_fake_search(delivered))
    cases.append(("replacement with no tracking anchor -> NOT landed",
                  r["landed"] is False, r))

    # 9e) Human confirmation closes a replacement at HIGH confidence.
    p = _hc({"TEST-9e": [{"kind": "replacement", "date": "2026-08-03", "confirmed": True,
                          "token": "1Z999AA10123456784", "note": "in my hands"}]})
    r = remedy_landed({"identifier": "TEST-9e", "mr": {REMEDY_TYPE_PROP: "Replacement"}},
                      "2026-07-20", tokens=["1Z999AA10123456784"],
                      human_confirm_file=p, gmail_search=_fake_search([]))
    cases.append(("replacement human-confirm -> landed (high)",
                  r["landed"] is True and r["confidence"] == "high", r))

    # 10) REPAIR: even a carrier delivering the unit back does NOT close it.
    back = [{
        "id": "m44d", "from": "auto-notify@fedex.com", "subject": "Delivered",
        "date": "Sun, 09 Aug 2026 11:00:00 -0500",
        "body": "Repair RMA-88213 was delivered and signed for.",
    }]
    r = remedy_landed({"identifier": "TEST-10", "mr": {REMEDY_TYPE_PROP: "Repair"}},
                      "2026-07-20", tokens=["RMA-88213"], gmail_search=_fake_search(back))
    cases.append(("repair unit delivered back but unverified -> NOT landed",
                  r["landed"] is False, r))

    # 10b) ... and the owner's confirmation that it WORKS does.
    p = _hc({"TEST-10b": [{"kind": "repair", "date": "2026-08-09", "confirmed": True,
                           "token": "RMA-88213", "note": "powers on, tested 20 min"}]})
    r = remedy_landed({"identifier": "TEST-10b", "mr": {REMEDY_TYPE_PROP: "Repair"}},
                      "2026-07-20", tokens=["RMA-88213"], human_confirm_file=p,
                      gmail_search=_fake_search(back))
    cases.append(("repair confirmed working by owner -> landed (high)",
                  r["landed"] is True and r["confidence"] == "high", r))

    # 10c) An UNconfirmed human entry is a note, not an attestation.
    p = _hc({"TEST-10c": [{"kind": "repair", "date": "2026-08-09",
                           "token": "RMA-88213", "note": "box arrived, not opened yet"}]})
    r = remedy_landed({"identifier": "TEST-10c", "mr": {REMEDY_TYPE_PROP: "Repair"}},
                      "2026-07-20", tokens=["RMA-88213"], human_confirm_file=p,
                      gmail_search=_fake_search([]))
    cases.append(("human entry without confirmed:true -> NOT landed",
                  r["landed"] is False, r))

    # 11) STORE CREDIT: an issuance PROMISE with no code is not the credit.
    promise = [{
        "id": "m44e", "from": "care@vendorbeingpursued.example", "subject": "Your credit",
        "date": "Mon, 03 Aug 2026 10:00:00 -0500",
        "body": "A store credit of $75.00 will be issued to your account shortly.",
    }]
    r = remedy_landed({"identifier": "TEST-11", "mr": {REMEDY_TYPE_PROP: "StoreCredit"}},
                      "2026-07-20", amount=75.00, gmail_search=_fake_search(promise))
    cases.append(("store credit promised, no code -> NOT landed", r["landed"] is False, r))

    # 11b) A credit with an actual code/certificate IS the deliverable -> landed (medium).
    issued = [{
        "id": "m44f", "from": "care@vendorbeingpursued.example", "subject": "Your credit",
        "date": "Mon, 03 Aug 2026 10:00:00 -0500",
        "body": "A store credit of $75.00 has been added to your account. "
                "Gift certificate code: SC-4KQ2-99XB1. It is available now.",
    }]
    r = remedy_landed({"identifier": "TEST-11b", "mr": {REMEDY_TYPE_PROP: "StoreCredit"}},
                      "2026-07-20", amount=75.00, gmail_search=_fake_search(issued))
    cases.append(("store credit issued WITH a code -> landed (medium)",
                  r["landed"] is True and r["confidence"] == "medium", r))

    # 11c) A money remedy with no amount cannot be checked.
    r = remedy_landed({"identifier": "TEST-11c", "mr": {REMEDY_TYPE_PROP: "StoreCredit"}},
                      "2026-07-20", gmail_search=_fake_search(issued))
    cases.append(("store credit with no amount -> NOT landed", r["landed"] is False, r))

    # 12) close_gate on a non-money remedy: granted + delivered -> may_close.
    g = close_gate({"identifier": "TEST-12", "mr": {REMEDY_TYPE_PROP: "Replacement"}},
                   None, "2026-07-20", {"resolved": True},
                   tokens=["1Z999AA10123456784"], gmail_search=_fake_search(delivered))
    cases.append(("close_gate replacement: granted AND delivered -> may_close",
                  g["may_close"] is True, g))

    # 12b) ... and granted with the parcel still moving -> HOLD.
    g = close_gate({"identifier": "TEST-12b", "mr": {REMEDY_TYPE_PROP: "Replacement"}},
                   None, "2026-07-20", {"resolved": True},
                   tokens=["1Z999AA10123456784"], gmail_search=_fake_search(shipped))
    cases.append(("close_gate replacement: still in transit -> HOLD",
                  g["may_close"] is False, g))

    try:
        os.remove(os.path.join(_tf.gettempdir(), "rl_m44_confirm.json"))
    except OSError:
        pass

    for name, passed, obj in cases:
        ok = ok and passed
        print("[%s] %s" % ("PASS" if passed else "FAIL", name))
        print("       %s" % json.dumps(obj, ensure_ascii=False)[:400])
    print("=== %s ===" % ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


def _live_check(argv):
    """Live Gmail check for one case. Every value is supplied on the command line —
    no case, amount or mailbox scope is baked into this file, because the skill ships
    to strangers and a hardcoded example would carry one user's real money into
    everyone else's copy.

        python3 refund_landed.py --live-check --case CASE-1 --amount 120.00 \\
                --amount 65.00 --since 2026-06-13 --query "from:examplebank.com"

    Exit 0 when the money is confirmed landed, 1 when it is not (which is the normal,
    informative answer for a case that is still open)."""
    import argparse
    ap = argparse.ArgumentParser(description="M28 live refund-landed check")
    ap.add_argument("--live-check", action="store_true")
    ap.add_argument("--case", required=True, help="case identifier")
    ap.add_argument("--amount", action="append", required=True,
                    help="expected amount; repeat for multiple credits")
    ap.add_argument("--since", required=True, help="resolution date, YYYY-MM-DD")
    ap.add_argument("--query", default="", help="Gmail scope for this case")
    a = ap.parse_args(argv)
    r = refund_landed({"query": a.query, "identifier": a.case}, a.amount, a.since)
    print("%s live refund_landed:" % a.case)
    print(json.dumps(r, indent=2, ensure_ascii=False))
    print("\nlanded=%s (confidence %s)" % (r["landed"], r["confidence"]))
    return 0 if r["landed"] else 1


def _gate_cli(argv):
    """M42: the CLOSE gate as a command, for the human close path that exists today.

    Exit 0 = CLOSE permitted. Exit 2 = HOLD. Exit 3 = the gate could not run — which is
    also a HOLD, because a gate that fails open is not a gate.
    """
    import argparse
    ap = argparse.ArgumentParser(description="M28 CLOSE gate — may this case close?")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--case", required=True, help="case identifier, e.g. CASE-1")
    ap.add_argument("--amount", action="append", default=[],
                    help="expected amount; repeat for multiple credits (ALL must land). "
                         "Required for refund / store_credit.")
    ap.add_argument("--since", required=True, help="resolution date, YYYY-MM-DD")
    ap.add_argument("--query", default="", help="optional Gmail scope for this case")
    ap.add_argument("--remedy-type", default=None,
                    help="refund | replacement | repair | store_credit. Omit to read `%s` "
                         "off the board. Never defaulted." % REMEDY_TYPE_PROP)
    ap.add_argument("--token", action="append", default=[], dest="tokens",
                    help="tracking number / RMA / credit code; repeat. Required for a "
                         "replacement (nothing else ties an arrival to THIS remedy).")
    ap.add_argument("--from-board", action="store_true",
                    help="read the case off the live board so `%s` is the REAL property "
                         "value and not a flag (read-only)." % REMEDY_TYPE_PROP)
    ap.add_argument("--resolved", action="store_true",
                    help="M19 says the vendor actually GRANTED the remedy. Without this the "
                         "gate holds — a case cannot close on an ungranted remedy either.")
    a = ap.parse_args(argv)

    case = {"identifier": a.case, "query": a.query}
    if a.from_board:
        try:
            import multica_api as mc
            hit = next((it for it in mc.list_issues()
                        if it.get("identifier") == a.case or it.get("id") == a.case), None)
            if hit is None:
                raise LookupError("no issue with identifier/id %r" % a.case)
            hit.setdefault("query", a.query)
            case = hit
        except Exception as exc:
            print("HOLD — could not read %s off the board (%s: %s). A gate that cannot read "
                  "the case must not let it close." % (a.case, type(exc).__name__, exc))
            return 3
    try:
        g = close_gate(case, a.amount, a.since, {"resolved": a.resolved},
                       remedy_type=a.remedy_type, tokens=a.tokens)
    except Exception as exc:
        print("HOLD — the close gate could not run (%s: %s). A gate that cannot check must "
              "not let a case close." % (type(exc).__name__, exc))
        return 3

    print("%s" % g["reason"])
    r = g["refund"]
    print("  remedy    : %s" % (r.get("remedy_type") or "(UNSET — gate cannot run)"))
    print("  amounts   : %s" % (", ".join(r.get("amounts") or []) or "(n/a)"))
    if r.get("anchors"):
        print("  anchors   : %s" % ", ".join(r["anchors"]))
    print("  since     : %s" % r["since"])
    print("  landed    : %s (confidence %s)" % (r["landed"], r["confidence"]))
    for e in r["evidence"]:
        print("  evidence  : [%s] %s" % (e.get("source"),
                                         e.get("detail") or e.get("snippet") or e.get("note", "")))
    if g["may_close"]:
        return 0
    print("\n" + _human_confirm_howto())
    return 2


if __name__ == "__main__":
    if "--gate" in sys.argv:
        sys.exit(_gate_cli(sys.argv[1:]))
    if "--live-check" in sys.argv:
        sys.exit(_live_check(sys.argv[1:]))
    sys.exit(_selftests())
