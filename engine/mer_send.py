#!/usr/bin/env python3
"""
mer_send.py — the gated outbound send primitive for the Merchandise Returns Engine (M6/M7).

Every autonomous outbound goes through here. Three modes via env MER_ENGINE_SEND:
  off   (default) — refuse to send. Nothing leaves.
  test  — redirect EVERY send to the profile owner's own mailbox (mer_config.email()) with a
          [TEST] banner, so a real send can be watched end-to-end without ever touching a
          vendor. This is the go-live safety gate.
  live  — send to the real recipient.

Safety, always on:
  - the idempotency guard (idempotency.reserve) runs before every send — a duplicate logical send
    is blocked no matter the mode or code path (the bank-double-email lesson).
  - sends via gmail_transport.send_mime (DMARC-clean SMTP MSA), threaded when in_reply_to is given.
"""
import email.mime.text
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import gmail_transport      # noqa: E402
import idempotency          # noqa: E402
import mer_config           # noqa: E402  (M32 — identity comes from the profile, not a literal)


def self_mailbox():
    """The profile owner's own mailbox — where TEST-mode sends are redirected."""
    return mer_config.email()


def mode():
    return os.environ.get("MER_ENGINE_SEND", "off").lower()


def send(to, subject, body, case="", action="reply", in_reply_to=None, references=None):
    """Send one message per the current mode. Returns a result dict; never raises."""
    m = mode()
    if m not in ("test", "live"):
        return {"sent": False, "reason": "MER_ENGINE_SEND=off (sending disabled)"}

    ok, key = idempotency.reserve(case or "-", action, to, body)
    if not ok:
        return {"sent": False, "reason": "idempotency: this exact send already went out", "key": key}

    real_to, subj, text = to, subject, body
    if m == "test":
        real_to = self_mailbox()
        subj = "[TEST -> %s] %s" % (to, subject)
        text = ("[[ TEST MODE — in LIVE this would send to: %s ]]\n"
                "[[ case=%s action=%s ]]\n\n%s" % (to, case, action, body))

    msg = email.mime.text.MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subj
    msg["To"] = real_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    try:
        gid = gmail_transport.send_mime(msg, to_addrs=[real_to])
    except Exception as e:
        return {"sent": False, "reason": "send error: %s" % e, "key": key}
    return {"sent": True, "mode": m, "to": real_to, "intended_to": to, "gmail_id": gid, "key": key}


if __name__ == "__main__":
    print("MER_ENGINE_SEND=%s" % mode())
    print(send("vendor@example.com", "mer_send self-test",
               "This is a mer_send self-test.", case="TEST", action="selftest"))
