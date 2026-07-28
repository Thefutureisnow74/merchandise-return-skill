#!/usr/bin/env python3
"""
onboard.py — REAL §0 onboarding for the AMBS Merchandise Returns Engine (Blueprint M35).

WHY THIS EXISTS
---------------
SKILL.md §0 promised onboarding that was DESCRIBED but never IMPLEMENTED. Nothing in the
engine provisioned a Multica workspace, created a project, or created the MR property
schema, so a new user could not actually start — and worse, since 2026-07-26 `case_tick.py`
FAILS CLOSED at PreSuit -> Tier4 unless the properties `MR Remedy Map` and `MR Remedy
Attempted` exist and are populated. Those two properties did not exist ANYWHERE in code or
on any board, which made the top of the escalation ladder unreachable. This script is what
unblocks it.

WHAT IT DOES (in order, and it stops at the first hard failure)
--------------------------------------------------------------
  1. Verifies the Multica connection and the token       (read-only, fails LOUDLY)
  2. Adopts or creates the user's WORKSPACE
  3. Adopts or creates a PROJECT to hold cases
  4. Adopts or creates the twelve MR PROPERTIES (the schema the engine reads by NAME)
  5. Reports the mailbox/OAuth status (it never invents or stores a credential)
  6. Writes a starter profile.json — LAST, and only if 1-4 all succeeded

IDEMPOTENT BY DESIGN
--------------------
Re-running adopts everything that already exists and creates only what is missing; every
item prints exactly one CREATED / ADOPTED / SKIPPED line. A partly-provisioned board is the
case this was designed for: a run there adopts every property that already exists and creates
only the remainder — never duplicate, never rename. (The count is NOT a constant. It was
"six of eight" when this was written; M44 added `MR Remedy Type` and M47 added the three
properties the shipping engine was already reading — `MR Last Vendor Reply`, `MR Delay
Explanation`, `MR Money Parties` — so MR_SCHEMA is now fifteen entries. Derive the number from
len(MR_SCHEMA); never retype it.)

SAFETY
------
  * --dry-run is the DEFAULT. It prints the plan and writes NOTHING — no HTTP write, no file.
  * --live is required to actually create anything.
  * Profile writing is the LAST step, so a failure mid-provision can never leave a profile
    pointing at a half-built board.
  * A secret is NEVER written into profile.json. `google_token_file` is a PATH.

USAGE
-----
    python3 onboard.py                          # interactive interview, DRY RUN
    python3 onboard.py --live                   # interactive interview, really provision
    python3 onboard.py --answers answers.json   # non-interactive plan, DRY RUN
    python3 onboard.py --answers answers.json --live --out ./profile.json
    python3 onboard.py --selftest               # offline proof, stubbed API, no network

Stdlib only. Runs from any working directory under a bare python3.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CREATED, ADOPTED, SKIPPED, FAILED = "CREATED", "ADOPTED", "SKIPPED", "FAILED"

# --------------------------------------------------------------------------------------
# THE MR PROPERTY SCHEMA — the single source of truth for what a merchandise-return board
# must carry. Every consumer resolves these BY NAME (multica_api.name_to_defs), which is
# what makes a board portable: a freshly provisioned workspace has different property ids
# but identical names, so no engine module needs an edit.
#
# NEVER rename an entry here without changing every reader. The names are the contract:
#   MR Phase / MR Phase Deadline / MR Intake Complete / MR Awaiting User YES  -> case_tick
#   MR Jurisdiction                                                          -> remedy map
#   MR Discrimination Flag                                                   -> Tier 3-D
#   MR Remedy Map / MR Remedy Attempted                                      -> the Tier4 gate
#   MR Remedy Type                                            -> refund_landed / close_case
#   MR Last Vendor Reply                          -> case_tick's escalation-hold gate (written
#                                                    by the inbound classifier in mer_engine)
#   MR Delay Explanation                                      -> letter-templates DELAY BLOCK
#   MR Money Parties                              -> remedy_map (chargeback / CFPB / payoff)
# --------------------------------------------------------------------------------------
PHASE_OPTIONS = [
    ("Intake",    "#6b7280"),
    ("CaseFile",  "#6b7280"),
    ("RemedyMap", "#8b5cf6"),
    ("Tier1",     "#3b82f6"),
    ("Tier2",     "#f59e0b"),
    ("Tier3",     "#ef4444"),
    ("PreSuit",   "#dc2626"),
    ("Tier4",     "#991b1b"),
    ("Closed",    "#22c55e"),
]

# M44 — the four remedies the ladder can actually win. The option NAMES are the contract
# with refund_landed.normalize_remedy(); do not rename one without changing that mapping.
REMEDY_TYPE_OPTIONS = [
    ("Refund",      "#22c55e"),
    ("Replacement", "#3b82f6"),
    ("Repair",      "#f59e0b"),
    ("StoreCredit", "#8b5cf6"),
]

MR_SCHEMA = [
    {
        "name": "MR Phase",
        "type": "select",
        "icon": "flag",
        "options": PHASE_OPTIONS,
        "description": ("Merchandise-return case phase (state machine). The daily-tick engine "
                        "reads this + MR Phase Deadline to advance/nudge/branch."),
    },
    {
        "name": "MR Phase Deadline",
        "type": "date",
        "description": ("Current phase SLA deadline (business-day computed). Drives the "
                        "daily-tick: elapsed + no reply -> advance."),
    },
    {
        "name": "MR Intake Complete",
        "type": "checkbox",
        "icon": "shield",
        "description": ("TRUE only when every blocking intake field is answered. Structural "
                        "gate: RemedyMap/Tier1 must not start until this is TRUE."),
    },
    {
        "name": "MR Awaiting User YES",
        "type": "checkbox",
        "icon": "flag",
        "description": ("A RED-lane action (new-vendor send / filing / spend / sign / legal "
                        "threat / close) is queued and needs the user's explicit yes."),
    },
    {
        "name": "MR Jurisdiction",
        "type": "text",
        "description": ("The user's state + county for THIS case. Decides the AG, the BBB "
                        "region and the small-claims venue. Never defaulted, never guessed."),
    },
    {
        "name": "MR Discrimination Flag",
        "type": "checkbox",
        "description": ("Set only when the intake discrimination question was answered YES "
                        "with supporting facts. Unlocks the Tier 3-D civil-rights track."),
    },
    {
        "name": "MR Remedy Map",
        "type": "text",
        "description": ("Tier 0 output: the comma/newline separated lever keys that apply to "
                        "THIS case (e.g. tier1_vendor, state_ag, bbb). case_tick FAILS CLOSED "
                        "at PreSuit -> Tier4 while this is empty."),
    },
    {
        "name": "MR Remedy Type",
        "type": "select",
        "icon": "flag",
        "options": REMEDY_TYPE_OPTIONS,
        "description": ("Which remedy this case must prove LANDED before it may close: "
                        "Refund (money posted), Replacement (item delivered), Repair (unit "
                        "back AND verified working), StoreCredit (credit issued with a "
                        "code). refund_landed/close_case FAIL CLOSED while this is blank — "
                        "a ranked intake preference is not a decision and is never guessed. "
                        "Set it to the remedy the vendor actually GRANTED."),
    },
    {
        "name": "MR Remedy Attempted",
        "type": "text",
        "description": ("The lever keys actually attempted AND logged. Tier4 (court/chargeback) "
                        "opens only when this covers every key in MR Remedy Map."),
    },
    # ----------------------------------------------------------------------------------------
    # M47 — three properties the SHIPPING engine already reads and writes, which this schema
    # never created. A fresh install was therefore broken by design: the escalation-hold gate in
    # case_tick (LAST_REPLY_PROP) reads a property nothing provisioned, so a case could advance a
    # tier over a live vendor conversation on any board but the author's. Names and types are
    # taken from the live board's own property definitions, because the readers resolve BY NAME.
    # ----------------------------------------------------------------------------------------
    {
        "name": "MR Last Vendor Reply",
        "type": "date",
        "icon": "flag",
        "description": ("Date of the most recent SUBSTANTIVE inbound reply from the vendor on "
                        "this case, written automatically by the inbound classifier. case_tick "
                        "reads this as the escalation-hold gate: a case must not auto-advance a "
                        "tier while a live conversation is in flight. Autoresponders and delivery "
                        "receipts do NOT count. A brush-off ('escalated internally') does NOT "
                        "hold the gate per the ladder rules — only a substantive reply does."),
    },
    {
        "name": "MR Delay Explanation",
        "type": "text",
        "description": ("Verbatim, in the user's own words: why time passed between the failure "
                        "and the claim, and who they told at the time. Per SKILL §1.6 this is a "
                        "LEVER, never a disqualifier — no warranty, return window or statute "
                        "estimate may close a case on it. Feeds the DELAY BLOCK in "
                        "letter-templates."),
    },
    {
        "name": "MR Money Parties",
        "type": "text",
        "description": ("Money-path parties for a case: seller, manufacturer, and the THIRD "
                        "money party (card issuer, payment processor, or financing/installment "
                        "company) that unlocks the chargeback/CFPB/payoff levers. remedy_map "
                        "reads it; with it blank the issuer/processor/lender lever cannot be "
                        "assessed at all."),
    },
    {
        "name": "MR Send Queued Until",
        "type": "date",
        "description": ("YELLOW-lane veto-window countdown. send_queue holds a drafted reply "
                        "until this passes, then sends it unless the user vetoed. Mirrors the "
                        "queue's own send_after onto the board so the window is visible — and "
                        "vetoable — from the phone, not only from a JSON file on the VPS."),
    },
    {
        "name": "MR Purchase Date",
        "type": "date",
        "description": ("Date of purchase. Anchors the statute-of-limitations watchdog. Without "
                        "it the outer legal deadline cannot be computed and a case can quietly "
                        "age out. Write a REAL date or leave it blank — a Jan-1 placeholder is "
                        "how a guess becomes a fact the watchdog then trusts."),
    },
    {
        "name": "MR Discovery Date",
        "type": "date",
        "description": ("Date the defect was discovered, or reasonably should have been. Under "
                        "many state consumer statutes (e.g. TX DTPA) the limitations clock runs "
                        "from DISCOVERY rather than purchase, so a long-owned item that just "
                        "failed may still be in time. sol_watchdog prefers this when present and "
                        "says which anchor it used. Same rule: a real date or nothing."),
    },
]

# The two that make the difference between a ladder that climbs to the top and one that
# stops at PreSuit. Called out separately so the report can say so out loud.
TIER4_GATE_PROPS = ("MR Remedy Map", "MR Remedy Attempted")

DEFAULT_WORKSPACE_NAME = "Merchandise Return"
DEFAULT_WORKSPACE_SLUG = "merchandise-return"
DEFAULT_ISSUE_PREFIX = "MER"
DEFAULT_PROJECT_TITLE = "My Return Cases"
DEFAULT_PROJECT_DESCRIPTION = (
    "Every merchandise-return case this user runs, one issue per case. Phase, deadline and "
    "gate state live on the issue's MR properties — never in a chat or a loose file.")

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class OnboardError(Exception):
    """A hard, loud failure. Nothing further is attempted and no profile is written."""


# ------------------------------------------------------------------------------ answers

# Keys accepted in an --answers JSON file. Profile keys must match profile.example.json
# EXACTLY — mer_config is the schema and this script does not get to invent fields.
PROFILE_KEYS = ("legal_name", "email", "phone", "mailing_address", "state", "county",
                "multica_workspace_id", "multica_project_id", "notify", "signature_block",
                "calendar_id", "google_token_file", "llm_providers")
PROVISION_KEYS = ("workspace_name", "workspace_slug", "issue_prefix", "project_title",
                  "project_description", "telegram_chat_id", "notify_channel")

REQUIRED_ANSWERS = ("legal_name", "email", "state", "county")

QUESTIONS = [
    ("legal_name", "Your full legal name, exactly as it should appear on a demand letter", True),
    ("email", "The email account for this engine — YOUR choice of your own mailboxes\n"
              "  (a dedicated returns inbox is recommended, not required)", True),
    ("phone", "Your phone (E.164 preferred, e.g. +15125550147). Blank = no voice escalation", False),
    ("mailing_address", "Your mailing address, one line (used on statutory demand letters)", False),
    ("state", "Your STATE of residence, full name (e.g. Ohio). NEVER defaulted", True),
    ("county", "Your COUNTY of residence, without the word 'County'. NEVER defaulted", True),
    ("workspace_name", "Multica workspace name for your cases", False),
    ("workspace_slug", "Multica workspace slug (lowercase-with-hyphens, PERMANENT)", False),
    ("project_title", "Project title inside that workspace", False),
    ("notify_channel", "How should the engine ping you? 'telegram' or 'none'", False),
    ("telegram_chat_id", "Your numeric Telegram chat id (not a secret; blank to skip)", False),
    ("calendar_id", "Google Calendar id for case deadlines (blank = your email)", False),
    ("google_token_file", "PATH to your mailbox OAuth token JSON (never the token itself;\n"
                          "  blank = derive a default path)", False),
]


def load_answers(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise OnboardError("cannot read --answers file %s: %s" % (path, e))
    if not isinstance(data, dict):
        raise OnboardError("--answers file %s must be a JSON object" % path)
    unknown = [k for k in data
               if k not in PROFILE_KEYS and k not in PROVISION_KEYS and not k.startswith("_")]
    if unknown:
        raise OnboardError(
            "--answers file has key(s) the profile schema does not define: %s\n"
            "  Valid profile keys (see profile.example.json): %s\n"
            "  Valid provisioning keys: %s"
            % (", ".join(sorted(unknown)), ", ".join(PROFILE_KEYS), ", ".join(PROVISION_KEYS)))
    return data


def interview(existing=None, ask=None):
    """Ask the §0 questions in order. `ask` is injectable so the self-test never blocks."""
    if ask is None:
        # Onboarding is an interview, so it needs a terminal. Run headless (cron, CI, `docker exec`
        # without -it, a piped shell) it would otherwise die on the first question with a raw
        # EOFError traceback that says nothing about what to do — a bad first impression for the
        # very first command a new user runs. Turn that into instructions.
        #
        # The guard is the EOF itself, NOT sys.stdin.isatty(): under Git Bash / MSYS on Windows,
        # isatty() returns True even with stdin redirected from /dev/null, so an isatty check
        # silently fails to fire on exactly the platform this repo is developed on.
        def ask(prompt):
            try:
                return input(prompt)
            except EOFError:
                raise OnboardError(
                    "onboarding needs an interactive terminal — stdin reached EOF.\n"
                    "  Either run it interactively (e.g. `docker exec -it <container> … onboard.py`),\n"
                    "  or supply the answers non-interactively:\n"
                    "      onboard.py --answers my-answers.json --dry-run\n"
                    "  See profile.example.json for every field, and references/onboarding.md for\n"
                    "  the order the questions are asked in.")
    a = dict(existing or {})
    print("\n=== §0 ONBOARDING INTERVIEW ===")
    print("Everything below is about YOU. This engine only ever collects your own details.\n")
    for key, prompt, required in QUESTIONS:
        if a.get(key):
            continue
        while True:
            v = (ask("  %s\n    %s: " % (prompt, key)) or "").strip()
            if v or not required:
                break
            print("    ^ required — this one has no default and is never guessed.")
        if v:
            a[key] = v
    return a


def validate_answers(a):
    missing = [k for k in REQUIRED_ANSWERS if not str(a.get(k) or "").strip()]
    if missing:
        raise OnboardError(
            "missing required answer(s): %s\n"
            "  'state' and 'county' in particular are NEVER defaulted — jurisdiction decides\n"
            "  the state AG, the BBB region and the small-claims venue, and a guess sends a\n"
            "  real complaint to the wrong regulator." % ", ".join(missing))
    slug = str(a.get("workspace_slug") or DEFAULT_WORKSPACE_SLUG).strip().lower()
    if not SLUG_RE.match(slug):
        raise OnboardError(
            "workspace_slug %r is not a valid slug. It is PERMANENT and must be lowercase\n"
            "  letters, digits and single hyphens, e.g. 'merchandise-return'." % slug)
    chan = str(a.get("notify_channel") or "none").strip().lower()
    if chan not in ("telegram", "none"):
        raise OnboardError("notify_channel must be 'telegram' or 'none', got %r" % chan)
    return True


# ------------------------------------------------------------------------------- report

class Report(object):
    """One line per item. This IS the deliverable — a run you cannot audit is not idempotent,
    it just looks like it."""

    def __init__(self, live):
        self.live = live
        self.rows = []
        self.warnings = []

    def add(self, kind, name, action, detail=""):
        self.rows.append({"kind": kind, "name": name, "action": action, "detail": detail})
        print("  %-8s %-11s %-24s %s" % (action, kind, name, detail))
        return self.rows[-1]

    def warn(self, msg):
        self.warnings.append(msg)
        print("  WARN     %s" % msg)

    def counts(self):
        c = {}
        for r in self.rows:
            c[r["action"]] = c.get(r["action"], 0) + 1
        return c

    def actions_for(self, kind):
        return {r["name"]: r["action"] for r in self.rows if r["kind"] == kind}


# --------------------------------------------------------------------------- the 6 steps

def step_verify(api, report):
    """Step 1 — connection + token. A bad token must fail HERE, before anything is created."""
    try:
        workspaces = api.whoami()
    except Exception as e:
        raise OnboardError(
            "Multica connection/token check FAILED — nothing was provisioned.\n"
            "  %s\n"
            "  Fix: run `multica login`, then export MULTICA_TOKEN=<token from "
            "~/.multica/config.json>." % e)
    report.add("token", "connection", ADOPTED, "verified — %d workspace(s) visible" % len(workspaces))
    return workspaces


def step_workspace(api, answers, report, live):
    """Step 2 — adopt the workspace if it exists (by id, slug, then name), else create it."""
    name = str(answers.get("workspace_name") or DEFAULT_WORKSPACE_NAME).strip()
    slug = str(answers.get("workspace_slug") or DEFAULT_WORKSPACE_SLUG).strip().lower()
    prefix = str(answers.get("issue_prefix") or DEFAULT_ISSUE_PREFIX).strip().upper()
    want_id = str(answers.get("multica_workspace_id") or "").strip() or None

    found = api.find_workspace(name=name, slug=slug, ws_id=want_id)
    if found:
        report.add("workspace", name, ADOPTED, "id=%s slug=%s" % (found.get("id"), found.get("slug")))
        return found
    if not live:
        report.add("workspace", name, SKIPPED, "would CREATE slug=%s prefix=%s (dry-run)" % (slug, prefix))
        return None
    ws = api.create_workspace(name, slug, issue_prefix=prefix)
    report.add("workspace", name, CREATED, "id=%s slug=%s prefix=%s" % (ws.get("id"), slug, prefix))
    return ws


def step_project(api, answers, report, live, ws_id):
    """Step 3 — a project to hold cases. Adopted by case-insensitive title."""
    title = str(answers.get("project_title") or DEFAULT_PROJECT_TITLE).strip()
    desc = str(answers.get("project_description") or DEFAULT_PROJECT_DESCRIPTION)
    if ws_id is None:
        report.add("project", title, SKIPPED, "would CREATE (workspace not created yet, dry-run)")
        return None
    found = api.find_project(title, ws=ws_id)
    if found:
        report.add("project", title, ADOPTED, "id=%s" % found.get("id"))
        return found
    if not live:
        report.add("project", title, SKIPPED, "would CREATE (dry-run)")
        return None
    pr = api.create_project(title, description=desc, ws=ws_id)
    report.add("project", title, CREATED, "id=%s" % pr.get("id"))
    return pr


def step_properties(api, report, live, ws_id):
    """Step 4 — THE schema. Adopt every property that already exists, create only the rest.

    A type mismatch is a HARD failure: property type is immutable server-side, so a board
    carrying `MR Phase` as text can never be made correct by this script and must not be
    silently accepted.
    """
    if ws_id is None:
        for spec in MR_SCHEMA:
            report.add("property", spec["name"], SKIPPED,
                       "would CREATE %s (workspace not created yet, dry-run)" % spec["type"])
        return {}

    existing = {p.get("name"): p for p in api.list_properties(ws=ws_id)}
    out = {}
    for spec in MR_SCHEMA:
        name, ptype = spec["name"], spec["type"]
        have = existing.get(name)
        if have:
            if (have.get("type") or "") != ptype:
                raise OnboardError(
                    "property %r already exists on this board with type %r, but the engine "
                    "requires %r.\n"
                    "  Property type is IMMUTABLE server-side — this cannot be repaired by "
                    "re-running.\n"
                    "  Fix: archive or rename the existing property, then re-run onboarding."
                    % (name, have.get("type"), ptype))
            detail = "id=%s type=%s" % (have.get("id"), ptype)
            if ptype == "select":
                want = [o[0] for o in spec.get("options") or []]
                got = [o.get("name") for o in ((have.get("config") or {}).get("options") or [])]
                missing = [o for o in want if o not in got]
                if missing:
                    report.add("property", name, ADOPTED, detail + " — INCOMPLETE")
                    report.warn(
                        "%s is missing option(s) %s. The ladder cannot write a phase the board "
                        "does not define, so the case would stall there.\n"
                        "           Fix (option update REPLACES the list; existing options are "
                        "matched by name so ids survive):\n"
                        "           multica property update \"%s\" %s"
                        % (name, ", ".join(missing), name,
                           " ".join('--option "%s:%s"' % (o[0], o[1]) for o in spec["options"])))
                    out[name] = have
                    continue
            report.add("property", name, ADOPTED, detail)
            out[name] = have
            continue
        if not live:
            report.add("property", name, SKIPPED, "would CREATE %s%s (dry-run)"
                       % (ptype, " with %d options" % len(spec["options"]) if spec.get("options") else ""))
            continue
        created = api.create_property(name, ptype, options=spec.get("options"),
                                      description=spec.get("description"),
                                      icon=spec.get("icon"), ws=ws_id)
        report.add("property", name, CREATED, "id=%s type=%s" % (created.get("id"), ptype))
        out[name] = created
    return out


def step_mailbox(answers, report):
    """Step 5 — mailbox status ONLY. This script never handles a credential.

    SKILL §0(A) says the user selects and OAuth-connects their own mailbox. The consent flow
    itself lives in gmail_connect.py (loopback + PKCE, the user's OWN Google client). Onboarding
    verifies rather than assumes: a token FILE existing is not the same as a WORKING mailbox —
    a revoked or wrong-scoped token sits on disk looking perfectly healthy. So when a token is
    present we actually exercise it, and a user is never told "you're set up" when the engine
    cannot send or read a single message.
    """
    token_path = derive_token_path(answers)
    email = answers.get("email", "(email)")

    if not os.path.isfile(token_path):
        report.add("mailbox", email, SKIPPED, "NOT CONNECTED")
        report.warn(
            "no OAuth token at %s — the engine can send/read NOTHING until it exists.\n"
            "           Connect it:  gmail_connect.py --help-setup   (one-time Google setup)\n"
            "                        gmail_connect.py --client-secrets <your client_secret.json>\n"
            "           profile.json stores only this PATH, never the credential."
            % token_path)
        return token_path

    # A file is present — prove it actually works.
    try:
        import gmail_connect
        res = gmail_connect.check(token_path)
    except Exception as e:                      # never let a probe failure abort provisioning
        report.add("mailbox", email, ADOPTED, "token present at %s (unverified: %s)"
                   % (token_path, str(e)[:60]))
        return token_path

    if res.get("ok"):
        who = res.get("email") or ""
        detail = "verified — %s can send and read" % (who or "token refreshes")
        report.add("mailbox", email, ADOPTED, detail)
        # A token for the WRONG mailbox is a quiet disaster: letters would go out over one
        # address while replies were watched on another, and the case would look unanswered.
        if who and email and who.strip().lower() != str(email).strip().lower():
            report.warn(
                "the connected mailbox is %s but the profile says %s. Outbound would be sent\n"
                "           from one address while replies were watched on the other. Fix one of\n"
                "           them before running the engine." % (who, email))
    else:
        report.add("mailbox", email, SKIPPED, "token present but NOT WORKING")
        report.warn(
            "the token at %s did not verify: %s\n"
            "           Reconnect:  gmail_connect.py --client-secrets <your client_secret.json>"
            % (token_path, res.get("reason", "unknown")))
    return token_path


def derive_token_path(answers):
    explicit = str(answers.get("google_token_file") or "").strip()
    if explicit:
        return explicit
    local = str(answers.get("email") or "user").split("@")[0]
    local = re.sub(r"[^A-Za-z0-9_.-]", "_", local) or "user"
    return "/opt/data/%s_google_token.json" % local


# ------------------------------------------------------------------------------- profile

def build_profile(answers, ws_id, project_id):
    """A starter profile.json, keyed EXACTLY to profile.example.json / mer_config's schema.

    No invented fields: mer_config.REQUIRED_FIELDS is
    (legal_name, email, state, county, multica_workspace_id) and the optional set is the
    rest of PROFILE_KEYS. Nothing secret is ever written — google_token_file is a PATH.
    """
    chan = str(answers.get("notify_channel") or "none").strip().lower()
    chat_id = str(answers.get("telegram_chat_id") or "").strip() or None
    addr = answers.get("mailing_address") or ""
    if isinstance(addr, str) and "\n" in addr:
        addr = [ln for ln in addr.split("\n") if ln.strip()]

    sig = answers.get("signature_block")
    if not sig:
        sig = [answers["legal_name"], answers["email"]]
        if answers.get("phone"):
            sig.append(str(answers["phone"]))

    out = {
        "_readme": [
            "profile.json — written by onboard.py (Blueprint M35). Schema: profile.example.json.",
            "NEVER put a password, OAuth token or API key in this file.",
            "'google_token_file' is a PATH to the credential file, not the credential.",
        ],
        "legal_name": str(answers["legal_name"]).strip(),
        "email": str(answers["email"]).strip(),
        "phone": str(answers.get("phone") or "").strip(),
        "mailing_address": addr,
        "state": str(answers["state"]).strip(),
        "county": str(answers["county"]).strip(),
        "multica_workspace_id": ws_id,
        "multica_project_id": project_id,
        "notify": {"channel": chan, "telegram_chat_id": chat_id},
        "signature_block": sig,
        "calendar_id": str(answers.get("calendar_id") or "").strip() or str(answers["email"]).strip(),
        "google_token_file": derive_token_path(answers),
    }
    # Optional, owned by other milestones (M36 llm_providers). Passed through only when the
    # user supplied it — onboarding does not invent model/provider config, and the LLM tier
    # degrades to the heuristic without it. api_key_env names an ENV VAR; never a key value.
    if answers.get("llm_providers"):
        out["llm_providers"] = answers["llm_providers"]
    return out


SECRETISH = re.compile(r"(?i)(?:^|_)(password|passwd|secret|api_key|apikey|token)$")


def assert_no_secrets(profile):
    """Belt and braces: refuse to write a profile that smuggled a credential in.
    google_token_file is explicitly allowed — it is a path, and it is checked as one."""
    for k, v in profile.items():
        if k == "google_token_file":
            if isinstance(v, str) and (v.startswith("ya29.") or v.startswith("mul_")
                                       or len(v) > 400 or v.strip().startswith("{")):
                raise OnboardError("google_token_file must be a PATH, not a credential.")
            continue
        if SECRETISH.search(k):
            raise OnboardError("refusing to write profile: field %r looks like a secret." % k)
        if isinstance(v, str) and (v.startswith("ya29.") or v.startswith("mul_")):
            raise OnboardError("refusing to write profile: field %r holds what looks like a live token." % k)
    return True


def write_profile(profile, out_path, report, live):
    if not live:
        report.add("profile", os.path.basename(out_path), SKIPPED,
                   "would WRITE %d fields (dry-run)" % (len(profile) - 1))
        return None
    assert_no_secrets(profile)
    if os.path.exists(out_path):
        report.add("profile", os.path.basename(out_path), ADOPTED,
                   "already exists at %s — NOT overwritten" % out_path)
        report.warn("an existing profile.json is never clobbered. Delete it or pass --out "
                    "<other path> if you meant to replace it.")
        return out_path
    d = os.path.dirname(os.path.abspath(out_path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
        f.write("\n")
    report.add("profile", os.path.basename(out_path), CREATED, out_path)
    return out_path


# ------------------------------------------------------------------------------ the run

def run(api, answers, live=False, out_path="profile.json", report=None):
    """The whole provision, dry or live, against any object with the multica_api surface."""
    report = report or Report(live)
    print("\n=== MERCHANDISE RETURNS ENGINE — §0 ONBOARDING (%s) ===" % ("LIVE" if live else "DRY RUN"))
    if not live:
        print("Nothing will be created or written. Re-run with --live to apply.\n")
    else:
        print("Creating what is missing; adopting what already exists.\n")

    validate_answers(answers)
    step_verify(api, report)
    ws = step_workspace(api, answers, report, live)
    ws_id = ws.get("id") if ws else None
    pr = step_project(api, answers, report, live, ws_id)
    pr_id = pr.get("id") if pr else None
    step_properties(api, report, live, ws_id)
    step_mailbox(answers, report)

    profile = None
    if ws_id:
        profile = build_profile(answers, ws_id, pr_id)
        write_profile(profile, out_path, report, live)
    else:
        report.add("profile", os.path.basename(out_path), SKIPPED,
                   "would WRITE after the workspace exists (dry-run)")

    c = report.counts()
    print("\nSUMMARY: created=%d adopted=%d skipped=%d warnings=%d"
          % (c.get(CREATED, 0), c.get(ADOPTED, 0), c.get(SKIPPED, 0), len(report.warnings)))
    if not live:
        print("This was a DRY RUN. Re-run with --live to apply the plan above.")
    return report, profile


# ------------------------------------------------------------------------------ selftest

class _StubBoard(object):
    """An in-memory Multica with the exact surface onboard.py uses. No network, ever."""

    def __init__(self, workspaces=None, projects=None, properties=None, token="mul_stub"):
        self.token = token
        self.workspaces = list(workspaces or [])
        self.projects = {k: list(v) for k, v in (projects or {}).items()}
        self.properties = {k: list(v) for k, v in (properties or {}).items()}
        self.calls = []
        self._n = 0

    def _id(self, kind):
        self._n += 1
        return "%s-%04d" % (kind, self._n)

    # -- reads
    def whoami(self):
        self.calls.append("whoami")
        if not self.token:
            raise RuntimeError("MULTICA_TOKEN is not set.")
        if not self.token.startswith("mul_"):
            raise RuntimeError("Multica API GET /workspaces -> 401: invalid token")
        return list(self.workspaces)

    def find_workspace(self, name=None, slug=None, ws_id=None):
        for w in self.workspaces:
            if ws_id and w.get("id") == ws_id:
                return w
        for w in self.workspaces:
            if slug and (w.get("slug") or "").lower() == str(slug).lower():
                return w
            if name and (w.get("name") or "").strip().lower() == str(name).strip().lower():
                return w
        return None

    def find_project(self, title, ws=None):
        want = str(title).strip().lower()
        for p in self.projects.get(ws, []):
            if (p.get("title") or "").strip().lower() == want:
                return p
        return None

    def list_properties(self, ws=None, include_archived=False):
        return list(self.properties.get(ws, []))

    # -- writes
    def create_workspace(self, name, slug, issue_prefix=None, description=None):
        self.calls.append("create_workspace:" + slug)
        if self.find_workspace(slug=slug):
            raise RuntimeError("duplicate workspace slug %s" % slug)
        w = {"id": self._id("ws"), "name": name, "slug": slug, "issue_prefix": issue_prefix}
        self.workspaces.append(w)
        self.projects.setdefault(w["id"], [])
        self.properties.setdefault(w["id"], [])
        return w

    def create_project(self, title, description=None, status=None, icon=None, ws=None):
        self.calls.append("create_project:" + title)
        p = {"id": self._id("pr"), "title": title, "description": description}
        self.projects.setdefault(ws, []).append(p)
        return p

    def create_property(self, name, type, options=None, description=None, icon=None, ws=None):
        self.calls.append("create_property:" + name)
        if any(p.get("name") == name for p in self.properties.get(ws, [])):
            raise RuntimeError("duplicate property %s" % name)
        cfg = {}
        if options:
            cfg["options"] = [{"id": self._id("opt"),
                               "name": o[0] if isinstance(o, (list, tuple)) else o,
                               "color": o[1] if isinstance(o, (list, tuple)) and len(o) > 1 else None}
                              for o in options]
        p = {"id": self._id("prop"), "name": name, "type": type, "config": cfg,
             "description": description, "icon": icon}
        self.properties.setdefault(ws, []).append(p)
        return p


_ANSWERS = {
    "legal_name": "Jordan A. Rivera",
    "email": "jordan.rivera@example.com",
    "phone": "+15125550147",
    "mailing_address": "4120 Cedar Ridge Lane, Austin, TX 78745",
    "state": "Texas",
    "county": "Travis",
    "workspace_name": "Merchandise Return",
    "workspace_slug": "merchandise-return",
    "project_title": "My Return Cases",
    "notify_channel": "telegram",
    "telegram_chat_id": "5550001111",
    "google_token_file": "/nonexistent/jordan_google_token.json",
}


def _quiet(fn, *a, **kw):
    """Run fn with stdout swallowed — the self-test asserts on state, not on chatter."""
    import io
    real, sys.stdout = sys.stdout, io.StringIO()
    try:
        return fn(*a, **kw), sys.stdout.getvalue()
    finally:
        sys.stdout = real


def _selftest():
    import tempfile
    fails = []
    total = [0]

    def check(label, ok, detail=""):
        total[0] += 1
        print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", label, detail))
        if not ok:
            fails.append(label)

    print("=== onboard.py M35 self-test (offline, stubbed API, no network) ===")

    tmp = tempfile.mkdtemp(prefix="mer_onboard_")
    schema_names = [s["name"] for s in MR_SCHEMA]

    # 1. FRESH BOARD, --live: everything is created, exactly once.
    board = _StubBoard()
    (rep, prof), _ = _quiet(run, board, dict(_ANSWERS), True, os.path.join(tmp, "fresh.json"))
    props = rep.actions_for("property")
    check("fresh board creates the workspace", rep.actions_for("workspace").get("Merchandise Return") == CREATED)
    check("fresh board creates the project", rep.actions_for("project").get("My Return Cases") == CREATED)
    check("fresh board creates all %d MR properties" % len(schema_names),
          all(props.get(n) == CREATED for n in schema_names),
          "created=%d" % sum(1 for n in schema_names if props.get(n) == CREATED))
    check("the full property set is present, by name",
          sorted(p["name"] for p in board.list_properties(ws=prof["multica_workspace_id"])) == sorted(schema_names))
    check("MR Phase carries all 9 phase options",
          [o["name"] for o in board.list_properties(ws=prof["multica_workspace_id"])[0]["config"]["options"]]
          == [o[0] for o in PHASE_OPTIONS])
    check("Tier4 gate properties exist (the case_tick fail-closed blocker)",
          all(any(p["name"] == n for p in board.list_properties(ws=prof["multica_workspace_id"]))
              for n in TIER4_GATE_PROPS), ", ".join(TIER4_GATE_PROPS))

    # 2. RE-RUN on the same board: pure adopt, ZERO creates. This is idempotency.
    creates_before = [c for c in board.calls if c.startswith("create_")]
    (rep2, prof2), _ = _quiet(run, board, dict(_ANSWERS), True, os.path.join(tmp, "second.json"))
    creates_after = [c for c in board.calls if c.startswith("create_")]
    c2 = rep2.counts()
    check("re-run creates NOTHING (idempotent)", creates_before == creates_after,
          "%d creates before, %d after" % (len(creates_before), len(creates_after)))
    check("re-run adopts every property", all(rep2.actions_for("property").get(n) == ADOPTED
                                              for n in schema_names))
    board_created = [r for r in rep2.rows
                     if r["action"] == CREATED and r["kind"] in ("workspace", "project", "property")]
    check("re-run reports created=0 on the board", board_created == [],
          "board created=%d adopted=%d (the one CREATED row is the new --out file)"
          % (len(board_created), c2.get(ADOPTED, 0)))
    check("re-run resolves the same workspace id", prof2["multica_workspace_id"] == prof["multica_workspace_id"])

    # 3. THE PARTIAL BOARD: everything except the two Tier-4 gate properties already
    #    exists. Adopt all of those, create exactly the two missing ones. The counts are
    #    derived from MR_SCHEMA, never hardcoded — M44 added `MR Remedy Type` and a literal
    #    "6" here would have failed for a reason that has nothing to do with the behaviour
    #    under test.
    ws = {"id": "ws-king", "name": "Merchandise Return", "slug": "merchandise-return"}
    partial = [{"id": "p%d" % i, "name": s["name"], "type": s["type"],
                "config": {"options": [{"id": "o%d" % j, "name": o[0], "color": o[1]}
                                       for j, o in enumerate(s.get("options") or [])]}}
               for i, s in enumerate(MR_SCHEMA) if s["name"] not in TIER4_GATE_PROPS]
    king = _StubBoard(workspaces=[ws], projects={"ws-king": []}, properties={"ws-king": partial})
    (rep3, prof3), _ = _quiet(run, king, dict(_ANSWERS), True, os.path.join(tmp, "king.json"))
    p3 = rep3.actions_for("property")
    check("partial board adopts the workspace", rep3.actions_for("workspace").get("Merchandise Return") == ADOPTED)
    n_pre_existing = len(MR_SCHEMA) - len(TIER4_GATE_PROPS)
    check("partial board adopts the %d existing properties" % n_pre_existing,
          sum(1 for v in p3.values() if v == ADOPTED) == n_pre_existing,
          "adopted=%d" % sum(1 for v in p3.values() if v == ADOPTED))
    check("partial board creates ONLY the 2 Tier4 gate properties",
          [n for n, v in p3.items() if v == CREATED] == list(TIER4_GATE_PROPS),
          str([n for n, v in p3.items() if v == CREATED]))

    # 4. DRY RUN is the default and writes NOTHING — no API create, no file.
    dry = _StubBoard()
    dry_out = os.path.join(tmp, "dry.json")
    (rep4, prof4), _ = _quiet(run, dry, dict(_ANSWERS), False, dry_out)
    check("dry run makes no API create call", [c for c in dry.calls if c.startswith("create_")] == [])
    check("dry run writes no profile file", not os.path.exists(dry_out))
    check("dry run still reports the full plan",
          rep4.counts().get(SKIPPED, 0) >= len(schema_names) + 2, str(rep4.counts()))

    # 5. A MISSING / INVALID TOKEN FAILS LOUDLY, BEFORE ANYTHING IS PROVISIONED.
    for label, tok in (("missing", ""), ("invalid", "not-a-token")):
        bad = _StubBoard(token=tok)
        out = os.path.join(tmp, "bad_%s.json" % label)
        try:
            _quiet(run, bad, dict(_ANSWERS), True, out)
            check("%s token aborts the run" % label, False, "no exception raised")
        except OnboardError as e:
            check("%s token aborts the run" % label, True, str(e).splitlines()[0][:44])
        check("%s token provisions NOTHING" % label, bad.calls == ["whoami"], str(bad.calls))
        check("%s token writes no profile" % label, not os.path.exists(out))

    # 6. Jurisdiction is never defaulted; a bad slug is caught before any write.
    for label, mutate in (("no state", {"state": ""}), ("no county", {"county": ""}),
                          ("bad slug", {"workspace_slug": "Not A Slug"})):
        b = _StubBoard()
        a = dict(_ANSWERS); a.update(mutate)
        try:
            _quiet(run, b, a, True, os.path.join(tmp, "x.json"))
            check("%s is rejected" % label, False, "no exception raised")
        except OnboardError:
            check("%s is rejected" % label, True, "and %d API calls made" % len(b.calls))

    # 7. A type collision on an adopted property is a HARD failure, not a silent accept.
    clash = _StubBoard(workspaces=[ws], projects={"ws-king": []},
                       properties={"ws-king": [{"id": "x", "name": "MR Phase", "type": "text", "config": {}}]})
    try:
        _quiet(run, clash, dict(_ANSWERS), True, os.path.join(tmp, "clash.json"))
        check("wrong property TYPE aborts the run", False, "no exception raised")
    except OnboardError as e:
        check("wrong property TYPE aborts the run", "IMMUTABLE" in str(e))

    # 8. A select property missing options is adopted but WARNED about, not passed over.
    thin = [dict(p) for p in partial]
    thin[0] = {"id": "p0", "name": "MR Phase", "type": "select",
               "config": {"options": [{"id": "o0", "name": "Intake"}]}}
    warnboard = _StubBoard(workspaces=[ws], projects={"ws-king": []}, properties={"ws-king": thin})
    (rep8, _p8), _ = _quiet(run, warnboard, dict(_ANSWERS), True, os.path.join(tmp, "warn.json"))
    check("an MR Phase missing Tier4 raises a warning", any("Tier4" in w for w in rep8.warnings),
          "%d warning(s)" % len(rep8.warnings))

    # 9. The written profile matches mer_config's schema EXACTLY — no invented fields.
    with open(os.path.join(tmp, "fresh.json"), encoding="utf-8") as f:
        written = json.load(f)
    example_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.example.json")
    try:
        with open(example_path, encoding="utf-8") as f:
            example = json.load(f)
        expected = {k for k in example if not k.startswith("_")}
        got = {k for k in written if not k.startswith("_")}
        # No INVENTED fields: every key written must be one profile.example.json defines.
        # Purely optional keys owned by other milestones (e.g. M36's llm_providers) may be
        # absent — onboarding does not fabricate them.
        check("profile invents no field outside profile.example.json", got <= expected,
              "extra=%s omitted-optional=%s" % (sorted(got - expected), sorted(expected - got)))
        check("profile carries every mer_config REQUIRED_FIELD",
              set(("legal_name", "email", "state", "county", "multica_workspace_id")) <= got)
    except OSError:
        check("profile.example.json readable", False, example_path)
    try:
        import mer_config
        p = mer_config.load(os.path.join(tmp, "fresh.json"))
        check("mer_config loads the written profile", p.email() == _ANSWERS["email"] and p.state() == "Texas",
              "%s / %s" % (p.email(), p.state()))
        check("required fields all satisfied",
              all(not str(written.get(k) or "").strip() == "" for k in mer_config.REQUIRED_FIELDS))
    except ImportError:
        check("mer_config importable", False, "mer_config.py not on the path")

    # 10. No secret can reach disk.
    try:
        assert_no_secrets(dict(written, google_token_file="ya29.a-real-looking-token"))
        check("a credential in google_token_file is refused", False, "no exception raised")
    except OnboardError:
        check("a credential in google_token_file is refused", True)

    print("\n%s — %d/%d checks passed%s"
          % ("SELF-TEST FAILED" if fails else "ALL PASS",
             total[0] - len(fails), total[0],
             ("  |  failed: " + ", ".join(fails)) if fails else ""))
    return 1 if fails else 0


# ---------------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="M35 — provision a Multica board for the Merchandise Returns Engine.")
    ap.add_argument("--live", action="store_true",
                    help="actually create/write. Without this the run is a DRY RUN.")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op flag; dry run is already the default.")
    ap.add_argument("--answers", metavar="PATH",
                    help="JSON file of answers; skips the interactive interview.")
    ap.add_argument("--out", default="profile.json", metavar="PATH",
                    help="where to write the starter profile (default ./profile.json).")
    ap.add_argument("--workspace", metavar="ID",
                    help="adopt this existing workspace id instead of matching by name/slug.")
    ap.add_argument("--selftest", action="store_true",
                    help="run the offline self-test against a stubbed API and exit.")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    import multica_api as api

    try:
        answers = load_answers(args.answers) if args.answers else {}
        if args.workspace:
            answers["multica_workspace_id"] = args.workspace
        if not args.answers:
            answers = interview(answers)
        run(api, answers, live=bool(args.live and not args.dry_run), out_path=args.out)
    except OnboardError as e:
        print("\nONBOARDING ABORTED — nothing further was attempted.\n%s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ncancelled — nothing was created.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
