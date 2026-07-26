#!/usr/bin/env python3
"""
send_queue.py — the veto-window queue for autonomous outbound (M6/M7).

The engine never sends instantly. It ENQUEUES a drafted send with a `send_after` time; the user
gets the queued item surfaced and can veto during the window. `process()` fires everything whose
window has elapsed and that wasn't vetoed, via mer_send (which honors MER_ENGINE_SEND: off/test/live)
and the idempotency guard.

Record: {id, case, to, subject, body, action, send_after(iso), in_reply_to, references, vetoed}
Queue file: /opt/data/mer_send_queue.json

CLI: --list | --process | --veto <id> | --enqueue-test
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import mer_send  # noqa: E402

QUEUE = os.environ.get("MER_SEND_QUEUE", "/opt/data/mer_send_queue.json")


def _load():
    try:
        return json.load(open(QUEUE))
    except Exception:
        return []


def _save(q):
    json.dump(q, open(QUEUE, "w"), indent=2)


def enqueue(case, to, subject, body, action="reply", window_hours=3,
            in_reply_to=None, references=None, lever=None):
    """Queue one outbound. `lever` (M44) is OPTIONAL and defaults to None.

    When a caller names the remedy lever this send IS (e.g. "tier2_exec" for an executive
    escalation), process() logs that lever to `MR Remedy Attempted` — but only after the
    send primitive actually reports sent=True. Records without a lever behave exactly as
    before: nothing is logged, nothing changes.

    The lever is deliberately NOT acted on at enqueue time. A queued letter can still be
    vetoed during its window, and a vetoed letter marked "attempted" would help open Tier 4
    on a lever nobody ever pulled.
    """
    q = _load()
    rec = {
        "id": uuid.uuid4().hex[:8],
        "case": case, "to": to, "subject": subject, "body": body, "action": action,
        "in_reply_to": in_reply_to, "references": references, "lever": lever,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "send_after": (datetime.now(timezone.utc) + timedelta(hours=window_hours)).isoformat(),
        "vetoed": False,
    }
    q.append(rec)
    _save(q)
    return rec["id"]


def veto(rec_id):
    q = _load()
    hit = False
    for r in q:
        if r["id"] == rec_id:
            r["vetoed"] = True
            hit = True
    _save(q)
    return hit


def process():
    now = datetime.now(timezone.utc)
    q = _load()
    keep, fired = [], []
    for r in q:
        if r.get("vetoed"):
            fired.append((r["id"], r["case"], "VETOED — dropped, not sent"))
            continue
        try:
            sa = datetime.fromisoformat(r["send_after"])
        except Exception:
            sa = now
        if sa > now:
            keep.append(r)
            fired.append((r["id"], r["case"], "counting down — sends %s" % r["send_after"]))
            continue
        res = mer_send.send(r["to"], r["subject"], r["body"], case=r.get("case", ""),
                            action=r.get("action", "reply"),
                            in_reply_to=r.get("in_reply_to"), references=r.get("references"))
        fired.append((r["id"], r["case"], res))
        if res.get("sent") and r.get("lever"):
            # M44 — THE CALL SITE `remedy_map.mark_attempted` never had. A lever counts as
            # attempted only when the letter genuinely left, which is exactly here: after
            # the veto window closed, after idempotency cleared, after the transport
            # returned an id. Best-effort and non-fatal by design — the mail is already
            # gone, so a board hiccup must not be reported as a failed send. A missed log
            # leaves Tier 4 shut, which is the safe direction; the human path
            # (`remedy_map.py --mark-attempted --live`) closes the gap.
            try:
                import remedy_map
                lv = remedy_map.log_attempt(r["case"], r["lever"], live=True)
                fired.append((r["id"], r["case"], "lever-log: %s" % lv["reason"]))
            except Exception as e:
                fired.append((r["id"], r["case"],
                              "lever-log FAILED for %r (%s) — the send DID go out; log it "
                              "by hand with: remedy_map.py --mark-attempted --case %s "
                              "--lever %s --live" % (r["lever"], e, r["case"], r["lever"])))
        # keep it queued ONLY if sending is globally disabled (so it fires once enabled);
        # otherwise it's done (sent, idempotency-blocked, or errored — don't loop forever).
        if not res.get("sent") and "sending disabled" in res.get("reason", ""):
            keep.append(r)
    _save(keep)
    return fired


# ------------------------------------------------------------------------------- self-test (M38)

def _selftest():
    """Offline proof of the enqueue/veto/process lifecycle.

    Nothing can leave: MER_ENGINE_SEND is forced to `off`, gmail_transport.send_mime is replaced
    with a raiser, the queue file is redirected to a tempfile, and the idempotency ledger is
    redirected to a tempfile (same pattern as idempotency.py's own self-test) so the LIVE queue
    and LIVE ledger are unreachable from here.
    """
    global QUEUE
    import tempfile

    os.environ["MER_ENGINE_SEND"] = "off"
    import idempotency

    live_queue = "/opt/data/mer_send_queue.json"
    tmpdir = tempfile.mkdtemp(prefix="send_queue_test_")
    QUEUE = os.path.join(tmpdir, "queue.json")
    idempotency.LEDGER = os.path.join(tmpdir, "ledger.json")

    # Hard network guard: if anything reaches the transport, the test blows up rather than sends.
    def _no_send(*a, **k):
        raise AssertionError("SELF-TEST TRIED TO SEND MAIL — gmail_transport was reached")
    try:
        import gmail_transport
        gmail_transport.send_mime = _no_send
    except Exception:
        pass
    mer_send.gmail_transport.send_mime = _no_send

    fails = []

    def check(label, cond, detail=""):
        ok = bool(cond)
        print("  %-4s %s%s" % ("PASS" if ok else "FAIL", label, ("  [%s]" % detail) if detail else ""))
        if not ok:
            fails.append(label)

    print("=== send_queue M38 lifecycle self-test (offline, tempfile queue) ===")

    check("test queue is NOT the live queue", QUEUE != live_queue, QUEUE)
    check("sending is globally off", mer_send.mode() == "off", mer_send.mode())

    # A spy in place of mer_send.send records exactly who the queue tried to send to.
    real_send = mer_send.send
    sent_to = []

    def spy(to, subject, body, case="", action="reply", in_reply_to=None, references=None):
        sent_to.append((case, to, action))
        return {"sent": False, "reason": "MER_ENGINE_SEND=off (sending disabled)"}
    mer_send.send = spy

    try:
        # ---- 1. a VETOED item never sends and is dropped from the queue.
        vid = enqueue("MER-T1", "vendor@example.com", "veto me", "body-veto",
                      action="t_veto", window_hours=0)
        check("veto() on a known id returns True", veto(vid) is True)
        res = process()
        row = [r for r in res if r[0] == vid]
        check("vetoed item is reported as dropped, not sent",
              row and "VETOED" in str(row[0][2]), str(row[:1])[:70])
        check("vetoed item never reached mer_send.send", sent_to == [], str(sent_to))
        check("vetoed item is removed from the queue", all(r["id"] != vid for r in _load()))

        # ---- 2. an item INSIDE its veto window does not send yet, and stays queued.
        wid = enqueue("MER-T2", "vendor@example.com", "not yet", "body-window",
                      action="t_window", window_hours=3)
        res = process()
        row = [r for r in res if r[0] == wid]
        check("in-window item is counting down, not sent",
              row and "counting down" in str(row[0][2]), str(row[:1])[:70])
        check("in-window item never reached mer_send.send", sent_to == [], str(sent_to))
        check("in-window item is still queued", any(r["id"] == wid for r in _load()))

        # ---- 3. an item PAST its window is eligible — it reaches the send primitive.
        eid = enqueue("MER-T3", "vendor@example.com", "due now", "body-due",
                      action="t_due", window_hours=0)
        process()
        check("past-window item reaches the send path",
              [s for s in sent_to if s[0] == "MER-T3"], str(sent_to))
        check("only the eligible item was routed to send", len(sent_to) == 1, str(sent_to))
        check("send-disabled item is retained so it fires once enabled",
              any(r["id"] == eid for r in _load()))

        # ---- 4. the queue survives a reload (it is on disk, not in memory).
        ids_before = sorted(r["id"] for r in _load())
        reloaded = sorted(r["id"] for r in json.load(open(QUEUE)))
        check("queue survives a reload", ids_before == reloaded and wid in reloaded,
              str(reloaded))
        rec = [r for r in _load() if r["id"] == wid][0]
        check("record keeps its fields across the reload",
              rec["to"] == "vendor@example.com" and rec["subject"] == "not yet"
              and rec["case"] == "MER-T2" and rec["vetoed"] is False)

        # ---- 5. --veto on an UNKNOWN id fails safely: returns False, corrupts nothing.
        snapshot = _load()
        check("veto() on an unknown id returns False", veto("deadbeef") is False)
        check("a failed veto leaves the queue byte-identical", _load() == snapshot)

        # ---- 5b. M44 lever logging: only on a CONFIRMED send, never on a queued one.
        import remedy_map
        real_log = remedy_map.log_attempt
        logged = []

        def _spy_log(case, lever, **kw):
            logged.append((case, lever, kw.get("live")))
            return {"reason": "stubbed", "refused": False, "logged": True, "attempted": []}
        remedy_map.log_attempt = _spy_log

        def _ok_send(to, subject, body, case="", action="reply", in_reply_to=None,
                     references=None):
            sent_to.append((case, to, action))
            return {"sent": True, "mode": "test", "to": to, "gmail_id": "stub"}
        try:
            # a record with NO lever logs nothing at all (the pre-M44 shape is untouched)
            enqueue("MER-T5", "vendor@example.com", "no lever", "b", action="t_nolever",
                    window_hours=0)
            mer_send.send = _ok_send
            process()
            check("a record with no lever logs nothing", logged == [], str(logged))

            # a VETOED record with a lever never reaches the log — it never sent
            lv = enqueue("MER-T6", "vendor@example.com", "vetoed", "b", action="t_vetolev",
                         window_hours=0, lever="bbb")
            veto(lv)
            process()
            check("a vetoed lever is NEVER logged as attempted", logged == [], str(logged))

            # a record that ACTUALLY sends logs its lever, live
            enqueue("MER-T7", "vendor@example.com", "real", "b", action="t_lever",
                    window_hours=0, lever="tier2_exec")
            process()
            check("a confirmed send logs its lever live",
                  logged == [("MER-T7", "tier2_exec", True)], str(logged))
        finally:
            remedy_map.log_attempt = real_log
            mer_send.send = spy

        # ---- 6. the real (un-spied) send primitive refuses while MER_ENGINE_SEND=off.
        mer_send.send = real_send
        out = mer_send.send("vendor@example.com", "s", "b", case="MER-T4", action="t_off")
        check("real send primitive refuses with sending off",
              out.get("sent") is False and "disabled" in out.get("reason", ""), str(out)[:70])
    finally:
        mer_send.send = real_send

    if fails:
        print("\nFAIL — %d lifecycle check(s) wrong: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("\nPASS — veto blocks, the window holds, the queue persists, unknown ids fail safely.")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--selftest":
        sys.exit(_selftest())
    if a and a[0] == "--veto" and len(a) > 1:
        print("vetoed" if veto(a[1]) else "not found")
    elif a and a[0] == "--enqueue-test":
        rid = enqueue("TEST", "vendor@example.com", "MER veto-queue test",
                      "This is a send_queue end-to-end test.", action="qtest", window_hours=0)
        print("enqueued test id=%s (send_after=now)" % rid)
    elif a and a[0] == "--process":
        for row in process():
            print(row)
    else:  # --list default
        for r in _load():
            print("%s  %s  -> %s  after %s  vetoed=%s" %
                  (r["id"], r["case"], r["to"], r["send_after"], r.get("vetoed")))
        if not _load():
            print("(queue empty)")
