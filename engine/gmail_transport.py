#!/usr/bin/env python3
"""gmail_transport.py — the LAST MILE of the send path. NOT a public API.

########################################################################
#  DO NOT CALL send_mime() DIRECTLY. CALL mer_send.send() INSTEAD.     #
#                                                                     #
#      import mer_send                                                 #
#      res = mer_send.send(to, subject, body, case="MER-3",            #
#                          action="tier1_followup")                    #
#                                                                     #
#  mer_send reserves the send in idempotency FIRST, hands the          #
#  resulting one-shot token down to send_mime, and commits or          #
#  releases the reservation depending on what the transport did.       #
########################################################################

This docstring used to RECOMMEND `from gmail_transport import send_mime` as the canonical way
to send. That advice is how three duplicate vendor emails happened — 2026-07-18 (Stride, to the
bank, twice), 2026-07-25 (PPG) and 2026-07-28 (Relax The Back). Each time an agent read this file,
saw a friendly public function, imported it, and sent mail with ZERO idempotency guard, because
the guard lived one layer up in mer_send and nothing structurally required it.

As of M45 the guard is not optional. send_mime() REFUSES to open an SMTP connection unless
(a) a send guard has been registered in this process — mer_send does that at import — and
(b) it is handed a `token=` minted by idempotency.reserve_send(). Tokens are single-use.
Miss either and it raises (UnguardedSendError / idempotency.SendGuardError) and nothing leaves.

If you have a genuine one-off human send that is not a case action, mint an audited token
explicitly — do not weaken this check:

    import mer_send      # registers the guard
    import idempotency
    tok = idempotency.mint_manual_token("re-sending the Stride trace by hand", operator="king")
    gmail_transport.send_mime(msg, to_addrs=[...], token=tok)

Why the SMTP path at all (locked 2026-07-17, King's standing directive):
Gmail's REST API send (`users.messages.send`) relays through gmailapi.google.com
with an HTTPREST fingerprint that fails Gmail's DMARC alignment check, so every
recipient sees the "Be careful with this message" soft-phishing banner. The
SMTP MSA path (gmail-smtp-msa.l.google.com:587, STARTTLS, XOAUTH2) carries a
clean DKIM signature and shows no banner.

RULE: no script may call `service.users().messages().send()` or POST to
`gmail.googleapis.com/.../messages/send` directly, and no script may call
send_mime() without a reservation token.

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


class UnguardedSendError(RuntimeError):
    """A send was attempted with no send guard registered at all. Fail closed."""


#: M45 — THE SEND GUARD, INJECTED RATHER THAN IMPORTED.
#:
#: This module deliberately does NOT `import idempotency`. gmail_transport is a leaf: it knows
#: how to put bytes on Gmail's MSA and nothing else, and inbox_watcher's self-test asserts that
#: leaf-ness by pinning the transitive import graph of everything it can reach. Making the
#: transport import the ledger would have widened that graph for every read-only module in the
#: engine just to police the one module that sends.
#:
#: So the policy layer registers itself instead: mer_send calls register_send_guard() at import
#: with idempotency.consume_send_token. The default is None, which means an UNREGISTERED process
#: cannot send AT ALL — a module that imports only gmail_transport and calls send_mime (exactly
#: what happened on 2026-07-18, 07-25 and 07-28) gets an exception, not a duplicate vendor email.
#: That is fail-closed twice: no guard -> refuse; guard present -> the guard must approve.
_SEND_GUARD = None


def register_send_guard(validator):
    """Install the reservation-token validator. Called by mer_send at import.

    `validator(token, recipient=None)` must raise if `token` is missing, unknown, already used
    or stale, and must consume it on success. Returns the previous validator (for tests).
    """
    global _SEND_GUARD
    prev, _SEND_GUARD = _SEND_GUARD, validator
    return prev


def _check_send_token(token, recipient=None):
    if _SEND_GUARD is None:
        raise UnguardedSendError(
            "REFUSING TO SEND: no send guard is registered in this process. That means nothing "
            "here reserved this send. Do not call gmail_transport.send_mime() — call "
            "mer_send.send(to, subject, body, case=..., action=...), which reserves the send "
            "in the idempotency ledger and installs the guard. Three duplicate vendor emails "
            "(2026-07-18 Stride, 07-25 PPG, 07-28 Relax The Back) came from calling this "
            "function directly.")
    _SEND_GUARD(token, recipient=recipient)


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


class TokenRefreshError(RuntimeError):
    """The refresh_token grant failed. M45: this is now fatal instead of silently degrading."""


def access_token(token_file=None):
    """Return a fresh access token, refreshing via the refresh_token grant.

    M45 — NO FALLBACK TO THE STORED TOKEN. This used to swallow any refresh failure and hand
    back `t["token"]`, which is an access token that expires in an hour and is almost always
    already dead by the time anyone looks. The effect was to convert a crisp, actionable auth
    error ("the refresh grant was revoked, re-run gmail_connect.py") into an opaque SMTP
    "XOAUTH2 auth failed: 535" hundreds of lines later, or worse into a send that just quietly
    never happened. Auth problems must surface as auth problems.
    """
    token_file = token_file or default_token_file()
    with open(token_file) as f:
        t = json.load(f)
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
    except Exception as e:
        raise TokenRefreshError(
            "OAuth refresh grant FAILED for %s (%s). Refusing to fall back to the stored access "
            "token — it is almost certainly expired and would surface later as an unexplained "
            "SMTP auth failure. Re-authorize with gmail_connect.py." % (token_file, e))
    tok = resp.get("access_token")
    if not tok:
        raise TokenRefreshError(
            "OAuth refresh grant for %s returned no access_token (%r). Re-authorize with "
            "gmail_connect.py." % (token_file, sorted(resp)))
    return tok


def send_mime(msg, to_addrs=None, token_file=None, sender=None, lookup_id=True, token=None):
    """INTERNAL. Send a MIME message via Gmail SMTP MSA (DMARC-clean, no banner).

    *** Do not call this. Call mer_send.send(). ***  `token` is REQUIRED and must be a one-shot
    reservation token from idempotency.reserve_send() (or an audited
    idempotency.mint_manual_token() for a deliberate human one-off). Without a valid, unused
    token this raises idempotency.SendGuardError and NOTHING is sent — that refusal is the
    structural replacement for a rule in a docstring that agents kept not following.

    The token is consumed BEFORE the SMTP connection opens, so a crash mid-send cannot leave a
    token that a retry could reuse to send twice; mer_send releases the whole reservation on
    failure, which mints a fresh token for the retry.

    token_file / sender default to the configured identity (env override, else profile) and
    raise mer_config.MerConfigError if there is none — mail is never sent as a default user.
    to_addrs defaults to every address in the To/Cc/Bcc headers.
    Returns the Gmail message id (looked up by Message-ID), or None.
    """
    # THE GATE. First statement in the function on purpose: nothing below it — not identity
    # resolution, not header defaulting, and certainly not smtplib — runs unreserved.
    _check_send_token(token, recipient=(to_addrs[0] if to_addrs else None))

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
