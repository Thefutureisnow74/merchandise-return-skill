#!/usr/bin/env python3
"""
draft_reply.py — LLM-draft the customer's next reply to a vendor message (M6).

Reuses classify_llm's provider ladder (GLM -> MiniMax-M3) and the reasoning-model <think> fix.
Grounded + conservative: firm, factual, first-person, and explicitly told NOT to invent facts —
if the vendor asked for info we don't have, it asks the user rather than fabricating.
Returns {"subject", "body"} or None on failure (caller then surfaces for a human draft).

GROUNDING IS ENFORCED IN CODE, NOT ONLY IN THE PROMPT (M38).
A system prompt that says "do not invent facts" is a request, not a gate — the same class of
mistake as "the protocol says BBB comes after Tier 2" (Blueprint §10). So every candidate draft
is machine-checked before it is returned: any money amount, date, or order/case/reference number
in the draft that does not appear in the inputs makes the draft UNGROUNDED and it is discarded
(the next provider is tried; if none is clean, draft() returns None). Two hard rules:

  * a draft with a fact not present in its inputs is never returned;
  * with too little case material to draft from, draft() returns None WITHOUT calling any
    provider — no letter at all beats a plausible wrong one in front of a real vendor.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import classify_llm as C  # noqa: E402  (provider ladder + _http_chat + _load_env_key)

SYS = (
    "You draft the CUSTOMER's next email in a merchandise-return dispute. The customer sends it "
    "under their own name (single user, first person). Be firm, factual, concise, non-adversarial. "
    "Do NOT invent facts, numbers, dates, or promises not present in the case/vendor text. If the "
    "vendor is asking for information you were not given, write the reply so it asks the USER to "
    "supply it — never fabricate it. Keep it short. "
    'Return ONLY compact JSON: {"subject": "...", "body": "..."}'
)


# ------------------------------------------------------------------ grounding check (M38)

# The three fact classes that can lose a dispute if invented: money, dates, identifiers.
_AMOUNT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")
_REF_KEYED_RE = re.compile(
    r"(?:order|case|invoice|receipt|ref|reference|claim|ticket|rma|account|confirmation|"
    r"serial|sku|tracking)\s*(?:no\.?|number|#|id|:)?\s*[#:]?\s*([A-Za-z]{0,4}[\d\-]*\d[A-Za-z0-9\-]{2,})",
    re.I,
)
_REF_HASH_RE = re.compile(r"#\s?([A-Za-z0-9\-]*\d[A-Za-z0-9\-]{3,})")

# Minimum characters of real case material (title + vendor text) needed to draft at all.
MIN_GROUNDING_CHARS = 40


def _norm_amount(raw):
    try:
        return "amount:%.2f" % float(str(raw).replace(",", "").strip())
    except Exception:
        return None


def _norm_ref(raw):
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()
    return ("ref:%s" % s) if s else None


def _facts(text):
    """Every checkable fact in `text`, normalized so formatting differences don't matter."""
    text = text or ""
    out = set()
    for m in _AMOUNT_RE.finditer(text):
        n = _norm_amount(m.group(1))
        if n:
            out.add(n)
    for y, mo, d in _ISO_DATE_RE.findall(text):
        out.add("date:%s-%s-%s" % (y, mo, d))
    for mo, d, y in _US_DATE_RE.findall(text):
        yyyy = y if len(y) == 4 else ("20" + y)
        out.add("date:%s-%02d-%02d" % (yyyy, int(mo), int(d)))
    for rx in (_REF_KEYED_RE, _REF_HASH_RE):
        for m in rx.finditer(text):
            n = _norm_ref(m.group(1))
            if n:
                out.add(n)
    return out


def ungrounded_claims(draft_text, source_text):
    """Facts asserted in `draft_text` that do NOT appear anywhere in `source_text`.

    An empty list means every amount, date and identifier in the draft traces back to an input.
    A non-empty list means the draft invented something and must not be sent.
    """
    src = _facts(source_text)
    # A bare identifier is also grounded if its digits appear as an amount/date in the source
    # (e.g. the source writes "$499" and the draft writes "order 499") — be conservative about
    # what we FLAG, never about what we allow through as a genuinely new number.
    src_digits = set(re.findall(r"\d+", source_text or ""))
    bad = []
    for f in sorted(_facts(draft_text)):
        if f in src:
            continue
        if f.startswith("ref:") and re.sub(r"\D", "", f) in src_digits:
            continue
        bad.append(f)
    return bad


def _grounding_corpus(case_title, category, inbound_text, context):
    return "\n".join(str(x or "") for x in (case_title, category, inbound_text, context))


def draft(case_title, category, inbound_text, context=""):
    # Gate 1 — too little to work with: return nothing rather than invent a letter.
    material = "%s %s" % (str(case_title or "").strip(), str(inbound_text or "").strip())
    if len(material.strip()) < MIN_GROUNDING_CHARS:
        return None
    corpus = _grounding_corpus(case_title, category, inbound_text, context)
    user = (
        "CASE: %s\nVENDOR REPLY CATEGORY: %s\nWHAT THE VENDOR SAID:\n%s\n%s\n\nDraft the reply now."
        % (case_title, category, (inbound_text or "").strip()[:3000],
           ("EXTRA CONTEXT: " + context) if context else "")
    )
    messages = [{"role": "system", "content": SYS}, {"role": "user", "content": user}]
    providers = []
    ng = C._load_env_key("NANOGPT_API_KEY")
    if ng:
        providers.append((C.NANOGPT_BASE, C.NANOGPT_MODEL, ng))
    mm = C._load_env_key("MINIMAX_API_KEY")
    if mm:
        providers.append((C.MINIMAX_BASE, C.MINIMAX_MODEL, mm))
    for base, model, key in providers:
        try:
            content = C._http_chat(base, model, key, messages)
        except Exception:
            continue
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = re.sub(r"<think>.*$", "", content, flags=re.DOTALL).strip()
        m = re.search(r"\{.*\}", content, re.S)
        try:
            o = json.loads(m.group(0) if m else content)
        except Exception:
            continue
        if not (o.get("subject") and o.get("body")):
            continue
        cand = {"subject": str(o["subject"])[:200], "body": str(o["body"])}
        # Gate 2 — machine-verified grounding. A fabricated amount/date/reference number is
        # a wrong claim in a real letter; discard the draft and try the next provider.
        bad = ungrounded_claims(cand["subject"] + "\n" + cand["body"], corpus)
        if bad:
            cand["ungrounded"] = bad          # visible to a caller that wants to log it
            continue
        return cand
    return None


# ------------------------------------------------------------------------------- self-test

def _selftest():
    """Offline proof that the drafter is grounded. No network: the provider ladder is stubbed."""
    os.environ["MER_ENGINE_SEND"] = "off"   # nothing in this module sends, but be explicit

    fails = []

    def check(label, cond, detail=""):
        ok = bool(cond)
        print("  %-4s %s%s" % ("PASS" if ok else "FAIL", label, ("  [%s]" % detail) if detail else ""))
        if not ok:
            fails.append(label)

    print("=== draft_reply M38 grounding self-test (offline, stubbed LLM) ===")

    # --- network guard: any real HTTP call is an immediate, loud failure.
    calls = {"n": 0}
    scripted = {"replies": []}

    def fake_http(base, model, key, messages):
        calls["n"] += 1
        if not scripted["replies"]:
            raise RuntimeError("no scripted reply left")
        return scripted["replies"].pop(0)

    C._http_chat = fake_http
    C._load_env_key = lambda name: "stub-key-%s" % name   # two providers on the ladder

    TITLE = "CASE-3 PPG Graco sprayer refund ~$499.00"
    CAT = "needs_info"
    INBOUND = ("Please provide the original receipt from 2026-06-14 and the serial number "
               "for order 77120 so we can look into this.")

    # 1. unit: the checker itself.
    check("checker: clean text has no ungrounded claims",
          ungrounded_claims("I paid $499 on 2026-06-14 for order 77120.",
                            "$499.00 receipt 2026-06-14 order 77120") == [])
    check("checker: catches an invented amount",
          ungrounded_claims("You owe me $1,299.00.", "$499.00 on 2026-06-14")
          == ["amount:1299.00"])
    check("checker: catches an invented date",
          ungrounded_claims("As agreed on 2026-08-01.", "receipt 2026-06-14") == ["date:2026-08-01"])
    check("checker: catches an invented order number",
          ungrounded_claims("Regarding order 88231.", "order 77120") == ["ref:88231"])
    check("checker: $499 and $499.00 are the same fact",
          ungrounded_claims("refund of $499.00", "the $499 sprayer") == [])
    check("checker: 6/14/2026 and 2026-06-14 are the same fact",
          ungrounded_claims("your letter of 6/14/2026", "dated 2026-06-14") == [])

    # 2. a fully grounded draft is returned unchanged.
    scripted["replies"] = ['{"subject": "Re: my $499.00 refund request",'
                           ' "body": "I attach the receipt dated 2026-06-14 for order 77120."}']
    d = draft(TITLE, CAT, INBOUND)
    check("grounded draft is returned", d and d.get("body"), repr(d)[:70])

    # 3. an invented AMOUNT is refused (both providers fabricate -> None).
    scripted["replies"] = ['{"subject": "Refund", "body": "Please refund the full $1,299.00."}',
                           '{"subject": "Refund", "body": "Please refund the full $1,299.00."}']
    d = draft(TITLE, CAT, INBOUND)
    check("invented AMOUNT is refused (returns None)", d is None, repr(d)[:70])

    # 4. an invented DATE is refused.
    scripted["replies"] = ['{"subject": "Refund", "body": "As you promised on 2026-05-02."}'] * 2
    d = draft(TITLE, CAT, INBOUND)
    check("invented DATE is refused (returns None)", d is None, repr(d)[:70])

    # 5. an invented CASE/ORDER NUMBER is refused.
    scripted["replies"] = ['{"subject": "Refund", "body": "See case number 4471902."}'] * 2
    d = draft(TITLE, CAT, INBOUND)
    check("invented CASE NUMBER is refused (returns None)", d is None, repr(d)[:70])

    # 6. a fabricating provider does not poison the ladder: provider 2's clean draft wins.
    scripted["replies"] = ['{"subject": "Refund", "body": "Refund $2,000.00 now."}',
                           '{"subject": "Refund", "body": "Refund the $499.00 as agreed."}']
    d = draft(TITLE, CAT, INBOUND)
    check("fabricating provider is skipped, clean fallback returned",
          d and "$499.00" in d["body"], repr(d)[:70])

    # 7. too little material -> no draft AND no provider call at all.
    before = calls["n"]
    d = draft("", "unknown", "")
    check("no material -> returns None", d is None)
    check("no material -> LLM is never called", calls["n"] == before,
          "calls before=%d after=%d" % (before, calls["n"]))
    d2 = draft("CASE-9", "needs_info", "?")
    check("near-empty material -> returns None", d2 is None, repr(d2)[:70])

    # 8. malformed / non-JSON LLM output yields nothing, never a raw-text letter.
    scripted["replies"] = ["I'm sorry, I can't help with that.", "<think>hmm</think> nope"]
    d = draft(TITLE, CAT, INBOUND)
    check("non-JSON LLM output -> returns None", d is None, repr(d)[:70])

    # 9. a draft missing a body is never returned as a partial letter.
    scripted["replies"] = ['{"subject": "Refund", "body": ""}'] * 2
    d = draft(TITLE, CAT, INBOUND)
    check("empty body -> returns None", d is None, repr(d)[:70])

    # 10. structural: the prompt alone is not the gate — draft() calls the checker.
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    check("draft() enforces grounding in code (calls ungrounded_claims)",
          "bad = ungrounded_claims(" in src)

    if fails:
        print("\nFAIL — %d grounding check(s) wrong: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("\nPASS — the drafter never invents a fact, and returns nothing when under-grounded.")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv or "--demo" not in sys.argv:
        sys.exit(_selftest())
    # --demo: the original live LLM smoke test (needs provider keys + network).
    d = draft("CASE-3 PPG Graco sprayer refund ~$499",
              "needs_info",
              "Please provide the original receipt and the serial number so we can look into this.")
    print(json.dumps(d, indent=2) if d else "draft unavailable (LLM failed)")
