#!/usr/bin/env python3
"""
sync_deadlines.py — deadline single-source-of-truth (Blueprint M26).

The engine tracks each case's real deadline in the custom property "MR Phase Deadline"
(property id de9875d1-4d31-4a19-af62-4ae4372c1c11). Multica also has a native `due_date`
field, which is what the calendar sync (M25) and every generic Multica view read.

This script keeps them in lock-step: for every OPEN case in the MER workspace it reads
MR Phase Deadline and PATCHes the native due_date to match, so `due_date == MR Phase Deadline`
always. That makes MR Phase Deadline the single source of truth and due_date its mirror.

Idempotent:
  * skips cases whose due_date already equals the deadline,
  * skips cases with no MR Phase Deadline set (never clears a due_date it didn't set here —
    an undated case is simply left alone),
  * skips closed/cancelled/done cases.

Default is a DRY RUN (prints the plan, writes nothing). Pass --live to actually PATCH.

Runs laptop-independent on the 24/7 VPS via multica_api.py (api.multica.ai + MULTICA_TOKEN
from /opt/data/.env). No pip install — stdlib only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import multica_api as mc  # noqa: E402

DEADLINE_PROP = "MR Phase Deadline"
CLOSED = {"done", "cancelled", "closed"}


def norm_date(v):
    """Normalize a Multica date value to 'YYYY-MM-DD' (or None)."""
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else s


def update_due_date(issue_id, due, ws=None):
    """PUT /api/issues/<id>?workspace_id=... {"due_date": "YYYY-MM-DD"}

    Verified 2026-07-25: the Multica issue endpoint accepts PUT (200) and treats a
    partial body as a MERGE — sending only due_date leaves title, status, MR Phase,
    MR Phase Deadline and all other properties untouched. PATCH/POST return 405.
    """
    ws = ws or mc.WORKSPACE
    return mc._req("PUT", "/issues/%s" % issue_id,
                   {"workspace_id": ws}, {"due_date": due})


def main():
    live = "--live" in sys.argv
    print("=== sync_deadlines (M26: MR Phase Deadline -> native due_date) %s ===" % (
        "LIVE" if live else "DRY-RUN"))

    issues = mc.list_issues()
    changed = same = undated = closed = 0
    for it in issues:
        status = (it.get("status") or "").lower()
        if status in CLOSED:
            closed += 1
            continue
        mr = it.get("mr", {})
        deadline = norm_date(mr.get(DEADLINE_PROP))
        ident = it.get("identifier", it.get("id", "?"))
        if not deadline:
            undated += 1
            continue
        cur = norm_date(it.get("due_date"))
        if cur == deadline:
            same += 1
            continue
        print("  %-8s due_date %s -> %s  (%s)" % (
            ident, cur or "(none)", deadline, (it.get("title") or "")[:48]))
        if live:
            try:
                update_due_date(it["id"], deadline)
                print("           PATCHED ok")
            except Exception as e:
                print("           PATCH FAILED: %s" % e)
                continue
        changed += 1

    print("\nsummary%s: %d %s, %d already-equal, %d undated(skipped), %d closed(skipped)" % (
        "" if live else " [DRY]",
        changed, "PATCHED" if live else "would change",
        same, undated, closed))


if __name__ == "__main__":
    main()
