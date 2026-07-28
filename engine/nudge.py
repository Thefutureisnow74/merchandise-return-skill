#!/usr/bin/env python3
"""
nudge.py — the generalized, time-based follow-up nudge for the Merchandise Returns Engine (M7).

This REPLACES the per-case, hand-armed crons (stride_trace_nudge.py, ppg_tier2_escalation.py).
Instead of one script per case with hard-coded threads, dates and bodies, this walks the whole
board every tick and, for any open case sitting at a Tier1/Tier2 WAIT phase whose deadline is
inside the Day-3 window (1-3 days out) or already elapsed, drafts ONE grounded follow-up nudge
to that case's vendor.

Grounding rule (no invented facts): every fact in a nudge is pulled from the case's own Multica
record — the vendor name, the item, the recipient email, the prior-letter date and the deadline
all come from the issue's structured description blocks (VENDOR/ITEM:, CONTACTS:, SENT:) and the
`MR Phase` / `MR Phase Deadline` properties. Nothing is fabricated. If a required fact (notably
the vendor recipient) cannot be resolved from the record, the nudge is LISTED but never enqueued.

The follow-up body is the Day-3 nudge from the skill's letter-templates (§2 "Retention ask",
Lane 🟡 — the days-3-7 status-request pivot), reduced to only the tokens we can ground.

Gates (M44): a case parked on `MR Awaiting User YES` is never nudged, and a CLIENT case is
never nudged at all — a nudge is an outbound to a third party sent on somebody else's behalf,
which needs that client's written authorization. An elapsed deadline is not consent.

Modes:
  (default)   dry-run — list every due nudge (subject + body + recipient). Writes NOTHING.
  --enqueue   enqueue each due nudge via send_queue with a 0-hour window. Sending itself is still
              governed by mer_send / MER_ENGINE_SEND (off/test/live) at process() time — this
              script never sends. In TEST mode the queued item redirects to King.

Idempotency (never enqueue the same nudge twice for the same case+phase):
  the action key is  nudge_<identifier>_<phase>  (e.g. nudge_MER-3_Tier1). Before enqueuing we
  skip the case if (a) a non-vetoed queue record already carries that case+action, or (b) the
  send-time idempotency ledger shows that exact logical send already went out.

Run it the same way as case_tick.py (inside the container, with /opt/data/.env sourced).
"""
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import multica_api as mc      # noqa: E402
import send_queue             # noqa: E402
import remedy_map             # noqa: E402  — PHASE_LEVER: which court-gate lever a send satisfies
import idempotency            # noqa: E402
import mer_config             # noqa: E402  (M32 — identity comes from the profile, not a literal)
import case_queries           # noqa: E402  (M33 — the shared description-block parser)
import businessday            # noqa: E402  — "today" in the USER's timezone, not the UTC container's

BUILD_PROJECT = "13805886-7b4d-45bf-b985-32128f91b288"  # engine-build milestones — skip
NUDGE_PHASES = ("Tier1", "Tier2")   # only WAIT-on-vendor tiers get a time-based follow-up
DAY3 = 3                            # "Day-3 window" = deadline is 1..3 days out
# The user sends every letter under their own name/email (single-user model, letter-templates.md).
# M32: resolved from the profile at call time — no personal literal lives in this module.
# M41: there is no built-in fallback any more. With no profile configured, signing a letter raises
# rather than quietly signing it with somebody else's name — which is the correct outcome for a
# module whose entire job is putting a signature on outbound mail.


def sender_signature():
    """How the nudge is signed — the profile's signature block (default: 'Name\\nemail')."""
    return mer_config.signature_block()

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------- record parsing (grounding)

# The block parser and vendor parser live in case_queries.py and are imported, not re-implemented
# — one parser at runtime. The direction matters: parsing is the LOWER layer, so a read-only
# consumer (inbox_watcher) can use it without transitively importing this module's send path.
_block = case_queries._block
parse_vendor = case_queries.parse_vendor


# ---------------------------------------------------------------- client-case gate (M44)
# Detection is the shared CONVENTION, not a copy of a roster: `CLIENT:` title prefix, a
# `CLIENT CASE` description marker, or any board property whose NAME mentions "client" with an
# affirmative value. case_tick owns the canonical implementation; it is imported LAZILY so this
# module keeps its dependency direction (nudge is a consumer, and case_tick imports nudge back
# inside main()). If the import is unavailable the two convention checks are done inline — the
# gate must never silently become a no-op because of an import error.
_CLIENT_AFFIRMATIVE = {"yes", "true", "y", "1", "client", "client case", "on", "checked", "✓"}


def _is_client_case(issue):
    try:
        import case_tick
        return case_tick.is_client_case(issue)
    except Exception:
        issue = issue or {}
        if (issue.get("title") or "").strip().upper().startswith("CLIENT:"):
            return True
        if (issue.get("description") or "").lstrip().upper().startswith("CLIENT CASE"):
            return True
        for name, val in (issue.get("mr") or {}).items():
            if "client" in str(name).lower() and str(val).strip().lower() in _CLIENT_AFFIRMATIVE:
                return True
        return False


def _tier_num(phase):
    m = re.search(r"(\d+)", phase or "")
    return m.group(1) if m else ""


def parse_item(desc):
    """Item phrase = the first sentence after the dash in VENDOR/ITEM, minus a leading 'sold '."""
    vi = _block(desc, "VENDOR/ITEM")
    parts = re.split(r"\s[—–-]\s", vi, maxsplit=1)
    if len(parts) < 2:
        return ""
    right = parts[1]
    right = re.split(r"\.\s+(?:Defect|The defect)\b", right, maxsplit=1)[0]   # drop the defect clause
    right = right.split(". ", 1)[0].rstrip(". ").strip()
    right = re.sub(r"(?i)^sold\s+", "", right)
    return right


def parse_recipient(desc, tier):
    """The email we already wrote to at this tier: prefer the SENT log, then the CONTACTS list."""
    for label in ("SENT", "CONTACTS"):
        block = _block(desc, label)
        if not block:
            continue
        # e.g. "Tier 1 letter to consumer.affairs@ppg.com" / "Tier 1: consumer.affairs@ppg.com"
        m = re.search(r"Tier\s*%s\b[^.\n]*?([A-Za-z0-9._%%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
                      % re.escape(tier), block)
        if m:
            return m.group(1)
    return ""


def parse_prior_date(desc, tier):
    """The date the current tier's letter was sent (from the SENT block)."""
    sent = _block(desc, "SENT")
    if not sent:
        return ""
    m = re.search(r"Tier\s*%s\b.*?(\d{4}-\d{2}-\d{2})" % re.escape(tier), sent)
    if m:
        return m.group(1)
    m = _DATE_RE.search(sent)
    return m.group(0) if m else ""


# ---------------------------------------------------------------- draft (from letter-templates §2)

def build_nudge(vendor, item, prior_date, deadline):
    """Grounded Day-3 follow-up (letter-templates §2 'Retention ask'), only grounded tokens filled."""
    subj_bits = ["Following up"]
    if prior_date:
        subj_bits.append("on my %s request" % prior_date)
    if vendor:
        subj_bits.append("to %s" % vendor)
    subject = " ".join(subj_bits)
    if item:
        subject += " — %s" % item[:60]

    greet = "Dear %s Customer Care," % vendor if vendor else "Hello,"
    ref = "my earlier message" + (" of %s" % prior_date if prior_date else "")
    about = (" regarding %s" % item) if item else ""
    deadline_line = (
        " I asked for a written response, with how you intend to resolve this, by %s."
        % deadline.isoformat() if deadline else ""
    )
    body = (
        "%s\n\n"
        "I am following up on %s%s.%s I have not yet received a substantive reply.\n\n"
        "I would still much prefer to resolve this directly with you. Could you please confirm, "
        "in writing, how and when this will be resolved? If it needs someone with authority to "
        "approve the remedy, please route it to your retention or refunds team.\n\n"
        "Thank you,\n%s\n"
        % (greet, ref, about, deadline_line, sender_signature())
    )
    return subject, body


# ---------------------------------------------------------------- selection

def _parse_deadline(raw):
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _was_vetoed(case, action):
    """True if a DURABLE veto record says the user already killed this action.

    Imported defensively and resolved at CALL time, not import time: the durable veto ledger
    is being added by another workstream, and this module must land and keep working whether
    or not that lands first. A missing lookup means "not vetoed" — the pre-existing
    behaviour, never an exception and never a silent block.
    """
    try:
        fn = getattr(idempotency, "was_vetoed", None)
        if fn is None:
            return False
        return bool(fn(case, action))
    except Exception:
        return False


def _already_handled(case, action, to, body):
    """True if this nudge was already enqueued (pending, un-vetoed), already sent, or VETOED.

    THE VETO HOLE (2026-07-28): the queue record check below only sees a LIVE queue record,
    and a vetoed record is skipped by the `not r.get("vetoed")` test — so once the queue was
    processed and pruned, the user's veto evaporated and the very same nudge was re-enqueued
    the next morning. A veto that expires overnight is not a veto. A durable veto record now
    outranks everything here.
    """
    if _was_vetoed(case, action):
        return True
    for r in send_queue._load():
        if r.get("case") == case and r.get("action") == action and not r.get("vetoed"):
            return True
    if to and idempotency.was_sent(case, action, to, body):
        return True
    return False


def due_nudges(today=None):
    """Every open Tier1/Tier2 case whose deadline is in the Day-3 window or elapsed → a nudge draft.

    Returns a list of dicts:
      {identifier, case, phase, deadline, days, vendor, item, to, subject, body, action, ready}
    `ready` is False when the vendor recipient could not be grounded from the record (list-only).
    """
    # USER-timezone today: the container runs UTC, where date.today() is already
    # tomorrow from 19:00 CT — a nudge window would open (and a deadline read as
    # elapsed) a full day early every evening.
    today = today or businessday.today()
    out = []
    for it in mc.list_issues():
        if (it.get("status") or "") in ("done", "cancelled"):
            continue
        if it.get("project_id") == BUILD_PROJECT:
            continue
        p = it.get("mr", {})
        # GATE — a case parked on the user's explicit YES must not be nudged. This became
        # load-bearing with M37 (2026-07-26): once the ladder actually climbs, advancing
        # Tier1 -> Tier2 sets `MR Awaiting User YES` AND drops the case into the nudge window,
        # so without this check the engine could fire a Tier-2 outbound at a brand-new
        # counterparty that the user has not approved. It was previously throttled only by
        # `parse_recipient` finding no Tier-2 address in the description — a data accident,
        # not a gate.
        if str(p.get("MR Awaiting User YES", "")).lower() in ("true", "1", "yes", "✓"):
            continue
        # GATE — a CLIENT case is never auto-nudged. A nudge is an outbound to a third party
        # sent on somebody else's behalf, and that needs the client's WRITTEN authorization;
        # a deadline elapsing is not consent. Same CONVENTION the rest of the engine uses
        # (case_tick.is_client_case / mer_engine.is_client_case), and the same one-way
        # fail-safe: any single signal is enough. The case is simply skipped — a human writes
        # the client's follow-up, which is what the workflow already required.
        if _is_client_case(it):
            continue
        phase = p.get("MR Phase")
        if phase not in NUDGE_PHASES:
            continue
        deadline = _parse_deadline(p.get("MR Phase Deadline"))
        if not deadline:
            continue
        days = (deadline - today).days
        if days > DAY3:            # still comfortably WAIT — no nudge yet
            continue
        ident = it["identifier"]
        desc = it.get("description") or ""
        tier = _tier_num(phase)
        vendor = parse_vendor(desc)
        item = parse_item(desc)
        to = parse_recipient(desc, tier)
        prior_date = parse_prior_date(desc, tier)
        subject, body = build_nudge(vendor, item, prior_date, deadline)
        action = "nudge_%s_%s" % (ident, phase)
        out.append({
            "identifier": ident, "case": ident, "phase": phase,
            "deadline": deadline, "days": days, "vendor": vendor, "item": item,
            "to": to, "subject": subject, "body": body, "action": action,
            "ready": bool(to),
        })
    return out


# ---------------------------------------------------------------- enqueue (the case_tick hook)

def enqueue_due(today=None, verbose=False):
    """Enqueue every due, ready, not-yet-handled nudge (window 0). Returns the list enqueued.

    This is the entry point case_tick.py calls when it sees a NUDGE. Sending itself still runs
    through send_queue.process() -> mer_send (MER_ENGINE_SEND off/test/live) — this only queues.
    """
    enqueued = []
    for n in due_nudges(today):
        if not n["ready"] or _already_handled(n["case"], n["action"], n["to"], n["body"]):
            continue
        # lever: the send_queue marks this lever ATTEMPTED only after the transport confirms the
        # letter actually went — not here, because a queued letter can still be vetoed, and a
        # vetoed letter marked attempted would open the court gate on a lever nobody pulled.
        # Tier3 is deliberately absent from PHASE_LEVER (four separate portal filings — only the
        # person who filed knows which), so it stays None and is logged by hand via the CLI.
        rid = send_queue.enqueue(n["case"], n["to"], n["subject"], n["body"],
                                 action=n["action"], window_hours=0,
                                 lever=remedy_map.PHASE_LEVER.get(n.get("phase")))
        n["queue_id"] = rid
        enqueued.append(n)
        if verbose:
            print("   -> ENQUEUED %s id=%s (%s)" % (n["identifier"], rid, n["action"]))
    return enqueued


# ---------------------------------------------------------------- CLI

def main():
    enqueue = "--enqueue" in sys.argv
    today = businessday.today()
    nudges = due_nudges(today)
    print("=== nudge (M7)  %s  (%s) ===" % (today.isoformat(), "ENQUEUE" if enqueue else "DRY-RUN"))
    print("mode: time-based Tier1/Tier2 follow-ups; Day-3 window or elapsed\n")

    if not nudges:
        print("  (no cases in the Day-3 nudge window)")
        return

    for n in nudges:
        when = "ELAPSED %dd ago" % -n["days"] if n["days"] < 0 else "%dd to deadline" % n["days"]
        print("-- %s  [%s]  %s (due %s)" % (n["identifier"], n["phase"], when, n["deadline"]))
        print("   vendor : %s" % (n["vendor"] or "(unresolved)"))
        print("   to     : %s" % (n["to"] or "(UNRESOLVED — list-only, will not enqueue)"))
        print("   action : %s" % n["action"])
        print("   subject: %s" % n["subject"])
        print("   body:")
        for line in n["body"].rstrip("\n").split("\n"):
            print("     | %s" % line)

        if not enqueue:
            print("   -> dry-run: not enqueued.\n")
            continue
        if not n["ready"]:
            print("   -> SKIP: no grounded vendor recipient — not enqueued.\n")
            continue
        if _already_handled(n["case"], n["action"], n["to"], n["body"]):
            print("   -> SKIP: this nudge was already enqueued/sent (idempotent).\n")
            continue
        # lever: the send_queue marks this lever ATTEMPTED only after the transport confirms the
        # letter actually went — not here, because a queued letter can still be vetoed, and a
        # vetoed letter marked attempted would open the court gate on a lever nobody pulled.
        # Tier3 is deliberately absent from PHASE_LEVER (four separate portal filings — only the
        # person who filed knows which), so it stays None and is logged by hand via the CLI.
        rid = send_queue.enqueue(n["case"], n["to"], n["subject"], n["body"],
                                 action=n["action"], window_hours=0,
                                 lever=remedy_map.PHASE_LEVER.get(n.get("phase")))
        print("   -> ENQUEUED id=%s (window 0; sends per MER_ENGINE_SEND at process time)\n" % rid)


# ---------------------------------------------------------------- self-test (M38)

def _issue(ident, phase="Tier1", deadline="2026-07-28", awaiting=False, status="in_progress",
           desc=None, project_id="pj-live"):
    """A fabricated board issue in exactly the shape multica_api.list_issues() returns."""
    if desc is None:
        desc = (
            "VENDOR/ITEM: Acme Tools (store, DFW) — a cordless drill purchased 2026-06-14. "
            "Defect: the chuck seized.\n"
            "\n"
            "CONTACTS:\n"
            "Tier 1: care@acme.example\n"
            "\n"
            "SENT:\n"
            "Tier 1 letter to care@acme.example on 2026-07-01\n"
        )
    mr = {"MR Phase": phase, "MR Phase Deadline": deadline}
    if awaiting is not None:
        mr["MR Awaiting User YES"] = awaiting
    return {"identifier": ident, "id": "id-" + ident, "status": status,
            "project_id": project_id, "description": desc, "mr": mr}


def _selftest():
    """Offline proof of the nudge gates. No network, no send: everything is stubbed/tempfiled."""
    import json as _json
    import tempfile

    os.environ["MER_ENGINE_SEND"] = "off"

    # M41: there is no built-in identity any more, and build_nudge() signs a real letter body — so
    # the self-test must supply a profile of its own. A FICTIONAL one, deliberately: a test that
    # borrowed the operator's identity would pass on their machine and fail on everyone else's,
    # and would put a real person's details in test output.
    if not os.environ.get("MER_PROFILE"):
        _p = os.path.join(tempfile.mkdtemp(prefix="mer_nudge_test_"), "profile.json")
        with open(_p, "w", encoding="utf-8") as _f:
            _json.dump({
                "legal_name": "Jordan A. Rivera",
                "email": "jordan.rivera@example.com",
                "state": "Ohio", "county": "Franklin",
                "multica_workspace_id": "ffffffff-1111-4444-8888-aaaaaaaaaaaa",
                "signature_block": "Jordan A. Rivera\njordan.rivera@example.com",
            }, _f)
        os.environ["MER_PROFILE"] = _p
        mer_config.reset()

    fails = []

    def check(label, cond, detail=""):
        ok = bool(cond)
        print("  %-4s %s%s" % ("PASS" if ok else "FAIL", label, ("  [%s]" % detail) if detail else ""))
        if not ok:
            fails.append(label)

    print("=== nudge M38 gate self-test (offline, fabricated board) ===")

    # --- isolate every side-effecting path from the live system.
    live_queue = "/opt/data/mer_send_queue.json"
    tmpdir = tempfile.mkdtemp(prefix="nudge_test_")
    send_queue.QUEUE = os.path.join(tmpdir, "queue.json")
    idempotency.LEDGER = os.path.join(tmpdir, "ledger.json")
    check("test queue is NOT the live queue", send_queue.QUEUE != live_queue, send_queue.QUEUE)

    def _no_send(*a, **k):
        raise AssertionError("NUDGE SELF-TEST TRIED TO SEND — mer_send.send was reached")
    send_queue.mer_send.send = _no_send
    try:
        import gmail_transport
        gmail_transport.send_mime = _no_send
    except Exception:
        pass

    # --- the board is fabricated; multica_api never goes to the network.
    board = []

    def fake_list_issues(ws=None):
        return [dict(x) for x in board]
    mc.list_issues = fake_list_issues

    TODAY = date(2026, 7, 26)

    def only(*issues):
        board[:] = list(issues)
        return due_nudges(TODAY)

    # 1. THE 2026-07-26 GATE — `MR Awaiting User YES` parks the case: no nudge, any truthy form.
    # (ascii labels: this self-test must print identically on a cp1252 console)
    for truthy, label in ((True, "True"), ("true", "'true'"), ("True", "'True'"),
                          ("yes", "'yes'"), ("1", "'1'"), ("✓", "u+2713 checkmark")):
        n = only(_issue("MER-A", phase="Tier1", deadline="2026-07-27", awaiting=truthy))
        check("Awaiting-User-YES=%s yields NO nudge" % label, n == [], "%d nudge(s)" % len(n))

    # ...and a falsey/absent flag does not block a genuinely due case.
    for falsey, label in ((False, "False"), ("false", "'false'"), ("", "empty"), (None, "absent")):
        n = only(_issue("MER-A", phase="Tier1", deadline="2026-07-27", awaiting=falsey))
        check("Awaiting-User-YES=%s still nudges" % label, len(n) == 1, "%d nudge(s)" % len(n))

    # 2. the Day-3 window.
    n = only(_issue("MER-B", deadline="2026-08-20"))
    check("deadline 25d out -> no nudge (outside Day-3 window)", n == [], "%d" % len(n))
    n = only(_issue("MER-B", deadline="2026-07-30"))
    check("deadline 4d out -> no nudge (just outside the window)", n == [], "%d" % len(n))
    n = only(_issue("MER-B", deadline="2026-07-29"))
    check("deadline 3d out -> nudge (edge of the window)", len(n) == 1 and n[0]["days"] == 3)
    n = only(_issue("MER-B", deadline="2026-07-20"))
    check("elapsed deadline -> nudge", len(n) == 1 and n[0]["days"] == -6)
    n = only(_issue("MER-B", deadline=None))
    check("no deadline -> no nudge (clock not armed)", n == [], "%d" % len(n))
    n = only(_issue("MER-B", deadline="not-a-date"))
    check("unparseable deadline -> no nudge", n == [], "%d" % len(n))

    # 3. only WAIT-on-vendor tiers, only open cases, never the engine-build project.
    for ph in ("Intake", "CaseFile", "Tier3", "PreSuit", "Tier4", "Closed", None):
        n = only(_issue("MER-C", phase=ph, deadline="2026-07-27"))
        check("phase=%r -> no nudge" % (ph,), n == [], "%d" % len(n))
    check("Tier2 IS a nudge phase", len(only(_issue("MER-C", phase="Tier2",
                                                    deadline="2026-07-27"))) == 1)
    for st in ("done", "cancelled"):
        n = only(_issue("MER-C", status=st, deadline="2026-07-27"))
        check("status=%s -> no nudge" % st, n == [], "%d" % len(n))
    n = only(_issue("MER-C", deadline="2026-07-27", project_id=BUILD_PROJECT))
    check("engine-build project is never nudged", n == [], "%d" % len(n))

    # 3b. M44 — a CLIENT case is never auto-nudged, by any of the three convention signals.
    #     A nudge is an outbound to a third party on somebody else's behalf: it needs the
    #     client's written authorization, and an elapsed deadline is not consent.
    base = _issue("MER-CL", deadline="2026-07-27")
    cl_title = dict(base, title="CLIENT: Jordan Rivera - a cordless drill via Acme Tools")
    check("CLIENT:-titled case yields NO nudge", only(cl_title) == [],
          "%d" % len(only(cl_title)))
    cl_desc = dict(base, description="CLIENT CASE. " + base["description"])
    check("'CLIENT CASE' description marker yields NO nudge", only(cl_desc) == [],
          "%d" % len(only(cl_desc)))
    cl_prop = dict(base, mr=dict(base["mr"], **{"MR Client Case": "yes"}))
    check("affirmative MR Client Case property yields NO nudge", only(cl_prop) == [],
          "%d" % len(only(cl_prop)))
    check("the same case WITHOUT a client signal still nudges", len(only(base)) == 1)
    # ...and nothing can be enqueued for one either.
    send_queue._save([])
    if os.path.exists(idempotency.LEDGER):
        os.remove(idempotency.LEDGER)
    board[:] = [cl_title]
    got = enqueue_due(TODAY)
    check("a client case enqueues NOTHING", got == [] and send_queue._load() == [],
          "%d enqueued" % len(got))

    # 4. an ungrounded recipient is ready=False and is NEVER auto-sent/enqueued.
    bare = "VENDOR/ITEM: Acme Tools — a cordless drill.\n"
    n = only(_issue("MER-D", deadline="2026-07-27", desc=bare))
    check("no derivable recipient -> a nudge row exists", len(n) == 1)
    check("no derivable recipient -> ready=False", n and n[0]["ready"] is False)
    check("no derivable recipient -> to is empty", n and n[0]["to"] == "")
    send_queue._save([])
    got = enqueue_due(TODAY)
    check("no derivable recipient -> nothing enqueued", got == [] and send_queue._load() == [],
          "%d enqueued" % len(got))

    # 5. the grounded happy path, and grounding of the drafted body itself.
    n = only(_issue("MER-E", deadline="2026-07-27"))
    d = n[0]
    check("grounded case resolves vendor", d["vendor"] == "Acme Tools", d["vendor"])
    check("grounded case resolves recipient", d["to"] == "care@acme.example", d["to"])
    check("grounded case is ready=True", d["ready"] is True)
    check("action key is case+phase scoped", d["action"] == "nudge_MER-E_Tier1", d["action"])
    try:
        import draft_reply
        corpus = "\n".join([board[0]["description"], d["deadline"].isoformat(),
                            sender_signature()])
        bad = draft_reply.ungrounded_claims(d["subject"] + "\n" + d["body"], corpus)
        check("nudge body invents no amount/date/reference", bad == [], str(bad))
    except Exception as e:                                     # pragma: no cover
        check("nudge body invents no amount/date/reference", False, "checker error: %s" % e)

    # 6. _already_handled suppresses a repeat — via the pending queue...
    send_queue._save([])
    if os.path.exists(idempotency.LEDGER):
        os.remove(idempotency.LEDGER)
    first = enqueue_due(TODAY)
    check("first pass enqueues the due nudge", len(first) == 1, "%d" % len(first))
    second = enqueue_due(TODAY)
    check("second pass enqueues NOTHING (pending queue suppresses)", second == [],
          "%d" % len(second))
    check("_already_handled is True while queued",
          _already_handled(d["case"], d["action"], d["to"], d["body"]) is True)
    check("queue holds exactly one copy", len(send_queue._load()) == 1)

    # ...and via the send-time idempotency ledger once the queue has drained.
    send_queue._save([])
    check("_already_handled is False on a drained queue + empty ledger",
          _already_handled(d["case"], d["action"], d["to"], d["body"]) is False)
    idempotency.reserve(d["case"], d["action"], d["to"], d["body"])
    check("_already_handled is True from the sent ledger",
          _already_handled(d["case"], d["action"], d["to"], d["body"]) is True)
    check("already-sent nudge is not re-enqueued", enqueue_due(TODAY) == [])

    # 7. a DIFFERENT phase for the same case is a different nudge (the ladder can still climb).
    n2 = only(_issue("MER-E", phase="Tier2", deadline="2026-07-27"))
    check("Tier2 nudge has its own action key", n2[0]["action"] == "nudge_MER-E_Tier2",
          n2[0]["action"])

    # 8. structural: nothing in this module can send — it only enqueues.
    # AST, not grep — docstrings that MENTION mer_send must not count as calling it.
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    tree.body = [nd for nd in tree.body
                 if not (isinstance(nd, ast.FunctionDef) and nd.name in ("_selftest", "_issue"))]
    called = set()
    for nd in ast.walk(tree):
        if isinstance(nd, ast.Call):
            called.add(ast.unparse(nd.func))
    check("nudge's production code never calls a send primitive",
          not any(c.startswith("mer_send.") or c == "send_queue.process" for c in called),
          ", ".join(sorted(c for c in called if "send" in c)))
    check("nudge only ever enqueues", "send_queue.enqueue" in called)

    # 9. (2026-07-28) A VETO MUST SURVIVE THE NIGHT.
    #    _already_handled only ever consulted a LIVE queue record — and a vetoed record is
    #    explicitly skipped by that check — so once the queue drained, the user's veto was
    #    gone and the identical nudge went back in the next morning. The durable veto lookup
    #    is imported DEFENSIVELY (it is another workstream's), so absence must read as
    #    "not vetoed" and never as an error or a silent block.
    send_queue._save([])
    if os.path.exists(idempotency.LEDGER):
        os.remove(idempotency.LEDGER)
    n = only(_issue("MER-V", deadline="2026-07-27"))
    dv = n[0]
    had = hasattr(idempotency, "was_vetoed")
    saved = getattr(idempotency, "was_vetoed", None)
    try:
        if had:
            del idempotency.was_vetoed
        check("a MISSING veto lookup reads as 'not vetoed' (no crash, no silent block)",
              _was_vetoed(dv["case"], dv["action"]) is False
              and _already_handled(dv["case"], dv["action"], dv["to"], dv["body"]) is False)
        check("...and the nudge still enqueues normally without it", len(enqueue_due(TODAY)) == 1)

        send_queue._save([])
        if os.path.exists(idempotency.LEDGER):
            os.remove(idempotency.LEDGER)
        idempotency.was_vetoed = lambda case, action: (case == dv["case"]
                                                       and action == dv["action"])
        check("a DURABLE veto suppresses the nudge on a drained queue",
              _already_handled(dv["case"], dv["action"], dv["to"], dv["body"]) is True)
        check("...so the vetoed nudge is NOT re-enqueued the next morning",
              enqueue_due(TODAY) == [] and send_queue._load() == [])
        idempotency.was_vetoed = lambda case, action: (_ for _ in ()).throw(RuntimeError("boom"))
        check("a RAISING veto lookup degrades to 'not vetoed', never to an exception",
              _was_vetoed(dv["case"], dv["action"]) is False)
    finally:
        if hasattr(idempotency, "was_vetoed"):
            del idempotency.was_vetoed
        if had:
            idempotency.was_vetoed = saved
    check("the veto lookup is resolved at CALL time, not bound at import",
          "getattr(idempotency, \"was_vetoed\", None)" in src,
          "so it picks the function up whenever the other workstream lands it")

    # 10. (2026-07-28) TIMEZONE — the nudge window must open on the USER's day.
    check("nudge takes 'today' from the profile timezone, never the UTC container",
          not any(c == "date.today" for c in called),
          "date.today() on a UTC container is already tomorrow from 19:00 CT")
    check("businessday.today is what it calls instead", "businessday.today" in called)

    if fails:
        print("\nFAIL — %d gate(s) wrong: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("\nPASS — awaiting-YES parks the case, the window holds, ungrounded never sends, "
          "repeats are suppressed.")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    main()
