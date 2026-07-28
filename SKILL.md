---
name: merchandise-return
description: >
  Pursue YOUR OWN refund, exchange, repair or vendor dispute — your name, your accounts. Runs intake,
  a jurisdiction-aware remedy map, and an escalating letter ladder on your own Multica board. Triggers
  on "start a return", "get my money back from [vendor]", "escalate my [vendor] refund", "letter to
  [vendor]'s CEO", "where do we stand on my refund case". Own name only; never for a client, a
  relative, or any third party.
---

# Merchandise Return — a single-user, self-run returns engine

**Do NOT fire this skill for:** selling, listing, or pricing an item (that is a marketplace skill);
a dispute pursued **on someone else's behalf**; or a general project/status question that is not
about a return or refund ("where do we stand on the website build" is not this skill).

**One person, your own return, your own name.** Whoever installs it runs their OWN merchandise
returns with their OWN identity and accounts — they act for themselves on every letter, filing, and
decision, and the skill only ever asks for **their** own information.

**The skill runs the show; Multica is its engine.** Case state, replies, deadlines, and phase gates
live on the user's Multica board as **properties** — never in the agent's head, never in a comment,
never in a loose file. Every user's board is identical in structure and isolated from every other's.

All engine code ships in **`engine/`**. Every command below is run from that directory.

---

## §0 — ONBOARDING (first run, before any case)

**Onboarding is a script, not a checklist. Run it — do not hand-provision a board.**

```bash
export MULTICA_TOKEN=<your token>       # from ~/.multica/config.json after `multica login`
python engine/onboard.py                # DRY RUN by default — prints the plan, writes nothing
python engine/onboard.py --live         # actually provisions
python engine/onboard.py --selftest     # offline proof, stubbed API, no network
```

Full flow, question order, endpoints and known gaps: **`references/onboarding.md`**.

`onboard.py` is **idempotent** — re-running adopts what exists and creates only what is missing,
printing one `CREATED` / `ADOPTED` / `SKIPPED` line per item.

**A. Your Profile.** The script asks for the user's own details — **only theirs, never anyone
else's** — and writes them to `profile.json` (schema in `engine/profile.example.json`, loaded by
`mer_config`). **No secret is ever written to that file.**
- Full legal name (as it appears on purchases)
- **Email account for this case — the user SELECTS which of their own mailboxes to use.** Do not
  impose one.
- Phone, mailing address
- **State AND county of residence** — this drives the jurisdiction engine (AG, statute, small-claims
  venue, civil-rights agency). **No default. Never assume a state.** The script refuses to run
  without them rather than guess.

> **Dedicated mailbox — recommended, not required.** A separate returns inbox contains the blast
> radius of any autonomous send and keeps the reply-classifier clean. Offer it; honor the choice.

> ⚠️ **Mailbox OAuth is still manual.** `onboard.py` reports whether the token file exists and
> refuses to claim the user is set up when it does not, but it does not run the consent flow.
> Until that file exists the engine can read and send **nothing**.

**B. Multica setup.** `onboard.py` provisions the user's OWN workspace, a project to hold cases, and
the MR property schema. Properties resolve **by name**, which is what makes a board portable — a
fresh workspace has different ids but behaves identically. **Never rename one.**

### §0B — the MR property table

`onboard.py` creates these **nine** properties. (Its own header text still says "eight" — a stale
count, not a missing property. Nine are created.)

| Property | Type | Meaning |
|---|---|---|
| `MR Phase` | select: `Intake, CaseFile, RemedyMap, Tier1, Tier2, Tier3, PreSuit, Tier4, Closed` | The case state machine. `case_tick` reads this. |
| `MR Phase Deadline` | date | Current phase SLA deadline, business-day computed. |
| `MR Intake Complete` | checkbox | TRUE only when every 🔴 intake field is answered. Gates RemedyMap/Tier1. |
| `MR Awaiting User YES` | checkbox | A RED-lane action is queued and needs the user's explicit yes. |
| `MR Jurisdiction` | text | The user's state + county for THIS case. Never defaulted, never guessed. |
| `MR Discrimination Flag` | checkbox | Set only on a YES **with supporting facts**. Unlocks Tier 3-D. |
| `MR Remedy Map` | text | Tier 0 output: the lever keys that apply to this case. `case_tick` fails **closed** at PreSuit → Tier4 while empty. |
| `MR Remedy Type` | select: `Refund, Replacement, Repair, StoreCredit` | **The remedy the vendor ACTUALLY GRANTED — never the user's preference.** Blank until a vendor grants something; `refund_landed` / `close_case` fail closed while blank. The user's ranked ask lives in the intake comment, not here. |
| `MR Remedy Attempted` | text | The lever keys actually attempted AND logged. Tier 4 opens only when this covers every key in `MR Remedy Map`. |

**Three more properties are read or written by the engine but are NOT created by `onboard.py`.**
Create them by hand on the board (same names, exact types) before relying on them:

| Property | Type | Who uses it |
|---|---|---|
| `MR Last Vendor Reply` | date | Written automatically by `mer_engine` on every substantive inbound vendor reply; read by `case_tick`'s escalation-hold gate — a fresh reply HOLDS the ladder so the engine does not escalate over a vendor who is actually answering. |
| `MR Delay Explanation` | text | The §1.6 delay narrative, verbatim. **Fallback while it does not exist:** record the explanation as a `RECORD ONLY` comment on the case AND say out loud in your report that the property is missing. |
| `MR Money Parties` | text | The money path (`references/money-trail.md`). |

**C. Autonomy preference.** Let the user set how much the engine sends on its own (§5). Default:
**supervised-autonomous**. Record it on the user's Multica (workspace context or a pinned issue).

---

## §1 — INTAKE (per case)

**Open and follow `references/intake-questionnaire.md` — this section is a compression aid, not a
substitute.** Every field there is 🔴 BLOCKING or 🟡 parallel; do not advance past CASE FILE until
every 🔴 is answered. Record answers as comments on the case's Multica issue.

⚠️ **Three questions are the spine of every intake:**
1. **What's actually wrong with the item or service — in the user's own words.** The plain "tell me
   what happened" question comes first and is easy to silently drop when working from a summary.
2. **What have they already tried — and WHO did they speak to, and WHEN?** Named reps, what they
   promised, and — critically — whether they reported it **at the time it failed**. That is what
   turns a stale-looking case into one where the vendor was on notice from day one.
3. **What outcome do they want — refund, replacement, repair, or store credit, ranked?** The ladder
   is built backward from this. Never guess it. Say "store credit" specifically, not a vague
   "credit". **This ranked answer goes in the intake comment — it is NOT `MR Remedy Type`.**

The rest of the fields that sink cases when skipped:
- exact brand + model (+ size/capacity/variant), serial / IMEI / VIN
- purchase date, exact amount paid, **how they paid** (chargeback path or not)
- **damage history** — dropped / liquid / repaired / modified (ask directly and early)
- **discrimination check** — *"Do you believe you were treated differently because of your race,
  color, religion, sex, national origin, age, disability, or another protected characteristic? If
  yes — what specifically was said or done, by whom, and when?"* Capture the FACTS, never manufacture
  a claim. Read `references/jurisdiction-lookup.md` §"Civil-rights track" before setting the flag.
- **why the delay** — §1.6. Ask on EVERY case where time has passed. It is a lever, not a test.

---

## §1.6 — NEVER POLICE THE USER OUT OF THEIR OWN CLAIM

**This engine does not decide that a claim is too old, too late, or out of policy.** An expired
warranty, a closed return window, a statute-of-limitations estimate reading EXPIRED — none of these
stop the pursuit. They change the *argument*, not the answer. Where a support agent would say
*"sorry, that's outside our window"*, this skill says *"tell me what happened, and I'll put it in
the letter."*

Never tell the user "that's outside the warranty period", "the return window closed", "the statute
has run", or "you waited too long". The SoL watchdog, warranty dates and return windows are
**INFORMATIONAL, report-only, and gate NOTHING.** No timer here may close a case, refuse to draft, or
decline to escalate.

**Instead, ask:** *"Walk me through what happened between then and now. Did you tell anyone at the
time? What did they say? What made you stop pushing? There's no wrong answer — the reason is useful
to your case."* Record it verbatim as **`MR Delay Explanation`** (§0B fallback if absent), then use
it. Notice given at the time → **the vendor was on notice from day one and the delay is theirs**.
Discouraged into silence → **the vendor's handling caused the gap**. Didn't know → **discovery
rule**. Illness/bereavement/deployment → **equitable tolling**, and human weight with retention.
Failed after a handful of uses → **merchantability**, which survives an expired warranty.

Say it once, as fact, never pleaded: *"I raised this with [WHO] at the time, in [WHEN/HOW], and was
told [WHAT]. I stopped pursuing it because [REASON]. I am raising it again now because the underlying
defect was never remedied."* Full drafting blocks: `references/letter-templates.md` §0.

**This does NOT license fabrication.** Capture what the user actually said, in their words. Never
invent a hardship, never coach a better story, never assert a notice that was not given.

---

## §2 — REMEDY MAPPING (Tier 0 — research, runs before Tier 1)

From the user's jurisdiction + the case facts, build a **case-specific** lever list and write it to
`MR Remedy Map`. It identifies:
- the **industry-specific regulator** (FCC carriers, CFPB banks/cards/financing, DOT airlines,
  NHTSA/state lemon law autos, state PUC, state insurance commissioner…)
- the **governing state consumer statute** + any **mandatory pre-suit notice** requirement
- any **arbitration / Notice-of-Dispute clause** in the vendor's terms — and, on a recent account,
  the **30-day arbitration opt-out window** (`references/court-and-chargeback.md`; irreversible once
  missed, so check it at Tier 0 on every case, not at Tier 3.5)
- **civil-rights avenues** — only if the flag is set and the facts clear the bar (§3-D)
- a **class-action / documented-defect** check
- **local county consumer office + nonprofit mediation** options

Data source: `references/jurisdiction-lookup.md` (all 50 states + DC).

---

## §3 — The tier ladder

Full phase table, day windows, and gate conditions: `references/ladder.md`.
Court + chargeback math: `references/court-and-chargeback.md`.

| Tier | Name | What it does |
|---|---|---|
| **0** | Remedy Mapping | §2 — the case-specific lever list |
| **1** | Vendor direct | Seller **+ manufacturer in parallel** where both apply. **5-business-day SLA**, Day-3 nudge |
| **2** | Executive + Public | C-suite + counsel letters, then optionally public/media/elected-official casework |
| **3** | Regulatory (case-specific) | The **researched** regulators in parallel: industry regulator + state consumer agency + state AG + BBB + FTC/FCC |
| **3-D** | Civil-Rights track (conditional) | Only if discrimination is flagged **and** the facts clear the bar in `jurisdiction-lookup.md`. **Entirely 🔴** — see below |
| **3.5** | Statutory pre-suit demand | The legally-required notice for the user's state. Often settles here |
| **4** | Chargeback + small claims | Chargeback (if inside ~120 days) parallel to a prepared petition. **Never auto-filed — the user files** |

**Vendor "escalated internally" ≠ resolution.** Only a substantive outcome (refund issued,
replacement shipped) or an elapsed wait-timer clears a gate.

**⚠️ Tier 2's public levers are not free.** A TV consumer reporter, a public social post, and an
elected official's casework form are all 🔴 by the §5 test (public record, third party, visible
beyond user and vendor). None of them may be used **as a price** for payment — see §6.9.

**⚠️ Tier 3-D is 🔴 in its entirety and never fires automatically.** Before any civil-rights filing
is drafted, the user must confirm the specific facts in their own words and give a per-item YES. A
civil-rights complaint is **sworn under penalty of perjury**. Read the bar in
`references/jurisdiction-lookup.md` first — §1981 requires **intentional** discrimination and
**but-for** causation (*Comcast v. NAAAOM*, 2020), a high bar on a retail-return fact pattern.

---

## §4 — How Multica runs this (the engine, on the user's own board)

Drive everything through the user's Multica. **Properties are the state; comments are prose.**

```bash
# 1. READ every MR property on a case. <issue> is a UUID or a human id like MER-76.
python engine/multica_api.py --get MER-76

# 2. WRITE properties. THIS is how state changes. Names resolve case-insensitively.
python engine/multica_api.py --set MER-76 "MR Phase=Tier1" "MR Intake Complete=true"

# Long / multi-line / dollar-bearing values come from a FILE with a leading @:
python engine/multica_api.py --set MER-76 "MR Delay Explanation=@C:\path\note.txt"
# (to write a value that literally starts with '@', double it: "Note=@@handle")

# 3. Comments and issues — ALWAYS from a file, never an inline shell literal:
multica issue comment add <issue-id> --content-file <file>
multica issue create --title "..." --description-file <file> --allow-external-file --project <id>
multica issue comment list <issue-id> --output json
multica issue list --output json
```

`--set` **re-reads the board after writing and prints the verified values.** An HTTP 200 proves
nothing; the read-back does. Paste it into your report.
**Exit codes: `0` ok · `1` usage/bad value · `2` API or auth failure · `3` read-back mismatch.**

- **Case = one Multica issue.** Title carries the vendor + item; append `(INTAKE INCOMPLETE)` until
  every §1 blocking field is done, and remove it with a title edit **plus**
  `--set <issue> "MR Intake Complete=true"`.
- **Every vendor reply is logged as a comment** the moment it arrives, classified (refund / partial /
  refused / needs-info / legal-threat / discrimination-signal), and the engine writes
  `MR Last Vendor Reply`.
- **Phase advances are structural** — real gates on real properties, never prose.
- **Deadlines self-fire** via the daily case-sweep (`case_tick`).

### ⚠️ Client / third-party cases are FENCED OUT (undocumented until now)

`case_tick` **refuses to auto-advance** any case whose **title starts `CLIENT:`**, or whose
**description contains `CLIENT CASE`** in its opening block, or that carries an affirmative
client-case property. Such a case sits at its phase forever with no letters, no nudges, no
escalation — by design, because third-party contact needs written authorization this skill does not
collect.

**This skill is for the user's own purchases only.** If someone is helping a relative or a friend,
that is not a stalled ladder to debug — it is out of scope. Say so plainly rather than leaving them
staring at a case that never moves.

### ⚠️ Wake-agent safety
A comment on an issue with a **live agent assigned** wakes that agent and reads as an instruction.
Write **observations, not imperatives**; prefix pure status notes `RECORD ONLY — NO ACTION REQUIRED`,
or log to an unassigned activity issue, or unassign first.
**BUT always correct the case's OWN state too.** Title, status and **property** updates do NOT wake
the agent — only comments do. So whenever a real event happens (a shipment, a receipt, a reply),
update the case itself to reflect reality. Logging *only* to a side issue leaves the case showing
stale state: it looks un-actioned, and the system will tell the user to do what they already did.
The side-log is the audit trail; **the case's own properties are the source of truth.**

---

## §4.4 — Logging case events (shipments, receipts, tracking, calls)

When a physical thing happens on a case, log it in ONE standardized line, never an improvised reply:

`EVENT: <type> | <case> | <detail> | <date> | next: <what it triggers>`

- **Always read a tracking number back to the user to confirm before logging** — they are easy to
  misread from a photo. If uncertain, ask them to verify on the carrier site.
- On a shipment, set `MR Phase Deadline` once the vendor's receipt is confirmed (the SLA starts on
  their scan, not the drop-off) — with `--set`, not with a sentence.
- Wake-agent safe: log to the case issue, or to the activity issue if the case has a live agent.

## §4.5 — Inbound triage

Full pipeline: `references/inbound-triage.md`. In short:
1. **Match** to a live case (thread `References`/`In-Reply-To` first, then sender, vendor domain,
   subject tokens). Unmatched non-claim mail is dropped.
2. **Classify** (`engine/reply_classify.py`) → refund / partial / refused / needs_info /
   legal_threat / discrimination_signal.
3. **Prioritize:** 🔴 HIGH (refused / legal-threat / discrimination / refund-approved) → push to the
   user immediately + set `MR Awaiting User YES`. 🟡 MEDIUM (needs-info / partial) → draft +
   veto-window. ⚪ LOW → log only.
4. **New-claim detection:** an unmatched "get my money back" request → surface "possible new case —
   open one?" (never auto-open).
5. **Log** each matched inbound as a RECORD-ONLY classification comment.

*Surfacing priority (how loud) and send lane (who may send) are separate — a message can be HIGH
priority AND 🔴 lane.*

## §5 — Autonomy lanes

**Classify by test, not by list. An action is 🔴 RED if ANY of these is true:**
- it reaches a party this case has never contacted
- it becomes a public or government record
- it is visible to anyone other than the user and the vendor
- it moves money
- it forfeits a right or remedy
- it ends the case
- it asserts a legal claim or threat

**It is 🟡 YELLOW only if ALL are true:** the recipient has already written to us on this case · it
is private one-to-one email · it asserts no new claim · every fact in it is already on the case
record.

**Anything you cannot place with certainty is RED. Never invent a third lane.**

**🟢 GREEN — fully autonomous, always.** Everything that changes nothing outside the board: log
facts/dates/deadlines · watch the inbox · classify replies · advance internal phases on structural
gates · run remedy research · **draft** all outbound · fire the daily sweep · compute business-day
SLAs · schedule nudges. Drafting is green; sending never is.

**🟡 YELLOW — autonomous with a veto window (the user's default dial).** Draft → queue → post to the
board with a countdown → auto-send when the window elapses unless the user vetoes.

**🔴 RED — always an explicit per-item YES.** Worked examples of the test above: first contact with a
new vendor · any regulatory or civil-rights filing · a court petition · a BBB complaint · a public
social post · a TV consumer reporter · an elected official's casework form · spending money ·
signing anything · a legal threat · filing or **withdrawing** a chargeback · closing a case. A
retention *phone call* the user places themselves is theirs to make; a call **the engine places** is
RED.

> ⚠️ **The YELLOW lane is inert out of the box.** `engine/schedule.json.example` ships with
> `MER_ENGINE_SEND: "test"`, which redirects **every** send to the user's own mailbox with a `[TEST]`
> banner. Nothing reaches a vendor until the user edits that value in their installed
> `schedule.json` and reinstalls the schedule. Tell them this rather than letting them believe
> letters are going out.

---

## §6 — Failure-class rules (baked in)

1. **Read the RIGHT email.** Pull the specific message by sender/subject/ID — never report the
   top-unread as "the whole email."
2. **A rule written down is not a rule enforced.** Every gate is a structural dependency or a real
   boolean, not a sentence the agent is trusted to obey.
3. **Business-day SLA math.** Use `engine/businessday.py`. Count business days, then sanity-check the
   calendar end date before sending. "5 business days from a Friday" is not "+5 calendar days."
4. **Never manufacture a discrimination claim.** Capture facts; pursue only what the facts support.
5. **Vendor "escalated internally" never clears a gate.**
6. **Never police the user out of a claim (§1.6).**
7. **Writing a sentence is not performing an action.** `MR Phase: Tier1` in a comment changes
   **NOTHING** — the dashboard, `case_tick` and every gate read **PROPERTIES**, never comment text.
   After any state change you MUST run `python engine/multica_api.py --set …`, then paste the printed
   read-back into your report. **If you did not run the command, the state did not change, and you
   may not say it did.** (2026-07-28: an agent wrote "MR Phase: Tier1" into a comment and reported
   success. The case displayed INTAKE INCOMPLETE for a full day while it had already sent a demand
   letter and received a vendor reply.)
8. **Never pass case content through a shell literal.** Write to a temp file and pass
   `--content-file` / `--description-file` / a `@file` value. Dollar amounts, newlines and quotes do
   not survive interpolation, and a mangled amount in a demand letter is a factual error over the
   user's signature. (2026-07-28: `"$2,500"` became `",500"` because the shell expanded `$2`, and the
   mangled figure went into the record.)
9. **Never price a regulator complaint or publicity.** Threatening **litigation** in a demand is
   lawful and normal. Conditioning a **regulatory filing, a BBB complaint, media contact, or a public
   post** on payment — "pay me within 14 days or I file with the regulator" — is the classic shape of
   coercion and must never appear in an outbound. Regulator complaints are filed on their merits and
   **announced, never bargained**. A demand letter may state a court deadline; it may not sell
   silence.
10. **The only permitted send path is `mer_send.send()`.** `gmail_transport.send_mime` now **raises**
    without a single-use reservation token minted by `idempotency.reserve_send()`, and a coarse
    48-hour `(case, recipient)` cooldown blocks a second letter on the same case even when the
    wording differs. (The old key was a body hash, so a reworded duplicate slipped through and a
    vendor was mailed twice on 2026-07-28.) Reservations are two-phase — written pending, then
    committed on a confirmed send or released on a transport error — so a failed send does not burn
    the letter forever. **Never import `gmail_transport` and call it directly.**
11. **Not legal advice.** This skill applies published consumer rules to facts the user supplies,
    over the **user's** signature. Verify every statutory citation live before it goes in a letter.
    For a signed arbitration agreement, a counter-claim, or an amount that matters, the user should
    talk to an attorney.

---

## §7 — Engine modules, the clock, and structural rules

All modules live in **`engine/`**.

**Core loop:** `mer_engine.py` · `case_tick.py` · `mer_hotpath.py` · `send_queue.py` + `mer_send.py`
· `nudge.py`. **Judgment:** `classify_llm.py` · `resolution_check.py` · `refund_landed.py` ·
`remedy_map.py`. **Read/ingest:** `gmail_fetch.py` · `pdf_text.py` · `image_text.py` ·
`reply_classify.py` · `inbox_watcher.py` · `unmatched_review.py` · `new_claim_draft.py`.
**Safety/data:** `multica_api.py` · `idempotency.py` · `businessday.py` · `remedy_gate.py` ·
`sol_watchdog.py` · `dup_guard.py` · `delivery_check.py` · `sync_deadlines.py` +
`multica_calendar_sync.py` · `heartbeat.py` · `run_tests.py`. **The clock:** `scheduler.py` +
`schedule.json.example`.

Key rules:
- **Deadlines have ONE source of truth:** the `MR Phase Deadline` property. `sync_deadlines.py`
  mirrors it onto Multica's native `due_date`; the calendar sync reads `due_date`. Never set one
  without the other — use the sync. (The Multica issue-update endpoint is **PUT**; PATCH/POST → 405.)
- **Tier 4 (court) is gated on `remedy_gate.remedy_complete()`** — court is unreachable until every
  applicable lever from the Tier-0 map is attempted AND logged.
- **`run_tests.py`** runs every module's self-test; green before any change ships.
- **Deadlines can auto-post to the user's own Google Calendar** (`multica_calendar_sync.py`) if they
  set `calendar_id` during onboarding — optional, and always their own calendar.

### The schedule (read `engine/schedule.json.example` before describing it)

Every expression in the manifest is **UTC**, with the US-Central business intent documented per job.
Do not describe this engine as "business hours only" — most of it is not.

| Job | Schedule (UTC) | Reality |
|---|---|---|
| `mer-hotpath` | `* * * * *` | **every minute, 24/7** — fires the engine the moment a reply lands |
| `mer-heartbeat` | `*/10 * * * *` | every 10 min, 24/7 — watchdog on every other job |
| `mer-engine` | `0 13-22 * * 1-5` | hourly, = 08:00–17:00 CT Mon–Fri |
| `mer-send-queue` | `*/10 13-22 * * *` | every 10 min daily, = 08:00–17:59 CT |
| `mer-case-tick` | `0 14 * * *` | **daily, = 09:00 CT — auto-advances the ladder** |
| `mer-calendar-sync` | `30 14 * * *` | daily, = 09:30 CT |
| `mer-delivery-check` | `0 13,17,21 * * *` | 3×/day bounce sweep |
| `mer-sol-watchdog` | `0 15 * * 1` | Mondays, = 10:00 CT — read-only |
| `mer-unmatched-review` | `30 15 * * 1` | Mondays, = 10:30 CT — read-only |

CT offsets shift with daylight saving; the manifest documents both. Install with
`python engine/scheduler.py --install` (dry run) then `--install --live`. Details:
`references/scheduler.md`.

## Cross-references
`references/intake-questionnaire.md` · `references/ladder.md` · `references/court-and-chargeback.md`
· `references/jurisdiction-lookup.md` · `references/inbound-triage.md` · `references/vendor-contacts.md`
· `references/letter-templates.md` · `references/money-trail.md` · `references/onboarding.md` ·
`references/scheduler.md`

Companion skills (not absorbed): `active-case-log`, `vendor-dispute-investigation`,
`inbound-phishing-recognition`, `email-draft-then-review`.
