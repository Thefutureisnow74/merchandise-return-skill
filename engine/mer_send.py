#!/usr/bin/env python3
"""
mer_send.py — the gated outbound send primitive for the Merchandise Returns Engine (M6/M7/M45).

Every autonomous outbound goes through here. THIS is the public send API; gmail_transport is
its last mile and now refuses to run without a token this module obtains.

Modes via env MER_ENGINE_SEND (an unknown value is REJECTED, loudly, at import):
  off   (default) — refuse to send. Nothing leaves.
  test  — redirect EVERY send to the profile owner's own mailbox (mer_config.email()) with a
          [TEST] banner, so a real send can be watched end-to-end without ever touching a
          vendor. This is the go-live safety gate.
  veto  — real sends to real recipients, but ONLY from send_queue.process(), i.e. only after a
          drafted letter has sat out its veto window un-vetoed. A direct mer_send.send() call is
          REFUSED in this mode. (M45: mer_engine documented off|veto|live from day one while
          mode() only ever accepted test|live, so under `veto` every send returned "sending
          disabled" — the one reason string that RETAINED the queue record. Letters piled up
          silently and would all have blasted out the moment someone flipped to live.)
  live  — send to the real recipient from any call site.

Safety, always on (M45 hardening):
  - idempotency.reserve_send() runs before every send and is TWO-PHASE: the reservation is
    written "pending", then commit()ed on a confirmed send or release()d if the transport
    raised. Before M45 a failed send burned the key forever — the letter became permanently
    un-sendable while the ledger claimed it had gone out.
  - a coarse (case, recipient) COOLDOWN sits above the exact-body key: no second letter to the
    same vendor on the same case inside MER_SEND_COOLDOWN_HOURS (default 48) unless the caller
    passes override=True, which is recorded in the ledger entry. The 2026-07-28 duplicate had a
    different deadline in the body, therefore a different hash, therefore a different key, and
    routing it through mer_send would NOT have stopped it. This stops it.
  - the transport itself now refuses to send without the reservation token, so a module that
    imports gmail_transport and calls send_mime directly (which is exactly what happened on
    07-18, 07-25 and 07-28) gets an exception instead of a duplicate vendor email.
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

# M45 — arm the transport. gmail_transport deliberately does not import idempotency (it is a
# leaf, and widening its import graph would widen it for every read-only module that touches it);
# instead the policy layer registers itself here. Until this line runs, send_mime() refuses
# EVERY send, so a module that imports only gmail_transport cannot mail anything at all.
gmail_transport.register_send_guard(idempotency.consume_send_token)

VALID_MODES = ("off", "test", "veto", "live")


class ModeError(RuntimeError):
    """MER_ENGINE_SEND holds a value this module does not implement."""


def self_mailbox():
    """The profile owner's own mailbox — where TEST-mode sends are redirected."""
    return mer_config.email()


def mode():
    """The current send mode. An unrecognised MER_ENGINE_SEND raises rather than degrading.

    Degrading was the bug: `veto` was documented, was accepted by mer_engine's drafting gate,
    and fell through mode()'s ("test", "live") test into "sending disabled" — which send_queue
    treated as "hold this letter until sending is enabled". A typo'd mode did the same thing.
    Silent accumulation of un-sent letters that later fire all at once is the worst possible
    failure for this system, so an unknown mode is now fatal at the point of use.
    """
    m = os.environ.get("MER_ENGINE_SEND", "off").lower().strip()
    if m not in VALID_MODES:
        raise ModeError(
            "MER_ENGINE_SEND=%r is not a mode. Valid: %s. Refusing to guess — an unrecognised "
            "mode used to behave as 'sending disabled', which made send_queue HOLD every letter "
            "indefinitely instead of sending or failing." % (m, "|".join(VALID_MODES)))
    return m


# Fail at import, not three hours into a cron run, if the environment is misconfigured.
mode()


def send(to, subject, body, case="", action="reply", in_reply_to=None, references=None,
         override=False, via_queue=False, cooldown_hours=None):
    """Send one message per the current mode. Returns a result dict; never raises.

    override=True   — bypass the coarse (case, recipient) cooldown for a send the operator has
                      consciously decided is genuinely a second letter. Recorded in the ledger.
    via_queue=True  — set ONLY by send_queue.process(). It attests that this letter served its
                      veto window, which is what `veto` mode requires before anything real goes
                      to a real vendor.
    """
    m = mode()
    if m == "off":
        return {"sent": False, "reason": "MER_ENGINE_SEND=off (sending disabled)"}
    if m == "veto" and not via_queue:
        return {"sent": False, "reason": (
            "MER_ENGINE_SEND=veto: a direct send is refused. In veto mode a letter must be "
            "enqueued via send_queue.enqueue() and survive its veto window; send_queue.process() "
            "is the only caller allowed to fire it.")}

    r = idempotency.reserve_send(case or "-", action, to, body,
                                 cooldown_hours=cooldown_hours, override=override,
                                 meta={"mode": m, "subject": subject, "via_queue": via_queue})
    if not r["ok"]:
        out = {"sent": False, "reason": "idempotency: %s" % r["reason"], "key": r["key"]}
        if r.get("cooldown"):
            out["cooldown"] = r["cooldown"]
        return out
    key, token = r["key"], r["token"]

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
        gid = gmail_transport.send_mime(msg, to_addrs=[real_to], token=token)
    except Exception as e:
        # PHASE TWO, SAD PATH. Before M45 the reservation stayed on the books here, so every
        # retry of this exact letter was answered "already sent" and the letter could never
        # leave — while the ledger insisted it had. Give the key back.
        released = idempotency.release(key)
        return {"sent": False, "reason": "send error: %s" % e, "key": key,
                "released": released, "error_type": type(e).__name__}
    idempotency.commit(key, gmail_id=gid)
    return {"sent": True, "mode": m, "to": real_to, "intended_to": to, "gmail_id": gid, "key": key}


if __name__ == "__main__":
    print("MER_ENGINE_SEND=%s" % mode())
    print(send("vendor@example.com", "mer_send self-test",
               "This is a mer_send self-test.", case="TEST", action="selftest"))
