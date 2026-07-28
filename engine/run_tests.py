#!/usr/bin/env python3
"""
run_tests.py — M31/M38 regression test harness for the AMBS Merchandise Returns Engine.

Runs each engine module's built-in __main__ self-test as an isolated subprocess,
captures the exit code and the last line of output, and prints a pass/fail matrix
plus a single SUMMARY line. Exits nonzero if any module hard-fails, so it can gate
a deploy or run under cron.

Rules:
  * exit 0 AND no failure marker in the output  -> PASS
  * a traceback, a failure marker, OR A NONZERO EXIT -> FAIL, unconditionally.

    2026-07-28 — THE SCORING BUG THIS RULE REPLACES. classify_result() used to honour an
    explicit "PASS" in the output even on a NONZERO exit ("Nonzero exit, but no traceback.
    Honor an explicit PASS with no fail marker."). Any module that printed a summary
    containing the word PASS and then died in cleanup, in an atexit handler, on a
    SystemExit(1), or on a Windows non-UTF8 console, scored GREEN. The harness's whole job
    is to be the thing that cannot be fooled; a scorer with a "but it said PASS" override
    is a scorer that reports what the module claims instead of what the module did.
    A process that exits nonzero FAILED. There is no exception.
  * classify_llm.py is LLM-backed: a 429 / rate-limit that still falls back and
    completes is NOT a failure — it is reported as DEGRADED (a graceful skip), so a
    real regression can never hide behind "the LLM was down".
  * a module that is not deployed on this host is SKIP, never FAIL.

M38 — the harness now also covers the ORCHESTRATOR and SEND PATH, not just the 7 leaf
modules. `send_queue`, `draft_reply` and `nudge` are the code that actually puts email
in front of a real vendor; before M38 none of them had a self-test. `case_tick` and
`mer_config` already had one but were never wired in.

IMPORTANT: some modules DO REAL WORK when run bare (case_tick.py runs the live tick;
send_queue.py --list reads the live queue; nudge.py walks the real board). Those are
invoked with an explicit `--selftest` argument via the "args" field below. Never add a
module here without checking what bare execution does.

Stdlib only. No third-party imports. The self-tests themselves are offline and force
MER_ENGINE_SEND=off with tempfile queues/ledgers, so this harness cannot send mail.
"""

import os
import subprocess
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Prefer the engine venv; fall back to whatever interpreter launched us.
_VENV_PY = "/opt/hermes/.venv/bin/python3"
PYTHON = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable

PER_TEST_TIMEOUT = 120  # seconds

# Known engine self-tests, in run order. `llm_tolerant` marks modules whose
# self-test may legitimately hit an LLM 429 and fall back to a heuristic; for
# those a graceful fallback is a PASS and only a crash is a FAIL.
# businessday.py / remedy_gate.py are expected from parallel work — auto-detected
# below if/when they land, so no edit is needed to pick them up.
KNOWN = [
    # --- leaf modules (M31) -------------------------------------------------
    {"file": "idempotency.py",      "llm_tolerant": False},
    {"file": "pdf_text.py",         "llm_tolerant": False},
    {"file": "reply_classify.py",   "llm_tolerant": False},
    {"file": "resolution_check.py", "llm_tolerant": False},
    {"file": "businessday.py",      "llm_tolerant": False},   # may appear later
    {"file": "remedy_gate.py",      "llm_tolerant": False},   # may appear later
    # M44 — both were self-tested from day one and neither was ever in this list, so a
    # regression in the CLOSE gate or the duplicate gate would not have failed a run.
    {"file": "refund_landed.py",    "llm_tolerant": False},
    {"file": "dup_guard.py",        "llm_tolerant": False},
    {"file": "classify_llm.py",     "llm_tolerant": True},
    # --- config + orchestrator (M38: already self-tested, never wired in) ----
    {"file": "mer_config.py",       "llm_tolerant": False},
    {"file": "case_queries.py",     "llm_tolerant": False},
    {"file": "llm_providers.py",    "llm_tolerant": False},
    {"file": "onboard.py",          "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "gmail_connect.py",    "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "remedy_map.py",       "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "new_case.py",         "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "close_case.py",       "llm_tolerant": False, "args": ["--selftest"]},
    # M41 — proves the engine runs as someone who is NOT King. Guards the product promise.
    {"file": "fresh_profile_check.py", "llm_tolerant": False},
    # THE CLOCK. Its self-test installs nothing and touches no real crontab / Task Scheduler:
    # the cron backend runs against an injected in-memory table and systemd against a temp dir.
    # It also asserts that neither scheduler.py nor the job manifest carries a host path, a
    # container name or an identity — the property that makes the package shippable at all.
    {"file": "scheduler.py",        "llm_tolerant": False, "args": ["--selftest"]},
    # M10 hot path + the read-only watcher. BOTH must be invoked with --selftest: bare
    # inbox_watcher.py reads the live mailbox and writes state, and bare mer_hotpath.py
    # dispatches the real engine.
    {"file": "mer_hotpath.py",      "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "inbox_watcher.py",    "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "case_tick.py",        "llm_tolerant": False, "args": ["--selftest"]},
    # merchandise-return-dashboard: read-only report, reuses case_tick's own filter (must
    # never re-implement it -- checked structurally in the module's own self-test).
    {"file": "mer_dashboard.py",    "llm_tolerant": False, "args": ["--selftest"]},
    # --- THE SEND PATH (M38: new self-tests; all offline, none can send) -----
    {"file": "send_queue.py",       "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "draft_reply.py",      "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "nudge.py",            "llm_tolerant": False, "args": ["--selftest"]},
    # --- M47: the modules that actually SEND or HOLD STATE, finally registered ------------
    # Every one of these existed for weeks with a working self-test that this harness never
    # ran. A regression in any of them was invisible to a "GREEN" suite. That is the same
    # defect as an unwired safety module, one layer up: the harness recorded coverage it did
    # not have.
    #
    # mer_engine.py MUST have --selftest: bare execution runs the live orchestrator tick
    # under heartbeat.guard(). sol_watchdog.py runs its self-test when given NEITHER --cron
    # NOR --scan, so bare is correct and --selftest would fall through to the self-test
    # branch anyway; the empty args are deliberate and documented here so nobody "fixes" it
    # by adding --cron, which would sweep the live board.
    {"file": "mer_engine.py",       "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "sol_watchdog.py",     "llm_tolerant": False, "args": []},
    {"file": "delivery_check.py",   "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "unmatched_review.py", "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "heartbeat.py",        "llm_tolerant": False, "args": ["--selftest"]},
    # --- THE STRUCTURAL GATES (M47) ------------------------------------------------------
    # These are the reason this file is not the only definition of done. check_wiring.py
    # --selftest tests the GATES (it builds synthetic orphans and rogue send calls in a temp
    # dir and asserts the gates fire). The gates' verdict ON THIS REPO is a separate stage,
    # printed after the matrix, because an orphan is a repo defect and not a test failure.
    {"file": "check_wiring.py",     "llm_tolerant": False, "args": ["--selftest"]},
]

# Modules whose self-test lives in a DEDICATED test_*.py rather than in the module.
# Discovered from the filesystem, not hand-listed: `test_send_path.py` and
# `test_multica_api.py` both appeared on 2026-07-28 and neither would have been picked up by
# a hardcoded list — which is exactly how the previous nine modules stayed uncovered.
TEST_FILE_GLOB_PREFIX = "test_"

# Modules that SEND or HOLD STATE and still have NO self-test entry point of any kind.
# This is not an allowlist and not an excuse — it is a visible, printed coverage debt, so
# "the suite is green" can never again be mistaken for "the send path is tested". Each entry
# says who is expected to close it. Remove an entry the moment it gains a self-test.
UNCOVERED = {
    "gmail_transport.py": "no __main__ at all — running it is a silent no-op that would score "
                          "a FALSE GREEN. Covered indirectly by test_send_path.py, which "
                          "exercises the token guard. Owner: gmail_transport.",
    "mer_send.py":        "bare __main__ ATTEMPTS A SEND (harmless only because this harness "
                          "forces MER_ENGINE_SEND=off). Never register it bare. Covered "
                          "indirectly by test_send_path.py. Owner: mer_send.",
    "multica_api.py":     "bare __main__ is the live CLI and talks to the real board. Covered "
                          "by test_multica_api.py. Owner: multica_api.",
    "king_nag.py":        "bare __main__ runs the live nag (Telegram + board reads); only "
                          "--dry-run is safe and it is not a self-test. Owner: king_nag.",
    "stride_trace_nudge.py": "bare __main__ SENDS EMAIL TO A REAL VENDOR. It must never be run "
                          "by a harness. It is also the repo's one wiring/send-path gate "
                          "violation (calls gt.send_mime directly). Owner: stride_trace_nudge.",
}


def discover_test_files(scripts_dir):
    """Every test_*.py in the engine directory, in a stable order.

    Dynamic on purpose. The failure being engineered out is a hand-maintained list that
    silently omits things; replacing one hand-maintained list (KNOWN) with a second
    hand-maintained list (of test files) would have re-created it.
    """
    return [
        {"file": f, "llm_tolerant": False, "args": [], "discovered": True}
        for f in sorted(os.listdir(scripts_dir))
        if f.startswith(TEST_FILE_GLOB_PREFIX) and f.endswith(".py")
    ]

TRACEBACK_MARK = "Traceback (most recent call last)"
FAIL_MARKS = ("SELF-TEST FAILED", "FAILURES PRESENT", "[FAIL]")
PASS_MARKS = ("PASS", "ALL PASS")


def _last_line(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else "(no output)"


def _looks_like_llm_fallback(text):
    low = text.lower()
    return ("429" in text) or ("rate limit" in low) or ("fallback" in low) \
        or ("too many requests" in low)


def classify_result(exit_code, output, llm_tolerant):
    """Return (status, reason) where status is 'PASS', 'FAIL' or 'DEGRADED'.

    DEGRADED is a graceful skip, not a pass and not a failure: the module completed but
    only because it fell back off an unavailable LLM, so it proved less than a clean run.
    It never sets the exit code — only FAIL does.
    """
    has_traceback = TRACEBACK_MARK in output
    has_fail_mark = any(m in output for m in FAIL_MARKS)
    has_pass_mark = any(m in output for m in PASS_MARKS)

    # A crash is always a hard failure — including for the LLM module.
    if has_traceback:
        return "FAIL", "crash (traceback)"

    # A NONZERO EXIT IS ALWAYS A FAILURE. Never overridden by anything printed to stdout.
    # See the module docstring: the old code returned PASS here whenever the output happened
    # to contain "PASS", which scored a module that crashed after printing its summary green.
    if exit_code != 0:
        if has_fail_mark:
            return "FAIL", "nonzero exit %d + explicit failure marker" % exit_code
        if has_pass_mark:
            return "FAIL", ("nonzero exit %d — output claims PASS but the process failed "
                            "(likely a crash AFTER the summary line)" % exit_code)
        return "FAIL", "nonzero exit %d" % exit_code

    # exit 0 from here down.
    if has_fail_mark:
        return "FAIL", "exit 0 but the output carries a failure marker"
    if llm_tolerant and _looks_like_llm_fallback(output):
        return "DEGRADED", "LLM unavailable (429/fallback) — degraded, not a failure"
    if has_pass_mark:
        return "PASS", "explicit PASS"
    return "PASS", "exit 0"


def _child_env():
    """Children print UTF-8 so the parent's decode is exact, and can never send."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MER_ENGINE_SEND"] = "off"
    return env


def run_one(path, args=None):
    start = time.time()
    try:
        proc = subprocess.run(
            [PYTHON, path] + list(args or []),
            cwd=SCRIPTS_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=PER_TEST_TIMEOUT,
            env=_child_env(),
        )
        out = proc.stdout.decode("utf-8", "replace")
        return proc.returncode, out, time.time() - start, False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if e.stdout else ""
        out += "\n[harness] TIMEOUT after %ds" % PER_TEST_TIMEOUT
        return 124, out, time.time() - start, True


def main():
    # The harness itself must never be able to send, whatever a module does on import.
    os.environ["MER_ENGINE_SEND"] = "off"
    # A module's output must never be able to crash the harness on a narrow console
    # (a self-test that prints an em-dash is not a test failure).
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:
        pass

    print("=" * 72)
    print("M31/M38 — AMBS Merchandise Returns Engine :: regression test harness")
    print("  scripts dir : %s" % SCRIPTS_DIR)
    print("  interpreter : %s" % PYTHON)
    print("  send mode   : MER_ENGINE_SEND=%s (forced)" % os.environ["MER_ENGINE_SEND"])
    print("=" * 72)

    results = []   # (invocation, status, exit_code, reason, last_line, secs)
    skipped = []

    discovered = discover_test_files(SCRIPTS_DIR)
    known_files = {e["file"] for e in KNOWN}
    plan = list(KNOWN) + [e for e in discovered if e["file"] not in known_files]

    if discovered:
        print("  discovered  : %d dedicated test file(s): %s"
              % (len(discovered), ", ".join(e["file"] for e in discovered)))
        print("=" * 72)

    for entry in plan:
        fname = entry["file"]
        args = entry.get("args") or []
        label = " ".join([fname] + args)
        path = os.path.join(SCRIPTS_DIR, fname)
        if not os.path.exists(path):
            skipped.append(fname)
            continue

        exit_code, output, secs, timed_out = run_one(path, args)
        if timed_out:
            status, reason = "FAIL", "timeout after %ds" % PER_TEST_TIMEOUT
        else:
            status, reason = classify_result(exit_code, output, entry["llm_tolerant"])
        results.append((label, status, exit_code, reason, _last_line(output), secs))

    # Matrix
    print("\nPASS/FAIL MATRIX")
    print("-" * 72)
    print("%-8s  %-24s  %-4s  %6s  %s" % ("RESULT", "MODULE", "EXIT", "TIME", "REASON"))
    print("-" * 72)
    for label, status, exit_code, reason, last, secs in results:
        print("%-8s  %-24s  %-4s  %5.1fs  %s" % (status, label, exit_code, secs, reason))
        print("%-8s  %-24s  last: %s" % ("", "", last))
    print("-" * 72)

    passed = [r[0] for r in results if r[1] == "PASS"]
    failed = [r[0] for r in results if r[1] == "FAIL"]
    degraded = [r[0] for r in results if r[1] == "DEGRADED"]

    # Non-failures are stated separately from failures so nothing can hide in the middle.
    if degraded:
        print("DEGRADED (ran, but a dependency was unavailable — NOT a failure): %s"
              % ", ".join(degraded))
    if skipped:
        print("SKIPPED (module not deployed on this host — NOT a failure): %s"
              % ", ".join(skipped))

    # COVERAGE DEBT, printed every run. A green matrix above this block is not the same
    # thing as a tested engine, and the harness must be the one to say so.
    present = [f for f in UNCOVERED if os.path.exists(os.path.join(SCRIPTS_DIR, f))]
    if present:
        print("\nUNCOVERED — deployed here, sends or holds state, NO self-test entry point:")
        for f in sorted(present):
            print("  %-24s %s" % (f, UNCOVERED[f]))
        print("  (this is printed coverage debt, not a pass — do not read the SUMMARY below")
        print("   as 'the engine is tested' while these lines are here)")

    verdict = "GREEN — every deployed self-test passed" if not failed \
        else "RED — %d module(s) FAILED: %s" % (len(failed), ", ".join(failed))
    print("\nSUMMARY: %d passed, %d failed, %d degraded, %d skipped (of %d registered), "
          "%d uncovered :: %s"
          % (len(passed), len(failed), len(degraded), len(skipped), len(plan),
             len(present), verdict))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
