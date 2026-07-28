#!/usr/bin/env python3
"""test_multica_api.py -- offline tests for the M45 board-write integrity fix.

    python test_multica_api.py

NO NETWORK, NO TOKEN, NO BOARD. Every HTTP call is intercepted, so this file can run in
CI, on a laptop with no credentials, and on the VPS without touching a live case.

What it protects, and why each one is here:

  * READ-BACK VERIFICATION. A fake server that accepts every PUT with 200 OK and stores
    nothing -- the exact 2026-07-26 trap -- must produce PropertyWriteVerificationError,
    not a silent success. This is the test that keeps "I set the property" checkable.
  * PROSE IS NOT A WRITE. MER-76 (2026-07-28) sat at Phase=Intake for a day because an
    agent wrote "MR Phase: Tier1" into a COMMENT. add_comment must never touch properties.
  * VALUE SURVIVAL. "$2,500", quotes and newlines must arrive byte-identical; a related
    incident turned "$2,500" into ",500" by routing content through a shell.
  * FAIL BEFORE WRITING. A bad option in the 3rd pair must leave the 1st two unwritten --
    half a phase change looks like a finished one.
  * AUTH. Missing token and 401 both raise MulticaAuthError with an actionable message.
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MULTICA_TOKEN", "test-token")
os.environ.setdefault("MULTICA_WORKSPACE_ID", "ws-test")

import multica_api as mc            # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------- fake board

PROPS = [
    {"id": "p-phase", "name": "MR Phase", "type": "select",
     "config": {"options": [{"id": "o-intake", "name": "Intake"},
                            {"id": "o-t1", "name": "Tier1"},
                            {"id": "o-t2", "name": "Tier2"}]}},
    {"id": "p-intake", "name": "MR Intake Complete", "type": "checkbox", "config": {}},
    {"id": "p-deadline", "name": "MR Phase Deadline", "type": "date", "config": {}},
    {"id": "p-note", "name": "MR Delay Explanation", "type": "text", "config": {}},
]


class FakeBoard(object):
    """A stand-in Multica. `honest=False` reproduces the silent-discard bug exactly."""

    def __init__(self, honest=True):
        self.honest = honest
        self.stored = {}
        self.comments = []
        self.puts = []

    def __call__(self, method, path, params=None, body=None, headers=None):
        self.calls = getattr(self, "calls", [])
        self.calls.append((method, path))
        if method == "GET" and path == "/properties":
            return {"properties": PROPS}
        if method == "GET" and path.startswith("/issues/"):
            return {"issue": {"id": "i-1", "identifier": "TEST-1", "title": "scratch",
                              "properties": dict(self.stored)}}
        if method == "PUT" and "/properties/" in path:
            pid = path.rsplit("/", 1)[1]
            self.puts.append((pid, (body or {}).get("value")))
            if self.honest:
                self.stored[pid] = (body or {}).get("value")
            return {"ok": True}                      # 200 either way -- that is the trap
        if method == "POST" and path.endswith("/comments"):
            self.comments.append((body or {}).get("content"))
            return {"ok": True}
        raise AssertionError("unexpected call %s %s" % (method, path))


def with_board(board, fn):
    real = mc._req
    mc._req = board
    try:
        return fn()
    finally:
        mc._req = real


# ------------------------------------------------------------------------- tests

def test_coercion():
    print("coercion")
    defs = {p["name"]: dict(id=p["id"], name=p["name"], type=p["type"],
                            options={o["id"]: o["name"]
                                     for o in (p["config"].get("options") or [])})
            for p in PROPS}
    d = defs["MR Phase"]
    check("select label -> option id", mc.encode_property_value(d, "Tier1") == "o-t1")
    check("select is case-insensitive", mc.encode_property_value(d, "tier1") == "o-t1")
    try:
        mc.encode_property_value(d, "Tier9")
        check("unknown option raises", False)
    except ValueError as e:
        check("unknown option raises and lists the valid ones",
              "Tier1" in str(e) and "Tier2" in str(e), e)

    cb = defs["MR Intake Complete"]
    for word in ("true", "TRUE", "yes", "1", "on"):
        check("checkbox %r -> True" % word, mc.encode_property_value(cb, word) is True)
    for word in ("false", "no", "0", "off"):
        # THE BUG THIS PREVENTS: bool("false") is True. A gate would read as satisfied
        # when the operator typed the exact opposite of what they meant.
        check("checkbox %r -> False" % word, mc.encode_property_value(cb, word) is False)
    try:
        mc.encode_property_value(cb, "maybe")
        check("ambiguous checkbox raises", False)
    except ValueError:
        check("ambiguous checkbox raises", True)

    dt = defs["MR Phase Deadline"]
    check("date passes through", mc.encode_property_value(dt, "2026-08-04") == "2026-08-04")
    check("datetime is trimmed to a date",
          mc.encode_property_value(dt, "2026-08-04T00:00:00Z") == "2026-08-04")
    import datetime
    check("date object", mc.encode_property_value(dt, datetime.date(2026, 8, 4)) == "2026-08-04")
    for bad in ("next tuesday", "2026-13-01", "08/04/2026"):
        try:
            mc.encode_property_value(dt, bad)
            check("invalid date %r raises" % bad, False)
        except ValueError:
            check("invalid date %r raises" % bad, True)

    txt = defs["MR Delay Explanation"]
    money = 'Vendor owes $2,500 for the "damaged" unit.\nLine two.\n'
    check("text survives $ , quotes and newlines byte for byte",
          mc.encode_property_value(txt, money) == money)


def test_readback_catches_silent_discard():
    print("read-back verification (THE fix)")
    values = {"MR Phase": "Tier1", "MR Intake Complete": True}

    honest = FakeBoard(honest=True)
    with_board(honest, lambda: mc.set_properties("i-1", values))
    check("an honest board verifies clean", honest.stored.get("p-phase") == "o-t1")

    liar = FakeBoard(honest=False)
    try:
        with_board(liar, lambda: mc.set_properties("i-1", values))
        check("a 200-OK-that-stores-nothing RAISES", False,
              "set_properties returned success for a write that never landed")
    except mc.PropertyWriteVerificationError as e:
        check("a 200-OK-that-stores-nothing RAISES PropertyWriteVerificationError", True)
        check("the exception names the property", e.property_name == "MR Phase", e.property_name)
        check("the exception carries intended and actual",
              e.intended == "Tier1" and e.actual in (None, ""), (e.intended, e.actual))
        check("every mismatch is listed, not just the first", len(e.mismatches) == 2, e.mismatches)
        check("still catchable as RuntimeError (old callers)", isinstance(e, RuntimeError))
        check("still catchable as MulticaAPIError", isinstance(e, mc.MulticaAPIError))
    check("the PUT was actually attempted (it is the READ that failed it)",
          len(liar.puts) == 2, liar.puts)


def test_prose_is_not_a_write():
    print("a comment is not a property (MER-76)")
    board = FakeBoard(honest=True)

    def go():
        mc.add_comment("i-1", "MR Intake Complete: TRUE\nMR Phase: Tier1")
        return mc.read_properties("i-1")[0]

    values = with_board(board, go)
    check("writing the sentence into a comment leaves MR Phase unset",
          not values.get("MR Phase"), values)
    check("the comment did land as a comment", len(board.comments) == 1)


def test_fail_before_writing_anything():
    print("fail-fast: nothing is written if any value is invalid")
    board = FakeBoard(honest=True)
    try:
        with_board(board, lambda: mc.set_properties(
            "i-1", {"MR Intake Complete": True, "MR Phase Deadline": "2026-08-04",
                    "MR Phase": "Tier9"}))
        check("a bad option aborts the whole set", False)
    except ValueError:
        check("a bad option aborts the whole set", True)
    check("NOTHING was written (no half-applied phase change)", board.puts == [], board.puts)


def test_name_resolution():
    print("property names resolve case-insensitively")
    board = FakeBoard(honest=True)
    with_board(board, lambda: mc.set_properties("i-1", {"mr phase": "tier2"}))
    check("'mr phase' resolves to 'MR Phase'", board.stored.get("p-phase") == "o-t2")
    try:
        with_board(board, lambda: mc.set_properties("i-1", {"MR Phaze": "Tier1"}))
        check("a misspelled property raises", False)
    except ValueError as e:
        check("a misspelled property raises and lists the real names", "MR Phase" in str(e), e)


def test_auth():
    print("auth failures are loud and actionable")
    saved_token, saved_env, saved_cfg = mc.TOKEN, os.environ.get("MULTICA_TOKEN"), mc.MULTICA_CLI_CONFIG
    try:
        mc.TOKEN = ""
        os.environ.pop("MULTICA_TOKEN", None)
        mc.MULTICA_CLI_CONFIG = os.path.join(tempfile.gettempdir(), "no-such-multica-config.json")
        try:
            mc._token()
            check("a missing token raises", False)
        except mc.MulticaAuthError as e:
            check("a missing token raises MulticaAuthError", True)
            check("the message says how to fix it", "MULTICA_TOKEN" in str(e), e)
        # Lazy, not import-time: setting the env var now must be enough.
        os.environ["MULTICA_TOKEN"] = "late-arriving-token"
        check("the token is read LAZILY, so a late env var works",
              mc._token() == "late-arriving-token")
    finally:
        mc.TOKEN = saved_token
        mc.MULTICA_CLI_CONFIG = saved_cfg
        if saved_env is None:
            os.environ.pop("MULTICA_TOKEN", None)
        else:
            os.environ["MULTICA_TOKEN"] = saved_env

    class Boom(object):
        def __call__(self, *a, **k):
            import urllib.error
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                         io.BytesIO(b'{"error":"invalid token"}'))

    real_open = mc.urllib.request.urlopen
    mc.urllib.request.urlopen = Boom()
    try:
        mc.list_issues()
        check("a 401 raises MulticaAuthError", False)
    except mc.MulticaAuthError as e:
        check("a 401 raises MulticaAuthError (not a generic API error)", e.status == 401)
    finally:
        mc.urllib.request.urlopen = real_open


def test_cli_value_parsing():
    print("CLI value parsing (@file, @@literal)")
    path = os.path.join(tempfile.gettempdir(), "m45_value.txt")
    money = 'Vendor owes $2,500 for the "damaged" unit.\nSecond line.\nThird.'
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(money)
    check("@file reads the file verbatim", mc._parse_value("@" + path) == money)
    check("@@ escapes a literal leading @", mc._parse_value("@@handle") == "@handle")
    check("a plain value is untouched", mc._parse_value("$2,500") == "$2,500")
    try:
        mc._parse_value("@/no/such/file.txt")
        check("a missing value file raises", False)
    except ValueError as e:
        check("a missing value file raises and explains @@", "@@" in str(e), e)
    pairs = dict(mc._parse_pairs(['MR Phase=Tier1', 'Note=a=b=c']))
    check("Name=Value splits on the FIRST = only", pairs.get("Note") == "a=b=c", pairs)
    os.remove(path)


def main():
    for t in (test_coercion, test_readback_catches_silent_discard, test_prose_is_not_a_write,
              test_fail_before_writing_anything, test_name_resolution, test_auth,
              test_cli_value_parsing):
        t()
    print("")
    if FAILS:
        print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
        return 1
    print("ALL PASS -- board-write integrity holds (no network was used)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
