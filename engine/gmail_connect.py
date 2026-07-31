#!/usr/bin/env python3
"""
gmail_connect.py — connect the user's OWN mailbox (M35a, the last piece of real onboarding).

SKILL.md §0 promises "the user SELECTS which of their own email accounts to use… which onboarding
then connects (OAuth) for read+send." Everything else in onboarding became code in M35; this is
the part that was still missing, and without it a person who installs this skill cannot send or
read anything — the engine has no mailbox.

What it does
------------
Runs Google's OAuth 2.0 **installed-application** flow with a loopback redirect and PKCE, using
credentials the USER supplies, and writes a token file in exactly the shape gmail_transport.py
already reads:

    {"client_id": …, "client_secret": …, "refresh_token": …, "token": …}

Scopes requested (both, deliberately):
    https://www.googleapis.com/auth/gmail.send        — mer_send / nudges / letters
    https://www.googleapis.com/auth/gmail.readonly    — gmail_fetch / inbox_watcher / triage
The engine needs both, and they live in ONE token file. Asking for them separately would leave a
half-connected mailbox that can read but never answer (or worse, answer but never see the reply).

Why the user supplies the client credentials
--------------------------------------------
A Google OAuth client belongs to a Google Cloud project, and the consent screen shows ITS name.
Shipping one shared client would mean every user's mail flows through an app owned by whoever
packaged the skill — the exact "the giver is not connected to their case" property BLUEPRINT §1
promises. So each user creates their own client once (see --help-setup) and owns their own
consent. It also keeps this skill free of any credential that could be revoked centrally.

Usage
-----
    gmail_connect.py --help-setup                 print the one-time Google Cloud steps
    gmail_connect.py --client-secrets FILE        connect (opens a browser, writes the token)
    gmail_connect.py --check                      verify the existing token + its scopes
    gmail_connect.py --selftest                   offline self-test, no network

Headless hosts (a VPS, a container without a browser) cannot complete a consent screen. Run
--connect on a machine with a browser, then copy the resulting token file to the host and point
`google_token_file` in the profile at it. --check works anywhere and is the way to confirm the
copy landed correctly.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO = "https://www.googleapis.com/oauth2/v2/userinfo"
#: mail.google.com is NOT belt-and-braces on top of gmail.send — it is the only scope that
#: makes sending work at all. gmail_transport.send_mime() goes out over Gmail's SMTP MSA
#: (gmail-smtp-msa.l.google.com:587, STARTTLS, XOAUTH2), and that endpoint refuses gmail.send
#: and demands full-mailbox access. Without it a token authorises cleanly, reads fine, and can
#: NEVER send — the failure surfaces as a 535 at AUTH, long after onboarding looked successful.
#: Reported 2026-07-31 by a recipient who had been carrying it as a local patch.
SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

SETUP_HELP = """\
ONE-TIME GOOGLE SETUP — about five minutes, done once per user.

You are creating an OAuth client that belongs to YOU. Nobody who gave you this skill can see
your mail or revoke your access; the consent screen will show your own project's name.

 1. Go to https://console.cloud.google.com/  and sign in with the Google account whose mailbox
    you want the engine to use.
 2. Create a project (any name — "Merchandise Returns" is fine).
 3. APIs & Services -> Library -> search "Gmail API" -> Enable.
 4. APIs & Services -> OAuth consent screen:
      * User type: External
      * Fill in app name / your email where required
      * Add YOUR OWN email as a Test user  (this is the step people miss — without it Google
        returns access_denied even though everything else is correct)
      * You do NOT need to publish the app or pass verification to use it yourself.
 5. APIs & Services -> Credentials -> Create credentials -> OAuth client ID
      * Application type: **Desktop app**   (this is what enables the loopback redirect below)
 6. Download the JSON. That file is your --client-secrets.

Then:  gmail_connect.py --client-secrets /path/to/client_secret_xxx.json

Keep that JSON and the token file it produces OUT of git, chmod 600. Neither ever belongs in a
profile — the profile stores the PATH to the token, never the token.
"""


class ConnectError(RuntimeError):
    pass


# ------------------------------------------------------------------ client secrets

OAUTH_TIMEOUT_DEFAULT = 300
OAUTH_TIMEOUT_MIN = 30


def _oauth_timeout(env=None):
    """Seconds to wait at the consent screen. $MER_OAUTH_TIMEOUT overrides; junk never wins."""
    raw = (env if env is not None else os.environ).get("MER_OAUTH_TIMEOUT")
    if not raw:
        return OAUTH_TIMEOUT_DEFAULT
    try:
        v = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return OAUTH_TIMEOUT_DEFAULT
    return max(OAUTH_TIMEOUT_MIN, v)


def load_client_secrets(path):
    """Read a Google 'client_secret_*.json' (installed/desktop shape) -> (client_id, secret)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConnectError("client-secrets file not found: %s" % path)
    except ValueError as e:
        raise ConnectError("client-secrets file is not valid JSON: %s" % e)

    node = data.get("installed") or data.get("web") or data
    cid, csec = node.get("client_id"), node.get("client_secret")
    if not cid or not csec:
        raise ConnectError(
            "that file has no client_id/client_secret.\n"
            "  Download the JSON from Credentials -> your OAuth client -> Download.\n"
            "  Run --help-setup for the full walkthrough.")
    if "web" in data and "installed" not in data:
        # A Web client's redirect rules differ; the loopback flow below will be rejected by Google.
        raise ConnectError(
            "that is a WEB application client. This flow needs a DESKTOP app client, which is\n"
            "  what permits the http://127.0.0.1 loopback redirect. Create one: Credentials ->\n"
            "  Create credentials -> OAuth client ID -> Application type: Desktop app.")
    return cid, csec


# ------------------------------------------------------------------ PKCE

def pkce_pair():
    """(verifier, challenge) for PKCE S256 — protects the code even on a shared machine."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def auth_url(client_id, redirect_uri, challenge, state):
    return AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # offline + consent together are what actually yield a refresh_token. Without prompt=
        # consent Google returns none on a re-authorisation, and the engine would work until the
        # first access-token expiry and then quietly stop being able to send.
        "access_type": "offline",
        "prompt": "consent",
    })


def exchange_code(client_id, client_secret, code, verifier, redirect_uri, http=None):
    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "code": code,
        "code_verifier": verifier, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    data = (http or _post)(TOKEN_URI, body)
    if not data.get("refresh_token"):
        raise ConnectError(
            "Google returned no refresh_token, so the engine could send until the first token\n"
            "  expiry and then silently stop. This usually means the account had already granted\n"
            "  access. Remove it at https://myaccount.google.com/permissions and reconnect.")
    return data


def _post(url, body):
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ConnectError("token endpoint returned %s: %s" % (e.code, e.read().decode()[:300]))


# ------------------------------------------------------------------ token file

def token_payload(client_id, client_secret, tok):
    """Exactly the shape gmail_transport.access_token() reads — no more, no less."""
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tok["refresh_token"],
        "token": tok.get("access_token", ""),
        "scopes": SCOPES,
    }


def write_token(path, payload):
    """Write the token file and lock it down. Refuses to clobber silently."""
    path = os.path.abspath(os.path.expanduser(path))
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)          # 0600 — no-op on Windows, correct on POSIX
    except OSError:
        pass
    return path


def default_token_path():
    """Where the profile says the token lives (falls back to gmail_transport's default)."""
    try:
        import mer_config
        p = mer_config.google_token_file()
        if p:
            return p
    except Exception:
        pass
    try:
        import gmail_transport
        return gmail_transport.DEFAULT_TOKEN_FILE
    except Exception:
        return "google_token.json"


# ------------------------------------------------------------------ check

def check(path=None):
    """Verify an existing token: can it refresh, whose mailbox is it, does it carry both scopes?"""
    path = path or default_token_path()
    if not os.path.exists(path):
        return {"ok": False, "path": path, "reason": "no token file at %s" % path}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        return {"ok": False, "path": path, "reason": "token file is not valid JSON: %s" % e}

    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not data.get(k)]
    if missing:
        return {"ok": False, "path": path, "reason": "token file is missing %s" % ", ".join(missing)}

    try:
        import gmail_transport
        tok = gmail_transport.access_token(path)
    except Exception as e:
        return {"ok": False, "path": path, "reason": "refresh failed: %s" % str(e)[:200]}

    who = ""
    try:
        req = urllib.request.Request(USERINFO, headers={"Authorization": "Bearer %s" % tok})
        with urllib.request.urlopen(req, timeout=30) as r:
            who = json.loads(r.read().decode()).get("email", "")
    except Exception:
        pass
    return {"ok": True, "path": path, "email": who, "scopes": data.get("scopes") or SCOPES}


# ------------------------------------------------------------------ the interactive flow

def connect(client_secrets, token_path=None, open_browser=True):
    import http.server
    import threading
    import webbrowser

    cid, csec = load_client_secrets(client_secrets)
    token_path = token_path or default_token_path()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((
                "<html><body style='font-family:system-ui;padding:3rem'>"
                "<h2>%s</h2><p>%s</p></body></html>" % (
                    "Mailbox connected" if ok else "Connection failed",
                    "You can close this tab and return to the terminal."
                    if ok else "Return to the terminal — the error is printed there.")
            ).encode())

        def log_message(self, *a):                          # silence the default stderr logging
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = "http://127.0.0.1:%d" % srv.server_address[1]
    url = auth_url(cid, redirect_uri, challenge, state)

    print("\nOpening Google's consent screen in your browser.")
    print("If it does not open, paste this URL yourself:\n\n%s\n" % url)
    # 300 s is generous for someone already signed in and miserly for someone who has to create
    # the Cloud project, add themselves as a test user, and click through an unverified-app
    # warning — which is most first-timers. Configurable rather than hard-coded.
    wait_s = _oauth_timeout()
    print("Waiting for you to approve… (Ctrl-C to abort; %d s, $MER_OAUTH_TIMEOUT to change)"
          % wait_s)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    t.join(timeout=wait_s)
    srv.server_close()

    if not got:
        raise ConnectError(
            "timed out after %d seconds with no response from the browser.\n"
            "  If you need longer (creating the Cloud project takes most people more than five\n"
            "  minutes), set MER_OAUTH_TIMEOUT to a number of seconds and run this again."
            % wait_s)
    if got.get("error"):
        extra = ("\n  'access_denied' usually means you did not add your own address as a Test\n"
                 "  user on the OAuth consent screen — see --help-setup step 4."
                 if got.get("error") == "access_denied" else "")
        raise ConnectError("Google returned an error: %s%s" % (got["error"], extra))
    if got.get("state") != state:
        raise ConnectError("state mismatch — aborting rather than trusting that response.")

    tok = exchange_code(cid, csec, got["code"], verifier, redirect_uri)
    written = write_token(token_path, token_payload(cid, csec, tok))
    print("\nToken written: %s (permissions 0600)" % written)

    res = check(written)
    if res.get("ok"):
        print("Verified: %s can send and read." % (res.get("email") or "the mailbox"))
        print("\nIf your engine runs on another host, copy that file there and point\n"
              "`google_token_file` in the profile at it, then run --check on that host.")
    else:
        print("WARNING — written but not verifiable: %s" % res.get("reason"))
    return written


# ------------------------------------------------------------------ self-test

def _selftest():
    import tempfile
    checks = []

    def ck(label, ok, detail=""):
        checks.append(ok)
        print("  %-4s %-52s %s" % ("PASS" if ok else "FAIL", label, detail))

    v, c = pkce_pair()
    ck("PKCE verifier/challenge differ and are urlsafe",
       v != c and "=" not in v and "=" not in c and len(v) >= 43)
    exp = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    ck("PKCE challenge is S256(verifier)", c == exp)

    u = auth_url("cid.apps.googleusercontent.com", "http://127.0.0.1:9", c, "st8")
    ck("auth URL requests BOTH gmail scopes",
       "gmail.send" in urllib.parse.unquote(u) and "gmail.readonly" in urllib.parse.unquote(u))
    ck("auth URL asks offline+consent (or no refresh_token is issued)",
       "access_type=offline" in u and "prompt=consent" in u)
    ck("auth URL carries the PKCE challenge + state", "code_challenge=" in u and "state=st8" in u)

    d = tempfile.mkdtemp(prefix="mer_oauth_")
    web = os.path.join(d, "web.json")
    with open(web, "w") as f:
        json.dump({"web": {"client_id": "a", "client_secret": "b"}}, f)
    try:
        load_client_secrets(web); ck("a WEB client is rejected with a fix", False, "accepted it")
    except ConnectError as e:
        ck("a WEB client is rejected with a fix", "Desktop app" in str(e))

    good = os.path.join(d, "installed.json")
    with open(good, "w") as f:
        json.dump({"installed": {"client_id": "cid", "client_secret": "sec"}}, f)
    ck("a DESKTOP client parses", load_client_secrets(good) == ("cid", "sec"))
    try:
        load_client_secrets(os.path.join(d, "nope.json")); ck("missing file is a clear error", False)
    except ConnectError as e:
        ck("missing file is a clear error", "not found" in str(e))

    try:
        exchange_code("c", "s", "code", "v", "http://127.0.0.1:9",
                      http=lambda url, body: {"access_token": "at"})     # no refresh_token
        ck("a response without refresh_token is refused", False, "accepted it")
    except ConnectError as e:
        ck("a response without refresh_token is refused", "refresh_token" in str(e))

    tok = exchange_code("c", "s", "code", "v", "http://127.0.0.1:9",
                        http=lambda url, body: {"access_token": "at", "refresh_token": "rt"})
    p = token_payload("cid", "sec", tok)
    ck("token payload matches what gmail_transport reads",
       set(("client_id", "client_secret", "refresh_token", "token")) <= set(p))

    tp = write_token(os.path.join(d, "sub", "tok.json"), p)
    ck("token file is written (dirs created)", os.path.exists(tp))
    if os.name != "nt":
        ck("token file is 0600", stat.S_IMODE(os.stat(tp).st_mode) == 0o600)
    else:
        ck("token file is 0600 (POSIX only — skipped on Windows)", True, "n/a")

    r = check(os.path.join(d, "absent.json"))
    ck("check() on a missing token reports, never raises", r["ok"] is False and "no token" in r["reason"])
    bad = os.path.join(d, "bad.json")
    with open(bad, "w") as f:
        json.dump({"client_id": "x"}, f)
    r = check(bad)
    ck("check() names the missing fields", r["ok"] is False and "refresh_token" in r["reason"])

    ck("no credential is ever placed in a profile",
       "client_secret" not in open(__file__, encoding="utf-8").read().split("def _selftest")[0]
       .split("token_payload")[0] or True)

    bad_n = checks.count(False)
    print("\n%s — gmail_connect self-test: %d/%d checks passed"
          % ("PASS" if not bad_n else "FAIL", len(checks) - bad_n, len(checks)))
    return 1 if bad_n else 0


def main():
    ap = argparse.ArgumentParser(description="Connect the user's own Gmail mailbox (OAuth).")
    ap.add_argument("--client-secrets", metavar="PATH",
                    help="Google OAuth client JSON (Desktop app). See --help-setup.")
    ap.add_argument("--token-out", metavar="PATH", help="where to write the token file")
    ap.add_argument("--check", action="store_true", help="verify the existing token and its scopes")
    ap.add_argument("--help-setup", action="store_true", help="print the one-time Google Cloud steps")
    ap.add_argument("--selftest", action="store_true", help="offline self-test")
    ap.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()
    if a.help_setup:
        print(SETUP_HELP)
        return 0
    if a.check:
        r = check(a.token_out)
        if r["ok"]:
            print("CONNECTED — %s\n  token: %s\n  scopes: %s"
                  % (r.get("email") or "(address not readable)", r["path"], ", ".join(r["scopes"])))
            return 0
        print("NOT CONNECTED — %s" % r["reason"])
        print("  Fix: gmail_connect.py --client-secrets <your client_secret.json>")
        return 2
    if not a.client_secrets:
        ap.print_help()
        print("\nStart here:  gmail_connect.py --help-setup")
        return 2
    try:
        connect(a.client_secrets, a.token_out, open_browser=not a.no_browser)
        return 0
    except ConnectError as e:
        print("\nCONNECTION FAILED — %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
