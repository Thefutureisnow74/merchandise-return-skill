#!/usr/bin/env python3
"""refund_landed.py — M28 refund-landed verification before CLOSE.

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
    """The manual-confirm instructions, addressed to whoever owns the profile (M32)."""
    who = mer_config.legal_name()
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
  is a deliberate human action — no agent may write this file on its own.""" % (
        who, confirm_file, who, who)

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
def close_gate(case, amount, since, resolution_verdict, **kw):
    """The gate that guards the move to CLOSE. Two independent conditions BOTH
    required:

        (A) M19 says the reply granted the remedy   -> resolution_verdict["resolved"]
        (B) M28 says the money actually landed       -> refund_landed(...)["landed"]

    A case may CLOSE only when A AND B. A vendor's promise (A alone) is NOT enough.

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
    r = refund_landed(case, amount, since, **kw)
    resolved = bool((resolution_verdict or {}).get("resolved"))
    may_close = resolved and r["landed"]
    if may_close:
        reason = ("CLOSE permitted: remedy granted (M19) AND refund landed (M28, "
                  "confidence=%s)." % r["confidence"])
    elif not resolved:
        reason = "HOLD: M19 says the remedy was not actually granted."
    else:
        reason = ("HOLD: M19 granted but M28 has NO landed confirmation — %s. "
                  "A promise is not a posted refund." %
                  next((e.get("detail") for e in r["evidence"]
                        if e.get("source") == "gap"), "money not confirmed"))
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
    ap.add_argument("--amount", action="append", required=True,
                    help="expected amount; repeat for multiple credits (ALL must land)")
    ap.add_argument("--since", required=True, help="resolution date, YYYY-MM-DD")
    ap.add_argument("--query", default="", help="optional Gmail scope for this case")
    ap.add_argument("--resolved", action="store_true",
                    help="M19 says the vendor actually GRANTED the remedy. Without this the "
                         "gate holds — a case cannot close on an ungranted remedy either.")
    a = ap.parse_args(argv)

    case = {"identifier": a.case, "query": a.query}
    try:
        g = close_gate(case, a.amount, a.since, {"resolved": a.resolved})
    except Exception as exc:
        print("HOLD — the close gate could not run (%s: %s). A gate that cannot check must "
              "not let a case close." % (type(exc).__name__, exc))
        return 3

    print("%s" % g["reason"])
    r = g["refund"]
    print("  amounts   : %s" % ", ".join(r["amounts"]))
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
