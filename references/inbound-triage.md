# Inbound Triage — don't let important case mail drown in the sea

The job: every message that lands in the user's connected mailbox gets sorted so the **important
case emails surface immediately** and everything else (marketing, digests, newsletters) is ignored.
This is what stops a vendor's resolution or a refusal from sitting unseen for a day under 25 junk
emails.

Two axes, kept separate:
- **Surfacing priority** = how loudly to alert the user (HIGH/MEDIUM/LOW).
- **Send lane** = who may send the response (🟢/🟡/🔴, per SKILL.md §5).
A message can be HIGH priority *and* 🔴 lane (e.g. a legal threat — tell the user NOW, but never
auto-reply).

---

## The pipeline (runs on every inbound)

**1. MATCH to a live case** — in order of reliability:
   1. the email thread's `References` / `In-Reply-To` header (most reliable — it ties a reply to the
      exact case thread)
   2. sender address recorded on the case
   3. the vendor domain from the case's Tier-0 remedy map
   4. subject tokens (case id, claim #, RMA/ticket number)
   Mail that matches nothing **and** isn't a claim (step 4) is dropped — no noise.

**2. CLASSIFY** the matched mail with `scripts/reply_classify.py` →
   `refund · partial · refused · needs_info · legal_threat · discrimination_signal · other`.
   (Heuristic fallback; live path uses Claude for judgment on the full thread.)

**3. PRIORITIZE + SURFACE** by category:
   | Priority | Categories | Action |
   |---|---|---|
   | 🔴 **HIGH — surface immediately** | refused · legal_threat · discrimination_signal · refund(approved) | Push to the user (Telegram) + bump the issue priority + set `MR Awaiting User YES`. Never-miss. |
   | 🟡 **MEDIUM** | needs_info · partial | Draft the response; queue on the veto window (🟡 lane). |
   | ⚪ **LOW** | acknowledgements, routine | Log only. |

**4. NEW-CLAIM DETECTION** — unmatched mail from a known contact that reads like a get-my-money-back
   request (contains refund / denied / return / warranty / chargeback / "help me get…") → surface
   **"possible new case — open one?"**. The engine **never auto-opens a case** — it asks. This is the
   gap that would otherwise lose a brand-new matter entirely.

**5. LOG** — every matched inbound is posted to the case's Multica issue as a **RECORD-ONLY**
   classification comment (wake-agent safe — see SKILL.md §4), carrying: category, suggested next
   action, and its send lane.

**6. CADENCE** — the watcher runs **once every hour, Monday–Friday, 8:00 AM–5:00 PM Central**
   (business hours only; idle on evenings and weekends). Cron: `0 13-22 * * 1-5` UTC
   (13:00–22:00 UTC = 08:00–17:00 CT during CDT). This keeps a resolution or refusal from sitting
   unseen for a day, without polling around the clock.

---

## Anti-noise / correctness rules
- **Match by thread header first** — the single most reliable signal; subject/sender are fallbacks.
- **A shared everyday inbox is allowed** but then triage tightens: only case-linked or new-claim mail
  is ever surfaced; all else is ignored. (A dedicated mailbox is still recommended — SKILL.md §0.)
- **De-dupe** — a reply already logged/classified is not surfaced again.
- **Never auto-open** a case and **never auto-send** on a 🔴 category — surface and wait for the user.
- **Preserve discrimination wording verbatim** as evidence when that category fires.

## Implements
`scripts/reply_classify.py` (classify + lane) · the generalized inbox watcher (match + cadence) ·
`scripts/case_tick.py` (reads reply-state to advance/branch). Live wiring = Blueprint M6.
