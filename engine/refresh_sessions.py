#!/usr/bin/env python3
"""
refresh_sessions.py  —  M27: stale-session SOUL/skill refresh advisor for Hermes/Lisa

WHY THIS EXISTS
---------------
Hermes freezes SOUL.md + the skills catalog into a session's *system prompt* at
AIAgent construction time. The gateway caches one AIAgent per session_key in an
in-memory dict (`_agent_cache`) keyed by a config signature. That signature
tracks model / credentials / toolsets / ephemeral-prompt / a few config keys +
the tool-registry generation -- but it does NOT hash the CONTENT of SOUL.md or
skill bodies. So editing SOUL.md (or a skill's text) never invalidates the cache,
and a running Telegram/agent session keeps serving the OLD SOUL until its cached
AIAgent is rebuilt for some other reason.

WHAT ACTUALLY REBUILDS A LIVE SESSION'S SYSTEM PROMPT (verified in gateway/run.py)
--------------------------------------------------------------------------------
Non-destructive (conversation transcript preserved, rebuilt from session_db):
  * Idle > 60 min  -> `_sweep_idle_cached_agents()` evicts the cached AIAgent
                      (_AGENT_CACHE_IDLE_TTL_SECS = 3600). The NEXT message
                      rebuilds the agent from the CURRENT SOUL.md + skills and
                      resumes the same conversation. This is the automatic path.
  * Gateway restart (`/restart`, or `docker restart <container>`): drains
                      in-flight turns, then every session rebuilds on its next
                      message. Transcripts persist in session_db. Fleet-wide,
                      immediate.
  * `/reload-skills` : rescans the skills dir at runtime so NEW/edited/removed
                      skills become usable immediately. NOTE: it does NOT rebuild
                      the frozen system prompt (skills are invoked at runtime via
                      /skill-name, skills_list, skill_view). Use it for SKILL
                      edits; it does NOT pick up SOUL.md edits.

Destructive to the conversation (fresh session_id -> history gone) -- AVOID for refresh:
  * `/new`, `/reset`
  * Reset policy crossing: idle > 24h OR daily at 04:00 (mode="both") -> the next
    message auto-resets to a fresh conversation.
  * Compression-exhaustion auto-reset.

Not useful for SOUL refresh:
  * `/model` -> switches the cached agent's model IN PLACE, keeps the frozen SOUL.
  * `/reload-mcp` -> reconnects MCP servers; does not reliably rebuild the prompt.

HARD TRUTH
----------
There is NO external IPC / HTTP admin endpoint / signal / CLI subcommand that can
evict a single live session's cached AIAgent from outside the gateway process.
Forcing "expiry" by editing sessions.json is UNSAFE: it trips the reset policy and
wipes the conversation on the next message. So this tool does NOT poke the cache.
It is a SAFE ADVISOR: it reads state, tells you which live sessions are still
serving a pre-edit SOUL, and prints the exact refresh action to take.

USAGE (run inside the container, HERMES_HOME defaults to /opt/data)
  python3 refresh_sessions.py                 # status + recommendation (read-only)
  python3 refresh_sessions.py --json          # machine-readable status
  python3 refresh_sessions.py --how           # print the refresh cheat-sheet
  python3 refresh_sessions.py --restart-gateway
                                              # PRINTS the graceful-restart command
                                              # (does NOT run it). Add --run to execute
                                              # a graceful in-container gateway restart.

This script never restarts anything unless BOTH --restart-gateway AND --run are
given. It never edits sessions.json, never sends a message, never deletes data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Constants mirrored from gateway/run.py + gateway/config.py (defaults).
AGENT_CACHE_IDLE_TTL_SECS = 3600           # _AGENT_CACHE_IDLE_TTL_SECS
DEFAULT_RESET_IDLE_MINUTES = 1440          # SessionResetPolicy.idle_minutes (24h)
DEFAULT_RESET_AT_HOUR = 4                  # SessionResetPolicy.at_hour
DEFAULT_RESET_MODE = "both"               # SessionResetPolicy.mode


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _now_local() -> datetime:
    return datetime.now()


def _proc_start_epoch(pid: int) -> float | None:
    """Accurate process start time (epoch seconds) via /proc/<pid>/stat.

    start = btime + starttime_ticks / CLK_TCK. The comm field (2) may contain
    spaces/parens, so parse the substring after the last ')'.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        rest = raw[raw.rindex(")") + 2:].split()
        starttime_ticks = int(rest[19])          # field 22 (1-based) = index 19 after comm
        clk_tck = os.sysconf("SC_CLK_TCK")
        btime = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None
        return btime + starttime_ticks / clk_tck
    except Exception:
        return None


def find_gateway_start() -> tuple[int | None, datetime | None]:
    """Return (pid, start_datetime) of the `hermes gateway run` process, if found.

    Reads /proc directly so it works without ps. Start time comes from
    /proc/<pid>/stat (btime + starttime/CLK_TCK), the true process start.
    """
    proc = Path("/proc")
    if not proc.exists():
        return None, None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
        except Exception:
            continue
        if "gateway run" in cmdline and "hermes" in cmdline:
            epoch = _proc_start_epoch(int(entry.name))
            start = datetime.fromtimestamp(epoch) if epoch else None
            return int(entry.name), start
    return None, None


def latest_mtime(path: Path) -> float | None:
    """Newest mtime under a dir (recursive), or the file's mtime, or None."""
    if not path.exists():
        return None
    if path.is_file():
        return path.stat().st_mtime
    newest = path.stat().st_mtime
    for p in path.rglob("*"):
        try:
            m = p.stat().st_mtime
            if m > newest:
                newest = m
        except Exception:
            continue
    return newest


def load_sessions(home: Path) -> dict:
    sj = home / "sessions" / "sessions.json"
    if not sj.exists():
        return {}
    try:
        return json.load(open(sj, encoding="utf-8"))
    except Exception as e:
        print(f"WARN: could not parse {sj}: {e}", file=sys.stderr)
        return {}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def classify_session(entry: dict, now: datetime) -> dict:
    updated = _parse_dt(entry.get("updated_at"))
    key = entry.get("session_key", "?")
    out = {
        "session_key": key,
        "display_name": entry.get("display_name"),
        "updated_at": entry.get("updated_at"),
        "idle_minutes": None,
        "agent_state": "unknown",
        "conversation_risk": "unknown",
        "note": "",
    }
    if updated is None:
        out["note"] = "no updated_at timestamp"
        return out
    idle = (now - updated).total_seconds()
    out["idle_minutes"] = round(idle / 60, 1)

    # Agent-cache state (non-destructive refresh boundary = 1h idle).
    if idle > AGENT_CACHE_IDLE_TTL_SECS:
        out["agent_state"] = "evicted"        # already swept; next msg = current SOUL
    else:
        out["agent_state"] = "cached"         # may hold pre-edit SOUL

    # Conversation reset policy (destructive) = 24h idle OR past today's 04:00.
    reset_soon = False
    if DEFAULT_RESET_MODE in ("idle", "both") and idle > DEFAULT_RESET_IDLE_MINUTES * 60:
        reset_soon = True
    if DEFAULT_RESET_MODE in ("daily", "both"):
        today_reset = now.replace(hour=DEFAULT_RESET_AT_HOUR, minute=0, second=0, microsecond=0)
        if now.hour < DEFAULT_RESET_AT_HOUR:
            today_reset -= timedelta(days=1)
        if updated < today_reset:
            reset_soon = True
    out["conversation_risk"] = "will hard-reset next msg" if reset_soon else "preserved"
    return out


def build_report(home: Path) -> dict:
    now = _now_local()
    soul = home / "SOUL.md"
    skills_dir = home / "skills"
    soul_m = latest_mtime(soul)
    skills_m = latest_mtime(skills_dir)
    gw_pid, gw_start = find_gateway_start()

    sessions_raw = load_sessions(home)
    sessions = [classify_session(e, now) for e in sessions_raw.values()]
    sessions.sort(key=lambda s: (s["idle_minutes"] is None, s["idle_minutes"] or 0))

    # A session is "possibly stale w.r.t. SOUL" if the file changed after the
    # gateway started AND the session's agent is still cached (idle <= 1h).
    soul_after_gw = bool(soul_m and gw_start and soul_m > gw_start.timestamp())
    skills_after_gw = bool(skills_m and gw_start and skills_m > gw_start.timestamp())
    stale_sessions = [
        s for s in sessions
        if s["agent_state"] == "cached" and (soul_after_gw or skills_after_gw)
    ]

    return {
        "now": now.isoformat(timespec="seconds"),
        "hermes_home": str(home),
        "soul_md": {
            "path": str(soul),
            "mtime": datetime.fromtimestamp(soul_m).isoformat(timespec="seconds") if soul_m else None,
        },
        "skills_dir": {
            "path": str(skills_dir),
            "latest_mtime": datetime.fromtimestamp(skills_m).isoformat(timespec="seconds") if skills_m else None,
        },
        "gateway": {
            "pid": gw_pid,
            "start": gw_start.isoformat(timespec="seconds") if gw_start else None,
        },
        "soul_edited_after_gateway_start": soul_after_gw,
        "skills_edited_after_gateway_start": skills_after_gw,
        "agent_cache_idle_ttl_min": AGENT_CACHE_IDLE_TTL_SECS // 60,
        "reset_policy": {
            "mode": DEFAULT_RESET_MODE,
            "idle_minutes": DEFAULT_RESET_IDLE_MINUTES,
            "at_hour": DEFAULT_RESET_AT_HOUR,
        },
        "sessions": sessions,
        "possibly_stale_sessions": [s["session_key"] for s in stale_sessions],
    }


CHEAT_SHEET = """\
HERMES SOUL / SKILL REFRESH -- CHEAT SHEET
==========================================
Editing SOUL.md or a skill does NOT reach a running session. Pick a lever:

SKILL edits (add / change / remove a skill):
  -> In the session, send:  /reload-skills
     Immediate, non-destructive, keeps the conversation and the prompt cache.

SOUL.md edits (identity / standing instructions):
  There is no per-session "reload SOUL" command. Choose:
   (a) DO NOTHING -- let it self-heal. After 60 min of session inactivity the
       cached agent is auto-evicted; the next message rebuilds from the new SOUL
       and keeps the conversation. Good when the edit isn't urgent.
   (b) FORCE NOW, fleet-wide, non-destructive -- restart the gateway:
         In any session:  /restart
         or on the host:  ssh hermes-vps 'docker restart <container>'
       Drains in-flight turns; conversations persist in session_db and resume.
   (c) AVOID for refresh: /new and /reset REBUILD the SOUL but WIPE the
       conversation. Only use if you also want a fresh conversation.

Do NOT try to "expire" a session by editing sessions.json -- that trips the
24h/daily reset policy and destroys the conversation on the next message.
"""


def print_status(report: dict) -> None:
    print("=" * 68)
    print("Hermes stale-session SOUL/skill refresh -- STATUS")
    print("=" * 68)
    print(f"now                 : {report['now']}")
    print(f"HERMES_HOME         : {report['hermes_home']}")
    print(f"SOUL.md mtime       : {report['soul_md']['mtime']}")
    print(f"skills/ latest mtime: {report['skills_dir']['latest_mtime']}")
    gw = report["gateway"]
    print(f"gateway pid / start : {gw['pid']} / {gw['start']}")
    print(f"agent-cache idle TTL: {report['agent_cache_idle_ttl_min']} min (non-destructive refresh)")
    rp = report["reset_policy"]
    print(f"reset policy        : mode={rp['mode']} idle={rp['idle_minutes']}min daily@{rp['at_hour']}:00 (destructive)")
    print(f"SOUL edited after gw start   : {report['soul_edited_after_gateway_start']}")
    print(f"skills edited after gw start : {report['skills_edited_after_gateway_start']}")
    print("-" * 68)
    print("SESSIONS:")
    if not report["sessions"]:
        print("  (none found)")
    for s in report["sessions"]:
        name = s.get("display_name") or ""
        print(f"  {s['session_key']}  [{name}]")
        print(f"      idle={s['idle_minutes']}min  agent={s['agent_state']}  conversation={s['conversation_risk']}")
        if s.get("note"):
            print(f"      note: {s['note']}")
    print("-" * 68)
    stale = report["possibly_stale_sessions"]
    if not (report["soul_edited_after_gateway_start"] or report["skills_edited_after_gateway_start"]):
        print("VERDICT: SOUL/skills were NOT edited since the gateway started -- every live")
        print("         agent was built with the current SOUL. No refresh needed.")
    elif not stale:
        print("VERDICT: SOUL/skills were edited, but no session has a still-cached agent")
        print("         (all are idle >60min). Each will load the new SOUL on its next")
        print("         message automatically. No action needed.")
    else:
        print("VERDICT: these sessions may still be serving the PRE-EDIT SOUL/skills:")
        for k in stale:
            print(f"           - {k}")
        print("         They self-refresh after 60min idle. To force NOW without losing")
        print("         the conversation, restart the gateway (see --how).")
    print("=" * 68)
    print("Run with --how for the full refresh cheat-sheet.")


# The container launches the gateway from /hermes.sh with exactly this line;
# there is NO supervisor that relaunches it if it dies. So any programmatic
# restart MUST kill AND relaunch, or the gateway stays down until the container
# is restarted. We mirror /hermes.sh's own launch command.
_GATEWAY_LAUNCH = (
    "nohup /opt/hermes/.venv/bin/hermes gateway run "
    ">>/opt/data/logs/gateway.log 2>&1 </dev/null &"
)


def do_restart(run: bool) -> int:
    """In-container gateway restart. Prints the plan; executes only with --run.

    IMPORTANT: the recommended path is the in-session `/restart` command -- it
    drains in-flight turns gracefully and relaunches itself. This tool's --run
    does NOT drain (it kill+relaunches), so prefer /restart when a turn may be
    mid-flight. Either way, conversations persist in session_db and resume.
    """
    import signal
    import subprocess
    gw_pid, _ = find_gateway_start()
    print(CHEAT_SHEET)
    print("-" * 68)
    print("PREFERRED: send  /restart  in a session (graceful drain + self-relaunch),")
    print("           or from the host:  docker restart <hermes-container-name>")
    print("-" * 68)
    if gw_pid is None:
        print("Could not locate the gateway process inside this container.")
        return 1
    print("This container runs the gateway as a bare `nohup ... &` (no supervisor),")
    print("so --run performs KILL + RELAUNCH (no drain):")
    print(f"    kill -TERM {gw_pid}   &&   {_GATEWAY_LAUNCH}")
    if not run:
        print()
        print("DRY RUN. Re-run with  --restart-gateway --run  to execute, or (better)")
        print("send /restart in a session so in-flight work drains first.")
        return 0
    print(f"\nStopping gateway pid {gw_pid} (SIGTERM) ...")
    try:
        os.kill(gw_pid, signal.SIGTERM)
    except ProcessLookupError:
        print("  (already gone)")
    except Exception as e:
        print(f"Failed to signal gateway: {e}")
        return 1
    # Wait up to ~15s for graceful exit.
    for _ in range(30):
        try:
            os.kill(gw_pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            break
    print("Relaunching gateway (detached) ...")
    subprocess.Popen(["/bin/sh", "-c", _GATEWAY_LAUNCH],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(2)
    new_pid, _ = find_gateway_start()
    if new_pid and new_pid != gw_pid:
        print(f"Gateway relaunched as pid {new_pid}. Sessions rebuild against the")
        print("current SOUL.md + skills on their next message.")
        return 0
    print("WARNING: could not confirm a fresh gateway pid. Check /opt/data/logs/gateway.log")
    print("and, if needed, restart the container from the host: docker restart <container>")
    return 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Hermes stale-session SOUL/skill refresh advisor (M27).")
    ap.add_argument("--json", action="store_true", help="emit machine-readable status")
    ap.add_argument("--how", action="store_true", help="print the refresh cheat-sheet and exit")
    ap.add_argument("--restart-gateway", action="store_true",
                    help="show (and with --run, perform) a graceful gateway restart")
    ap.add_argument("--run", action="store_true",
                    help="with --restart-gateway: actually send SIGTERM (default is dry-run)")
    args = ap.parse_args(argv)

    if args.how:
        print(CHEAT_SHEET)
        return 0

    if args.restart_gateway:
        return do_restart(run=args.run)

    report = build_report(hermes_home())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_status(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
