# Merchandise Return — User Guide

**Read the whole of section 4 before you turn sending on.** That section is the only one that
describes something irreversible.

---

## 1. What this is

This is a skill — a package of instructions plus a set of small Python programs — that helps **one
person chase their own refund**. That person is you. You are the only name on every letter it
writes. There is no client, no lawyer, no representative, and no fee.

What it actually does, day to day:

- Keeps one record per case (a "case" = one item, one vendor, one refund you want).
- Watches the email account you connect, finds mail belonging to a case, and reads it — including
  PDF attachments.
- Classifies each vendor reply into one of seven buckets: refund, partial, refused, needs-info,
  legal-threat, discrimination-signal, other.
- Judges whether a reply is a real resolution or a brush-off. "We've escalated this internally"
  is treated as nothing happening.
- Computes deadlines in **business days** (weekends and US federal holidays excluded) and tracks
  them.
- Drafts your next email, in your voice, using only facts already on the case record.
- Sends some of those drafts by itself — see section 4 — and surfaces the rest to you.
- Climbs an escalation ladder: vendor → executives and public pressure → regulators → (if the
  facts support it) a civil-rights track → statutory pre-suit demand → chargeback and a prepared
  small-claims petition.

What it is not doing: it never files anything in court, never spends your money, and never signs
anything.

The case record and the deadlines live on **Multica**, a project-management tool. Each case is a
Multica issue. Every classification, every reply, and every deadline gets written there, so the
history of your case is auditable and does not live inside a chat window.

---

## 2. What you need before you start

**1. A Multica account.** Free to create at https://multica.ai. Your board is yours alone — it has
no connection to whoever handed you this skill. You will end up with one workspace ("Merchandise
Return") and one project inside it holding your cases.

**2. An email account you are willing to connect.** The engine reads this mailbox to find vendor
replies, and — once you enable sending — sends from it. You choose which account.

> **A separate inbox is recommended, not required.** A dedicated returns address limits the damage
> if something sends when you did not expect it, and keeps the reply-classifier from wading through
> newsletters. If you would rather use your everyday inbox, that is your call — just keep the send
> mode tighter (section 5).

**3. Your state AND county of residence.** This is not paperwork. It decides:

- **Which attorney general** you can complain to. Consumer complaints go to *your* state AG, and
  each one has a different form, different intake unit, and different appetite.
- **Which consumer-protection statute governs you.** Texas has the DTPA; California has the CLRA
  and Song-Beverly; New York has GBL §349. They give different remedies — some give you treble
  damages, some give you attorney's fees, some require you to send a **pre-suit demand letter a
  fixed number of days before you are allowed to sue**. Miss that notice requirement and your case
  can be dismissed on a technicality.
- **Where you would file a small-claims case, and for how much.** Venue is your county — usually a
  specific justice court or district within it. Dollar caps vary a lot by state, and the cap decides
  whether small claims is even the right forum for your amount.
- **Which civil-rights agency** applies, if you were treated differently because of a protected
  characteristic.

There is no default state. Do not let anything assume one.

**4. Somewhere for the engine to run.** The automated parts are cron jobs on an always-on machine
(a small server / VPS). If you only run the skill interactively in a chat session, you get the
drafting, research, and case-management half — the deadline watching and auto-sending half needs a
machine that stays awake. See section 7.

**5. An LLM API key.** Classification and drafting call a language model over HTTP. The code uses an
OpenAI-compatible endpoint and reads its key from environment variables (`NANOGPT_API_KEY`, with
`MINIMAX_API_KEY` as a fallback). If no key works, it degrades to a keyword-based classifier rather
than guessing — it never fails open.

---

## 3. Install and first run

### 3a. Put the skill somewhere your assistant can see it

Copy the whole `merchandise-return/` folder (this file, `SKILL.md`, `references/`, and `engine/` —
all the Python lives in `engine/`) into your skills directory:

- **Claude Code:** `~/.claude/skills/merchandise-return/`
- **An agent runtime (e.g. a Telegram agent):** that runtime's skills directory.
- **Multica:** import it as a workspace skill.

Keep one master copy and redeploy from it. Editing a copy in place is how the two predecessors of
this skill drifted apart and started contradicting each other.

### 3b. Onboarding

Onboarding is a **conversation the skill runs with you**, once, before any case. Start it by saying
*"start a return"* or invoking the skill. It captures:

- Your full legal name, exactly as it appears on the purchase.
- Which of your email accounts to use, and connects it (OAuth) so mail can be read and sent.
- Your phone and mailing address.
- **Your state and county.**
- Your autonomy preference — how much the engine may send on its own (section 4).

These get stored on your Multica board so every case inherits them.

### 3c. Open your first case

Say what happened, plainly: what you bought, from whom, what went wrong, what you want. The skill
runs an intake questionnaire (`references/intake-questionnaire.md`). Some fields **block** the case
from advancing until answered, because cases die without them:

- exact brand, model, size/variant, and serial / IMEI / VIN
- purchase date, exact amount paid, and **how you paid** — this decides whether a chargeback is
  available to you at all
- **was it defective from the start, or did it fail later?** The single most valuable question
- damage history — dropped, liquid, repaired, modified. Answer honestly; a surprise here later
  destroys your credibility with a regulator
- what outcome you want, ranked: refund / replacement / repair / credit
- who you have already contacted, and any case or RMA numbers
- whether you believe you were treated differently because of race, color, religion, sex, national
  origin, age, disability, or another protected characteristic — and if so, exactly what was said or
  done, by whom, and when. Facts only. The engine will never manufacture this claim, and neither
  should you.

### 3d. Give it a clock — run this once

**This is the step people skip, and it is the one that makes the rest of it work.**

Without a clock, everything here happens only while you are sitting in front of it. The engine can
draft the letter, compute the deadline and check whether the refund landed — on demand, forever —
and it will never once notice that a deadline passed on a Tuesday while you were at work. A vendor's
silence produces no event. The statute of limitations produces no event. Those are exactly the
failures that kill a case, and only a clock catches a non-event.

```
cd engine
python scheduler.py --install          # prints the plan, changes nothing
python scheduler.py --install --live   # actually installs it
python scheduler.py --status           # what is installed, and when did it last run?
```

It works out what your machine uses — cron, systemd timers, or Windows Task Scheduler — and installs
**nine** jobs. Two of them run around the clock: a hot path **every minute, 24/7** that fires the
moment a reply lands, and a watchdog every ten minutes. The rest are business-hours or daily: the
hourly mail loop, the send queue, the **daily case tick at 09:00 CT that advances your ladder**, the
calendar sync, a bounce check three times a day, and two Monday-morning sweeps. On a host with no
scheduler at all, `python scheduler.py --run-forever` **is** the clock.

Four things worth knowing now:

- **`--dry-run` is the default.** Nothing touches your crontab until you type `--live`.
- **Every schedule in the manifest is written in UTC**, with the US-Central time it corresponds to
  spelled out next to it. If you are not on Central time, read those notes before assuming when a
  job runs.
- **It ships in `test` mode** (`MER_ENGINE_SEND: "test"` in `schedule.json`). Every drafted reply is
  redirected to your own mailbox, not a vendor's, **until you change that value and reinstall**. See
  section 5 for the switch.
- **Silence means healthy.** Most jobs print nothing when all is well, on purpose. Full detail
  always goes to a log next to the engine.

Full detail — editing the schedule, pointing it at a different Python, container prefixes,
troubleshooting: **`references/scheduler.md`**.

### "But my warranty expired" / "it's been two years" — bring it anyway

**This engine will not tell you your claim is too old.** That is the vendor's line, and it is not
this skill's job to say it for them. An expired warranty, a closed return window, even a
statute-of-limitations estimate that reads EXPIRED — none of them stop the pursuit here. They
change the *argument*, not the answer.

So when time has passed, you will be asked two more questions, and they are **not** a test:

- **Did you tell anyone at the time it broke?** A store, a rep, a chat, a phone call — who, roughly
  when, and what did they say?
- **What happened between then and now? What made you stop pushing?**

There is no wrong answer, and a vague one is fine. Say "I told the store and they were no help, so
I gave up" and you have just handed the case its strongest fact — because it means the vendor was
**on notice from the beginning** and the delay is *their* unresponsiveness, not your neglect. Other
answers work too: you were ill, you were moving, you didn't know you could do anything. Each maps
to a real argument the letters will make for you.

The one rule: **it has to be true.** The engine records what you actually say and puts it in a
letter over your signature. It will never invent a hardship for you, and you should not either — an
invented excuse is the one thing that can genuinely sink an otherwise good case.

And the line the letters will make on your behalf: *an expired warranty period is not an expired
obligation.* Something that failed after a handful of normal uses was not of merchantable quality
when it was sold, and that argument outlives any manufacturer's 12-month card.

Then it builds your **remedy map**: the specific levers that apply to *your* case — the industry
regulator (FCC for carriers, CFPB for banks and cards, DOT for airlines, NHTSA or your state lemon
law for cars, your state PUC or insurance commissioner), your governing state statute and its
pre-suit notice rule, any arbitration clause in the vendor's terms, a check for a known defect
pattern or existing class action, and your local county consumer office. Everything the ladder does
later is aimed at the targets on that map.

---

## 4. What it will and will not send in your name

**Read this twice.** The engine can send email from your own account, under your own name. Once a
message is delivered you cannot recall it. This is the only irreversible thing the system does.

Actions are sorted into three lanes. **`SKILL.md` §5 is the authoritative definition** — it carries
the test the engine applies to an action nobody listed in advance. This section is the plain-English
version of the same rules; where they ever differ, §5 wins.

The short form of the test: an action is **RED** if any of these is true — it reaches a party this
case has never contacted, it becomes a public or government record, it is visible to anyone besides
you and the vendor, it moves money, it forfeits a right, it ends the case, or it asserts a legal
claim. It is **YELLOW** only if all of these are true — the recipient already wrote to you on this
case, it is private one-to-one email, it asserts nothing new, and every fact in it is already on the
record. **Anything you cannot place with certainty is RED.**

### GREEN — always automatic, no permission asked

Nothing in this lane leaves your machine as a message to a vendor.

- Logging case facts, dates, and deadlines to your board
- Watching your inbox and matching mail to a case
- Classifying vendor replies
- Judging whether a reply is a real resolution
- Researching your remedy map
- **Drafting** every outbound letter (drafting is not sending)
- Computing business-day deadlines and scheduling nudges
- Running the daily sweep over your open cases

### YELLOW — sends by itself after a countdown

Two things, and only two:

1. **A reply to a vendor you are already engaged with** — where the vendor has written to you and
   the reply was classified `needs_info` or `partial`. The engine drafts a response, puts it in a
   queue with a `send_after` timestamp, and shows it to you. When the window elapses and you have
   not vetoed, it sends.
2. **Scheduled nudges** — a Tier-1 or Tier-2 follow-up on a case whose deadline is near or passed.
   Every fact in a nudge is pulled from the case record. If the vendor's email address cannot be
   resolved from the record, the nudge is listed but **never** queued.

**Default veto window: 3 hours** (`MER_ENGINE_WINDOW_HOURS=3`). You get three hours to kill it.

> ⚠️ **This lane is switched off in the box.** The shipped schedule sets `MER_ENGINE_SEND=test`, so
> every "sent" letter is actually redirected to your own mailbox with a `[TEST]` banner. Nothing in
> the Yellow lane reaches a vendor until you change that value in your own `schedule.json` and
> reinstall the schedule. If you are waiting for a letter to go out, check this first.

If a case's inbound mail is coming from someone who is not the vendor — a friend or relative whose
problem you are helping with — that mail is **never** auto-replied to. It is only surfaced to you.

### RED — never without your explicit yes, every single time

- **First contact with a new vendor.** The opening letter is always yours to approve.
- **Any regulatory filing** — AG, BBB, FTC, FCC, CFPB, state agency.
- **Any court petition.** It is prepared and handed to you. You file it. Nothing is ever filed
  automatically.
- **Spending money** — filing fees, return shipping, anything.
- **Signing anything.**
- **Sending a legal threat.**
- **Closing a case.**
- **A BBB complaint, a public social post, contacting a TV consumer reporter, or an elected
  official's casework form.** Every one of these is a public or third-party record.
- **Anything on the civil-rights track.** A civil-rights complaint is sworn under penalty of
  perjury, and you are the one swearing it.
- **Filing a chargeback — and also withdrawing one.** Both move money or give up a remedy.

One thing that is never a lever, in any lane: **you may not offer to withhold a regulator complaint,
a BBB complaint, or publicity in exchange for being paid.** Saying you intend to sue is normal and
lawful. Selling silence about a complaint is coercion, and the engine will not draft it.

Also red by classification: any reply classified `refused`, `legal_threat`,
`discrimination_signal`, or `refund` is surfaced to you and never auto-answered. Those are the four
moments where the wrong reflex reply costs you the case.

**In one sentence:** the engine will chase a conversation you already started; it will never start
one, file one, pay for one, or end one without you.

---

## 5. How to stop it

### Veto one queued send

```
python send_queue.py --list            # every queued send, its id, recipient, and countdown
python send_queue.py --veto <id>       # kill that one; it is dropped, not sent
```

A vetoed record is dropped permanently on the next pass. It does not come back.

### Stop all sending

The single switch is the environment variable `MER_ENGINE_SEND`, set per job in the schedule
manifest (`schedule.json` — copy `schedule.json.example` and edit your copy, then re-run
`python scheduler.py --install --live`):

| Value | Effect |
|---|---|
| `off` | **Nothing is sent, ever.** Queued items stay queued until you re-enable. This is the default in code. |
| `test` | Every draft is redirected **to you** with a `[TEST -> vendor@example.com]` subject prefix and a banner showing where it would have gone in live mode. Nothing reaches a vendor. |
| `live` | Real sends to real recipients. |

`test` is the setting to run in for your first week. You see the exact messages the engine would
send, addressed to you, before a vendor ever gets one.

### Stop everything

Take the clock away:

```
python scheduler.py --uninstall --live
```

That removes every job it installed — and nothing else. The engine has no other way to act; it does
nothing at all between scheduled runs.

---

## 6. The safety guarantees

These are enforced in code, not promised in a document. That distinction is the entire design
philosophy here: a rule written in a file is not a rule an automated system obeys.

**It cannot send the same message twice.** Every outbound passes through an idempotency ledger
before it goes anywhere. Each send is fingerprinted by (case, action, recipient, message body). If
that exact logical send already went out — from a retry, a second cron, a duplicate code path, a
restart — it is refused. This exists because a real incident sent a bank the same escalation email
twice, and a per-script flag would only have protected one script.

**It cannot go to court until every remedy on your map was tried.** Tier 4 is gated on a
completeness check against your case's own remedy map. Every applicable lever must be **attempted
and logged** — intending to do something, or doing it without recording it, does not count. Levers
that do not apply to your case were never on the map and are never owed.

**It cannot close a case on a promise.** "A refund of $129.99 has been issued, allow 3–5 business
days" is a sentence, not money. Closing requires confirmation that the funds actually posted —
either a notice from a **financial institution** (your bank or card issuer, not the vendor) dated
after the resolution, showing a credit of the expected amount, or your own logged confirmation that
you watched it land. A vendor's own claim that it credited you is recorded as weak evidence and
never establishes payment. This rule exists because a case once had two credits discussed at length
that never arrived — the non-arrival *was* the case.

**"We've escalated internally" is not a resolution.** Only a substantive outcome — refund issued,
replacement shipped — or an elapsed wait timer clears a phase gate. A vendor cannot pause your
ladder by saying they are looking into it.

**Deadlines have one source of truth.** The `MR Phase Deadline` property on the case. It is mirrored
onto Multica's native due date and from there to a calendar. You never have two dates disagreeing.

---

## 7. Limits and troubleshooting

**It only runs when its runtime runs.** The engine is cron jobs on an always-on machine. On the
reference deployment: the main loop runs hourly on weekday business hours (roughly 8am–5pm Central),
the case tick runs daily in the morning, the calendar sync runs daily. Nothing happens overnight, on
weekends, or while the host is off. If a deadline passes while the machine is down, it is caught on
the next run — late, not lost.

**Deadlines are business-day math.** "5 business days" from a Friday is not Friday + 5. Weekends and
US federal holidays are excluded. If a deadline looks wrong to you, check the calendar date rather
than the day count — this is a genuine, repeated source of error and it is worth your sanity check.

**It needs its own LLM credentials.** Classification and drafting call a model over HTTP. Without a
working key the system falls back to a keyword classifier: still safe, noticeably dumber, and
drafting stops working entirely (a case with no draft is surfaced to you for a manual reply rather
than being answered badly).

**A missed reply usually means a mail-matching miss.** Mail is matched to a case by thread headers,
then sender, vendor domain, and subject tokens. Vendors who answer from a different domain than the
one you wrote to — a helpdesk platform, an outsourced support vendor — can be missed. If a case has
gone quiet, search your mailbox yourself before assuming the vendor is silent.

**Nothing advances your phase without you.** The daily tick tells you a case is due to escalate; in
the current build it does not move the case up the ladder on its own. A case parked at Tier 1 will
keep being nudged at Tier 1 until you advance it. Check your board weekly.

**Bounces matter.** If a vendor address bounces, the letter was not delivered and your SLA clock
never started. Verify at least one working contact channel before you start counting days.

**Statute of limitations.** Every state caps how long you have to sue. It is typically years, not
months, but a case that sits for a year is a case you can lose by waiting. Note your purchase date
and your state's limit at intake.

---

## 8. What this is NOT

- **Not a lawyer, and not legal advice.** It applies published consumer-protection rules to facts
  you supply. It does not know your case the way a lawyer would, and it can be wrong about your
  state's law. For anything material — a meaningful amount of money, a signed arbitration agreement,
  a contract dispute, anything with a counter-claim — talk to an actual attorney.
- **Not representation.** It does not act for anyone else, and you should not use it to act for
  anyone else. Contacting a company on another person's behalf raises authorization questions this
  product deliberately avoids by having exactly one user: you.
- **Not a guaranteed outcome.** Escalation improves your odds. It does not create a legal right you
  did not already have.
- **Not a substitute for your judgment.** You are always the principal. Every letter goes out over
  your name, and you own what it says. Read the drafts.

If you are uncomfortable with a draft, veto it. That is what the window is for.
