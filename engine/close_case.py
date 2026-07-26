#!/usr/bin/env python3
"""close_case.py — the CASE CLOSE path for the merchandise-returns engine (M43).

WHY THIS FILE EXISTS
--------------------
"A case cannot close until the money actually landed" was a documented guarantee that no
code enforced. `refund_landed.py` (M28) implements the check and was called by nothing,
because nothing in this engine ever closed a case: `case_tick.py` lists "Closed" in
NEVER_WRITE_PHASES by design, and `multica_api.py` had no call that set a status at all.
A gate in front of a door that does not exist is documentation, not enforcement
(BLUEPRINT §10).

This module is the door, and the gate is welded into it. THREE independent conditions,
ALL required, evaluated BEFORE any write:

  (A) M19 resolution_check.is_resolved(remedy, vendor_reply)   — the remedy was actually
      GRANTED, computed from the vendor's own words. This is also where SKILL.md §6.5 is
      enforced structurally: "escalated internally" is a brush-off the detector rejects,
      so it can never clear the close. The caller does not get to assert "resolved" — it
      supplies the reply text and the detector rules.
  (B) M28 refund_landed(...)["landed"]                          — the money POSTED, at or
      after the resolution date, per a bank/issuer notice or the human-confirm log. A
      vendor's promise is not a posted refund (the canonical live case is the proof).
  (C) an explicit --confirm-close flag                          — SKILL.md §5 puts closing
      a case in the RED lane: always an explicit per-item YES.

If (A) or (B) cannot be EVALUATED — no reply text, no amount, Gmail unreachable — the
close is REFUSED, not warned about. FAIL CLOSED: a gate that cannot check must not let a
case through.

Only after all three does anything get written, and the write is two steps:
    MR Phase -> Closed        (PUT /api/issues/<id>/properties/<prop_id>)
    status   -> done          (PUT /api/issues/<id>)
MR Phase goes first: a case marked done in status but still sitting at Tier2 in MR Phase
would keep being picked up by the phase-driven sweeps.

Run:
    python3 close_case.py --selftest                # offline, stubbed, no network
    python3 close_case.py --case CASE-1 --remedy refund --amount 120.00 --amount 65.00 \
        --since 2026-06-13 --reply-file reply.txt   # dry run — evaluates, writes nothing
    ... --confirm-close --live                      # the only form that writes
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")

import refund_landed as RL  # noqa: E402
import resolution_check as RC  # noqa: E402

CLOSED_PHASE = "Closed"
CLOSED_STATUS = "done"

# Exit codes. 2 and 3 are BOTH holds — a case that could not be checked is exactly as
# unclosable as one that failed the check.
EXIT_CLOSED, EXIT_HOLD, EXIT_CANNOT_CHECK = 0, 2, 3


def _blank(v):
    return v is None or not str(v).strip()


def _verdict(**kw):
    base = {"closed": False, "may_close": False, "reason": "", "exit": EXIT_HOLD,
            "resolution": None, "refund": None, "case": None, "dry_run": True,
            "writes": []}
    base.update(kw)
    return base


def remedy_type_for(case, remedy=None, remedy_type=None):
    """The remedy this case must prove landed, from the case's REAL board data first.

    Precedence, and the reason for it:
      1. `MR Remedy Type` on the issue — the board is the system of record (M44).
      2. an explicit --remedy-type flag — the operator override for a case whose property
         is not set yet.
      3. the --remedy text, but ONLY if it names exactly one remedy family. A ranked
         intake answer ("refund / replacement / repair / credit") names four and is
         refused, not guessed.
    Returns None when none of the three yields an unambiguous answer. None is a HOLD.
    """
    return (RL.remedy_type_from_case(case)
            or RL.normalize_remedy(remedy_type)
            or RL.normalize_remedy(remedy))


def evaluate(case, remedy, vendor_reply, amounts, since, *,
             gmail_search=None, human_confirm_file=RL.DEFAULT_CONFIRM_FILE, use_llm=False,
             remedy_type=None, tokens=None):
    """Run gates (A) and (B). Returns (may_close: bool, reason: str, resolution, refund).

    Never writes. Never raises for a missing input — a missing input is a HOLD with a
    reason, because "we could not check" and "it failed the check" must both keep the
    case open.

    M44: gate (B) covers all four remedies. What counts as "cannot check" depends on the
    remedy — a refund with no amount and a replacement with no tracking number are the same
    failure wearing different clothes, and both hold.
    """
    ident = case.get("identifier") or case.get("id") or "case"

    # --- (A) was the remedy actually granted? -------------------------------------------
    if _blank(vendor_reply):
        return (False,
                "HOLD (cannot check): no vendor reply text supplied, so M19 cannot judge "
                "whether the remedy was granted. A close may not be taken on trust.",
                None, None)
    if _blank(remedy):
        return (False,
                "HOLD (cannot check): no demanded remedy supplied, so M19 has nothing to "
                "test the reply against.", None, None)
    try:
        resolution = RC.is_resolved(remedy, vendor_reply, use_llm=use_llm)
    except Exception as exc:
        return (False, "HOLD (cannot check): the resolution check failed to run (%s: %s)."
                % (type(exc).__name__, exc), None, None)
    if not resolution.get("resolved"):
        # This is the branch that eats "we've escalated internally", "under review",
        # "we'll get back to you" — SKILL.md §6.5, enforced by a detector, not a promise.
        return (False, "HOLD: M19 says the remedy was NOT actually granted — %s"
                % resolution.get("reason", "no grant found"), resolution, None)

    # --- (B) did the remedy actually land? ----------------------------------------------
    rt = remedy_type_for(case, remedy, remedy_type)
    if rt is None:
        return (False,
                "HOLD (cannot check): no unambiguous remedy type. Set `%s` on the issue to "
                "one of %s (or pass --remedy-type). A ranked preference list is not a "
                "decision and is never guessed." % (RL.REMEDY_TYPE_PROP,
                                                    "/".join(RL.REMEDY_TYPES)),
                resolution, None)
    if rt in RL.MONEY_REMEDIES and not amounts:
        return (False,
                "HOLD (cannot check): %s is a money remedy and no expected amount was "
                "supplied, so M28 cannot confirm anything posted. A money case cannot "
                "close on an unspecified amount." % rt,
                resolution, None)
    if rt == "replacement" and not tokens:
        return (False,
                "HOLD (cannot check): a replacement needs a tracking / RMA identifier "
                "(--token). Without one, any parcel on the thread would satisfy the gate.",
                resolution, None)
    if _blank(since):
        return (False,
                "HOLD (cannot check): no resolution date supplied; without it something "
                "that arrived BEFORE the dispute would count as the remedy landing.",
                resolution, None)
    kw = {"human_confirm_file": human_confirm_file}
    if gmail_search is not None:
        kw["gmail_search"] = gmail_search
    try:
        gate = RL.close_gate(case, amounts, since, resolution, remedy_type=rt,
                             tokens=tokens, **kw)
    except Exception as exc:
        return (False, "HOLD (cannot check): the refund-landed gate failed to run (%s: %s). "
                "A gate that cannot check must not let a case close."
                % (type(exc).__name__, exc), resolution, None)
    refund = gate["refund"]
    if any(e.get("source") == "gmail-error" for e in refund.get("evidence", [])) \
            and not refund.get("landed"):
        return (False, "HOLD (cannot check): the mailbox could not be searched, so there is "
                "no way to see whether the %s landed (%s). %s"
                % (rt, ", ".join(refund.get("amounts") or refund.get("anchors") or ["-"]),
                   gate["reason"]), resolution, refund)
    if not gate["may_close"]:
        return False, gate["reason"], resolution, refund
    return (True, "Gates cleared for %s: %s" % (ident, gate["reason"]), resolution, refund)


def close_case(case, remedy, vendor_reply, amounts, since, *, confirm_close=False,
               api=None, dry_run=True, gmail_search=None,
               human_confirm_file=RL.DEFAULT_CONFIRM_FILE, use_llm=False,
               remedy_type=None, tokens=None):
    """Close a case — only when (A) remedy granted AND (B) money landed AND (C) confirmed.

    Returns a verdict dict; `exit` carries the process exit code the CLI should use.
    `api` is injectable (defaults to multica_api) so the self-test can prove the refusal
    paths never reach a write.
    """
    case = dict(case or {})
    ident = case.get("identifier") or case.get("id") or "case"

    may, reason, resolution, refund = evaluate(
        case, remedy, vendor_reply, amounts, since, gmail_search=gmail_search,
        human_confirm_file=human_confirm_file, use_llm=use_llm,
        remedy_type=remedy_type, tokens=tokens)

    v = _verdict(case=ident, dry_run=bool(dry_run), resolution=resolution, refund=refund,
                 may_close=may, reason=reason)

    if not may:
        v["exit"] = EXIT_CANNOT_CHECK if "cannot check" in reason else EXIT_HOLD
        return v

    # (C) RED lane: closing a case always needs an explicit per-item YES (SKILL.md §5).
    if not confirm_close:
        v["may_close"] = True
        v["reason"] = ("HOLD: both gates cleared, but closing a case is a RED-lane action "
                       "(SKILL.md §5) and needs an explicit --confirm-close. %s" % reason)
        v["exit"] = EXIT_HOLD
        return v

    if dry_run:
        v["reason"] = ("DRY RUN — %s and --confirm-close given. Nothing was written. "
                       "Re-run with --live to close." % reason)
        v["exit"] = EXIT_HOLD
        return v

    if api is None:
        import multica_api as api  # noqa: F811  (deferred so --selftest needs no token)

    # MR Phase FIRST: a case flipped to status=done while MR Phase still says Tier2 keeps
    # being selected by the phase-driven sweeps.
    api.set_properties(case, {"MR Phase": CLOSED_PHASE})
    v["writes"].append("MR Phase=%s" % CLOSED_PHASE)
    api.update_issue(case.get("id") or ident, {"status": CLOSED_STATUS})
    v["writes"].append("status=%s" % CLOSED_STATUS)
    v["closed"] = True
    v["exit"] = EXIT_CLOSED
    v["reason"] = "CLOSED %s — %s" % (ident, reason)
    return v


def _load_case(ident, api=None):
    """Read the case off the board (READ ONLY) so the close writes to a real issue."""
    if api is None:
        import multica_api as api  # noqa: F811
    for it in api.list_issues():
        if it.get("identifier") == ident or it.get("id") == ident:
            return it
    raise LookupError("no issue on the board with identifier/id %r" % ident)


# =========================================================================================
# SELF-TEST — offline, fabricated data, stubbed API. No network, no live board, no writes.
# =========================================================================================
class _StubAPI(object):
    """Records writes that WOULD have happened. Never opens a socket."""

    def __init__(self):
        self.writes = []

    def set_properties(self, issue, values_by_name, ws=None, defs=None):
        self.writes.append(("set_properties", issue.get("identifier"), dict(values_by_name)))

    def update_issue(self, issue_id, fields, ws=None):
        self.writes.append(("update_issue", issue_id, dict(fields)))


def _search(messages):
    def _s(query, since_d, amounts, token=None):
        return messages
    return _s


def _boom_search(query, since_d, amounts, token=None):
    raise RuntimeError("gmail unreachable")


_CASE = {"identifier": "TEST-C", "id": "test-c", "query": "from:examplebank.com"}
_REMEDY = "refund"
_GRANTED_REPLY = ("Thank you for your patience. A refund of $129.99 has been issued back "
                  "to your original payment method.")
_BRUSHOFF_REPLY = ("Thank you for reaching out. I have escalated this internally to our "
                   "specialist team and someone will get back to you shortly.")
_BANK_POSTED = [{
    "id": "s1", "from": "alerts@examplebank.com", "subject": "Credit posted",
    "date": "Sat, 02 Aug 2026 10:00:00 -0500",
    "body": "Good news. Your refund of $129.99 has posted to your account ending 4321 "
            "and is now available.",
}]
_VENDOR_CLAIM = [{
    "id": "s2", "from": "support@vendorbeingpursued.com", "subject": "Re: refund",
    "date": "Sat, 02 Aug 2026 10:00:00 -0500",
    "body": "My records show a $129.99 refund credited to that account.",
}]

# The OTHER shape of non-answer: the vendor confirms the account and restates the CHARGES
# that went out, without ever granting the refund that was demanded. Money moving the wrong
# way, dressed as a helpful reply. It must not close a case either.
_WRONG_CONFIRMATION_REPLY = (
    "I have reviewed the account and can confirm it has been active since 2024. "
    "The charges of $120.00 and $65.00 were both billed to the card on file as expected.")

# A refund arriving in TWO parts — a real behaviour, and the all-or-nothing case: until
# BOTH credits post, the case is not closable.
_AMOUNTS_SPLIT = [120.00, 65.00]
_GRANTED_REPLY_SPLIT = ("A refund has been issued in two parts: $120.00 and $65.00 have "
                        "been credited back to your original payment method.")
_BANK_POSTED_ONE_OF_TWO = [{
    "id": "s3", "from": "alerts@examplebank.com", "subject": "Credit posted",
    "date": "Sat, 02 Aug 2026 10:00:00 -0500",
    "body": "A credit of $120.00 has been posted to your account ending 4321.",
}]
_BANK_POSTED_BOTH = [{
    "id": "s4", "from": "alerts@examplebank.com", "subject": "Credits posted",
    "date": "Sat, 02 Aug 2026 10:00:00 -0500",
    "body": "A credit of $120.00 has been posted, and a refund of $65.00 was credited "
            "to your account ending 4321.",
}]


def _selftest():
    ok = True
    results = []

    def check(name, cond, detail=""):
        nonlocal ok
        if not cond:
            ok = False
        results.append(("PASS" if cond else "FAIL", name, detail))

    print("=== close_case self-test (fabricated data, stubbed API, no network) ===\n")

    # 1) NO CONFIRMED FUNDS -> refused. The vendor's own "we credited you" is a promise.
    s1 = _StubAPI()
    v1 = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [129.99], "2026-07-20",
                    confirm_close=True, api=s1, dry_run=False,
                    gmail_search=_search(_VENDOR_CLAIM), human_confirm_file="/nonexistent")
    print("Test 1 (remedy granted, money NOT landed):\n   %s" % v1["reason"])
    check("close REFUSED without confirmed funds", v1["closed"] is False)
    check("exit is non-zero", v1["exit"] != 0, "exit=%s" % v1["exit"])
    check("NOTHING was written", s1.writes == [], repr(s1.writes))

    # 2) CANNOT CHECK -> refused (fail closed), and distinctly so.
    s2 = _StubAPI()
    v2 = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [129.99], "2026-07-20",
                    confirm_close=True, api=s2, dry_run=False,
                    gmail_search=_boom_search, human_confirm_file="/nonexistent")
    print("\nTest 2 (mailbox unreachable):\n   %s" % v2["reason"])
    check("close REFUSED when it cannot check", v2["closed"] is False)
    check("reported as 'cannot check', exit 3", v2["exit"] == EXIT_CANNOT_CHECK,
          "exit=%s" % v2["exit"])
    check("nothing written when the gate cannot run", s2.writes == [], repr(s2.writes))

    # 2b) No amount at all -> cannot check -> refused.
    s2b = _StubAPI()
    v2b = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [], "2026-07-20",
                     confirm_close=True, api=s2b, dry_run=False,
                     gmail_search=_search(_BANK_POSTED), human_confirm_file="/nonexistent")
    print("\nTest 2b (no expected amount):\n   %s" % v2b["reason"])
    check("no amount -> cannot check -> refused", v2b["closed"] is False
          and v2b["exit"] == EXIT_CANNOT_CHECK, "exit=%s" % v2b["exit"])
    check("nothing written", s2b.writes == [])

    # 2c) No vendor reply -> cannot check -> refused.
    s2c = _StubAPI()
    v2c = close_case(_CASE, _REMEDY, "", [129.99], "2026-07-20", confirm_close=True,
                     api=s2c, dry_run=False, gmail_search=_search(_BANK_POSTED),
                     human_confirm_file="/nonexistent")
    print("\nTest 2c (no vendor reply text):\n   %s" % v2c["reason"])
    check("no reply text -> cannot check -> refused", v2c["closed"] is False
          and v2c["exit"] == EXIT_CANNOT_CHECK, "exit=%s" % v2c["exit"])

    # 3) 'ESCALATED INTERNALLY' never closes a case (SKILL.md §6.5) — even with the money
    #    confirmed landed and the confirm flag given.
    s3 = _StubAPI()
    v3 = close_case(_CASE, _REMEDY, _BRUSHOFF_REPLY, [129.99], "2026-07-20",
                    confirm_close=True, api=s3, dry_run=False,
                    gmail_search=_search(_BANK_POSTED), human_confirm_file="/nonexistent")
    print("\nTest 3 ('escalated internally' + money landed):\n   %s" % v3["reason"])
    check("'escalated internally' never closes a case", v3["closed"] is False)
    check("nothing written on a brush-off", s3.writes == [], repr(s3.writes))

    # 3b) The other non-answer: the vendor confirms the ACCOUNT and restates the CHARGES
    #     that went out, never granting the refund. Money the wrong way is not a refund.
    s3b = _StubAPI()
    v3b = close_case(_CASE, _REMEDY, _WRONG_CONFIRMATION_REPLY, _AMOUNTS_SPLIT, "2026-07-20",
                     confirm_close=True, api=s3b, dry_run=False,
                     gmail_search=_search(_BANK_POSTED_BOTH), human_confirm_file="/nonexistent")
    print("\nTest 3b (vendor confirms the account + restates charges):\n   %s" % v3b["reason"])
    check("a wrong-thing confirmation never closes a case", v3b["closed"] is False)
    check("nothing written on a wrong-thing confirmation", s3b.writes == [], repr(s3b.writes))

    # 3c) A SPLIT refund: one of two credits posts -> still NOT closable (all-or-nothing).
    s3c = _StubAPI()
    v3c = close_case(_CASE, _REMEDY, _GRANTED_REPLY_SPLIT, _AMOUNTS_SPLIT, "2026-07-20",
                     confirm_close=True, api=s3c, dry_run=False,
                     gmail_search=_search(_BANK_POSTED_ONE_OF_TWO),
                     human_confirm_file="/nonexistent")
    print("\nTest 3c (split refund, only 1 of 2 credits posted):\n   %s" % v3c["reason"])
    check("a partly-landed split refund does NOT close the case", v3c["closed"] is False)
    check("nothing written while a credit is still missing", s3c.writes == [])

    # 3d) ... and once BOTH parts post, it closes.
    s3d = _StubAPI()
    v3d = close_case(_CASE, _REMEDY, _GRANTED_REPLY_SPLIT, _AMOUNTS_SPLIT, "2026-07-20",
                     confirm_close=True, api=s3d, dry_run=False,
                     gmail_search=_search(_BANK_POSTED_BOTH), human_confirm_file="/nonexistent")
    print("\nTest 3d (split refund, both credits posted):\n   %s" % v3d["reason"])
    check("a fully-landed split refund closes", v3d["closed"] is True, v3d["reason"][:90])

    # 4) FUNDS CONFIRMED but NO --confirm-close -> refused (RED lane, SKILL.md §5).
    s4 = _StubAPI()
    v4 = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [129.99], "2026-07-20",
                    confirm_close=False, api=s4, dry_run=False,
                    gmail_search=_search(_BANK_POSTED), human_confirm_file="/nonexistent")
    print("\nTest 4 (gates clear, no --confirm-close):\n   %s" % v4["reason"])
    check("gates cleared but close still REFUSED", v4["closed"] is False and
          v4["may_close"] is True)
    check("nothing written without the RED-lane flag", s4.writes == [], repr(s4.writes))

    # 5) DRY RUN is the default: gates clear, flag given, still writes nothing.
    s5 = _StubAPI()
    v5 = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [129.99], "2026-07-20",
                    confirm_close=True, api=s5, gmail_search=_search(_BANK_POSTED),
                    human_confirm_file="/nonexistent")
    print("\nTest 5 (dry run):\n   %s" % v5["reason"])
    check("dry run writes nothing", v5["closed"] is False and s5.writes == [])

    # 6) THE ONLY PATH THAT CLOSES: granted + landed + confirmed + live.
    s6 = _StubAPI()
    v6 = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [129.99], "2026-07-20",
                    confirm_close=True, api=s6, dry_run=False,
                    gmail_search=_search(_BANK_POSTED), human_confirm_file="/nonexistent")
    print("\nTest 6 (granted + landed + confirmed + live):\n   %s" % v6["reason"])
    check("case CLOSES only with all three", v6["closed"] is True, v6["reason"][:90])
    check("exit 0", v6["exit"] == EXIT_CLOSED, "exit=%s" % v6["exit"])
    check("MR Phase written FIRST, then status",
          [w[0] for w in s6.writes] == ["set_properties", "update_issue"], repr(s6.writes))
    check("MR Phase=Closed", s6.writes[0][2].get("MR Phase") == CLOSED_PHASE,
          repr(s6.writes[0]))
    check("status=done", s6.writes[1][2].get("status") == CLOSED_STATUS, repr(s6.writes[1]))

    # 7) Human-confirm log is an accepted proof of landing (the intended fallback when the
    #    bank never emails) — and only for amounts dated at/after the resolution date.
    import tempfile
    hc = os.path.join(tempfile.gettempdir(), "close_case_selftest_confirm.json")
    with open(hc, "w", encoding="utf-8") as fh:
        json.dump({"TEST-C": [{"amount": 129.99, "date": "2026-08-02",
                               "note": "saw it post", "by": "self-test"}]}, fh)
    s7 = _StubAPI()
    v7 = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [129.99], "2026-07-20",
                    confirm_close=True, api=s7, dry_run=False,
                    gmail_search=_search([]), human_confirm_file=hc)
    print("\nTest 7 (human-confirm log):\n   %s" % v7["reason"])
    check("human-confirm log closes the case", v7["closed"] is True, v7["reason"][:90])

    # 7b) ... but a confirmation dated BEFORE the resolution date does not count.
    with open(hc, "w", encoding="utf-8") as fh:
        json.dump({"TEST-C": [{"amount": 129.99, "date": "2026-05-01",
                               "note": "an older credit", "by": "self-test"}]}, fh)
    s7b = _StubAPI()
    v7b = close_case(_CASE, _REMEDY, _GRANTED_REPLY, [129.99], "2026-07-20",
                     confirm_close=True, api=s7b, dry_run=False,
                     gmail_search=_search([]), human_confirm_file=hc)
    print("\nTest 7b (confirmation predates the resolution):\n   %s" % v7b["reason"])
    check("a pre-resolution credit does NOT close the case", v7b["closed"] is False)
    check("nothing written", s7b.writes == [])
    try:
        os.remove(hc)
    except OSError:
        pass

    # ---------------------------------------------------------------------------------
    # M44 — the three non-money remedies now have a close path, and it is just as strict.
    # ---------------------------------------------------------------------------------
    _REPL_CASE = {"identifier": "TEST-R", "id": "test-r",
                  "mr": {"MR Remedy Type": "Replacement"}}
    _REPL_GRANTED = ("We are sorry about the fault. A replacement unit has been shipped to "
                     "your address on file.")
    _CARRIER_DELIVERED = [{
        "id": "r1", "from": "auto-notify@ups.com", "subject": "Your package was delivered",
        "date": "Sun, 03 Aug 2026 14:02:00 -0500",
        "body": "Your replacement shipment 1Z999AA10123456784 was delivered and left at "
                "the front door."}]
    _CARRIER_IN_TRANSIT = [{
        "id": "r2", "from": "auto-notify@ups.com", "subject": "Shipment created",
        "date": "Fri, 01 Aug 2026 09:00:00 -0500",
        "body": "Your replacement 1Z999AA10123456784 has shipped and is in transit."}]

    # 8) A replacement with the parcel still in transit does NOT close.
    s8 = _StubAPI()
    v8 = close_case(_REPL_CASE, "replacement", _REPL_GRANTED, [], "2026-07-20",
                    confirm_close=True, api=s8, dry_run=False, tokens=["1Z999AA10123456784"],
                    gmail_search=_search(_CARRIER_IN_TRANSIT),
                    human_confirm_file="/nonexistent")
    print("\nTest 8 (replacement shipped but not delivered):\n   %s" % v8["reason"])
    check("a replacement in transit does NOT close", v8["closed"] is False)
    check("nothing written while the parcel is moving", s8.writes == [], repr(s8.writes))

    # 9) ... and once the CARRIER says delivered, it closes. No amount anywhere.
    s9 = _StubAPI()
    v9 = close_case(_REPL_CASE, "replacement", _REPL_GRANTED, [], "2026-07-20",
                    confirm_close=True, api=s9, dry_run=False, tokens=["1Z999AA10123456784"],
                    gmail_search=_search(_CARRIER_DELIVERED),
                    human_confirm_file="/nonexistent")
    print("\nTest 9 (replacement delivered by the carrier):\n   %s" % v9["reason"])
    check("a delivered replacement closes with NO amount", v9["closed"] is True,
          v9["reason"][:90])
    check("MR Phase=Closed then status=done", [w[0] for w in s9.writes]
          == ["set_properties", "update_issue"], repr(s9.writes))

    # 10) A replacement with no tracking identifier cannot be checked -> hold.
    s10 = _StubAPI()
    v10 = close_case(_REPL_CASE, "replacement", _REPL_GRANTED, [], "2026-07-20",
                     confirm_close=True, api=s10, dry_run=False,
                     gmail_search=_search(_CARRIER_DELIVERED),
                     human_confirm_file="/nonexistent")
    print("\nTest 10 (replacement, no tracking identifier):\n   %s" % v10["reason"])
    check("no tracking id -> cannot check -> refused",
          v10["closed"] is False and v10["exit"] == EXIT_CANNOT_CHECK,
          "exit=%s" % v10["exit"])

    # 11) A REPAIR needs the owner's word that it works — the carrier's is not enough.
    _REPAIR_CASE = {"identifier": "TEST-P", "id": "test-p",
                    "mr": {"MR Remedy Type": "Repair"}}
    _REPAIR_GRANTED = ("The repair on your unit has been completed and it has been "
                       "returned to you.")
    _REPAIR_BACK = [{
        "id": "p1", "from": "auto-notify@fedex.com", "subject": "Delivered",
        "date": "Sun, 09 Aug 2026 11:00:00 -0500",
        "body": "Repair RMA-88213 was delivered and signed for."}]
    s11 = _StubAPI()
    v11 = close_case(_REPAIR_CASE, "repair", _REPAIR_GRANTED, [], "2026-07-20",
                     confirm_close=True, api=s11, dry_run=False, tokens=["RMA-88213"],
                     gmail_search=_search(_REPAIR_BACK), human_confirm_file="/nonexistent")
    print("\nTest 11 (repair unit back, working not verified):\n   %s" % v11["reason"])
    check("a returned-but-unverified repair does NOT close", v11["closed"] is False)
    check("nothing written on an unverified repair", s11.writes == [], repr(s11.writes))

    # 11b) ... and the owner's confirmation closes it.
    hcp = os.path.join(tempfile.gettempdir(), "close_case_m44_confirm.json")
    with open(hcp, "w", encoding="utf-8") as fh:
        json.dump({"TEST-P": [{"kind": "repair", "date": "2026-08-09", "confirmed": True,
                               "token": "RMA-88213", "note": "tested, works"}]}, fh)
    s11b = _StubAPI()
    v11b = close_case(_REPAIR_CASE, "repair", _REPAIR_GRANTED, [], "2026-07-20",
                      confirm_close=True, api=s11b, dry_run=False, tokens=["RMA-88213"],
                      gmail_search=_search(_REPAIR_BACK), human_confirm_file=hcp)
    print("\nTest 11b (repair confirmed working by the owner):\n   %s" % v11b["reason"])
    check("an owner-verified repair closes", v11b["closed"] is True, v11b["reason"][:90])

    # 12) STORE CREDIT: the amount must be named and a real credit artifact handed over.
    _SC_CASE = {"identifier": "TEST-S", "id": "test-s",
                "mr": {"MR Remedy Type": "StoreCredit"}}
    _SC_GRANTED = "A store credit of $75.00 has been issued to your account."
    _SC_PROMISE = [{
        "id": "c1", "from": "care@vendorbeingpursued.example", "subject": "Your credit",
        "date": "Mon, 03 Aug 2026 10:00:00 -0500",
        "body": "A store credit of $75.00 will be issued to your account shortly."}]
    _SC_ISSUED = [{
        "id": "c2", "from": "care@vendorbeingpursued.example", "subject": "Your credit",
        "date": "Mon, 03 Aug 2026 10:00:00 -0500",
        "body": "A store credit of $75.00 has been added to your account. "
                "Gift certificate code: SC-4KQ2-99XB1. It is available now."}]
    s12 = _StubAPI()
    v12 = close_case(_SC_CASE, "store credit", _SC_GRANTED, [75.00], "2026-07-20",
                     confirm_close=True, api=s12, dry_run=False,
                     gmail_search=_search(_SC_PROMISE), human_confirm_file="/nonexistent")
    print("\nTest 12 (store credit promised, no code):\n   %s" % v12["reason"])
    check("a promised store credit does NOT close", v12["closed"] is False)
    check("nothing written on a credit promise", s12.writes == [], repr(s12.writes))

    s12b = _StubAPI()
    v12b = close_case(_SC_CASE, "store credit", _SC_GRANTED, [75.00], "2026-07-20",
                      confirm_close=True, api=s12b, dry_run=False,
                      gmail_search=_search(_SC_ISSUED), human_confirm_file="/nonexistent")
    print("\nTest 12b (store credit issued with a code):\n   %s" % v12b["reason"])
    check("an issued store credit with a code closes", v12b["closed"] is True,
          v12b["reason"][:90])

    # 13) A case whose remedy type is UNSET and which has no amount cannot be checked.
    s13 = _StubAPI()
    v13 = close_case({"identifier": "TEST-U", "id": "test-u"}, "refund or replacement",
                     _GRANTED_REPLY, [], "2026-07-20", confirm_close=True, api=s13,
                     dry_run=False, gmail_search=_search(_BANK_POSTED),
                     human_confirm_file="/nonexistent")
    print("\nTest 13 (ambiguous remedy, no amount):\n   %s" % v13["reason"])
    check("an ambiguous remedy type is never guessed",
          v13["closed"] is False and v13["exit"] == EXIT_CANNOT_CHECK,
          "exit=%s" % v13["exit"])
    check("nothing written on an unknown remedy type", s13.writes == [])
    try:
        os.remove(hcp)
    except OSError:
        pass

    print("")
    for status, name, detail in results:
        print("[%s] %s%s" % (status, name,
                             ("  -> %s" % detail) if detail and status == "FAIL" else ""))
    print("\n%s" % ("ALL PASS" if ok else "SOME FAILED"))
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Close a merchandise-return case. Structurally gated on refund_landed.")
    ap.add_argument("--selftest", action="store_true",
                    help="offline self-test: stubbed API, fabricated data, no network")
    ap.add_argument("--case", help="case identifier on the board, e.g. CASE-1")
    ap.add_argument("--remedy", default="refund",
                    help="the remedy that was DEMANDED, in the words used to the vendor. "
                         "This is M19's input — what the reply is tested against.")
    ap.add_argument("--remedy-type", default=None,
                    help="override for gate (B): refund | replacement | repair | "
                         "store_credit. Normally read from `%s` on the issue; this flag "
                         "exists for a case whose property is not set yet."
                         % RL.REMEDY_TYPE_PROP)
    ap.add_argument("--token", action="append", default=[], dest="tokens",
                    help="tracking number / RMA / credit code; repeat. Required to close a "
                         "replacement.")
    ap.add_argument("--reply-file", help="file holding the vendor's reply text (M19 input)")
    ap.add_argument("--amount", action="append", default=[],
                    help="expected amount; repeat for multiple credits (ALL must land)")
    ap.add_argument("--since", help="resolution date YYYY-MM-DD; only credits at/after count")
    ap.add_argument("--confirm-close", action="store_true",
                    help="the RED-lane per-item YES (SKILL.md §5). Required to close.")
    ap.add_argument("--live", action="store_true",
                    help="actually write. Without this nothing is written.")
    ap.add_argument("--llm", action="store_true", help="also ask the M19 LLM judge")
    a = ap.parse_args(argv)

    if a.selftest:
        return 0 if _selftest() else 1
    if not a.case:
        print("Nothing to do: --case is required (or --selftest).")
        return EXIT_CANNOT_CHECK

    reply = ""
    if a.reply_file:
        try:
            with open(a.reply_file, encoding="utf-8") as fh:
                reply = fh.read()
        except IOError as exc:
            print("HOLD (cannot check): could not read --reply-file (%s)." % exc)
            return EXIT_CANNOT_CHECK

    try:
        case = _load_case(a.case)
    except Exception as exc:
        print("HOLD (cannot check): could not read %s off the board (%s: %s). A gate that "
              "cannot check must not let a case close." % (a.case, type(exc).__name__, exc))
        return EXIT_CANNOT_CHECK

    try:
        v = close_case(case, a.remedy, reply, a.amount, a.since,
                       confirm_close=a.confirm_close, dry_run=not a.live, use_llm=a.llm,
                       remedy_type=a.remedy_type, tokens=a.tokens)
    except Exception as exc:
        print("HOLD (cannot check): the close path failed (%s: %s)."
              % (type(exc).__name__, exc))
        return EXIT_CANNOT_CHECK

    print(v["reason"])
    if v["resolution"]:
        print("  M19 resolved : %s (%s)" % (v["resolution"].get("resolved"),
                                            v["resolution"].get("reason", "")[:120]))
    if v["refund"]:
        r = v["refund"]
        print("  remedy       : %s" % (r.get("remedy_type") or "(unset)"))
        print("  M28/M44      : landed=%s (confidence %s) for %s since %s"
              % (r["landed"], r["confidence"],
                 ", ".join(r.get("amounts") or r.get("anchors") or ["-"]), r["since"]))
        for e in r["evidence"]:
            print("    evidence   : [%s] %s"
                  % (e.get("source"), (e.get("detail") or e.get("snippet")
                                       or e.get("note", ""))[:150]))
    for w in v["writes"]:
        print("  WROTE        : %s" % w)
    if not v["closed"] and v["exit"] != EXIT_CLOSED:
        print("\n" + RL._human_confirm_howto())
    return v["exit"]


if __name__ == "__main__":
    sys.exit(main())
