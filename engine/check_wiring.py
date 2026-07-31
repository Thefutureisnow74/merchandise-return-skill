#!/usr/bin/env python3
"""
check_wiring.py — the STRUCTURAL gates for the Merchandise Returns Engine.

WHY THIS FILE EXISTS
--------------------
On 2026-07-26 an audit found SEVEN safety modules — remedy_gate, refund_landed,
sol_watchdog, dup_guard, delivery_check, unmatched_review, businessday — that had been
built, given a self-test, watched to go green, and marked DONE. Every one of them was
imported by NOTHING and scheduled by NOTHING. The invariants this repo recorded as
"enforced" held only because a human happened to perform those steps by hand.

The generating mechanism was a definition:

    Definition of Done  ==  "the module's self-test passes"

which is satisfiable by code that no live path can ever reach. This file replaces it:

    Definition of Done  ==  "a live call site exists"   <- checked here, mechanically

Three gates live here. All are pure static analysis: no imports of engine modules, no
network, no state, stdlib only. Nothing here can send mail or write to the board.

  --wiring       every module in this directory is imported by something, scheduled by
                 something, or invoked as a subprocess by something. Orphans fail the
                 build and are named with EXACTLY which conditions they failed.

  --send-path    ONE repo-wide rule about who may touch the send path, replacing the
                 per-module hand-rolled `_BANNED_IMPORTS` / `_send_mods` denylists. Those
                 were the same anti-pattern in miniature: each module enumerated its own
                 forbidden set by hand, so remedy_map.py's `_send_mods` omitted
                 "gmail_transport" while mer_dashboard.py's included it. A denylist you
                 write out by hand in fourteen places has fourteen chances to be wrong.
                 The canonical set is SEND_MODULES, below — import it, do not retype it.

  --board-write  no board write may be shell-interpolated. On 2026-07-28 "$2,500" became
                 ",500" because a multi-line value containing a `$` was passed unquoted
                 through a shell to the `multica` CLI, and the mangled figure entered the
                 client record. Board writes go through multica_api.py with structured
                 JSON, where a value is a value and not a token the shell may re-read.

USAGE
-----
    python3 check_wiring.py                 # run every gate (this is what CI runs)
    python3 check_wiring.py --wiring        # one gate only
    python3 check_wiring.py --send-path
    python3 check_wiring.py --board-write
    python3 check_wiring.py --selftest      # the gates' own tests (synthetic violations)
    python3 check_wiring.py --list-allowlist

Exit code is 0 only when every selected gate passes. Any violation exits 1.

THE ALLOWLISTS
--------------
Each allowlist entry is `name: "reason"` and the reason is MANDATORY — `--selftest`
fails if any entry carries an empty reason. That is deliberate. The failure this file
exists to stop was not "someone wrote a bad module", it was "someone made a thing true
by declaration instead of by construction". An allowlist without reasons is a silent
escape hatch; an allowlist with a written reason per line is a visible decision that
shows up in `git diff` and has to be defended in review.
"""

import argparse
import ast
import json
import os
import re
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# ===========================================================================================
# CANONICAL SETS — import these; never retype them in a module's own self-test.
# ===========================================================================================

#: Every module through which bytes can reach an SMTP socket. remedy_map.py's private
#: `_send_mods` omitted "gmail_transport" while mer_dashboard.py's included it; that class of
#: drift is only fixable by having one set. A read-only module must import NONE of these.
SEND_MODULES = frozenset({"gmail_transport", "mer_send", "send_queue", "smtplib"})

#: The last-mile transport. Importing it is a much stronger statement than importing, say,
#: send_queue: it is the module that owns the socket.
TRANSPORT_MODULE = "gmail_transport"

#: The function that actually puts a message on the wire.
SEND_FUNCTION = "send_mime"


# ===========================================================================================
# 1. WIRING GATE
# ===========================================================================================

# Genuine entry points and libraries-by-design. Every entry needs a written reason.
# Adding a line here is a decision, not a workaround: you are asserting that this module
# has a real invoker that static analysis cannot see (a human, a skill, an operator runbook).
WIRING_ALLOWLIST = {
    # --- the test / gate machinery itself -------------------------------------------------
    "run_tests":            "the CI entry point; nothing imports a test harness by design",
    "check_wiring":         "this file — the gate cannot require a gate to invoke it",
    # --- operator CLI entry points: a HUMAN or the packaged skill runs these --------------
    "onboard":              "first-run operator CLI; the skill's onboarding step runs it by hand",
    "new_case":             "operator CLI — opens a case; invoked from the skill, never on a timer",
    "close_case":           "operator CLI — closes a case; deliberately manual, a close is a judgement",
    "new_claim_draft":      "operator CLI — drafts a claim for human review before anything is sent",
    "scheduler":            "THE INSTALLER. It is the thing that schedules; it cannot be scheduled",
    "refresh_sessions":     "operator credential-refresh CLI; run by a human when a token dies",
    "stride_trace_nudge":   "operator-run one-off; Stride trace window 2026-07-31 to 08-15; delete "
                            "after the window closes (M39 teardown). Case-specific scaffolding "
                            "with no importer by design. NOTE: this allowlists its WIRING only — "
                            "its send-path violation was FIXED (it routes through mer_send now), "
                            "never allowlisted.",
    "fresh_profile_check":  "operator/CI check that the engine runs as someone who is NOT King",
    # --- read-only reports the operator or skill runs on demand ---------------------------
    "mer_dashboard":        "read-only report; /merchandise-return-dashboard skill runs it on demand",
    "multica_dashboard":    "read-only whole-business report; run on demand by the /dashboard skill",
    "send_mer_dashboard":   "thin operator wrapper that pushes mer_dashboard output to Telegram",
    "send_dashboard":       "operator entry point — the /dashboard quick_command runs it directly. "
                            "It is non-orphan in the VPS tree only incidentally, because "
                            "send_mer_dashboard.py wraps it; in the shipped skill package that "
                            "wrapper is absent and the real reason has to be stated.",
    "multica_board_state":  "read-only board snapshot; invoked by king_nag by absolute VPS path",
    # --- libraries by design --------------------------------------------------------------
    "image_text":           "OCR helper library, imported lazily only when an attachment is an image",
}

# Files that must not count as "an importer/invoker" when deciding whether something is
# wired. A test harness runs every module by construction, so if it counted, every module in
# the repo would be trivially wired and this gate would be decorative.
NON_WIRING_CALLERS = {"run_tests", "check_wiring"}

# Test files prove behaviour; they are not a live call site. Discovered dynamically so a new
# test_*.py never needs an allowlist edit — and never launders an orphan into "wired".
def _is_test_module(name):
    return name.startswith("test_")


SUBPROCESS_FUNCS = {
    "run", "call", "check_call", "check_output", "Popen",          # subprocess.*
    "system", "execv", "execvp", "execve", "spawnv", "spawnl",      # os.*
}


class ModuleFacts(object):
    """What static analysis can prove about one .py file."""

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.imports = set()        # local module names imported (top-level or in-function)
        self.subprocess_refs = set()  # local module names invoked as `python x.py`
        self.send_calls = []        # (lineno, rendered call) for send_mime(...)
        self.parse_error = None


def _module_level_str_consts(tree):
    """MODULE-LEVEL `NAME = "..."` bindings, so `subprocess.run([PY, WATCHER])` resolves.

    king_nag.py does exactly this: WATCHER = "/opt/data/scripts/inbox_watcher.py" at module
    level, then hands WATCHER to subprocess. Without this, inbox_watcher looks like an orphan
    and would get allowlisted with a reason that is not true.
    """
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _strings_in(node, consts):
    """Every string reachable in this subtree, resolving module-level constant names."""
    found = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            found.append(n.value)
        elif isinstance(n, ast.Name) and n.id in consts:
            found.append(consts[n.id])
    return found


def _callee_name(func):
    """'subprocess.run' -> 'run'; 'os.system' -> 'system'; bare 'system' -> 'system'."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def analyse_module(name, path, local_modules):
    facts = ModuleFacts(name, path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        facts.parse_error = "%s line %s: %s" % (type(exc).__name__, exc.lineno, exc.msg)
        return facts
    except OSError as exc:
        facts.parse_error = str(exc)
        return facts

    consts = _module_level_str_consts(tree)

    for node in ast.walk(tree):
        # -- imports, including the ones hidden inside a function body ---------------------
        if isinstance(node, ast.Import):
            for alias in node.names:
                facts.imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; this directory is flat, so treat as local
            if node.module:
                facts.imports.add(node.module.split(".")[0])
        # -- subprocess invocation of a sibling script ------------------------------------
        elif isinstance(node, ast.Call):
            callee = _callee_name(node.func)
            if callee in SUBPROCESS_FUNCS:
                for s in _strings_in(node, consts):
                    for chunk in re.findall(r"([A-Za-z0-9_]+)\.py", s):
                        if chunk in local_modules:
                            facts.subprocess_refs.add(chunk)
            # -- a call to send_mime, however it is spelled -------------------------------
            if callee == SEND_FUNCTION:
                facts.send_calls.append((node.lineno, _render_call(node)))

    facts.imports &= local_modules
    facts.imports.discard(name)
    facts.subprocess_refs.discard(name)
    return facts


def _render_call(node):
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return "%s.%s(...)" % (node.func.value.id, node.func.attr)
    if isinstance(node.func, ast.Attribute):
        return "<expr>.%s(...)" % node.func.attr
    if isinstance(node.func, ast.Name):
        return "%s(...)" % node.func.id
    return "%s(...)" % SEND_FUNCTION


def local_module_names(scripts_dir):
    return {f[:-3] for f in os.listdir(scripts_dir)
            if f.endswith(".py") and not f.startswith("_")}


def scheduled_modules(scripts_dir):
    """Modules the CLOCK reaches: schedule.json job steps + *_cron.sh wrappers.

    Returns {module_name: "how"} so the failure report can say what evidence was looked for.

    ONE manifest name, matching scheduler.MANIFEST_SEARCH_NAMES. This used to search
    ("schedule.json", "schedule.json.example") and break on the first hit — the same
    shadowing mechanism the scheduler had, and it MUST stay identical to it. If this gate
    reads a different manifest than the clock executes, the gate is asserting things about
    a file nothing runs, which is worse than having no gate at all.
    """
    found = {}
    manifest = "schedule.json"
    p = os.path.join(scripts_dir, manifest)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            data = None
        if data:
            for job in data.get("jobs", []):
                for step in job.get("steps", []):
                    mod = str(step.get("module", ""))
                    if mod.endswith(".py"):
                        found.setdefault(mod[:-3], "%s job %r" % (manifest, job.get("name")))

    for fname in sorted(os.listdir(scripts_dir)):
        if not fname.endswith("_cron.sh"):
            continue
        try:
            with open(os.path.join(scripts_dir, fname), "r",
                      encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for mod in re.findall(r"([A-Za-z0-9_]+)\.py", text):
            found.setdefault(mod, fname)
    return found


def build_facts(scripts_dir):
    mods = local_module_names(scripts_dir)
    return {m: analyse_module(m, os.path.join(scripts_dir, m + ".py"), mods) for m in sorted(mods)}


def gate_wiring(scripts_dir, out=sys.stdout):
    facts = build_facts(scripts_dir)
    sched = scheduled_modules(scripts_dir)

    imported_by = {m: set() for m in facts}
    invoked_by = {m: set() for m in facts}
    for name, f in facts.items():
        if name in NON_WIRING_CALLERS or _is_test_module(name):
            continue        # a harness/test is not a live call site
        for target in f.imports:
            imported_by.setdefault(target, set()).add(name)
        for target in f.subprocess_refs:
            invoked_by.setdefault(target, set()).add(name)

    orphans, allowed, broken = [], [], []
    for name in sorted(facts):
        if facts[name].parse_error:
            broken.append((name, facts[name].parse_error))
            continue
        imps, invs, sch = imported_by[name], invoked_by[name], sched.get(name)
        if imps or invs or sch:
            continue
        if name in WIRING_ALLOWLIST:
            allowed.append(name)
        elif _is_test_module(name):
            allowed.append(name)
        else:
            orphans.append(name)

    out.write("WIRING GATE — every module must have a live call site\n")
    out.write("  scanned   : %d module(s) in %s\n" % (len(facts), scripts_dir))
    out.write("  scheduled : %d module(s) reachable from the clock\n" % len(sched))
    out.write("  allowlist : %d entry point(s)/library(ies), %d test file(s)\n"
              % (len(WIRING_ALLOWLIST), sum(1 for m in facts if _is_test_module(m))))

    for name, err in broken:
        out.write("\n  [BROKEN] %s.py does not parse — %s\n" % (name, err))

    if orphans:
        out.write("\n  %d ORPHAN(S) — built, self-tested, wired into nothing:\n" % len(orphans))
        for name in orphans:
            out.write("\n    %s.py\n" % name)
            out.write("      FAILED condition 1 (imported): no module in this directory "
                      "imports `%s`\n" % name)
            out.write("      FAILED condition 2 (scheduled): no schedule.json job step and no "
                      "*_cron.sh wrapper names %s.py\n" % name)
            out.write("      FAILED condition 3 (invoked)  : no module runs %s.py as a "
                      "subprocess\n" % name)
            out.write("      -> wire it into a real call path, OR add it to WIRING_ALLOWLIST "
                      "in check_wiring.py WITH A WRITTEN REASON.\n")
    else:
        out.write("\n  no orphans — every module is imported, scheduled, or invoked\n")

    stale = sorted(n for n in WIRING_ALLOWLIST if n not in facts)
    if stale:
        out.write("\n  note: allowlist names %d module(s) that no longer exist: %s\n"
                  % (len(stale), ", ".join(stale)))

    ok = not orphans and not broken
    out.write("\n  RESULT: %s\n" % ("PASS" if ok else "FAIL"))
    return ok


# ===========================================================================================
# 2. SEND-PATH IMPORT GATE
# ===========================================================================================

# Who may `import gmail_transport`. Importing the transport is NOT the same as being allowed
# to send — most of these need it only for its self-refreshing Google access token, because
# that is where the OAuth refresh lives. They are listed so the set is visible and finite.
TRANSPORT_IMPORT_ALLOWLIST = {
    "mer_send":              "THE public send API; the one module whose job is to send",
    "gmail_connect":         "OAuth setup CLI — verifies the credential the transport will use",
    "gmail_fetch":           "read-only fetch; borrows the transport's identity resolver only",
    "inbox_watcher":         "read-only watcher; borrows the self-refreshing access_token only",
    "multica_calendar_sync": "needs a live Google access token; never composes a message",
    "multica_dashboard":     "needs a live Google access token for a read-only report",
    "unmatched_review":      "read-only review sweep; access_token only",
    "mer_engine":            "the orchestrator; imports the gated path and calls it via mer_send",
    "mer_hotpath":           "the minute-resolution dispatcher on the same gated path",
    "fresh_profile_check":   "asserts identity resolution works for a NON-King profile",
    "nudge":                 "lazy in-function import behind the send_queue veto window",
    "send_queue":            "the veto queue — the only caller allowed to fire a held letter",
    "test_send_path":        "the dedicated test for the send guard; must import what it tests",
}

# Who may CALL send_mime(). This is deliberately far smaller than the import allowlist.
# As of M45 gmail_transport.send_mime refuses to open a socket without a single-use token
# minted by idempotency.reserve_send(), so a stray call now raises instead of mailing a
# vendor twice. This gate enforces the same rule STATICALLY, so the violation is visible at
# commit time rather than at 3am when the guard fires in production.
SEND_CALL_ALLOWLIST = {
    "gmail_transport": "it DEFINES send_mime; the definition is not a call site",
    "mer_send":        "the ONE call site — it holds the reservation token that unlocks it",
    "test_send_path":  "proves send_mime refuses a missing/forged token; must attempt the call",
}


def gate_send_path(scripts_dir, out=sys.stdout):
    facts = build_facts(scripts_dir)
    import_violations, call_violations = [], []

    for name in sorted(facts):
        f = facts[name]
        if f.parse_error:
            continue
        if TRANSPORT_MODULE in f.imports and name != TRANSPORT_MODULE \
                and name not in TRANSPORT_IMPORT_ALLOWLIST:
            import_violations.append(name)
        if f.send_calls and name not in SEND_CALL_ALLOWLIST:
            for lineno, rendered in f.send_calls:
                call_violations.append((name, lineno, rendered))

    out.write("SEND-PATH GATE — one repo-wide rule, not a denylist per module\n")
    out.write("  canonical send modules : %s\n" % ", ".join(sorted(SEND_MODULES)))
    out.write("  may import %-12s: %d module(s)\n"
              % (TRANSPORT_MODULE, len(TRANSPORT_IMPORT_ALLOWLIST)))
    out.write("  may call   %-12s: %s\n"
              % (SEND_FUNCTION + "()", ", ".join(sorted(SEND_CALL_ALLOWLIST))))

    if import_violations:
        out.write("\n  %d MODULE(S) IMPORT %s WITHOUT PERMISSION:\n"
                  % (len(import_violations), TRANSPORT_MODULE))
        for name in import_violations:
            out.write("    %s.py imports %s and is not in TRANSPORT_IMPORT_ALLOWLIST\n"
                      % (name, TRANSPORT_MODULE))

    if call_violations:
        out.write("\n  %d DIRECT CALL(S) TO %s() OUTSIDE THE ALLOWLIST:\n"
                  % (len(call_violations), SEND_FUNCTION))
        for name, lineno, rendered in call_violations:
            out.write("    %s.py:%d  %s\n" % (name, lineno, rendered))
            out.write("      -> call mer_send.send(...) instead. It reserves an idempotency\n"
                      "         token, honours the (case, recipient) cooldown, and is the only\n"
                      "         path that can obtain the token send_mime now demands.\n")

    if not import_violations and not call_violations:
        out.write("\n  no unauthorised imports of %s and no direct %s() calls\n"
                  % (TRANSPORT_MODULE, SEND_FUNCTION))

    ok = not import_violations and not call_violations
    out.write("\n  RESULT: %s\n" % ("PASS" if ok else "FAIL"))
    return ok


# ===========================================================================================
# 3. SHELL-INTERPOLATED BOARD WRITE GATE
# ===========================================================================================
#
# 2026-07-28: "$2,500" was written to the board as ",500". A multi-line value containing a
# `$` went unquoted through a shell to the `multica` CLI; the shell read `$2` as a positional
# parameter, found it empty, and deleted it. The mangled figure entered the client record and
# had to be corrected by hand. No exception was raised anywhere — a shell does not consider
# eating your money a failure.
#
# The rule: a board WRITE never crosses a shell. multica_api.py takes structured JSON, where
# "$2,500" is a string and stays a string.

#: Sub-commands that MUTATE the board. Reads are allowed through the CLI — a mangled read
#: produces a wrong answer on a screen, not a wrong number in a client record.
BOARD_WRITE_VERBS = (
    "issue create", "issue update", "issue comment", "issue assign", "issue delete",
    "issue move", "issue label", "project create", "project update", "workspace create",
    "agent create", "agent update", "autopilot create", "autopilot update",
)

BOARD_WRITE_ALLOWLIST = {
    "check_wiring": "this gate quotes the forbidden patterns in order to detect them",
}

_MULTICA_TOKEN = re.compile(r"\bmultica(?:\.exe)?\s+([a-z]+(?:\s+[a-z]+)?)")


def _is_write_invocation(text):
    for m in _MULTICA_TOKEN.finditer(text):
        verb = " ".join(m.group(1).split())
        for w in BOARD_WRITE_VERBS:
            if verb == w or verb == w.split()[0] + " " + w.split()[-1]:
                return w
        # `multica issue create` also matches on the first token pair; catch `multica create`
        if verb.split()[0] in ("issue", "project", "workspace", "agent", "autopilot") \
                and len(verb.split()) > 1 and verb.split()[1] in (
                    "create", "update", "comment", "assign", "delete", "move", "label"):
            return verb
    return None


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}


def _iter_files(root, suffix, recursive):
    """(display_name, full_path) for every file with `suffix`. Sorted, deterministic."""
    if not recursive:
        for f in sorted(os.listdir(root)):
            if f.endswith(suffix) and os.path.isfile(os.path.join(root, f)):
                yield f, os.path.join(root, f)
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for f in sorted(filenames):
            if f.endswith(suffix):
                full = os.path.join(dirpath, f)
                yield os.path.relpath(full, root).replace("\\", "/"), full


def gate_board_writes(scripts_dir, out=sys.stdout, recursive=False):
    violations = []

    # -- (a) any board WRITE invoked from a shell script ----------------------------------
    for fname, path in _iter_files(scripts_dir, ".sh", recursive):
        if os.path.basename(fname)[:-3] in BOARD_WRITE_ALLOWLIST:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            verb = _is_write_invocation(stripped)
            if verb:
                violations.append((fname, i, "shell script invokes `multica %s`" % verb,
                                   stripped[:90]))

    # -- (b) a board WRITE built by INTERPOLATING a value into a Python string ------------
    for fname, path in _iter_files(scripts_dir, ".py", recursive):
        if os.path.basename(fname)[:-3] in BOARD_WRITE_ALLOWLIST:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            hit = None
            if isinstance(node, ast.JoinedStr):
                # f-string: literal chunks + at least one interpolated value
                literal = "".join(v.value for v in node.values
                                  if isinstance(v, ast.Constant) and isinstance(v.value, str))
                interpolated = any(isinstance(v, ast.FormattedValue) for v in node.values)
                verb = _is_write_invocation(literal)
                if verb and interpolated:
                    hit = "f-string builds `multica %s` with an interpolated value" % verb
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
                    and isinstance(node.left, ast.Constant) \
                    and isinstance(node.left.value, str):
                verb = _is_write_invocation(node.left.value)
                if verb:
                    hit = "%%-format builds `multica %s` with an interpolated value" % verb
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "format" \
                    and isinstance(node.func.value, ast.Constant) \
                    and isinstance(node.func.value.value, str):
                verb = _is_write_invocation(node.func.value.value)
                if verb:
                    hit = "str.format builds `multica %s` with an interpolated value" % verb
            if hit:
                violations.append((fname, node.lineno, hit, ""))

    out.write("BOARD-WRITE GATE — no board write may cross a shell\n")
    out.write("  rule: writes go through multica_api.py with structured JSON.\n")
    out.write("        On 2026-07-28 \"$2,500\" reached the board as \",500\" because a value\n")
    out.write("        containing a $ was interpolated into a shell command line.\n")

    if violations:
        out.write("\n  %d VIOLATION(S):\n" % len(violations))
        for fname, lineno, why, snippet in violations:
            out.write("    %s:%d  %s\n" % (fname, lineno, why))
            if snippet:
                out.write("      %s\n" % snippet)
            out.write("      -> use multica_api.py (structured JSON) — never a shell command line.\n")
    else:
        out.write("\n  no shell-interpolated board writes found\n")

    ok = not violations
    out.write("\n  RESULT: %s\n" % ("PASS" if ok else "FAIL"))
    return ok


# ===========================================================================================
# SELF-TEST — the gates are tested by making them SEE a synthetic violation.
# ===========================================================================================

def _selftest():
    import io
    import shutil
    import tempfile

    checks = []

    def check(label, ok, detail=""):
        checks.append((label, bool(ok)))
        print("%-5s %s%s" % ("PASS" if ok else "FAIL", label,
                             ("  [%s]" % detail) if detail and not ok else ""))

    print("check_wiring --selftest  (pure static analysis; no network, no board, no mail)")
    print("-" * 78)

    # -- every allowlist entry carries a real reason ---------------------------------------
    for label, table in (("WIRING_ALLOWLIST", WIRING_ALLOWLIST),
                         ("TRANSPORT_IMPORT_ALLOWLIST", TRANSPORT_IMPORT_ALLOWLIST),
                         ("SEND_CALL_ALLOWLIST", SEND_CALL_ALLOWLIST),
                         ("BOARD_WRITE_ALLOWLIST", BOARD_WRITE_ALLOWLIST)):
        bad = [k for k, v in table.items() if not (isinstance(v, str) and len(v.strip()) >= 12)]
        check("%s: every entry has a written reason" % label, not bad, ",".join(bad))

    check("SEND_MODULES names the transport (remedy_map's private copy did not)",
          TRANSPORT_MODULE in SEND_MODULES)

    tmp = tempfile.mkdtemp(prefix="check_wiring_selftest_")
    try:
        # ---------------------------------------------------------------- wiring gate
        sandbox = os.path.join(tmp, "wiring")
        os.makedirs(sandbox)
        with open(os.path.join(sandbox, "leaf.py"), "w", encoding="utf-8") as fh:
            fh.write("VALUE = 1\n")
        with open(os.path.join(sandbox, "user.py"), "w", encoding="utf-8") as fh:
            fh.write("import leaf\nprint(leaf.VALUE)\n")
        with open(os.path.join(sandbox, "schedule.json"), "w", encoding="utf-8") as fh:
            json.dump({"jobs": [{"name": "j", "steps": [{"module": "user.py"}]}]}, fh)

        buf = io.StringIO()
        check("wiring gate PASSES when every module is imported or scheduled",
              gate_wiring(sandbox, buf) is True, buf.getvalue()[-200:])

        # introduce the exact 2026-07-26 defect: a self-tested module nothing imports
        with open(os.path.join(sandbox, "safety_guard.py"), "w", encoding="utf-8") as fh:
            fh.write('"""A safety module with a passing self-test and no call site."""\n'
                     'def enforce():\n    return True\n'
                     'if __name__ == "__main__":\n    print("PASS")\n')
        buf = io.StringIO()
        fired = gate_wiring(sandbox, buf) is False
        text = buf.getvalue()
        check("wiring gate FAILS on an orphan safety module", fired)
        check("wiring gate NAMES the orphan", "safety_guard.py" in text)
        check("wiring gate says which conditions failed",
              "FAILED condition 1" in text and "FAILED condition 2" in text)

        # a string mention is NOT an import — grep would be fooled here, AST is not
        with open(os.path.join(sandbox, "user.py"), "a", encoding="utf-8") as fh:
            fh.write('\nNOTE = "we should really use safety_guard one day"\n')
        buf = io.StringIO()
        check("a string mention does not launder an orphan into 'wired'",
              gate_wiring(sandbox, buf) is False)

        # a real import clears it
        with open(os.path.join(sandbox, "user.py"), "a", encoding="utf-8") as fh:
            fh.write("import safety_guard\nsafety_guard.enforce()\n")
        buf = io.StringIO()
        check("wiring gate PASSES once the orphan gets a real import",
              gate_wiring(sandbox, buf) is True, buf.getvalue()[-300:])

        # an in-function import counts (nudge.py imports gmail_transport this way)
        os.remove(os.path.join(sandbox, "user.py"))
        with open(os.path.join(sandbox, "user.py"), "w", encoding="utf-8") as fh:
            fh.write("def go():\n    import safety_guard\n    return safety_guard.enforce()\n"
                     "import leaf\nprint(leaf.VALUE)\n")
        buf = io.StringIO()
        check("an import inside a function body counts as a live call site",
              gate_wiring(sandbox, buf) is True)

        # a subprocess invocation via a module-level constant counts (king_nag does this)
        with open(os.path.join(sandbox, "helper.py"), "w", encoding="utf-8") as fh:
            fh.write("print('hi')\n")
        with open(os.path.join(sandbox, "user.py"), "a", encoding="utf-8") as fh:
            fh.write('\nimport subprocess\nH = "/opt/data/scripts/helper.py"\n'
                     'subprocess.run(["python3", H])\n')
        buf = io.StringIO()
        check("subprocess invocation through a module-level constant counts as wiring",
              gate_wiring(sandbox, buf) is True, buf.getvalue()[-300:])

        # a test file must NOT be able to wire an orphan
        with open(os.path.join(sandbox, "orphan2.py"), "w", encoding="utf-8") as fh:
            fh.write("def f():\n    return 1\n")
        with open(os.path.join(sandbox, "test_orphan2.py"), "w", encoding="utf-8") as fh:
            fh.write("import orphan2\nprint(orphan2.f())\n")
        buf = io.StringIO()
        check("a test file cannot launder an orphan into 'wired'",
              gate_wiring(sandbox, buf) is False and "orphan2.py" in buf.getvalue())

        # ------------------------------------------------------------- send-path gate
        sp = os.path.join(tmp, "sendpath")
        os.makedirs(sp)
        with open(os.path.join(sp, "gmail_transport.py"), "w", encoding="utf-8") as fh:
            fh.write("def send_mime(msg, to_addrs=None, token=None):\n    return 'id'\n")
        with open(os.path.join(sp, "mer_send.py"), "w", encoding="utf-8") as fh:
            fh.write("import gmail_transport\n"
                     "def send(to, body):\n"
                     "    return gmail_transport.send_mime(None, to_addrs=[to], token='t')\n")
        buf = io.StringIO()
        check("send-path gate PASSES on a clean send path", gate_send_path(sp, buf) is True,
              buf.getvalue()[-300:])

        # the exact stride_trace_nudge.py shape
        with open(os.path.join(sp, "rogue_nudge.py"), "w", encoding="utf-8") as fh:
            fh.write("import gmail_transport as gt\n"
                     "def go(mime):\n    gt.send_mime(mime, to_addrs=['vendor@x.com'])\n")
        buf = io.StringIO()
        fired = gate_send_path(sp, buf) is False
        text = buf.getvalue()
        check("send-path gate FAILS on a direct send_mime() call", fired)
        check("send-path gate names the file and line",
              "rogue_nudge.py:3" in text, text[-400:])
        check("send-path gate also flags the unauthorised import",
              "rogue_nudge.py imports gmail_transport" in text)

        # a docstring that merely mentions send_mime( is not a call — AST, not grep
        with open(os.path.join(sp, "rogue_nudge.py"), "w", encoding="utf-8") as fh:
            fh.write('"""Never call gmail_transport.send_mime( directly."""\n'
                     'import mer_send\ndef go():\n    return mer_send.send("a", "b")\n')
        buf = io.StringIO()
        check("a docstring mentioning send_mime( is not treated as a call",
              gate_send_path(sp, buf) is True, buf.getvalue()[-300:])

        # ------------------------------------------------------------ board-write gate
        bw = os.path.join(tmp, "board")
        os.makedirs(bw)
        with open(os.path.join(bw, "ok.py"), "w", encoding="utf-8") as fh:
            fh.write("import json\n"
                     "def note(case, amount):\n"
                     "    return json.dumps({'case': case, 'amount': amount})\n")
        buf = io.StringIO()
        check("board gate PASSES on structured JSON", gate_board_writes(bw, buf) is True,
              buf.getvalue()[-300:])

        # the exact 2026-07-28 defect, in Python
        with open(os.path.join(bw, "bad.py"), "w", encoding="utf-8") as fh:
            fh.write("import os\n"
                     "def note(issue, amount):\n"
                     "    os.system(f'multica issue comment add {issue} "
                     "--content {amount}')\n")
        buf = io.StringIO()
        fired = gate_board_writes(bw, buf) is False
        check("board gate FAILS on an f-string board write", fired, buf.getvalue()[-400:])
        check("board gate names the offending file", "bad.py" in buf.getvalue())

        # and in a shell script
        os.remove(os.path.join(bw, "bad.py"))
        with open(os.path.join(bw, "bad.sh"), "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nAMOUNT="$2,500"\n'
                     'multica issue comment add MER-1 --content $AMOUNT\n')
        buf = io.StringIO()
        check("board gate FAILS on `multica` inside a .sh file",
              gate_board_writes(bw, buf) is False, buf.getvalue()[-400:])

        # a READ through the CLI in a shell script is allowed
        os.remove(os.path.join(bw, "bad.sh"))
        with open(os.path.join(bw, "read.sh"), "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nmultica issue list --output json\n')
        buf = io.StringIO()
        check("board gate ALLOWS a read (`multica issue list`) from a shell",
              gate_board_writes(bw, buf) is True, buf.getvalue()[-300:])

        # a comment naming the forbidden command is not a violation
        with open(os.path.join(bw, "read.sh"), "a", encoding="utf-8") as fh:
            fh.write('# never do: multica issue create --title "$X"\n')
        buf = io.StringIO()
        check("a commented-out board write is not a violation",
              gate_board_writes(bw, buf) is True, buf.getvalue()[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [c for c, ok in checks if not ok]
    print("-" * 78)
    if failed:
        print("SELF-TEST FAILED — %d of %d checks failed" % (len(failed), len(checks)))
        return 1
    print("PASS — check_wiring self-test: %d/%d checks passed" % (len(checks), len(checks)))
    return 0


# ===========================================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Structural gates: wiring, send path, and shell-interpolated board writes.")
    ap.add_argument("--wiring", action="store_true", help="orphan-module gate only")
    ap.add_argument("--send-path", action="store_true", help="send-path import/call gate only")
    ap.add_argument("--board-write", action="store_true", help="board-write gate only")
    ap.add_argument("--selftest", action="store_true", help="test the gates themselves")
    ap.add_argument("--list-allowlist", action="store_true",
                    help="print every allowlist entry with its written reason")
    ap.add_argument("--dir", default=SCRIPTS_DIR, help="directory to scan (default: this one)")
    ap.add_argument("--board-write-root", default=None,
                    help="scan this tree RECURSIVELY for shell-interpolated board writes "
                         "(the $2,500 bug can be committed anywhere in the repo, not just in "
                         "the engine directory). The pre-commit hook passes the repo root.")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.list_allowlist:
        for label, table in (("WIRING_ALLOWLIST", WIRING_ALLOWLIST),
                             ("TRANSPORT_IMPORT_ALLOWLIST", TRANSPORT_IMPORT_ALLOWLIST),
                             ("SEND_CALL_ALLOWLIST", SEND_CALL_ALLOWLIST),
                             ("BOARD_WRITE_ALLOWLIST", BOARD_WRITE_ALLOWLIST)):
            print("\n%s (%d)" % (label, len(table)))
            for k in sorted(table):
                print("  %-22s %s" % (k, table[k]))
        return 0

    selected = [args.wiring, args.send_path, args.board_write]
    run_all = not any(selected)

    results = []
    print("=" * 78)
    print("STRUCTURAL GATES :: %s" % args.dir)
    print("=" * 78)
    if run_all or args.wiring:
        results.append(("wiring", gate_wiring(args.dir)))
        print()
    if run_all or args.send_path:
        results.append(("send-path", gate_send_path(args.dir)))
        print()
    if run_all or args.board_write:
        root = args.board_write_root or args.dir
        results.append(("board-write",
                        gate_board_writes(root, recursive=bool(args.board_write_root))))
        print()

    bad = [n for n, ok in results if not ok]
    print("=" * 78)
    if bad:
        print("GATES FAILED: %s" % ", ".join(bad))
        print("A module that passes its own self-test and is wired into nothing is NOT done.")
        return 1
    print("PASS — all %d structural gate(s) green" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
