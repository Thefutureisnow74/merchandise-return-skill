#!/usr/bin/env python3
"""
mer_hotpath.py — the event-driven hot path (Blueprint M10).

THE PROBLEM
-----------
The engine polls. `mer-engine` runs hourly, business hours, Mon-Fri; `mer-case-tick` runs
once a day. So a vendor reply that lands at 09:01 Monday waits up to an hour, and one that
lands Friday at 17:30 waits until Monday morning — roughly 60 hours. Every tier deadline in
the ladder runs against the clock from the moment that reply lands, and a 🔴HIGH reply
(refused / legal threat / discrimination signal / refund approved) is exactly the mail where
the delay costs the most.

WHY NOT GMAIL PUSH (watch + Cloud Pub/Sub)
------------------------------------------
Gmail's real push notification API is the textbook answer and it is the wrong one for THIS
product. `users.watch()` requires, per recipient:
  * a Google Cloud project the user owns, with the Pub/Sub API enabled;
  * a Pub/Sub topic + subscription, with `gmail-api-push@system.gserviceaccount.com` granted
    publish rights on it;
  * EITHER a publicly reachable HTTPS endpoint with a CA-valid certificate on a verified
    domain (for push delivery) — which a laptop or a plain VPS does not have — or a pull
    subscriber, which is just a poller again, wearing a hat;
  * a `watch()` re-registration at least every 7 days, or delivery stops SILENTLY. A silent
    stop is the worst possible failure mode for this engine: it looks exactly like "the
    vendor hasn't replied yet", which is the state the whole ladder is built to detect.

This package ships to ONE person to run for THEMSELVES. That person has an email account.
They do not necessarily have a Google Cloud project, a domain, a certificate, or a public
host, and requiring all four would make onboarding fail before the first case is ever opened.
Push would also mean a second, publicly-exposed ingress into a system whose entire safety
model is "nothing sends without passing the veto window and the idempotency ledger".

So this module implements the achievable version of the same outcome: an adaptive
change-detector in front of the EXISTING pipeline. Gmail hands out a monotonic mailbox
cursor — `users.getProfile().historyId` — for one cheap API call and no setup at all. Polling
that cursor every minute costs ~1 request/min and tells us, with certainty and no
per-case fan-out, whether ANYTHING changed in the mailbox. Only when it moves (and only when
`users.history` confirms a message was actually ADDED to the inbox, not just read or
relabelled) do we spend the real work: the full classify -> resolution-check -> draft ->
veto-queue chain.

Result: detection latency goes from up to ~60 minutes (and ~60 hours over a weekend) to
~30-60 seconds, with zero new infrastructure, zero new credentials, and nothing for the
recipient to configure. That is the trade this module makes on purpose.

IT ADDS NO SECOND SEND PATH
---------------------------
This file does not classify, does not draft, does not send, and does not import anything that
can. When the gate opens it DISPATCHES `mer_engine.py` as a subprocess — the same entry point
the hourly job already runs, with the same flags, in the same environment. There is exactly
one classify/draft/queue implementation in this engine and this module is a trigger for it,
not a copy of it.

IT CANNOT WIDEN THE VETO WINDOW
-------------------------------
The queue is drained by `send_queue.py --process`, which runs ONLY on the business-hours
`mer-engine` job. This module NEVER runs it (asserted in the self-test). So a hot-path run at
03:00 can classify, alert and QUEUE a draft, but the draft still cannot leave the machine
until a business-hours run drains the queue — by which time a human is awake to veto it. The
veto window's protection is structural here, not a comment.

IT CANNOT DOUBLE-SEND
---------------------
Three layers, in order:
  1. a cross-process LOCK file, so a hot-path dispatch and the hourly cron job can never
     classify the same message concurrently and queue two different LLM-written drafts of the
     same reply (different bodies => different fingerprints => the ledger would not catch it);
  2. mer_engine's own per-case `seen` message-id state;
  3. `idempotency.reserve()` on the actual send.
And the send mode itself is untouched: this module never sets `MER_ENGINE_SEND`. It inherits
whatever the job/environment says, which ships as `test`.

MODES
-----
    mer_hotpath.py                 one gate check; dispatch only if new inbox mail. (cron)
    mer_hotpath.py --run-forever   internal adaptive loop, for a host with no minute-cron.
    mer_hotpath.py --force         dispatch unconditionally, under the lock (wrap the
                                   hourly job with this to share the lock).
    mer_hotpath.py --dry-run       decide and print; never dispatch.
    mer_hotpath.py --status        print the cursor state and exit.
    mer_hotpath.py --selftest      offline self-test; no network, no dispatch, no send.

ENVIRONMENT (all optional; every one has a working default)
    MER_DATA_DIR                 where cursor/lock live (default: /opt/data if it exists,
                                 else a per-user directory)
    MER_HOTPATH_STATE            explicit cursor file path
    MER_HOTPATH_LOCK             explicit lock file path
    MER_HOTPATH_MIN_INTERVAL     run-forever: seconds between polls when hot   (default 20)
    MER_HOTPATH_MAX_INTERVAL     run-forever: ceiling when idle                (default 300)
    MER_HOTPATH_BACKOFF          run-forever: idle growth factor               (default 1.6)
    MER_HOTPATH_LOCK_STALE       seconds after which a lock is treated as dead (default 900)
    MER_HOTPATH_DISPATCH_TIMEOUT seconds to let a dispatched engine run        (default 900)
    MER_HOTPATH_ENGINE_ARGS      override the dispatched engine args, space-separated
                                 (default: --commit --notify)
    MER_ENV_FILE                 shared KEY=VALUE secrets file to hand the dispatched child
                                 (default: <data dir>/.env if it exists)

WHY THIS MODULE LOADS A SECRETS FILE (M46)
------------------------------------------
It did not, and that was a silent, total failure of the hot path for an unknown number of
days. The other entry points are shell wrappers that each `export` MULTICA_TOKEN out of the
shared .env before running Python; this job is invoked directly by the scheduler, whose own
env-file setting is optional and was not configured. So the minute loop looked perfectly
healthy (rc=0, "idle — no mailbox change", 1439 times a day) and the ONE thing it exists to
do — dispatch the engine when mail actually lands — died every single time with

    multica_api.MulticaAPIError: GET /properties -> 401: missing authorization

A component whose failure mode only fires on the rare path it was built for is a component
that will be broken when you need it and green every other minute. The child now inherits the
same credentials the wrappers export, resolved here, from config, with no host literal.

AND IT NO LONGER EATS THE MAIL THAT BROKE IT
--------------------------------------------
`decide()` advances the mailbox cursor and `tick()` used to save that advanced cursor after
the dispatch, UNCONDITIONALLY. So a failed dispatch left the cursor sitting past the very
message that triggered it, and that message could never fire the hot path again — the 401
did not merely delay the reply, it LOST it. The authors got this exactly right for the
lock-busy branch (which rewinds on purpose) and missed it for the failure branch. A non-zero
dispatch now rewinds the cursor the same way, so the next tick re-evaluates the same mail.

No identity, no case identifier, no host path, no container name is written in this file.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The ONLY engine module imported here, and it is imported for a token, not for a verb.
# Anything that can classify, draft or send is reached by SUBPROCESS, never by import, so
# this process has no in-memory route to a send function. See _selftest().
import gmail_transport  # noqa: E402
import heartbeat        # noqa: E402  (M46 — liveness ledger + the one alarm bell)

GMAIL_PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GMAIL_HISTORY = "https://gmail.googleapis.com/gmail/v1/users/me/history"

ENGINE_MODULE = "mer_engine.py"
DEFAULT_ENGINE_ARGS = ["--commit", "--notify"]

# Draining the send queue is deliberately NOT one of the things this module may dispatch.
# It stays on the business-hours job so a draft queued overnight cannot leave unvetoed.
FORBIDDEN_DISPATCH = ("send_queue.py", "mer_send.py", "nudge.py")


# --------------------------------------------------------------------------- paths / config

def data_dir():
    """Directory for the cursor and lock. Config-driven, never a literal at the call site."""
    d = os.environ.get("MER_DATA_DIR")
    if d:
        return d
    if os.path.isdir("/opt/data"):
        return "/opt/data"
    base = os.environ.get("XDG_STATE_HOME") or os.environ.get("LOCALAPPDATA") \
        or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "merchandise-return")


def state_file():
    return os.environ.get("MER_HOTPATH_STATE") or os.path.join(data_dir(), "mer_hotpath_state.json")


def lock_file():
    # Shared with any other engine invocation that chooses to take it (see --force).
    return os.environ.get("MER_HOTPATH_LOCK") or os.path.join(data_dir(), "mer_engine.lock")


def _envf(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


def min_interval():
    return max(5.0, _envf("MER_HOTPATH_MIN_INTERVAL", 20))


def max_interval():
    return max(min_interval(), _envf("MER_HOTPATH_MAX_INTERVAL", 300))


def backoff_factor():
    return max(1.0, _envf("MER_HOTPATH_BACKOFF", 1.6))


def lock_stale_seconds():
    return max(30.0, _envf("MER_HOTPATH_LOCK_STALE", 900))


def dispatch_timeout():
    return max(30.0, _envf("MER_HOTPATH_DISPATCH_TIMEOUT", 900))


def engine_args():
    raw = os.environ.get("MER_HOTPATH_ENGINE_ARGS")
    if raw is None or not raw.strip():
        return list(DEFAULT_ENGINE_ARGS)
    return raw.split()


# ------------------------------------------------------------------- shared credentials

# Exactly the keys the shell wrappers export. Named explicitly rather than sourced, because
# the shared secrets file in the wild contains prose, comments and half-written lines, and
# `source`-ing one of those takes the whole job down (the lesson already encoded in
# case_tick_cron.sh's per-key `grep`).
SHARED_ENV_KEYS = ("MULTICA_TOKEN", "MULTICA_WORKSPACE_ID", "MULTICA_SERVER_URL",
                   "NANOGPT_API_KEY", "MINIMAX_API_KEY")


def env_file():
    """The shared KEY=VALUE secrets file, or None. Config first, data-dir default second."""
    p = os.environ.get("MER_ENV_FILE")
    if p:
        return p
    p = os.path.join(data_dir(), ".env")
    return p if os.path.isfile(p) else None


def load_shared_env(path=None, keys=SHARED_ENV_KEYS, environ=None):
    """Fill in any of `keys` that are missing from the environment. Returns what it added.

    DEFENSIVE ON PURPOSE, in both directions. An already-set variable is never overwritten,
    so a job that deliberately sets a different workspace keeps it. And a value already
    present is not re-read, so this is a no-op on the hosts where the wrapper already
    exported everything. multica_api resolves its token lazily now, but this module cannot
    assume the deployed copy of that module has caught up — it demonstrably had not on
    2026-07-28 — so the environment is corrected here regardless.
    """
    environ = os.environ if environ is None else environ
    path = path if path is not None else env_file()
    added = {}
    if not path or not os.path.isfile(path):
        return added
    wanted = {k for k in keys if not (environ.get(k) or "").strip()}
    if not wanted:
        return added
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k in wanted and k not in added:
                    added[k] = v.strip().strip('"').strip("'")
    except OSError:
        return added
    for k, v in added.items():
        environ[k] = v
    return added


# ------------------------------------------------------------------------------ cursor state

def load_state(path=None):
    path = path or state_file()
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(st, path=None):
    path = path or state_file()
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------- the lock

class LockBusy(Exception):
    """Another engine run holds the lock. Skip this tick; do not queue behind it."""


class EngineLock(object):
    """Cross-process, cross-platform exclusive lock via O_CREAT|O_EXCL.

    A faster loop is only safe if it cannot run concurrently with the slower one. Two
    mer_engine processes classifying the same inbound would each ask the LLM for a draft; the
    two drafts would differ, so their body fingerprints would differ, so idempotency.reserve()
    would see two DIFFERENT logical sends and permit both. That is the double-send this lock
    exists to make impossible.

    Stale locks (a killed process) are reclaimed after MER_HOTPATH_LOCK_STALE seconds, so a
    crash cannot wedge the hot path permanently.
    """

    def __init__(self, path=None, stale=None, now_fn=None):
        self.path = path or lock_file()
        self.stale = lock_stale_seconds() if stale is None else stale
        self.now = now_fn or time.time
        self.held = False

    def _write(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "at": self.now()}).encode("utf-8"))
        finally:
            os.close(fd)

    def _age(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                at = float(json.load(fh).get("at") or 0)
        except Exception:
            try:
                at = os.path.getmtime(self.path)
            except Exception:
                return None
        return self.now() - at

    def acquire(self):
        try:
            self._write()
            self.held = True
            return self
        except FileExistsError:
            pass
        age = self._age()
        if age is not None and age > self.stale:
            try:
                os.remove(self.path)
                self._write()
                self.held = True
                return self
            except Exception:
                pass
        raise LockBusy("engine lock held (%s)" % self.path)

    def release(self):
        if not self.held:
            return
        try:
            os.remove(self.path)
        except Exception:
            pass
        self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


# ------------------------------------------------------------------------------- the gate

def _get(url, token, timeout=25):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def current_history_id(token, getter=_get):
    """The mailbox's monotonic cursor. One cheap call; this is the whole idle cost."""
    return str(getter(GMAIL_PROFILE, token).get("historyId") or "")


def inbox_additions(token, start_history_id, getter=_get):
    """(has_new_inbox_mail, detail).

    Confirms the cursor moved because a message was ADDED to the inbox — not because we sent
    something, read something, or a filter relabelled something. Without this the hot path
    would dispatch the full engine on every read receipt.

    A 404 means the cursor is older than Gmail's history retention. That is unresolvable here,
    so we fail TOWARD work: dispatch, and let mer_engine's own per-message dedup decide there
    is nothing new. Failing toward silence would mean a missed vendor reply.
    """
    q = urllib.parse.urlencode({
        "startHistoryId": str(start_history_id),
        "historyTypes": "messageAdded",
        "labelId": "INBOX",
        "maxResults": "50",
    })
    try:
        data = getter(GMAIL_HISTORY + "?" + q, token)
    except urllib.error.HTTPError as exc:
        if getattr(exc, "code", None) == 404:
            return True, "history cursor expired (404) — dispatching to be safe"
        return True, "history lookup failed (%s) — dispatching to be safe" % exc
    except Exception as exc:
        return True, "history lookup failed (%s) — dispatching to be safe" % exc
    added = 0
    for rec in (data.get("history") or []):
        added += len(rec.get("messagesAdded") or [])
    if added:
        return True, "%d message(s) added to INBOX" % added
    return False, "mailbox changed but nothing was added to INBOX"


def decide(state, token, getter=_get):
    """Pure-ish decision step. Returns (dispatch: bool, reason: str, new_state: dict).

    Never dispatches on the very first run: a fresh install records a baseline silently, the
    same contract inbox_watcher already uses, so day one does not replay the mailbox.
    """
    st = dict(state or {})
    hid = current_history_id(token, getter=getter)
    prior = str(st.get("history_id") or "")
    st["history_id"] = hid
    st["checked_at"] = datetime.now(timezone.utc).isoformat()

    if not prior:
        st["baseline_at"] = st["checked_at"]
        return False, "first run — baseline cursor recorded, nothing dispatched", st
    if hid == prior:
        return False, "no mailbox change", st

    new_mail, detail = inbox_additions(token, prior, getter=getter)
    return bool(new_mail), detail, st


# ---------------------------------------------------------------------------- the dispatch

def engine_command(args=None):
    cmd = [sys.executable, os.path.join(HERE, ENGINE_MODULE)] + list(
        engine_args() if args is None else args)
    # Belt and braces: this module may never be the thing that drains the send queue.
    joined = " ".join(cmd)
    for bad in FORBIDDEN_DISPATCH:
        if bad in joined:
            raise RuntimeError("mer_hotpath must never dispatch %s" % bad)
    return cmd


def dispatch(args=None, runner=None, timeout=None):
    """Run the EXISTING engine entry point. Returns (exit_code, tail_of_output)."""
    cmd = engine_command(args)
    if runner is not None:
        return runner(cmd)
    env = dict(os.environ)
    # The child needs the board credentials this job's own environment may not carry. See
    # the module docstring: without this the dispatch 401'd every time it mattered.
    load_shared_env(environ=env)
    env["PYTHONIOENCODING"] = "utf-8"
    # MER_ENGINE_SEND is deliberately NOT set here — the send gate belongs to the job/env.
    try:
        proc = subprocess.run(cmd, cwd=HERE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env,
                              timeout=(timeout or dispatch_timeout()))
        out = proc.stdout.decode("utf-8", "replace")
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "mer_hotpath: dispatched engine timed out"


# ------------------------------------------------------------------------------- one tick

def _default_alerter(text, key):
    return heartbeat.alert(text, key=key)


def tick(token=None, getter=_get, runner=None, dry_run=False, force=False, out=None,
         alerter=None):
    """One gate check + conditional dispatch. Returns a dict describing what happened.

    `alerter(text, key)` is injectable so the self-test can prove the alarm fires without a
    network call. Default: heartbeat.alert (Telegram, with a cooldown per key).
    """
    out = out or sys.stdout
    alerter = alerter or _default_alerter
    st = load_state()
    # The cursor as it stood BEFORE this tick. This is the value we must be able to put back
    # if the dispatch fails — otherwise the mail that triggered the dispatch is skipped
    # forever, because the next tick compares against a cursor already past it.
    prior_hid = st.get("history_id")
    if force:
        fired, reason = True, "forced"
    else:
        if token is None:
            token = gmail_transport.access_token()
        fired, reason, st = decide(st, token, getter=getter)

    result = {"dispatched": False, "reason": reason, "history_id": st.get("history_id")}

    if not fired:
        st["consecutive_idle"] = int(st.get("consecutive_idle") or 0) + 1
        save_state(st)
        out.write("mer_hotpath: idle — %s\n" % reason)
        return result

    if dry_run:
        st["consecutive_idle"] = 0
        save_state(st)
        out.write("mer_hotpath: WOULD dispatch (%s): %s\n"
                  % (reason, " ".join(engine_command())))
        result["reason"] = reason
        result["would_dispatch"] = True
        return result

    try:
        lock = EngineLock().acquire()
    except LockBusy as exc:
        # Do NOT advance the cursor past unseen mail just because we were locked out: rewind
        # so the next tick re-evaluates. The run holding the lock is doing the work anyway.
        st["history_id"] = load_state().get("history_id")
        save_state(st)
        out.write("mer_hotpath: skipped — %s\n" % exc)
        result["reason"] = str(exc)
        result["locked_out"] = True
        return result
    try:
        code, output = dispatch(runner=runner)
    finally:
        lock.release()

    st["consecutive_idle"] = 0
    st["last_dispatch_at"] = datetime.now(timezone.utc).isoformat()
    st["last_dispatch_code"] = code

    if code:
        # ---- the compounding bug, closed ----------------------------------------------
        # Same reasoning as the LockBusy branch above, for the failure the authors missed:
        # the engine did NOT process this mail, so the cursor must not move past it. Rewind,
        # and the next tick (60 seconds away) re-evaluates the same message. Combined with
        # the alert below, a broken dispatch is now loud AND recoverable instead of silent
        # AND lossy.
        st["history_id"] = prior_hid
        st["consecutive_dispatch_fail"] = int(st.get("consecutive_dispatch_fail") or 0) + 1
        st["last_dispatch_error_at"] = st["last_dispatch_at"]
        st["last_dispatch_tail"] = "\n".join(
            [ln for ln in str(output or "").splitlines() if ln.strip()][-20:])[:4000]
    else:
        st["consecutive_dispatch_fail"] = 0
    save_state(st)

    out.write("mer_hotpath: DISPATCHED engine (%s) -> exit %s\n" % (reason, code))
    if output:
        tail = "\n".join([ln for ln in str(output).splitlines() if ln.strip()][-12:])
        out.write(tail + "\n")

    if code:
        n = st["consecutive_dispatch_fail"]
        tail = st.get("last_dispatch_tail") or "(no output)"
        heartbeat.log_error("mer-hotpath", "dispatch rc=%s (failure #%d)\n%s" % (code, n, tail))
        detail = _dispatch_failure_detail(tail)
        # Keyed on the CONDITION, not the message: the count and the timestamp change every
        # minute, and a key that changes every minute is a key that never de-duplicates.
        alerter("\U0001f6a8 mer-hotpath: the engine dispatch FAILED (exit %s, %d in a row).\n"
                "%s\nThe mailbox cursor has been rewound, so the triggering mail will be "
                "retried — but nothing is being classified until this is fixed.\n\n%s"
                % (code, n, detail, tail[-900:]),
                "hotpath-dispatch-fail:%s" % _failure_kind(tail))
        out.write("mer_hotpath: cursor REWOUND to %s (dispatch failed) — %s\n"
                  % (prior_hid, detail))
        result["rewound"] = True

    result.update({"dispatched": True, "exit_code": code})
    return result


def _failure_kind(tail):
    """A stable de-duplication key for the class of failure, from the child's output."""
    low = str(tail or "").lower()
    if "missing authorization" in low or "401" in low:
        return "multica-401"
    if "invalid_grant" in low or "unauthorized_client" in low:
        return "google-auth"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    return "other"


def _dispatch_failure_detail(tail):
    kind = _failure_kind(tail)
    return {
        "multica-401": "MULTICA AUTH FAILURE (401) — the board token is missing or rejected "
                       "in the dispatched child's environment. Check MULTICA_TOKEN in the "
                       "shared .env and in this job's environment.",
        "google-auth": "GOOGLE/GMAIL AUTH FAILURE — the OAuth refresh token is dead. No mail "
                       "can be read until the mailbox is re-authorised.",
        "timeout": "The dispatched engine exceeded its timeout and was killed.",
    }.get(kind, "The dispatched engine exited non-zero. See the tail below.")


# ---------------------------------------------------------------------- adaptive long-poll

def next_interval(prev, dispatched):
    """Adaptive backoff. Hot after a hit, gently backing off while the mailbox is quiet.

    The point is to spend request budget where the mail is: a case that just got a reply is
    very likely to get another one in the next few minutes (vendor threads arrive in bursts),
    while a mailbox that has been silent for an hour does not need a 20-second poll.
    """
    lo, hi, f = min_interval(), max_interval(), backoff_factor()
    if dispatched:
        return lo
    return min(hi, max(lo, (prev or lo) * f))


def run_forever(getter=_get, runner=None, sleep_fn=None, max_ticks=None, out=None,
                token_fn=None):
    """The clock for a host with no minute-granularity cron. One process, no ingress."""
    out = out or sys.stdout
    sleep_fn = sleep_fn or time.sleep
    token_fn = token_fn or gmail_transport.access_token
    out.write("mer_hotpath: watching (poll %.0f-%.0fs adaptive). Ctrl-C to stop.\n"
              % (min_interval(), max_interval()))
    interval, ticks, fired = min_interval(), 0, 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        try:
            r = tick(token=token_fn(), getter=getter, runner=runner, out=out)
        except Exception as exc:                      # a bad tick must never kill the clock
            out.write("mer_hotpath: tick error (%s) — backing off\n" % exc)
            r = {"dispatched": False}
        if r.get("dispatched"):
            fired += 1
        interval = next_interval(interval, r.get("dispatched"))
        sleep_fn(interval if max_ticks is None else 0)
    return fired


# -------------------------------------------------------------------------------- selftest

def _code_only(src):
    """`src` with comments and docstrings removed, so a structural check tests CODE not prose.

    A literal in a docstring is documentation; a literal in an expression is configuration
    baked into the program. Only the second one is the bug these checks hunt.
    """
    import io
    import tokenize
    out, prev = [], tokenize.NEWLINE
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and prev in (
                    tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
                    tokenize.ENCODING):
                continue                       # a bare string statement == a docstring
            if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENCODING):
                out.append(tok.string)
            prev = tok.type
    except (tokenize.TokenError, IndentationError):
        return src
    return "\n".join(out)


def _selftest():
    """Offline. No network, no dispatch, no board, no send."""
    import ast
    import tempfile

    fails = []

    def ck(name, cond, detail=""):
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               "" if cond else "  <- " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="mer_hotpath_selftest_")
    os.environ["MER_DATA_DIR"] = tmp
    os.environ["MER_HOTPATH_STATE"] = os.path.join(tmp, "state.json")
    os.environ["MER_HOTPATH_LOCK"] = os.path.join(tmp, "engine.lock")
    os.environ["MER_ENGINE_SEND"] = "off"

    print("mer_hotpath --selftest  (offline)")
    print("-" * 70)
    print("A. the gate")

    def fake_getter(hid, added=0, history_error=None):
        def _g(url, token, timeout=25):
            if url.startswith(GMAIL_PROFILE):
                return {"historyId": hid}
            if history_error:
                raise history_error
            return {"history": [{"messagesAdded": [{"message": {"id": "m%d" % i}}
                                                   for i in range(added)]}] if added else []}
        return _g

    d, why, st = decide({}, "tok", getter=fake_getter("100"))
    ck("first run records a baseline and does not dispatch", d is False and st["history_id"] == "100", why)

    d, why, st2 = decide(st, "tok", getter=fake_getter("100"))
    ck("unchanged cursor does not dispatch", d is False, why)

    d, why, _ = decide(st, "tok", getter=fake_getter("140", added=2))
    ck("new INBOX message dispatches", d is True and "2 message" in why, why)

    d, why, _ = decide(st, "tok", getter=fake_getter("140", added=0))
    ck("cursor moved but nothing added to INBOX does NOT dispatch", d is False, why)

    err404 = urllib.error.HTTPError(GMAIL_HISTORY, 404, "Not Found", None, None)
    d, why, _ = decide(st, "tok", getter=fake_getter("999", history_error=err404))
    ck("expired history cursor fails TOWARD work (dispatches)", d is True and "404" in why, why)

    d, why, _ = decide(st, "tok", getter=fake_getter("999", history_error=RuntimeError("boom")))
    ck("history lookup failure fails TOWARD work", d is True, why)

    print("B. the lock (no double-classify, therefore no double-draft)")
    lk = EngineLock().acquire()
    busy = False
    try:
        EngineLock().acquire()
    except LockBusy:
        busy = True
    ck("a second holder is refused", busy)
    lk.release()
    lk2 = None
    try:
        lk2 = EngineLock().acquire()
        ck("lock is reusable after release", True)
    except LockBusy as e:
        ck("lock is reusable after release", False, e)
    finally:
        if lk2:
            lk2.release()

    # stale=-1.0, not 0.0. acquire() reclaims on `age > self.stale`, a strict >, and the lock it
    # is about to reclaim was stamped microseconds earlier with time.time() — whose granularity
    # on Windows is ~0.78 ms, so two consecutive calls almost always return the identical float.
    # age comes out exactly 0.0, the reclaim is refused, the lock stays held, and three later
    # checks cascade into an uncaught LockBusy that reds the whole module. Green on Linux, red on
    # Windows, for no production reason at all — the real stale window is 900 s. Reported
    # 2026-07-31 from a Windows install.
    st_lock = EngineLock(stale=-1.0)
    # simulate a dead holder: write a lock, then reclaim it because it is stale
    EngineLock().acquire()
    reclaimed = False
    try:
        st_lock.acquire()
        reclaimed = True
        st_lock.release()
    except LockBusy:
        pass
    ck("a stale lock is reclaimed (a crash cannot wedge the hot path)", reclaimed)

    print("C. dispatch reuses the one existing chain and cannot drain the queue")
    cmd = engine_command()
    ck("dispatches mer_engine.py", ENGINE_MODULE in " ".join(cmd), cmd)
    ck("dispatches with --commit --notify", "--commit" in cmd and "--notify" in cmd, cmd)
    for bad in FORBIDDEN_DISPATCH:
        ck("never dispatches %s" % bad, bad not in " ".join(cmd))
    blocked = False
    try:
        # an operator trying to smuggle the queue-drainer in through the arg override
        engine_command(["--commit", "send_queue.py", "--process"])
    except RuntimeError:
        blocked = True
    ck("an override that smuggles in the queue-drainer is refused", blocked)

    print("D. the tick end to end (stubbed dispatcher — nothing runs)")
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        return 0, "stub engine ok"

    save_state({"history_id": "100"})
    r = tick(token="tok", getter=fake_getter("100"), runner=fake_runner)
    ck("idle tick does not dispatch", r["dispatched"] is False and not calls, r)
    r = tick(token="tok", getter=fake_getter("150", added=1), runner=fake_runner)
    ck("hot tick dispatches exactly once", r["dispatched"] is True and len(calls) == 1, r)
    ck("cursor advanced after dispatch", load_state().get("history_id") == "150")
    ck("lock released after dispatch", not os.path.exists(lock_file()))

    held = EngineLock().acquire()
    save_state({"history_id": "150"})
    r = tick(token="tok", getter=fake_getter("200", added=1), runner=fake_runner)
    ck("a tick locked out by the hourly job skips instead of racing it",
       r.get("locked_out") is True and len(calls) == 1, r)
    held.release()

    save_state({"history_id": "200"})
    r = tick(token="tok", getter=fake_getter("250", added=1), runner=fake_runner, dry_run=True)
    ck("--dry-run never dispatches", r["dispatched"] is False and len(calls) == 1, r)

    print("D2. a FAILED dispatch must not eat the mail that triggered it (M46)")
    alerts = []

    def capture(text, key):
        alerts.append((key, text))
        return True, "captured"

    def failing_runner(cmd):
        calls.append(cmd)
        # The real child output starts with a Python traceback banner. That banner is OMITTED
        # from this fixture on purpose: tick() echoes the child's output tail, run_tests.py
        # scans module output for the banner, and a green self-test would otherwise be
        # reported as a crash. The load-bearing part — the error line the failure classifier
        # keys on — is verbatim.
        return 1, ('  File "/x/multica_api.py", line 55, in _req\n'
                   "multica_api.MulticaAPIError: GET /properties -> 401: "
                   '{"error":"missing authorization"}')

    save_state({"history_id": "300"})
    n_before = len(calls)
    r = tick(token="tok", getter=fake_getter("350", added=1), runner=failing_runner,
             alerter=capture)
    ck("a failing dispatch still reports itself dispatched", r["dispatched"] is True, r)
    ck("a failing dispatch reports the exit code", r.get("exit_code") == 1, r)
    ck("THE CURSOR IS REWOUND so the mail fires again next tick",
       load_state().get("history_id") == "300", load_state())
    ck("the failure is counted", load_state().get("consecutive_dispatch_fail") == 1,
       load_state())
    ck("a failing dispatch ALERTS", len(alerts) == 1, alerts)
    ck("the alert key names the failure class, not the timestamp",
       alerts and alerts[0][0] == "hotpath-dispatch-fail:multica-401", alerts)
    ck("the alert says what is actually wrong",
       alerts and "MULTICA AUTH FAILURE" in alerts[0][1], alerts)
    ck("the dispatch was attempted exactly once", len(calls) == n_before + 1, calls)

    r = tick(token="tok", getter=fake_getter("360", added=1), runner=failing_runner,
             alerter=capture)
    ck("consecutive failures accumulate", load_state().get("consecutive_dispatch_fail") == 2,
       load_state())
    ck("the cursor stays rewound while it keeps failing",
       load_state().get("history_id") == "300", load_state())

    r = tick(token="tok", getter=fake_getter("370", added=1), runner=fake_runner,
             alerter=capture)
    ck("a recovered dispatch advances the cursor again",
       load_state().get("history_id") == "370", load_state())
    ck("a recovered dispatch clears the failure counter",
       load_state().get("consecutive_dispatch_fail") == 0, load_state())

    print("D3. the dispatched child inherits the shared credentials (M46)")
    envp = os.path.join(tmp, "shared.env")
    with open(envp, "w", encoding="utf-8") as fh:
        fh.write("# a real secrets file has prose and junk in it\n"
                 "this line is not a KEY=VALUE pair at all\n"
                 'MULTICA_TOKEN="tok-from-file"\n'
                 "MULTICA_WORKSPACE_ID=ws-from-file\n"
                 "UNRELATED_SECRET=nope\n")
    fake_env = {}
    added = load_shared_env(path=envp, environ=fake_env)
    ck("the token is read out of the shared file", fake_env.get("MULTICA_TOKEN") == "tok-from-file",
       fake_env)
    ck("quotes are stripped", '"' not in fake_env.get("MULTICA_TOKEN", '"'))
    ck("only the named keys are taken", "UNRELATED_SECRET" not in fake_env, fake_env)
    ck("prose lines do not break the parse", "this line is not a KEY" not in str(added))
    preset = {"MULTICA_TOKEN": "already-set"}
    load_shared_env(path=envp, environ=preset)
    ck("an already-set variable is NEVER overwritten",
       preset["MULTICA_TOKEN"] == "already-set", preset)
    ck("a missing env file is not an error",
       load_shared_env(path=os.path.join(tmp, "nope.env"), environ={}) == {})

    print("E. adaptive backoff")
    lo, hi = min_interval(), max_interval()
    ck("a hit resets to the fast interval", next_interval(hi, True) == lo)
    grow = next_interval(lo, False)
    ck("idle backs off", grow > lo, grow)
    v = lo
    for _ in range(50):
        v = next_interval(v, False)
    ck("backoff is capped", v <= hi, v)

    print("F. run_forever loops, survives a bad tick, and never sends")
    def boom_getter(url, token, timeout=25):
        raise RuntimeError("network down")
    n = run_forever(getter=boom_getter, runner=fake_runner, sleep_fn=lambda s: None,
                    max_ticks=3, out=open(os.devnull, "w"), token_fn=lambda: "tok")
    ck("a failing tick does not kill the clock", isinstance(n, int))

    print("G. structural — no send path, no identity, no host literals")
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # Transitive, not just direct: the guarantee is that this process has NO in-memory route to
    # a send/draft function, so an edit two modules away must break the test, not slip past it.
    local = {f[:-3]: os.path.join(HERE, f) for f in os.listdir(HERE) if f.endswith(".py")}
    reach, stack = set(imported), list(imported)
    while stack:
        nm = stack.pop()
        if nm not in local:
            continue
        try:
            sub = ast.parse(open(local[nm], encoding="utf-8").read())
        except Exception:
            continue
        for n in ast.walk(sub):
            names = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else (
                [n.module.split(".")[0]] if isinstance(n, ast.ImportFrom)
                and n.module and n.level == 0 else [])
            for nn in names:
                if nn not in reach:
                    reach.add(nn)
                    stack.append(nn)
    for banned in ("nudge", "send_queue", "mer_send", "draft_reply", "classify_llm"):
        ck("cannot reach %s (transitively)" % banned, banned not in reach,
           sorted(x for x in reach if x in local))

    # Prose is not configuration. The rule being enforced is "no identity or host literal in
    # the CODE"; the module docstring is allowed to name Google's own push service account and
    # to document the /opt/data default, because neither is a thing the program acts on.
    body = _code_only(src.split("def _selftest")[0])
    import re as _re
    ck("no MER-N case identifier", not _re.search(r"\bMER-\d+\b", body))
    ck("no email address literal", not _re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", body))
    ck("no hardcoded container name", "hermes" not in body.lower())
    # A host path may exist ONLY as a detected default inside data_dir(); anywhere else it is
    # a call site quietly assuming somebody else's machine.
    exempt = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name in ("data_dir", "_selftest")]
    strays = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and n.value.startswith("/opt/")
              and not any(a <= n.lineno <= b for a, b in exempt)]
    ck("host path appears only inside data_dir()", not strays, strays)
    ck("never sets MER_ENGINE_SEND", "MER_ENGINE_SEND\"] =" not in body
       and "MER_ENGINE_SEND'] =" not in body)

    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass

    print("-" * 70)
    if fails:
        print("SELF-TEST FAILED: %s" % ", ".join(fails))
        return 1
    print("PASS — mer_hotpath self-test green (offline; no dispatch, no send).")
    return 0


# ------------------------------------------------------------------------------------ CLI

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    if "--status" in argv:
        st = load_state()
        print("mer_hotpath")
        print("  state file  : %s" % state_file())
        print("  lock file   : %s%s" % (lock_file(),
                                        "  (HELD)" if os.path.exists(lock_file()) else ""))
        print("  env file    : %s" % (env_file() or "(none found — the dispatched child will "
                                                    "inherit this process's environment only)"))
        print("  cursor      : %s" % (st.get("history_id") or "(no baseline yet)"))
        print("  last check  : %s" % (st.get("checked_at") or "-"))
        print("  last dispatch: %s (exit %s)" % (st.get("last_dispatch_at") or "-",
                                                 st.get("last_dispatch_code")))
        # last_dispatch_code used to be written and never read by anything. It is now the
        # health signal for this component, so print it, act on it, and heartbeat it.
        nfail = int(st.get("consecutive_dispatch_fail") or 0)
        print("  dispatch fails (consecutive): %d%s"
              % (nfail, "   <-- THE ENGINE IS NOT RUNNING" if nfail else ""))
        if nfail:
            print("  last error at: %s" % (st.get("last_dispatch_error_at") or "-"))
            print("  last error tail:")
            for ln in (st.get("last_dispatch_tail") or "").splitlines()[-10:]:
                print("    %s" % ln)
        print("  idle ticks  : %s" % (st.get("consecutive_idle") or 0))
        print("  would run   : %s" % " ".join(engine_command()))
        return 0
    if "--run-forever" in argv:
        run_forever(runner=None)
        return 0

    # Credentials first: the child inherits this environment, and on the scheduler path
    # nothing else fills it in.
    load_shared_env()

    dry = "--dry-run" in argv
    forced = "--force" in argv

    def _once(note):
        r = tick(dry_run=dry, force=forced)
        code = r.get("exit_code")
        if code:
            note("engine dispatch exited %s (%s)"
                 % (code, _failure_kind(load_state().get("last_dispatch_tail"))))
        return 0

    # HEARTBEAT: every tick — idle, dispatching, or crashing — writes {name, ts, ok, err}.
    # This is what makes "the hot path has not run since Tuesday" a detectable statement
    # rather than something a human has to go and notice.
    return heartbeat.guard("mer-hotpath", _once, expect_seconds=300.0, window=None)


if __name__ == "__main__":
    sys.exit(main())
