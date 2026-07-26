#!/usr/bin/env python3
"""
scheduler.py — THE CLOCK. Installs the merchandise-return engine's jobs on whatever host
you are actually running on.

WHY THIS EXISTS
---------------
The engine is ~40 modules that watch a mailbox, classify replies, draft letters, track
deadlines and watch the statute of limitations. Every one of them is worth something only
when it runs while nobody is looking. Without a clock the package is a very good filing
cabinet: it can do everything, on demand, forever, and it will never once notice that a
deadline passed on a Tuesday.

The author's own instance ran off six hand-written shell wrappers bolted to one particular
container on one particular VPS: an absolute interpreter path, an absolute scripts path, a
shared secrets file at a fixed location, and a container-specific cron API. None of that
travels. This module is the portable replacement: one declarative manifest (schedule.json)
plus an installer that speaks whichever scheduler the host has.

    cron            Linux / macOS / anything with crontab
    systemd timers  modern Linux, user units, no root needed
    schtasks        Windows Task Scheduler
    --run-forever   a foreground loop, for a host with none of the above (containers)

DESIGN RULES, AND WHY
---------------------
1. **--dry-run is the DEFAULT.** Every mutating verb prints exactly what it would do and
   changes nothing until you add --live. A tool that edits your crontab because you typed
   its name wrong is a tool you cannot trust with your crontab.
2. **Idempotent.** cron entries live inside one delimited managed block; systemd units and
   Windows tasks use deterministic names. Installing five times leaves one copy of each job.
   Nothing outside the managed block is ever touched.
3. **Nothing host-specific in the manifest.** The manifest describes the WORK (which module,
   which schedule, which env). Config describes the MACHINE (interpreter, directories, an
   optional command prefix such as a container exec). Neither contains a person.
4. **Silence = healthy.** The jobs print to stdout only when a human must act; the full run
   detail goes to a bounded per-job log. The scheduler preserves that property, because a
   watchdog that speaks every week is one nobody reads. Its inverse matters as much and is
   also preserved: a sweep that could not RUN must never be mistaken for a clean sweep, so a
   job that dies without output says so.

CONFIG — where the host-specific values come from
-------------------------------------------------
Resolution order, last one wins: built-in default -> profile.json "scheduler" section ->
environment variable -> command-line flag.

    key           env var                  default
    ------------- ------------------------ ---------------------------------------------
    python        MER_SCHED_PYTHON         the interpreter running scheduler.py
    scripts_dir   MER_SCHED_SCRIPTS_DIR    the directory scheduler.py lives in
    log_dir       MER_SCHED_LOG_DIR        <scripts_dir>
    env_file      MER_SCHED_ENV_FILE       (none) — a shared KEY=VALUE secrets file, if you
                                           keep one. Jobs read only the keys they declare.
    prefix        MER_SCHED_PREFIX         (none) — a command prefix, e.g. a container exec
    tag           MER_SCHED_TAG            merchandise-return — namespaces the installed jobs

USAGE
-----
    python scheduler.py --status                 # what is installed, and did it run?
    python scheduler.py --install                # DRY RUN — prints the plan, changes nothing
    python scheduler.py --install --live         # actually installs
    python scheduler.py --uninstall --live       # removes exactly what it installed
    python scheduler.py --run-forever            # foreground clock, no host scheduler needed
    python scheduler.py --run mer-case-tick      # run one job now, exactly as the clock would
    python scheduler.py --list                   # the manifest, in a table
    python scheduler.py --selftest               # offline self-test (installs nothing)

Stdlib only, deliberately: this must work from a bare python3 on a box you have never seen.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

__all__ = [
    "SchedulerError", "Config", "Job", "Manifest", "load_manifest", "resolve_config",
    "CronExpr", "job_command", "run_job", "backend_for", "CronBackend", "SystemdBackend",
    "SchtasksBackend", "ForeverBackend", "MANIFEST_SEARCH_NAMES",
]

HERE = os.path.dirname(os.path.abspath(__file__))

# The manifest, in search order. `schedule.json.example` is LAST and is the shipped default —
# a complete working manifest, not a stub. It carries the .example suffix only because that is
# what the packager's file whitelist ships; see its own _readme.
MANIFEST_SEARCH_NAMES = ("schedule.json", "schedule.json.example")
MANIFEST_ENV_VAR = "MER_SCHEDULE"

# The markers that delimit our managed block in a crontab. Everything between them is ours to
# rewrite; everything outside is somebody else's and is copied through untouched.
BLOCK_BEGIN = "# >>> %s scheduler (managed block — do not edit by hand) >>>"
BLOCK_END = "# <<< %s scheduler <<<"

VALID_STDOUT_MODES = ("silent", "alert", "match", "summary")
VALID_STEP_OUTPUTS = ("log", "log+match", "alert")


class SchedulerError(Exception):
    """Anything the user did wrong, or anything about this host we refuse to guess at."""


# =================================================================================== config

_CONFIG_KEYS = ("python", "scripts_dir", "log_dir", "env_file", "prefix", "tag")

_CONFIG_ENV = {
    "python": "MER_SCHED_PYTHON",
    "scripts_dir": "MER_SCHED_SCRIPTS_DIR",
    "log_dir": "MER_SCHED_LOG_DIR",
    "env_file": "MER_SCHED_ENV_FILE",
    "prefix": "MER_SCHED_PREFIX",
    "tag": "MER_SCHED_TAG",
}

DEFAULT_TAG = "merchandise-return"


class Config(object):
    """Everything about THIS MACHINE. No job description belongs in here, and no identity."""

    def __init__(self, python=None, scripts_dir=None, log_dir=None,
                 env_file=None, prefix=None, tag=None):
        self.scripts_dir = os.path.abspath(scripts_dir or HERE)
        self.python = python or sys.executable or "python3"
        self.log_dir = os.path.abspath(log_dir or self.scripts_dir)
        self.env_file = env_file or None
        self.prefix = (prefix or "").strip()
        self.tag = (tag or DEFAULT_TAG).strip()

    def as_dict(self):
        return {k: getattr(self, k) for k in _CONFIG_KEYS}

    def log_path(self, name):
        return os.path.join(self.log_dir, name)

    def module_path(self, module):
        return os.path.join(self.scripts_dir, module)

    def __repr__(self):
        return "<Config python=%s scripts_dir=%s>" % (self.python, self.scripts_dir)


def _profile_scheduler_section():
    """The optional "scheduler" object inside the user's profile.json.

    Read through mer_config when it is importable AND a profile exists. A missing or
    unconfigured profile is NOT an error here: installing a clock must not require the
    identity layer to be set up first, because the natural order of operations is
    'install the engine, install the clock, then onboard'.
    """
    try:
        import mer_config
    except Exception:
        return {}
    try:
        sect = mer_config.profile().get("scheduler")
    except Exception:
        return {}
    return sect if isinstance(sect, dict) else {}


def resolve_config(overrides=None, env=None, profile_section=None):
    """Built-in default -> profile.json["scheduler"] -> environment -> explicit override."""
    env = os.environ if env is None else env
    values = {}
    sect = _profile_scheduler_section() if profile_section is None else profile_section
    for key in _CONFIG_KEYS:
        v = sect.get(key)
        if v:
            values[key] = str(v)
    for key in _CONFIG_KEYS:
        v = env.get(_CONFIG_ENV[key])
        if v:
            values[key] = v
    for key, v in (overrides or {}).items():
        if v:
            values[key] = v
    return Config(**values)


# ================================================================================= manifest

class Job(object):
    """One scheduled unit of work: a name, a cron expression, and an ordered list of steps."""

    def __init__(self, data, index=0):
        self.raw = dict(data or {})
        self.name = str(self.raw.get("name") or "").strip()
        if not self.name:
            raise SchedulerError("job #%d in the manifest has no 'name'." % index)
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", self.name):
            raise SchedulerError(
                "job name %r is not usable as a cron comment / unit / task name.\n"
                "  Use letters, digits, '.', '_' and '-' only." % self.name)
        sched = self.raw.get("schedule")
        if not sched:
            raise SchedulerError("job %r has no 'schedule'." % self.name)
        self.schedule = str(sched).strip()
        self.cron = CronExpr(self.schedule, where="job %r" % self.name)
        self.description = str(self.raw.get("description") or "").strip()
        self.schedule_human = str(self.raw.get("schedule_human") or "").strip()

        self.steps = []
        for i, st in enumerate(self.raw.get("steps") or []):
            if not isinstance(st, dict) or not st.get("module"):
                raise SchedulerError("job %r step #%d has no 'module'." % (self.name, i))
            out = str(st.get("output") or "log")
            if out not in VALID_STEP_OUTPUTS:
                raise SchedulerError(
                    "job %r step %r has output=%r; expected one of %s."
                    % (self.name, st["module"], out, ", ".join(VALID_STEP_OUTPUTS)))
            self.steps.append({
                "module": str(st["module"]),
                "args": [str(a) for a in (st.get("args") or [])],
                "output": out,
            })
        if not self.steps:
            raise SchedulerError("job %r has no 'steps' — it would do nothing." % self.name)

        self.env = {str(k): str(v) for k, v in (self.raw.get("env") or {}).items()}
        self.env_from_file = [str(k) for k in (self.raw.get("env_from_file") or [])]
        self.log = str(self.raw.get("log") or ("%s.log" % self.name))

        so = self.raw.get("stdout") or {"mode": "silent"}
        mode = str(so.get("mode") or "silent")
        if mode not in VALID_STDOUT_MODES:
            raise SchedulerError(
                "job %r has stdout.mode=%r; expected one of %s."
                % (self.name, mode, ", ".join(VALID_STDOUT_MODES)))
        if mode == "match" and not so.get("match"):
            raise SchedulerError("job %r uses stdout.mode='match' but sets no 'match' regex."
                                 % self.name)
        if mode == "summary" and not so.get("lines"):
            raise SchedulerError("job %r uses stdout.mode='summary' but sets no 'lines'."
                                 % self.name)
        self.stdout = so

    def __repr__(self):
        return "<Job %s %r>" % (self.name, self.schedule)


class Manifest(object):
    def __init__(self, data, source):
        self.source = source
        if not isinstance(data, dict):
            raise SchedulerError("manifest at %s must be a JSON object." % source)
        self.version = data.get("version", 1)
        self.defaults = data.get("defaults") or {}
        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise SchedulerError("manifest at %s has no 'jobs' list." % source)
        self.jobs = [Job(j, i) for i, j in enumerate(raw_jobs)]
        names = [j.name for j in self.jobs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise SchedulerError(
                "manifest at %s defines the same job name twice: %s.\n"
                "  Job names are the installer's identity for a job — duplicates would make\n"
                "  install/uninstall ambiguous." % (source, ", ".join(dupes)))
        try:
            self.log_max_lines = int(self.defaults.get("log_max_lines") or 500)
        except (TypeError, ValueError):
            raise SchedulerError("manifest defaults.log_max_lines must be an integer.")

    def get(self, name):
        for j in self.jobs:
            if j.name == name:
                return j
        raise SchedulerError(
            "no job named %r in %s.\n  Known jobs: %s"
            % (name, self.source, ", ".join(j.name for j in self.jobs)))


def manifest_search_path(explicit=None, cfg=None, env=None):
    env = os.environ if env is None else env
    out = []
    if explicit:
        out.append(str(explicit))
    if env.get(MANIFEST_ENV_VAR):
        out.append(env[MANIFEST_ENV_VAR])
    out.append(os.path.join(os.getcwd(), MANIFEST_SEARCH_NAMES[0]))
    base = cfg.scripts_dir if cfg else HERE
    for n in MANIFEST_SEARCH_NAMES:
        out.append(os.path.join(base, n))
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def load_manifest(explicit=None, cfg=None, env=None):
    candidates = manifest_search_path(explicit, cfg, env)
    if explicit and not os.path.isfile(str(explicit)):
        raise SchedulerError("manifest not found at the path you gave: %s" % explicit)
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                raise SchedulerError("manifest at %s is not valid JSON: %s" % (path, e))
            except OSError as e:
                raise SchedulerError("cannot read manifest at %s: %s" % (path, e))
            return Manifest(data, path)
    raise SchedulerError(
        "no job manifest found.\n  Searched, in order:\n    %s\n"
        "  The package ships one (schedule.json.example) next to scheduler.py; if it is gone,\n"
        "  reinstall the engine or point $%s at a manifest."
        % ("\n    ".join(candidates), MANIFEST_ENV_VAR))


# ============================================================================ cron expression

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec")


class CronExpr(object):
    """A 5-field cron expression, expanded to explicit integer sets.

    Supports `*`, `a`, `a-b`, `a-b/n`, `*/n` and comma lists, plus three-letter month and
    day names — i.e. everything the shipped manifest uses and everything a user is likely
    to write. Deliberately does NOT support the nonstandard extensions (@reboot, L, W, #):
    they do not map cleanly onto systemd or Task Scheduler, and silently dropping one would
    give the user a job that never fires.
    """

    def __init__(self, expr, where=""):
        self.expr = " ".join(str(expr).split())
        parts = self.expr.split(" ")
        if len(parts) != 5:
            raise SchedulerError(
                "%s: %r is not a 5-field cron expression (minute hour dom month dow); got %d "
                "field(s)." % (where or "schedule", expr, len(parts)))
        self.fields = []
        self.stars = []
        for i, raw in enumerate(parts):
            self.stars.append(raw == "*")
            self.fields.append(self._expand(raw, i, where))
        # cron folds 7 onto Sunday
        if 7 in self.fields[4]:
            self.fields[4] = {0} | (self.fields[4] - {7})

    @staticmethod
    def _named(tok, index):
        low = tok.lower()
        if index == 3 and low in _MONTH_NAMES:
            return str(_MONTH_NAMES.index(low) + 1)
        if index == 4 and low in _DOW_NAMES:
            return str(_DOW_NAMES.index(low))
        return tok

    def _expand(self, raw, index, where):
        lo, hi = _FIELD_RANGES[index]
        out = set()
        for token in raw.split(","):
            token = token.strip()
            if not token:
                raise SchedulerError("%s: empty item in the %s field of %r"
                                     % (where, _FIELD_NAMES[index], self.expr))
            step = 1
            if "/" in token:
                token, _, s = token.partition("/")
                try:
                    step = int(s)
                except ValueError:
                    raise SchedulerError("%s: bad step %r in the %s field of %r"
                                         % (where, s, _FIELD_NAMES[index], self.expr))
                if step < 1:
                    raise SchedulerError("%s: step must be >= 1 in %r" % (where, self.expr))
            if token == "*":
                start, end = lo, hi
            elif "-" in token[1:]:
                a, _, b = token.partition("-")
                start = self._to_int(self._named(a, index), index, where)
                end = self._to_int(self._named(b, index), index, where)
                if end < start:
                    raise SchedulerError("%s: reversed range %r in the %s field of %r"
                                         % (where, token, _FIELD_NAMES[index], self.expr))
            else:
                start = end = self._to_int(self._named(token, index), index, where)
            for v in range(start, end + 1, step):
                out.add(v)
        if not out:
            raise SchedulerError("%s: the %s field of %r matches nothing"
                                 % (where, _FIELD_NAMES[index], self.expr))
        return out

    def _to_int(self, tok, index, where):
        lo, hi = _FIELD_RANGES[index]
        try:
            v = int(tok)
        except ValueError:
            raise SchedulerError(
                "%s: %r is not a number in the %s field of %r"
                % (where, tok, _FIELD_NAMES[index], self.expr))
        if not (lo <= v <= hi):
            raise SchedulerError(
                "%s: %d is outside %d-%d in the %s field of %r"
                % (where, v, lo, hi, _FIELD_NAMES[index], self.expr))
        return v

    # -- matching -----------------------------------------------------------------
    def matches(self, when):
        """True when `when` (a naive local datetime) is a firing minute.

        Implements the classic dom/dow rule: when BOTH day fields are restricted, a day
        matches if EITHER does. Getting this wrong is the single most common way a
        hand-rolled cron parser fires on the wrong days.
        """
        minute, hour = self.fields[0], self.fields[1]
        dom, month, dow = self.fields[2], self.fields[3], self.fields[4]
        if when.minute not in minute or when.hour not in hour:
            return False
        if when.month not in month:
            return False
        w = (when.weekday() + 1) % 7          # python Mon=0 -> cron Sun=0
        dom_star, dow_star = self.stars[2], self.stars[4]
        if dom_star and dow_star:
            return True
        if dom_star:
            return w in dow
        if dow_star:
            return when.day in dom
        return (when.day in dom) or (w in dow)

    # -- rendering ----------------------------------------------------------------
    def occurrences_per_day(self):
        return len(self.fields[0]) * len(self.fields[1])

    def times(self):
        """Sorted (hour, minute) pairs this expression fires at."""
        return sorted((h, m) for h in self.fields[1] for m in self.fields[0])

    def weekdays(self):
        """The cron day-of-week set, or None when unrestricted."""
        return None if self.stars[4] else sorted(self.fields[4])

    def to_systemd(self):
        """An OnCalendar= value equivalent to this expression."""
        if not self.stars[2]:
            dom = ",".join("%02d" % d for d in sorted(self.fields[2]))
        else:
            dom = "*"
        month = "*" if self.stars[3] else ",".join("%02d" % d for d in sorted(self.fields[3]))
        hour = ",".join("%02d" % h for h in sorted(self.fields[1]))
        minute = ",".join("%02d" % m for m in sorted(self.fields[0]))
        dows = self.weekdays()
        prefix = ""
        if dows is not None:
            names = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
            prefix = ",".join(names[d] for d in dows) + " "
        return "%s*-%s-%s %s:%s:00" % (prefix, month, dom, hour, minute)

    def __repr__(self):
        return "<CronExpr %r>" % self.expr


# ================================================================== the runner (one job, now)

def job_command(job, cfg, quote=True):
    """The exact command a scheduler entry invokes: run THIS job through THIS scheduler.

    Every scheduler entry is `<python> <scripts_dir>/scheduler.py --run <job>`. The jobs are
    NOT expanded into the crontab, on purpose: the manifest stays the single source of truth,
    so editing a schedule's ENV or steps does not require re-installing the clock, and a
    crontab line can never drift from the manifest that generated it.
    """
    self_path = os.path.join(cfg.scripts_dir, os.path.basename(os.path.abspath(__file__)))
    parts = [cfg.python, self_path, "--run", job.name]
    if quote:
        parts = [_q(p) for p in parts]
    cmd = " ".join(parts) if quote else parts
    if quote and cfg.prefix:
        cmd = "%s %s" % (cfg.prefix, cmd)
    return cmd


def _q(s):
    s = str(s)
    if not s or re.search(r"[\s\"'$`\\]", s):
        return '"%s"' % s.replace('"', '\\"')
    return s


def load_env_file(path, keys):
    """Pull ONLY `keys` out of a KEY=VALUE file.

    Line-by-line and key-by-key rather than sourcing the file, because a shared secrets file
    in the wild contains prose, comments and half-finished lines, and sourcing one of those
    takes the whole job down. First occurrence of a key wins; everything else is ignored.
    """
    out = {}
    if not path or not os.path.isfile(path):
        return out
    wanted = set(keys or [])
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k in wanted and k not in out:
                    out[k] = v.strip().strip('"').strip("'")
    except OSError:
        return out
    return out


def _trim_log(path, max_lines):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= max_lines:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-max_lines:])
    except OSError:
        pass


def _stamp():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def run_job(job, cfg, manifest=None, out=None, dry_run=False, timeout=1800):
    """Run one job end to end, exactly as the installed clock entry does.

    Returns the exit code. Preserves the two properties the hand-written wrappers had:
    the full detail goes to a bounded log, and stdout carries ONLY what a human must act on.
    """
    out = sys.stdout if out is None else out
    max_lines = manifest.log_max_lines if manifest else 500
    log_path = cfg.log_path(job.log)

    env = dict(os.environ)
    env.update(load_env_file(cfg.env_file, job.env_from_file))
    env.update(job.env)
    env["PYTHONIOENCODING"] = "utf-8"

    if dry_run:
        out.write("DRY RUN — job %s would run:\n" % job.name)
        for st in job.steps:
            out.write("    %s %s %s\n" % (_q(cfg.python), _q(cfg.module_path(st["module"])),
                                          " ".join(_q(a) for a in st["args"])))
        out.write("    log      : %s (bounded to %d lines)\n" % (log_path, max_lines))
        out.write("    env      : %s\n" % (", ".join("%s=%s" % kv for kv in sorted(job.env.items()))
                                           or "(none)"))
        if job.env_from_file:
            out.write("    env file : %s -> %s\n"
                      % (cfg.env_file or "(not configured)", ", ".join(job.env_from_file)))
        return 0

    log_chunks = ["===== %s %s =====" % (job.name, _stamp())]
    alert_parts, matchable, worst_rc = [], [], 0

    for st in job.steps:
        path = cfg.module_path(st["module"])
        if not os.path.isfile(path):
            log_chunks.append("[scheduler] MISSING MODULE %s" % path)
            worst_rc = max(worst_rc, 2)
            continue
        try:
            proc = subprocess.run([cfg.python, path] + st["args"],
                                  cwd=cfg.scripts_dir, env=env, timeout=timeout,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rc = proc.returncode
            so = proc.stdout.decode("utf-8", "replace")
            se = proc.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            rc, so, se = 124, "", "[scheduler] TIMEOUT after %ds" % timeout
        except OSError as e:
            rc, so, se = 2, "", "[scheduler] could not launch: %s" % e
        worst_rc = max(worst_rc, rc)
        log_chunks.append("--- %s (rc=%d)" % (st["module"], rc))
        if st["output"] == "alert":
            if so.strip():
                alert_parts.append(so.rstrip())
            if se.strip():
                log_chunks.append(se.rstrip())
        else:
            if so.strip():
                log_chunks.append(so.rstrip())
                matchable.append(so)
            if se.strip():
                log_chunks.append(se.rstrip())
                matchable.append(se)

    try:
        os.makedirs(cfg.log_dir, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(log_chunks) + "\n")
        _trim_log(log_path, max_lines)
    except OSError as e:
        out.write("scheduler: could not write %s: %s\n" % (log_path, e))

    _emit(job, out, "\n".join(alert_parts).strip(), "\n".join(matchable), worst_rc, log_path)
    return worst_rc


def _emit(job, out, alert, matchable, rc, log_path):
    """stdout policy. Silence means 'checked and fine' — never 'never checked'."""
    spec = job.stdout
    mode = spec.get("mode", "silent")

    if mode == "silent":
        return

    if mode == "alert":
        if alert:
            _line(out, spec.get("header"))
            out.write(alert + "\n")
            _line(out, spec.get("footer"), blank_before=True)
            return
        threshold = spec.get("fail_rc_above")
        if threshold is not None and rc > int(threshold):
            msg = str(spec.get("fail_message") or "%s exited {rc} with no output." % job.name)
            out.write("WARNING: " + msg.replace("{rc}", str(rc)) + "\n")
            out.write("   last log: %s\n" % log_path)
        return

    if mode == "match":
        rx = re.compile(spec["match"])
        hits = [ln for ln in matchable.splitlines() if rx.search(ln)]
        if hits:
            _line(out, spec.get("header"))
            out.write("\n".join(hits) + "\n")
            _line(out, spec.get("footer"), blank_before=True)
        return

    if mode == "summary":
        out.write("%s %s\n" % (job.name, _stamp()))
        for rule in spec.get("lines") or []:
            rx = re.compile(rule.get("match", ".^"))
            for ln in matchable.splitlines():
                if rx.search(ln):
                    out.write("%s%s\n" % (rule.get("prefix", "  "), ln.strip()))


def _line(out, text, blank_before=False):
    if not text:
        return
    if blank_before:
        out.write("\n")
    out.write(str(text) + "\n")


# ==================================================================================== backends

class Backend(object):
    name = "base"

    def available(self):
        return False

    def plan_install(self, jobs, cfg):
        """-> list of (human description, payload). Payload is backend-private."""
        raise NotImplementedError

    def apply_install(self, plan, cfg):
        raise NotImplementedError

    def plan_uninstall(self, jobs, cfg):
        raise NotImplementedError

    def apply_uninstall(self, plan, cfg):
        raise NotImplementedError

    def installed(self, jobs, cfg):
        """-> set of job names this backend currently has registered."""
        return set()


def _which(prog):
    from shutil import which
    return which(prog)


def _run(argv, stdin=None):
    try:
        p = subprocess.run(argv, input=(stdin.encode() if stdin is not None else None),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


# ------------------------------------------------------------------------------------- cron

class CronBackend(Backend):
    """crontab(1). All our entries live inside ONE delimited block.

    Rewriting a whole managed block is what makes install idempotent AND safe: the block is
    regenerated from the manifest every time (so it cannot drift), and every line outside it
    is copied through byte for byte (so we cannot eat somebody's backup job).

    read/write are injectable so the self-test can exercise the real block logic without ever
    touching a real crontab.
    """
    name = "cron"

    def __init__(self, read=None, write=None):
        self._read = read
        self._write = write

    def available(self):
        if self._read is not None:
            return True
        return os.name != "nt" and bool(_which("crontab"))

    def read_crontab(self):
        if self._read is not None:
            return self._read()
        rc, so, _ = _run(["crontab", "-l"])
        return so if rc == 0 else ""

    def write_crontab(self, text):
        if self._write is not None:
            self._write(text)
            return 0
        rc, _, se = _run(["crontab", "-"], stdin=text)
        if rc != 0:
            raise SchedulerError("crontab refused the new table: %s" % se.strip())
        return rc

    def _markers(self, cfg):
        return BLOCK_BEGIN % cfg.tag, BLOCK_END % cfg.tag

    def strip_block(self, text, cfg):
        begin, end = self._markers(cfg)
        out, skipping = [], False
        for line in text.splitlines():
            if line.strip() == begin:
                skipping = True
                continue
            if line.strip() == end:
                skipping = False
                continue
            if not skipping:
                out.append(line)
        while out and not out[-1].strip():
            out.pop()
        return "\n".join(out)

    def render_block(self, jobs, cfg):
        begin, end = self._markers(cfg)
        lines = [begin,
                 "# generated by scheduler.py from the job manifest — re-run --install to update",
                 "# times are in this host's local time zone"]
        for job in jobs:
            if job.description:
                lines.append("# %s: %s" % (job.name, job.description))
            lines.append("%s %s" % (job.cron.expr, job_command(job, cfg)))
        lines.append(end)
        return "\n".join(lines)

    def _compose(self, jobs, cfg):
        base = self.strip_block(self.read_crontab(), cfg)
        block = self.render_block(jobs, cfg)
        return (base + "\n\n" + block + "\n") if base.strip() else (block + "\n")

    def plan_install(self, jobs, cfg):
        new = self._compose(jobs, cfg)
        current = self.read_crontab()
        desc = ["crontab: managed block for %d job(s)" % len(jobs)]
        for job in jobs:
            desc.append("    %-22s %s" % (job.cron.expr, job.name))
        if current == new:
            desc.append("    (already installed and identical — no change)")
        return desc, new

    def apply_install(self, payload, cfg):
        self.write_crontab(payload)

    def plan_uninstall(self, jobs, cfg):
        base = self.strip_block(self.read_crontab(), cfg)
        text = (base + "\n") if base.strip() else ""
        return ["crontab: remove the managed block (%s)" % cfg.tag], text

    def apply_uninstall(self, payload, cfg):
        self.write_crontab(payload)

    def installed(self, jobs, cfg):
        begin, end = self._markers(cfg)
        text = self.read_crontab()
        inside, found = False, set()
        for line in text.splitlines():
            if line.strip() == begin:
                inside = True
                continue
            if line.strip() == end:
                inside = False
                continue
            if inside:
                for job in jobs:
                    if ("--run %s" % job.name) in line or ('--run "%s"' % job.name) in line:
                        found.add(job.name)
        return found


# ---------------------------------------------------------------------------------- systemd

class SystemdBackend(Backend):
    """systemd --user timers. One .service + one .timer per job, deterministic names.

    User units, not system units: installing a clock must not need root, and a per-user timer
    dies with the user's session config rather than lingering as an orphaned root job.
    Persistent=true so a laptop that was asleep at 09:00 still runs the tick when it wakes —
    a missed deadline sweep is the one thing this whole engine exists to prevent.
    """
    name = "systemd"

    def __init__(self, unit_dir=None, runner=None):
        self._unit_dir = unit_dir
        self._runner = runner or _run

    def unit_dir(self):
        if self._unit_dir:
            return self._unit_dir
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
        return os.path.join(base, "systemd", "user")

    def available(self):
        if self._unit_dir is not None:
            return True
        if os.name == "nt" or not _which("systemctl"):
            return False
        rc, _, _ = self._runner(["systemctl", "--user", "show-environment"])
        return rc == 0

    def unit_name(self, job, cfg, ext):
        return "%s-%s.%s" % (cfg.tag, job.name, ext)

    def render_service(self, job, cfg):
        return "\n".join([
            "[Unit]",
            "Description=%s" % (job.description or job.name),
            "",
            "[Service]",
            "Type=oneshot",
            "WorkingDirectory=%s" % cfg.scripts_dir,
            "ExecStart=%s" % job_command(job, cfg),
            "",
        ])

    def render_timer(self, job, cfg):
        return "\n".join([
            "[Unit]",
            "Description=timer for %s" % (job.description or job.name),
            "",
            "[Timer]",
            "OnCalendar=%s" % job.cron.to_systemd(),
            "Persistent=true",
            "AccuracySec=1min",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])

    def plan_install(self, jobs, cfg):
        d = self.unit_dir()
        files, desc = [], ["systemd --user units in %s" % d]
        for job in jobs:
            files.append((os.path.join(d, self.unit_name(job, cfg, "service")),
                          self.render_service(job, cfg)))
            files.append((os.path.join(d, self.unit_name(job, cfg, "timer")),
                          self.render_timer(job, cfg)))
            desc.append("    %-34s OnCalendar=%s"
                        % (self.unit_name(job, cfg, "timer"), job.cron.to_systemd()))
        return desc, {"dir": d, "files": files,
                      "timers": [self.unit_name(j, cfg, "timer") for j in jobs]}

    def apply_install(self, payload, cfg):
        os.makedirs(payload["dir"], exist_ok=True)
        for path, text in payload["files"]:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        self._runner(["systemctl", "--user", "daemon-reload"])
        for t in payload["timers"]:
            self._runner(["systemctl", "--user", "enable", "--now", t])

    def plan_uninstall(self, jobs, cfg):
        d = self.unit_dir()
        paths, timers, desc = [], [], ["systemd --user units removed from %s" % d]
        for job in jobs:
            for ext in ("service", "timer"):
                p = os.path.join(d, self.unit_name(job, cfg, ext))
                paths.append(p)
                if ext == "timer":
                    timers.append(self.unit_name(job, cfg, ext))
            desc.append("    %s" % self.unit_name(job, cfg, "timer"))
        return desc, {"paths": paths, "timers": timers}

    def apply_uninstall(self, payload, cfg):
        for t in payload["timers"]:
            self._runner(["systemctl", "--user", "disable", "--now", t])
        for p in payload["paths"]:
            try:
                os.remove(p)
            except OSError:
                pass
        self._runner(["systemctl", "--user", "daemon-reload"])

    def installed(self, jobs, cfg):
        d = self.unit_dir()
        return {j.name for j in jobs
                if os.path.isfile(os.path.join(d, self.unit_name(j, cfg, "timer")))}


# --------------------------------------------------------------------------------- schtasks

class SchtasksBackend(Backend):
    """Windows Task Scheduler via schtasks.exe.

    schtasks has no cron expression, so each job is expanded into one task per firing TIME:
    `\\<tag>\\<job>-0800`, `-0900`, ... Explicit tasks are what make this idempotent — /F
    overwrites a task of the same name in place, so re-installing cannot accumulate copies,
    and uninstall deletes exactly the names it would create.

    A job that fires more than MAX_TASKS times a day is REFUSED rather than approximated.
    An installer that silently gives you a coarser schedule than you asked for is worse than
    one that tells you it cannot do it.
    """
    name = "schtasks"
    MAX_TASKS = 24
    _DOW = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")

    def __init__(self, runner=None):
        self._runner = runner or _run

    def available(self):
        return os.name == "nt"

    def folder(self, cfg):
        return "\\%s" % cfg.tag

    def task_names(self, job, cfg):
        return ["%s\\%s-%02d%02d" % (self.folder(cfg), job.name, h, m)
                for h, m in job.cron.times()]

    def plan_install(self, jobs, cfg):
        desc, actions = ["Windows Task Scheduler tasks under %s" % self.folder(cfg)], []
        for job in jobs:
            n = job.cron.occurrences_per_day()
            if n > self.MAX_TASKS:
                raise SchedulerError(
                    "job %r fires %d times a day; the Windows backend registers one task per\n"
                    "  firing time and refuses above %d. Either coarsen the schedule, or run\n"
                    "  the clock with --run-forever, which handles any cron expression."
                    % (job.name, n, self.MAX_TASKS))
            if not job.cron.stars[2] or not job.cron.stars[3]:
                raise SchedulerError(
                    "job %r restricts day-of-month or month; the Windows backend supports\n"
                    "  DAILY and WEEKLY schedules only. Use --run-forever for this job."
                    % job.name)
            dows = job.cron.weekdays()
            for (h, m), tn in zip(job.cron.times(), self.task_names(job, cfg)):
                argv = ["schtasks", "/Create", "/F", "/TN", tn,
                        "/TR", job_command(job, cfg),
                        "/ST", "%02d:%02d" % (h, m)]
                if dows is None:
                    argv += ["/SC", "DAILY"]
                else:
                    argv += ["/SC", "WEEKLY", "/D", ",".join(self._DOW[d] for d in dows)]
                actions.append(argv)
                desc.append("    %-40s %s %02d:%02d"
                            % (tn, "DAILY" if dows is None else
                               ",".join(self._DOW[d] for d in dows), h, m))
        return desc, actions

    def apply_install(self, actions, cfg):
        for argv in actions:
            rc, _, se = self._runner(argv)
            if rc != 0:
                raise SchedulerError("schtasks failed for %s: %s"
                                     % (argv[argv.index("/TN") + 1], se.strip()))

    def plan_uninstall(self, jobs, cfg):
        desc, actions = ["Windows tasks removed from %s" % self.folder(cfg)], []
        for job in jobs:
            for tn in self.task_names(job, cfg):
                actions.append(["schtasks", "/Delete", "/F", "/TN", tn])
                desc.append("    %s" % tn)
        return desc, actions

    def apply_uninstall(self, actions, cfg):
        for argv in actions:
            self._runner(argv)          # a task that is already gone is not an error

    def installed(self, jobs, cfg):
        rc, so, _ = self._runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        if rc != 0:
            return set()
        return {j.name for j in jobs
                if any(tn.lower() in so.lower() for tn in self.task_names(j, cfg))}


# ---------------------------------------------------------------------------------- forever

class ForeverBackend(Backend):
    """No host scheduler at all — scheduler.py IS the clock, in the foreground.

    For a container, a stripped VM, or anyone who would rather see the clock running than
    trust an invisible one. Ticks once a minute on the minute; a job fires at most once per
    minute even if the loop is late.
    """
    name = "forever"

    def available(self):
        return True

    def plan_install(self, jobs, cfg):
        return (["foreground loop — nothing is registered with the host",
                 "    run: %s --run-forever" % job_command_self(cfg)], None)

    def apply_install(self, payload, cfg):
        raise SchedulerError(
            "the 'forever' backend installs nothing — it IS the clock.\n"
            "  Start it with:  %s --run-forever\n"
            "  Keep it alive with whatever supervisor you have (a service, tmux, or "
            "restart-always in your container)." % job_command_self(cfg))

    def plan_uninstall(self, jobs, cfg):
        return (["foreground loop — stop the process; nothing is registered"], None)

    def apply_uninstall(self, payload, cfg):
        pass


def job_command_self(cfg):
    self_path = os.path.join(cfg.scripts_dir, os.path.basename(os.path.abspath(__file__)))
    return "%s %s" % (_q(cfg.python), _q(self_path))


BACKENDS = {
    "cron": CronBackend,
    "systemd": SystemdBackend,
    "schtasks": SchtasksBackend,
    "forever": ForeverBackend,
}


def backend_for(name="auto"):
    """Pick a backend. 'auto' prefers what the host most likely already relies on.

    Windows -> schtasks (the only real option). Elsewhere cron before systemd: cron is
    universal, needs no session bus, and survives in containers where `systemctl --user`
    does not exist. systemd is the fallback for the modern desktop with no crontab binary.
    """
    if name and name != "auto":
        if name not in BACKENDS:
            raise SchedulerError("unknown backend %r; expected one of: auto, %s"
                                 % (name, ", ".join(sorted(BACKENDS))))
        return BACKENDS[name]()
    if os.name == "nt":
        return SchtasksBackend()
    for cls in (CronBackend, SystemdBackend):
        b = cls()
        if b.available():
            return b
    return ForeverBackend()


# ==================================================================================== forever

def run_forever(manifest, cfg, out=None, now_fn=None, sleep_fn=None, max_ticks=None):
    """The fallback clock. Returns the number of jobs it ran (used by the self-test)."""
    out = sys.stdout if out is None else out
    now_fn = now_fn or datetime.datetime.now
    sleep_fn = sleep_fn or time.sleep
    out.write("clock running — %d job(s) from %s\n" % (len(manifest.jobs), manifest.source))
    for job in manifest.jobs:
        out.write("    %-22s %s\n" % (job.cron.expr, job.name))
    out.write("Ctrl-C to stop. Nothing was registered with the host.\n")
    ran, ticks, last = 0, 0, None
    while max_ticks is None or ticks < max_ticks:
        now = now_fn().replace(second=0, microsecond=0)
        if now != last:
            last = now
            for job in manifest.jobs:
                if job.cron.matches(now):
                    out.write("[%s] %s\n" % (now.strftime("%Y-%m-%d %H:%M"), job.name))
                    try:
                        run_job(job, cfg, manifest, out=out)
                    except Exception as e:                       # a job must not kill the clock
                        out.write("scheduler: job %s raised %s\n" % (job.name, e))
                    ran += 1
        ticks += 1
        if max_ticks is None:
            sleep_fn(20)
        else:
            sleep_fn(0)
    return ran


# ====================================================================================== CLI

BANNER = "merchandise-return :: scheduler"


def cmd_list(manifest, cfg, out):
    out.write("%s\n" % BANNER)
    out.write("manifest : %s\n" % manifest.source)
    out.write("%-22s  %-16s  %s\n" % ("JOB", "SCHEDULE", "WHEN (local)"))
    out.write("-" * 78 + "\n")
    for job in manifest.jobs:
        out.write("%-22s  %-16s  %s\n" % (job.name, job.cron.expr, job.schedule_human))
        if job.description:
            out.write("%-22s  %s\n" % ("", job.description))
    return 0


def cmd_status(manifest, cfg, backend, out):
    out.write("%s\n" % BANNER)
    out.write("manifest : %s\n" % manifest.source)
    out.write("backend  : %s%s\n" % (backend.name,
                                     "" if backend.available() else "  (NOT AVAILABLE on this host)"))
    for k, v in sorted(cfg.as_dict().items()):
        out.write("%-11s: %s\n" % (k, v if v else "(not set)"))
    out.write("\n%-22s  %-10s  %s\n" % ("JOB", "INSTALLED", "LAST LOG WRITE"))
    out.write("-" * 78 + "\n")
    try:
        have = backend.installed(manifest.jobs, cfg)
    except Exception:
        have = set()
    for job in manifest.jobs:
        p = cfg.log_path(job.log)
        if os.path.isfile(p):
            when = datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
        else:
            when = "(never run)"
        out.write("%-22s  %-10s  %s\n" % (job.name, "yes" if job.name in have else "no", when))
    if not have:
        out.write("\nNo jobs are installed. The engine has no clock — run:\n")
        out.write("    %s --install --live\n" % job_command_self(cfg))
    return 0


def cmd_install(manifest, cfg, backend, out, live, jobs):
    if not backend.available():
        raise SchedulerError(
            "backend %r is not available on this host.\n"
            "  Try --backend forever (a foreground clock that needs nothing), or name a\n"
            "  different backend with --backend cron|systemd|schtasks." % backend.name)
    desc, payload = backend.plan_install(jobs, cfg)
    out.write("%s — install (%s)\n" % (BANNER, "LIVE" if live else "DRY RUN"))
    for line in desc:
        out.write("%s\n" % line)
    if live:
        backend.apply_install(payload, cfg)
        out.write("\ninstalled. %d job(s) now have a clock.\n" % len(jobs))
    else:
        out.write("\nDRY RUN — nothing was written. Re-run with --live to apply.\n")
    return 0


def cmd_uninstall(manifest, cfg, backend, out, live, jobs):
    desc, payload = backend.plan_uninstall(jobs, cfg)
    out.write("%s — uninstall (%s)\n" % (BANNER, "LIVE" if live else "DRY RUN"))
    for line in desc:
        out.write("%s\n" % line)
    if live:
        backend.apply_uninstall(payload, cfg)
        out.write("\nremoved.\n")
    else:
        out.write("\nDRY RUN — nothing was removed. Re-run with --live to apply.\n")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="scheduler.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Install the merchandise-return engine's jobs on this host.\n"
                    "The engine only earns its keep when it runs while nobody is watching;\n"
                    "this is what makes that happen.",
        epilog="""examples
  python scheduler.py --status                what is installed, and did it last run?
  python scheduler.py --install               DRY RUN: print the plan, change nothing
  python scheduler.py --install --live        actually install (cron/systemd/schtasks)
  python scheduler.py --install --live --backend forever
                                              print how to run the foreground clock
  python scheduler.py --run-forever           BE the clock, in this terminal
  python scheduler.py --uninstall --live      remove exactly what was installed
  python scheduler.py --run mer-case-tick     run one job now, as the clock would
  python scheduler.py --run mer-engine --dry-run
                                              show what that job would execute

--dry-run is the DEFAULT for --install and --uninstall. Nothing touches your crontab,
your systemd units or your Task Scheduler until you type --live.

config (host-specific values; flag beats env beats profile.json["scheduler"]):
  --python       $MER_SCHED_PYTHON        interpreter that runs the engine modules
  --scripts-dir  $MER_SCHED_SCRIPTS_DIR   where the engine modules live
  --log-dir      $MER_SCHED_LOG_DIR       where per-job logs are written
  --env-file     $MER_SCHED_ENV_FILE      shared KEY=VALUE file; jobs read only their own keys
  --prefix       $MER_SCHED_PREFIX        command prefix (e.g. a container exec)
  --tag          $MER_SCHED_TAG           namespace for installed entries
""")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="install every job on this host")
    g.add_argument("--uninstall", action="store_true", help="remove the jobs this tool installed")
    g.add_argument("--status", action="store_true", help="show what is installed and when it last ran")
    g.add_argument("--list", action="store_true", help="print the manifest as a table")
    g.add_argument("--run", metavar="JOB", help="run one job now, exactly as the clock would")
    g.add_argument("--run-forever", action="store_true",
                   help="be the clock in the foreground (no host scheduler needed)")
    g.add_argument("--selftest", action="store_true", help="offline self-test; installs nothing")

    p.add_argument("--live", action="store_true",
                   help="actually apply changes (default is a dry run)")
    p.add_argument("--dry-run", action="store_true",
                   help="explicit dry run (this is already the default)")
    p.add_argument("--backend", default="auto",
                   help="auto (default) | cron | systemd | schtasks | forever")
    p.add_argument("--manifest", help="path to a job manifest (default: schedule.json beside this file)")
    p.add_argument("--job", action="append", default=[],
                   help="limit --install/--uninstall to this job (repeatable)")
    p.add_argument("--python", help="interpreter used to run engine modules")
    p.add_argument("--scripts-dir", help="directory holding the engine modules")
    p.add_argument("--log-dir", help="directory for per-job logs")
    p.add_argument("--env-file", help="shared KEY=VALUE file jobs draw their declared keys from")
    p.add_argument("--prefix", help="command prefix for every scheduled entry")
    p.add_argument("--tag", help="namespace for installed entries (default: %s)" % DEFAULT_TAG)
    return p


def main(argv=None, out=None):
    out = sys.stdout if out is None else out
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest(out)

    cfg = resolve_config({
        "python": args.python, "scripts_dir": args.scripts_dir, "log_dir": args.log_dir,
        "env_file": args.env_file, "prefix": args.prefix, "tag": args.tag,
    })

    try:
        manifest = load_manifest(args.manifest, cfg)
    except SchedulerError as e:
        out.write("scheduler: %s\n" % e)
        return 2

    # --live is opt-in and --dry-run always wins if both are somehow present.
    live = bool(args.live) and not args.dry_run

    try:
        if args.list:
            return cmd_list(manifest, cfg, out)
        if args.run:
            # --run is what an installed cron/timer/task line invokes, so it RUNS by default.
            # Only an explicit --dry-run turns it into an explanation.
            job = manifest.get(args.run)
            return run_job(job, cfg, manifest, out=out, dry_run=bool(args.dry_run))
        if args.run_forever:
            run_forever(manifest, cfg, out=out)
            return 0

        backend = backend_for(args.backend)
        jobs = manifest.jobs
        if args.job:
            jobs = [manifest.get(n) for n in args.job]

        if args.install:
            return cmd_install(manifest, cfg, backend, out, live, jobs)
        if args.uninstall:
            return cmd_uninstall(manifest, cfg, backend, out, live, jobs)
        return cmd_status(manifest, cfg, backend, out)
    except SchedulerError as e:
        out.write("scheduler: %s\n" % e)
        return 2


# =================================================================================== selftest

def selftest(out=None):
    """Offline. Registers nothing, writes nothing outside a temp dir, runs no engine module."""
    import io
    import shutil
    import tempfile

    out = sys.stdout if out is None else out
    results = []

    def check(desc, cond, detail=""):
        results.append(bool(cond))
        out.write("%s — %s%s\n" % ("PASS" if cond else "FAIL", desc,
                                   ("  [%s]" % detail) if detail and not cond else ""))

    tmp = tempfile.mkdtemp(prefix="mer_scheduler_test_")
    prev_env = {k: os.environ.pop(v, None) for k, v in _CONFIG_ENV.items()}
    prev_manifest_env = os.environ.pop(MANIFEST_ENV_VAR, None)
    try:
        # ---------------------------------------------------------------- 1. the shipped manifest
        m = load_manifest()
        check("shipped manifest loads", isinstance(m, Manifest), m.source)
        check("shipped manifest is the .example default or a user copy",
              os.path.basename(m.source) in MANIFEST_SEARCH_NAMES, m.source)
        check("shipped manifest defines jobs", len(m.jobs) >= 6, "%d jobs" % len(m.jobs))
        cfg0 = Config(scripts_dir=HERE)
        missing = [st["module"] for j in m.jobs for st in j.steps
                   if not os.path.isfile(cfg0.module_path(st["module"]))]
        check("every module named by the manifest exists next to scheduler.py",
              not missing, "missing: %s" % sorted(set(missing)))
        check("every job has a bounded log target",
              all(j.log for j in m.jobs))

        # ---------------------------------------------------- 2. NO IDENTITY, NO HOST, ANYWHERE
        # The whole product promise is that a stranger can run this. A single absolute path or
        # container name baked into either file breaks it silently — it would still work on the
        # author's box, which is exactly how such a bug survives review.
        # The patterns are ASSEMBLED rather than written literally, so this list cannot match
        # itself. Written out, the first check would fail on its own source line — and the
        # obvious "fix" for that is to delete the check.
        forbidden = [
            ("/opt/" + "hermes", "an author-specific interpreter path"),
            ("hermes" + "-agent", "an author-specific container name"),
            (r"\bMER" + r"-\d+\b", "a specific case id"),
            ("/opt/" + "data/scripts", "a hardcoded engine directory"),
            ("docker" + r"\s+exec\s+\S", "a hardcoded container exec"),
            (r"\+\d{10,15}", "a phone number"),
        ]
        for path in (os.path.abspath(__file__),
                     os.path.join(HERE, "schedule.json.example")):
            src = open(path, encoding="utf-8").read()
            for rx, why in forbidden:
                check("%s carries no %s" % (os.path.basename(path), why),
                      not re.search(rx, src), "matched %r" % rx)
            emails = [e for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", src)
                      if "example.com" not in e]
            check("%s carries no email address" % os.path.basename(path), not emails,
                  "found %s" % emails)
            uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                               src)
            check("%s carries no workspace uuid" % os.path.basename(path), not uuids)

        # a fresh install must not auto-send on day one
        engine = m.get("mer-engine")
        check("shipped manifest does NOT ship the send switch live",
              engine.env.get("MER_ENGINE_SEND") in ("test", "off"),
              engine.env.get("MER_ENGINE_SEND"))

        # ------------------------------------------------------------------ 3. config indirection
        a = Config(python="/a/py", scripts_dir="/a/dir", tag="ta")
        b = Config(python="/b/py", scripts_dir="/b/dir", tag="tb")
        ca, cb = job_command(engine, a), job_command(engine, b)
        check("the command is built from config, not constants", ca != cb)
        check("the command names the configured interpreter", "/a/py" in ca)
        check("the command names the configured scripts dir", a.scripts_dir in ca, ca)
        check("the command runs the job through the manifest, not an expanded copy",
              "--run mer-engine" in ca)
        withpfx = job_command(engine, Config(python="/a/py", scripts_dir="/a/dir",
                                             prefix="somectl exec box"))
        check("a configured prefix is applied", withpfx.startswith("somectl exec box "))

        env_over = resolve_config({}, env={"MER_SCHED_PYTHON": "/env/py"}, profile_section={})
        check("env var configures the interpreter", env_over.python == "/env/py")
        flag_over = resolve_config({"python": "/flag/py"}, env={"MER_SCHED_PYTHON": "/env/py"},
                                   profile_section={})
        check("an explicit flag outranks the env var", flag_over.python == "/flag/py")
        prof_over = resolve_config({}, env={}, profile_section={"python": "/prof/py"})
        check("profile.json['scheduler'] configures the interpreter",
              prof_over.python == "/prof/py")
        check("env outranks profile",
              resolve_config({}, env={"MER_SCHED_PYTHON": "/env/py"},
                             profile_section={"python": "/prof/py"}).python == "/env/py")
        check("no profile / no env still yields a usable config",
              bool(resolve_config({}, env={}, profile_section={}).python))

        # ------------------------------------------------------------------- 4. cron expressions
        c = CronExpr("*/15 * * * *")
        check("*/15 expands to 4 minutes", sorted(c.fields[0]) == [0, 15, 30, 45])
        check("*/15 matches :30", c.matches(datetime.datetime(2026, 7, 27, 4, 30)))
        check("*/15 does not match :31", not c.matches(datetime.datetime(2026, 7, 27, 4, 31)))
        biz = CronExpr("0 8-17 * * 1-5")
        check("business-hours expr matches Mon 09:00",
              biz.matches(datetime.datetime(2026, 7, 27, 9, 0)))
        check("business-hours expr does not match Sat 09:00",
              not biz.matches(datetime.datetime(2026, 8, 1, 9, 0)))
        check("business-hours expr does not match 18:00",
              not biz.matches(datetime.datetime(2026, 7, 27, 18, 0)))
        mon = CronExpr("0 10 * * 1")
        check("weekly expr matches its Monday", mon.matches(datetime.datetime(2026, 7, 27, 10, 0)))
        check("weekly expr skips Tuesday", not mon.matches(datetime.datetime(2026, 7, 28, 10, 0)))
        check("sunday is 0 and 7 alike", CronExpr("0 0 * * 7").matches(
            datetime.datetime(2026, 7, 26, 0, 0)))
        check("named weekdays parse", CronExpr("0 10 * * mon").fields[4] == {1})
        # the dom/dow OR rule — the classic cron trap
        both = CronExpr("0 0 1 * 1")
        check("dom OR dow: fires on the 1st", both.matches(datetime.datetime(2026, 8, 1, 0, 0)))
        check("dom OR dow: fires on a Monday", both.matches(datetime.datetime(2026, 8, 3, 0, 0)))
        check("dom OR dow: not on other days",
              not both.matches(datetime.datetime(2026, 8, 4, 0, 0)))
        for bad in ("0 8 * *", "0 99 * * *", "0 8 * * 1-", "x * * * *", "0 8-4 * * *"):
            try:
                CronExpr(bad)
                check("rejects bad expression %r" % bad, False)
            except SchedulerError:
                check("rejects bad expression %r" % bad, True)

        # -------------------------------------------------------------------- 5. systemd render
        check("OnCalendar for daily 09:00",
              CronExpr("0 9 * * *").to_systemd() == "*-*-* 09:00:00",
              CronExpr("0 9 * * *").to_systemd())
        check("OnCalendar for Monday 10:00",
              CronExpr("0 10 * * 1").to_systemd() == "Mon *-*-* 10:00:00",
              CronExpr("0 10 * * 1").to_systemd())
        check("OnCalendar lists business hours",
              CronExpr("0 8-17 * * 1-5").to_systemd()
              == "Mon,Tue,Wed,Thu,Fri *-*-* 08,09,10,11,12,13,14,15,16,17:00:00",
              CronExpr("0 8-17 * * 1-5").to_systemd())
        sysd = SystemdBackend(unit_dir=os.path.join(tmp, "units"),
                              runner=lambda argv: (0, "", ""))
        d1, p1 = sysd.plan_install(m.jobs, a)
        check("systemd plans a service+timer per job", len(p1["files"]) == 2 * len(m.jobs))
        sysd.apply_install(p1, a)
        check("systemd wrote its unit files",
              os.path.isfile(os.path.join(tmp, "units",
                                          "ta-mer-engine.timer")))
        check("systemd reports what it installed",
              sysd.installed(m.jobs, a) == {j.name for j in m.jobs})
        sysd.apply_install(p1, a)      # twice
        check("systemd install is idempotent (no duplicate units)",
              len(os.listdir(os.path.join(tmp, "units"))) == 2 * len(m.jobs))
        unit = open(os.path.join(tmp, "units", "ta-mer-engine.service"),
                    encoding="utf-8").read()
        check("the unit's ExecStart is the configured command", "/a/py" in unit)
        d2, p2 = sysd.plan_uninstall(m.jobs, a)
        sysd.apply_uninstall(p2, a)
        check("systemd uninstall removes every unit",
              not os.listdir(os.path.join(tmp, "units")))

        # ------------------------------------------------------------- 6. cron block, idempotent
        store = {"text": "# somebody else's crontab\n0 3 * * * /usr/local/bin/backup.sh\n"}
        cron = CronBackend(read=lambda: store["text"],
                           write=lambda t: store.__setitem__("text", t))
        d3, p3 = cron.plan_install(m.jobs, a)
        cron.apply_install(p3, a)
        first = store["text"]
        check("cron installed one line per job",
              sum(1 for ln in first.splitlines() if "--run" in ln) == len(m.jobs))
        check("cron preserved the foreign entry", "backup.sh" in first)
        cron.apply_install(cron.plan_install(m.jobs, a)[1], a)
        cron.apply_install(cron.plan_install(m.jobs, a)[1], a)
        check("cron install is idempotent (3x install == 1 block)", store["text"] == first)
        check("cron block appears exactly once",
              store["text"].count(BLOCK_BEGIN % a.tag) == 1)
        check("cron reports what it installed",
              cron.installed(m.jobs, a) == {j.name for j in m.jobs})
        d4, p4 = cron.plan_uninstall(m.jobs, a)
        cron.apply_uninstall(p4, a)
        check("cron uninstall removed our block", "--run" not in store["text"])
        check("cron uninstall left the foreign entry alone", "backup.sh" in store["text"])
        check("cron reports nothing installed after uninstall",
              cron.installed(m.jobs, a) == set())
        # two tags coexist without colliding
        cron.apply_install(cron.plan_install(m.jobs, a)[1], a)
        cron.apply_install(cron.plan_install(m.jobs, b)[1], b)
        check("a second tag does not clobber the first",
              BLOCK_BEGIN % a.tag in store["text"] and BLOCK_BEGIN % b.tag in store["text"])

        # ------------------------------------------------------------------ 7. Windows schtasks
        calls = []
        st = SchtasksBackend(runner=lambda argv: (calls.append(argv), (0, "", ""))[1])
        d5, p5 = st.plan_install([m.get("mer-sol-watchdog")], a)
        check("schtasks creates one task for a weekly job", len(p5) == 1)
        argv = p5[0]
        check("schtasks task name is namespaced and deterministic",
              argv[argv.index("/TN") + 1] == "\\ta\\mer-sol-watchdog-1000",
              argv[argv.index("/TN") + 1])
        check("schtasks uses WEEKLY for a weekday-restricted job",
              argv[argv.index("/SC") + 1] == "WEEKLY")
        check("schtasks names the weekday", argv[argv.index("/D") + 1] == "MON")
        check("schtasks sets the start time", argv[argv.index("/ST") + 1] == "10:00")
        check("schtasks forces overwrite so re-install cannot duplicate", "/F" in argv)
        d6, p6 = st.plan_install([m.get("mer-delivery-check")], a)
        check("schtasks expands an hour list into one task per time", len(p6) == 3)
        check("schtasks uses DAILY when no weekday restriction",
              p6[0][p6[0].index("/SC") + 1] == "DAILY")
        d7, p7 = st.plan_install([m.get("mer-engine")], a)
        check("schtasks handles the 10-hour business-day job", len(p7) == 10)
        names = [x[x.index("/TN") + 1] for x in p7]
        check("schtasks task names are unique", len(set(names)) == len(names))
        d8, p8 = st.plan_uninstall([m.get("mer-engine")], a)
        check("schtasks uninstall deletes exactly the names it creates",
              [x[x.index("/TN") + 1] for x in p8] == names)

        too_many = Job({"name": "toomany", "schedule": "*/5 * * * *",
                        "steps": [{"module": "x.py"}]})
        try:
            st.plan_install([too_many], a)
            check("schtasks refuses a schedule it cannot express exactly", False)
        except SchedulerError as e:
            check("schtasks refuses a schedule it cannot express exactly",
                  "run-forever" in str(e))
        dom_job = Job({"name": "domjob", "schedule": "0 9 1 * *",
                       "steps": [{"module": "x.py"}]})
        try:
            st.plan_install([dom_job], a)
            check("schtasks refuses a day-of-month schedule", False)
        except SchedulerError:
            check("schtasks refuses a day-of-month schedule", True)

        # -------------------------------------------------------------- 8. DRY RUN IS THE DEFAULT
        buf = io.StringIO()
        store2 = {"text": ""}
        cron2 = CronBackend(read=lambda: store2["text"],
                            write=lambda t: store2.__setitem__("text", t))
        cmd_install(m, a, cron2, buf, live=False, jobs=m.jobs)
        check("--install without --live writes nothing", store2["text"] == "")
        check("--install without --live says so", "DRY RUN" in buf.getvalue())
        cmd_install(m, a, cron2, io.StringIO(), live=True, jobs=m.jobs)
        check("--install --live does write", "--run" in store2["text"])
        buf = io.StringIO()
        cmd_uninstall(m, a, cron2, buf, live=False, jobs=m.jobs)
        check("--uninstall without --live removes nothing", "--run" in store2["text"])
        check("--uninstall without --live says so", "DRY RUN" in buf.getvalue())
        parsed = build_parser().parse_args(["--install"])
        check("the parser defaults --live to off", parsed.live is False)

        # --------------------------------------------------------------- 9. selecting a subset
        store3 = {"text": ""}
        cron3 = CronBackend(read=lambda: store3["text"],
                            write=lambda t: store3.__setitem__("text", t))
        d9, p9 = cron3.plan_install([m.get("mer-case-tick")], a)
        check("a single job can be installed alone",
              sum(1 for ln in p9.splitlines() if "--run" in ln) == 1)

        # -------------------------------------------------------------- 10. the env-file reader
        envp = os.path.join(tmp, "shared.env")
        with open(envp, "w", encoding="utf-8") as f:
            f.write("# a comment\n"
                    "this line is prose and has no equals sign\n"
                    "WANTED=value-one\n"
                    "WANTED=value-two-ignored\n"
                    'QUOTED="quoted value"\n'
                    "NOT_WANTED=secret\n"
                    "=malformed\n")
        got = load_env_file(envp, ["WANTED", "QUOTED", "ABSENT"])
        check("env file reads only the declared keys", set(got) == {"WANTED", "QUOTED"}, str(got))
        check("env file takes the first occurrence", got["WANTED"] == "value-one")
        check("env file strips quotes", got["QUOTED"] == "quoted value")
        check("env file survives prose and malformed lines", "NOT_WANTED" not in got)
        check("a missing env file is not an error", load_env_file(os.path.join(tmp, "no"), ["A"]) == {})

        # ------------------------------------------------------- 11. the runner, on a fake job
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        with open(os.path.join(work, "noisy.py"), "w", encoding="utf-8") as f:
            f.write("import os,sys\n"
                    "sys.stdout.write('[DUE] fake-case needs action\\n')\n"
                    "sys.stdout.write('quiet detail line\\n')\n"
                    "sys.stderr.write('stderr detail\\n')\n"
                    "sys.stdout.write('env=%s\\n' % os.environ.get('FROM_FILE',''))\n")
        with open(os.path.join(work, "alerting.py"), "w", encoding="utf-8") as f:
            f.write("import sys\nsys.stderr.write('sweep detail\\n')\nsys.exit(2)\n")
        wcfg = Config(python=sys.executable, scripts_dir=work, log_dir=work, env_file=envp)

        jm = Job({"name": "fake-match", "schedule": "0 9 * * *", "log": "fake.log",
                  "env_from_file": ["WANTED"], "env": {"FROM_FILE": "x"},
                  "steps": [{"module": "noisy.py", "output": "log+match"}],
                  "stdout": {"mode": "match", "match": r"\[DUE",
                             "header": "needs action:", "footer": "(end)"}})
        buf = io.StringIO()
        rc = run_job(jm, wcfg, m, out=buf)
        text = buf.getvalue()
        check("match mode surfaces only matching lines", "[DUE] fake-case" in text)
        check("match mode suppresses the rest", "quiet detail line" not in text)
        check("match mode prints its header/footer", "needs action:" in text and "(end)" in text)
        logtext = open(os.path.join(work, "fake.log"), encoding="utf-8").read()
        check("the full detail went to the log", "quiet detail line" in logtext
              and "stderr detail" in logtext)
        check("job env reached the child", "env=x" in logtext)
        check("the runner returned the child's exit code", rc == 0)

        ja = Job({"name": "fake-alert", "schedule": "0 9 * * *", "log": "alert.log",
                  "steps": [{"module": "alerting.py", "output": "alert"}],
                  "stdout": {"mode": "alert", "fail_rc_above": 1,
                             "fail_message": "sweep exited {rc} with no output."}})
        buf = io.StringIO()
        rc = run_job(ja, wcfg, m, out=buf)
        check("a sweep that died with no output is NOT silent",
              "sweep exited 2 with no output." in buf.getvalue(), buf.getvalue())
        check("a broken sweep still returns its exit code", rc == 2)

        js = Job({"name": "fake-silent", "schedule": "0 9 * * *", "log": "silent.log",
                  "steps": [{"module": "noisy.py", "output": "log"}],
                  "stdout": {"mode": "silent"}})
        buf = io.StringIO()
        run_job(js, wcfg, m, out=buf)
        check("silence = healthy: a silent job prints nothing at all", buf.getvalue() == "")

        # the log is bounded — an unbounded log on a small VPS is a disk-full outage
        tiny = Manifest({"version": 1, "defaults": {"log_max_lines": 5},
                         "jobs": [js.raw]}, "memory")
        for _ in range(6):
            run_job(js, wcfg, tiny, out=io.StringIO())
        nlines = len(open(os.path.join(work, "silent.log"), encoding="utf-8").readlines())
        check("the per-job log is bounded", nlines <= 5, "%d lines" % nlines)

        # a job naming a module that is not installed must be loud, not silently fine
        jmissing = Job({"name": "fake-missing", "schedule": "0 9 * * *", "log": "missing.log",
                        "steps": [{"module": "not_here.py", "output": "alert"}],
                        "stdout": {"mode": "alert", "fail_rc_above": 1,
                                   "fail_message": "did not run ({rc})"}})
        buf = io.StringIO()
        rc = run_job(jmissing, wcfg, m, out=buf)
        check("a missing module is reported, never treated as a clean run",
              rc == 2 and "did not run" in buf.getvalue())

        # --run --dry-run explains without executing
        buf = io.StringIO()
        run_job(jm, wcfg, m, out=buf, dry_run=True)
        check("--run --dry-run explains and executes nothing",
              "DRY RUN" in buf.getvalue() and "noisy.py" in buf.getvalue())

        # ------------------------------------------------------------------ 12. the forever loop
        fired = []
        clock = {"t": datetime.datetime(2026, 7, 27, 8, 59)}

        def now_fn():
            return clock["t"]

        def sleep_fn(_):
            clock["t"] += datetime.timedelta(minutes=1)

        loopm = Manifest({"version": 1, "defaults": {"log_max_lines": 50},
                          "jobs": [dict(js.raw, name="loopjob", schedule="0 9 * * *")]},
                         "memory")
        real_run = globals()["run_job"]
        globals()["run_job"] = lambda job, cfg, mf=None, out=None, **kw: fired.append(job.name)
        try:
            run_forever(loopm, wcfg, out=io.StringIO(), now_fn=now_fn,
                        sleep_fn=sleep_fn, max_ticks=5)
        finally:
            globals()["run_job"] = real_run
        check("the foreground clock fires a job at its minute", fired == ["loopjob"],
              str(fired))

        # ------------------------------------------------------------------- 13. manifest errors
        for bad, why in (
            ({"version": 1, "jobs": []}, "no jobs"),
            ({"version": 1, "jobs": [{"schedule": "0 9 * * *", "steps": [{"module": "a.py"}]}]},
             "no name"),
            ({"version": 1, "jobs": [{"name": "a", "steps": [{"module": "a.py"}]}]}, "no schedule"),
            ({"version": 1, "jobs": [{"name": "a", "schedule": "0 9 * * *"}]}, "no steps"),
            ({"version": 1, "jobs": [{"name": "a", "schedule": "0 9 * * *",
                                      "steps": [{"module": "a.py"}]},
                                     {"name": "a", "schedule": "0 9 * * *",
                                      "steps": [{"module": "a.py"}]}]}, "duplicate names"),
            ({"version": 1, "jobs": [{"name": "a b", "schedule": "0 9 * * *",
                                      "steps": [{"module": "a.py"}]}]}, "unusable name"),
            ({"version": 1, "jobs": [{"name": "a", "schedule": "0 9 * * *",
                                      "steps": [{"module": "a.py"}],
                                      "stdout": {"mode": "match"}}]}, "match with no regex"),
        ):
            try:
                Manifest(bad, "memory")
                check("manifest with %s is rejected" % why, False)
            except SchedulerError:
                check("manifest with %s is rejected" % why, True)

        badp = os.path.join(tmp, "bad.json")
        with open(badp, "w", encoding="utf-8") as f:
            f.write("{not json")
        try:
            load_manifest(badp)
            check("malformed manifest JSON is rejected", False)
        except SchedulerError as e:
            check("malformed manifest JSON is rejected", "not valid JSON" in str(e))
        try:
            load_manifest(os.path.join(tmp, "nope.json"))
            check("an explicit manifest path that does not exist is an error", False)
        except SchedulerError:
            check("an explicit manifest path that does not exist is an error", True)

        # ------------------------------------------------------------------- 14. CLI plumbing
        buf = io.StringIO()
        rc = main(["--list"], out=buf)
        check("--list works end to end", rc == 0 and "mer-engine" in buf.getvalue())
        buf = io.StringIO()
        rc = main(["--status", "--backend", "forever"], out=buf)
        check("--status works end to end", rc == 0 and "backend" in buf.getvalue())
        buf = io.StringIO()
        rc = main(["--install", "--backend", "forever"], out=buf)
        check("--install --backend forever explains itself without registering anything",
              rc == 0 and "DRY RUN" in buf.getvalue())
        buf = io.StringIO()
        rc = main(["--run", "no-such-job"], out=buf)
        check("an unknown job name is a clear error, not a traceback",
              rc == 2 and "no job named" in buf.getvalue())
        buf = io.StringIO()
        rc = main(["--run", "mer-engine", "--dry-run"], out=buf)
        check("--run --dry-run from the CLI executes nothing",
              rc == 0 and "DRY RUN" in buf.getvalue())
        try:
            backend_for("nonsense")
            check("an unknown backend is rejected", False)
        except SchedulerError:
            check("an unknown backend is rejected", True)
        check("auto backend selection returns something usable",
              backend_for("auto").name in BACKENDS)
    finally:
        for k, v in prev_env.items():
            if v is not None:
                os.environ[_CONFIG_ENV[k]] = v
        if prev_manifest_env is not None:
            os.environ[MANIFEST_ENV_VAR] = prev_manifest_env
        shutil.rmtree(tmp, ignore_errors=True)

    bad = results.count(False)
    if bad:
        out.write("SELF-TEST FAILED — %d of %d checks failed\n" % (bad, len(results)))
        return 1
    out.write("PASS — scheduler self-test: %d/%d checks passed\n" % (len(results), len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
