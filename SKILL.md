---
name: merchandise-return
description: >
  A complete, self-contained merchandise-return engine for a SINGLE user pursuing their OWN
  refund/exchange/vendor-dispute — under their own name, email, phone, and accounts. Onboards the
  user (identity, own Multica board), runs a jurisdiction-aware intake, builds a case-specific
  remedy map (regulator, statute, civil-rights avenues, class-action check), escalates a multi-tier
  ladder (vendor -> executives -> regulators -> conditional civil-rights track -> pre-suit demand ->
  chargeback/small-claims) using the user's own Multica as system of record and autopilot. Own
  name, own info only -- no representation, no client. Packaged for any recipient to run for
  themselves. Triggers on "start a return", "get my money back from [vendor]", "escalate [vendor]",
  "send a letter to [vendor]'s CEO", "where do we stand on my [case]". Runs in Claude Code,
  OpenCode, Telegram, Multica. Companion: active-case-log, vendor-dispute-investigation,
  inbound-phishing-recognition.
---

# Merchandise Return — a single-user, self-run returns engine

**One person, your own return, your own name.** This skill is a self-contained product. Whoever
installs it runs their OWN merchandise returns with their OWN identity and accounts — you act for
yourself on every letter, filing, and decision, and the skill only ever asks for **your** own
information. That keeps it simple and safe to hand to anyone, with no connection back to whoever
gave it to them.

**The skill runs the show; Multica is its engine.** The user operates their OWN Multica account.
Case state, replies, deadlines, and phase gates all live on the user's Multica board — never in the
skill's head or in loose files. Every user's board is identical in structure and isolated from every
other user's.

---

## §0 — ONBOARDING (first run, before any case)

**Onboarding is a script, not a checklist. Run it — do not hand-provision a board.**

```bash
export MULTICA_TOKEN=<your token>          # from ~/.multica/config.json after `multica login`
python3 scripts/vps/onboard.py             # DRY RUN by default — prints the plan, writes nothing
python3 scripts/vps/onboard.py --live      # actually provisions
```

Full flow, question order, endpoints and the known gaps: **`references/onboarding.md`**.

`onboard.py` is **idempotent** — re-running adopts what exists and creates only what is missing,
printing one `CREATED` / `ADOPTED` / `SKIPPED` line per item. `--dry-run` is the default; `--live`
is required to create anything. Prove it offline with `python3 scripts/vps/onboard.py --selftest`.

**A. Your Profile.** The script asks for the user's own details — **only theirs, never anyone
else's** — and writes them to `profile.json` (the schema in `profile.example.json`, loaded by
`mer_config`). **No secret is ever written to that file.**
- Full legal name (as it appears on purchases)
- **Email account for this case — the user SELECTS which of their own email accounts to use.** Do not
  impose one. The user can pick an everyday inbox or a dedicated one (see note).
- Phone, mailing address
- **State AND county of residence** — this drives the jurisdiction engine (AG, statute, small-claims
  venue, civil-rights agency). **No default. Never assume a state.** The script refuses to run
  without them rather than guess.

> **Dedicated mailbox — recommended, not required.** A separate returns inbox contains the blast
> radius of any autonomous send and keeps the reply-classifier clean. Offer it during onboarding, but
> **the account is always the user's choice** — if they pick their main inbox, honor that and tighten
> the send lanes accordingly.

> ⚠️ **Mailbox OAuth is still manual.** `onboard.py` reports whether the token file exists and
> refuses to claim the user is set up when it does not, but it does not run the consent flow itself.
> Until that file exists the engine can read and send **nothing**.

**B. Multica setup.** The engine requires the user's OWN Multica account. `onboard.py` provisions it:
- **Workspace** — adopted if it already exists (by id, then slug, then name), else created.
- **Project** to hold cases — adopted by title, else created.
- **The MR property schema, created on the board** — this is the part that did not exist before and
  without which the engine cannot run:

  | Property | Type |
  |---|---|
  | `MR Phase` | select: `Intake, CaseFile, RemedyMap, Tier1, Tier2, Tier3, PreSuit, Tier4, Closed` |
  | `MR Phase Deadline` | date |
  | `MR Intake Complete` | checkbox |
  | `MR Awaiting User YES` | checkbox |
  | `MR Jurisdiction` | text |
  | `MR Discrimination Flag` | checkbox |
  | `MR Remedy Map` | text |
  | `MR Remedy Attempted` | text |

  Properties resolve **by name**, which is what makes a board portable — a fresh workspace has
  different ids but behaves identically. **Never rename one.** `MR Remedy Map` and
  `MR Remedy Attempted` are load-bearing: `case_tick` fails *closed* at `PreSuit → Tier4` while
  either is empty, so without them the ladder cannot reach chargeback or small claims at all.

- If the user has no Multica account, they sign up at <https://multica.ai> and run `multica login`
  first — there is no API to create an account from a token you do not yet have.
- The script confirms out loud which workspace/project their cases will live in. Their board is
  theirs alone.

**C. Autonomy preference.** Let the user set how much the engine sends on its own (see §5). Default:
**supervised-autonomous** — drafts everything, auto-sends already-engaged-vendor follow-ups after a
veto window, and always asks before a brand-new outbound, a filing, spending, or signing. This one is
*not* a profile field — record it on the user's Multica (workspace context or a pinned issue) so
every case inherits it.

---

## §1 — INTAKE (per case)

**Open and follow `references/intake-questionnaire.md` for the actual questioning — this section
is a compression aid, not a substitute for it.** Every field there is 🔴 BLOCKING or 🟡 parallel;
do not advance past CASE FILE until every 🔴 is answered. Record answers as comments on the case's
Multica issue.

⚠️ **Three questions are the spine of every intake and must always be asked, verbatim in spirit,
even if you never open the reference file:**
1. **What's actually wrong with the item or service — in the user's own words.** (§ref 3.1) Not
   "from-start vs later" (that's a follow-up classifier, § ref 3.2) — the plain "tell me what
   happened" question comes first and is easy to silently drop when working from a summary instead
   of the full questionnaire. This was found missing from an earlier version of this very
   compressed list on 2026-07-26.
2. **What have they already tried — and WHO did they speak to, and WHEN?** (§ref 4.1, 4.3, 4.5,
   4.6) Troubleshooting done, prior contact with the seller/manufacturer, named reps and what they
   promised, and — critically — whether they reported it to anyone AT THE TIME it failed. That last
   part is what turns a stale-looking case into one where the vendor was on notice from day one.
3. **What outcome do they want — refund, replacement, repair, or store credit, ranked if more
   than one?** (§ref 5.1) The whole ladder is built backward from this. Never guess it. Use
   "store credit" specifically, not a vague "credit" — it's one of the four `MR Remedy Type`
   options the board tracks, and `refund_landed` requires a real code/card/certificate before a
   store-credit case can close, not just the vendor's word that credit was issued.

The rest of the fields that sink cases when skipped:
- exact brand + model (+ size/capacity/variant), serial / IMEI / VIN
- purchase date, exact amount paid, **how they paid** (chargeback path or not)
- **damage history** — dropped / liquid / repaired / modified (ask directly and early)
- **discrimination check** — *"Do you believe you were treated differently or unfairly because of
  your race, color, religion, sex, national origin, age, disability, or another protected
  characteristic? If yes — what specifically was said or done, by whom, and when?"* Capture the
  FACTS, never manufacture a claim. A yes with supporting facts unlocks the civil-rights track (§3).
- **why the delay** — see §1.6. Ask it on EVERY case where time has passed. It is a lever, not a test.

---

## §1.6 — NEVER POLICE THE USER OUT OF THEIR OWN CLAIM

**This engine does not decide that a claim is too old, too late, or out of policy. It is not the
vendor's compliance department, and it is not the user's opponent.** A warranty that has expired, a
return window that has closed, a statute-of-limitations estimate that reads EXPIRED — **none of
these stop the pursuit.** They change the *argument*, not the answer.

**The rule:** where a normal support agent would say *"sorry, that's outside our window"*, this
skill says *"tell me what happened, and I'll put it in the letter."*

**Forbidden.** Never tell the user, in any words:
- "that's outside the warranty period, so there's nothing to do"
- "the return window closed, this isn't worth pursuing"
- "the statute of limitations has run, you don't have a case"
- "you waited too long"

The SoL watchdog, warranty dates and return windows are **INFORMATIONAL** — they tell the user and
the drafter which lever to reach for. They are report-only and they **gate nothing**. No timer in
this system may close a case, refuse to draft, or decline to escalate.

**Instead — collect the explanation and use it.** Whenever time has passed, ask plainly and
without judgment:

> *"Walk me through what happened between then and now. Did you tell anyone at the time — a store,
> a rep, a chat, a phone call? What did they say? And what made you stop pushing? There's no wrong
> answer here — I'm asking because the reason is useful to your case, not because it counts against
> you."*

Record it verbatim on the case as **`MR Delay Explanation`**. Then **use it in every outbound**,
because a delay with a reason is a fundamentally different fact-pattern from a delay without one:

| What the user says | What it becomes in the letter |
|---|---|
| *"I told the store at the time and they didn't help"* | **Timely notice was given.** The vendor was on notice from the start; the delay is the vendor's unresponsiveness, not the customer's neglect |
| *"I got discouraged and gave up"* | The vendor's own handling **caused** the gap — a company should not profit from having stonewalled someone into silence |
| *"I didn't know I could do anything"* | Supports the **discovery rule** — a clock that runs from when the consumer knew or reasonably should have known |
| *"I was ill / bereaved / deployed / moving"* | Equitable-tolling framing, and plain human weight with a retention team |
| *"It only failed after a handful of uses"* | Goes to **merchantability** — the goods were never fit for ordinary use, which survives an expired manufacturer warranty |

**Say it in the letter, once, plainly and without apology** — asserted as a fact, never pleaded:

> *"I raised this with [WHO] at the time, in [WHEN/HOW], and was told [WHAT]. I stopped pursuing it
> because [REASON]. I am raising it again now because the underlying defect was never remedied."*

**And the closing line that keeps a stale-looking case alive:** an expired *warranty* is not an
expired *obligation*. Goods that failed after a handful of ordinary uses were never merchantable in
the first place, and the implied warranty of merchantability — plus the user's state consumer
statute — does not evaporate because a manufacturer's own 12-month card ran out.

**One thing this does NOT license: fabrication.** Capture the explanation the user actually gives,
in their words. Never invent a hardship, never coach them toward a better story, and never assert a
notice that was not given. Same rule as the discrimination question in §1 — the facts are the
leverage, and an invented one is a liability the user signs their name to.

---

## §2 — REMEDY MAPPING (Tier 0 — research, runs before Tier 1)

From the user's jurisdiction + the case facts, build a **case-specific** lever list and write it as
a structured comment on the issue. This configures which later-tier targets actually fire — the
ladder is never generic. It identifies:
- the **industry-specific regulator** (FCC for carriers, CFPB for banks/cards/financing, DOT for
  airlines, NHTSA/state lemon law for autos, state PUC, state insurance commissioner…)
- the **governing state consumer statute** + any **mandatory pre-suit notice** requirement
- any **arbitration / Notice-of-Dispute** clause in the vendor's terms
- **civil-rights avenues** (if the intake discrimination flag is set)
- a **class-action / documented-defect** check (a known defect pattern is major leverage)
- **local county consumer office + nonprofit mediation** options

Data source: `references/jurisdiction-lookup.md` (nationwide, all 50 states).

---

## §3 — The tier ladder (all targets resolve per the user's jurisdiction)

Full phase table, day windows, and gate conditions: `references/ladder.md`.
Court + chargeback math: `references/court-and-chargeback.md`.

| Tier | Name | What it does |
|---|---|---|
| **0** | Remedy Mapping | §2 — the case-specific lever list |
| **1** | Vendor direct | Seller **+ manufacturer in parallel** where both apply. 7-business-day SLA, Day-3 nudge |
| **2** | Executive + Public | C-suite + counsel letters **+ local TV consumer reporter + public social + elected-official constituent casework** |
| **3** | Regulatory (case-specific) | The **researched** regulators in parallel: industry regulator + state consumer agency + state AG + BBB + FTC/FCC |
| **3-D** | Civil-Rights track (conditional, **parallel** from flag) | Only if discrimination flagged **and** facts support: state civil-rights agency + statutory claim (each state's own) + advocacy org. Runs alongside, doesn't wait |
| **3.5** | Statutory pre-suit demand | The legally-required notice (per the user's state) + arbitration threat. Often settles here |
| **4** | Chargeback + small claims | Chargeback (if inside ~120 days) parallel to a prepared petition. **Never auto-filed — the user files** |

**Vendor "escalated internally" ≠ resolution.** Only a substantive outcome (refund issued,
replacement shipped) or an elapsed wait-timer clears a gate.

---

## §4 — How Multica runs this (the engine, on the user's own board)

Drive everything through the user's Multica via the CLI (headless; no window-focus theft).

```
multica issue create  --title "..." --description-file <file> --allow-external-file --project <id>
multica issue comment add <issue-id> --content "..."
multica issue comment list <issue-id> --output json
multica issue list --output json
```

- **Case = one Multica issue.** Title carries the vendor + item; append `(INTAKE INCOMPLETE)` until §1
  blocking fields are done.
- **State lives in the issue**, not in your head: `current_phase`, `jurisdiction`, `amount`,
  `vendor(s)`, `desired_outcome`, per-phase `cleared` flags, remedy map. Use issue properties/metadata
  where available, else a structured description block.
- **Every vendor reply is logged as a comment** the moment it arrives, classified (refund / partial /
  refused / needs-info / legal-threat / discrimination-signal).
- **Phase advances are structural.** A later-tier task is not created until the prior phase's task is
  marked cleared — real Multica dependencies, never prose.
- **Deadlines self-fire** via the daily case-sweep autopilot on the user's board: for each open case,
  check the phase timer + reply state → advance, nudge, or branch to classify.

### ⚠️ Wake-agent safety
A comment on an issue with a **live agent assigned** wakes that agent and reads as an instruction.
Write **observations, not imperatives**; prefix pure status notes `RECORD ONLY — NO ACTION REQUIRED`,
or log to an unassigned activity issue, or unassign first.
**BUT always correct the case's OWN state too.** Title, status, and **property** updates do NOT wake
the agent (only comments do) — so whenever a real event happens (a shipment, a receipt, a reply),
update the case itself to reflect reality (e.g. flip the title from `awaiting King ship` to
`SHIPPED …`). Logging *only* to a side issue leaves the case showing stale state — it looks
un-actioned, and the system (and Lisa) will tell King to do what he already did. The side-log is the
audit trail; **the case's own state is the source of truth and must never drift from reality.**
(Lesson: 2026-07-25 — a shipped Nike return still read `awaiting King ship/decline` because the log
went only to MER-16, so Lisa told King he still needed to ship it.)

---

## §4.4 — Logging case events (shipments, receipts, tracking, calls)

When a physical thing happens on a case — the user mails an item back, a tracking number or
receipt/photo arrives, a call is placed — log it in ONE standardized line, never an improvised
chat reply:

`EVENT: <type> | <case> | <detail> | <date> | next: <what it triggers>`

e.g. `EVENT: shipment | MER-2 | UPS 1Z492Y920361718000 → Rebound/Nike | 2026-07-24 | next: 7-business-day SLA starts on the vendor's receipt scan`.

- **Always read a tracking number back to the user to confirm before logging** — they are easy to
  misread from a photo. If uncertain, say so and ask them to verify on the carrier site.
- On a shipment, set the case's `MR Phase Deadline` once the vendor's receipt is confirmed (the SLA
  starts on their scan, not the drop-off).
- Wake-agent safe: log to the case issue, or to the activity issue if the case has a live agent.

## §4.5 — Inbound triage (so important case mail isn't lost in the sea)

Every message in the connected mailbox runs through triage so the mail that matters surfaces and the
rest is ignored. Full pipeline: `references/inbound-triage.md`. In short:
1. **Match** to a live case (thread `References`/`In-Reply-To` header first, then sender, vendor
   domain, subject tokens). Unmatched non-claim mail is dropped.
2. **Classify** (`reply_classify.py`) → refund / partial / refused / needs_info / legal_threat /
   discrimination_signal.
3. **Prioritize + surface:** 🔴 HIGH (refused / legal-threat / discrimination / refund-approved) →
   push to the user immediately + set `MR Awaiting User YES`. 🟡 MEDIUM (needs-info / partial) →
   draft + veto-window. ⚪ LOW → log only.
4. **New-claim detection:** an unmatched "get my money back" request from a known contact → surface
   "possible new case — open one?" (never auto-open).
5. **Log** each matched inbound as a RECORD-ONLY classification comment on the case.
6. **Cadence:** once **every hour, Mon–Fri, 8 AM–5 PM Central** (business hours only; idle nights/weekends).

*Surfacing priority (how loud) and send lane (who may send) are separate — a message can be HIGH
priority AND 🔴 lane (tell the user now, never auto-reply).*

## §5 — Autonomy lanes (you act for yourself; you approve your own sends)

Because it is always the user's own name and accounts, there is no third-party exposure — but sending
the wrong thing is still hard to undo, so autonomy is graduated:

**🟢 GREEN — fully autonomous, always.** Log facts/dates/deadlines · watch inbox · detect + classify
replies · advance internal phases on structural gates · run remedy research · draft all outbound ·
fire the daily sweep · compute business-day SLAs · schedule nudges.

**🟡 YELLOW — autonomous with a veto window (the user's default dial).** Outbound to an
**already-engaged** vendor and routine follow-ups: draft → queue → post to the board with a countdown
→ auto-send when the window elapses unless the user vetoes.

**🔴 RED — always an explicit per-item YES from the user.** First contact with a **new vendor** ·
any **regulatory filing** or **court petition** · **spending money** (fees, shipping) · **signing**
anything · sending a **legal threat** · **closing** a case.

---

## §6 — Failure-class rules (baked in)

1. **Read the RIGHT email.** Pull the specific message by sender/subject/ID — never report the
   top-unread as "the whole email."
2. **A rule written down is not a rule enforced.** Every gate is a structural Multica dependency or a
   real boolean, not a sentence the agent is trusted to obey.
3. **Business-day SLA math.** Count business days, then sanity-check the calendar end date before
   sending. "7 business days from a Friday" is not "+7 calendar days."
4. **Never manufacture a discrimination claim.** Capture facts; pursue only what the facts support.
5. **Vendor "escalated internally" never clears a gate.**
6. **Never police the user out of a claim (§1.6).** No warranty date, return window, or
   statute-of-limitations estimate may close a case, block a draft, or decline an escalation. They
   are informational and gate nothing. Where a vendor would say "outside our window", this skill
   collects the user's explanation for the delay and puts it in the letter.
7. **Every outbound passes the idempotency guard first.** Before ANY send, call
   `idempotency.reserve(case, action, recipient, body)`; send only if it returns ok. This makes a
   double-send structurally impossible from any code path — the generalized fix for the
   2026-07-18 bank-double-email, not a per-script flag.

---

## §7 — Packaging & deployment (one source, portable)

Canonical master: `Multica/skills/merchandise-return/`. The skill is self-contained portable
knowledge — hand the folder to anyone and it runs on THEIR Multica with THEIR identity. Deploy from
the master to whichever surface the user runs in:
- **Claude Code** — readable in-repo, or copied into their Claude skills dir.
- **Telegram / an agent runtime** — copy the folder into the runtime's skills dir.
- **Multica** — `multica skill import` into the user's own workspace.

**Edit the master only, then redeploy.** Never hand-edit a copy — divergence between copies is the
original sin that split this skill's two predecessors apart.

## Cross-references
- `references/intake-questionnaire.md` · `references/ladder.md` · `references/court-and-chargeback.md`
  · `references/jurisdiction-lookup.md` · `references/inbound-triage.md` · `references/vendor-contacts.md`
  · `references/letter-templates.md` · `references/money-trail.md` · `references/scheduler.md`

### §8.1 — Engine module roster (scripts/vps/, on the 24/7 VPS)
Core loop: `mer_engine.py` (inbound→classify→resolution→draft→queue), `case_tick.py` (deadline
sweep), `send_queue.py` + `mer_send.py` (veto-window + gated send: off/test/live), `nudge.py`
(time-based follow-ups that replace the per-case crons). Judgment: `classify_llm.py`,
`resolution_check.py`, `refund_landed.py` (money actually posted before close). Read/ingest:
`gmail_fetch.py`, `pdf_text.py`, `image_text.py` (vision-LLM OCR), `inbound-triage`, `unmatched_review.py`
(weekly missed-mail sweep), `new_claim_draft.py`. Safety/data: `idempotency.py`, `businessday.py`,
`remedy_gate.py` (court gate), `sol_watchdog.py` (statute-of-limitations), `dup_guard.py`,
`sync_deadlines.py` + `multica_calendar_sync.py`, `delivery_check.py` (bounce), `run_tests.py`
(green-before-ship), `refresh_sessions.py` (SOUL/skill reload advisor).
**The clock:** `scheduler.py` + `schedule.json.example` — the declarative job manifest and the
installer that registers it on cron, systemd timers, Windows Task Scheduler, or its own foreground
loop. Every other module above is inert without it: nothing inside the engine can trigger on a
non-event (a vendor's silence, a limitations date, a bounce that arrived after acceptance), and
those are the failures that kill cases. `--dry-run` is the default; see `references/scheduler.md`.

## §8 — Engine modules & structural rules (the running parts)
The skill's automation lives in `scripts/vps/` on the 24/7 VPS (see BLUEPRINT.md). Key rules:
- **Deadlines have ONE source of truth:** the `MR Phase Deadline` property. `sync_deadlines.py` mirrors
  it onto Multica's native `due_date`; the Google Calendar sync reads `due_date`. Never set one without
  the other — use the sync. (Multica issue-update endpoint is **PUT** — PATCH/POST return 405.)
- **SLA math uses `businessday.py`** — never count calendar days by hand (the "Day 7" bug class).
- **Tier 4 (court) is gated on `remedy_gate.remedy_complete()`** — court is unreachable until every
  applicable lever from the Tier-0 remedy map is attempted AND logged. Structural, not a note.
- **Deadlines auto-post to King's Google Calendar** (`multica_calendar_sync.py`, daily cron) and
  actionable cases push to Telegram (`case_tick` cron) — 24/7, laptop-independent.
- **`run_tests.py`** runs every module's self-test; green before any change ships.
- Companions (not absorbed): `active-case-log`, `vendor-dispute-investigation`,
  `inbound-phishing-recognition`, `email-draft-then-review`.
