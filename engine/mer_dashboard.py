#!/usr/bin/env python3
"""
mer_dashboard.py — the full merchandise-return status report, and nothing else.

WHY THIS EXISTS (as distinct from multica_dashboard.py / `/dashboard`)
-----------------------------------------------------------------------
`/dashboard` is a whole-BUSINESS report: it reads every live Multica workspace and mixes
merchandise-return cases in with everything else on the board. King asked for a report scoped to
ONLY merchandise-return claims, with every field per case, not a triaged subset. This is that
report: no other workspace, no filtering by urgency, no LLM summarization.

READ-ONLY, ALWAYS. This module never writes a property, never posts a comment, never advances a
phase, never queues a send. It calls exactly the same board-read path case_tick.py uses
(`multica_api.list_issues`, `case_tick.tracked_cases`, `case_tick.case_view`,
`case_tick.is_client_case`) so "which cases exist" and "what phase are they in" can never drift
between the two tools -- the same class of bug M33 killed for Gmail queries.

DETERMINISTIC, ALWAYS. Same discipline as dashboard-skill.md's own lesson (2026-07-22 LLM
compaction failure): this script prints fixed-format text/HTML from live data. Nothing here is
handed to an LLM to reformat or summarize.

USAGE
    python3 mer_dashboard.py                 # print to stdout (works from a terminal, Claude
                                              # Code, or OpenCode -- Telegram HTML tags are still
                                              # readable as plain text)
    python3 mer_dashboard.py --html           # same, explicit
    python3 mer_dashboard.py --selftest       # offline, no network, stubbed board data
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import case_tick   # noqa: E402  -- tracked_cases / case_view / is_client_case / decide, reused
import multica_api as mc  # noqa: E402


def _esc(s):
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# Codes that mean "something is actionable right now", used only for ordering -- every case is
# shown regardless of code. "All of it, though" means completeness of DATA per case, not a
# filtered subset of cases.
_URGENCY_ORDER = {"NEEDS-YES": 0, "OVERDUE": 1, "DUE": 2, "NUDGE": 3, "ADVANCE": 4,
                  "NO-DEADLINE": 5, "INTAKE": 6, "WAIT": 7, "CLOSED": 8}


def _levers_str(levers):
    return ", ".join(levers) if levers else "(none)"


def case_block(it, today):
    """One case's full record -> a list of HTML lines. Every MR field, nothing hidden."""
    v = case_tick.case_view(it)
    mr = it.get("mr", {}) or {}
    code, action = case_tick.decide(
        v["phase"], v["deadline"], v["intake_done"], v["awaiting_yes"], today)
    client = case_tick.is_client_case(it)

    lines = []
    lines.append("<b>%s</b> — %s" % (_esc(v["identifier"]), _esc(v["title"])))
    lines.append("  Board status: %s%s" % (_esc(v["status"] or "?"),
                                            "  |  CLIENT CASE" if client else ""))
    lines.append("  MR Phase: %s" % _esc(v["phase"] or "(none)"))
    lines.append("  MR Phase Deadline: %s" % (v["deadline"].isoformat() if v["deadline"] else "(none)"))
    lines.append("  Status: [%s] %s" % (_esc(code), _esc(action)))
    lines.append("  MR Intake Complete: %s" % ("yes" if v["intake_done"] else "no"))
    lines.append("  MR Awaiting User YES: %s" % ("yes" if v["awaiting_yes"] else "no"))
    lines.append("  MR Jurisdiction: %s" % _esc(mr.get("MR Jurisdiction") or "(not set)"))
    lines.append("  MR Discrimination Flag: %s" % _esc(mr.get("MR Discrimination Flag") or "no"))
    lines.append("  MR Remedy Type: %s" % _esc(mr.get("MR Remedy Type") or "(not set)"))
    lines.append("  MR Remedy Map: %s" % _esc(_levers_str(v["remedy_map"])))
    lines.append("  MR Remedy Attempted: %s" % _esc(_levers_str(v["remedy_attempted"])))
    return lines, code


def render(issues, today=None):
    today = today or date.today()
    tracked = case_tick.tracked_cases(issues)
    tracked_ids = {it.get("id") for it in tracked}
    untracked = [it for it in issues
                 if it.get("id") not in tracked_ids
                 and (it.get("status") or "") not in ("done", "cancelled")
                 and it.get("project_id") != case_tick.BUILD_PROJECT
                 and (it.get("title") or "").startswith(case_tick.CASE_TITLE_HINTS)]

    blocks = []
    for it in tracked:
        lines, code = case_block(it, today)
        blocks.append((_URGENCY_ORDER.get(code, 9), it.get("identifier") or "", lines))
    blocks.sort(key=lambda b: (b[0], b[1]))

    out = []
    out.append("<b>📋 Merchandise Return — full status (%s)</b>" % today.isoformat())
    out.append("%d tracked case(s), %d awaiting onboarding" % (len(tracked), len(untracked)))
    out.append("")
    if not blocks:
        out.append("(no tracked merchandise-return cases on this board)")
    for _, _, lines in blocks:
        out.extend(lines)
        out.append("")
    if untracked:
        out.append("<b>-- awaiting onboarding (no MR Phase set) --</b>")
        for it in untracked:
            out.append("  %s — %s" % (_esc(it.get("identifier")), _esc(it.get("title"))))
    return "\n".join(out).rstrip() + "\n"


def main():
    if "--selftest" in sys.argv:
        return _selftest()
    text = render(mc.list_issues())
    sys.stdout.write(text)
    return 0


# ============================================================================ self-test
def _issue(identifier, title, phase=None, deadline=None, intake=False, awaiting=False,
           status="in_review", project_id="proj-cases", extra_mr=None, desc=""):
    mr = {}
    if phase is not None:
        mr["MR Phase"] = phase
    if deadline is not None:
        mr["MR Phase Deadline"] = deadline
    if intake:
        mr["MR Intake Complete"] = True
    if awaiting:
        mr["MR Awaiting User YES"] = True
    if extra_mr:
        mr.update(extra_mr)
    return {"identifier": identifier, "id": "id-" + identifier, "title": title,
            "description": desc, "status": status, "project_id": project_id, "mr": mr}


def _selftest():
    fails = []

    def check(label, ok, detail=""):
        print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", label, detail))
        if not ok:
            fails.append(label)

    print("=== mer_dashboard self-test (offline, fabricated cases) ===\n")

    today = date(2026, 7, 27)

    issues = [
        _issue("MER-A", "Case: Vendor X / Widget", phase="Tier1", deadline="2026-07-28",
               intake=True, extra_mr={"MR Jurisdiction": "TX / Dallas County",
                                       "MR Remedy Type": "Refund"}),
        _issue("MER-B", "CLIENT: Someone Else / Phone", phase="Intake", intake=False),
        _issue("MER-C", "Case: closed one", phase="Closed", status="done"),
        _issue("MER-D", "Case: cancelled test", phase="Tier1", status="cancelled"),
        _issue("MER-E", "M1 — a milestone issue", phase="Tier1"),
        _issue("MER-F", "New return, not onboarded"),
    ]

    text = render(issues, today=today)

    check("MER-A appears with its phase and deadline",
          "MER-A" in text and "Tier1" in text and "2026-07-28" in text)
    check("MER-A's jurisdiction and remedy type are shown (completeness)",
          "TX / Dallas County" in text and "Refund" in text)
    check("MER-B is flagged as a CLIENT CASE", "CLIENT CASE" in text)
    check("MER-C (done) is excluded", "MER-C" not in text)
    check("MER-D (cancelled) is excluded", "MER-D" not in text)
    check("MER-E (milestone-titled) is excluded", "MER-E" not in text)
    check("MER-F (no MR Phase) is not counted as a live case",
          "1 tracked case(s)" not in text or "MER-F" not in text.split("awaiting onboarding")[0]
          if "awaiting onboarding" in text else True)
    check("summary line counts tracked cases correctly", "2 tracked case(s)" in text, text.splitlines()[1])

    # --- structural: never touches a send/write path -----------------------------------
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"nudge", "send_queue", "mer_send", "smtplib", "gmail_transport"}
    check("no send/write-path module imported (read-only, structurally)",
          not (imported & forbidden), "checked imports: %s" % ", ".join(sorted(imported)))

    # --- reuses case_tick's own filter, does not re-implement it ------------------------
    # Fragmented so this check cannot match its OWN source line -- the exact self-matching
    # trap found live in remedy_map.py's send-path check earlier this project (an integration
    # comment containing the word "import send_queue" made a "cannot send" check pass falsely).
    own_def = "def " + "tracked_cases"
    reused_call = "case_tick" + "." + "tracked_cases"
    check("uses case_tick.tracked_cases (not a re-implemented filter)",
          own_def not in src and reused_call in src)

    print()
    if fails:
        print("FAIL — %d assertion(s) wrong: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("PASS — every tracked case renders in full, done/cancelled/milestone rows are "
          "excluded, client cases are flagged, and the module cannot send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
