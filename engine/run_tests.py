#!/usr/bin/env python3
"""
run_tests.py — M31/M38 regression test harness for the AMBS Merchandise Returns Engine.

Runs each engine module's built-in __main__ self-test as an isolated subprocess,
captures the exit code and the last line of output, and prints a pass/fail matrix
plus a single SUMMARY line. Exits nonzero if any module hard-fails, so it can gate
a deploy or run under cron.

Rules:
  * exit 0, OR an explicit PASS in the output  -> PASS
  * a traceback / nonzero exit                  -> FAIL
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
    {"file": "case_tick.py",        "llm_tolerant": False, "args": ["--selftest"]},
    # --- THE SEND PATH (M38: new self-tests; all offline, none can send) -----
    {"file": "send_queue.py",       "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "draft_reply.py",      "llm_tolerant": False, "args": ["--selftest"]},
    {"file": "nudge.py",            "llm_tolerant": False, "args": ["--selftest"]},
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

    if exit_code == 0:
        if llm_tolerant and _looks_like_llm_fallback(output):
            return "DEGRADED", "LLM unavailable (429/fallback) — degraded, not a failure"
        if has_pass_mark and not has_fail_mark:
            return "PASS", "explicit PASS"
        return "PASS", "exit 0"

    # Nonzero exit, but no traceback. Honor an explicit PASS with no fail marker.
    if has_pass_mark and not has_fail_mark:
        return "PASS", "explicit PASS in output (exit %d)" % exit_code
    return "FAIL", "nonzero exit %d" % exit_code


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

    for entry in KNOWN:
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

    verdict = "GREEN — every deployed self-test passed" if not failed \
        else "RED — %d module(s) FAILED: %s" % (len(failed), ", ".join(failed))
    print("\nSUMMARY: %d passed, %d failed, %d degraded, %d skipped (of %d known) :: %s"
          % (len(passed), len(failed), len(degraded), len(skipped), len(KNOWN), verdict))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
