#!/usr/bin/env python3
"""resolution_check.py — M19 fake-resolution / brush-off detector.

Vendors dodge: they reply in a way that LOOKS like a resolution but does not
actually deliver the demanded remedy. This module decides whether a vendor reply
truly grants the remedy that was demanded (refund / replacement / repair / a
trace of specific credits) or is merely a brush-off, a non-answer, or a
confirmation of the WRONG thing.

Public API
----------
    is_resolved(demanded_remedy: str, vendor_reply_text: str,
                use_llm: bool = False) -> dict
        -> {"resolved": bool, "reason": str}

Design
------
Rule-based CORE (stdlib only, deterministic — this is the authority):
  1. Classify the demanded remedy (refund/credit, replacement, repair, trace).
  2. Look for AFFIRMATIVE grant language for THAT remedy ("a refund of $X has
     been issued", "replacement has shipped", ...). Future/conditional/hedged
     verbs ("we will look into", "will be processed") do NOT count as granted.
  3. Detect deflections / brush-offs ("escalated to our internal team", "under
     review", "get back to you", "thank you for your patience", ...).
  4. Detect WRONG-CONFIRMATION mismatch — the classic dodge where a demand to
     trace missing REFUND CREDITS is answered by confirming the original
     CHARGES/debits posted (different money moving the other direction).

  resolved=True only when a clear grant is found AND there is no wrong-thing
  mismatch. Everything else -> resolved=False with a reason naming the flaw.

Optional LLM judge (use_llm=True): a NanoGPT/GLM second opinion. It is combined
CONSERVATIVELY — resolved stays True only if BOTH the rules and the LLM agree the
remedy was delivered. The LLM can never upgrade a rule-based False to True, so the
critical MER-1 case cannot regress if the model is flaky/unavailable.

Run directly to execute the three built-in regression cases:
    python3 resolution_check.py            # rule-based
    python3 resolution_check.py --llm      # also exercise the LLM judge
"""
import json
import os
import re
import sys

# --------------------------------------------------------------------------- #
# Remedy vocabulary
# --------------------------------------------------------------------------- #
REMEDY_WORDS = {
    "refund": ("refund", "reimburse", "reimbursement", "money back",
               "credit back", "refunded", "return the money", "chargeback"),
    "replacement": ("replacement", "replace", "send another", "new unit",
                    "reship", "re-ship", "resend", "re-send", "ship a new"),
    "repair": ("repair", "fix", "fixed", "servicing", "service the"),
}
# A "trace" demand (find where specific credits landed) is a refund-family demand.
TRACE_WORDS = ("trace", "locate", "where the", "posted", "returned or held",
               "credit posted")

# Affirmative grant patterns, per remedy. Each is a compiled regex over lowercase
# text. They require a PERFECT/PAST outcome, not a promise.
# A "non-sentence-breaking" character: anything but a period, EXCEPT a period that is part
# of a decimal amount ("$75.00"). Plain `[^.]` treats the dot in a price as a full stop and
# silently truncates the window before the verb ever arrives.
_NB = r"(?:[^.]|\.(?=\d))"

GRANTED_VERB = r"(?:has|have|is|are|was|were|been|we've|we have)\b[^.]{0,25}?" \
               r"(?:issued|processed|completed|refunded|credited|posted|sent|" \
               r"returned|reimbursed|approved and (?:sent|issued)|made)"
GRANT_PATTERNS = {
    "refund": [
        re.compile(r"(refund|reimburs\w*|credit)\b[^.]{0,40}?" + GRANTED_VERB),
        re.compile(GRANTED_VERB + r"[^.]{0,40}?(refund|reimburs\w*|credit)"),
        re.compile(r"(you have been|you've been|has been) (refunded|reimbursed|credited)"),
        re.compile(r"(issued|processed|approved|completed) (a|your|the|this|full) (refund|reimbursement)"),
        re.compile(r"refund of \$?[\d,]+(?:\.\d{2})? (?:has|is|was) (?:been )?(?:issued|processed|completed|sent|credited)"),
        # M44 — a STORE CREDIT grant. Two things the patterns above miss:
        #   1. the instrument is named ("store credit", "gift card"), not "refund";
        #   2. `[^.]` stops at the decimal point inside "$75.00", so "a store credit of
        #      $75.00 has been issued" never reached its verb. _NB below lets a period
        #      through only when a digit follows it, so a money amount does not read as
        #      the end of the sentence while a real sentence boundary still does.
        re.compile(r"(store credit|gift card|gift certificate|merchandise credit|"
                   r"account credit|voucher|e-?gift)" + _NB + r"{0,40}?" + GRANTED_VERB),
        re.compile(r"(store credit|gift card|gift certificate|merchandise credit|"
                   r"account credit|voucher|e-?gift)" + _NB + r"{0,40}?"
                   r"(?:has|have|was|were)\s+(?:been\s+)?(?:added|applied)"),
    ],
    "replacement": [
        re.compile(r"(replacement|replace\w*|new unit)\b[^.]{0,40}?"
                   r"(shipped|dispatched|sent|mailed|on its way|is being sent|has gone out)"),
        re.compile(r"(shipped|dispatched|sent|mailed) (a|your|the|out a) (replacement|new (unit|item|one))"),
    ],
    "repair": [
        re.compile(r"(repair|fixed|serviced|repaired)\b[^.]{0,40}?"
                   r"(completed|done|has been|is complete|shipped back|returned to you)"),
        re.compile(r"(completed|finished) (the|your) (repair|service)"),
    ],
}

# Deflection / brush-off markers. Presence of any (without a grant) => not resolved.
DEFLECTIONS = [
    r"escalat",                       # escalated / escalating
    r"internal team", r"internally", r"to our team", r"our team will",
    r"forwarded (?:this|it|your)", r"passed (?:this|it|your)",
    r"looking into", r"look into", r"investigat", r"under review",
    r"reviewing your", r"being reviewed",
    r"get back to you", r"be in touch", r"will (?:contact|reach out|update)",
    r"thank you for your patience", r"appreciate your patience",
    r"we apologize for", r"we're sorry for",
    r"ticket (?:has been|was) (?:created|opened|logged)",
    r"working on (?:it|this|your)", r"higher priority",
    r"within \d+\s*(?:-\s*\d+\s*)?(?:business )?(?:days|hours)",  # vague timeline
    r"we value your", r"kindly (?:wait|bear)",
]

# Words that signal the reply is talking about the ORIGINAL charges/debits
# (money out) rather than the demanded refund CREDIT (money back).
CHARGE_TERMS = ("charge", "charged", "debit", "purchase", "subscription",
                "sub ", " sub;", "trial", "billed", "payment posted", "not closed",
                "is not closed", "card is not closed")
CHARGE_CONFIRM = ("post", "posted", "confirm", "appears", "shows", "reflect",
                  "not closed", "is active", "went through")


def _classify_demand(d):
    """Return the set of remedy families demanded, plus a 'wants_credit' flag."""
    demanded = set()
    for remedy, words in REMEDY_WORDS.items():
        if any(w in d for w in words):
            demanded.add(remedy)
    is_trace = any(w in d for w in TRACE_WORDS)
    # A trace of credits, or an explicit 'credit', is a refund-family demand.
    wants_credit = ("refund" in demanded) or ("credit" in d) or is_trace
    if wants_credit:
        demanded.add("refund")
    return demanded, wants_credit, is_trace


def _first_grant(demanded, r):
    for remedy in demanded:
        for pat in GRANT_PATTERNS.get(remedy, []):
            if pat.search(r):
                return remedy
    return None


def _deflection_hit(r):
    for pat in DEFLECTIONS:
        if re.search(pat, r):
            return pat
    return None


def _wrong_confirmation(wants_credit, r, granted):
    """Detect the 'confirmed the charges, not the refund credits' dodge."""
    if not wants_credit or granted:
        return False
    mentions_charge = any(t in r for t in CHARGE_TERMS)
    confirms = any(c in r for c in CHARGE_CONFIRM)
    # Reply must NOT actually contain refund-grant language for this to be a dodge.
    talks_refund_credit = bool(re.search(r"refund|reimburs|credit(?:ed)?\b", r))
    return mentions_charge and confirms and not talks_refund_credit


def rule_based(demanded_remedy, vendor_reply_text):
    d = (demanded_remedy or "").lower()
    r = (vendor_reply_text or "").lower()

    if not r.strip():
        return {"resolved": False, "reason": "Empty vendor reply — nothing delivered."}

    demanded, wants_credit, is_trace = _classify_demand(d)
    if not demanded:
        # Unknown demand: fall back to generic refund-family checks.
        demanded, wants_credit = {"refund"}, True

    granted = _first_grant(demanded, r)
    mismatch = _wrong_confirmation(wants_credit, r, granted)
    deflect = _deflection_hit(r)

    if granted and not mismatch:
        return {"resolved": True,
                "reason": "Reply affirmatively delivers the demanded %s "
                          "(grant language found)." % granted}

    if mismatch:
        return {"resolved": False,
                "reason": ("DODGE: reply confirms the ORIGINAL charges/debits "
                           "posted (money out), not the demanded refund CREDITS "
                           "(money back). Confirming the wrong direction of money "
                           "is not resolution.")}

    if deflect:
        return {"resolved": False,
                "reason": ("Brush-off: reply only defers/escalates (matched "
                           "'%s') without delivering the demanded remedy."
                           % deflect.strip("\\b"))}

    want = "trace of the refund credits" if is_trace else "/".join(sorted(demanded))
    return {"resolved": False,
            "reason": ("Reply does not deliver the demanded %s — no affirmative "
                       "grant language found." % want)}


# --------------------------------------------------------------------------- #
# Optional LLM judge (NanoGPT / GLM, OpenAI-compatible). Fail-safe.
# --------------------------------------------------------------------------- #
def _load_env_key():
    key = os.environ.get("NANOGPT_API_KEY")
    if key:
        return key
    for path in ("/opt/data/.env", os.path.join(os.path.dirname(__file__), ".env")):
        try:
            with open(path) as fh:
                for line in fh:
                    if line.startswith("NANOGPT_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except IOError:
            continue
    return None


def _key(name):
    v = os.environ.get(name)
    if v:
        return v
    for path in ("/opt/data/.env", os.path.join(os.path.dirname(__file__), ".env")):
        try:
            for line in open(path):
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except IOError:
            continue
    return None


def llm_judge(demanded_remedy, vendor_reply_text, timeout=30):
    """Return {'resolved': bool|None, 'reason': str}. Tries GLM then MiniMax-M3;
    strips reasoning-model <think> blocks; None if all providers fail."""
    import urllib.request
    sys_prompt = (
        "You are a strict consumer-protection auditor. Decide whether a vendor's "
        "reply ACTUALLY delivers the specific remedy the customer demanded. A "
        "brush-off, an 'escalated internally', a vague timeline, or confirming the "
        "WRONG thing (e.g. confirming original charges when a refund credit was "
        "demanded, or denying/closing a claim) is NOT resolution. Reply ONLY with "
        'compact JSON: {"resolved": true|false, "reason": "<short>"}.')
    user = "DEMANDED REMEDY:\n%s\n\nVENDOR REPLY:\n%s" % (demanded_remedy, vendor_reply_text[:6000])
    providers = []
    ng = _key("NANOGPT_API_KEY")
    if ng:
        providers.append(("https://nano-gpt.com/api/v1", "zai-org/glm-5.2", ng))
    mm = _key("MINIMAX_API_KEY")
    if mm:
        providers.append(("https://api.minimax.io/v1", "MiniMax-M3", mm))
    for base, model, key in providers:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": 1500,
        }).encode()
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions", data=payload,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            content = json.loads(urllib.request.urlopen(req, timeout=timeout).read())["choices"][0]["message"]["content"]
        except Exception:
            continue
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = re.sub(r"<think>.*$", "", content, flags=re.DOTALL).strip()
        m = re.search(r"\{.*\}", content, re.S)
        try:
            obj = json.loads(m.group(0) if m else content)
            return {"resolved": bool(obj.get("resolved")),
                    "reason": "LLM[%s]: %s" % (model, str(obj.get("reason", "")).strip())}
        except Exception:
            continue
    return {"resolved": None, "reason": "LLM judge unavailable"}


def is_resolved(demanded_remedy, vendor_reply_text, use_llm=False):
    """Rule-based authority, optionally hardened by a conservative LLM second
    opinion. resolved=True only if the rules say True AND (LLM off, LLM failed,
    or LLM also says True). The LLM can never flip a rule-based False to True."""
    verdict = rule_based(demanded_remedy, vendor_reply_text)
    if not use_llm:
        return verdict

    judged = llm_judge(demanded_remedy, vendor_reply_text)
    if not judged or judged.get("resolved") is None:
        verdict = dict(verdict)
        verdict["reason"] += " (%s)" % (judged["reason"] if judged
                                        else "LLM judge not configured")
        return verdict

    if verdict["resolved"] and not judged["resolved"]:
        # Conservative AND: disagreement toward NOT-resolved wins.
        return {"resolved": False,
                "reason": "Rules said resolved but LLM disagrees -> treating as "
                          "UNRESOLVED (conservative). " + judged["reason"]}
    verdict = dict(verdict)
    verdict["reason"] += " | " + judged["reason"]
    return verdict


# --------------------------------------------------------------------------- #
# Built-in regression cases
# --------------------------------------------------------------------------- #
CASES = [
    {
        "name": "Bank dodge (charges != refund credits)  [MUST be False]",
        "demand": "trace and confirm two missing REFUND CREDITS ($120.00, $65.00) "
                  "posted/returned/held",
        "reply": ("Thank you for contacting Stride Bank. We reviewed the account "
                  "on card ending 4321. The two transactions posted as follows: "
                  "Jun 13 $120.00 VENDOR AGENCY SUB; Jun 15 $65.00 VENDOR "
                  "TRIAL OVER. The card is not closed and is active. Please let us "
                  "know if we can help further."),
        "expect": False,
    },
    {
        "name": "Clear TRUE — refund issued",
        "demand": "issue a full refund of $120.00",
        "reply": "A full refund of $120.00 has been issued to your account and "
                 "should appear within 3-5 business days.",
        "expect": True,
    },
    {
        "name": "Clear brush-off — escalated internally",
        "demand": "issue a full refund of $120.00",
        "reply": "We have escalated this to our internal team.",
        "expect": False,
    },
]


def _run(use_llm):
    ok = True
    for c in CASES:
        res = is_resolved(c["demand"], c["reply"], use_llm=use_llm)
        passed = (res["resolved"] == c["expect"])
        ok = ok and passed
        print("[%s] %s" % ("PASS" if passed else "FAIL", c["name"]))
        print("       expected resolved=%s  got resolved=%s"
              % (c["expect"], res["resolved"]))
        print("       reason: %s\n" % res["reason"])
    print("=== %s ===" % ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run(use_llm=("--llm" in sys.argv)))
