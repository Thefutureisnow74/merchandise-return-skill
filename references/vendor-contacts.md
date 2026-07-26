# Vendor + Regulator Directory

**Purpose.** A pre-built lever list for the vendors we hit most often, so the **Tier-0 remedy map**
(§2) resolves them instantly instead of doing live research from scratch every case. For each vendor:
its **Tier-1 consumer channel**, its **Tier-2 executive/legal escalation channel**, and the matching
**industry regulator** for Tier-3. For anything not listed here, fall back to
`references/jurisdiction-lookup.md` and live research.

## How to use this file (read this FIRST)

- **This is a starting map, not a filing source.** Every row carries a **⚠️ VERIFY before use** flag.
  Contacts — especially specific executive names, emails, and intake URLs — go stale fast. The
  structural facts (which regulator, which federal statute, which escalation *channel* exists) are
  stable; the exact address/URL/person is volatile.
- **Live probe-verification happens at Discovery / Tier 0.** Before a target actually fires, confirm
  the current address or URL live. Treat every value below as "go check this," not "send it here."
- **Honesty rule that built this file:** where a specific executive email could not be confirmed with
  confidence, it is **NOT invented**. Instead the row gives the correct *department / channel* to reach
  (executive relations, legal/registered agent, corporate switchboard) plus the escalation *method*.
  A real channel beats a plausible-looking fake address every time.
- **Seller vs manufacturer are separate parallel targets** (e.g., T-Mobile *and* Samsung). Pursue both.
- **Never fire a regulator before its gate opens** (Tier 3). This file just tells you *which* one and
  the federal statute leverage; the ladder (`references/ladder.md`) decides *when*.

Legend: **T1** = consumer/front-door channel · **T2** = executive/legal escalation · **REG** = regulator/statute.
Address markers: **✅ verified alive on `<date>`** = a real message was accepted at that address on that
date (and, where noted, answered) · **❌ DEAD** = hard-bounced, do not retry · no marker = channel
description only, still ⚠️ VERIFY. A ✅ decays: re-probe anything older than a few months.

---

## Named vendors

### T-Mobile (wireless carrier)
| Layer | Channel | Notes |
|---|---|---|
| **T1** | Care: 1-800-937-8997 / in-app chat / `t-mobile.com` support. For billing/service disputes, open a formal case and get a case number. | ⚠️ VERIFY |
| **T2** | **T-Mobile Executive Response / Office of the President** — reached by escalating a Care case and explicitly asking for executive relations, or via a letter to corporate HQ (12920 SE 38th St, Bellevue, WA 98006). Registered agent for legal service: **CT Corporation** (WA). Do **not** rely on a guessed exec@ address — route through Executive Response. | ⚠️ VERIFY — exec individual emails not confirmed; use the ER channel |
| **REG** | **FCC informal complaint** — `consumercomplaints.fcc.gov` (carrier must respond on the record — high ROI). **PLUS the user's state PUC / PSC** (the state utility/telecom commission for their state — see `jurisdiction-lookup.md`). Also CFPB if a device-financing (EIP) balance is in dispute. | ⚠️ VERIFY intake URL |

**Leverage:** T-Mobile arbitration/Notice-of-Dispute clause exists in its terms — a §3.5 pre-suit
Notice of Dispute is often what moves them. Device EIP financing pulls CFPB into play (see banks row).

### Samsung (electronics manufacturer)
| Layer | Channel | Notes |
|---|---|---|
| **T1** | Samsung Care: 1-800-726-7864 (1-800-SAMSUNG) / `samsung.com/us/support`. Open a warranty service ticket; get the ticket/RMA number. | ⚠️ VERIFY |
| **T2** | **Samsung Executive Customer Relations** — escalate the Care ticket and ask for executive relations; or write Samsung Electronics America HQ (85 Challenger Rd, Ridgefield Park, NJ 07660). Legal service via its registered agent (CT Corporation). Individual exec emails not confirmed — use exec relations channel. | ⚠️ VERIFY |
| **REG** | **Magnuson-Moss Warranty Act** (15 U.S.C. §2301) is the core lever against a manufacturer — federal written-warranty law with **fee-shifting on a win**. Pair with the **state deceptive-practices act** (jurisdiction-lookup) and **FTC** for pattern/defect reporting. If a known defect pattern exists, that is major leverage (documented-defect / class check). | ⚠️ VERIFY |

**Leverage:** Manufacturer warranty is independent of the seller — pursue Samsung in parallel with
wherever the device was bought (carrier, Best Buy, Amazon, etc.).

### Nike (footwear/apparel manufacturer + direct retailer)
| Layer | Channel | Notes |
|---|---|---|
| **T1** | Nike Consumer Services: 1-800-806-6453 / `nike.com/help`. For a **defect**, file a **Nike Rebound** claim (the footwear/apparel manufacturing-defect program) — photos of the defect required; Nike issues a gift card / refund decision. Standard returns run through the order's return flow. | ⚠️ VERIFY — program name/flow |
| **T2** | **Nike Consumer Claims / escalated Consumer Services supervisor** — escalate the Rebound/claim decision; corporate HQ One Bowerman Dr, Beaverton, OR 97005. Individual exec emails not confirmed — escalate through Consumer Claims. **Corporate media relations** `media.relations@nike.com` — ✅ verified alive 2026-07-17 (accepted a letter *and replied*); it is a public/press channel, so use it as the Tier-2 "executives + public" lever, not as a returns desk. ❌ **DEAD: `media.africa@nike.com`** (hard-bounced 2026-07-17 — do not retry). Inbound claim mail arrives from `nikeclaims.com` and `assist.nike.com` as well as `nike.com`; watch all three. | ⚠️ VERIFY currency |
| **REG** | No product-specific federal regulator for footwear. Use the **state deceptive-practices act** (jurisdiction-lookup) + **BBB** (Nike/Oregon region) + **FTC** for pattern. If bought on a card, **card chargeback** (Tier 4) is often the fastest real remedy. | ⚠️ VERIFY |

**Leverage:** Rebound is the manufacturer-defect route; a plain "I changed my mind" return is a
different (weaker) path. Confirm which the case actually is at intake.

### PPG Paints / PPG Industries (paint & coatings manufacturer)
| Layer | Channel | Notes |
|---|---|---|
| **T1** | PPG Paints consumer help / store: `ppgpaints.com` "Contact Us" + the retail store where purchased (PPG-owned stores and independent dealers differ — identify which). Product-quality complaints route to PPG technical/consumer support; keep the batch/lot number and receipt. Direct consumer-affairs mailbox: **`consumer.affairs@ppg.com`** — ✅ verified alive 2026-07-17 (accepted a Tier-1 letter, no bounce). | ⚠️ VERIFY currency |
| **T2** | **PPG consumer affairs / product complaint escalation**, then corporate. PPG Industries HQ: One PPG Place, Pittsburgh, PA 15272. Legal service via registered agent. Individual exec emails not confirmed — use consumer-affairs escalation + certified letter to HQ. Working Tier-2 broadcast set (all three ✅ verified alive 2026-07-17, no bounce): **`contact@ppgac.com`**, **`eaccountservice@ppg.com`**, **`techservicerequests@pittsburghpaints.com`**. | ⚠️ VERIFY currency |
| **REG** | No single federal "paint regulator" for performance. Levers: **product warranty** (Magnuson-Moss if a written warranty) + **state deceptive-practices act** + **BBB** (PPG/Pennsylvania region) + **FTC**. Health/VOC/labeling issues would add **EPA/CPSC**, but those are safety, not refund, routes. | ⚠️ VERIFY |

**Leverage:** Paint disputes usually turn on written product warranty + documented failure (photos,
lot number). A Magnuson-Moss warranty demand is the strongest single lever if a warranty was given.

### GoHighLevel / HighLevel (SaaS — CRM/marketing platform)
| Layer | Channel | Notes |
|---|---|---|
| **T1** | In-app support chat + `help.gohighlevel.com` / support ticket. For **billing disputes**, open a ticket and request a supervisor; cite the specific charge/invoice. Email support (support@ / billing@ style) — **VERIFY current address** before relying on it. ⚠️ **Watch the reply domain, not just the send domain:** HighLevel support answers from its Freshdesk tenant `…@gohighlevelassist.freshdesk.com`, which a `from:gohighlevel.com` inbox filter never sees. Watch both. | ⚠️ VERIFY |
| **T2** | **HighLevel billing/retention escalation → then legal.** HighLevel, Inc. is Texas-based (Dallas/Eugene footprint); legal service via its registered agent. No confirmed individual exec email — escalate through a support supervisor and, if needed, a certified demand to the corporate/registered-agent address. Check the **Terms of Service for an arbitration + Notice-of-Dispute clause** (SaaS ToS almost always has one) — that Notice is the real §3.5 lever. | ⚠️ VERIFY |
| **REG** | SaaS has **no industry regulator**. Levers: **card chargeback** for a disputed charge (often fastest — Tier 4), **CFPB** only if the payment/card-issuer angle applies, **state deceptive-practices act / state AG**, **BBB**, and **FTC**. | ⚠️ VERIFY |

**Leverage:** For recurring-billing SaaS, the chargeback + the ToS Notice-of-Dispute clause do more
than any regulator. Preserve the cancellation timestamp and the ToS version in force at signup.

### Stride Bank, N.A. (sponsor bank behind fintech / neobank debit programs)
| Layer | Channel | Notes |
|---|---|---|
| **T1** | The **fintech program's own support first** (Stride is the issuing bank behind white-label programs such as Payfare-operated driver cards — the app is the front door). Stride's own consumer line: `customerservice@stridebank.com`. Ask in writing for a **written trace result** on any credit that never posted, and give the **ARN and RRN** of each transaction — a trace goes nowhere without them. | ⚠️ VERIFY |
| **T2** | **Stride Bank Complaint Management Program ("C3")** — `interact@stridebank.com`. ✅ verified alive: two-way correspondence on a live trace thread through 2026-07-20. This is the escalation desk, not the app's support queue. **They will ask you to identify the partner program and BIN before they will act** — supply the program name and the card BIN up front to save a round trip. | ⚠️ VERIFY currency |
| **REG** | **CFPB** — `consumerfinance.gov/complaint` (bank must respond, ~15 days). Stride Bank, N.A. is a **national bank**, so the **OCC** is its prudential regulator; **Reg E / EFTA** governs error resolution on a debit-card credit that never landed (written 10-business-day investigation duty). | ⚠️ VERIFY |

**Leverage:** With a sponsor bank the money is provably *somewhere* — insist on where each credit
settled (posted / returned to originator / held in suspense) and, if not returned, when it releases.
`alerts@stridebank.com` is an **outbound transaction-alert sender only** — never reply to it and never
treat an alert as proof a refund landed; only a posted credit on the statement is proof.

---

## General categories (use when the specific vendor isn't listed above)

| Category | T1 — consumer channel | T2 — executive/legal | REG — regulator + statute |
|---|---|---|---|
| **Banks / credit cards / financing** (incl. BNPL, device EIP, EIP loans) | Issuer dispute line / secure message; for a card charge, file a formal **billing dispute (FCBA)** in writing within 60 days of the statement. | Issuer's executive office / "office of the president"; escalate the dispute; certified demand to registered agent. | **CFPB** — `consumerfinance.gov/complaint` (**company must respond, ~15 days — high ROI**). Plus **OCC** (national banks) or the **state banking dept**, and **FCBA/EFTA** statutory leverage. ⚠️ VERIFY |
| **Airlines** | Airline customer relations / refund request; cite the specific fare rule or DOT refund right. | Airline executive/customer-relations escalation; certified letter to HQ. | **DOT Aviation Consumer Protection** — `secure.dot.gov/air-travel-complaint` (airline must respond). DOT rules govern refunds for cancellations/major changes. ⚠️ VERIFY |
| **Autos / dealers / manufacturers** | Dealer service manager → manufacturer customer assistance / zone rep; open a case number. | Manufacturer customer-assistance escalation; **BBB Auto Line** where the maker participates; certified demand. | **NHTSA** — `nhtsa.gov/report-a-safety-problem` (safety defects/recalls) **+ the user's state Lemon Law** (per-state; see jurisdiction-lookup) + state AG. ⚠️ VERIFY |
| **Insurance** | Insurer claims dept / formal claim-dispute or appeal in writing; get the claim/appeal number. | Insurer's appeals / consumer-relations escalation; certified demand. | **The user's STATE Insurance Commissioner / Dept of Insurance** (insurance is state-regulated — no single federal body; find the specific state office). ⚠️ VERIFY |

**Cross-cutting levers that apply to almost any category:** the **state deceptive-practices act** and
**state AG** (jurisdiction-lookup), the **BBB** for the vendor's HQ region, the **FTC** for pattern
reporting, and — if paid by card and inside the window — a **chargeback** (Tier 4). Magnuson-Moss
applies to any written product warranty nationwide.

---

## Maintenance

- When a live Tier-0 probe confirms a current address/URL/person for a listed vendor, update that row
  and note the date confirmed (`✅ verified alive YYYY-MM-DD`). When one is found stale, downgrade it
  to a channel description; when one hard-bounces, keep it as an explicit `❌ DEAD` row so nobody
  rediscovers it and tries again.
- **This file is the only place a vendor address belongs.** Engine code must not carry its own
  hardcoded address table — a second copy drifts, and a stale address in code sends a real letter
  into a void. Verified addresses discovered by a case script get promoted here, not left in the `.py`.
- Add a vendor here only after it has come up in a real case — keep this to high-frequency targets.
- Keep the honesty rule intact: **channel + VERIFY flag over a guessed specific contact.**
