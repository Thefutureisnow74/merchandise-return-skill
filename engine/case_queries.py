#!/usr/bin/env python3
"""
case_queries.py — board-derived per-case Gmail queries (Blueprint M33).

WHY THIS EXISTS
---------------
Until M33 the engine discovered case mail through a HAND-MAINTAINED table of King's own cases,
written out twice:

    mer_engine.CASE_QUERIES      {"MER-1": "from:stridebank.com OR from:gohighlevel.com", ...}
    inbox_watcher.CASE_QUERIES   the same table again, with a comment admitting the two
                                 "must be kept in sync" — i.e. a drift bug with a countdown on it.

Two consequences, both fatal to the product:
  1. A brand-new user's cases are INVISIBLE to the engine. Their case exists on their board and
     the watcher never looks at it, because the watcher only knows the five rows someone typed
     into a Python literal. Portability was blocked on this.
  2. Two copies of the same fact drift. They already had: mer_engine carried MER-21, inbox_watcher
     did not; inbox_watcher carried `from:nikeclaims.com`, mer_engine did not.

M33 deletes both tables. The Gmail query for a case is now DERIVED AT RUNTIME from that case's own
record on the Multica board, so adding a case is a board action, not a code edit, and there is
exactly one implementation of the derivation — this module.

WHERE THE ANSWER COMES FROM (in order, all unioned)
---------------------------------------------------
  1. an explicit `MAIL FROM:` block in the description, or an `MR Mail From` / `MR Vendor Domains`
     board property. This is the escape hatch for anything the record cannot imply: a secondary
     domain the vendor actually answers from (HighLevel replies from
     `support@gohighlevelassist.freshdesk.com`, which `from:gohighlevel.com` never sees — the
     documented MER-1 miss), or a bare Gmail token like `rebound` for a returns processor that has
     no address in the record at all.
  2. email addresses found in the record's structured blocks — `CONTACTS:`, `SENT:`, `VENDOR/ITEM:`
     — parsed with nudge.py's block parser. That parser is IMPORTED, not re-implemented: nudge
     already grounds vendor/recipient out of these exact blocks, and a second parser here would
     recreate the duplication M33 exists to kill.
  3. if the record has none of those blocks (client-intake cases are written as prose), a
     whole-description address scan as a last resort.

SAFETY — WHY A CASE IS SOMETIMES SKIPPED INSTEAD OF GUESSED
-----------------------------------------------------------
The dangerous failure here is not "no query", it is "too broad a query". A case watched with
`from:gmail.com` pulls every unrelated personal email into that case, where it is classified,
recorded on the board, and — in the send lanes — potentially replied to. So:

  * a free-mail / shared-helpdesk domain NEVER becomes a `from:<domain>` scope. Only the full
    address is used (`from:sam.okafor.demo@gmail.com`, never `from:gmail.com`). Subdomains are
    exempt, which is exactly what lets `gohighlevelassist.freshdesk.com` through while
    `freshdesk.com` alone is refused.
  * the profile owner's own address and domain are stripped (mer_config identity, never a literal).
  * a case that yields NOTHING is SKIPPED and LOGGED with what is missing. It is never given a
    fallback query. Silence about a skipped case would be the worst outcome of all, so every skip
    prints one line naming the case and the fix.

Public API
----------
    resolve(issue)                  -> CaseQuery  (never raises on bad data; .skipped says why)
    resolve_all(issues)             -> (OrderedDict identifier->query, [skipped CaseQuery, ...])
    case_queries(issues=None)       -> OrderedDict identifier->query  (fetches the board if needed)
    live_case_queries()             -> lazy read-only Mapping of the above (back-compat view for
                                       unmatched_review.py, which imports mer_engine.CASE_QUERIES)
    eligible_issues(issues)         -> the open, non-build-milestone issues worth watching

CLI:
    case_queries.py                 offline self-test (no network)
    case_queries.py --board         resolve against the LIVE board and print (read-only)
"""
import os
import re
import sys

try:                                  # py3
    from collections.abc import Mapping
except ImportError:                   # pragma: no cover
    from collections import Mapping

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import mer_config                     # noqa: E402  identity (self-address filter) — never a literal

# THE description-block parser lives HERE, and nudge.py imports it from this module — not the
# other way round. M33 first had case_queries import nudge, which meant inbox_watcher (a
# read-only watcher that must never be able to send) transitively imported nudge -> send_queue ->
# mer_send. Reversing the direction keeps the single shared parser while putting the send path
# out of a watcher's reach entirely: dependencies point from the sending modules DOWN to the
# parsing ones, never up.


def _block(desc, label):
    """Return the text of a 'LABEL: ...' block from the description, up to the next blank line."""
    if not desc:
        return ""
    m = re.search(r"(?ms)^\s*%s\s*:\s*(.+?)(?:\n\s*\n|\Z)" % re.escape(label), desc)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def parse_vendor(desc):
    """Vendor name = the text before the first em/en/hyphen dash in the VENDOR/ITEM block."""
    vi = _block(desc, "VENDOR/ITEM")
    if not vi:
        return ""
    left = re.split(r"\s[—–-]\s", vi, maxsplit=1)[0]
    left = re.sub(r"\s*\(.*?\)\s*$", "", left).strip()   # drop a trailing "(store, DFW)"
    return left


# =========================================================================================
# M52 — SPEAK IN PLAIN NAMES. "MER-76" is a database key, not a name.
#
# On 2026-07-29 the user said, of his own cases: *"when you use MER I dont know what it means."*
# He was right, and it had been happening for two days: status reports, next-actions and whole
# summaries were written in MER-1 / MER-3 / MER-76, which are Multica's auto-generated row ids.
# They are perfect for the engine and meaningless to the person whose money is at stake.
#
# Worse than meaningless — actively risky. A person who cannot tell MER-74 from MER-75 (both
# Lowe's, different items) cannot check whether the right one is being escalated, and cannot
# catch us when we are wrong. Every correction he has made this week came from recognising a
# THING: the chair, the sprayer, the shoes.
#
# So the identifier is never the label. `case_label()` renders "Relax The Back - Perfect Chair
# PC-610" from the record, and the id rides along in parentheses only where a lookup needs it.
# =========================================================================================
_TITLE_PREFIX_RE = re.compile(r"^\s*(?:case|client)\s*:\s*", re.I)
#: Trailing status/bookkeeping that is not part of the thing's name.
_TITLE_NOISE_RE = re.compile(
    r"\s*[-–—(]\s*(?:\$[\d,]+.*|tier\s*\d.*|intake\s+incomplete.*|shipped\b.*"
    r"|awaiting\b.*|cancelled\b.*|vendor\s+deflect.*|refund\s+sought.*|claim\s*#.*)$", re.I)


def case_label(issue_or_title, with_id=False):
    """A name a human recognises. 'MER-76' -> 'Relax The Back - Perfect Chair PC-610'.

    Never raises; falls back to the identifier only when there is genuinely no title to use."""
    ident, title = "", ""
    if isinstance(issue_or_title, dict):
        ident = issue_or_title.get("identifier") or ""
        title = issue_or_title.get("title") or ""
    else:
        title = str(issue_or_title or "")
    name = _TITLE_PREFIX_RE.sub("", title).strip()
    for _ in range(3):                              # strip stacked trailing clauses
        stripped = _TITLE_NOISE_RE.sub("", name).strip(" -–—")
        if stripped == name:
            break
        name = stripped
    name = re.sub(r"\s+", " ", name).strip(" -–—")
    if not name:
        return ident or "(unnamed case)"
    if len(name) > 58:
        name = name[:55].rstrip() + "..."
    return "%s (%s)" % (name, ident) if (with_id and ident) else name


def case_ref(issue_or_title):
    """The user's own preferred form: "MER-3 Paint Sprayer".

    Asked for by name on 2026-07-30 — *"maybe use MER-3 Paint Sprayer? use this style"* — after a
    bare identifier confused him twice. It is the right compromise and worth stating why: the
    identifier is the only thing that can be looked up on the board, and the short name is the only
    thing a human recognises a week later. Neither works alone, so they always travel together.

    The short name is the title's lead segment, before the em dash: titles are written
    "Paint Sprayer — Graco Ultra, bought at PPG White Rock", so the lead IS the plain-English name.
    Falls back to the full label, then to the bare identifier, so this never returns nothing."""
    ident, title = "", ""
    if isinstance(issue_or_title, dict):
        ident = issue_or_title.get("identifier") or ""
        title = issue_or_title.get("title") or ""
    else:
        title = str(issue_or_title or "")
    label = case_label({"identifier": ident, "title": title})
    short = re.split(r"\s+[—–-]\s+", label, maxsplit=1)[0].strip()
    if not short or short == ident:
        return ident or label
    return ("%s %s" % (ident, short)).strip() if ident else short


# Engine-build milestones (M1..M42) live in this project and are not cases.
BUILD_PROJECT = "13805886-7b4d-45bf-b985-32128f91b288"

# ------------------------------------------------------------------ where a case names its mail
# Description blocks scanned for addresses, most-authoritative first.
OVERRIDE_BLOCKS = ("MAIL FROM", "MAIL-FROM", "WATCH FROM")
STRUCTURED_BLOCKS = ("CONTACTS", "SENT", "VENDOR/ITEM")
SUBJECT_BLOCKS = ("MAIL SUBJECT", "MAIL-SUBJECT")

# Board properties consulted for the same thing (issue['mr'] as multica_api resolves it, by NAME
# so it stays portable across workspaces).
OVERRIDE_PROPS = ("MR Mail From", "MR Vendor Domains", "MR Vendor Domain", "Vendor Domains")
SUBJECT_PROPS = ("MR Mail Subject", "MR Subject Scope")

# ------------------------------------------------------------------ the over-broad guard
# A domain in here is a shared mailbox host: thousands of unrelated senders live behind it, so
# `from:<domain>` would be a net, not a scope. Matched EXACTLY — a subdomain such as
# `gohighlevelassist.freshdesk.com` is vendor-specific and is allowed through.
FREEMAIL_DOMAINS = frozenset("""
gmail.com googlemail.com yahoo.com yahoo.co.uk ymail.com rocketmail.com hotmail.com hotmail.co.uk
outlook.com live.com msn.com aol.com icloud.com me.com mac.com proton.me protonmail.com pm.me
gmx.com gmx.net mail.com zoho.com yandex.com tutanota.com hey.com fastmail.com fastmail.fm
comcast.net sbcglobal.net att.net verizon.net bellsouth.net cox.net earthlink.net juno.com
""".split())

SHARED_HELPDESK_DOMAINS = frozenset("""
freshdesk.com zendesk.com zdesk.com helpscout.net helpscout.com intercom.io intercom-mail.com
hubspot.com salesforce.com desk.com kayako.com groovehq.com front.com frontapp.com
sendgrid.net mailgun.org mailchimp.com sparkpostmail.com amazonses.com
""".split())

BROAD_DOMAINS = FREEMAIL_DOMAINS | SHARED_HELPDESK_DOMAINS

_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_DOMAINISH_RE = re.compile(r"^[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
_TOKEN_SPLIT_RE = re.compile(r"[\s,;|]+|\bOR\b", re.IGNORECASE)
MIN_TOKEN_LEN = 4          # a 1-3 char bare Gmail token would match half the mailbox


class CaseQuery(object):
    """One case's resolved watch scope. `query` is None exactly when `skipped` is True."""

    def __init__(self, identifier, title="", vendor="", query=None, domains=(), addresses=(),
                 tokens=(), subject="", source="", skipped=False, reason=""):
        self.identifier = identifier
        self.title = title
        self.vendor = vendor
        self.query = query
        self.domains = list(domains)
        self.addresses = list(addresses)
        self.tokens = list(tokens)
        self.subject = subject
        self.source = source
        self.skipped = bool(skipped)
        self.reason = reason

    @property
    def label(self):
        """Human display key, e.g. 'MER-1 (HighLevel Inc.)' — what inbox_watcher reports under."""
        return "%s (%s)" % (self.identifier, self.vendor) if self.vendor else self.identifier

    def as_dict(self):
        return {"identifier": self.identifier, "vendor": self.vendor, "query": self.query,
                "domains": self.domains, "addresses": self.addresses, "tokens": self.tokens,
                "subject": self.subject, "source": self.source,
                "skipped": self.skipped, "reason": self.reason}

    def __repr__(self):
        return "<CaseQuery %s %s>" % (self.identifier, "SKIPPED: " + self.reason
                                      if self.skipped else self.query)


# ------------------------------------------------------------------------------- identity

def _self_identity():
    """(address, domain) of the profile owner — never watched, it is our own sent copy."""
    try:
        addr = (mer_config.email() or "").strip().lower()
    except Exception:
        return "", ""
    return addr, addr.split("@", 1)[1] if "@" in addr else ""


# ------------------------------------------------------------------------------- extraction

def _prop(issue, names):
    """First non-empty value among `names` in the issue's name-resolved property map."""
    mr = issue.get("mr") or {}
    for n in names:
        v = mr.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _blocks_text(desc, labels):
    return " ".join(_block(desc, lab) for lab in labels).strip()


def _addresses_in(text):
    out, seen = [], set()
    for m in _ADDR_RE.finditer(text or ""):
        a = m.group(0).lower().rstrip(".")
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _split_override(text):
    """A MAIL FROM value -> (addresses, domains, bare tokens). Accepts commas, spaces or OR."""
    addrs, domains, tokens = [], [], []
    for raw in _TOKEN_SPLIT_RE.split(text or ""):
        t = raw.strip().strip("<>()[]{}'\"").strip(".,;:").lower()
        if not t:
            continue
        if t.startswith("from:"):
            t = t[5:]
        if "@" in t:
            m = _ADDR_RE.search(t)
            if m:
                addrs.append(m.group(0).lower())
        elif _DOMAINISH_RE.match(t):
            domains.append(t)
        elif len(t) >= MIN_TOKEN_LEN and t.isalnum():
            tokens.append(t)
    return addrs, domains, tokens


def _domain_of(addr):
    return addr.split("@", 1)[1] if "@" in addr else ""


def _dedupe(seq):
    out, seen = [], set()
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ------------------------------------------------------------------------------- the resolver

def resolve(issue):
    """Derive one case's Gmail query from its own board record. Never raises on bad data."""
    ident = (issue.get("identifier") or issue.get("id") or "?").strip()
    title = (issue.get("title") or "").strip()
    desc = issue.get("description") or ""
    try:
        vendor = parse_vendor(desc)
    except Exception:
        vendor = ""

    self_addr, self_dom = _self_identity()
    sources = []

    # 1) explicit override — the only place a bare token or a secondary domain can come from
    override_text = " ".join([_prop(issue, OVERRIDE_PROPS), _blocks_text(desc, OVERRIDE_BLOCKS)])
    o_addrs, o_domains, o_tokens = _split_override(override_text)
    if o_addrs or o_domains or o_tokens:
        sources.append("MAIL FROM override")

    # 2) addresses in the record's structured blocks (nudge's parser)
    block_text = _blocks_text(desc, STRUCTURED_BLOCKS)
    b_addrs = _addresses_in(block_text)
    if b_addrs:
        sources.append("CONTACTS/SENT/VENDOR block")

    # 3) last resort — the whole description (client-intake cases are prose, not blocks)
    s_addrs = []
    if not (o_addrs or o_domains or o_tokens or b_addrs):
        s_addrs = _addresses_in(desc)
        if s_addrs:
            sources.append("description scan")

    addrs = _dedupe(o_addrs + b_addrs + s_addrs)
    addrs = [a for a in addrs if a != self_addr]

    # split addresses into domain scopes vs address scopes. A shared-mailbox host is NEVER
    # widened to its domain — that is the `from:gmail.com` foot-gun this milestone must not ship.
    domains = list(o_domains)
    keep_addrs = []
    for a in addrs:
        d = _domain_of(a)
        if not d:
            continue
        if d in BROAD_DOMAINS:
            # NOTE: this branch must come BEFORE the self-domain test. The profile owner's own
            # mailbox is usually a free-mail one, and dropping every gmail.com sender because WE
            # are on gmail.com would blank out every client-intake case (it silently skipped
            # MER-4 and MER-21 in the first cut of M33). The exact self ADDRESS is already gone;
            # a different person on the same host is a legitimate counterparty.
            keep_addrs.append(a)        # precise: the person, not the whole host
        elif d != self_dom:
            domains.append(d)
    # NOTE: a subdomain is deliberately NOT collapsed into its parent (`assist.nike.com` is kept
    # alongside `nike.com`). Gmail's from:<domain> does match subdomains, so the term is
    # redundant — but a redundant OR term costs nothing, and silently deleting a scope the
    # operator wrote down is how a case stops seeing mail nobody notices it stopped seeing.
    domains = [d for d in _dedupe(domains) if d and d != self_dom and d not in BROAD_DOMAINS]
    keep_addrs = _dedupe(keep_addrs)
    tokens = _dedupe(o_tokens)

    subject = _prop(issue, SUBJECT_PROPS) or _blocks_text(desc, SUBJECT_BLOCKS)

    if not (domains or keep_addrs or tokens):
        return CaseQuery(
            ident, title, vendor, None, skipped=True,
            reason=("no vendor address or domain on the record — nothing safe to watch. "
                    "Fix on the board: add a `CONTACTS:` line with the vendor's email, or a "
                    "`MAIL FROM: vendor.com, other.vendor.com` block."))

    terms = (["from:%s" % d for d in domains]
             + ["from:%s" % a for a in keep_addrs]
             + ["from:%s" % t for t in tokens])
    from_scope = terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"
    query = from_scope + (" subject:(%s)" % _norm_subject(subject) if subject else "")

    return CaseQuery(ident, title, vendor, query, domains, keep_addrs, tokens,
                     _norm_subject(subject) if subject else "", " + ".join(sources))


def _norm_subject(raw):
    """'Textra, tablet, Galaxy' or 'Textra OR tablet' -> 'Textra OR tablet OR Galaxy'."""
    s = (raw or "").strip().strip("()")
    if not s:
        return ""
    if re.search(r"\bOR\b", s):
        return re.sub(r"\s+", " ", s)
    parts = [p.strip() for p in re.split(r"[,;|]+", s) if p.strip()]
    return " OR ".join(parts) if parts else ""


# ------------------------------------------------------------------------------- collection

CASE_TITLE_RE = re.compile(r"^\s*(case|client)\s*:", re.IGNORECASE)


def looks_like_case(issue):
    """Is this board record a merchandise-return CASE at all?

    A workspace holds more than cases — engine milestones, daily sweep notes, SYSTEM/KING chores.
    Those are not "cases that failed to resolve", they are not cases, and reporting them as skips
    every hour would bury the one skip that actually matters. A record counts as a case if it
    carries the MR property schema, is titled `Case:`/`CLIENT:`, or is written in the case record
    format (a VENDOR/ITEM or CONTACTS block, or an explicit MAIL FROM override).
    """
    if any(str(k).startswith("MR ") for k in (issue.get("mr") or {})):
        return True
    if CASE_TITLE_RE.match(issue.get("title") or ""):
        return True
    desc = issue.get("description") or ""
    if any(_block(desc, lab) for lab in ("VENDOR/ITEM", "CONTACTS") + OVERRIDE_BLOCKS):
        return True
    return bool(_prop(issue, OVERRIDE_PROPS))


def is_case_issue(issue):
    """Open, an actual case, and not an engine-build milestone."""
    if (issue.get("status") or "") in ("done", "cancelled"):
        return False
    if issue.get("project_id") == BUILD_PROJECT:
        return False
    return looks_like_case(issue)


def eligible_issues(issues):
    return [it for it in (issues or []) if is_case_issue(it)]


def resolve_all(issues):
    """-> (OrderedDict identifier -> query, [skipped CaseQuery, ...]) in board order."""
    try:
        from collections import OrderedDict
    except ImportError:                              # pragma: no cover
        OrderedDict = dict
    out, skipped = OrderedDict(), []
    for it in eligible_issues(issues):
        cq = resolve(it)
        if cq.skipped:
            skipped.append(cq)
        else:
            out[cq.identifier] = cq.query
    return out, skipped


def resolve_cases(issues=None):
    """-> [CaseQuery] for every eligible case, resolved and skipped alike. Fetches if needed."""
    if issues is None:
        import multica_api as mc
        issues = mc.list_issues()
    return [resolve(it) for it in eligible_issues(issues)]


def log_skips(skipped, out=None):
    """Print one visible line per un-watchable case. A silent skip is how a case goes dark."""
    printer = out or (lambda s: sys.stdout.write(s + "\n"))
    for cq in skipped:
        printer("  SKIP %s — %s" % (cq.identifier, cq.reason))
        if cq.title:
            printer("       (%s)" % cq.title[:90])


# =========================================================================================
# M47 — THE COVERAGE INVARIANT: every live case is watched, or somebody is told.
#
# `log_skips` prints. Printing is not telling. On 2026-07-28 MER-79 was skipped from the moment
# it was created and nobody learned of it for a day — the skip line went to a log that runs on a
# host nobody was reading, and two demand letters went out on a case with no watcher behind them.
#
# The difference between this and log_skips is the EXIT CODE. `audit()` is meant to be scheduled,
# so an unwatched case becomes a failing job — the one signal that reliably escalates — instead
# of a line in a file. Same data, consequences attached.
# =========================================================================================
def audit(issues=None):
    """-> (watched, unwatched) as lists of CaseQuery. Read-only, never raises on bad records."""
    if issues is None:
        import multica_api as mc
        issues = mc.list_issues()
    resolved = resolve_cases(issues)
    return ([q for q in resolved if not q.skipped],
            [q for q in resolved if q.skipped])


def audit_report(issues=None, out=None):
    """Human-readable coverage report. -> number of UNWATCHED cases (0 == healthy)."""
    printer = out or (lambda s: sys.stdout.write(s + "\n"))
    watched, unwatched = audit(issues)
    total = len(watched) + len(unwatched)
    printer("=" * 78)
    printer("MAIL COVERAGE AUDIT — %d of %d live case(s) are being watched" % (len(watched), total))
    printer("=" * 78)
    for q in watched:
        printer("  OK   %-8s %s" % (q.identifier, str(q.query)[:88]))
    if not unwatched:
        printer("")
        printer("  every live case has a watch scope.")
        return 0
    printer("")
    printer("  %d UNWATCHED CASE(S) — no reply to these will EVER be detected:" % len(unwatched))
    for q in unwatched:
        printer("    %-8s %s" % (q.identifier, (q.title or "")[:70]))
        printer("             why: %s" % q.reason)
        printer("             fix: add `MAIL FROM: vendor.com` to the issue description")
    return len(unwatched)


def case_queries(issues=None, log=True, out=None):
    """identifier -> Gmail query for every watchable case. Skips are logged, never silent."""
    if issues is None:
        import multica_api as mc
        issues = mc.list_issues()
    queries, skipped = resolve_all(issues)
    if log and skipped:
        log_skips(skipped, out)
    return queries


# ------------------------------------------------------------------------------- lazy view
class LazyCaseQueries(Mapping):
    """A read-only, board-derived stand-in for the deleted CASE_QUERIES literal.

    unmatched_review.py (M17) does `from mer_engine import CASE_QUERIES` at import time and needs
    a mapping of identifier -> query. Resolution must therefore be LAZY (an import must never hit
    the network) and must NEVER raise (unmatched_review's guard only wraps the import statement,
    so an exception raised later, inside .items(), would crash the reviewer). An unreachable board
    degrades to {} — same as the pre-existing failure mode.
    """

    def __init__(self, loader=None):
        self._loader = loader or (lambda: case_queries(log=False))
        self._cache = None

    def _data(self):
        if self._cache is None:
            try:
                self._cache = dict(self._loader())
            except Exception:
                self._cache = {}
        return self._cache

    def refresh(self):
        self._cache = None
        return self

    def __getitem__(self, k):
        return self._data()[k]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())

    def __repr__(self):
        return "<LazyCaseQueries %s>" % ("unresolved" if self._cache is None
                                         else "%d case(s)" % len(self._cache))


def live_case_queries():
    return LazyCaseQueries()


# ------------------------------------------------------------------------------- CLI / self-test

def _board_report():
    """Read-only: resolve against the live board and print. Never sends, never writes."""
    cases = resolve_cases()
    print("=== case_queries — LIVE BOARD (read-only) ===")
    for cq in cases:
        if cq.skipped:
            print("  %-8s SKIP — %s" % (cq.identifier, cq.reason))
        else:
            print("  %-8s %s" % (cq.identifier, cq.query))
            print("           vendor=%s  source=%s" % (cq.vendor or "(none)", cq.source))
    ok = [c for c in cases if not c.skipped]
    print("\n%d case(s) watchable, %d skipped." % (len(ok), len(cases) - len(ok)))


def _self_test():
    import json
    import tempfile

    results = []

    def check(desc, cond, detail=""):
        results.append(bool(cond))
        print("%s — %s%s" % ("PASS" if cond else "FAIL", desc,
                             ("  [%s]" % detail) if detail and not cond else ""))

    tmp = tempfile.mkdtemp(prefix="case_queries_test_")
    prof_path = os.path.join(tmp, "profile.json")
    with open(prof_path, "w", encoding="utf-8") as f:
        json.dump({"legal_name": "Jordan A. Rivera", "email": "jordan.rivera@example.com",
                   "state": "Texas", "county": "Travis",
                   "multica_workspace_id": "ws-test"}, f)
    mer_config.reset()
    mer_config.profile(prof_path)

    # ---- fabricated board records (shaped exactly like the live ones, no network) -------------
    ghl = {
        "identifier": "CASE-A", "status": "in_review",
        "title": "Case: HighLevel (GHL) #1000001 — $129.99 confirmed AT Stride",
        "description": (
            "VENDOR/ITEM: HighLevel Inc. (GoHighLevel) — SaaS subscription chargebacks.\n\n"
            "CONTACTS: Vendor legal desk, legal.escalations@gohighlevel.com (2 replies). "
            "Stride Bank (money is stuck here) customerservice@stridebank.com. TX AG "
            "consumer.protection@oag.texas.gov. BBB NCTX info@nctx.bbb.org.\n\n"
            "MAIL FROM: support@gohighlevelassist.freshdesk.com\n\n"
            "SENT: Stride email fired 2026-07-17 17:48 CT from jordan.rivera@example.com.\n"),
    }
    nike = {
        "identifier": "CASE-B", "status": "in_review", "title": "Case: Nike Reax 8 exchange",
        "description": (
            "VENDOR/ITEM: Nike Inc. — Nike Reax 8 TR Mesh. Defect: sole squeaking.\n\n"
            "CONTACTS: Tier 1: nike.com/help chat. Media (replied): media.relations@nike.com. "
            "DEAD: media.africa@nike.com (bounced).\n\n"
            "MAIL FROM: nikeclaims.com, assist.nike.com, rebound\n\n"
            "SENT: Tier 1 letter to media.relations@nike.com 2026-07-17.\n"),
    }
    ppg = {
        "identifier": "CASE-C", "status": "in_review", "title": "Case: PPG Paints / Graco sprayer",
        "description": (
            "VENDOR/ITEM: PPG Paints (White Rock store, DFW) — sold a Graco sprayer.\n\n"
            "CONTACTS: Tier 1: consumer.affairs@ppg.com (verified alive). Tier 2 (all verified "
            "alive): contact@ppgac.com, eaccountservice@ppg.com, "
            "techservicerequests@pittsburghpaints.com.\n\n"
            "SENT: 2026-07-17 11:54 CT — Tier 1 letter to consumer.affairs@ppg.com.\n"),
    }
    # NOTE: these two client fixtures deliberately sit on a real free-mail host (gmail.com).
    # The whole point of the pair is the over-broad guard — a free-mail sender must be watched
    # by FULL ADDRESS and never widened to `from:<host>` — and an RFC-2606 example.* domain is
    # not in FREEMAIL_DOMAINS, so it would not exercise that branch at all. The names and
    # local-parts are invented; only the host is real.
    person = {   # a client-intake case: prose, no structured blocks, a free-mail sender
        "identifier": "CASE-D", "status": "todo", "title": "CLIENT: Sam Okafor — Samsung tablet",
        "description": ("CLIENT CASE. CLIENT: Sam Okafor — sam.okafor.demo@gmail.com\n"
                        "STATUS: PHASE 1 — INTAKE INCOMPLETE.\n\n"
                        "MAIL SUBJECT: Textra, tablet, Galaxy, Samsung, defective, warranty\n"),
    }
    person_bare = {
        "identifier": "CASE-E", "status": "todo", "title": "CLIENT: Alex Moreau — Galaxy Z Fold",
        "description": "- Client: Alex Moreau, alex.moreau.demo@gmail.com, +1 555-0142\n",
    }
    by_prop = {
        "identifier": "CASE-F", "status": "todo", "title": "New user's first case",
        "description": "Bought a blender that arrived cracked.\n",
        "mr": {"MR Mail From": "acme-appliances.example, support@acme-appliances.example"},
    }
    empty = {"identifier": "CASE-G", "status": "todo", "title": "Case: no vendor recorded yet",
             "description": "I want my money back. Nothing else has been filled in yet.\n"}
    self_only = {"identifier": "CASE-H", "status": "todo", "title": "Case: only our own address",
                 "description": "CONTACTS: jordan.rivera@example.com (that is us)\n"}
    closed = {"identifier": "CASE-I", "status": "done", "title": "Case: closed",
              "description": "CONTACTS: someone@closedvendor.example\n"}
    build = {"identifier": "CASE-J", "status": "todo", "title": "M33 — board-derived discovery",
             "project_id": BUILD_PROJECT,
             "description": "CONTACTS: engineering@internal.example\n"}
    chore = {"identifier": "CASE-K", "status": "todo", "title": "SYSTEM: rotate the GitHub token",
             "description": "Ping ops@internal.example when done.\n"}
    sweep = {"identifier": "CASE-L", "status": "todo", "title": "Daily case sweep 2026-07-25",
             "description": "Walked the board. Nothing due.\n"}

    board = [ghl, nike, ppg, person, person_bare, by_prop, empty, self_only, closed, build,
             chore, sweep]

    # ---- 1. today's live behavior is preserved --------------------------------------------
    a = resolve(ghl)
    check("MER-1 shape: keeps from:stridebank.com", "from:stridebank.com" in a.query, a.query)
    check("MER-1 shape: keeps from:gohighlevel.com", "from:gohighlevel.com" in a.query, a.query)
    check("MER-1 shape: KNOWN MISS FIXED — secondary freshdesk domain is watched",
          "from:gohighlevelassist.freshdesk.com" in a.query, a.query)
    check("MER-1 shape: our own sent copy is not a watch target",
          "jordan.rivera@example.com" not in a.query, a.query)
    check("MER-1 shape: vendor parsed from the record", a.vendor == "HighLevel Inc.", a.vendor)

    b = resolve(nike)
    check("MER-2 shape: keeps from:nike.com", "from:nike.com" in b.query, b.query)
    check("MER-2 shape: keeps the bare `rebound` token", "from:rebound" in b.query, b.query)
    check("MER-2 shape: keeps from:nikeclaims.com", "from:nikeclaims.com" in b.query, b.query)
    check("MER-2 shape: keeps from:assist.nike.com (a scope is never silently dropped)",
          "from:assist.nike.com" in b.query, b.query)

    c = resolve(ppg)
    check("MER-3 shape: keeps from:ppg.com", "from:ppg.com" in c.query, c.query)
    check("MER-3 shape: keeps from:pittsburghpaints.com",
          "from:pittsburghpaints.com" in c.query, c.query)
    check("MER-3 shape: also picks up the Tier-2 domain the old table missed",
          "from:ppgac.com" in c.query, c.query)

    # ---- 2. the over-broad guard ----------------------------------------------------------
    d = resolve(person)
    check("free-mail sender: watched by full address",
          "from:sam.okafor.demo@gmail.com" in d.query, d.query)
    check("free-mail sender: NEVER widened to from:gmail.com",
          "from:gmail.com" not in d.query, d.query)
    check("free-mail sender: subject scope from the record is applied",
          d.query.endswith("subject:(Textra OR tablet OR Galaxy OR Samsung OR defective OR "
                           "warranty)"), d.query)
    e = resolve(person_bare)
    check("prose-only record: address found by the description-scan fallback",
          e.query == "from:alex.moreau.demo@gmail.com", e.query)
    check("prose-only record: source is reported as a fallback scan",
          "description scan" in e.source, e.source)
    allq = " ".join(cq.query or "" for cq in (resolve(x) for x in board))
    check("no broad host domain is ever emitted as a from: scope",
          not any(("from:%s" % dom) in allq for dom in BROAD_DOMAINS),
          allq)

    # REGRESSION: the operator's own mailbox is itself free-mail (the operator's is usually a free-mail one). Filtering
    # by self-DOMAIN there would blank out every client on the same host — it silently skipped
    # both live client cases in the first cut of this milestone.
    fm_path = os.path.join(tmp, "freemail.json")
    with open(fm_path, "w", encoding="utf-8") as f:
        json.dump({"legal_name": "Jordan A. Rivera", "email": "jordan.rivera@gmail.com",
                   "state": "Texas", "county": "Travis", "multica_workspace_id": "ws-test"}, f)
    mer_config.profile(fm_path)
    fm = resolve(person_bare)
    check("operator on a free-mail host does NOT blank out clients on the same host",
          fm.query == "from:alex.moreau.demo@gmail.com", str(fm))
    self_mail = resolve({"identifier": "CASE-S", "status": "todo", "title": "Case: self only",
                         "description": "CONTACTS: jordan.rivera@gmail.com\n"})
    check("our own free-mail address is still filtered out", self_mail.skipped, str(self_mail))
    mer_config.profile(prof_path)

    # ---- 3. degenerate cases are skipped, loudly ------------------------------------------
    g = resolve(empty)
    check("case with no vendor data is SKIPPED", g.skipped and g.query is None)
    check("skip reason names the board fix", "MAIL FROM" in g.reason and "CONTACTS" in g.reason,
          g.reason)
    h = resolve(self_only)
    check("case naming only our own address is SKIPPED", h.skipped and h.query is None)

    lines = []
    log_skips([g, h], out=lines.append)
    check("every skip prints a visible line naming the case",
          any("CASE-G" in x for x in lines) and any("CASE-H" in x for x in lines), str(lines))

    # ---- 4. a brand-new user's case works with zero code edits -----------------------------
    f = resolve(by_prop)
    check("new user: a board PROPERTY alone resolves the case",
          f.query == "from:acme-appliances.example", f.query)

    # ---- 5. collection semantics ----------------------------------------------------------
    queries, skipped = resolve_all(board)
    check("closed cases are not watched", "CASE-I" not in queries)
    check("engine-build milestones are not watched", "CASE-J" not in queries)
    check("non-case board records (chores, sweep notes) are not cases",
          not looks_like_case(chore) and not looks_like_case(sweep))
    check("non-case records are excluded SILENTLY, not reported as skipped cases",
          not any(s.identifier in ("CASE-J", "CASE-K", "CASE-L") for s in skipped),
          str([s.identifier for s in skipped]))
    check("every real case shape is recognised as a case",
          all(looks_like_case(x) for x in (ghl, nike, ppg, person, person_bare, by_prop)))
    check("all six real cases resolve", len(queries) == 6, str(sorted(queries)))
    check("both degenerate cases are reported as skipped",
          sorted(s.identifier for s in skipped) == ["CASE-G", "CASE-H"],
          str([s.identifier for s in skipped]))
    check("board order is preserved",
          list(queries) == ["CASE-A", "CASE-B", "CASE-C", "CASE-D", "CASE-E", "CASE-F"],
          str(list(queries)))
    check("case_queries() logs nothing when asked not to",
          case_queries(board, log=False) == queries)

    # ---- 6. the lazy back-compat view never raises and never hits the net on import --------
    lz = LazyCaseQueries(loader=lambda: {"CASE-A": "from:x.example"})
    check("lazy view is unresolved until touched", "unresolved" in repr(lz))
    check("lazy view resolves on access", dict(lz) == {"CASE-A": "from:x.example"})

    def _boom():
        raise RuntimeError("board unreachable")
    lz2 = LazyCaseQueries(loader=_boom)
    check("lazy view degrades to {} when the board is unreachable (unmatched_review stays alive)",
          dict(lz2) == {} and len(lz2) == 0 and list(lz2.items()) == [])

    # ---- 7. structural proof: the hardcoded tables are gone --------------------------------
    here = os.path.dirname(os.path.abspath(__file__))

    def _src(name):
        with open(os.path.join(here, name), encoding="utf-8") as fh:
            return fh.read()

    def _code_of(name):
        """Source with the module docstring stripped — docs may quote the deleted tables."""
        return re.sub(r'(?s)^#![^\n]*\n', "", _src(name)).split('"""', 2)[-1]

    check("this resolver hardcodes no case identifier",
          not re.search(r'"MER-\d+"\s*:', _code_of("case_queries.py")))
    for mod in ("mer_engine.py", "inbox_watcher.py"):
        try:
            body = _code_of(mod)
        except OSError as exc:
            check("%s readable" % mod, False, str(exc))
            continue
        check("%s: no hardcoded CASE_QUERIES table" % mod,
              not re.search(r"CASE_QUERIES\s*=\s*\{\s*[\"']MER", body))
        check("%s: no hardcoded vendor query literal" % mod,
              "from:stridebank.com" not in body and "from:ppg.com" not in body)
        check("%s: imports the shared resolver" % mod, "case_queries" in body)

    mer_config.reset()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    bad = results.count(False)
    if bad:
        print("SELF-TEST FAILED — %d of %d checks failed" % (bad, len(results)))
        sys.exit(1)
    # M52 — the identifier is a lookup key, never the name a human reads.
    check("case_label strips the Case:/CLIENT: prefix",
          case_label({"identifier": "MER-7", "title": "Case: Lowe's / Craftsman washer"})
          == "Lowe's / Craftsman washer")
    check("case_label drops trailing money and status noise",
          case_label({"identifier": "MER-76",
                      "title": "Case: Relax The Back / Perfect Chair PC-610 - $3,000, Tier 1 sent"})
          == "Relax The Back / Perfect Chair PC-610")
    check("case_label drops an (INTAKE INCOMPLETE) suffix",
          case_label({"identifier": "MER-75",
                      "title": "Case: Lowe's / Hisense Dehumidifier (INTAKE INCOMPLETE)"})
          == "Lowe's / Hisense Dehumidifier")
    check("case_label falls back to the id only when there is no title",
          case_label({"identifier": "MER-9", "title": ""}) == "MER-9")
    check("case_label can append the id when a lookup needs it",
          case_label({"identifier": "MER-1", "title": "Case: HighLevel / refund"}, with_id=True)
          == "HighLevel / refund (MER-1)")
    check("case_label never raises on junk", case_label(None) == "(unnamed case)")

    # M57 — the user's own naming style: "MER-3 Paint Sprayer".
    check("case_ref pairs the identifier with the plain-English name",
          case_ref({"identifier": "MER-3",
                    "title": "Paint Sprayer - Graco Ultra, bought at PPG White Rock"})
          == "MER-3 Paint Sprayer")
    check("case_ref handles an em dash as well as a hyphen",
          case_ref({"identifier": "MER-76",
                    "title": "Massage Chair — Human Touch PC-610, bought at Relax The Back"})
          == "MER-76 Massage Chair")
    check("case_ref falls back to the identifier when there is no title",
          case_ref({"identifier": "MER-9", "title": ""}) == "MER-9")
    check("case_ref never returns an empty string",
          case_ref({"identifier": "", "title": ""}) not in ("", None))

    print("PASS — case_queries self-test: %d/%d checks passed" % (len(results), len(results)))


if __name__ == "__main__":
    if "--audit" in sys.argv:
        # M47 — schedulable. EXIT 1 when any live case is unwatched, so the job fails loudly
        # instead of logging quietly. This is the whole point of it not being log_skips.
        sys.exit(1 if audit_report() else 0)
    elif "--board" in sys.argv:
        _board_report()
    else:
        _self_test()
