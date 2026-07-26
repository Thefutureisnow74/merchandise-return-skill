#!/usr/bin/env python3
"""gmail_transport.py — THE canonical Gmail send path for every script on this VPS.

Why this module exists (locked 2026-07-17, King's standing directive):
Gmail's REST API send (`users.messages.send`) relays through gmailapi.google.com
with an HTTPREST fingerprint that fails Gmail's DMARC alignment check, so every
recipient sees the "Be careful with this message" soft-phishing banner. The
SMTP MSA path (gmail-smtp-msa.l.google.com:587, STARTTLS, XOAUTH2) carries a
clean DKIM signature and shows no banner.

RULE: no script may call `service.users().messages().send()` or POST to
`gmail.googleapis.com/.../messages/send` directly. Import this module instead:

    import sys
    sys.path.insert(0, "/opt/data/scripts")
    from gmail_transport import send_mime

    msg = MIMEText("body")          # or MIMEMultipart with attachments
    msg["To"] = "someone@example.com"
    msg["Subject"] = "Subject"
    gmail_id = send_mime(msg)       # returns the Gmail message id (or None)

The daily hermes-full-diagnostic has a probe that greps for raw REST sends
outside the allowlist and alerts King's Telegram if one appears. Stdlib-only
on purpose — works from any venv or bare python3.

--------------------------------------------------------------------------
CREDENTIALS (M34 — read this before deploying to a fresh machine)
--------------------------------------------------------------------------
This file contains NO secrets and must never contain any. Every credential is
read at runtime from an OAuth token JSON file that is NOT in this repo:

    {"client_id": "...", "client_secret": "...",
     "refresh_token": "...", "token": "..."}

The operator must supply that file themselves. Its location and the sending
identity are configurable so a fresh clone does not have to edit code:

    MER_GOOGLE_TOKEN_FILE   path to the OAuth token JSON
    MER_SENDER              the From address / XOAUTH2 user

When those are not set, BOTH values come from your profile (mer_config: run
onboard.py, or point $MER_PROFILE at your profile.json).

There is NO default sender and NO default token path. If nothing is configured
the module raises mer_config.MerConfigError naming the fix, and sends nothing.
That is deliberate: mailing under an unconfigured — i.e. somebody else's —
identity is the worst failure this system could have, and it would be silent.
Resolution is LAZY, so importing this module never fails; only actually sending
(or reading DEFAULT_SENDER / DEFAULT_TOKEN_FILE) requires an identity.

The token file needs Gmail scopes `gmail.send` (this module) and
`gmail.readonly` (gmail_fetch.py / inbox_watcher.py share the same file).
NEVER commit the token file. Keep it chmod 600 and outside the repo tree.
"""
import base64
import email.utils
import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mer_config  # noqa: E402  (M41 — identity comes from the profile, never from a literal)

SMTP_MSA_HOST = "gmail-smtp-msa.l.google.com"
SMTP_MSA_PORT = 587


def _identity():
    """Sending identity: env override -> profile. There is NO third tier.

    Resolved lazily (per call, not at import) so that importing this module — 8 engine modules
    do — never explodes, while every path that could actually put mail on the wire is forced to
    know whose mail it is. If neither source answers, mer_config.MerConfigError propagates with
    the full "run onboard.py / set $MER_PROFILE" text. Nothing is guessed and nothing is sent.
    """
    token = os.environ.get("MER_GOOGLE_TOKEN_FILE")
    sender = os.environ.get("MER_SENDER")
    if token and sender:
        return token, sender
    p = mer_config.profile()          # raises MerConfigError when unconfigured — on purpose
    return (token or p.google_token_file(), sender or p.email())


def default_token_file():
    """str — the OAuth token path this process will use. Raises if unconfigured."""
    return _identity()[0]


def default_sender():
    """str — the address this process will send as. Raises if unconfigured."""
    return _identity()[1]


def __getattr__(name):
    """Module-level DEFAULT_TOKEN_FILE / DEFAULT_SENDER, resolved on ACCESS rather than on
    import (PEP 562). Callers that read them keep working; an unconfigured install gets the
    MerConfigError with the fix instead of a stranger's address."""
    if name == "DEFAULT_TOKEN_FILE":
        return default_token_file()
    if name == "DEFAULT_SENDER":
        return default_sender()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def access_token(token_file=None):
    """Return a fresh access token, refreshing via the refresh_token grant."""
    token_file = token_file or default_token_file()
    with open(token_file) as f:
        t = json.load(f)
    stored = t.get("token") or t.get("access_token")
    data = urllib.parse.urlencode({
        "client_id": t["client_id"],
        "client_secret": t["client_secret"],
        "refresh_token": t["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    try:
        resp = json.loads(urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/token", data=data),
            timeout=30,
        ).read())
        return resp["access_token"]
    except Exception:
        if stored:
            return stored
        raise


def send_mime(msg, to_addrs=None, token_file=None, sender=None, lookup_id=True):
    """Send a MIME message via Gmail SMTP MSA (DMARC-clean, no banner).

    token_file / sender default to the configured identity (env override, else profile) and
    raise mer_config.MerConfigError if there is none — mail is never sent as a default user.
    to_addrs defaults to every address in the To/Cc/Bcc headers.
    Returns the Gmail message id (looked up by Message-ID), or None.
    """
    if token_file is None or sender is None:
        _tok, _snd = _identity()
        token_file = token_file or _tok
        sender = sender or _snd
    if "From" not in msg:
        msg["From"] = sender
    if "Message-ID" not in msg:
        msg["Message-ID"] = email.utils.make_msgid()
    if to_addrs is None:
        to_addrs = [addr for _, addr in email.utils.getaddresses(
            msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Bcc", [])
        ) if addr]
    if not to_addrs:
        raise ValueError("no recipients: set To/Cc headers or pass to_addrs")

    token = access_token(token_file)
    auth_string = f"user={sender}\x01auth=Bearer {token}\x01\x01"
    smtp = smtplib.SMTP(SMTP_MSA_HOST, SMTP_MSA_PORT, timeout=30)
    try:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        code, resp = smtp.docmd(
            "AUTH", "XOAUTH2 " + base64.b64encode(auth_string.encode()).decode())
        if code != 235:
            raise RuntimeError(f"XOAUTH2 auth failed: {code} {resp}")
        smtp.sendmail(sender, to_addrs, msg.as_string())
    finally:
        smtp.quit()

    if not lookup_id:
        return None
    return find_gmail_id(msg["Message-ID"], token)


def find_gmail_id(rfc822_message_id, token, retries=5, delay=2):
    """Look up the Gmail id of a just-sent message by its RFC822 Message-ID.

    Read-only REST call — reads don't affect DMARC/banner, only sends do.
    """
    query = urllib.parse.quote(f"rfc822msgid:{rfc822_message_id}")
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}"
    for _ in range(retries):
        try:
            resp = json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}),
                timeout=30,
            ).read())
            messages = resp.get("messages", [])
            if messages:
                return messages[0]["id"]
        except Exception:
            pass
        time.sleep(delay)
    return None
