#!/usr/bin/env python3
"""
llm_providers.py — the provider/model configuration layer for the engine's LLM tier
(Blueprint M36).

WHY THIS EXISTS
---------------
Before M36 the LLM tier was hardwired: `classify_llm.py` carried NANOGPT_BASE /
NANOGPT_MODEL / MINIMAX_BASE / MINIMAX_MODEL as literals and looked up exactly two
env vars, NANOGPT_API_KEY and MINIMAX_API_KEY. A person handed this skill has NEITHER
key, and no way to point the engine at their own provider without editing code. That
is the same un-shippable shape M32 fixed for identity — a personal literal baked into
a module — so M36 fixes it the same way: **the provider list is configuration.**

WHAT A PROVIDER IS
------------------
An ordered LIST of OpenAI-compatible chat endpoints, tried top to bottom. Each entry:

    {
      "name":        "nanogpt/GLM",                 # label shown in rationales/logs
      "base_url":    "https://nano-gpt.com/api/v1", # /chat/completions is appended
      "model":       "zai-org/glm-5.2",
      "api_key_env": "NANOGPT_API_KEY",             # env var (or .env key) holding the key
      "max_tokens":  1500,                          # optional
      "reasoning":   false,                         # optional — see below
      "strip_think": true,                          # optional — see below
      "temperature": 0,                             # optional
      "timeout":     40,                            # optional, seconds
      "extra":       {}                             # optional, merged into the request body
    }

A provider whose `api_key_env` names a variable that is not set is SKIPPED — exactly as
the two hardcoded providers were skipped before M36. `api_key_env` may be null/omitted
for a keyless endpoint (a local Ollama / llama.cpp server), which is then always tried.

REASONING MODELS ARE CONFIGURATION, NOT A SPECIAL CASE
-----------------------------------------------------
MiniMax-M3 is a reasoning model: it spends tokens on a `<think>...</think>` block before
the JSON answer. With max_tokens=200 the answer was truncated away and every reply
classified as garbage — a real bug that took real debugging. The fix used to be two
hardcoded lines. Now it is two per-provider flags:

  * "reasoning": true  -> max_tokens is floored at REASONING_MIN_TOKENS (1500). A config
                          that asks for less is raised, not honored — the floor is the fix.
  * "strip_think": true-> a `<think>` block (closed OR truncated open) is stripped from
                          the response before parsing. DEFAULTS TO TRUE for every provider,
                          because that is what the pre-M36 code did unconditionally and
                          stripping a block that is not there is a no-op.

So the next reasoning model is a config entry, not a patch.

WHERE THE PROVIDER LIST COMES FROM (precedence, first hit wins)
---------------------------------------------------------------
  1. an explicit list/path passed to providers(...)
  2. $MER_LLM_PROVIDERS — inline JSON, a path to a JSON file, or "none" for zero providers
  3. the profile's "llm_providers" key (mer_config — same profile.json as identity)
  4. ./llm_providers.json          (current working directory — run-from-anywhere)
  5. /opt/data/mer_llm_providers.json  (the VPS DEFAULT — a default, never an assumption)
  6. DEFAULT_PROVIDERS below — King's live NanoGPT + MiniMax ladder, so an instance with
     NO configuration at all behaves byte-identically to pre-M36.

ZERO PROVIDERS IS A VALID, SUPPORTED STATE
------------------------------------------
Configuring `[]` (or $MER_LLM_PROVIDERS=none), or configuring providers whose keys are
all absent, is NOT an error. The caller degrades to the deterministic heuristic in
reply_classify.py and the engine keeps routing. A user with no LLM credentials at all
gets a working, DEGRADED engine — never a FAILED one.

Stdlib only, on purpose.
"""
import json
import os
import sys

__all__ = [
    "LLMProviderError", "LLMProvider", "DEFAULT_PROVIDERS", "DEFAULT_SOURCE",
    "REASONING_MIN_TOKENS", "DEFAULT_MAX_TOKENS", "DEFAULT_TIMEOUT", "DEFAULT_TEMPERATURE",
    "ENV_VAR", "CWD_PROVIDERS_NAME", "DEFAULT_PROVIDERS_PATH", "PROFILE_KEY",
    "parse", "providers", "provider_source", "reset", "SEARCH_PATH",
]

# --- knobs -------------------------------------------------------------------------
# Both defaults are the pre-M36 literals. Do not "tidy" them: 1500 is the number that
# fixed the MiniMax-M3 truncation bug, and 40s is the timeout the live cron runs on.
DEFAULT_MAX_TOKENS = 1500
REASONING_MIN_TOKENS = 1500   # hard floor for reasoning:true providers
DEFAULT_TIMEOUT = 40          # seconds per call
DEFAULT_TEMPERATURE = 0

ENV_VAR = "MER_LLM_PROVIDERS"
CWD_PROVIDERS_NAME = "llm_providers.json"
DEFAULT_PROVIDERS_PATH = "/opt/data/mer_llm_providers.json"
PROFILE_KEY = "llm_providers"

# ---------------------------------------------------------------------------------------------
# THE SHIPPED DEFAULT LADDER (pre-M36 behavior, verbatim).
#
# These are the exact base urls, model ids, env var names and token limits that were hardcoded
# in classify_llm.py before M36. Keeping them here means King's live instance, which has no
# provider config at all, resolves to precisely the ladder it ran yesterday.
#
# NOTE what is NOT here: an API KEY. Only the NAME of the env var that holds one. Keys live in
# .env and nowhere else.
# ---------------------------------------------------------------------------------------------
DEFAULT_PROVIDERS = [
    {
        "name": "nanogpt/GLM",
        "base_url": "https://nano-gpt.com/api/v1",
        "model": "zai-org/glm-5.2",
        "api_key_env": "NANOGPT_API_KEY",
        "max_tokens": 1500,
        "reasoning": False,
    },
    {
        "name": "MiniMax-M3",
        "base_url": "https://api.minimax.io/v1",
        "model": "MiniMax-M3",
        "api_key_env": "MINIMAX_API_KEY",
        "max_tokens": 1500,
        # MiniMax-M3 emits <think> before the JSON and needs room for it. This flag, not a
        # hardcoded `if model == "MiniMax-M3"`, is what enforces that.
        "reasoning": True,
    },
]

DEFAULT_SOURCE = "<built-in default ladder (no provider config found)>"

_TRUE = ("1", "true", "yes", "on")
_NONE_WORDS = ("none", "off", "disabled", "[]")


class LLMProviderError(Exception):
    """Raised when a provider list is present but malformed. Callers degrade, never crash."""


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _as_bool(v, default):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _TRUE


def _as_int(v, default, field, where):
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise LLMProviderError("provider %s: '%s' must be a number, got %r" % (where, field, v))


class LLMProvider(object):
    """One configured OpenAI-compatible chat endpoint."""

    __slots__ = ("name", "base_url", "model", "api_key_env", "max_tokens",
                 "reasoning", "strip_think", "temperature", "timeout", "extra")

    def __init__(self, name, base_url, model, api_key_env=None, max_tokens=None,
                 reasoning=False, strip_think=True, temperature=DEFAULT_TEMPERATURE,
                 timeout=DEFAULT_TIMEOUT, extra=None):
        self.name = name
        self.base_url = base_url
        self.model = model
        self.api_key_env = api_key_env
        self.reasoning = bool(reasoning)
        self.strip_think = bool(strip_think)
        self.temperature = temperature
        self.timeout = timeout
        self.extra = dict(extra or {})
        mt = DEFAULT_MAX_TOKENS if max_tokens is None else int(max_tokens)
        # The reasoning floor. A reasoning model given 200 tokens spends them all on <think>
        # and returns nothing parseable; that is the M11 bug. Config cannot re-introduce it.
        if self.reasoning and mt < REASONING_MIN_TOKENS:
            mt = REASONING_MIN_TOKENS
        self.max_tokens = mt

    def as_dict(self):
        return {
            "name": self.name, "base_url": self.base_url, "model": self.model,
            "api_key_env": self.api_key_env, "max_tokens": self.max_tokens,
            "reasoning": self.reasoning, "strip_think": self.strip_think,
            "temperature": self.temperature, "timeout": self.timeout,
            "extra": dict(self.extra),
        }

    def chat_url(self):
        return str(self.base_url).rstrip("/") + "/chat/completions"

    def body(self, messages):
        """The JSON request body for this provider. `extra` is merged LAST but may not
        override the model or the messages."""
        out = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        for k, v in self.extra.items():
            if k in ("model", "messages"):
                continue
            out[k] = v
        return out

    def __repr__(self):
        return "<LLMProvider %s model=%s key_env=%s max_tokens=%d%s>" % (
            self.name, self.model, self.api_key_env, self.max_tokens,
            " reasoning" if self.reasoning else "")


# ------------------------------------------------------------------------------- parsing

def parse(raw, source="<inline>"):
    """Validate a raw provider list (list of dicts) into [LLMProvider].

    An empty list is legal and returns []. Anything else malformed raises
    LLMProviderError with a message that names the source, so a typo is loud.
    """
    if isinstance(raw, dict):
        # tolerate {"llm_providers": [...]} as well as a bare list
        raw = raw.get(PROFILE_KEY, raw.get("providers"))
    if raw is None:
        raise LLMProviderError("provider config at %s has no provider list" % source)
    if not isinstance(raw, (list, tuple)):
        raise LLMProviderError(
            "provider config at %s must be a JSON array of provider objects, got %s"
            % (source, type(raw).__name__))
    out = []
    seen = set()
    for i, item in enumerate(raw):
        where = "#%d in %s" % (i + 1, source)
        if not isinstance(item, dict):
            raise LLMProviderError("provider %s must be an object, got %s"
                                   % (where, type(item).__name__))
        name = item.get("name")
        base = item.get("base_url") or item.get("base")
        model = item.get("model")
        for field, val in (("base_url", base), ("model", model)):
            if _blank(val):
                raise LLMProviderError(
                    "provider %s is missing required field '%s'.\n"
                    "  Each entry needs at least: {\"name\", \"base_url\", \"model\", "
                    "\"api_key_env\"}.\n"
                    "  See scripts/vps/README.md -> 'LLM provider configuration'."
                    % (where, field))
        if _blank(name):
            name = str(model).strip()
        name = str(name).strip()
        if name in seen:
            name = "%s#%d" % (name, i + 1)   # keep labels unique for logs/rationales
        seen.add(name)
        key_env = item.get("api_key_env")
        key_env = None if _blank(key_env) else str(key_env).strip()
        extra = item.get("extra") or item.get("extra_params") or {}
        if not isinstance(extra, dict):
            raise LLMProviderError("provider %s: 'extra' must be an object, got %s"
                                   % (where, type(extra).__name__))
        reasoning = _as_bool(item.get("reasoning"), False)
        out.append(LLMProvider(
            name=name,
            base_url=str(base).strip(),
            model=str(model).strip(),
            api_key_env=key_env,
            max_tokens=_as_int(item.get("max_tokens"), None, "max_tokens", where),
            reasoning=reasoning,
            # Defaults to True for EVERY provider: that is what the pre-M36 parser did
            # unconditionally, and stripping an absent <think> block is a no-op.
            strip_think=_as_bool(item.get("strip_think"), True),
            temperature=item.get("temperature", DEFAULT_TEMPERATURE),
            timeout=_as_int(item.get("timeout"), DEFAULT_TIMEOUT, "timeout", where),
            extra=extra,
        ))
    return out


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise LLMProviderError("provider config at %s is not valid JSON: %s" % (path, e))
    except OSError as e:
        raise LLMProviderError("cannot read provider config at %s: %s" % (path, e))


def _from_profile():
    """(raw_list, source) from the identity profile, or (None, None).

    Deliberately defensive: a broken/absent profile must not take the LLM tier down.
    mer_config raises loudly for missing jurisdiction etc. — that is its business, not ours.
    """
    try:
        import mer_config
    except Exception:
        return None, None
    try:
        prof = mer_config.profile()
        raw = prof.get(PROFILE_KEY)
        if raw is None:
            return None, None
        return raw, "profile %s (key '%s')" % (prof.source, PROFILE_KEY)
    except Exception:
        return None, None


def SEARCH_PATH(explicit=None):
    """The ordered list of places a provider list can come from. Exposed for diagnostics."""
    out = []
    if explicit is not None:
        out.append("explicit argument")
    out.append("$%s" % ENV_VAR)
    out.append("profile key '%s'" % PROFILE_KEY)
    out.append(os.path.join(os.getcwd(), CWD_PROVIDERS_NAME))
    out.append(DEFAULT_PROVIDERS_PATH)
    out.append(DEFAULT_SOURCE)
    return out


def _discover(explicit=None):
    """Return (raw, source). Never returns None — the built-in ladder is the last resort."""
    # 1. explicit
    if explicit is not None:
        if isinstance(explicit, str):
            if not os.path.isfile(explicit):
                raise LLMProviderError(
                    "provider config not found at the path you gave: %s" % explicit)
            return _read_json_file(explicit), explicit
        return explicit, "<explicit argument>"

    # 2. $MER_LLM_PROVIDERS — inline JSON, a file path, or an explicit "none"
    env = os.environ.get(ENV_VAR)
    if env is not None and env.strip():
        s = env.strip()
        if s.lower() in _NONE_WORDS:
            return [], "$%s=%s (LLM tier disabled)" % (ENV_VAR, s)
        if s[0] in "[{":
            try:
                return json.loads(s), "$%s (inline JSON)" % ENV_VAR
            except json.JSONDecodeError as e:
                raise LLMProviderError("$%s is not valid JSON: %s" % (ENV_VAR, e))
        if not os.path.isfile(s):
            raise LLMProviderError(
                "$%s points at a file that does not exist: %s\n"
                "  (set it to a path, to inline JSON, or to 'none' to disable the LLM tier)"
                % (ENV_VAR, s))
        return _read_json_file(s), "$%s -> %s" % (ENV_VAR, s)

    # 3. the identity profile
    raw, src = _from_profile()
    if raw is not None:
        return raw, src

    # 4/5. a providers file, cwd first then the VPS default
    for path in (os.path.join(os.getcwd(), CWD_PROVIDERS_NAME), DEFAULT_PROVIDERS_PATH):
        if os.path.isfile(path):
            return _read_json_file(path), path

    # 6. the shipped ladder — pre-M36 behavior, unchanged
    return DEFAULT_PROVIDERS, DEFAULT_SOURCE


_CACHE = None      # (list, source)


def providers(explicit=None, use_cache=True):
    """Return the configured [LLMProvider], in the order they must be tried.

    Raises LLMProviderError only when a provider list IS configured but is malformed.
    Callers (classify_llm) catch that and degrade to the heuristic — a bad config must
    never take the engine down, but it must never be silent either.
    """
    global _CACHE
    if explicit is None and use_cache and _CACHE is not None:
        return list(_CACHE[0])
    raw, source = _discover(explicit)
    plist = parse(raw, source)
    if explicit is None and use_cache:
        _CACHE = (plist, source)
    return list(plist)


def provider_source(explicit=None):
    """str — where the active provider list came from."""
    if explicit is None and _CACHE is not None:
        return _CACHE[1]
    raw, source = _discover(explicit)
    parse(raw, source)
    if explicit is None:
        globals()["_CACHE"] = (parse(raw, source), source)
    return source


def reset():
    """Drop the cached provider list (tests, and any process that rewrites its config)."""
    global _CACHE
    _CACHE = None


# ------------------------------------------------------------------------------- self-test

if __name__ == "__main__":
    import shutil
    import tempfile

    results = []

    def check(desc, cond, detail=""):
        results.append(bool(cond))
        print("%s — %s%s" % ("PASS" if cond else "FAIL", desc,
                             ("  [%s]" % detail) if detail and not cond else ""))

    tmp = tempfile.mkdtemp(prefix="llm_providers_test_")
    prev_cwd = os.getcwd()
    prev_env = os.environ.pop(ENV_VAR, None)
    try:
        os.chdir(tmp)          # no ./llm_providers.json here
        reset()

        # 1) no config anywhere -> the shipped pre-M36 ladder, in order
        ps = providers(use_cache=False)
        check("no config -> 2 default providers", len(ps) == 2, repr(ps))
        check("default #1 is nanogpt/GLM", ps[0].name == "nanogpt/GLM")
        check("default #1 base", ps[0].base_url == "https://nano-gpt.com/api/v1")
        check("default #1 model", ps[0].model == "zai-org/glm-5.2")
        check("default #1 key env", ps[0].api_key_env == "NANOGPT_API_KEY")
        check("default #2 is MiniMax-M3", ps[1].name == "MiniMax-M3")
        check("default #2 base", ps[1].base_url == "https://api.minimax.io/v1")
        check("default #2 key env", ps[1].api_key_env == "MINIMAX_API_KEY")
        check("both defaults keep max_tokens=1500 (the M11 truncation fix)",
              ps[0].max_tokens == 1500 and ps[1].max_tokens == 1500)
        check("MiniMax is flagged as a reasoning model", ps[1].reasoning is True)
        check("<think> stripping defaults ON for every provider",
              ps[0].strip_think and ps[1].strip_think)

        # 2) the reasoning floor cannot be configured away
        p = parse([{"name": "r", "base_url": "http://x/v1", "model": "m",
                    "reasoning": True, "max_tokens": 200}], "test")[0]
        check("reasoning provider floors max_tokens at 1500", p.max_tokens == 1500,
              str(p.max_tokens))
        p2 = parse([{"name": "n", "base_url": "http://x/v1", "model": "m",
                     "max_tokens": 200}], "test")[0]
        check("NON-reasoning provider keeps its configured max_tokens", p2.max_tokens == 200)

        # 3) an empty list is legal (a user with no credentials), not an error
        check("empty provider list parses to []", parse([], "test") == [])
        os.environ[ENV_VAR] = "none"
        check("$MER_LLM_PROVIDERS=none -> zero providers",
              providers(use_cache=False) == [])
        os.environ.pop(ENV_VAR)

        # 4) inline JSON via env
        os.environ[ENV_VAR] = json.dumps([{"name": "local", "base_url": "http://127.0.0.1:11434/v1",
                                           "model": "llama3", "api_key_env": None}])
        ps = providers(use_cache=False)
        check("inline JSON env config is honored", len(ps) == 1 and ps[0].name == "local")
        check("keyless provider is allowed (api_key_env None)", ps[0].api_key_env is None)
        os.environ.pop(ENV_VAR)

        # 5) a file path via env, and order is preserved exactly as written
        pfile = os.path.join(tmp, "custom.json")
        with open(pfile, "w", encoding="utf-8") as f:
            json.dump([{"name": "A", "base_url": "http://a/v1", "model": "ma"},
                       {"name": "B", "base_url": "http://b/v1", "model": "mb"},
                       {"name": "C", "base_url": "http://c/v1", "model": "mc"}], f)
        os.environ[ENV_VAR] = pfile
        ps = providers(use_cache=False)
        check("file config via env is honored", len(ps) == 3)
        check("provider ORDER is preserved", [x.name for x in ps] == ["A", "B", "C"])
        os.environ.pop(ENV_VAR)

        # 6) ./llm_providers.json is picked up from the cwd
        with open(os.path.join(tmp, CWD_PROVIDERS_NAME), "w", encoding="utf-8") as f:
            json.dump({"llm_providers": [{"name": "cwd", "base_url": "http://cwd/v1",
                                          "model": "mc"}]}, f)
        ps = providers(use_cache=False)
        check("./llm_providers.json is found from cwd", len(ps) == 1 and ps[0].name == "cwd")
        check("wrapper object {'llm_providers': [...]} is accepted", ps[0].model == "mc")

        # 7) $MER_LLM_PROVIDERS outranks ./llm_providers.json
        os.environ[ENV_VAR] = pfile
        check("$%s outranks ./%s" % (ENV_VAR, CWD_PROVIDERS_NAME),
              [x.name for x in providers(use_cache=False)] == ["A", "B", "C"])
        os.environ.pop(ENV_VAR)
        os.remove(os.path.join(tmp, CWD_PROVIDERS_NAME))

        # 8) malformed config is LOUD, never silently ignored
        for bad, why in (
            ([{"name": "x"}], "missing base_url/model"),
            ("not a list", "not a list"),
            ([{"name": "x", "base_url": "http://x/v1", "model": "m", "extra": 5}], "bad extra"),
            ([{"name": "x", "base_url": "http://x/v1", "model": "m",
               "max_tokens": "lots"}], "bad max_tokens"),
        ):
            try:
                parse(bad, "test")
                check("malformed config rejected (%s)" % why, False, "parse() returned")
            except LLMProviderError:
                check("malformed config rejected (%s)" % why, True)

        # 9) the request body a default provider builds == the pre-M36 hardcoded body
        msgs = [{"role": "user", "content": "hi"}]
        legacy = {"model": "zai-org/glm-5.2", "messages": msgs, "temperature": 0,
                  "max_tokens": 1500}
        reset()
        ps = providers(use_cache=False)
        check("default provider body is byte-identical to pre-M36",
              ps[0].body(msgs) == legacy, json.dumps(ps[0].body(msgs)))
        check("chat_url appends /chat/completions",
              ps[0].chat_url() == "https://nano-gpt.com/api/v1/chat/completions")

        # 10) extra params are merged but cannot hijack model/messages
        p = parse([{"name": "e", "base_url": "http://e/v1", "model": "m",
                    "extra": {"top_p": 0.9, "model": "EVIL", "messages": []}}], "test")[0]
        b = p.body(msgs)
        check("extra params are merged", b.get("top_p") == 0.9)
        check("extra cannot override model", b["model"] == "m")
        check("extra cannot override messages", b["messages"] == msgs)

        # 11) duplicate names stay distinguishable in logs
        ps = parse([{"name": "dup", "base_url": "http://a/v1", "model": "m"},
                    {"name": "dup", "base_url": "http://b/v1", "model": "m"}], "test")
        check("duplicate provider names are disambiguated", ps[0].name != ps[1].name,
              "%s / %s" % (ps[0].name, ps[1].name))
    finally:
        os.chdir(prev_cwd)
        if prev_env is not None:
            os.environ[ENV_VAR] = prev_env
        else:
            os.environ.pop(ENV_VAR, None)
        reset()
        shutil.rmtree(tmp, ignore_errors=True)

    bad_count = results.count(False)
    if bad_count:
        print("SELF-TEST FAILED — %d of %d checks failed" % (bad_count, len(results)))
        sys.exit(1)
    print("PASS — llm_providers self-test: %d/%d checks passed" % (len(results), len(results)))
