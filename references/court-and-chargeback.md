# Court & Chargeback — the end-game math

> **Not legal advice.** This file applies published consumer rules to facts the USER supplies, and
> every action here happens over the **user's own signature and attestation**. Verify every statutory
> citation and every window live before relying on it. For a signed arbitration agreement, a
> counter-claim, or an amount that matters, the user should talk to an attorney.

The last two levers, and the decision of whether to pull them. Both are 🔴 (spend + file / cardholder
attestation) — never automatic.

---

## Chargeback (parallel to court, its own clock)

**What it is:** the card issuer claws the money back from the merchant. The bank holds the power, so
the merchant can't stall it the way it stalls letters.

**Rules:**
1. Runs from the **card issuer's dispute portal**, not the vendor. Independent of everything else.
2. **~120-day window** from the transaction (network rules vary; some run from the expected
   delivery/service date). **On an older purchase this path is usually dead — do not build strategy
   on it.** Confirm the exact window with the issuer before relying on it.
3. Requires the **cardholder to attest** to the fraud/warranty/non-conformance basis — one click, but
   it must be the user's own attestation, truthfully stated.
4. Works only if paid by **card or financing**. Cash / gift card = no chargeback.
5. For **financing / installment (e.g. a phone EIP)** disputes, the **CFPB** complaint is the sibling
   lever — the issuer must respond, typically within ~15 days.

**When to fire:** vendor silent or refusing through Tier 2, AND still inside the window. It can run
in parallel with a prepared petition — a live chargeback is often what makes a vendor finally settle.

### 🛑 Running a chargeback alongside a live refund demand — the rules

The ladder deliberately runs a chargeback in parallel with an open refund demand. That means **both
can land.** What happens then is not optional:

1. **Collecting both a chargeback and a refund for the same purchase is double recovery.**
   **Withdraw the dispute in writing the moment a refund posts** — email the issuer, keep the
   confirmation, and log it on the case. Keeping both, or keeping the item after a successful
   chargeback, is **fraud**, not a win. If the vendor refunds after the chargeback has already
   settled, tell the vendor immediately and return one of the two.
2. **State the true basis of the dispute.** Chargeback reason codes are not interchangeable. A
   defective-goods or services-not-as-described dispute is exactly that; filing "I did not authorize
   this charge" or "I never received it" on a charge the user **did** authorize and **did** receive
   is **bank fraud**, and the attestation is made under the cardholder's own name. Never coach a
   stronger-sounding code.
3. **Withdrawing a chargeback is 🔴** — same lane as filing one. It forfeits a remedy, so it needs
   the user's explicit YES, and it is theirs to submit.
4. **A chargeback routinely triggers account closure.** Issuers and merchants both react: the
   merchant may close the user's account, ban the login, cancel a warranty or service plan, and
   refuse future orders. On a carrier, a bank, or a store the user depends on, that consequence can
   outweigh the amount in dispute. Say this out loud **before** the user files, not after.
5. Chargeback deadlines are network rules, not law, and issuers apply them inconsistently. Confirm
   the actual window with the issuer before building strategy on it.

---

## Arbitration — the 30-day opt-out window

**Check this at Tier 0 on every case, not at Tier 3.5.** Most consumer contracts (carriers, banks,
retailers with accounts, BNPL, streaming, SaaS) contain a binding-arbitration and class-action-waiver
clause — **and almost all of them give a window, commonly 30 days from account activation or from
the date the terms changed, to opt out in writing.**

- It is the single most valuable and most **irreversibly time-barred** thing a consumer can be told.
  Miss it and the right to sue in court, and to join a class action, is gone for the life of the
  account.
- Opting out is normally free, does not affect service, and is usually done by a short written
  notice to a named address or a specific web form in the terms.
- **So: on any account opened recently, surface the opt-out window immediately** — before the ladder
  even starts. On an older account the window has almost certainly closed; say so plainly and plan
  the case around arbitration rather than court.
- Where a clause is in force, the contract's own **Notice of Dispute** step is usually a mandatory
  precondition to arbitration and is itself a strong settlement lever (Tier 3.5).
- Read the actual clause. Never assert an opt-out right, a deadline, or an address from memory.

---

## Small-claims court (Tier 4 — prepared, never auto-filed)

**We prepare the petition; the user files it.** Court filing is a hard-stop human action. The engine
drafts the petition, assembles the exhibit set (intake facts, defect photos, the paper trail of every
tier), and hands it over. The user signs and files.

### The decision: fight or settle or walk
Court costs time and a filing fee. Fight only when the math works. The multipliers that change the
math:

| Case shape | Play |
|---|---|
| Amount < $200, vendor reachable | Tier 1–2 only. Chargeback as last resort. Court costs more than the win. |
| Amount $200–$500, documentable defect | Tier 1 → 2 → chargeback. Skip Tier 3 unless the vendor is interstate. |
| Amount $500+, documented defect | Full ladder. Court on the table if Tier 2 stalls. |
| Amount $1K+, statute with fee-shifting applies | **Full ladder + court.** Fee-shifting makes the net cost near-zero on a win. |
| Vendor interstate / serial defaulter | Skip the Tier 1 letters — chargeback + BBB + AG + industry regulator in parallel. |
| Vendor has in-house counsel (carriers, big brands) | Letters get read. Run the full ladder — they settle before court most of the time. |
| Receipt lost / card closed | Lean on serial number + retailer/maker lookup. Document. Don't restart from scratch. |
| **Discrimination facts that clear the bar** | Only where the user can name a specific thing said or done tied to a protected characteristic, or a comparator. Where that bar is met, some states attach statutory damages (see `jurisdiction-lookup.md`). **Do not let the number drive the decision** — the claim is sworn under penalty of perjury, and a weak one damages the consumer case it is attached to. Where the bar is not met, the consumer ladder runs unchanged and loses nothing. |

### Fee-shifting — the multiplier that makes court cheap
Several statutes let a winning consumer recover **attorney's fees + enhanced/treble damages**, which
flips the cost-benefit:
- **Magnuson-Moss Warranty Act** (federal) — written-warranty breach; fees recoverable.
- **State UDAP acts** — many allow treble damages + fees (e.g. TX DTPA, NJ CFA, NC Ch. 75, MA 93A).
- Resolve which applies from `jurisdiction-lookup.md` at Tier 0; if a fee-shifting statute is in play
  on a $500+ documented case, court is a strong lever, not a bluff.

### Don't bluff court you won't file
If a Tier 2 letter threatens court and you'd never actually file, that's an empty threat — vendors
and their counsel notice. Only invoke court when the case genuinely qualifies (amount + documented
defect + applicable statute). An honest ladder that stops at Tier 2 beats a bluff that gets called.

---

## Statute of limitations (don't let a case age out)
Every state has a limitations period for warranty/UDAP/contract claims (often 2–4 years for UCC
warranty; varies by statute and state). Capture the purchase/failure dates at intake and flag the
outer deadline on the Multica issue so a case never quietly expires mid-ladder. Verify the exact
period per state at Tier 0.
