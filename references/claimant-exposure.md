# Claimant-side exposure — who is scoring the USER

Most of this skill points outward: what the vendor owes, which regulator compels them, what the
statute says. This file points the other way. **Running a lot of claims creates a record about the
person making them**, and a user who does not know that is exposed in a way the ladder never
mentions.

Raised by a user on 2026-07-30, unprompted: *"Is there an entity that tracks people who make large
sums of return claims?"* The answer is yes, and nothing in the skill had told him.

---

## 1. The Retail Equation (Appriss Retail) — a consumer reporting agency for returns

**What it is.** The Retail Equation, owned by **Appriss Retail**, Irvine, California. Participating
retailers send it your return and exchange transactions. It scores the shopper and can cause a
return to be **refused at the counter** — the decision is the algorithm's, not the clerk's.

**What it flags.** Frequent returns · high-value returns · **returns without a receipt** · patterns
across different stores. That is a close description of a user running several claims at once, which
is exactly what this skill produces.

**Why it is not just a private blacklist.** It operates as a **consumer reporting agency under the
FCRA** and is listed on the CFPB's own register of consumer reporting companies. That gives the user
real statutory rights:

- a **free file disclosure**, at least annually (FCRA §612)
- the right to **dispute** anything inaccurate, with a **30-day** investigation (FCRA §611)
- the right to know what is in the file (FCRA §609)

**What it does NOT cover.** It records **point-of-sale returns and exchanges** — physically bringing
an item back. Warranty correspondence, defect claims by email, and regulator complaints are not
return transactions and do not feed it. So a user running claims mostly by letter has far less
exposure than the volume of claims suggests. **Say that plainly** — the point is accuracy, not alarm.

### Two different requests — do not confuse them

| | **Return Activity Report (RAR)** | **FCRA file disclosure** |
|---|---|---|
| For | someone whose return was **warned or denied** | **anyone**, no denial needed |
| Needs | a **Transaction ID** from that receipt | identity + address only |
| Window | ~60 days after that transaction | any time; free at least annually |
| Route | `rar.theretailequation.com` | email / post, citing FCRA §609 and §612 |

**A user who has not been denied has no Transaction ID and cannot use the RAR portal.** Sending them
there is a dead end. The correct route is the **file disclosure**, which does not depend on a denial
ever having happened. Getting this wrong wastes the user's time and makes the skill look like it is
guessing.

### Channels

- **Email — `consumerinquiry@theretailequation.com`** (the written route; preferred)
- Post — The Retail Equation, RAR, P.O. Box 51373, Irvine, CA 92619-1373
- Portal — `rar.theretailequation.com` (Transaction ID required)
- Phone — 800-652-2331 *(never offered as an action: see §10 E20, email only)*

---

## 2. Card-issuer dispute history — invisible and unappealable

Separate from TRE, and worth naming because it has no FCRA hook. **Issuers track how often a
cardholder files disputes and chargebacks.** A heavy disputer can quietly have an account closed or
future disputes treated with suspicion. There is no file to request, no dispute process, and no
notice — it is internal risk scoring.

**Practical consequence for the ladder:** prefer recovering from the **merchant** where the merchant
can pay the whole amount. A chargeback is a fast lever with a permanent, invisible cost, and it
should not be the first reach when a demand letter reaches the same money.

---

## 3. What the skill must DO about this

**At intake, once, in plain words — before the ladder generates a single claim:**

> Before we start: there is a company called **The Retail Equation**, owned by Appriss Retail, that
> a lot of retailers use to score shoppers on their return history. It can cause a return to be
> refused at the counter. It mostly tracks returns you make in person — not warranty letters or
> regulator complaints, which is most of what we will be doing — so this probably matters less than
> it sounds. But you are entitled to a free copy of your file under federal law, and if we are going
> to run several claims it is worth knowing what is in it. **Want me to request it for you?**

Then honour the answer. **Requesting it is an outbound to a new recipient — RED lane, explicit yes
required** (§5). Template: `references/letter-templates.md` → *TRE file disclosure*.

**Do not** frame this as a warning against claiming. §1.6 governs: the skill never polices a user out
of a claim, and it does not scare them off one either. This is disclosure so they can decide with
open eyes — the same principle as §6.6 part 4, weighing what a counterparty holds over them.

**Log the answer on the case** so a later session does not ask again.
