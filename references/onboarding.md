# Onboarding — what actually happens (Blueprint M35)

This is the implemented flow. Everything below is done by **`scripts/vps/onboard.py`**, not by
a person following a checklist. Before M35, §0 was a description with no code behind it: nothing
provisioned a workspace, nothing created a project, and **nothing created the MR property schema**.

> **Why that mattered.** Since 2026-07-26 `case_tick.py` fails *closed* at `PreSuit → Tier4` unless
> the properties **`MR Remedy Map`** and **`MR Remedy Attempted`** exist and are populated. No code
> anywhere created them, so the top of the escalation ladder — chargeback and small claims, the
> whole point of the product — was unreachable on a fresh board. `onboard.py` is what fixes that.

---

## Run it

```bash
export MULTICA_TOKEN=<your token>          # from ~/.multica/config.json after `multica login`
python3 onboard.py                         # interactive interview, DRY RUN — writes nothing
python3 onboard.py --live                  # interactive interview, actually provisions
python3 onboard.py --answers answers.json  # non-interactive plan, DRY RUN
python3 onboard.py --answers answers.json --live --out ./profile.json
python3 onboard.py --selftest              # offline proof, stubbed API, no network
```

| Flag | Meaning |
|---|---|
| *(none)* | **DRY RUN is the default.** Prints the full plan, makes zero writes — no HTTP create, no file. |
| `--live` | Required to create anything. Creates only what is missing; adopts the rest. |
| `--answers PATH` | JSON answers; skips the interview. Unknown keys are a hard error. |
| `--out PATH` | Where the starter profile is written (default `./profile.json`). An existing file is never clobbered. |
| `--workspace ID` | Adopt this exact workspace id instead of matching by slug/name. |
| `--selftest` | 31 offline checks against a stubbed board. Exit 0 = pass. |

---

## What the user is asked, in order

The interview follows SKILL §0. It only ever asks for the user's **own** details.

| # | Answer key | Question | Required? |
|---|---|---|---|
| 1 | `legal_name` | Full legal name, exactly as it should appear on a demand letter | **yes** |
| 2 | `email` | **The user chooses which of their own mailboxes** the engine sends from and reads. A dedicated returns inbox is *recommended, not required* — it contains the blast radius of any autonomous send. | **yes** |
| 3 | `phone` | E.164 preferred. Blank disables voice escalation. | no |
| 4 | `mailing_address` | Used on statutory pre-suit demand letters | no |
| 5 | `state` | Full state name. **NEVER defaulted.** | **yes** |
| 6 | `county` | Without the word "County". **NEVER defaulted.** | **yes** |
| 7 | `workspace_name` | Multica workspace name (default `Merchandise Return`) | no |
| 8 | `workspace_slug` | Slug — lowercase-with-hyphens, **permanent** (default `merchandise-return`) | no |
| 9 | `project_title` | Project inside it (default `My Return Cases`) | no |
| 10 | `notify_channel` | `telegram` or `none` | no |
| 11 | `telegram_chat_id` | Numeric chat id. Not a secret; the bot **token** is, and lives in `.env`. | no |
| 12 | `calendar_id` | Google Calendar for deadlines (blank = the email above) | no |
| 13 | `google_token_file` | **PATH** to the mailbox OAuth token JSON — *never the token itself* | no |

Jurisdiction is the one that is refused rather than guessed: state + county decide the state
Attorney General, the BBB region, the small-claims venue and the damages cap. A guessed
jurisdiction sends a real complaint to the wrong regulator.

---

## What gets created (or adopted)

Six steps, in order. The run **stops at the first hard failure** and the profile is written
**last**, so a failure mid-provision can never leave a profile pointing at a half-built board.

| Step | Item | Adopt rule | Create |
|---|---|---|---|
| 1 | **Connection + token** | read-only `GET /api/workspaces` | — fails **loudly** here if the token is missing or invalid, before anything exists |
| 2 | **Workspace** | matched by id → slug → case-insensitive name | `POST /api/workspaces` |
| 3 | **Project** | matched by case-insensitive title | `POST /api/projects` |
| 4 | **The 8 MR properties** | matched by **name** | `POST /api/properties` for each missing one |
| 5 | **Mailbox** | reports whether the OAuth token file exists | never handles a credential — see the gap below |
| 6 | **`profile.json`** | an existing file is reported and **not overwritten** | written only if steps 1–4 succeeded |

### The MR property schema

Names are the contract. Every engine module resolves properties **by name**
(`multica_api.name_to_defs`), which is what makes a board portable — a fresh workspace has
completely different property ids but behaves identically. **Never rename one to "fix" something.**

| Property | Type | Read by |
|---|---|---|
| `MR Phase` | select — `Intake, CaseFile, RemedyMap, Tier1, Tier2, Tier3, PreSuit, Tier4, Closed` | `case_tick` (the state machine) |
| `MR Phase Deadline` | date | `case_tick` (the clock) |
| `MR Intake Complete` | checkbox | `case_tick` — blocks leaving Intake/CaseFile |
| `MR Awaiting User YES` | checkbox | `case_tick` — a RED-lane action blocks every advance |
| `MR Jurisdiction` | text | remedy mapping (Tier 0) |
| `MR Discrimination Flag` | checkbox | the Tier 3-D civil-rights track |
| `MR Remedy Map` | text | **the Tier4 gate** — comma/newline separated lever keys |
| `MR Remedy Attempted` | text | **the Tier4 gate** — levers actually done AND logged |

`MR Remedy Map` / `MR Remedy Attempted` are the two that decide whether the ladder can reach
court. `case_tick` fails closed while either is empty.

### Idempotency

Re-running is safe and is the normal case. Every item prints exactly one
`CREATED` / `ADOPTED` / `SKIPPED` line, so the run is auditable — a run you cannot audit is not
idempotent, it just looks like it.

A real dry run against a board that already carried the whole schema:

```
=== MERCHANDISE RETURNS ENGINE - §0 ONBOARDING (DRY RUN) ===
  ADOPTED  token       connection               verified - 7 workspace(s) visible
  ADOPTED  workspace   Merchandise Return       id=a1b2c3d4-... slug=merchandise-return
  SKIPPED  project     My Return Cases          would CREATE (dry-run)
  ADOPTED  property    MR Phase                 id=12215804-... type=select
  ...
  ADOPTED  property    MR Remedy Attempted      id=e440ac51-... type=text
  SKIPPED  mailbox     you@example.com          NOT CONNECTED
  SKIPPED  profile     profile.json             would WRITE 12 fields (dry-run)

SUMMARY: created=0 adopted=10 skipped=3 warnings=1
```

Two adoption edge cases are handled explicitly rather than silently:

* **Wrong type** on an existing property (e.g. `MR Phase` as `text`) → **hard abort**. Property
  type is immutable server-side, so re-running can never repair it. The user is told to archive
  or rename it first.
* **A select missing options** (e.g. an `MR Phase` with no `Tier4`) → adopted, but flagged
  `INCOMPLETE` with a `WARN` and the exact `multica property update` command. The ladder cannot
  write a phase the board does not define, so the case would stall there.

### The profile it writes

Exactly the keys in `profile.example.json` — the schema `mer_config` enforces. No invented
fields; the self-test asserts that every key written is one the example defines and that
`mer_config` loads the result. **No secret is ever written**: `google_token_file` is a *path*, and
a final guard refuses to write anything that looks like a live credential.

Purely optional keys owned by other milestones are **omitted rather than fabricated** — notably
`llm_providers` (M36). Supply it in an `--answers` file and it is passed straight through;
otherwise the LLM tier degrades to its heuristic, which is the designed fail-safe.

---

## Endpoints, and how they were verified

Discovered 2026-07-25. Everything is `https://api.multica.ai/api`, `Authorization: Bearer <token>`,
workspace passed as the `workspace_id` query parameter.

| Method | Path | Body |
|---|---|---|
| `GET` | `/workspaces` | — (returns a **bare array**, not `{"workspaces": …}`) |
| `POST` | `/workspaces` | `{"name","slug","issue_prefix"}` |
| `GET` | `/projects?workspace_id=` | — (returns `{"projects": […]}`) |
| `POST` | `/projects?workspace_id=` | `{"title","description","status"}` |
| `GET` | `/properties?workspace_id=` | — (returns `{"properties": […]}`) |
| `POST` | `/properties?workspace_id=` | `{"name","type","description","icon","config":{"options":[{"name","color"}]}}` |
| `PUT` | `/issues/<id>?workspace_id=` | issue update — **PUT only**; PATCH and POST return 405 |

Verified **without writing anything to a live board**:

1. Every `GET` was run read-only against the live API.
2. Every `POST` route was proved to exist by posting a body that *cannot* create anything —
   `POST /api/workspaces {}` → `400 {"error":"name and slug are required"}`,
   `POST /api/properties?workspace_id=… {}` → `400 {"error":"name is required"}`,
   `POST /api/projects?workspace_id=… {}` → `400 {"error":"title is required"}`.
   A route that does not exist answers with plain-text `404 page not found` (proved against
   `POST /api/floopdoop`), so a JSON `400` is proof of a real route.
3. The exact request **body shapes** were captured by pointing the official `multica` CLI at a
   loopback stub via `--server-url` / `$MULTICA_SERVER_URL` and logging what it sent. Nothing
   left the machine.

Property types the API accepts: `text, number, select, multi_select, date, checkbox, url`.

---

## What onboarding still cannot do

* **Mailbox OAuth is not automated.** `onboard.py` reports whether the token file the profile
  points at exists, and refuses to claim the user is set up when it does not — but the consent
  flow itself lives in `gmail_transport.py`. Until that file exists the engine can send and read
  nothing. This is the remaining half of Blueprint M35(a).
* **Multica account creation is not an API operation.** A user with no Multica account must sign
  up at <https://multica.ai> and run `multica login` first; there is no endpoint to create an
  account from a token you do not yet have.
* **A workspace slug is permanent.** It cannot be changed after creation, so onboarding validates
  it before the call rather than after.
* **Select options can only be replaced, not appended.** `PUT`-style option updates replace the
  whole list (ids survive by name match), so `onboard.py` reports a missing option and hands over
  the exact command instead of mutating an existing board's schema on its own.
