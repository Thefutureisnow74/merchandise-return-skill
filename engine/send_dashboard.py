#!/usr/bin/env python3
"""Run multica_dashboard.py and send the HTML output to King via Telegram.
Used by the /dashboard quick_command (deterministic, bypasses LLM).
Also callable by the dashboard skill or AGENTS.md trigger.
"""
import os
import subprocess
import sys
import json
import urllib.request
import urllib.error

ENV_FILE = "/opt/data/.env"
def _chat_id():
    """Whose Telegram this dashboard goes to — profile first, then the environment.

    Was a hardcoded chat id: one person's Telegram account baked into a skill that ships to
    strangers. WHO gets notified is profile data; the bot TOKEN stays a secret in the environment
    and never enters a profile.
    """
    try:
        import mer_config
        v = mer_config.notify_telegram_chat_id()
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get("TELEGRAM_USER_ID", "")


CHAT_ID = _chat_id()
GEN = "/opt/data/scripts/multica_dashboard.py"


def load_token():
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except IOError:
        return None
    return None


def send_html(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return True, data.get("result", {}).get("message_id")
            return False, data.get("description", "unknown error")
    except (urllib.error.URLError, Exception) as e:
        return False, str(e)


def run_and_send(generator=None):
    """Run `generator` (default: the whole-business /dashboard), send its stdout to Telegram.

    Parameterized so a second report (merchandise-return-dashboard) reuses this exact delivery
    path instead of a second copy of the Telegram-send logic -- see send_mer_dashboard.py.
    Telegram caps a message at 4096 chars; truncated to 4000 for margin. A report with many
    verbose cases can exceed this -- known limitation, not solved here (out of scope for what
    was asked; revisit with pagination if a real report actually gets cut off).
    """
    gen = generator or GEN
    result = subprocess.run(
        [sys.executable, gen],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        body = result.stderr.strip() or f"Dashboard generator exited code {result.returncode}"
        print(f"ERROR: {body}", file=sys.stderr)
        return 1
    dashboard = result.stdout.strip()
    if not dashboard:
        print("ERROR: dashboard generator returned empty output", file=sys.stderr)
        return 2

    token = load_token()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not in .env", file=sys.stderr)
        return 3

    ok, info = send_html(token, CHAT_ID, dashboard[:4000])
    if ok:
        print(f"dashboard sent msg_id={info}")
        return 0
    print(f"ERROR: telegram send failed: {info}", file=sys.stderr)
    return 4


def main():
    override = sys.argv[1] if len(sys.argv) > 1 else None
    return run_and_send(override)


if __name__ == "__main__":
    sys.exit(main())
