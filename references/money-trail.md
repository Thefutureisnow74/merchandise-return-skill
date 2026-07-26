# Money-trail parties — follow the money, not just the box

The case model started with two parties: **seller** and **manufacturer**. That's the *product* path —
who sold it, who made it. But a recovery is a **money** event, and the money almost always moved through
a **third party** the two-party model never captured: the **card issuer, payment processor, or
financing/installment company**. That third party is a lever the seller and manufacturer can't stall,
because it holds the money (or the debt) directly. This reference names the money-path parties, says
when each is a lever, and tells you how to fill the **MR Money Parties** property.

---

## The parties on the money path

| Party | Role on the money path | The lever it unlocks |
|---|---|---|
| **Seller / retailer** | Took the payment; first point of contact | Tier 1 vendor-direct letter, return/refund policy |
| **Manufacturer** | Made the item; owns the warranty | Warranty claim, Magnuson-Moss (parallel Tier 1) |
| **Card issuer** (bank behind the Visa/MC/Amex) | Fronted the money; can claw it back | **Chargeback** (~120-day window) + **CFPB** complaint |
| **Payment processor** (PayPal, Klarna, Affirm, Shop Pay, Apple Pay) | Routed / held the payment; often has its own dispute flow | Processor buyer-protection / dispute case (own clock, own portal) |
| **Financing / installment company** (phone EIP, BNPL, store card, auto loan) | Holds the *debt*, not just the payment | **CFPB** complaint against the lender + **EIP/loan payoff status** as leverage |

The seller and manufacturer are the *product* parties. The bottom three are the *money* parties — and
they're the ones most cases forget to name, so their levers go unused.

---

## When each money party is a lever

### Card issuer — chargeback + CFPB
- **Lever when:** paid by **card or financing**, AND still inside the **~120-day window** (network rules
  vary; some run from the expected delivery/service date). On an older purchase this path is usually
  dead — confirm the exact window with the issuer before relying on it.
- **How it works:** the issuer claws the money back from the merchant; the merchant can't stall it the
  way it stalls letters. Requires the **cardholder's own truthful attestation** (one click).
- **Sibling lever:** **CFPB** complaint against the issuer — the issuer must respond, typically within
  ~15 days. Strong when the chargeback is denied or the window is tight.
- Cash / gift-card purchases have **no** issuer lever. Record that explicitly so no one chases a dead path.
- Full mechanics: `references/court-and-chargeback.md`.

### Payment processor — its own dispute flow
- **Lever when:** the payment ran through **PayPal, Klarna, Affirm, Shop Pay, Apple Pay, Cash App**, etc.
  Each has its own buyer-protection / dispute portal with its **own clock**, separate from the card
  network's chargeback. PayPal buyer protection, for example, runs its own 180-day window.
- **Why it matters:** a processor dispute can succeed where a card chargeback is out of window, and vice
  versa. Always name the processor if one sat between the user and the seller.

### Financing / installment company — CFPB + payoff status
- **Lever when:** the item is on an **installment plan** — a phone **EIP** (T-Mobile/Verizon/AT&T),
  **BNPL** (Affirm/Klarna/Afterpay), a **store card**, or an **auto loan**.
- **Two distinct levers:**
  1. **CFPB complaint against the lender** — for financing/installment disputes the CFPB is the primary
     regulator; the lender must respond.
  2. **Payoff status as leverage** — if the user is still paying the EIP/loan on a defective item, the
     open balance is itself a pressure point (stop-payment risk, dispute of the financed amount). Track
     whether the plan is **open (still paying), paid off, or in dispute** — it changes the play.
- **Record the payoff status** in MR Money Parties (e.g. "EIP: T-Mobile financing — 14 of 24 payments,
  open").

---

## How to fill **MR Money Parties**

Free-text property. Write the money path as **semicolon-separated party clauses**, each tagged with its
role and, where relevant, its lever status. Order: seller → manufacturer → issuer/processor → financing.

**Format:**
```
<Seller> (seller); <Maker> (mfr); <Bank/Processor> (<role>, <lever status>); EIP: <lender> (<payoff status>)
```

**Examples:**
- `T-Mobile (seller); Samsung (mfr); Chase Visa (card issuer, chargeback window open ~40d left); EIP: T-Mobile financing (14 of 24 pmts, open — CFPB lever)`
- `Best Buy (seller); LG (mfr); Amex (card issuer, ~120d window PASSED — chargeback dead); no financing`
- `Etsy shop "X" (seller); n/a maker; PayPal (processor, buyer-protection 180d, ~90d left); paid by PayPal balance — no card chargeback`
- `Local dealer (seller); n/a; paid CASH — no issuer/processor lever; small-claims + BBB only`

**Rules for filling it:**
1. **Always name the money party**, even to say it's absent — `paid CASH — no issuer lever` is a
   deliberate record, not a blank. A blank reads as "not yet investigated."
2. **Tag the lever status inline** so the field doubles as a lever checklist: window open/passed, payoff
   open/paid, processor clock remaining.
3. **Keep it one line per case** — it's an at-a-glance money map, not the full narrative. The mechanics
   live in `court-and-chargeback.md`; this field just says *which* parties and *which* levers are live.
4. **Update it when a clock changes** — when the chargeback window closes or the EIP is paid off, edit
   the status so a stale "window open" doesn't send someone down a dead path.

---

## Why this exists (the failure it prevents)

A two-party (seller + manufacturer) model quietly loses every chargeback, CFPB, and EIP-payoff lever,
because nothing in the case ever named the bank, processor, or lender that actually held the money. On
a card or financed purchase those are often the **fastest** paths to recovery — the issuer can claw the
money back in weeks while a letter ladder crawls. Naming the money parties up front, with their lever
status, makes sure no live lever goes unpulled and no dead one gets chased.
