# Letter Templates — parameterized, ready-to-fill

> **Not legal advice.** These templates apply published consumer rules to facts the USER supplies,
> and go out over the **user's own signature**. Every statutory citation must be verified live before
> it goes in a letter. For a signed arbitration agreement, a counter-claim, or an amount that
> matters, the user should talk to an attorney.

Every outbound the ladder sends starts from a template here, not a blank page. Fill the
`{{placeholders}}` from the case's Multica issue, then let the autonomy lane (SKILL.md §5) decide
whether it auto-sends after a veto window (🟡) or waits for the user's explicit YES (🔴).

**Single-user model.** The user sends every one of these **under their own name, from their own
email, about their own purchase.** There is no third party. The tone is always **firm, factual,
and non-adversarial** — a reasonable person stating a documented problem and a specific remedy, not
a threat. Facts and dates do the persuading; heat does not.

---

## Placeholders (fill from the case issue)

| Token | Meaning |
|---|---|
| `{{name}}` | User's full legal name, as on the purchase |
| `{{item}}` | Product, plain language (e.g. "Nike Pegasus 41 running shoes") |
| `{{model}}` | Brand + model / variant / size |
| `{{serial}}` | Serial / IMEI / VIN / order-line ID |
| `{{purchase_date}}` | Date of purchase |
| `{{amount}}` | Exact amount paid, incl. tax |
| `{{payment_method}}` | How paid (Visa card, financing, PayPal, cash…) |
| `{{card_last4}}` | Last 4 of the card used (Tier 1 cites this; omit the line if paid in cash) |
| `{{defect}}` | The problem, one factual clause |
| `{{desired_outcome}}` | Ranked remedy (refund / replacement / repair / credit) |
| `{{deadline_date}}` | The SLA end date — a **calendar date**, computed from business days and sanity-checked (SKILL.md §6.3) |
| `{{vendor}}` | Seller or manufacturer being addressed |
| `{{prior_letter_date}}` | Date of the letter the current one references |
| `{{prior_message_id}}` | RFC-822 Message-ID / thread ref of the prior letter (proves it was sent) |
| `{{statute}}` | Governing statute resolved at Tier 0 from `jurisdiction-lookup.md` |

Extra per-case tokens (fill where a template uses them): `{{order_number}}`, `{{case_number}}`,
`{{exec_name}}`, `{{exec_title}}`, `{{legal_contact}}`, `{{vendor_address}}`, `{{agency}}`,
`{{purchase_channel}}` (store/URL), `{{timeline}}` (dated bullet list of what happened).

> **Fill-completeness gate.** Do not send a letter with an unfilled `{{token}}` still in the body.
> If a required field is blank, the case is still `(INTAKE INCOMPLETE)` — go back to §1, don't guess.

---

## 0. The DELAY BLOCK — paste into any tier when time has passed

Per SKILL.md §1.6, an old claim is never abandoned; it is argued differently. Whenever the gap
between the failure and the letter is long enough that a vendor might reach for "outside our
window", include this block. Fill it **only** from `MR Delay Explanation` and intake 4.6/4.7 —
never invent a hardship, and never assert a notice that was not actually given.

Tokens: `{{notice_who}}`, `{{notice_when}}`, `{{notice_what_they_said}}`, `{{delay_reason}}`,
`{{uses_before_failure}}`.

**A. Notice was given at the time** (use whenever the user told anyone — this is the strongest form):

```
For the record, this was not left unreported. I raised it with {{notice_who}} at the time,
in {{notice_when}}, and was told {{notice_what_they_said}}. {{vendor}} has therefore been on
notice of this defect since it first occurred. I stopped pursuing it because {{delay_reason}} —
but the underlying defect was never remedied, and the obligation to remedy it did not lapse
because I was discouraged from pressing it.
```

**B. No contemporaneous notice** (use only when nobody was told):

```
I recognise time has passed. {{delay_reason}}. That does not change the underlying facts: the
{{item}} failed after {{uses_before_failure}} ordinary uses and has never been remedied.
```

**C. The merchantability close** — pair with A or B whenever a warranty or return window has
expired. This is the sentence that keeps a stale-looking claim alive:

```
I would add that an expired warranty period is not an expired obligation. A {{item}} that failed
after {{uses_before_failure}} ordinary uses was not of merchantable quality when it was sold, and
the implied warranty of merchantability — together with {{statute}} — is not defeated by the
expiry of a manufacturer's own limited term.
```

**Tone rules.** State it once, as fact. Do not apologise, do not plead, do not over-explain, and
do not repeat it in later tiers beyond a single reference — a delay asserted confidently reads as
context; a delay repeated anxiously reads as a weakness the vendor will aim at.

---

## 1. Tier 1 — Vendor demand

*First contact. Seller and manufacturer get this in parallel where both apply. 5-business-day SLA,
Day-3 nudge. Lane 🔴 (new vendor) → user YES on first send. One factual paragraph on the defect;
cites purchase date, amount, and card last-4 so the vendor can find the transaction instantly.*

```
Subject: Refund request — {{item}} (order {{order_number}}, purchased {{purchase_date}})

Dear {{vendor}} Customer Care,

I am writing about a {{item}} ({{model}}, serial {{serial}}) I purchased from you on
{{purchase_date}} for {{amount}}, paid by {{payment_method}} ending {{card_last4}}
(order {{order_number}}). The product is defective: {{defect}}. This was not caused by
misuse, accident, or modification — the item failed to perform as sold.

I am requesting a {{desired_outcome}}. Please confirm how you will resolve this, and the
timeline, by {{deadline_date}} (5 business days). I would prefer to settle this directly
with you and expect that we can.

Please reply to this email so we have a written record. My order details and a photo of the
defect are available on request.

Thank you,
{{name}}
{{email}} · {{phone}}
```

---

## 2. Retention ask

*Days 3–7, when a front-line rep stalls or a phone/chat channel is open. A short pivot that asks to
be routed to someone who can actually authorize the remedy. Lane 🟡. Use as an email or a
call/chat script.*

```
Subject: Re: Refund request — {{item}} (order {{order_number}})

I appreciate your help, but this needs someone with authority to approve a {{desired_outcome}}.
Please transfer me to your retention or refunds team, or escalate this to a supervisor who can
issue it.

To recap so they have the facts: {{item}} ({{model}}, serial {{serial}}), purchased
{{purchase_date}} for {{amount}}, order {{order_number}}. The defect: {{defect}}. I am asking
for a {{desired_outcome}} and would like it resolved by {{deadline_date}}.

Please confirm in writing what the retention/refunds team decides.

{{name}}
```

*Phone note:* "I'd like to be transferred to retention or a refund supervisor." Log the rep name,
department, and any case/RMA number they give as a `RECORD ONLY` comment on the issue.

---

## 3. Tier 2 — Executive / corporate escalation

*Fires at Day 7 when Tier 1 died with no resolution. Addressed to a named executive and copies
legal. References the dead Tier-1 letter by date and Message-ID. Raises the implied-warranty-of-
merchantability argument. Tighter SLA. Lane 🔴 (new channel).*

```
To: {{exec_name}}, {{exec_title}} — {{vendor}}
Cc: {{legal_contact}} (Office of the General Counsel / Legal)
Subject: Unresolved defect, {{item}} — escalation after no response to my {{prior_letter_date}} request

Dear {{exec_name}},

I am escalating a matter your customer-care team has not resolved. On {{prior_letter_date}} I
sent {{vendor}} a written request regarding a defective {{item}} ({{model}}, serial {{serial}}),
purchased {{purchase_date}} for {{amount}} (order {{order_number}}). That message
(ref {{prior_message_id}}) set a 5-business-day deadline, which has now passed without a
substantive response.

The facts are undisputed: the product {{defect}}. A product sold for ordinary use carries an
implied warranty of merchantability — it must be fit for the purpose it was sold for. This one
was not. Under that warranty, and under {{statute}}, I am entitled to a remedy.

I am still asking only for a {{desired_outcome}} — the same reasonable resolution I requested at
the outset. Please have someone with authority confirm it in writing by {{deadline_date}}
(5 business days).

I would much rather resolve this with {{vendor}} directly than take it further. Please treat this
as the opportunity to do so.

Sincerely,
{{name}}
{{email}} · {{phone}}
{{mailing_address}}
```

*Sourcing the addressees:* find the executive + legal contact during Tier 2 discovery (SKILL.md §3
Tier 2). If no individual name is verifiable, address "Office of the CEO" and "Office of the General
Counsel" — never invent a name.

---

## 4. Tier 3 — Regulatory complaint one-pager

*One structured page filed with a regulator (state AG, BBB, FCC/CFPB/DOT/… as resolved at Tier 0)
and, where a portal has fields, mapped field-by-field. Facts → timeline → what was tried → what is
demanded. Lane 🔴 (filing). Keep it to one page; regulators skim.*

```
CONSUMER COMPLAINT — {{name}} v. {{vendor}}

Complainant:   {{name}} · {{email}} · {{phone}} · {{mailing_address}}
Respondent:    {{vendor}} · {{vendor_address}}
Filed with:    {{agency}}
Amount at issue: {{amount}}          Purchased: {{purchase_date}}          Order: {{order_number}}

THE PRODUCT
{{item}} ({{model}}, serial {{serial}}), purchased {{purchase_date}} from {{purchase_channel}}
for {{amount}}, paid by {{payment_method}}.

THE PROBLEM
{{defect}}. The product was not fit for ordinary use and breached the implied warranty of
merchantability.

TIMELINE OF WHAT I DID
{{timeline}}
  e.g.
  - {{purchase_date}} — purchased {{item}}.
  - {{prior_letter_date}} — wrote {{vendor}} customer care requesting a {{desired_outcome}}
    (5-business-day deadline). No substantive response.
  - [date] — escalated in writing to {{exec_name}}/legal. No resolution.

WHAT I HAVE TRIED
Direct request to customer care; escalation to executive and legal contacts. {{vendor}} has not
provided the remedy the facts and {{statute}} require. "Escalated internally" is the only response
I have received, which is not a resolution.

WHAT I AM ASKING
A {{desired_outcome}} of {{amount}}, and any relief {{agency}} finds appropriate under {{statute}}.

Supporting documents (purchase receipt, defect photos, full correspondence) are available and will
be provided on request.

{{name}} — {{today_date}}
```

---

## 5. Tier 3.5 — Statutory pre-suit demand

*The legally-required notice before suit, per the user's state. Sending it starts the state's
statutory clock and preserves enhanced damages + fee-shifting. Lane 🔴 (legal notice). Send certified
mail / with delivery proof; log the send date — the clock runs from it. Pick the variant for the
user's state (resolve at Tier 0 from `jurisdiction-lookup.md`); a generic fallback follows for
states without a specific pre-suit statute.*

### 5a. Texas — DTPA §17.505 (60-day notice)

*Tex. Bus. & Com. Code §17.41 et seq. Consumer-friendly: economic + up to **treble** damages for a
knowing violation, **plus attorney's fees**. §17.505 requires **60 days'** written notice before
filing.*

```
Subject: NOTICE OF CONSUMER COMPLAINT UNDER TEX. BUS. & COM. CODE §17.505 — {{name}} / {{item}}

To: {{vendor}} — {{legal_contact}} / Registered Agent
    {{vendor_address}}

This letter is written notice under Section 17.505 of the Texas Deceptive Trade Practices–
Consumer Protection Act (Tex. Bus. & Com. Code §17.41 et seq.), given at least 60 days before
the filing of suit.

Consumer:        {{name}}, {{mailing_address}}
Transaction:     {{item}} ({{model}}, serial {{serial}}), purchased {{purchase_date}} for
                 {{amount}} via {{payment_method}} (order {{order_number}}).

Complaint of specific conduct: {{vendor}} sold a product that {{defect}}, breaching the implied
warranty of merchantability (Tex. Bus. & Com. Code §2.314) and constituting a false, misleading,
or deceptive act under the DTPA. Despite written requests dated {{prior_letter_date}}
(ref {{prior_message_id}}) and subsequent escalation, {{vendor}} has not provided a remedy.

Economic damages claimed: {{amount}}, plus incidental costs. The DTPA provides for up to three
times economic damages for a knowing violation, and reasonable attorney's fees.

To resolve this matter without litigation, I demand a {{desired_outcome}} of {{amount}} within
60 days of your receipt of this notice. Please respond in writing.

{{name}}
{{email}} · {{phone}}
Sent {{today_date}} via certified mail, return receipt requested.
```

### 5b. California — CLRA 30-day notice + Song-Beverly implied warranty

*CLRA (Civ. §1750) requires **30 days'** written notice by certified mail before seeking damages;
Song-Beverly (Civ. §1790) gives a strong implied-warranty-of-merchantability claim on defective
goods, with fee-shifting; UCL (Bus. & Prof. §17200) is the companion unfair-practice lever.*

```
Subject: NOTICE UNDER CAL. CIV. CODE §1782 (CLRA) — {{name}} / {{item}}

To: {{vendor}} — {{legal_contact}} / Agent for Service of Process
    {{vendor_address}}

This letter is notice under California Civil Code §1782 of the Consumers Legal Remedies Act
(Civ. Code §1750 et seq.), given at least 30 days before an action for damages.

Consumer:        {{name}}, {{mailing_address}}
Transaction:     {{item}} ({{model}}, serial {{serial}}), purchased {{purchase_date}} for
                 {{amount}} via {{payment_method}} (order {{order_number}}).

Nature of the violation: {{vendor}} sold a product that {{defect}}. This breaches the implied
warranty of merchantability under the Song-Beverly Consumer Warranty Act (Civ. Code §1790 et
seq.) and constitutes an unlawful/unfair practice under the CLRA (Civ. Code §1770) and the UCL
(Bus. & Prof. Code §17200). I requested a remedy in writing on {{prior_letter_date}}
(ref {{prior_message_id}}); {{vendor}} has not resolved it.

Demand: a {{desired_outcome}} of {{amount}} within 30 days of your receipt of this notice.
Song-Beverly provides for the buyer's remedies and reasonable attorney's fees on a successful
claim. I would prefer to resolve this without litigation.

{{name}}
{{email}} · {{phone}}
Sent {{today_date}} via certified mail, return receipt requested.
```

### 5c. Generic fallback (state without a specific pre-suit statute)

*Use ONLY where `jurisdiction-lookup.md` shows **no** mandatory pre-suit notice for the user's state.*

> 🛑 **A "Verify" row is a STOP, not a green light.** "Verify" means *go check the current rule
> live* — 40+ states are marked that way, and a missed mandatory pre-suit notice **can bar the
> lawsuit entirely**. Resolve the state's actual requirement before sending this template. If a
> mandatory notice turns out to exist, use 5a/5b (the statutory form), not this one.

*Still cites the state UDAP act + implied warranty, still gives a firm deadline, still preserves the
paper trail.*

```
Subject: FINAL PRE-LITIGATION DEMAND — {{name}} / {{item}}

To: {{vendor}} — {{legal_contact}}
    {{vendor_address}}

This is a final written demand before I pursue my remedies in court.

I purchased a {{item}} ({{model}}, serial {{serial}}) on {{purchase_date}} for {{amount}}
(order {{order_number}}). The product {{defect}}, breaching the implied warranty of
merchantability and {{statute}}. I requested a remedy on {{prior_letter_date}}
(ref {{prior_message_id}}); it remains unresolved.

I demand a {{desired_outcome}} of {{amount}} within 14 days of this letter. If I do not receive
it, I intend to pursue the remedies available to me, including a small-claims filing. I would
rather resolve this directly.

{{name}}
{{email}} · {{phone}}
Sent {{today_date}}.
```

> **Never bluff court you won't file** (`court-and-chargeback.md`). Send 5a/5b/5c only when the case
> genuinely qualifies (amount + documented defect + applicable statute). The notice period runs from
> the vendor's **receipt** — log the send date and the delivery proof on the issue, and start the
> §17.505 / §1782 clock from receipt, not from drafting.

> 🛑 **Never price a regulator complaint or publicity.** A demand may state that the user intends to
> **sue** if not paid by a date — that is lawful and normal. A demand may **never** offer to withhold
> a **regulatory filing, a BBB complaint, media contact, or a public post** in exchange for payment;
> that is the classic shape of coercion. Regulator complaints are filed on their merits and
> **announced, never bargained**. Do not add such a clause back into any template here, and strike it
> if a drafted letter contains one.

---

## Filling & sending checklist (applies to every template)

1. **Resolve the statute/agency/venue at Tier 0** — `jurisdiction-lookup.md`, never assume a state.
2. **Compute `{{deadline_date}}` as a real calendar date** using `engine/businessday.py` and
   sanity-check it (SKILL.md §6.3 — "5 business days from a Friday" ≠ "+5 calendar days"). The
   Tier-1 window is **5 business days** everywhere in this package; never write 7.
3. **No unfilled `{{token}}` may remain** in the body at send time.
4. **Send only through `mer_send.send()`** (SKILL.md §6.10). It reserves the send, mints the
   single-use token the transport now demands, and enforces the 48-hour `(case, recipient)` cooldown.
   Calling `gmail_transport.send_mime` directly raises.
4b. **Never build a letter through a shell literal.** Write the body to a file and pass it by path.
   `$2,500` does not survive shell interpolation, and a mangled amount is a factual error over the
   user's signature.
5. **Honor the autonomy lane** — 🟡 auto-send after the veto window; 🔴 wait for the user's YES.
6. **Log the send** as an `EVENT:` line + a `RECORD ONLY` comment on the case, and capture the
   outbound Message-ID so the next tier can reference it as `{{prior_message_id}}`.
