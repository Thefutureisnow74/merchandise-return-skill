#!/usr/bin/env python3
"""
new_claim_draft.py — pre-fill a CASE SKELETON from a raw inbound "get my money back"
email (Merchandise Returns Engine, Blueprint M16).

WHY: the new-claim detector (M13) surfaces an unmatched inbound that looks like a fresh
"I want a refund" request and asks King "open a case?". Without this module that click
still drops him into a blank intake. M16 fills the intake fields it can *read from the
email* so the case opens pre-populated and "open a case?" is genuinely one click.

HARD RULE — NEVER INVENT. Every field is either quoted/paraphrased from the email or it
goes into missing_fields[]. A blank field is safe (the intake asks the user for it); a
fabricated vendor/amount/model is dangerous (it seeds a case with a lie). So the LLM is
told to return null for anything not stated, and this module — not the model — decides
what is "missing" by inspecting which fields came back empty.

THIS MODULE NEVER OPENS A CASE. draft_skeleton() returns the dict only; opening remains a
human click in the detector's surfaced prompt.

Skeleton fields (mapped to intake-questionnaire.md):
    vendor                    <- 1.1 where bought (retailer/carrier/manufacturer)
    item                      <- 2.1 brand + product
    model_or_serial_if_present<- 2.1/2.2 exact model, capacity, IMEI/serial (if stated)
    approx_amount             <- 1.3 amount paid
    purchase_timeframe        <- 1.2 purchase date (approx OK, verbatim phrasing)
    defect_summary            <- 3.1 what's wrong, in their words
    desired_outcome           <- 5.1 refund / replacement / repair / credit
    jurisdiction_hint         <- any state/county/country signal in the mail (else missing)
    missing_fields[]          <- every skeleton field left unknown, by name

Public API:
    draft_skeleton(subject, body) -> dict   (never raises; LLM-optional)

Design: reuses classify_llm's provider ladder (nanogpt/GLM -> MiniMax-M3), its
_http_chat / _load_env_key helpers, and the reasoning-model <think>-stripping fix.
stdlib + urllib only, no pip installs.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import classify_llm as C  # noqa: E402  (provider ladder + _http_chat + _load_env_key + <think> fix)

# The extractable skeleton fields, in intake order. jurisdiction_hint and
# model_or_serial_if_present are commonly absent from a first email — that is fine, they
# just land in missing_fields.
FIELDS = [
    "vendor",
    "item",
    "model_or_serial_if_present",
    "approx_amount",
    "purchase_timeframe",
    "defect_summary",
    "desired_outcome",
    "jurisdiction_hint",
]

SYS = (
    "You extract a merchandise-return CASE SKELETON from a customer's raw inbound email. "
    "The customer is asking to get their money back on a product. Read the whole message "
    "and fill ONLY what the email actually states.\n"
    "ABSOLUTE RULE: never invent, guess, or infer a value that is not in the email. If a "
    "field is not stated, return null for it. A null is correct and expected; a fabricated "
    "value is a serious error. Do not normalize a vague phrase into a fake precise one "
    "(keep 'about two years ago' as-is; do not turn it into a date).\n\n"
    "Fields:\n"
    "  vendor                     = who they bought it from / who they'd pursue: retailer, "
    "carrier (e.g. T-Mobile), marketplace, or manufacturer. Use the entity named as the seller.\n"
    "  item                       = the product, as specifically as the email allows "
    "(brand + product, e.g. 'Samsung Galaxy Z Fold 3').\n"
    "  model_or_serial_if_present = exact model/variant/capacity, IMEI, or serial number, "
    "ONLY if stated. Else null.\n"
    "  approx_amount              = the price paid / amount at stake, only if a number is given. Else null.\n"
    "  purchase_timeframe         = when they bought it, in the email's own words "
    "(e.g. 'two years ago'). Else null.\n"
    "  defect_summary             = what is wrong, in their words (short).\n"
    "  desired_outcome            = what they want: refund, replacement, repair, or credit "
    "(preserve 'refund or replacement' if they say both).\n"
    "  jurisdiction_hint          = any location signal (state, county, country, city) that "
    "hints at legal jurisdiction, ONLY if present. Else null.\n\n"
    'Return ONLY compact JSON with exactly these keys and no others: '
    '{"vendor":..,"item":..,"model_or_serial_if_present":..,"approx_amount":..,'
    '"purchase_timeframe":..,"defect_summary":..,"desired_outcome":..,"jurisdiction_hint":..}. '
    "Use null (not empty string, not a guess) for anything the email does not state. "
    "No markdown, no code fence, no commentary."
)


def _empty_skeleton(note):
    sk = {f: None for f in FIELDS}
    sk["missing_fields"] = list(FIELDS)
    sk["_source"] = note
    return sk


def _clean(v):
    """Normalize a model value: strip whitespace; treat null-ish / placeholder as absent."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).strip()
    if not s:
        return None
    if s.lower() in ("null", "none", "n/a", "na", "unknown", "not stated",
                     "not provided", "not specified", "-", "--"):
        return None
    return s


def _finalize(raw):
    """Turn a raw model dict into a validated skeleton: keep only known fields, coerce
    empties/placeholders to None, and derive missing_fields[] from what's actually absent.
    missing_fields is computed HERE, never trusted from the model."""
    sk = {}
    missing = []
    for f in FIELDS:
        val = _clean(raw.get(f) if isinstance(raw, dict) else None)
        sk[f] = val
        if val is None:
            missing.append(f)
    sk["missing_fields"] = missing
    return sk


def draft_skeleton(subject, body):
    """Extract a pre-filled case skeleton from a raw inbound email.

    Returns a dict with the FIELDS keys plus 'missing_fields' (list of field names left
    unknown) and '_source' (which provider / fallback produced it). Never raises; on total
    LLM failure returns an all-missing skeleton so the detector can still open a blank case.
    NEVER opens a case.
    """
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject and not body:
        return _empty_skeleton("empty input")

    user = (
        "=== EMAIL SUBJECT ===\n%s\n\n=== EMAIL BODY ===\n%s\n\n"
        "Extract the case skeleton now. Remember: null for anything not stated."
        % (subject or "(none)", body[:4000] or "(none)")
    )
    messages = [{"role": "system", "content": SYS}, {"role": "user", "content": user}]

    providers = []
    ng = C._load_env_key("NANOGPT_API_KEY")
    if ng:
        providers.append(("nanogpt/GLM", C.NANOGPT_BASE, C.NANOGPT_MODEL, ng))
    mm = C._load_env_key("MINIMAX_API_KEY")
    if mm:
        providers.append(("MiniMax-M3", C.MINIMAX_BASE, C.MINIMAX_MODEL, mm))

    if not providers:
        return _empty_skeleton("no LLM API key — all fields for manual intake")

    last = "LLM unavailable"
    for label, base, model, key in providers:
        try:
            content = C._http_chat(base, model, key, messages)
        except Exception as e:  # HTTP, timeout, URLError, key error, etc.
            last = "%s error: %s" % (label, type(e).__name__)
            continue
        # Reasoning models (MiniMax-M3) emit <think>...</think> before the JSON — strip it.
        content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL)
        content = re.sub(r"<think>.*$", "", content, flags=re.DOTALL).strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
            content = re.sub(r"\s*```$", "", content).strip()
        obj = None
        try:
            obj = json.loads(content)
        except Exception:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except Exception:
                    obj = None
        if isinstance(obj, dict):
            sk = _finalize(obj)
            sk["_source"] = label
            return sk
        last = "%s returned unparseable output" % label

    # Every provider failed — return an all-missing skeleton, never a fabricated one.
    sk = _empty_skeleton("LLM failed (%s) — all fields for manual intake" % last)
    return sk


# --- CLI / self-test ---------------------------------------------------------------
# Synthetic new-claim email, shaped like a real one: vague, lower-case, no amount, the
# model name arriving only in a trailing fragment. Expected: vendor=a carrier, item=a
# foldable phone, defect=screen, outcome=refund or replacement,
# purchase_timeframe~=two years ago, approx_amount in missing_fields.
DEMO_SUBJECT = "Refund or replacement for my phone"
DEMO_BODY = (
    "I bought this phone through Northwind Mobile two years ago and the screen stopped "
    "working in the front. I would like to get a refund or a full replacement. This is "
    "the Foldex three..."
)


def _run_demo():
    print("=== new_claim_draft — M16 case-skeleton extractor ===")
    print("    provider ladder: nanogpt/%s -> %s  | fallback: all-missing skeleton\n"
          % (C.NANOGPT_MODEL, C.MINIMAX_MODEL))
    sk = draft_skeleton(DEMO_SUBJECT, DEMO_BODY)
    print("foldable-phone new-claim test:")
    print(json.dumps(sk, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 2:
        print(json.dumps(draft_skeleton(sys.argv[1], " ".join(sys.argv[2:])), indent=2))
    else:
        _run_demo()
