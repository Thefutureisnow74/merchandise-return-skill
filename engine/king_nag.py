#!/usr/bin/env python3
"""
king_nag.py - escalating reminder for King, sourced from Multica (v3 2026-07-20).

Data sources (all on the VPS, read fresh each run):
  /opt/data/multica_state.json    - Multica board state, pushed ~9:10 AM CT by
                                     Multica/scripts/multica_export.py (King's
                                     laptop, all workspaces). Carries items[] and
                                     alerts[] (pipeline-health, e.g. a sweep that
                                     failed to run at all).
  /opt/data/multica_inbound.json  - new vendor/client email on live cases, written
                                     by inbox_watcher.py (run at the top of this
                                     script). Multica has no email watcher of its
                                     own; this is it.

If the laptop was asleep and multica_state.json is stale, we still nag from
yesterday's data - that is correct: a stale item is exactly a forgotten one.

Nag ladder (King's choice 2026-07-18, unchanged):
  day 0-1  : Telegram
  day 2-3  : Telegram (firmer)
  day >=4  : Telegram + IVY PHONE CALL
  deadline today/overdue : Telegram + IVY PHONE CALL

Speaks when there is anything needs-King, any deadline, any pipeline ALERT, or
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


def run_board_state():
    """Refresh multica_state.json from the LIVE Multica board (VPS-side, via
    Lisa's token) so the review works whether or not King's laptop is awake.
    Non-fatal: on any failure we fall back to whatever state file is present."""
    gen = "/opt/data/scripts/multica_board_state.py"
    if not os.path.exists(gen):
        return
    try:
        res = subprocess.run(["python3", gen], capture_output=True, text=True, timeout=60)
        print("board_state rc=%s out=%s" % (res.returncode, (res.stdout or "").strip()[:200]))
    except Exception as exc:
        print("board_state refresh failed (non-fatal, using existing state): %s" % exc)


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
    except Exception as exc:
        print("watcher run failed (non-fatal): %s" % exc)


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
    msg = "*King — Multica state*\n_as of %s_\n\n" % gen + "\n".join(lines)
    return msg, urgent


def main():
    run_board_state()
    run_watcher()

    if not os.path.exists(STATE):
        print("no state file at %s - nothing to do" % STATE)
        return

    with open(STATE, encoding="utf-8") as fh:
        state = json.load(fh)
    inbound = load_inbound()

    msg, urgent = build_message(state, inbound)
    if not msg:
        print("nothing needs-King, no deadlines, no alerts, no new mail - staying silent")
        return

    send_telegram(msg)

    if urgent:
        titles = "; ".join(v["title"] for v in urgent.values())
        first_name = mer_config.legal_name().split()[0]
        call_ivy("%s, you have items that need attention today. %s. "
                 "These are either due today, overdue, or have been sitting for several days."
                 % (first_name, titles))
    else:
        print("no ivy escalation needed")


if __name__ == "__main__":
    main()
