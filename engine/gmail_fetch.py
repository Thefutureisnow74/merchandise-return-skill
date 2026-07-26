#!/usr/bin/env python3
"""gmail_fetch.py — read-side companion to gmail_transport.py.

Fetches Gmail message attachments so downstream classifiers (e.g. the M18
merchandise-returns connector) can read denial-letter PDFs.

Auth mirrors gmail_transport exactly: read the OAuth token file named by
$MER_GOOGLE_TOKEN_FILE, else by your profile (mer_config.google_token_file()),
mint a fresh access token via the refresh_token grant (self-refreshing), fall
back to the stored token on failure. Stdlib-only on purpose — works from any
venv or bare python3, and needs no google-auth / google-api-python-client.

There is no default mailbox. With nothing configured this raises
mer_config.MerConfigError naming the fix (run onboard.py, or set $MER_PROFILE)
rather than reading a mailbox that belongs to somebody else. Resolution is
lazy, so importing this module never fails — only fetching does.

Usage:
    import sys
    sys.path.insert(0, "/opt/data/scripts")
    from gmail_fetch import list_pdf_attachments, fetch_attachment

    for att in list_pdf_attachments(message_id):
        data = fetch_attachment(message_id, att["attachment_id"])
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_transport  # noqa: E402  (M41 — one identity resolver for read and send alike)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


def default_token_file():
    """str — the OAuth token path this process reads with ($MER_GOOGLE_TOKEN_FILE, else the
    profile). Raises mer_config.MerConfigError when nothing is configured."""
    return gmail_transport.default_token_file()


def __getattr__(name):
    """DEFAULT_TOKEN_FILE resolved on ACCESS, not on import (PEP 562), so an unconfigured
    install gets the fix-it error instead of a stranger's token path."""
    if name == "DEFAULT_TOKEN_FILE":
        return default_token_file()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def access_token(token_file=None):
    """Return a fresh access token, refreshing via the refresh_token grant.

    Mirrors gmail_transport.access_token() — same token file, same fallback.
    """
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


def _api_get(path, token, params=None):
    """GET a Gmail REST endpoint, return parsed JSON."""
    url = f"{GMAIL_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _iter_parts(part):
    """Yield every part in a message payload, recursing into multipart bodies."""
    if not part:
        return
    yield part
    for sub in part.get("parts", []) or []:
        yield from _iter_parts(sub)


def get_message(message_id, token=None, token_file=None):
    """Fetch the full message resource (format=full)."""
    if token is None:
        token = access_token(token_file)
    return _api_get(f"/messages/{message_id}", token, {"format": "full"})


def list_pdf_attachments(message_id, token=None, token_file=None):
    """List a message's PDF attachments.

    Returns a list of dicts: {"filename", "attachment_id", "mime_type", "size"}.
    Matches on application/pdf MIME type or a .pdf filename (case-insensitive).
    """
    if token is None:
        token = access_token(token_file)
    msg = get_message(message_id, token=token)
    out = []
    for part in _iter_parts(msg.get("payload", {})):
        filename = part.get("filename") or ""
        mime = part.get("mimeType") or ""
        att_id = (part.get("body") or {}).get("attachmentId")
        if not att_id:
            continue
        is_pdf = mime.lower() == "application/pdf" or filename.lower().endswith(".pdf")
        if is_pdf:
            out.append({
                "filename": filename,
                "attachment_id": att_id,
                "mime_type": mime,
                "size": (part.get("body") or {}).get("size", 0),
            })
    return out


def fetch_attachment(message_id, attachment_id, token=None,
                     token_file=None):
    """Fetch one attachment's raw bytes.

    Calls users.messages.attachments.get and base64url-decodes the data field.
    """
    if token is None:
        token = access_token(token_file)
    resp = _api_get(
        f"/messages/{message_id}/attachments/{attachment_id}", token
    )
    data = resp["data"]
    # Gmail returns base64url ("-" / "_"); add padding before decoding.
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: gmail_fetch.py <message_id> [attachment_id]")
        sys.exit(1)
    mid = sys.argv[1]
    tok = access_token()
    if len(sys.argv) >= 3:
        blob = fetch_attachment(mid, sys.argv[2], token=tok)
        sys.stdout.buffer.write(blob)
    else:
        for a in list_pdf_attachments(mid, token=tok):
            print(f"{a['filename']}\t{a['size']}\t{a['attachment_id']}")
