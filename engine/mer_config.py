#!/usr/bin/env python3
"""
mer_config.py — the profile / identity layer for the Merchandise Returns Engine (Blueprint M32).

WHY THIS EXISTS
---------------
Before M32 the engine had NO config module: the operator's legal name, email, phone, Multica
workspace id, Google token path and Telegram chat id were hardcoded as literals in 13 different
files. That makes the skill un-shippable — a second person cannot run it without editing code,
and every new module invents its own copy of the same literal (which then drifts).

M32 puts identity in exactly one place: a `profile.json` read through this loader. Every module
asks mer_config for who the user is; nobody hardcodes it again.

WHERE THE PROFILE IS FOUND (precedence, first hit wins)
------------------------------------------------------
  1. an explicit path passed to load(path) / profile(path)
  2. $MER_PROFILE
  3. ./profile.json               (the current working directory — run-from-anywhere)
  4. /opt/data/mer_profile.json   (the VPS DEFAULT — a default, never an assumption)

If none of those exist, the loader RAISES. There is no built-in identity and no default user
(there was a transitional one from M32 to M41; it is gone). This is deliberate: the engine sends
real mail and reads a real mailbox, so an unconfigured install must fail loudly and name the fix
(`onboard.py`, or `$MER_PROFILE`) rather than quietly act as whoever the author happened to be.

JURISDICTION IS NEVER DEFAULTED
------------------------------
`state` and `county` have NO fallback at any tier. The skill's rule is "No default. Never assume
a state." — jurisdiction decides the state AG, the BBB region, the small-claims venue and the
damages cap, and guessing it is worse than failing. A profile file that omits them fails to load
with a loud error; with no profile file at all, state()/county() raise on access.

USAGE
-----
    import mer_config
    mer_config.email()            # -> "you@example.com"
    mer_config.legal_name()
    mer_config.state()            # raises MerConfigError if not configured
    p = mer_config.profile()      # the cached singleton (file read once per process)

Stdlib only, on purpose — works from a bare python3, any venv, any working directory.
"""
import json
import os
import sys

__all__ = [
    "MerConfigError", "Profile", "load", "profile", "reset",
    "legal_name", "email", "phone", "mailing_address", "state", "county",
    "multica_workspace_id", "multica_project_id", "notify", "notify_channel",
    "notify_telegram_chat_id", "signature_block", "calendar_id", "google_token_file",
    "profile_path", "PROFILE_SEARCH_PATH", "REQUIRED_FIELDS", "NEVER_DEFAULTED",
    "no_profile_error", "DEFAULT_TOKEN_PATH",
]

# The DEFAULT VPS location. A default, not an assumption — it is the last thing tried.
DEFAULT_PROFILE_PATH = "/opt/data/mer_profile.json"
CWD_PROFILE_NAME = "profile.json"
ENV_VAR = "MER_PROFILE"

# Every field a profile FILE must carry. Missing any of these is a hard, loud failure.
REQUIRED_FIELDS = ("legal_name", "email", "state", "county", "multica_workspace_id")

# Fields that have NO fallback anywhere, ever. Jurisdiction correctness depends on them.
NEVER_DEFAULTED = ("state", "county")

# ---------------------------------------------------------------------------------------------
# NO BUILT-IN IDENTITY (M41).
#
# Until M41 this module carried a transitional FALLBACK dict holding the original author's legal
# name, email, phone, token path and Multica workspace id, so that his live instance kept working
# before a profile file existed. A real profile is now installed, and the engine ships to
# strangers — a built-in person is now purely a hazard, so the block is deleted.
#
# Nothing here is a person. The only defaults left are container paths.
# ---------------------------------------------------------------------------------------------

# Where the OAuth token JSON lives when a profile does not say. A PATH, not an identity —
# it is the same generic location for every install, and it holds no name.
DEFAULT_TOKEN_PATH = "/opt/data/mer_google_token.json"


class MerConfigError(Exception):
    """Raised when the profile is missing, unreadable, or missing a required field."""


def no_profile_error():
    """The error a user sees when nothing is configured. This message IS the product's
    entire user experience of that failure, so it names the searched paths and the fix."""
    env = os.environ.get(ENV_VAR)
    return MerConfigError("\n".join([
        "no profile found — the engine does not know whose identity it is running under,",
        "and it will NOT guess.",
        "",
        "  Searched, in order:",
        "    1. $%s  ->  %s" % (ENV_VAR, env if env else "(not set)"),
        "    2. %s" % os.path.join(os.getcwd(), CWD_PROFILE_NAME),
        "    3. %s" % DEFAULT_PROFILE_PATH,
        "",
        "  There is NO built-in identity to fall back on, by design: an unconfigured engine",
        "  must never send mail from — or read a mailbox as — somebody else's account.",
        "",
        "  Fix (either one):",
        "    * run:  python onboard.py        — interviews you and writes your profile.json",
        "    * or:   set $%s=/path/to/your/profile.json" % ENV_VAR,
        "",
        "  See profile.example.json for the full schema.",
    ]))


# ------------------------------------------------------------------------------- discovery

def PROFILE_SEARCH_PATH(explicit=None):
    """The ordered list of candidate paths, first hit wins. Exposed so errors can name it."""
    out = []
    if explicit:
        out.append(str(explicit))
    env = os.environ.get(ENV_VAR)
    if env:
        out.append(env)
    out.append(os.path.join(os.getcwd(), CWD_PROFILE_NAME))
    out.append(DEFAULT_PROFILE_PATH)
    return out


def _discover(explicit=None):
    """Return the first existing candidate path, or None. An EXPLICIT path that does not exist
    is an error, not a silent fallthrough — the caller asked for that file by name."""
    candidates = PROFILE_SEARCH_PATH(explicit)
    if explicit and not os.path.isfile(str(explicit)):
        raise MerConfigError(
            "profile not found at the path you gave: %s\n"
            "  (searched, in order: %s)" % (explicit, ", ".join(candidates)))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise MerConfigError("profile at %s is not valid JSON: %s" % (path, e))
    except OSError as e:
        raise MerConfigError("cannot read profile at %s: %s" % (path, e))
    if not isinstance(data, dict):
        raise MerConfigError("profile at %s must be a JSON object, got %s"
                             % (path, type(data).__name__))
    return data


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


# ------------------------------------------------------------------------------- the object

class Profile(object):
    """A loaded profile. Accessors are typed and fail loudly rather than guessing."""

    def __init__(self, data, source):
        self._d = dict(data or {})
        self.source = source
        # Kept for callers that still ask; there is no built-in identity any more, so it is
        # always False. A Profile only ever comes from a real file now.
        self.is_fallback = False

    # -- internals ----------------------------------------------------------------
    def _req(self, key):
        v = self._d.get(key)
        if _blank(v):
            raise MerConfigError(
                "required profile field '%s' is missing or empty.\n"
                "  profile source: %s\n"
                "  Fix: add \"%s\" to that file. See profile.example.json for the full schema."
                % (key, self.source, key))
        return v

    def _opt(self, key, default=None):
        v = self._d.get(key)
        return default if _blank(v) else v

    def raw(self):
        """The underlying dict (a copy). For diagnostics only — prefer the accessors."""
        return dict(self._d)

    def get(self, key, default=None):
        return self._d.get(key, default)

    # -- identity -----------------------------------------------------------------
    def legal_name(self):
        """str — the full legal name every letter is signed with."""
        return str(self._req("legal_name")).strip()

    def email(self):
        """str — the mailbox the engine sends from and reads."""
        return str(self._req("email")).strip()

    def phone(self):
        """str — E.164 preferred. '' when not configured (voice escalation then skips)."""
        return str(self._opt("phone", "")).strip()

    def mailing_address(self):
        """str — the postal address used on statutory demand letters. '' when not configured."""
        v = self._opt("mailing_address", "")
        if isinstance(v, (list, tuple)):
            v = "\n".join(str(x) for x in v)
        return str(v).strip()

    # -- jurisdiction (NEVER defaulted) --------------------------------------------
    def state(self):
        """str — full state name. NO DEFAULT. Never assume a state."""
        return str(self._req_jurisdiction("state")).strip()

    def county(self):
        """str — county of residence. NO DEFAULT. Drives small-claims venue."""
        return str(self._req_jurisdiction("county")).strip()

    def _req_jurisdiction(self, key):
        v = self._d.get(key)
        if _blank(v):
            raise MerConfigError(
                "jurisdiction field '%s' is not configured — and it is NEVER defaulted.\n"
                "  profile source: %s\n"
                "  Why this is fatal: state + county decide the state Attorney General, the BBB\n"
                "  region, the small-claims venue and the damages cap. A guessed jurisdiction\n"
                "  sends a real complaint to the wrong regulator. The rule is: no default,\n"
                "  never assume a state.\n"
                "  Fix: set \"state\" and \"county\" in your profile.json." % (key, self.source))
        return v

    # -- Multica ------------------------------------------------------------------
    def multica_workspace_id(self):
        """str — the workspace that holds this user's cases."""
        return str(self._req("multica_workspace_id")).strip()

    def multica_project_id(self):
        """str | None — optional project scope inside the workspace."""
        v = self._opt("multica_project_id")
        return str(v).strip() if v is not None else None

    # -- notification -------------------------------------------------------------
    def notify(self):
        """dict — {'channel': 'telegram'|'none', 'telegram_chat_id': str|None}. Always a dict."""
        raw = self._d.get("notify")
        if _blank(raw):
            return {"channel": "none", "telegram_chat_id": None}
        if not isinstance(raw, dict):
            raise MerConfigError(
                "profile field 'notify' must be an object like "
                "{\"channel\": \"telegram\", \"telegram_chat_id\": \"12345\"} — got %s.\n"
                "  profile source: %s" % (type(raw).__name__, self.source))
        chan = str(raw.get("channel") or "none").strip().lower()
        if chan not in ("telegram", "none"):
            raise MerConfigError(
                "profile field notify.channel must be 'telegram' or 'none', got %r.\n"
                "  profile source: %s" % (chan, self.source))
        cid = raw.get("telegram_chat_id")
        cid = None if _blank(cid) else str(cid).strip()
        if chan == "telegram" and cid is None:
            # Not fatal: the legacy $TELEGRAM_USER_ID env var still supplies it on King's box.
            pass
        return {"channel": chan, "telegram_chat_id": cid}

    def notify_channel(self):
        """str — 'telegram' or 'none'."""
        return self.notify()["channel"]

    def notify_telegram_chat_id(self):
        """str | None — None means "not configured here"; callers may fall back to env."""
        n = self.notify()
        return n["telegram_chat_id"] if n["channel"] == "telegram" else None

    # -- derived ------------------------------------------------------------------
    def signature_block(self):
        """str — how letters are signed. Defaults to 'Legal Name\\nemail' (the pre-M32 format)."""
        v = self._opt("signature_block")
        if v is None:
            return "%s\n%s" % (self.legal_name(), self.email())
        if isinstance(v, (list, tuple)):
            return "\n".join(str(x) for x in v).strip()
        return str(v).strip()

    def calendar_id(self):
        """str — the Google Calendar id deadlines sync to. Defaults to the profile email."""
        return str(self._opt("calendar_id") or self.email()).strip()

    def google_token_file(self):
        """str — path to the OAuth token json. Defaults to the generic container path
        (a location, never an identity). Set `google_token_file` in your profile to move it."""
        return str(self._opt("google_token_file", DEFAULT_TOKEN_PATH)).strip()

    def __repr__(self):
        return "<Profile %s from %s>" % (self._d.get("email") or "(no email)", self.source)


# ------------------------------------------------------------------------------- loading

def load(path=None):
    """Load and validate a profile. Returns a Profile. Does NOT touch the singleton cache."""
    found = _discover(path)
    if found is None:
        raise no_profile_error()
    data = _read(found)
    missing = [k for k in REQUIRED_FIELDS if _blank(data.get(k))]
    if missing:
        raise MerConfigError(
            "profile at %s is missing required field(s): %s\n"
            "  Required: %s\n"
            "  %s are NEVER defaulted — jurisdiction decides the AG, the BBB region and the\n"
            "  small-claims venue, so the engine refuses to guess.\n"
            "  Fix: see profile.example.json for a documented, filled-in example."
            % (found, ", ".join(missing), ", ".join(REQUIRED_FIELDS),
               " and ".join(NEVER_DEFAULTED)))
    return Profile(data, found)


_SINGLETON = None


def profile(path=None):
    """The cached profile singleton, so N modules don't each re-read the file.

    The first call decides. Pass an explicit path (or call reset()) to re-point it.
    """
    global _SINGLETON
    if _SINGLETON is None or path is not None:
        _SINGLETON = load(path)
    return _SINGLETON


def reset():
    """Drop the cached singleton (tests, and any process that rewrites its profile)."""
    global _SINGLETON
    _SINGLETON = None


def profile_path():
    """str — the file the active profile came from. Raises if nothing is configured."""
    return profile().source


# ------------------------------------------------------- module-level convenience accessors
# These are what modules import. Each just delegates to the singleton.

def legal_name():
    return profile().legal_name()


def email():
    return profile().email()


def phone():
    return profile().phone()


def mailing_address():
    return profile().mailing_address()


def state():
    return profile().state()


def county():
    return profile().county()


def multica_workspace_id():
    return profile().multica_workspace_id()


def multica_project_id():
    return profile().multica_project_id()


def notify():
    return profile().notify()


def notify_channel():
    return profile().notify_channel()


def notify_telegram_chat_id():
    return profile().notify_telegram_chat_id()


def signature_block():
    return profile().signature_block()


def calendar_id():
    return profile().calendar_id()


def google_token_file():
    return profile().google_token_file()


# ------------------------------------------------------------------------------- self-test

if __name__ == "__main__":
    import shutil
    import tempfile

    results = []

    def check(desc, cond, detail=""):
        results.append(bool(cond))
        print("%s — %s%s" % ("PASS" if cond else "FAIL", desc,
                             ("  [%s]" % detail) if detail and not cond else ""))

    tmp = tempfile.mkdtemp(prefix="mer_config_test_")
    prev_cwd = os.getcwd()
    prev_env = os.environ.pop(ENV_VAR, None)
    try:
        good = {
            "legal_name": "Jordan A. Rivera",
            "email": "jordan.rivera@example.com",
            "phone": "+15125550147",
            "mailing_address": "4120 Cedar Ridge Ln, Apt 5\nAustin, TX 78745",
            "state": "Texas",
            "county": "Travis",
            "multica_workspace_id": "ws-0000-1111",
            "multica_project_id": "pj-2222",
            "notify": {"channel": "telegram", "telegram_chat_id": "555000111"},
            "signature_block": "Jordan A. Rivera\njordan.rivera@example.com\n(512) 555-0147",
        }
        p_explicit = os.path.join(tmp, "explicit.json")
        with open(p_explicit, "w", encoding="utf-8") as f:
            json.dump(good, f)

        # 1) explicit path loads and typed accessors return what the file says
        pr = load(p_explicit)
        check("explicit path loads", pr.source == p_explicit)
        check("legal_name accessor", pr.legal_name() == "Jordan A. Rivera")
        check("email accessor", pr.email() == "jordan.rivera@example.com")
        check("phone accessor", pr.phone() == "+15125550147")
        check("mailing_address accessor", pr.mailing_address().startswith("4120 Cedar Ridge"))
        check("state accessor", pr.state() == "Texas")
        check("county accessor", pr.county() == "Travis")
        check("multica_workspace_id accessor", pr.multica_workspace_id() == "ws-0000-1111")
        check("multica_project_id accessor", pr.multica_project_id() == "pj-2222")
        check("notify channel", pr.notify_channel() == "telegram")
        check("notify telegram chat id", pr.notify_telegram_chat_id() == "555000111")
        check("signature_block from file", pr.signature_block().endswith("(512) 555-0147"))
        check("calendar_id defaults to email", pr.calendar_id() == "jordan.rivera@example.com")

        # 2) signature_block derives 'name\nemail' when absent (the pre-M32 letter format)
        no_sig = dict(good)
        no_sig.pop("signature_block")
        check("signature_block derived when absent",
              Profile(no_sig, "test").signature_block()
              == "Jordan A. Rivera\njordan.rivera@example.com")

        # 3) missing jurisdiction is a LOUD failure, never a silent default
        for field in NEVER_DEFAULTED:
            bad = dict(good)
            bad.pop(field)
            p_bad = os.path.join(tmp, "no_%s.json" % field)
            with open(p_bad, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            try:
                load(p_bad)
                check("missing '%s' rejected" % field, False, "load() returned instead of raising")
            except MerConfigError as e:
                check("missing '%s' rejected loudly" % field, field in str(e))

        # 4) missing identity fields are rejected too
        bad = dict(good)
        bad["email"] = "   "
        p_bad = os.path.join(tmp, "blank_email.json")
        with open(p_bad, "w", encoding="utf-8") as f:
            json.dump(bad, f)
        try:
            load(p_bad)
            check("blank required field rejected", False, "load() returned instead of raising")
        except MerConfigError as e:
            check("blank required field rejected", "email" in str(e))

        # 5) an explicit path that does not exist is an error, not a silent fallthrough
        try:
            load(os.path.join(tmp, "nope.json"))
            check("nonexistent explicit path rejected", False)
        except MerConfigError:
            check("nonexistent explicit path rejected", True)

        # 6) malformed JSON fails clearly
        p_junk = os.path.join(tmp, "junk.json")
        with open(p_junk, "w", encoding="utf-8") as f:
            f.write("{not json")
        try:
            load(p_junk)
            check("malformed JSON rejected", False)
        except MerConfigError as e:
            check("malformed JSON rejected", "not valid JSON" in str(e))

        # 7) precedence: $MER_PROFILE beats ./profile.json
        cwd_dir = os.path.join(tmp, "work")
        os.makedirs(cwd_dir)
        cwd_prof = dict(good)
        cwd_prof["email"] = "cwd@example.com"
        with open(os.path.join(cwd_dir, CWD_PROFILE_NAME), "w", encoding="utf-8") as f:
            json.dump(cwd_prof, f)
        os.chdir(cwd_dir)
        check("./profile.json is found from cwd", load().email() == "cwd@example.com")
        os.environ[ENV_VAR] = p_explicit
        check("$MER_PROFILE outranks ./profile.json",
              load().email() == "jordan.rivera@example.com")
        check("explicit arg outranks $MER_PROFILE",
              load(os.path.join(cwd_dir, CWD_PROFILE_NAME)).email() == "cwd@example.com")
        os.environ.pop(ENV_VAR, None)

        # 8) THE CONTRACT (M41): no profile + no env => a clear, catchable error, never a
        #    built-in person. This replaces the old "fallback equivalence" assertions, which
        #    asserted the opposite (that a hardcoded identity was returned). Each property the
        #    old block checked has a counterpart here.
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        os.chdir(empty)

        # Isolate from an ambient profile. `DEFAULT_PROFILE_PATH` is a real file on any machine
        # where the engine is actually configured — including the VPS this runs on and every
        # real user's install — so without this the "no profile" assertions below would fail for
        # exactly the people who set the product up correctly. The test would then be RED on a
        # healthy system and GREEN only on a broken one, which is worse than no test at all.
        _real_default = DEFAULT_PROFILE_PATH
        DEFAULT_PROFILE_PATH = os.path.join(empty, "definitely-not-here.json")

        err = None
        try:
            got = load()
            check("no profile -> raises instead of returning an identity", False,
                  "load() returned %r" % (got,))
        except MerConfigError as e:
            err = str(e)
            check("no profile -> raises instead of returning an identity", True)

        if err is not None:
            # The error must be USEFUL: it names both fixes and every path it searched.
            check("error names onboard.py", "onboard.py" in err)
            check("error names $%s" % ENV_VAR, ENV_VAR in err)
            check("error names ./%s" % CWD_PROFILE_NAME, CWD_PROFILE_NAME in err)
            check("error names the default profile path", DEFAULT_PROFILE_PATH in err)
            check("error states there is no built-in identity",
                  "NO built-in identity" in err)
            check("error points at the schema example", "profile.example.json" in err)
            # And it must be an EMPTY error — no person may appear in it.
            check("error contains no email address", "@" not in err)

        # 8b) NO built-in identity survives anywhere in this module's source or namespace.
        #     A literal person reintroduced as a default would be caught here.
        mod_src = open(os.path.abspath(__file__), encoding="utf-8").read()
        import re as _re
        # Any address-shaped literal, any E.164-shaped literal, any UUID-shaped literal.
        leaks = (_re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", mod_src)
                 + _re.findall(r"\+\d{10,15}", mod_src)
                 + _re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                               mod_src))
        # The fictional example user in this file's own test data is expected and harmless.
        leaks = [x for x in leaks if "example.com" not in x and x != good["phone"]]
        check("no personal literal remains in mer_config.py source", not leaks,
              "found %s" % leaks)
        check("no FALLBACK identity symbol remains",
              not hasattr(sys.modules[__name__], "FALLBACK"))
        check("the only token default is a generic container path",
              DEFAULT_TOKEN_PATH == "/opt/data/mer_google_token.json")

        # 8c) every module-level accessor fails the same way — none of them invents a value.
        reset()
        for name in ("email", "legal_name", "phone", "state", "county",
                     "multica_workspace_id", "google_token_file", "signature_block",
                     "calendar_id", "profile_path"):
            fn = globals()[name]
            try:
                v = fn()
                check("%s() refuses to invent a value" % name, False, "returned %r" % (v,))
            except MerConfigError:
                check("%s() refuses to invent a value" % name, True)
            reset()

        DEFAULT_PROFILE_PATH = _real_default      # restore before the profile-present checks
        reset()

        # 8d) profile PRESENT still resolves to the profile's own values (the live-instance path).
        pr2 = load(p_explicit)
        check("profile present -> profile's email wins", pr2.email() == good["email"])
        check("profile present -> profile's legal_name wins",
              pr2.legal_name() == good["legal_name"])
        check("profile present -> profile's workspace wins",
              pr2.multica_workspace_id() == good["multica_workspace_id"])
        check("profile present -> signature is name\\nemail",
              pr2.signature_block().startswith(good["legal_name"]))
        no_tok = dict(good)
        no_tok.pop("google_token_file", None)
        check("token path defaults to the generic container path, not a person",
              Profile(no_tok, "test").google_token_file() == DEFAULT_TOKEN_PATH)

        # 8e) jurisdiction is still never invented, even from a valid profile object.
        no_juris = {k: v for k, v in good.items() if k not in NEVER_DEFAULTED}
        stub = Profile(no_juris, "test")
        try:
            stub.state()
            check("state() still refuses to invent a state", False, "state() returned a value")
        except MerConfigError as e:
            check("state() still refuses to invent a state", "never assume a state" in str(e))
        try:
            stub.county()
            check("county() still refuses to invent a county", False)
        except MerConfigError:
            check("county() still refuses to invent a county", True)

        # 9) singleton: the file is read once and shared
        reset()
        a = profile(p_explicit)
        b = profile()
        check("profile() returns the same object (singleton)", a is b)
        check("module-level accessor delegates to singleton",
              email() == "jordan.rivera@example.com")
        reset()
        check("reset() clears the singleton", _SINGLETON is None)

        # 10) the shipped example files parse and validate.
        #
        # profile.example.json is REQUIRED — it is the schema every new user copies, so its
        # absence is a broken package. Any *_profile.json.example is an OPERATOR template: a
        # particular person's own filled-in profile, deliberately excluded from the distributed
        # package (it contains their real identity). Requiring one would make a correctly
        # sanitised package fail its own test suite — which it did, on the first fresh clone:
        # 18 passed, 1 FAILED, and the failure was the packaging working as intended.
        here = os.path.dirname(os.path.abspath(__file__))
        examples = ["profile.example.json"]
        examples += sorted(f for f in os.listdir(here)
                           if f.endswith("_profile.json.example"))
        for ex in examples:
            path = os.path.join(here, ex)
            if not os.path.isfile(path):
                check("%s exists" % ex, ex != "profile.example.json",
                      "not found (required)" if ex == "profile.example.json"
                      else "not found (optional operator template — fine)")
                continue
            try:
                exp = load(path)
                check("%s loads and validates" % ex,
                      bool(exp.legal_name() and exp.email() and exp.state() and exp.county()))
            except MerConfigError as e:
                check("%s loads and validates" % ex, False, str(e).splitlines()[0])
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
    print("PASS — mer_config self-test: %d/%d checks passed" % (len(results), len(results)))
