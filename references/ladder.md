# The Ladder — phases, day-windows, and structural gates

The **tiers** are escalation levels; the **phases** are the concrete steps the engine runs on the
user's Multica board. Every regulator/statute/venue reference resolves per the user's jurisdiction
via `jurisdiction-lookup.md`. Nothing here is state-specific.

Legend for the autonomy lane on each phase (full model in `SKILL.md` §5):
🟢 fully autonomous · 🟡 auto after veto window · 🔴 explicit user YES required.

---

## Phase table

| # | Phase | Tier | Day | What happens | Lane | Gate to ENTER this phase |
|---|---|---|---|---|---|---|
| −1 | ONBOARDING | — | once | User profile + provision user's Multica + autonomy pref | 🟢 | first run only |
| 0 | TRIGGER | — | 0 | Case opened; issue created; source email logged | 🟢 | user starts a return / inbound detected |
| 1 | INTAKE | — | 0 | Run questionnaire (incl. discrimination Q); log answers | 🟢 | Phase 0 done |
| 2 | CASE FILE | — | 0 | Assign case_id; stamp jurisdiction/amount/vendor(s)/outcome; drop "(INTAKE INCOMPLETE)" | 🟢 | **all 🔴 intake fields answered** |
| 3 | REMEDY MAP | **0** | 0 | Build case-specific lever list (regulator, statute, pre-suit rule, arbitration clause, civil-rights, class/defect check) | 🟢 | Phase 2 complete |
| 4 | DISCOVERY | — | 0–1 | Find every vendor contact channel; **probe-verify no bounces**; identify seller AND manufacturer | 🟢 | Phase 3 complete |
| 5 | TIER 1 SEND | **1** | 1 | Draft + send first vendor demand (seller + manufacturer in parallel); 7-business-day SLA | 🔴 (new vendor) | Phase 4 complete, ≥1 verified contact |
| 6 | WAIT (Tier 1) | 1 | 1–7 | Daily SLA tick; **Day-3 follow-up nudge**; on reply → CLASSIFY | 🟡 (nudge) | Phase 5 sent |
| 7 | RETENTION ASK | 1 | 3–7 | "Transfer me to retention/refund authority" | 🟡 | Phase 6 elapsed to day ≥3 **or** a reply |
| 8 | TIER 2 | **2** | 7 | Exec + counsel letters **+ public/media + elected-official casework**; new tighter SLA | 🔴 (new channel) | Phase 7 cleared (no resolution) **and** day ≥7 |
| 9 | WAIT (Tier 2) | 2 | 7–14 | Daily SLA tick; on reply → CLASSIFY | 🟡 (nudge) | Phase 8 sent |
| 10 | TIER 3 REGULATORY | **3** | 14 | The **researched** regulators in parallel: industry (FCC/CFPB/…) + state consumer agency + AG + BBB + FTC | 🔴 (filing) | **Phase 9 marked cleared** (no resolution) **and** day ≥14 |
| — | CIVIL-RIGHTS TRACK | **3-D** | on flag | State civil-rights agency + statutory claim + advocacy org — **parallel, does not wait** | 🔴 (filing) | discrimination flagged in Phase 1 **and** facts support |
| 11 | WAIT (Regulatory) | 3 | 15–21 | Wait for regulator/review responses | 🟢 | Phase 10 complete |
| 12 | PRE-SUIT DEMAND | **3.5** | 21 | Statutory pre-suit notice (per state) + arbitration/Notice-of-Dispute threat | 🔴 (legal notice) | Phase 11 cleared (no resolution) |
| 13 | TIER 4 | **4** | 30+ | Chargeback (if in window) **∥** small-claims petition prepared → handed to user to file | 🔴 (spend + file) | Phase 12 cleared (no resolution) and notice period elapsed |
| 14 | CLOSE | — | any | Only on the user's explicit close phrase | 🔴 | explicit close; "escalated internally" ≠ close |

**Cross-cutting — CLASSIFY-AND-RESPOND** (fires on every inbound on a live case): 🟢 detect → 🟢
classify (refund / partial / refused / needs-info / legal-threat / discrimination-signal) → 🟢 log a
`RECORD ONLY` comment → 🟢 draft response → send per lane (🟡 to an engaged vendor, 🔴 if new
channel / filing / legal threat).

---

## Day windows (the current standard)

- **Tier 1 window = 5 business days, with a Day-3 follow-up nudge.** Count **business** days, then
  sanity-check the calendar end date before sending. ("7 business days from a Friday" ≠ "+7 calendar
  days" — this is a documented, repeated error.)
- Downstream tiers open on the **prior phase being marked cleared AND** the day threshold — both, not
  either. A gate is never opened by the calendar alone.
- These windows are defaults; a specific vendor letter may state its own SLA, in which case honor the
  window you put in writing (never shorten a promised deadline — that is a gate-jump).

---

## Phase 3 / Tier 0 — who actually writes the remedy map (added 2026-07-26)

`scripts/vps/remedy_map.py` **is** phase 3. `case_tick.py` calls it in `--live` for any case
sitting at `RemedyMap` with an empty `MR Remedy Map`, writes the lever list to that property via
`multica_api.set_properties`, and logs a `RECORD ONLY - NO ACTION REQUIRED` comment (routed to the
activity issue when the case has a live agent). Tier 0 **sends nothing** — it is research and
record-keeping. Dry-run writes nothing.

This closes a structural dead end: `remedy_gate` gates Tier 4 on the map and `case_tick` fails
CLOSED when it is empty, but nothing ever wrote one — so **court was unreachable by construction
for every case, forever**.

**The inclusion rule (this is the sharp edge).** `remedy_gate` treats every key in the map as
*owed* before court opens, so a lever that can never be attempted for this case blocks Tier 4
**permanently**. The test for including a lever is therefore not "is it plausible?" but *"can it be
attempted and logged for THIS case, today?"* Levers that fail it are excluded with a written reason
(`plan["excluded"]`), so the omission is a decision on the record and not an oversight. Notably:

- **no `industry_regulator`** unless a real regulator resolves (carrier→FCC, airline→DOT,
  auto→NHTSA + lemon law, utility→PUC, insurer→DOI, financing→CFPB). A non-regulated vendor gets none.
- **no `arbitration`** unless a clause is actually recorded; **no `nonprofit_mediation`** unless a
  local office is actually named; **no `civil_rights`** unless the flag is set *and* facts support it.
- **chargeback and court are NOT in the map.** Chargeback is a Tier-4 component on its own ~120-day
  clock (gate rule 4) and is dead on a cash purchase; court is the destination, not its own
  prerequisite. Both are reported as advisories so a cash case, or one whose window has closed, can
  still reach Tier 4.
- **§1.6 holds:** purchase age only statuses the chargeback window. It never removes a lever.

Levers are logged as done with `remedy_map.mark_attempted(issue, lever)` — idempotent, never
duplicating and never dropping an existing entry.

---

## Gate rules (structural, not prose)

1. A later-tier task is **not created** until the prior phase's task is marked `cleared`. Model each
   gate as a real Multica dependency / boolean on the issue — never a note an agent is trusted to obey.
2. "Cleared" = a **substantive resolution** (refund issued, replacement shipped) OR an **elapsed
   wait-timer with no resolution**. A vendor saying "escalated internally / transmitted to another
   department" does **not** clear a gate.
3. **Never auto-file in court.** The petition is prepared; the user files it. (Phase 13 is 🔴.)
4. **Chargeback is parallel, not sequential** — it runs from the card issuer's portal on its own
   ~120-day clock, independent of the court petition. If the purchase is older than the window, the
   chargeback path is dead; do not build strategy on it (see `court-and-chargeback.md`).
5. The **civil-rights track (3-D)** is the one branch that runs in parallel from the moment it's
   flagged — discrimination does not wait for the consumer ladder to fail.

---

## What advances a case with no human nudging (the daily tick)

The daily case-sweep autopilot, for every open case:
- reads `current_phase` + the phase's deadline + the reply state on the board,
- if a reply arrived → routes to CLASSIFY-AND-RESPOND (branch, don't advance),
- else if the wait-timer elapsed with no resolution → marks the phase cleared, creates/unlocks the
  next phase's task, and either drafts+queues the next outbound (🟡) or surfaces it for the user's
  YES (🔴),
- else → stays silent (no noise on a case that's simply waiting).

This is the generalized engine (Blueprint M7) that replaces per-case hand-armed crons and closes the
"silent-at-deadline" flaw.
