# The clock — `scheduler.py`

**Your engine needs a clock. Run one command, once.**

```
cd engine
python scheduler.py --install          # prints the plan, changes NOTHING
python scheduler.py --install --live   # actually installs it
```

That is the whole task. Everything below is why, and what to do when it is not that simple.

---

## Why this matters more than it sounds like it does

Without a clock, this package is a very good filing cabinet. It can draft the letter, look up your
state's regulator, compute the deadline and check whether a refund landed — all of it, on demand,
forever — and it will never once notice that a deadline passed on a Tuesday while you were at work.

Every one of the six jobs below exists because **nothing inside the engine can trigger it.** A
vendor's silence produces no event. The statute of limitations produces no event. A message that
was accepted by a mail server and bounced ten minutes later produces no event. Those are exactly
the failures that kill a return case, and the only thing that catches a non-event is a clock.

---

## What gets installed

Six jobs, defined in `schedule.json.example` (the manifest) next to `scheduler.py`. Times are in
**your** local time zone.

| Job | When | What it does |
|---|---|---|
| `mer-engine` | hourly, 08:00–17:00, Mon–Fri | Read new mail, classify it, draft a reply, then process the veto queue. |
| `mer-case-tick` | daily 09:00 | Walk every open case, work out what is due, surface only what needs you. |
| `mer-calendar-sync` | daily 09:30 | Mirror each case's phase deadline onto its due date, then onto your calendar. |
| `mer-delivery-check` | 08:00, 12:00, 16:00 | Did the letters you sent actually arrive, or did they bounce? |
| `mer-sol-watchdog` | Mondays 10:00 | How much legal runway is left on each case? |
| `mer-unmatched-review` | Mondays 10:30 | Return-related mail that matched no case — the slow leak. |

Business hours, on purpose. An alert is worth exactly as much as the correction that follows it,
and corrections happen while you are awake. A draft queued at 3am is also a draft you cannot veto.

### Silence means healthy

Five of the six jobs print **nothing** when everything is fine. That is deliberate: a watchdog that
reports "all clear" every week is one you learn to swipe away, and a watchdog nobody reads is the
same as no watchdog. The full detail of every run still goes to a per-job log next to the engine
(`mer_engine.log`, `sol_watchdog.log`, …), bounded to the last 500 lines so it can never fill a
disk.

The inverse rule is enforced too, and it matters just as much: **a job that could not run says so.**
Silence means "checked and fine", never "never checked".

`mer-calendar-sync` is the exception and always prints one summary line, so you have a way to tell
"nothing is wrong" apart from "nothing is running".

---

## Your first week: it will not email a vendor

The shipped manifest sets `MER_ENGINE_SEND=test`. In test mode every drafted reply is redirected to
**your own mailbox** with a banner showing where it would have gone. A fresh install cannot write to
a stranger's vendor on day one.

When you have watched the drafts for a week and trust them, copy the manifest and change it:

```
cp schedule.json.example schedule.json      # your copy wins over the shipped default
# edit schedule.json: "MER_ENGINE_SEND": "live"
python scheduler.py --install --live       # re-install so the change takes effect
```

Values: `off` (draft nothing) · `test` (drafts go to you) · `live` (drafts go to the vendor after
the veto window).

---

## Which scheduler it uses

`--install` detects your host. You can force one with `--backend`.

| Backend | Where | What it creates |
|---|---|---|
| `cron` | Linux, macOS, most servers | One managed block in your crontab. Lines outside it are never touched. |
| `systemd` | modern Linux desktops | One user `.service` + `.timer` per job in `~/.config/systemd/user/`. No root needed. `Persistent=true`, so a laptop asleep at 09:00 still runs the tick when it wakes. |
| `schtasks` | Windows | One Task Scheduler task per firing time, under a `\merchandise-return\` folder. |
| `forever` | containers, or a host with none of the above | Nothing is registered — `scheduler.py --run-forever` **is** the clock, in your terminal. |

```
python scheduler.py --run-forever
```

Keep that alive however you keep anything alive: a terminal you leave open, a `restart: always`
container, a supervisor. It ticks once a minute and handles any cron expression, including ones the
Windows backend refuses.

---

## Every command

```
python scheduler.py --status              # what is installed, and when did each job last run?
python scheduler.py --list                # the manifest, as a table
python scheduler.py --install             # DRY RUN — print the plan, change nothing
python scheduler.py --install --live      # apply it
python scheduler.py --uninstall --live    # remove exactly what was installed, nothing else
python scheduler.py --run mer-case-tick   # run one job right now, exactly as the clock would
python scheduler.py --run mer-engine --dry-run   # show what that job would execute
python scheduler.py --run-forever         # be the clock
python scheduler.py --help
```

**`--dry-run` is the default.** `--install` and `--uninstall` print their plan and change nothing
until you add `--live`. A tool that edits your crontab because you typed its name is a tool you
cannot leave lying around.

**Installing twice is safe.** cron entries live inside one delimited block that is regenerated whole;
systemd units and Windows tasks have deterministic names that get overwritten in place. Install it
five times and you have one copy of each job.

**Uninstall is exact.** It removes the entries it created and nothing else. Your own crontab lines
are copied through untouched.

---

## Pointing it at a different machine layout

Nothing about your machine is baked into the manifest or the code. Every host-specific value is
config, and a flag beats an environment variable beats your `profile.json`:

| What | Flag | Environment variable | Default |
|---|---|---|---|
| Python interpreter | `--python` | `MER_SCHED_PYTHON` | the one running `scheduler.py` |
| Engine directory | `--scripts-dir` | `MER_SCHED_SCRIPTS_DIR` | the directory `scheduler.py` is in |
| Log directory | `--log-dir` | `MER_SCHED_LOG_DIR` | same as the engine directory |
| Shared secrets file | `--env-file` | `MER_SCHED_ENV_FILE` | none |
| Command prefix | `--prefix` | `MER_SCHED_PREFIX` | none |
| Namespace for entries | `--tag` | `MER_SCHED_TAG` | `merchandise-return` |

Or put them in your `profile.json`:

```json
{
  "scheduler": {
    "python": "/srv/venv/bin/python3",
    "log_dir": "/var/log/returns"
  }
}
```

**The secrets file.** If you keep API tokens in a shared `KEY=VALUE` file, point `--env-file` at it.
Each job reads **only** the keys it declares in the manifest — key by key, never by sourcing the
file, because a real secrets file in the wild has comments, prose and half-finished lines in it and
sourcing one of those takes the job down with it.

**The prefix.** If your engine lives inside a container, `--prefix "<your container runtime> exec
<your container>"` puts that in front of every scheduled command. There is no default and no
built-in container name.

---

## Editing the schedule

Copy `schedule.json.example` to `schedule.json` in the same directory (or in your working
directory, or point `$MER_SCHEDULE` at it) and edit your copy. The shipped file is the fallback, so
an upgrade never overwrites your changes.

A job looks like this:

```json
{
  "name": "mer-sol-watchdog",
  "schedule": "0 10 * * 1",
  "env": { "SOL_ALERT_DAYS": "90" },
  "log": "sol_watchdog.log",
  "steps": [ { "module": "sol_watchdog.py", "args": ["--cron"], "output": "alert" } ],
  "stdout": { "mode": "alert", "fail_rc_above": 0,
              "fail_message": "sol_watchdog exited {rc} with no output." }
}
```

- **`schedule`** — a standard 5-field cron expression in local time. `*`, ranges, lists and `*/n`
  all work, as do `mon`/`jan` style names. The nonstandard extensions (`@reboot`, `L`, `W`, `#`) are
  rejected rather than silently ignored, because they cannot be expressed on every backend and a
  silently-dropped field gives you a job that never fires.
- **`steps`** — engine modules, run in order, in the engine directory.
  `output` is `log` (everything to the log), `log+match` (also searchable by the `stdout` rule) or
  `alert` (this step's stdout is the alert).
- **`stdout`** — what reaches you. `silent`, `alert` (print if the step said anything),
  `match` (print only lines matching a regex) or `summary` (always print a one-liner).
  `fail_rc_above` is what stops a crashed job from looking like a clean one.

After editing, re-run `--install --live`. The installed entries only ever say
`scheduler.py --run <job>` — the manifest stays the single source of truth, so a scheduler entry can
never drift from the schedule you wrote.

---

## Troubleshooting

**"No jobs are installed."** `--status` says this when it cannot find its entries. Run
`--install --live`.

**A job says installed but `LAST LOG WRITE` says never run.** The clock is registered but the job is
not executing. Run it by hand — `python scheduler.py --run <job>` — and read what it says. The
usual causes are an interpreter that cannot import the engine's dependencies (`--python`) and a
missing profile (`python onboard.py`).

**The Windows backend refuses a job.** Task Scheduler has no cron expression, so `scheduler.py`
expands each job into one task per firing time and refuses above 24 a day, or when a job restricts
day-of-month. It refuses rather than approximating: an installer that quietly gives you a coarser
schedule than you asked for is worse than one that tells you it cannot. Use `--run-forever` for
that job.

**cron runs the job but it behaves differently than by hand.** cron starts with almost no
environment. Set `--python` to an absolute interpreter path and `--env-file` to your secrets file,
then re-install.

**Nothing has printed in weeks.** That is probably correct — five of six jobs are silent when
healthy. Confirm the clock is alive by checking `--status` for recent `LAST LOG WRITE` times, or by
reading the tail of a log.
