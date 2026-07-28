#!/usr/bin/env python3
"""
king_nag.py - escalating reminder for the engine's OWNER, sourced from Multica (v3 2026-07-20).

NAMING (M47): this file is packaged and handed to strangers, so the operator's name has been
taken out of the prose and out of every string a user reads. The MODULE name `king_nag` and the
function name `send_telegram` are deliberately UNCHANGED: heartbeat.alert() does
`import king_nag; king_nag.send_telegram(...)` by name, and cron wrappers invoke the file by
path. Renaming either would break callers silently, which is a worse defect than a legacy name.
The board-state keys (`needs_king`, `king_action`) and the `KING-ACTION:` description block are
likewise untouched — they are a parsed contract owned by multica_board_state.py, and a user who
writes the block the docs describe must still have it recognised.

Data sources (all on the VPS, read fresh each run):
  /opt/data/multica_state.json    - Multica board state, pushed ~9:10 AM CT by
                                     Multica/scripts/multica_export.py (the operator's
                                     laptop, all workspaces). Carries items[] and
                                     alerts[] (pipeline-health, e.g. a sweep that
                                     failed to run at all).
  /opt/data/multica_inbound.json  - new vendor/client email on live cases, written
                                     by inbox_watcher.py (run at the top of this
                                     script). Multica has no email watcher of its
                                     own; this is it.

If the laptop was asleep and multica_state.json is stale, we still nag from
yesterday's data - that is correct: a stale item is exactly a forgotten one.

Nag ladder (operator's choice 2026-07-18, unchanged):
  day 0-1  : Telegram
  day 2-3  : Telegram (firmer)
  day >=4  : Telegram + IVY PHONE CALL
  deadline today/overdue : Telegram + IVY PHONE CALL

Speaks when there is anything needing the owner, any deadline, any pipeline ALERT, or
any new inbound mail on a live case. Silent otherwise - no noise on quiet days.
Runs daily 10:00 AM CT (15:00 UTC) via hermes cron 'king-action-nag'.

v3 changes (2026-07-20):
  - run inbox_watcher.py first, then surface its findings ("NEW MAIL").
  - surface state.alerts[] ("PIPELINE ALERT") - a sweep that never ran used to
    fail silently while the nag looked healthy off yesterday's file.
  - --dry-run: print the message, send nothing, call no one.
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import mer_config  # noqa: E402  (M32 — identity comes from the profile, not a literal)

# M46 — the liveness ledger. NOTE THE DIRECTION OF THE DEPENDENCY: heartbeat.alert() imports
# king_nag lazily, INSIDE the function, precisely so this module-level import cannot become a
# cycle. Do not move heartbeat's import of king_nag to module level.
import heartbeat  # noqa: E402

STATE = "/opt/data/multica_state.json"
INBOUND = "/opt/data/multica_inbound.json"
WATCHER = "/opt/data/scripts/inbox_watcher.py"
ENV = "/opt/data/.env"
IVY = "/opt/data/scripts/call_ivy.py"
# M32: the voice-escalation number comes from the profile. Empty string = no phone configured,
# in which case the voice call is skipped rather than dialled at a wrong number.
USER_PHONE = mer_config.phone()
IVY_ESCALATE_DAYS = 4

DRY_RUN = "--dry-run" in sys.argv


def _owner_name():
    """The profile owner's first name for the message header, or "" if unavailable.

    Never raises: this is presentation. An unconfigured profile must not be able to turn a
    reminder into a crash, and the header simply loses the name.
    """
    try:
        return (mer_config.legal_name() or "").split()[0]
    except Exception:
        return ""


def _header():
    who = _owner_name()
    return "*%s — Multica state*" % who if who else "*Multica state*"


def load_env():
    env = {}
    if os.path.exists(ENV):
        with open(ENV) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def send_telegram(msg):
    if DRY_RUN:
        print("---- DRY RUN: telegram message ----\n%s\n----" % msg)
        return True
    env = load_env()
    tok = env.get("TELEGRAM_BOT_TOKEN")
    # M32: the chat id is identity, not a secret — the profile wins, .env is the fallback.
    # (The bot TOKEN stays in .env and never goes in a profile.)
    if mer_config.notify_channel() == "none":
        print("SKIP telegram: notify channel is 'none' in the profile")
        return False
    uid = mer_config.notify_telegram_chat_id() or env.get("TELEGRAM_USER_ID")
    if not tok or not uid:
        print("SKIP telegram: creds missing")
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % tok
    payload = json.dumps({"chat_id": uid, "text": msg, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
        print("telegram sent")
        return True
    except Exception as exc:
        print("telegram FAILED: %s" % exc)
        return False


def call_ivy(purpose):
    if DRY_RUN:
        print("---- DRY RUN: would call Ivy ----\n%s\n----" % purpose)
        return True
    if not os.path.exists(IVY):
        print("SKIP ivy: dispatcher not found at %s" % IVY)
        return False
    if not USER_PHONE:
        print("SKIP ivy: no phone configured in the profile (set \"phone\" in profile.json)")
        return False
    try:
        res = subprocess.run(["python3", IVY, "--to", USER_PHONE, "--purpose", purpose, "--live"],
                             capture_output=True, text=True, timeout=90)
        print("ivy rc=%s out=%s" % (res.returncode, (res.stdout or "").strip()[:200]))
        return res.returncode == 0
    except Exception as exc:
        print("ivy FAILED: %s" % exc)
        return False


_DEGRADED = []


def _degraded(summary, key, detail=""):
    """Record a sub-step failure: printed, logged unbounded, alerted, and — crucially —
    remembered so the nag itself can say out loud that it is running on partial data."""
    _DEGRADED.append(summary)
    print("!! %s" % summary)
    if DRY_RUN:
        return
    heartbeat.log_error("mer-king-nag", "%s\n%s" % (summary, detail))
    heartbeat.alert("⚠️ king_nag is running DEGRADED: %s%s"
                    % (summary, ("\n\n" + detail) if detail else ""),
                    key="king-nag:%s" % key, cooldown=6 * 3600)


def run_board_state():
    """Refresh multica_state.json from the LIVE Multica board (VPS-side, via
    the server-side token) so the review works whether or not the operator's laptop is awake.
    Non-fatal: on any failure we fall back to whatever state file is present."""
    gen = "/opt/data/scripts/multica_board_state.py"
    if not os.path.exists(gen):
        return
    try:
        res = subprocess.run(["python3", gen], capture_output=True, text=True, timeout=60)
        print("board_state rc=%s out=%s" % (res.returncode, (res.stdout or "").strip()[:200]))
        if res.returncode != 0:
            # M46: "non-fatal" is not the same as "unremarkable". A refresh that keeps failing
            # means the nag is running off a frozen snapshot of the board and will happily
            # report last week's state as today's, with no visible difference.
            _degraded("board state refresh exited %s" % res.returncode,
                      "board-state-refresh", (res.stderr or res.stdout or "")[:600])
    except Exception as exc:
        print("board_state refresh failed (non-fatal, using existing state): %s" % exc)
        _degraded("board state refresh failed: %s" % exc, "board-state-refresh")


def run_watcher():
    """Best-effort: read the inbox before we build the message. A watcher failure
    must never block the nag - a broken watcher is itself worth a quiet log line,
    not a silent crash of the whole reminder."""
    if not os.path.exists(WATCHER):
        print("watcher not present at %s - skipping email check" % WATCHER)
        return
    try:
        res = subprocess.run(["python3", WATCHER], capture_output=True, text=True, timeout=120)
        print("watcher rc=%s out=%s" % (res.returncode, (res.stdout or "").strip()[:200]))
        if res.returncode != 0:
            # M46: a broken watcher used to cost exactly one log line. But this nag's "NEW MAIL"
            # section is sourced entirely from the watcher's output file — so a dead watcher
            # makes the nag print a confident, complete-looking message with the mail section
            # silently missing. That is the single most dangerous shape a failure can take here.
            _degraded("inbox_watcher exited %s — the NEW MAIL section of this nag is NOT "
                      "trustworthy" % res.returncode, "watcher-run",
                      (res.stderr or res.stdout or "")[:600])
    except Exception as exc:
        print("watcher run failed (non-fatal): %s" % exc)
        _degraded("inbox_watcher could not be run (%s) — the NEW MAIL section of this nag is "
                  "NOT trustworthy" % exc, "watcher-run")


def load_inbound():
    if not os.path.exists(INBOUND):
        return []
    try:
        with open(INBOUND, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("new_mail", []) or []
    except Exception:
        return []


def days_open(item):
    try:
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")).date()
    except Exception:
        return 0
    return (date.today() - created).days


def deadline_tag(item, today):
    dl = item.get("due_date")
    if not dl:
        return None, False
    try:
        d = datetime.strptime(dl, "%Y-%m-%d").date()
    except Exception:
        return None, False
    left = (d - today).days
    if left < 0:
        return "OVERDUE by %sd" % abs(left), True
    if left == 0:
        return "DUE TODAY", True
    if left <= 2:
        return "due in %sd" % left, False
    return "due %s" % dl, False


def build_message(state, inbound):
    items = state.get("items", [])
    alerts = state.get("alerts", []) or []
    needs_king = [i for i in items if i.get("needs_king")]
    deadlines = [i for i in items if i.get("due_date")]
    other_open = [i for i in items if not i.get("needs_king") and not i.get("due_date")]

    # speak if ANYTHING is actionable: needs-King, a deadline, a pipeline alert,
    # or new inbound mail. Otherwise stay silent.
    if not (needs_king or deadlines or alerts or inbound):
        return None, {}

    today = date.today()
    urgent = {}
    lines = []

    if alerts:
        lines.append("*⚠ PIPELINE ALERT*")
        for a in alerts:
            lines.append("- %s" % a.get("detail", a))
        lines.append("")

    if inbound:
        lines.append("*\U0001f4e8 NEW MAIL (on live cases)*")
        for m in inbound:
            lines.append("- *%s* — %s\n  _%s_" % (
                m.get("case", "?"),
                (m.get("subject") or "(no subject)"),
                (m.get("from") or "")))
        lines.append("")

    if needs_king:
        lines.append("*NEEDS YOU*")
        for it in sorted(needs_king, key=days_open, reverse=True):
            d = days_open(it)
            if d >= IVY_ESCALATE_DAYS:
                urgent[it["identifier"]] = it
            reason = it.get("needs_king_reason")
            suffix = " — open %sd" % d + (" · %s" % reason if reason else "")
            lines.append("- *%s* (%s)%s" % (it["title"].split("KING: ", 1)[-1],
                                            it["identifier"], suffix))
        lines.append("")

    if deadlines:
        lines.append("*DEADLINES*")
        for it in sorted(deadlines, key=lambda i: i["due_date"]):
            tag, is_urgent = deadline_tag(it, today)
            if is_urgent:
                urgent[it["identifier"]] = it
            lines.append("- *%s* (%s) — %s" % (it["title"], it["identifier"], tag))
        lines.append("")

    if other_open:
        lines.append("_%d other open item(s) in progress, no action needed._" % len(other_open))

    gen = state.get("generated_at", "")
    msg = _header() + "\n_as of %s_\n\n" % gen + "\n".join(lines)
    return msg, urgent


def main(note=None):
    note = note or (lambda text: None)
    del _DEGRADED[:]
    run_board_state()
    run_watcher()

    if not os.path.exists(STATE):
        # Not "nothing to do" — nothing KNOWN. Say so rather than exiting quietly with the
        # cheerful message of a clean day.
        msg = "no state file at %s - the nag has no board data at all" % STATE
        print(msg)
        note(msg)
        _degraded(msg, "no-state-file")
        return 1

    with open(STATE, encoding="utf-8") as fh:
        state = json.load(fh)
    inbound = load_inbound()

    msg, urgent = build_message(state, inbound)
    if _DEGRADED:
        # The nag must never look complete when it is not. This line goes INTO the message a
        # human reads, not only into a log they do not.
        warn = ("*⚠ THIS REPORT IS INCOMPLETE*\n" +
                "\n".join("- %s" % d for d in _DEGRADED) + "\n")
        msg = (msg + "\n\n" + warn) if msg else (_header() + "\n\n" + warn)
        for d in _DEGRADED:
            note(d)
    if not msg:
        print("nothing needs-King, no deadlines, no alerts, no new mail - staying silent")
        return 0

    send_telegram(msg)

    if urgent:
        titles = "; ".join(v["title"] for v in urgent.values())
        first_name = mer_config.legal_name().split()[0]
        call_ivy("%s, you have items that need attention today. %s. "
                 "These are either due today, overdue, or have been sitting for several days."
                 % (first_name, titles))
    else:
        print("no ivy escalation needed")
    return 1 if _DEGRADED else 0


if __name__ == "__main__":
    if DRY_RUN:
        sys.exit(main())
    # M46 — beat on every run. This job is the operator's daily read of the whole system; if it
    # stops firing, the most likely way to notice used to be "the operator realises they have not been
    # nagged in a while", which is not a monitoring strategy.
    sys.exit(heartbeat.guard("mer-king-nag", main, expect_seconds=108000.0))
