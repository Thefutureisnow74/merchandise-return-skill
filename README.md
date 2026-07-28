# Merchandise Return

A self-contained engine for chasing **your own** refund — under your own name, your own email
account, and your own project board. There is no client, no representative, and no fee. You are
the only name on every letter it writes.

**New here? Read [`USER-GUIDE.md`](USER-GUIDE.md) first.** It is written for a capable
non-programmer and covers install, what the engine will and will not send in your name, and how to
stop it.

---

## What it does

- Keeps one record per case, on **your** Multica board (`https://multica.ai`).
- Watches the mailbox you connect, matches replies to cases, and reads them — PDFs included.
- Classifies each vendor reply, and judges whether it is a real resolution or a brush-off.
  *"We've escalated this internally"* is treated as nothing happening.
- Computes deadlines in **business days**, and watches the outer statute-of-limitations clock.
- Drafts your next letter using only facts already on the case record — a draft that asserts
  anything not in its inputs is discarded rather than sent.
- Climbs an escalation ladder: vendor → executives and public pressure → the regulators that
  actually apply to *your* case → (where the facts support it) a civil-rights track → statutory
  pre-suit demand → chargeback and a prepared small-claims petition.

It never files in court, never spends your money, and never signs anything.

## Your own purchases only

Every letter goes out over **your** name and signature. The engine has no way to represent anyone
else, and it will not try: a case marked as a client's or a third party's (`CLIENT:` in the title,
or `CLIENT CASE` in the description) is **fenced out of the ladder entirely** and never advances.
Helping a relative with their refund is out of scope for this tool.

## What it will send by itself

Three lanes. **The normative definition — including the test that decides which lane a novel action
falls into — is `SKILL.md` §5, and that is the one to read.** In summary:

| Lane | Behaviour |
|---|---|
| 🟢 **Green** | Always automatic — logging, watching, classifying, drafting, deadline maths. Nothing leaves. |
| 🟡 **Yellow** | Sends **by itself** after a countdown (default 3 h) — but only to a vendor already writing to you on this case, in private email, asserting nothing new. |
| 🔴 **Red** | Never without your explicit yes. Anything that reaches a new party, becomes a public or government record, moves money, forfeits a remedy, ends the case, or asserts a legal claim. **Anything ambiguous is Red.** |

Sending is **off** until you turn it on (`MER_ENGINE_SEND=off|test|live|veto`), and the shipped
schedule sets `test`, in which every draft is redirected to **you** instead of the vendor. That means
the Yellow lane does nothing at all until you edit that value in your own `schedule.json` and
reinstall. Watch it work in `test` before trusting it.

The user guide walks through the same ground for a non-programmer in §4.

## It will not talk you out of a claim

An expired warranty, a closed return window, or a statute estimate that reads EXPIRED does **not**
stop the pursuit — none of those gate anything. They change the argument, not the answer. Where a
support agent would say *"that's outside our window"*, this asks what happened and puts the reason
in the letter. See `SKILL.md` §1.6.

## Getting started

```bash
python engine/onboard.py            # dry-run: shows exactly what it would create
python engine/onboard.py --live     # provisions your workspace, project and case properties
python engine/gmail_connect.py --help-setup   # one-time Google setup, then connect your mailbox
python engine/run_tests.py          # everything should be green before you trust it
python engine/scheduler.py --install        # dry-run: the clock, and what it would register
python engine/scheduler.py --install --live # give the engine a clock
```

**Do not skip the last one.** Without a clock the engine only acts while you are sitting in front
of it, and the failures that kill a return case are non-events: a vendor's silence, a limitations
date, a letter that bounced after the mail server accepted it. Nothing inside the engine can
trigger on those. `scheduler.py` detects whether your host uses cron, systemd timers or Windows
Task Scheduler and installs nine jobs; on a host with none of them, `scheduler.py --run-forever`
*is* the clock. `--dry-run` is the default, installing twice is safe, and it ships in `test` mode
so your first week cannot reach a vendor. Details: [`references/scheduler.md`](references/scheduler.md).

You will need: a Multica account and API token, a Google OAuth **Desktop** client of your own, and
optionally an LLM API key (without one, classification degrades to a keyword matcher rather than
guessing). `engine/profile.example.json` documents every configuration field.

**There is no built-in identity.** Run unconfigured, the engine refuses to send and tells you how
to fix it, rather than mailing as somebody else. That is deliberate.

## Honest limits

- The automated half (deadline watching, auto-sending) needs a machine that stays awake. `scheduler.py`
  installs the clock, but it cannot run jobs on a laptop that is asleep. Run only in a chat session,
  you get the drafting and case-management half.
- Confirming that a refund actually landed relies on a bank/issuer email or a line you write
  yourself. There is no bank API.
- A case whose remedy is a replacement or repair rather than money has no automated close path yet.
- It is not a lawyer, it is not legal advice, and it does not represent anyone. You are always your
  own principal.

## Licence

See [`LICENSE`](LICENSE).
