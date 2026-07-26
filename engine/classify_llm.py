#!/usr/bin/env python3
"""
classify_llm.py — LLM-judged classification of an inbound vendor/regulator reply on a
live return case (Merchandise Returns Engine, milestone M11; provider layer M36).

WHY: reply_classify.py is a fast, deterministic HEURISTIC classifier. Its keyword rules
misfire on real replies — e.g. a refusal that quotes the word "refund", or a vendor that
merely *confirms an account is active* while denying the refund. This module asks an LLM
to read the whole reply (plus any attachment text) and pick ONE of the 7 canonical
categories with a one-line rationale.

M36 — THE PROVIDER IS CONFIGURATION, NOT CODE
---------------------------------------------
Until M36 this file hardcoded two vendors and their env-var key names (NANOGPT_* /
MINIMAX_*). A person handed this skill has NEITHER key and no way to point the engine at
their own provider without editing this file. Now the ladder is an ordered LIST loaded by
`llm_providers.py` — from $MER_LLM_PROVIDERS, the identity profile's "llm_providers" key,
./llm_providers.json, /opt/data/mer_llm_providers.json, or (last) the shipped default
ladder, which is exactly the pre-M36 NanoGPT + MiniMax pair. An instance with no provider
config resolves to the same two endpoints, the same models and the same request body it
ran before M36. See llm_providers.py's docstring for the full precedence order.

Reasoning-model handling is likewise per-provider config now, not an `if model ==`:
MiniMax-M3 emits a `<think>` block before its JSON and needs max_tokens >= 1500 (with 200
the answer was truncated away — a real bug, do not regress it). The default entry carries
`"reasoning": true`, which floors max_tokens at llm_providers.REASONING_MIN_TOKENS; and
`strip_think` (default true for every provider) removes the block before parsing.

  Robustness ladder (fail-safe, never fail-open):
    1..N. every configured provider, in the configured order
    last.  reply_classify.py heuristic  (deterministic last resort — the engine keeps routing)

  ZERO providers configured — or configured providers whose API keys are all absent — is a
  SUPPORTED state, not a failure. classify() returns a heuristic answer and never raises.
  A user with no LLM credentials at all still gets a working, DEGRADED engine.

Public API:
    classify(reply_text, attachment_text="") -> {"category": <one of 7>, "rationale": <str>}

Compatibility note: draft_reply.py, new_claim_draft.py and resolution_check.py import
NANOGPT_BASE / NANOGPT_MODEL / MINIMAX_BASE / MINIMAX_MODEL, _http_chat() and
_load_env_key() from here. Those names are part of this module's contract and are kept
working; the constants are now DERIVED from the default provider list rather than typed
twice.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

# --- pull the canonical category list + heuristic fallback from reply_classify.py ---
_HERE = os.path.dirname(os.path.abspath(__file__))
# reply_classify.py may sit next to this file (VPS: /opt/data/scripts/) or one dir up
# in the repo layout (Multica/scripts/). Make both importable.
for _p in (_HERE, os.path.dirname(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import reply_classify  # heuristic classifier + CATEGORIES + demo harness
    CATEGORIES = list(reply_classify.CATEGORIES)
except Exception:  # pragma: no cover - reply_classify should always be present
    reply_classify = None
    CATEGORIES = ["refund", "partial", "refused", "needs_info",
                  "legal_threat", "discrimination_signal", "other"]

# --- the provider configuration layer (M36) ----------------------------------------
try:
    import llm_providers
except Exception as _e:  # pragma: no cover - shipped alongside this file
    llm_providers = None
    _PROVIDER_IMPORT_ERROR = _e
else:
    _PROVIDER_IMPORT_ERROR = None

# --- config / secrets --------------------------------------------------------------
ENV_PATHS = ["/opt/data/.env", os.path.join(_HERE, ".env"),
             os.path.join(os.path.dirname(_HERE), ".env")]


def _default_provider(key_env):
    """The shipped default entry that uses `key_env`, so the legacy module constants
    below stay in exactly one place (llm_providers.DEFAULT_PROVIDERS)."""
    if llm_providers is not None:
        for d in llm_providers.DEFAULT_PROVIDERS:
            if d.get("api_key_env") == key_env:
                return d
    return {}


# BACKWARD-COMPATIBLE CONSTANTS.
# draft_reply.py / new_claim_draft.py read these (C.NANOGPT_BASE, C.MINIMAX_MODEL, ...).
# They are the DEFAULT ladder's values — the same literals as pre-M36 — not a second copy.
NANOGPT_BASE = _default_provider("NANOGPT_API_KEY").get("base_url", "https://nano-gpt.com/api/v1")
NANOGPT_MODEL = _default_provider("NANOGPT_API_KEY").get("model", "zai-org/glm-5.2")
MINIMAX_BASE = _default_provider("MINIMAX_API_KEY").get("base_url", "https://api.minimax.io/v1")
MINIMAX_MODEL = _default_provider("MINIMAX_API_KEY").get("model", "MiniMax-M3")

# Default per-call timeout when no provider object is supplied (legacy _http_chat callers).
TIMEOUT = llm_providers.DEFAULT_TIMEOUT if llm_providers is not None else 40
# Legacy default max_tokens for the same callers. 1500, not 200: reasoning models spend
# tokens on <think> before the JSON, and 200 truncated the answer on real mail.
DEFAULT_MAX_TOKENS = llm_providers.DEFAULT_MAX_TOKENS if llm_providers is not None else 1500


def _load_env_key(name):
    """Return an env var, reading /opt/data/.env if it is not already in the process env.
    Never prints the value."""
    if not name:
        return None
    v = os.environ.get(name)
    if v:
        return v.strip()
    for path in ENV_PATHS:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, val = line.split("=", 1)
                    if k.strip() == name:
                        return val.strip().strip('"').strip("'")
        except OSError:
            continue
    return None


CATEGORY_GUIDE = (
    "refund               = a full refund / full replacement is approved, issued, or on its way.\n"
    "partial              = a partial remedy only: partial refund, store credit, a percentage off, "
    "or a one-time goodwill gesture short of the full amount.\n"
    "refused              = the request is DENIED or the vendor is unable/unwilling to refund "
    "(even if the word 'refund' appears, or they only confirm the account/card is fine while giving nothing back).\n"
    "needs_info           = they are asking US for something before deciding: receipt, serial, "
    "order number, photos, or more information. No decision yet.\n"
    "legal_threat         = a threat of legal action against us — litigation, defamation, "
    "cease-and-desist, referral to their attorneys/legal department.\n"
    "discrimination_signal= language indicating refusal/mistreatment because of who the customer is "
    "(a protected-class or 'people like you' signal). Preserve as evidence.\n"
    "other                = none of the above / cannot tell."
)

SYSTEM_PROMPT = (
    "You are a precise dispute-resolution triage classifier for a merchandise-return case. "
    "You read an inbound reply from a vendor, retailer, or regulator and assign EXACTLY ONE "
    "category. Judge the reply's actual outcome for the customer, not its keywords: a message "
    "that says 'we are unable to offer a refund' is 'refused', and a message that only confirms "
    "an account/card is active without giving money back is 'refused', not 'refund'. "
    "Reply ONLY with a compact JSON object, no markdown, no code fence."
)


def _build_user_prompt(reply_text, attachment_text):
    parts = [
        "Categories (choose exactly one):",
        CATEGORY_GUIDE,
        "",
        "Return ONLY this JSON: {\"category\": \"<one category>\", \"rationale\": \"<one short sentence>\"}",
        "",
        "=== INBOUND REPLY ===",
        (reply_text or "").strip() or "(empty)",
    ]
    if attachment_text and attachment_text.strip():
        parts += ["", "=== ATTACHMENT TEXT ===", attachment_text.strip()[:4000]]
    return "\n".join(parts)


def _legacy_body(model, messages):
    """The exact request body this module sent before M36. Used when a caller invokes
    _http_chat() without a provider object (draft_reply.py, new_claim_draft.py)."""
    return {
        "model": model,
        "messages": messages,
        "temperature": 0,
        # Reasoning models (MiniMax-M3) spend tokens on a <think> block BEFORE the JSON.
        # 200 truncated the answer on complex mail; give room for reasoning + the JSON.
        "max_tokens": DEFAULT_MAX_TOKENS,
    }


def _http_chat(base, model, api_key, messages, provider=None, timeout=None):
    """One OpenAI-compatible /chat/completions call. Returns assistant content str.
    Raises on any transport/HTTP error.

    Signature is unchanged for the three modules that import it. `provider` is an optional
    llm_providers.LLMProvider supplying max_tokens / temperature / timeout / extra params;
    with provider=None the pre-M36 body is sent verbatim."""
    if provider is not None:
        payload = provider.body(messages)
        url = provider.chat_url()
        tmo = provider.timeout if timeout is None else timeout
    else:
        payload = _legacy_body(model, messages)
        url = base.rstrip("/") + "/chat/completions"
        tmo = TIMEOUT if timeout is None else timeout
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=tmo) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def _strip_think(text):
    """Remove a reasoning model's <think> block — closed, or left open by truncation."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return re.sub(r"<think>.*$", "", text, flags=re.DOTALL).strip()


def _parse_llm_json(content, provider=None):
    """Extract {category, rationale} from a model reply that should be JSON but might be
    wrapped in prose / a code fence. Returns a validated dict or None.

    <think> stripping is a per-provider setting (llm_providers strip_think, default True
    for every provider) rather than a hardcoded MiniMax special case. With provider=None
    it strips unconditionally — the pre-M36 behavior."""
    if not content:
        return None
    text = content.strip()
    if provider is None or getattr(provider, "strip_think", True):
        text = _strip_think(text)
    # strip a ```json ... ``` fence if present
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return None
    cat = str(obj.get("category", "")).strip().lower()
    # tolerate minor variants
    cat = cat.replace(" ", "_")
    if cat not in CATEGORIES:
        # last-chance: bare category word somewhere in the reply
        for c in CATEGORIES:
            if re.search(r"\b" + re.escape(c) + r"\b", text.lower()):
                cat = c
                break
        else:
            return None
    rationale = str(obj.get("rationale", "")).strip() or "(no rationale returned)"
    return {"category": cat, "rationale": rationale[:300]}


def _heuristic_fallback(reply_text, attachment_text, reason):
    """Deterministic last resort using reply_classify.py so the engine never stalls."""
    combined = ((reply_text or "") + "\n" + (attachment_text or "")).strip()
    if reply_classify is not None:
        cat, sig = reply_classify.classify(combined)
        detail = f"matched /{sig}/" if sig else "no signal matched"
        return {
            "category": cat,
            "rationale": f"heuristic fallback ({reason}); {detail}",
        }
    return {"category": "other",
            "rationale": f"heuristic fallback ({reason}); reply_classify unavailable"}


def active_providers():
    """Return (usable_providers, note).

    `usable_providers` is [(LLMProvider, api_key)] for every configured provider whose key
    is available (or that needs none), in configured order. `note` explains an empty list.

    NEVER raises: a missing/misconfigured provider list yields an empty ladder plus a note,
    and the caller degrades to the heuristic. A bad config is loud in the rationale and on
    stderr, but it does not take the engine down.
    """
    if llm_providers is None:
        return [], "llm_providers module unavailable (%s)" % (_PROVIDER_IMPORT_ERROR,)
    try:
        configured = llm_providers.providers()
    except llm_providers.LLMProviderError as e:
        msg = str(e).splitlines()[0]
        print("classify_llm: LLM provider config problem — %s" % msg, file=sys.stderr)
        return [], "provider config error: %s" % msg
    except Exception as e:  # pragma: no cover - defensive
        return [], "provider config error: %s" % type(e).__name__
    if not configured:
        return [], "no LLM providers configured"
    usable = []
    skipped = []
    for p in configured:
        if p.api_key_env:
            key = _load_env_key(p.api_key_env)
            if not key:
                skipped.append("%s (no $%s)" % (p.name, p.api_key_env))
                continue
        else:
            key = None            # a keyless endpoint, e.g. a local Ollama server
        usable.append((p, key))
    if not usable:
        return [], "no LLM API key found for: %s" % ", ".join(skipped)
    return usable, ""


def classify(reply_text, attachment_text=""):
    """Classify an inbound reply. Returns {'category', 'rationale'}.

    Tries every configured provider in order; on ANY error/timeout/invalid output falls
    through to the next, and finally to reply_classify.py's heuristic. Never raises."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(reply_text, attachment_text)},
    ]

    providers, note = active_providers()
    if not providers:
        return _heuristic_fallback(reply_text, attachment_text, note or "no LLM available")

    last_reason = "LLM unavailable"
    for p, key in providers:
        try:
            content = _http_chat(p.base_url, p.model, key, messages, provider=p)
        except urllib.error.HTTPError as e:
            last_reason = f"{p.name} HTTP {e.code}"
            continue
        except Exception as e:  # timeout, URLError, JSON, key error, etc.
            last_reason = f"{p.name} error: {type(e).__name__}"
            continue
        parsed = _parse_llm_json(content, provider=p)
        if parsed:
            parsed["rationale"] = f"[{p.name}] {parsed['rationale']}"
            return parsed
        last_reason = f"{p.name} returned unparseable output"

    return _heuristic_fallback(reply_text, attachment_text, last_reason)


# --- CLI / live demo ---------------------------------------------------------------
# A synthetic bank brush-off. It keeps the SHAPE of the real reply this fixture was built from —
# the bank confirms the account and lists the original CHARGES while never addressing the missing
# REFUND credits — because that shape is the whole point: it is what the fake-resolution detector
# (M19) has to catch. The amounts, merchant descriptors and dates are invented; the original used a
# real person's transactions, which cannot ship in a skill handed to strangers.
STRIDE_REPLY = (
    "We have reviewed your concern regarding the two refunds missing on your account for "
    "$120.00 and $65.00. We confirmed the account has been active since October 2024 and "
    "the card is not closed. Transactions that posted: Jun 13 $120.00 VENDOR AGENCY SUB; "
    "Jun 15 $65.00 VENDOR TRIAL OVER."
)


def _run_demo():
    """The LIVE smoke test — makes real LLM calls. Not run by the harness."""
    print("=== classify_llm — LLM-judged classification (M11/M36) ===")
    if llm_providers is not None:
        try:
            print("    provider source: %s" % llm_providers.provider_source())
            for i, p in enumerate(llm_providers.providers(), 1):
                print("      %d. %s  (%s @ %s)" % (i, p.name, p.model, p.base_url))
        except Exception as e:
            print("    provider config error: %s" % e)
    print("    last resort: heuristic reply_classify\n")
    cases = []
    if reply_classify is not None:
        cases = [(who, msg) for who, msg in reply_classify.DEMO]
    cases.append(("Stride/Lyft (REAL)", STRIDE_REPLY))
    for who, msg in cases:
        r = classify(msg)
        print(f"[{r['category']:>22}]  from {who}")
        print(f"     reply: {msg[:72]}")
        print(f"     rationale: {r['rationale']}\n")


# --- offline self-test (M36) -------------------------------------------------------

def _selftest():
    """Offline proof of the provider ladder. No network: _http_chat is stubbed and any
    real HTTP call would be an immediate failure (urlopen is never reached)."""
    import tempfile

    results = []

    def check(desc, cond, detail=""):
        results.append(bool(cond))
        print("%s — %s%s" % ("PASS" if cond else "FAIL", desc,
                             ("  [%s]" % detail) if detail and not cond else ""))

    print("=== classify_llm M36 provider-ladder self-test (offline, stubbed HTTP) ===")

    global _http_chat, _load_env_key
    real_http, real_key = _http_chat, _load_env_key
    prev_env = os.environ.pop(llm_providers.ENV_VAR, None)
    prev_cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="classify_llm_test_")
    calls = []            # [(provider_name, body_dict)] in the order attempted

    REFUSAL = ("Unfortunately the device is out of warranty and we are unable to offer a "
               "refund. This is our final decision.")

    def make_stub(script):
        """script: {provider_name: str_reply | Exception}. Records every attempt."""
        def stub(base, model, api_key, messages, provider=None, timeout=None):
            name = provider.name if provider is not None else "(legacy)"
            body = provider.body(messages) if provider is not None else _legacy_body(model, messages)
            calls.append((name, body))
            out = script.get(name, RuntimeError("unreachable"))
            if isinstance(out, BaseException):
                raise out
            return out
        return stub

    def set_providers(lst):
        os.environ[llm_providers.ENV_VAR] = json.dumps(lst)
        llm_providers.reset()

    try:
        os.chdir(tmp)                              # no stray ./llm_providers.json
        _load_env_key = lambda name: "stub-key-%s" % name if name else None  # noqa: E731

        # ---------------------------------------------------------------- 1. defaults
        # No provider config at all must resolve to the exact pre-M36 ladder.
        os.environ.pop(llm_providers.ENV_VAR, None)
        llm_providers.reset()
        act, note = active_providers()
        check("no config -> 2 providers on the ladder", len(act) == 2, "%d %s" % (len(act), note))
        check("no config -> #1 is nanogpt/GLM at the pre-M36 base+model",
              act[0][0].name == "nanogpt/GLM"
              and act[0][0].base_url == "https://nano-gpt.com/api/v1"
              and act[0][0].model == "zai-org/glm-5.2")
        check("no config -> #2 is MiniMax-M3 at the pre-M36 base+model",
              act[1][0].name == "MiniMax-M3"
              and act[1][0].base_url == "https://api.minimax.io/v1"
              and act[1][0].model == "MiniMax-M3")
        check("legacy constants still match the default ladder",
              (NANOGPT_BASE, NANOGPT_MODEL, MINIMAX_BASE, MINIMAX_MODEL)
              == ("https://nano-gpt.com/api/v1", "zai-org/glm-5.2",
                  "https://api.minimax.io/v1", "MiniMax-M3"))
        # the request body sent with no config == the body sent before M36
        calls[:] = []
        _http_chat = make_stub({"nanogpt/GLM":
                                '{"category":"refused","rationale":"denied outright"}'})
        r = classify(REFUSAL)
        check("no config -> body is byte-identical to pre-M36",
              calls and calls[0][1] == _legacy_body("zai-org/glm-5.2", calls[0][1]["messages"]),
              json.dumps(calls[0][1], sort_keys=True)[:120] if calls else "no call")
        check("no config -> classification comes from provider #1",
              r["category"] == "refused" and r["rationale"].startswith("[nanogpt/GLM]"), str(r))

        # ---------------------------------------------------------------- 2. order
        set_providers([
            {"name": "P1", "base_url": "http://p1/v1", "model": "m1", "api_key_env": "K1"},
            {"name": "P2", "base_url": "http://p2/v1", "model": "m2", "api_key_env": "K2"},
            {"name": "P3", "base_url": "http://p3/v1", "model": "m3", "api_key_env": "K3"},
        ])
        calls[:] = []
        _http_chat = make_stub({"P1": '{"category":"refused","rationale":"p1 answered"}'})
        r = classify(REFUSAL)
        check("provider ORDER is honored — P1 is tried first",
              [c[0] for c in calls] == ["P1"], str([c[0] for c in calls]))
        check("P1's answer is used and labeled", r["rationale"].startswith("[P1]"), str(r))

        # ------------------------------------------------- 3. fall-through on failure
        calls[:] = []
        _http_chat = make_stub({
            "P1": urllib.error.HTTPError("http://p1/v1", 429, "Too Many Requests", {}, None),
            "P2": '{"category":"refused","rationale":"p2 rescued it"}',
        })
        r = classify(REFUSAL)
        check("a failing provider falls through to the NEXT one",
              [c[0] for c in calls] == ["P1", "P2"], str([c[0] for c in calls]))
        check("the next provider's answer is returned",
              r["category"] == "refused" and r["rationale"].startswith("[P2]"), str(r))
        check("a throttled P1 does NOT reach P3", "P3" not in [c[0] for c in calls])

        # unparseable output also falls through
        calls[:] = []
        _http_chat = make_stub({
            "P1": "I'm sorry, I can't help with that.",
            "P2": "<think>hmm</think> still not JSON",
            "P3": '{"category":"needs_info","rationale":"p3 parsed"}',
        })
        r = classify(REFUSAL)
        check("unparseable output falls through the whole ladder",
              [c[0] for c in calls] == ["P1", "P2", "P3"], str([c[0] for c in calls]))
        check("the first parseable answer wins",
              r["category"] == "needs_info" and r["rationale"].startswith("[P3]"), str(r))

        # ------------------------------------- 4. every provider down -> DEGRADED
        calls[:] = []
        _http_chat = make_stub({
            "P1": urllib.error.HTTPError("http://p1/v1", 429, "Too Many Requests", {}, None),
            "P2": urllib.error.URLError("connection refused"),
            "P3": TimeoutError("timed out"),
        })
        r = classify(REFUSAL)
        check("all providers down -> heuristic, no exception",
              r["category"] == "refused" and "heuristic fallback" in r["rationale"], str(r))
        check("all providers were actually tried before degrading",
              [c[0] for c in calls] == ["P1", "P2", "P3"], str([c[0] for c in calls]))

        # -------------------------------- 5. ZERO providers configured -> DEGRADED
        for label, value in (("empty list", "[]"), ("'none'", "none")):
            os.environ[llm_providers.ENV_VAR] = value
            llm_providers.reset()
            calls[:] = []
            _http_chat = make_stub({})       # any call at all would raise "unreachable"
            try:
                r = classify(REFUSAL)
                raised = None
            except Exception as e:           # the thing that must NEVER happen
                r, raised = None, e
            check("zero providers (%s) does NOT raise" % label, raised is None, repr(raised))
            check("zero providers (%s) degrades to the heuristic" % label,
                  r and r["category"] == "refused" and "heuristic fallback" in r["rationale"],
                  str(r))
            check("zero providers (%s) makes no HTTP call at all" % label, calls == [],
                  str(calls))

        # ---------------------- 6. providers configured but NO credentials -> DEGRADED
        set_providers([{"name": "NeedsKey", "base_url": "http://x/v1", "model": "m",
                        "api_key_env": "TOTALLY_ABSENT_KEY"}])
        _load_env_key = lambda name: None    # noqa: E731 — the fresh-install state
        calls[:] = []
        _http_chat = make_stub({})
        r = classify(REFUSAL)
        check("no credentials at all -> degrades, never raises",
              r["category"] == "refused" and "heuristic fallback" in r["rationale"], str(r))
        check("no credentials -> the reason names the missing key",
              "TOTALLY_ABSENT_KEY" in r["rationale"], str(r))
        check("no credentials -> no HTTP call attempted", calls == [], str(calls))
        _load_env_key = lambda name: "stub-key-%s" % name if name else None  # noqa: E731

        # ---------------------------------------------- 7. reasoning-model handling
        set_providers([
            {"name": "Reasoner", "base_url": "http://r/v1", "model": "big-thinker",
             "api_key_env": "RK", "reasoning": True, "max_tokens": 200},
            {"name": "Plain", "base_url": "http://p/v1", "model": "small",
             "api_key_env": "PK", "max_tokens": 200},
        ])
        calls[:] = []
        _http_chat = make_stub({
            "Reasoner": ("<think>The vendor says out of warranty and declines. That is a "
                         "refusal, not a refund.</think>\n"
                         '{"category":"refused","rationale":"vendor declined outright"}'),
        })
        r = classify(REFUSAL)
        body = calls[0][1]
        check("reasoning provider gets the max_tokens FLOOR (1500), not its configured 200",
              body["max_tokens"] == llm_providers.REASONING_MIN_TOKENS, str(body["max_tokens"]))
        check("a NON-reasoning provider keeps its configured max_tokens",
              [p for p, _ in active_providers()[0] if p.name == "Plain"][0].max_tokens == 200)
        check("<think> block is stripped and the JSON behind it parses",
              r["category"] == "refused" and r["rationale"].startswith("[Reasoner]"), str(r))
        check("the <think> text does not leak into the rationale",
              "<think>" not in r["rationale"] and "vendor" in r["rationale"].lower(), str(r))

        # a TRUNCATED (unclosed) <think> — the original M11 bug shape — is still stripped,
        # and when nothing parseable is left the ladder falls through rather than guessing.
        calls[:] = []
        _http_chat = make_stub({
            "Reasoner": "<think>I need to weigh whether out of warranty means refus",
            "Plain": '{"category":"refused","rationale":"plain model answered"}',
        })
        r = classify(REFUSAL)
        check("a truncated <think> block yields no false answer, falls to next provider",
              [c[0] for c in calls] == ["Reasoner", "Plain"]
              and r["rationale"].startswith("[Plain]"), str(r))

        # strip_think is per-provider CONFIG, not a hardcoded MiniMax special case. Same
        # payload, two providers, opposite results — that is the proof.
        THINK_ONLY = '<think>{"category":"refund","rationale":"reasoning scratchpad"}</think>'
        set_providers([{"name": "Strips", "base_url": "http://n/v1", "model": "m",
                        "api_key_env": "NK"}])            # strip_think defaults to true
        _http_chat = make_stub({"Strips": THINK_ONLY})
        r = classify(REFUSAL)
        check("strip_think defaults ON: content inside <think> is not treated as the answer",
              "heuristic fallback" in r["rationale"], str(r))
        set_providers([{"name": "NoStrip", "base_url": "http://n/v1", "model": "m",
                        "api_key_env": "NK", "strip_think": False}])
        _http_chat = make_stub({"NoStrip": THINK_ONLY})
        r = classify(REFUSAL)
        check("strip_think=false is honored (same payload now parses)",
              r["category"] == "refund" and r["rationale"].startswith("[NoStrip]"), str(r))

        # ------------------------------------------- 8. bad config degrades, loudly
        os.environ[llm_providers.ENV_VAR] = '[{"name": "broken"}]'   # no base_url/model
        llm_providers.reset()
        calls[:] = []
        _http_chat = make_stub({})
        try:
            r = classify(REFUSAL)
            raised = None
        except Exception as e:
            r, raised = None, e
        check("a malformed provider config does NOT raise", raised is None, repr(raised))
        check("a malformed provider config degrades to the heuristic",
              r and "heuristic fallback" in r["rationale"], str(r))

        # -------------------------------- 9. the legacy _http_chat contract still holds
        _http_chat = real_http
        import inspect
        sig = list(inspect.signature(_http_chat).parameters)
        check("_http_chat(base, model, api_key, messages, ...) signature preserved",
              sig[:4] == ["base", "model", "api_key", "messages"], str(sig))
        check("_load_env_key is still module-level and callable", callable(real_key))
        check("_parse_llm_json still strips <think> with no provider (pre-M36 default)",
              _parse_llm_json('<think>x</think>{"category":"refund","rationale":"y"}')
              == {"category": "refund", "rationale": "y"})
    finally:
        _http_chat, _load_env_key = real_http, real_key
        os.chdir(prev_cwd)
        if prev_env is not None:
            os.environ[llm_providers.ENV_VAR] = prev_env
        else:
            os.environ.pop(llm_providers.ENV_VAR, None)
        llm_providers.reset()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    bad = results.count(False)
    if bad:
        print("SELF-TEST FAILED — %d of %d checks failed" % (bad, len(results)))
        return 1

    # ---- honest report of THIS host's live LLM tier -------------------------------
    # The checks above are fully offline and deterministic, so they never depend on a
    # provider being reachable. But run_tests.py still needs to know whether the LIVE
    # classifier on this host has an LLM at all: with no credentials the engine is
    # DEGRADED (heuristic-only) — a supported, working state — never FAILED. The
    # wording below is what the harness keys on, so it must stay accurate.
    live, why = active_providers()
    if live:
        print("LLM tier: %d provider(s) with credentials on this host: %s"
              % (len(live), ", ".join(p.name for p, _ in live)))
    else:
        print("LLM tier: none available on this host (%s) — the live classifier uses the "
              "heuristic fallback. DEGRADED, not FAILED: the engine still routes." % why)
    try:
        src = llm_providers.provider_source() if llm_providers else "n/a"
    except Exception as e:
        src = "unreadable (%s)" % e
    print("provider config source: %s" % src)

    print("PASS — classify_llm self-test: %d/%d checks passed "
          "(provider order, fall-through, zero-provider degrade, reasoning floor + <think>)"
          % (len(results), len(results)))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in ("--demo", "-d"):
        _run_demo()                       # LIVE: real LLM calls
    elif args and args[0] not in ("--selftest",):
        print(json.dumps(classify(" ".join(args)), indent=2))
    else:
        sys.exit(_selftest())             # default: offline, no network
