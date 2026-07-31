#!/usr/bin/env python3
"""stop_list.py — the DO-NOT-CONTACT register (M49).

WHY THIS FILE EXISTS
--------------------
On 2026-07-28 the user said, of a party this engine had just written to: *"do not send the letter
to Ron."* The letter had already gone about fifteen minutes earlier, and it could not be recalled.
Then, correctly, he said the agency was not a respondent and no further contact should go to them.

Nothing in the engine could hold that instruction. "Never contact them again" lived in a comment
on a Multica issue and in a session's memory — and a session is stateless, while the VPS runtime
never read the comment at all. The next scheduled nudge, follow-up or draft against that case had
no idea the recipient was off-limits. The only thing standing between a withdrawn instruction and
another email was somebody remembering, which is precisely the failure class BLUEPRINT §10 exists
to eliminate.

So: a recipient the user has withdrawn is a STRUCTURAL fact, stored on disk, checked before every
send, by every runtime.

DESIGN NOTES, each one a lesson from a prior incident
----------------------------------------------------
* **It sits with `idempotency`, not beside it.** `reserve_send()` calls it. A guard reachable only
  by a caller who chooses to look is not a guard (M22's lesson, relearned in #14).
* **It fails CLOSED on an unreadable register.** A corrupt file is not "nobody is blocked" — that
  reading is what let the corrupt-ledger bug silently reset an entire send history (M45).
* **Blocks are matched on the ADDRESS and on the DOMAIN.** "Do not contact the agency" means the
  firm, not one mailbox at it. Free-mail hosts are the exception: blocking `gmail.com` would block
  the world, so a gmail address blocks only that exact address. Same asymmetry as case_queries.
* **A block is never silently forgotten.** Entries carry who set it, when, and why, and `release()`
  records the removal rather than deleting history. A stop that vanished without a trace would be
  indistinguishable from one that was never set.
* **Scope is per-case OR global.** `case=None` blocks a recipient everywhere, which is what "never
  contact them again" actually means. A per-case block is the narrower, commoner form.

CLI
    stop_list.py --selftest
    stop_list.py --list
    stop_list.py --block <recipient> [--case MER-79] --why "user withdrew contact" [--live]
    stop_list.py --release <recipient> [--case MER-79] --why "..." [--live]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")

REGISTER = os.environ.get(
    "MER_STOP_LIST",
    os.path.join(os.path.dirname(os.environ.get("MER_SENT_LEDGER",
                                                "/opt/data/scripts/mer_sent_ledger.json")) or ".",
                 "mer_stop_list.json"))

GLOBAL = "*"          # the case key meaning "everywhere"


class StopListCorrupt(RuntimeError):
    """The register exists but could not be read. NEVER read as 'nobody is blocked'."""


def _now():
    return datetime.now(timezone.utc)


def _broad_hosts():
    """Shared with case_queries so 'too broad to block on' has one definition."""
    try:
        import case_queries
        return case_queries.BROAD_DOMAINS
    except Exception:
        return frozenset("""gmail.com googlemail.com yahoo.com hotmail.com outlook.com live.com
        icloud.com aol.com proton.me protonmail.com gmx.com mail.com zoho.com yandex.com
        zendesk.com freshdesk.com helpscout.net intercom.io front.com frontapp.com""".split())


def _load():
    if not os.path.exists(REGISTER):
        return {"blocks": [], "history": []}
    try:
        with open(REGISTER, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        raise StopListCorrupt("%s is present but unreadable (%s: %s). Refusing to treat that as "
                              "'nothing is blocked'." % (REGISTER, type(e).__name__, e))
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise StopListCorrupt("%s has an unexpected shape; refusing to guess." % REGISTER)
    data.setdefault("history", [])
    return data


def _save(data):
    try:
        import idempotency
        lock = idempotency.file_lock(REGISTER)
    except Exception:
        import contextlib
        lock = contextlib.nullcontext()
    d = os.path.dirname(REGISTER) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    with lock:
        tmp = REGISTER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        try:
            os.chmod(tmp, 0o664)
        except OSError:
            pass
        os.replace(tmp, REGISTER)


def _norm(recipient):
    return (recipient or "").strip().lower()


def _host(addr):
    return addr.split("@", 1)[1] if "@" in addr else ""


def block(recipient, case=None, why="", who="user", live=True):
    """Record a do-not-contact. `case=None` blocks the recipient EVERYWHERE."""
    addr = _norm(recipient)
    if not addr:
        raise ValueError("a stop needs a recipient")
    if not why:
        raise ValueError("a stop needs a reason — an unexplained block is unreviewable")
    entry = {"recipient": addr, "case": (case or GLOBAL), "why": why, "who": who,
             "at": _now().isoformat()}
    if not live:
        return {"would_add": entry}
    data = _load()
    for b in data["blocks"]:
        if b["recipient"] == addr and b["case"] == entry["case"]:
            return {"already": b}
    data["blocks"].append(entry)
    data["history"].append(dict(entry, event="block"))
    _save(data)
    return {"added": entry}


def release(recipient, case=None, why="", who="user", live=True):
    """Lift a stop. The removal is recorded; history is never rewritten."""
    addr = _norm(recipient)
    target = case or GLOBAL
    if not live:
        return {"would_release": {"recipient": addr, "case": target}}
    data = _load()
    keep = [b for b in data["blocks"]
            if not (b["recipient"] == addr and b["case"] == target)]
    removed = len(data["blocks"]) - len(keep)
    if removed:
        data["blocks"] = keep
        data["history"].append({"recipient": addr, "case": target, "why": why, "who": who,
                                "at": _now().isoformat(), "event": "release"})
        _save(data)
    return {"released": removed}


def is_blocked(recipient, case=None):
    """-> (blocked: bool, reason: str). Raises StopListCorrupt rather than failing open.

    Matches, in order: the exact address for this case; the exact address globally; the DOMAIN for
    this case; the DOMAIN globally. A free-mail host never matches as a domain."""
    addr = _norm(recipient)
    if not addr:
        return False, ""
    host = _host(addr)
    data = _load()
    broad = _broad_hosts()
    for b in data["blocks"]:
        b_addr, b_case = b.get("recipient", ""), b.get("case", GLOBAL)
        scoped = b_case in (GLOBAL, case)
        if not scoped:
            continue
        if b_addr == addr:
            return True, "%s is on the do-not-contact register (%s, set by %s on %s)" % (
                addr, b.get("why", "no reason recorded"), b.get("who", "?"), b.get("at", "?")[:10])
        # a bare domain block, or an address block promoted to its company domain
        b_host = b_addr if "@" not in b_addr else _host(b_addr)
        if b_host and b_host == host and b_host not in broad:
            return True, "the domain %s is on the do-not-contact register (%s, set by %s on %s)" % (
                b_host, b.get("why", "no reason recorded"), b.get("who", "?"),
                b.get("at", "?")[:10])
    return False, ""


def blocks(case=None):
    """Every block in force, optionally narrowed to a case (global blocks always included)."""
    return [b for b in _load()["blocks"]
            if case is None or b.get("case") in (GLOBAL, case)]


# ------------------------------------------------------------------------------------ CLI
def _print_list():
    try:
        data = _load()
    except StopListCorrupt as e:
        print("CORRUPT: %s" % e)
        return 1
    if not data["blocks"]:
        print("do-not-contact register is empty (%s)" % REGISTER)
        return 0
    print("DO-NOT-CONTACT REGISTER (%s)" % REGISTER)
    for b in sorted(data["blocks"], key=lambda x: (x.get("case", ""), x.get("recipient", ""))):
        scope = "ALL CASES" if b.get("case") == GLOBAL else b.get("case")
        print("  %-34s %-12s %s" % (b.get("recipient"), scope, b.get("why", "")))
    return 0


def _selftest():
    import tempfile
    global REGISTER
    ok = [True]

    def check(name, cond, detail=""):
        ok[0] = ok[0] and bool(cond)
        print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                             ("  -> %s" % detail) if detail and not cond else ""))

    d = tempfile.mkdtemp()
    REGISTER = os.path.join(d, "stop.json")

    check("an empty register blocks nobody", is_blocked("x@y.com")[0] is False)

    block("withdrawn.contact@example.com", case=None, why="user withdrew contact", live=True)
    b, why = is_blocked("withdrawn.contact@example.com", case="MER-79")
    check("a global block applies to every case", b is True, why)
    check("the reason is carried back", "withdrew" in why, why)
    check("a free-mail host does NOT promote to a domain block",
          is_blocked("someone.else@gmail.com")[0] is False)

    block("care@vendor.com", case="MER-3", why="wrong department", live=True)
    check("a per-case block applies to its case", is_blocked("care@vendor.com", "MER-3")[0] is True)
    check("...and NOT to another case", is_blocked("care@vendor.com", "MER-9")[0] is False)
    check("a company address promotes to its domain",
          is_blocked("someone@vendor.com", "MER-3")[0] is True)

    check("a duplicate block is idempotent",
          "already" in block("care@vendor.com", case="MER-3", why="again", live=True))
    check("a block needs a reason", _raises(lambda: block("a@b.com", why="", live=True)))
    check("dry run writes nothing",
          "would_add" in block("z@z.com", why="x", live=False)
          and is_blocked("z@z.com")[0] is False)

    release("care@vendor.com", case="MER-3", why="resolved", live=True)
    check("release lifts the block", is_blocked("care@vendor.com", "MER-3")[0] is False)
    check("release is recorded in history",
          any(h.get("event") == "release" for h in _load()["history"]))
    check("the global block survives an unrelated release",
          is_blocked("withdrawn.contact@example.com")[0] is True)

    with open(REGISTER, "w", encoding="utf-8") as fh:
        fh.write('{"blocks": ')                       # truncated mid-write
    check("a corrupt register FAILS CLOSED (raises, never 'nobody blocked')",
          _raises(lambda: is_blocked("anyone@anywhere.com"), StopListCorrupt))

    print("\n%s" % ("ALL PASS" if ok[0] else "SOME FAILED"))
    return ok[0]


def _raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="the do-not-contact register")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--block")
    ap.add_argument("--release")
    ap.add_argument("--case")
    ap.add_argument("--why", default="")
    ap.add_argument("--who", default="user")
    ap.add_argument("--live", action="store_true", help="required to write anything")
    a = ap.parse_args(argv)

    if a.selftest:
        return 0 if _selftest() else 1
    if a.block:
        print(json.dumps(block(a.block, case=a.case, why=a.why, who=a.who, live=a.live), indent=2))
        if not a.live:
            print("\nDRY RUN — nothing written. Re-run with --live.")
        return 0
    if a.release:
        print(json.dumps(release(a.release, case=a.case, why=a.why, who=a.who, live=a.live),
                         indent=2))
        if not a.live:
            print("\nDRY RUN — nothing written. Re-run with --live.")
        return 0
    return _print_list()


if __name__ == "__main__":
    sys.exit(main())
