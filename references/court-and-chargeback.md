# Court & Chargeback — the end-game math

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
| **Discrimination facts present** | Adds a **statutory-damages** claim (e.g. Unruh $4k min) that can exceed the item value — materially changes the fight/settle math toward fighting. |

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
