#!/usr/bin/env python3
"""
heartbeat.py — the engine's liveness ledger and its one alarm bell (Blueprint M46).

WHY THIS EXISTS
---------------
Every scheduled component in this engine is SILENT WHEN HEALTHY, on purpose: a watchdog that
says "all clear" every hour is a watchdog nobody reads. The cost of that design is that
"nothing wrong" and "nothing running" look identical from the outside — and this engine has
now failed that way at least four times (2026-07-18, 07-20, 07-26, 07-28). The canonical
shape of the failure:

    the Gmail OAuth token expires -> every per-case query raises -> the exception handler
    prints one line and continues -> `seen` is never updated -> the run finishes and prints
    "done. 0 HIGH item(s)" -> which is EXACTLY what a quiet week prints.

Meanwhile every tier deadline in the ladder keeps running.

So: silence stays the healthy signal for OUTPUT, but liveness gets its own explicit channel.
Every scheduled component writes {name, ts, ok, err} on EVERY tick — a success and a failure
are both a heartbeat, they differ only in `ok`. One checker (`--check`, on its own frequent
job) compares each component's last beat against that component's declared cadence and speaks
up on three things and only three things:

    1. a MISSED beat      — the component did not run when it was supposed to
    2. ok:false           — it ran and reported a failure
    3. a non-zero rc      — it ran and died

A component that is broken can no longer report itself green, because the thing that reports
green is the same write that would have carried the error.

THE FILE
--------
ONE JSON file, `<data-dir>/mer_heartbeat.json`, written atomically (tmp + os.replace) so a
crash mid-write cannot corrupt it:

    {
      "version": 1,
      "components": {
        "<name>": {
          "name":  "<name>",                  # redundant on purpose: a component row is
          "ts":    "2026-07-28T16:32:10+00:00",#   self-describing when copied out of context
          "ok":    true,
          "err":   null,                      # str on failure
          "rc":    0,
          "detail":"idle - no mailbox change",
          "expect_seconds": 300,              # what "on time" means for THIS component
          "window": {"hours": [13, 23], "dows": [0,1,2,3,4]},   # or null for 24/7
          "consecutive_fail": 0,
          "last_ok_ts":   "...",
          "last_fail_ts": null,
          "runs": 41231
        }
      },
      "alerts": { "<key>": "<iso ts last sent>" }     # cooldown ledger, see alert()
    }

`expect_seconds` and `window` are written BY THE COMPONENT, not held in the checker. That is
deliberate: the thing that knows how often it runs is the thing that runs. A component that
is removed from the clock stops updating its row and the checker keeps complaining about it
until a human deletes the row — which is the correct behaviour for a job that vanished.

THE WINDOW
----------
Several components legitimately run only during US business hours, so a naive "no beat in an
hour" checker would cry wolf every single night. Each row therefore carries an optional
active window in UTC (`hours` = [start, end_exclusive], `dows` = python weekday(), Mon=0).
The checker ignores a component that is outside its window right now, and gives it one full
`expect_seconds` of grace after the window opens before calling it missing.

ALERTING
--------
`alert()` routes through `king_nag.send_telegram` — the alert channel that already exists and
already resolves the chat id from the profile. It is imported LAZILY and wrapped, with a
direct Telegram fallback, because "the alerter itself failed silently" is the exact bug class
this module was built to end (`mer_engine._telegram` used to end in `except Exception: pass`).
A failure to alert is printed AND recorded in the heartbeat file as a failed beat of the
pseudo-component `heartbeat-alert`, so a mute alarm bell is itself alarming.

Every alert carries a `key`; repeats of the same key inside `cooldown` seconds are suppressed.
A component broken for a week should wake a human a few times, not 1,008 times.

CLI
    heartbeat.py --check              compare every row to its cadence; alert; rc 0/1
    heartbeat.py --check --no-alert   same, print only (what a test / a human runs)
    heartbeat.py --status             human-readable table of every row
    heartbeat.py --beat NAME [--fail "msg"] [--rc N] [--expect SECONDS]
    heartbeat.py --test-alert         send exactly ONE test message and exit
    heartbeat.py --selftest           offline; no network, no alert, temp file only

USE FROM PYTHON

    import heartbeat
    rc = heartbeat.guard("mer-engine", main, expect_seconds=5400,
                         window={"hours": [13, 23], "dows": [0, 1, 2, 3, 4]})
    sys.exit(rc)

`guard()` beats ok on a clean return, and on ANY exception beats ok:false, alerts with the
traceback, and returns a non-zero rc — so a crash is loud in three independent places (the
heartbeat file, Telegram, and the exit code the scheduler logs).

No identity, no case identifier, no host path outside data_dir() lives in this file.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VERSION = 1

# How long a repeat of the same alert key is suppressed. Long enough that a component broken
# overnight does not produce a hundred notifications; short enough that a real outage is
# re-stated a few times a day and cannot be swiped away once and forgotten.
DEFAULT_COOLDOWN = 3600.0

# The default cadence claim for a component that beats without declaring one. Deliberately
# generous: a wrong-but-quiet default is better than a checker that invents alarms about
# components whose schedule it does not actually know.
DEFAULT_EXPECT_SECONDS = 3600.0

# The known components and the cadence/window each SHOULD declare. This table is advisory —
# a component's own beat always wins — and exists so `--check` can notice a component that
# has never beaten AT ALL, which is otherwise invisible (an absent row raises no alarm).
KNOWN_COMPONENTS = {
    "mer-hotpath":       {"expect_seconds": 300.0,    "window": None},
    "mer-engine":        {"expect_seconds": 5400.0,
                          "window": {"hours": [13, 24], "dows": [0, 1, 2, 3, 4]}},
    "mer-send-queue":    {"expect_seconds": 1800.0,   "window": {"hours": [13, 23]}},
    "mer-case-tick":     {"expect_seconds": 108000.0, "window": None},
    "mer-inbox-watcher": {"expect_seconds": 21600.0,  "window": None},
}


# ------------------------------------------------------------------------------ paths

def data_dir():
    """Where the heartbeat file lives. Config first, host default second, never a literal at
    a call site — this package ships to machines that have no shared data directory."""
    d = os.environ.get("MER_DATA_DIR")
    if d:
        return d
    if os.path.isdir("/opt/data"):
        return "/opt/data"
    base = os.environ.get("XDG_STATE_HOME") or os.environ.get("LOCALAPPDATA") \
        or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "merchandise-return")


def heartbeat_file():
    return os.environ.get("MER_HEARTBEAT_FILE") or os.path.join(data_dir(), "mer_heartbeat.json")


def error_log_file():
    """The NEVER-TRUNCATED error log. The bounded per-job logs roll a traceback out of
    existence within the hour (mer_engine.log was capped at 400 lines and rewritten every
    run); this file only ever grows, and only ever receives failures, so the forensic record
    of a bad night survives long enough to be read the next morning."""
    return os.environ.get("MER_ERROR_LOG") or os.path.join(data_dir(), "mer_errors.log")


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat()


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


# ------------------------------------------------------------------------- the ledger file

def load(path=None):
    """The ledger. A MISSING file is fine (fresh install); a CORRUPT one is not silently
    replaced — it is moved aside so the corruption is evidence rather than a mystery."""
    path = path or heartbeat_file()
    if not os.path.exists(path):
        return {"version": VERSION, "components": {}, "alerts": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("heartbeat file is not a JSON object")
    except Exception as exc:
        try:
            os.replace(path, path + ".corrupt")
        except Exception:
            pass
        sys.stderr.write("heartbeat: ledger unreadable (%s) — moved aside, starting fresh\n" % exc)
        return {"version": VERSION, "components": {}, "alerts": {}}
    data.setdefault("version", VERSION)
    data.setdefault("components", {})
    data.setdefault("alerts", {})
    return data


def save(data, path=None):
    path = path or heartbeat_file()
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def log_error(component, text):
    """Append to the unbounded error log. Best-effort: a failure to log must never be the
    reason a component dies, but it IS printed so it cannot pass unnoticed."""
    try:
        p = error_log_file()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("===== %s  %s =====\n%s\n" % (component, _iso(), text))
    except Exception as exc:
        sys.stderr.write("heartbeat: could not write the error log: %s\n" % exc)


# ---------------------------------------------------------------------------- the beat

def beat(name, ok=True, err=None, rc=0, detail=None, expect_seconds=None, window=None,
         path=None):
    """Record one tick of `name`. Returns the row written.

    A success and a failure are the SAME call with a different `ok` — there is no separate
    "report a failure" path that a component could forget to take. That symmetry is the whole
    point: the write that proves the component is alive is the write that carries its error.
    """
    known = KNOWN_COMPONENTS.get(name, {})
    if expect_seconds is None:
        expect_seconds = known.get("expect_seconds", DEFAULT_EXPECT_SECONDS)
    if window is None:
        window = known.get("window")

    data = load(path)
    prev = data["components"].get(name) or {}
    now = _iso()
    err_txt = None if err is None else str(err)[:2000]
    ok = bool(ok) and not err_txt and not rc

    row = {
        "name": name,
        "ts": now,
        "ok": ok,
        "err": err_txt,
        "rc": int(rc or 0),
        "detail": (str(detail)[:400] if detail is not None else None),
        "expect_seconds": float(expect_seconds),
        "window": window,
        "consecutive_fail": 0 if ok else int(prev.get("consecutive_fail") or 0) + 1,
        "last_ok_ts": now if ok else prev.get("last_ok_ts"),
        "last_fail_ts": prev.get("last_fail_ts") if ok else now,
        "runs": int(prev.get("runs") or 0) + 1,
    }
    data["components"][name] = row
    save(data, path)
    if not ok:
        log_error(name, "rc=%s ok=False err=%s detail=%s" % (row["rc"], err_txt, row["detail"]))
    return row


# ------------------------------------------------------------------------------ alerting

def _sanitize(text):
    """king_nag.send_telegram posts with parse_mode=Markdown, and a Python traceback is full
    of `_` and `*`. Unbalanced markdown makes Telegram reject the whole message with a 400 —
    i.e. the alert about the outage would itself vanish. Strip the markup characters rather
    than trust a traceback to be well-formed markdown."""
    return "".join(" " if c in "*_`[]" else c for c in str(text))


def _telegram_direct(text):
    """Last-resort transport, used only if king_nag is unavailable or itself fails.

    Duplicating ~15 lines of urllib is worth it here: this is the one code path whose job is
    to work when other code paths are broken, and making it depend on the module that just
    failed would defeat its purpose.
    """
    env = {}
    try:
        with open(os.path.join(data_dir(), ".env"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    tok = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    uid = env.get("TELEGRAM_USER_ID") or os.environ.get("TELEGRAM_USER_ID")
    if not (tok and uid):
        return False, "no telegram credentials available"
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok,
            data=json.dumps({"chat_id": uid, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"}), timeout=20)
        return True, "sent (direct)"
    except Exception as exc:
        return False, "direct telegram failed: %s" % exc


def alert(text, key=None, cooldown=DEFAULT_COOLDOWN, path=None, sender=None, force=False):
    """Push one alert to the operator. Returns (sent: bool, why: str).

    `key` de-duplicates: the same key inside `cooldown` seconds is suppressed. Callers should
    key on the CONDITION ("mer-engine:401"), not on the message text, so a message that
    contains a changing timestamp still de-duplicates.
    """
    key = key or (text[:60] if text else "alert")
    data = load(path)
    last = _parse((data.get("alerts") or {}).get(key))
    if last is not None and not force:
        age = (_now() - last).total_seconds()
        if age < cooldown:
            return False, "suppressed (same key %.0fs ago, cooldown %.0fs)" % (age, cooldown)

    body = _sanitize(text)
    sent, why = False, ""
    if sender is not None:
        try:
            sent = bool(sender(body))
            why = "sent (injected sender)" if sent else "injected sender declined"
        except Exception as exc:
            sent, why = False, "injected sender raised: %s" % exc
    else:
        try:
            import king_nag
            sent = bool(king_nag.send_telegram(body))
            why = "sent (king_nag)" if sent else "king_nag.send_telegram returned False"
        except Exception as exc:
            sent, why = False, "king_nag unavailable/raised: %s" % exc
        if not sent:
            ok2, why2 = _telegram_direct(body)
            sent, why = ok2, "%s; %s" % (why, why2)

    data = load(path)
    data.setdefault("alerts", {})
    if sent:
        data["alerts"][key] = _iso()
    save(data, path)

    if not sent:
        # A mute alarm bell is itself an alarm. Print it, log it unbounded, and record it as a
        # failed beat so `--check` reports "the alerter is broken" on the next pass.
        sys.stderr.write("heartbeat: ALERT NOT DELIVERED (%s): %s\n" % (why, text[:300]))
        log_error("heartbeat-alert", "UNDELIVERED (%s)\n%s" % (why, text))
        try:
            beat("heartbeat-alert", ok=False, err=why, detail=text[:200],
                 expect_seconds=DEFAULT_EXPECT_SECONDS, path=path)
        except Exception:
            pass
    return sent, why


# ------------------------------------------------------------------------------- the check

def _in_window(win, now=None):
    """Is `now` inside this component's declared active window?"""
    if not win:
        return True
    now = now or _now()
    hours = win.get("hours")
    dows = win.get("dows")
    if dows is not None and now.weekday() not in dows:
        return False
    if hours:
        lo, hi = int(hours[0]), int(hours[1])
        if not (lo <= now.hour < hi):
            return False
    return True


def _window_opened_at(win, now=None):
    """When the current window opened (UTC), or None for a 24/7 component. Used for grace: a
    job scheduled at :15 must not be called missing at :01 just because the window opened."""
    now = now or _now()
    if not win or not win.get("hours"):
        return None
    lo = int(win["hours"][0])
    return now.replace(hour=lo, minute=0, second=0, microsecond=0)


def check(path=None, now=None, known=None):
    """Compare every row to its own cadence. Returns a list of problem dicts.

    Three, and only three, things are problems:
      MISSED   — inside its window, and older than expect_seconds (plus window grace)
      FAILED   — the last beat said ok:false
      NEVER    — a KNOWN component that has no row at all (it has never once run)
    """
    now = now or _now()
    known = KNOWN_COMPONENTS if known is None else known
    data = load(path)
    rows = data.get("components") or {}
    problems = []

    for name, spec in sorted(known.items()):
        if name in rows:
            continue
        problems.append({
            "kind": "NEVER", "name": name, "detail":
            "no heartbeat has EVER been recorded — the component is not wired to the clock, "
            "or it has never completed a single tick."})

    for name, row in sorted(rows.items()):
        if not isinstance(row, dict):
            continue
        ts = _parse(row.get("ts"))
        expect = float(row.get("expect_seconds") or DEFAULT_EXPECT_SECONDS)
        win = row.get("window")
        if not row.get("ok"):
            problems.append({
                "kind": "FAILED", "name": name,
                "detail": "last tick at %s reported ok=false (rc=%s, %d consecutive): %s"
                          % (row.get("ts"), row.get("rc"),
                             int(row.get("consecutive_fail") or 1),
                             (row.get("err") or row.get("detail") or "no detail"))})
            continue
        if ts is None:
            problems.append({"kind": "FAILED", "name": name,
                             "detail": "heartbeat row has no readable timestamp"})
            continue
        if not _in_window(win, now):
            continue
        opened = _window_opened_at(win, now)
        if opened is not None and (now - opened).total_seconds() < expect:
            continue                    # window just opened; give it one full period of grace
        age = (now - ts).total_seconds()
        if age > expect:
            problems.append({
                "kind": "MISSED", "name": name,
                "detail": "last heartbeat %s (%.0f min ago); expected one every %.0f min"
                          % (row.get("ts"), age / 60.0, expect / 60.0)})
    return problems


def check_and_alert(path=None, now=None, do_alert=True, sender=None, out=None):
    """The scheduled checker. rc 0 = everything on time and ok; rc 1 = at least one problem."""
    out = out or sys.stdout
    problems = check(path=path, now=now)
    if not problems:
        out.write("heartbeat: all components on time and ok.\n")
        return 0
    lines = ["\U0001f6a8 merchandise-return engine — component health"]
    for p in problems:
        lines.append("- %s %s: %s" % (p["kind"], p["name"], p["detail"]))
        out.write("%s %s: %s\n" % (p["kind"], p["name"], p["detail"]))
    if do_alert:
        # One alert per component+kind, not one per run: the cooldown key must survive a
        # detail string that changes every ten minutes ("...42 min ago" -> "...52 min ago").
        for p in problems:
            alert("%s\n- %s %s: %s" % (lines[0], p["kind"], p["name"], p["detail"]),
                  key="health:%s:%s" % (p["name"], p["kind"]), path=path, sender=sender)
    return 1


# ------------------------------------------------------------------------------- the guard

class _Fail(object):
    """Collected non-fatal failures inside a guarded run (see guard()'s `note` callback)."""

    def __init__(self):
        self.items = []

    def __call__(self, text):
        self.items.append(str(text))

    def __bool__(self):
        return bool(self.items)

    __nonzero__ = __bool__


def guard(name, fn, expect_seconds=None, window=None, path=None, sender=None,
          alert_on_fail=True, out=None):
    """Run `fn` and beat exactly once, whatever happens. Returns an exit code.

    `fn` may take zero arguments, or one — in which case it is handed a callable `note` it can
    use to record a non-fatal failure ("three per-case Gmail queries errored"). A run that
    notes anything beats ok:false even though it returned normally. That is the fix for the
    engine's oldest lie: `print(...); continue` used to turn a total Gmail outage into a run
    that finished and reported "done. 0 HIGH item(s)".
    """
    out = out or sys.stdout
    failures = _Fail()
    t0 = time.time()
    try:
        try:
            rv = fn(failures)
        except TypeError as exc:
            # Only a zero-arg fn should be retried; a TypeError from INSIDE fn must not be
            # swallowed and re-run (that would run the engine twice).
            if "positional argument" not in str(exc) and "takes 0" not in str(exc):
                raise
            rv = fn()
        rc = int(rv) if isinstance(rv, int) else 0
    except BaseException as exc:                       # noqa: BLE001 — this is the top of a job
        tb = traceback.format_exc()
        detail = _classify_exception(exc)
        beat(name, ok=False, err="%s: %s" % (type(exc).__name__, exc), rc=1,
             detail=detail, expect_seconds=expect_seconds, window=window, path=path)
        log_error(name, tb)
        out.write("%s: FAILED — %s\n" % (name, exc))
        if alert_on_fail:
            alert("\U0001f6a8 %s CRASHED\n%s\n\n%s" % (name, detail, tb[-1200:]),
                  key="crash:%s:%s" % (name, type(exc).__name__), path=path, sender=sender)
        return 1

    took = time.time() - t0
    if failures:
        err = "; ".join(failures.items)[:1500]
        beat(name, ok=False, err=err, rc=rc or 1, detail="completed with %d failure(s) in %.1fs"
             % (len(failures.items), took), expect_seconds=expect_seconds, window=window,
             path=path)
        if alert_on_fail:
            alert("⚠️ %s completed but reported failures:\n%s" % (name, err),
                  key="degraded:%s" % name, path=path, sender=sender)
        return rc or 1

    beat(name, ok=(rc == 0), err=None if rc == 0 else "exit code %s" % rc, rc=rc,
         detail="ok in %.1fs" % took, expect_seconds=expect_seconds, window=window, path=path)
    if rc != 0 and alert_on_fail:
        alert("⚠️ %s exited %s" % (name, rc), key="rc:%s" % name, path=path,
              sender=sender)
    return rc


def _classify_exception(exc):
    """Name the failure modes that have actually bitten this engine, so an alert says WHAT is
    wrong rather than pasting a traceback and leaving the diagnosis to a human at 6am."""
    txt = "%s: %s" % (type(exc).__name__, exc)
    low = txt.lower()
    status = getattr(exc, "status", None)
    if status in (401, 403) or "missing authorization" in low or "401" in low and "multica" in low:
        return ("MULTICA AUTH FAILURE (%s). The board token is missing or rejected. Every board "
                "read and write is failing; the engine is doing nothing at all. Fix MULTICA_TOKEN "
                "in the shared .env / the job environment." % (status or "401"))
    if type(exc).__name__ in ("MulticaAuthError",):
        return ("MULTICA AUTH FAILURE. The board token is missing or rejected — the engine is "
                "doing nothing at all until it is fixed.")
    if "invalid_grant" in low or "invalid_client" in low or "token has been expired" in low \
            or "unauthorized_client" in low:
        return ("GMAIL/GOOGLE AUTH FAILURE. The OAuth refresh token is dead — no mail can be "
                "read and no reply can be sent. Re-authorise the mailbox.")
    if "gmail" in low and ("401" in low or "403" in low):
        return ("GMAIL AUTH FAILURE (%s). Mailbox reads are failing, so no vendor reply is "
                "being seen at all." % (status or "401/403"))
    if isinstance(exc, (OSError,)) or "urlopen error" in low or "timed out" in low:
        return "NETWORK/IO FAILURE — the component could not reach a dependency."
    return "unhandled exception"


def is_auth_failure(exc):
    """True for the two auth failures that make the engine silently do nothing."""
    d = _classify_exception(exc)
    return d.startswith("MULTICA AUTH") or d.startswith("GMAIL")


# ------------------------------------------------------------------------------ status/CLI

def status_text(path=None, now=None):
    now = now or _now()
    data = load(path)
    rows = data.get("components") or {}
    lines = ["heartbeat: %s" % (path or heartbeat_file()),
             "%-20s %-5s %-9s %-24s %s" % ("COMPONENT", "OK", "AGE", "LAST BEAT", "DETAIL")]
    if not rows:
        lines.append("(no component has ever beaten)")
    for name, row in sorted(rows.items()):
        ts = _parse(row.get("ts"))
        age = "-" if ts is None else "%.0fm" % ((now - ts).total_seconds() / 60.0)
        lines.append("%-20s %-5s %-9s %-24s %s"
                     % (name[:20], "yes" if row.get("ok") else "NO", age,
                        (row.get("ts") or "-")[:24],
                        (row.get("err") or row.get("detail") or "")[:60]))
    for p in check(path=path, now=now):
        lines.append("  !! %s %s: %s" % (p["kind"], p["name"], p["detail"]))
    return "\n".join(lines)


# ------------------------------------------------------------------------------- selftest

def _selftest():
    import shutil
    import tempfile

    fails = []

    def ck(name, cond, detail=""):
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               "" if cond else "  <- " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="mer_heartbeat_selftest_")
    hb = os.path.join(tmp, "hb.json")
    os.environ["MER_DATA_DIR"] = tmp
    os.environ["MER_HEARTBEAT_FILE"] = hb
    os.environ["MER_ERROR_LOG"] = os.path.join(tmp, "err.log")
    sent = []

    def fake_sender(text):
        sent.append(text)
        return True

    print("heartbeat --selftest  (offline; no network, no real alert)")
    print("-" * 74)

    print("A. the beat")
    r = beat("x", ok=True, detail="fine", expect_seconds=60, path=hb)
    ck("a beat records name/ts/ok/err", set(("name", "ts", "ok", "err")) <= set(r), sorted(r))
    ck("a healthy beat is ok", r["ok"] is True and r["err"] is None, r)
    r = beat("x", ok=False, err="boom", rc=2, path=hb, expect_seconds=60)
    ck("a failing beat carries the error", r["ok"] is False and r["err"] == "boom", r)
    ck("consecutive failures count up", r["consecutive_fail"] == 1, r)
    r = beat("x", ok=False, err="boom", rc=2, path=hb, expect_seconds=60)
    ck("consecutive failures keep counting", r["consecutive_fail"] == 2, r)
    r = beat("x", ok=True, path=hb, expect_seconds=60)
    ck("a success resets the failure counter", r["consecutive_fail"] == 0, r)
    ck("ok:true with a non-zero rc is still a FAILURE",
       beat("x", ok=True, rc=3, path=hb, expect_seconds=60)["ok"] is False)
    beat("x", ok=True, path=hb, expect_seconds=60)

    print("B. the check — the three, and only three, problems")
    now = _now()
    ck("a fresh healthy component is not a problem",
       not [p for p in check(path=hb, now=now, known={}) if p["name"] == "x"],
       check(path=hb, now=now, known={}))
    ck("a MISSED beat is caught",
       any(p["kind"] == "MISSED" for p in
           check(path=hb, now=now + timedelta(seconds=300), known={})))
    beat("y", ok=False, err="gmail 401", path=hb, expect_seconds=60)
    ck("ok:false is caught",
       any(p["kind"] == "FAILED" and p["name"] == "y" for p in check(path=hb, known={})))
    ck("a KNOWN component that has never beaten is caught",
       any(p["kind"] == "NEVER" and p["name"] == "mer-hotpath"
           for p in check(path=hb, known={"mer-hotpath": {}})))

    print("C. the window — a business-hours job is not 'missing' at 3am")
    # The beat's timestamp is pinned rather than taken from the wall clock, so this section
    # tests the WINDOW LOGIC and not what time the test happens to be run at.
    win = {"hours": [13, 23], "dows": [0, 1, 2, 3, 4]}
    beat("w", ok=True, path=hb, expect_seconds=3600, window=win)
    _d = load(hb)
    _d["components"]["w"]["ts"] = "2026-07-27T22:30:00+00:00"     # Monday evening, last run
    save(_d, hb)
    night = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)      # Tuesday 03:00 UTC
    ck("outside its window a stale component is silent",
       not [p for p in check(path=hb, now=night, known={}) if p["name"] == "w"])
    weekend = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)    # Saturday
    ck("outside its weekdays a stale component is silent",
       not [p for p in check(path=hb, now=weekend, known={}) if p["name"] == "w"])
    just_open = datetime(2026, 7, 28, 13, 5, tzinfo=timezone.utc)
    ck("a just-opened window grants one period of grace",
       not [p for p in check(path=hb, now=just_open, known={}) if p["name"] == "w"])
    late = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)
    ck("inside its window, well past its cadence, it IS missing",
       any(p["name"] == "w" and p["kind"] == "MISSED"
           for p in check(path=hb, now=late, known={})), check(path=hb, now=late, known={}))

    print("D. alerting — cooldown, and a mute bell is itself an alarm")
    del sent[:]
    ok1, _ = alert("first", key="k1", path=hb, sender=fake_sender)
    ok2, why2 = alert("second", key="k1", path=hb, sender=fake_sender)
    ck("first alert is sent", ok1 and len(sent) == 1)
    ck("a repeat of the same key is suppressed", ok2 is False and "suppressed" in why2, why2)
    ok3, _ = alert("other", key="k2", path=hb, sender=fake_sender)
    ck("a different key still gets through", ok3 and len(sent) == 2)
    ok4, _ = alert("forced", key="k1", path=hb, sender=fake_sender, force=True)
    ck("force overrides the cooldown", ok4 and len(sent) == 3)

    def dead_sender(text):
        raise RuntimeError("telegram is down")

    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TELEGRAM_USER_ID"] = ""
    ok5, why5 = alert("nobody will hear this", key="k3", path=hb, sender=dead_sender)
    ck("an undelivered alert reports failure", ok5 is False, why5)
    ck("an undelivered alert becomes a failed beat of heartbeat-alert",
       (load(hb)["components"].get("heartbeat-alert") or {}).get("ok") is False)
    ck("an undelivered alert is written to the unbounded error log",
       os.path.exists(os.environ["MER_ERROR_LOG"]))

    print("E. the guard — a crash is loud in three places")
    del sent[:]

    def good(note):
        return 0

    rc = guard("g-ok", good, expect_seconds=60, path=hb, sender=fake_sender)
    ck("a clean run returns 0 and beats ok", rc == 0 and load(hb)["components"]["g-ok"]["ok"])
    ck("a clean run alerts nothing", not sent)

    def zero_arg():
        return 0

    ck("a zero-argument function is supported",
       guard("g-zero", zero_arg, expect_seconds=60, path=hb, sender=fake_sender) == 0)

    ran = []

    def bad_typeerror(note):
        ran.append(1)
        raise TypeError("this is a real bug inside the job, not an arity mismatch")

    rc = guard("g-te", bad_typeerror, expect_seconds=60, path=hb, sender=fake_sender)
    ck("a TypeError from INSIDE the job is not retried", len(ran) == 1 and rc == 1, ran)

    def crash(note):
        raise RuntimeError("kaboom")

    del sent[:]
    rc = guard("g-crash", crash, expect_seconds=60, path=hb, sender=fake_sender)
    ck("a crash returns non-zero", rc == 1)
    ck("a crash beats ok:false", load(hb)["components"]["g-crash"]["ok"] is False)
    ck("a crash alerts", len(sent) == 1 and "CRASHED" in sent[0], sent)

    def degraded(note):
        note("3 of 3 per-case Gmail queries failed")
        return 0

    del sent[:]
    rc = guard("g-degraded", degraded, expect_seconds=60, path=hb, sender=fake_sender)
    ck("a run that returns 0 but noted failures is NOT green", rc != 0)
    ck("a degraded run beats ok:false", load(hb)["components"]["g-degraded"]["ok"] is False)
    ck("a degraded run alerts", len(sent) == 1 and "reported failures" in sent[0], sent)

    print("F. exception classification names the four recurring outages")

    class FakeMulticaAuth(RuntimeError):
        status = 401

    ck("a Multica 401 is named",
       "MULTICA AUTH" in _classify_exception(
           FakeMulticaAuth("GET /properties -> 401: missing authorization")))
    ck("is_auth_failure agrees",
       is_auth_failure(FakeMulticaAuth("GET /properties -> 401: missing authorization")))
    ck("a dead Google refresh token is named",
       "GMAIL/GOOGLE AUTH" in _classify_exception(RuntimeError("invalid_grant: bad refresh")))
    ck("a network failure is named",
       "NETWORK/IO" in _classify_exception(OSError("urlopen error timed out")))

    print("G. durability — a corrupt ledger is moved aside, never silently trusted")
    with open(hb, "w", encoding="utf-8") as fh:
        fh.write("{not json at all")
    data = load(hb)
    ck("a corrupt ledger yields an empty ledger, not a crash", data["components"] == {})
    ck("the corrupt ledger is preserved as evidence", os.path.exists(hb + ".corrupt"))
    beat("z", ok=True, path=hb, expect_seconds=60)
    ck("the ledger is usable again after corruption", load(hb)["components"]["z"]["ok"])

    print("H. check_and_alert exit codes")
    rc = check_and_alert(path=hb, do_alert=False, out=open(os.devnull, "w"))
    ck("a ledger with a failed component exits 1", rc == 1)
    save({"version": VERSION, "components": {}, "alerts": {}}, hb)
    beat("only-me", ok=True, path=hb, expect_seconds=86400)
    # patch this module's own globals — running as __main__ means `import heartbeat` would
    # create a SECOND module object and patch the wrong one.
    _g = globals()
    _known, _g["KNOWN_COMPONENTS"] = _g["KNOWN_COMPONENTS"], {}
    try:
        rc = check_and_alert(path=hb, do_alert=False, out=open(os.devnull, "w"))
    finally:
        _g["KNOWN_COMPONENTS"] = _known
    ck("an all-healthy ledger exits 0", rc == 0)

    print("I. structural — no identity, no host literal outside data_dir()")
    import ast
    import re as _re
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    exempt = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name in ("data_dir", "_selftest")]
    strays = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and n.value.startswith("/opt/")
              and not any(a <= n.lineno <= b for a, b in exempt)]
    ck("host path appears only inside data_dir()", not strays, strays)
    body = src.split("def _selftest")[0]
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    ck("no email address literal", not _re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", code))
    ck("no MER-N case identifier", not _re.search(r"\bMER-\d+\b", code))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    for banned in ("send_queue", "mer_send", "nudge", "gmail_transport", "draft_reply"):
        ck("the alarm bell cannot reach %s" % banned, banned not in imported, sorted(imported))

    shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 74)
    if fails:
        print("SELF-TEST FAILED: %s" % ", ".join(fails))
        return 1
    print("PASS — heartbeat self-test green (offline; no network, no alert delivered).")
    return 0


# ------------------------------------------------------------------------------------ CLI

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    if "--status" in argv:
        print(status_text())
        return 0
    if "--test-alert" in argv:
        sent, why = alert("✅ merchandise-return heartbeat: this is a TEST alert. "
                          "Nothing is wrong. Sent at %s." % _iso(),
                          key="test-alert", force=True)
        print("test alert: sent=%s (%s)" % (sent, why))
        return 0 if sent else 1
    if "--beat" in argv:
        i = argv.index("--beat")
        name = argv[i + 1] if len(argv) > i + 1 else "unnamed"
        err = None
        if "--fail" in argv:
            j = argv.index("--fail")
            err = argv[j + 1] if len(argv) > j + 1 else "unspecified failure"
        rc = 0
        if "--rc" in argv:
            j = argv.index("--rc")
            rc = int(argv[j + 1]) if len(argv) > j + 1 else 0
        expect = None
        if "--expect" in argv:
            j = argv.index("--expect")
            expect = float(argv[j + 1]) if len(argv) > j + 1 else None
        detail = None
        if "--detail" in argv:
            j = argv.index("--detail")
            detail = argv[j + 1] if len(argv) > j + 1 else None
        row = beat(name, ok=(err is None and not rc), err=err, rc=rc, detail=detail,
                   expect_seconds=expect)
        if err or rc:
            alert("⚠️ %s failed (rc=%s): %s" % (name, rc, err or "non-zero exit"),
                  key="rc:%s" % name)
        print("heartbeat: %s ok=%s" % (name, row["ok"]))
        return 0
    if "--check" in argv:
        return check_and_alert(do_alert=("--no-alert" not in argv))
    print(__doc__.strip().split("CLI")[-1].strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
