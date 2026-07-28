#!/usr/bin/env python3
"""
unmatched_review.py — the unmatched-mail weekly review (Blueprint M17).

WHY THIS EXISTS
The Merchandise Returns Engine matches inbound to a case by a hand-maintained
per-case Gmail query (mer_engine.CASE_QUERIES) — almost always `from:<vendor>`.
That is precise but narrow: a genuinely case-relevant email can slip through when
it arrives from a sender NOT in any case query. Two real, observed slips:

  * A vendor answers from a different domain than the one in the case query —
    e.g. HighLevel replies from `support@gohighlevelassist.freshdesk.com`, which
    the MER-1 query `from:gohighlevel.com` never sees.
  * A client emails about a NEW/different dispute from the same address as an
    existing case — e.g. a client's credit-card denial arrives from her Gmail,
    which is scoped to her Samsung-phone case, not a card-issuer matter.

This closes that last gap. Once a week it scans recent inbound that LOOKS like a
return/refund/dispute (keyword net) but did NOT match any live case's query, drops
obvious marketing / newsletters / our own sent digests, and surfaces the rest as
"possible missed case mail — review."

READ-ONLY — HARD BOUNDARY (same rule as inbox_watcher.py)
It never sends, replies, labels, archives, opens a case, or touches a Multica
issue. It only reads Gmail (the gmail.readonly scope already on the profile
owner's own token) and returns/prints findings. Reading vendor mail is one step
from replying to it; this script has no send path, by design. Keep it that way.

THE "MATCHED SET" AND WHY PERSON-ONLY QUERIES DO NOT SUPPRESS
The matched set is the union of message IDs returned by each live case query in
mer_engine.CASE_QUERIES — but only the *reliable* ones suppress a candidate:

  * vendor/domain-scoped (`from:stridebank.com`) or subject-scoped
    (`... subject:(Textra OR tablet ...)`) queries own their case's mail cleanly,
    so anything they match is excluded.
  * a bare person-address query (`from:someone@gmail.com`, no subject: scope) is
    KNOWN to over-match: a single person legitimately has mail about more than one
    matter (the 2026-07-20 person-query over-match, documented in inbox_watcher.py). So a
    person-only query does NOT suppress the reviewer — return-related mail from
    that person still surfaces for a human glance. This is precisely the class of
    slip M17 exists to catch.

Public API:
    review(window="newer_than:8d", token=None)
        -> [ {from, subject, date, why_flagged}, ... ]  (most recent first)

CLI:
    unmatched_review.py            human-readable summary
    unmatched_review.py --json     machine-readable JSON
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# reuse the self-refreshing read token the rest of the engine uses
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")


def _install_selftest_profile():
    """Plant a throwaway profile so --selftest is genuinely OFFLINE and identity-free.

    SELF = mer_config.email() resolves at IMPORT time, so `--selftest` died with "no profile
    configured" (exit 1) on any machine without one. It only ever passed in the canonical
    scripts directory because that directory happens to contain a profile.json — i.e. the
    self-test was silently borrowing the operator's identity, and the byte-identical copy in
    the shipped skill package failed. A self-test that needs a configured identity is not a
    self-test.

    Same pattern as fresh_profile_check.py: write a fictional profile to a temp dir and point
    $MER_PROFILE at it. Done BEFORE the mer_config import below, because that is when the
    module-level identity constants are resolved. Unconditional during --selftest, so the
    result is identical in canonical and in the package.
    """
    import json as _json
    import tempfile as _tempfile
    d = _tempfile.mkdtemp(prefix="mer_unmatched_selftest_")
    path = os.path.join(d, "profile.json")
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump({
            "legal_name": "Priya N. Raman",
            "email": "priya.raman@example.net",
            "state": "Rhode Island",
            "county": "Providence",
            "multica_workspace_id": "ffffffff-0000-4444-8888-cccccccccccc",
            "notify": {"channel": "none"},
            "google_token_file": os.path.join(d, "selftest_google_token.json"),
        }, fh)
    os.environ["MER_PROFILE"] = path


if "--selftest" in sys.argv:
    _install_selftest_profile()

import gmail_transport as _gt  # noqa: E402
import mer_config              # noqa: E402  (M32 — identity from the profile, not a literal)

# single source of truth for the live case queries (the matched set)
try:
    from mer_engine import CASE_QUERIES  # noqa: E402
except Exception:  # pragma: no cover — mer_engine should always be present
    CASE_QUERIES = {}

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SELF = mer_config.email()      # M32: the profile owner's own mailbox — never a literal
DEFAULT_WINDOW = os.environ.get("UNMATCHED_REVIEW_WINDOW", "newer_than:8d")
MAX_CANDIDATES = int(os.environ.get("UNMATCHED_REVIEW_MAX", "100"))

# the "looks like a return/refund/dispute" net
KEYWORDS = ["refund", "return", "warranty", "defective", "denied",
            "chargeback", "rma", "claim"]
_KW_RE = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)
_KW_QUERY = "(" + " OR ".join(KEYWORDS) + ")"

# self-sent digests we never want to surface (belt-and-suspenders on top of the
# self-address filter): the Buy&SellNow sweep digests and their kin.
_DIGEST_SUBJECT_RE = re.compile(r"buy\s*&?\s*sell|sweep\s+digest|reply\s+monitor",
                                re.IGNORECASE)

_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_FROM_TARGET_RE = re.compile(r"from:([^\s()]+)", re.IGNORECASE)


def _api(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


def _search(query, token, cap=MAX_CANDIDATES):
    """Return a list of message IDs for a Gmail search query."""
    q = urllib.parse.quote(query)
    url = "%s/messages?q=%s&maxResults=%d" % (GMAIL, q, cap)
    try:
        return [m["id"] for m in (_api(url, token).get("messages") or [])]
    except Exception:
        return []


def _is_suppressing_query(query):
    """True if a case query reliably owns its case's mail (so a match should be
    excluded); False for bare person-address queries that over-match.

    Reliable  = has a subject: scope, OR any from: target is a bare domain.
    Unreliable = every from: target is a personal address and there is no subject:.
    """
    ql = (query or "").lower()
    if "subject:" in ql:
        return True
    targets = _FROM_TARGET_RE.findall(ql)
    if not targets:
        # not a from:-based query at all — treat as reliable (keyword/label scoped)
        return True
    # a bare domain target (no '@') is a vendor scope and is reliable
    if any("@" not in t for t in targets):
        return True
    return False


def _matched_ids(token, window):
    """Union of message IDs claimed by the *suppressing* live case queries."""
    matched = set()
    for ident, query in CASE_QUERIES.items():
        if not _is_suppressing_query(query):
            continue
        matched.update(_search("%s %s" % (query, window), token))
    return matched


def _meta(mid, token):
    """Fetch the headers + snippet + labels needed to judge one candidate."""
    url = ("%s/messages/%s?format=metadata"
           "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date"
           "&metadataHeaders=List-Unsubscribe&metadataHeaders=Precedence" % (GMAIL, mid))
    m = _api(url, token)
    hdr = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    return {
        "from": hdr.get("from", ""),
        "subject": (hdr.get("subject") or "").strip(),
        "date": hdr.get("date", ""),
        "list_unsub": hdr.get("list-unsubscribe"),
        "precedence": (hdr.get("precedence") or "").lower(),
        "labels": m.get("labelIds", []) or [],
        "snippet": m.get("snippet", "") or "",
        "internal": int(m.get("internalDate", "0") or "0"),
    }


def _addr(frm):
    m = _ADDR_RE.search(frm or "")
    return m.group(0).lower() if m else ""


def _is_marketing(meta):
    """Newsletter / bulk / promotional? Genuine dispute mail is 1:1 and carries
    none of these signals; marketing reliably does."""
    if meta["list_unsub"]:
        return True
    if meta["precedence"] in ("bulk", "list", "junk"):
        return True
    labels = meta["labels"]
    if "CATEGORY_PROMOTIONS" in labels or "CATEGORY_SOCIAL" in labels:
        return True
    return False


def _why(meta):
    hits = []
    seen = set()
    for m in _KW_RE.finditer(meta["subject"] + " \n " + meta["snippet"]):
        w = m.group(0).lower()
        if w not in seen:
            seen.add(w)
            hits.append(w)
    if hits:
        return "return/refund keywords: " + ", ".join(hits)
    # the Gmail net matched somewhere in the body even if not in subject/snippet
    return "return/refund keyword matched in message body"


def review(window=DEFAULT_WINDOW, token=None):
    """Scan recent inbound that looks return-related but matched no live case
    query. Returns a list of {from, subject, date, why_flagged}, newest first.
    Read-only; never opens a case or sends."""
    if token is None:
        token = _gt.access_token()

    matched = _matched_ids(token, window)

    # candidate net: return-ish keywords, inbound only, obvious promo/social pruned
    cand_query = "%s %s in:inbox -category:promotions -category:social" % (_KW_QUERY, window)
    candidates = _search(cand_query, token)

    rows = []
    for mid in candidates:
        if mid in matched:
            continue
        try:
            meta = _meta(mid, token)
        except Exception:
            continue
        addr = _addr(meta["from"])
        if addr == SELF:
            continue  # our own sent copy / self-mailed report
        if _DIGEST_SUBJECT_RE.search(meta["subject"]):
            continue  # Buy&SellNow sweep digest & kin
        if _is_marketing(meta):
            continue
        rows.append({
            "from": meta["from"].strip(),
            "addr": addr,
            "subject": meta["subject"] or "(no subject)",
            "date": meta["date"],
            "internal": meta["internal"],
            "why_flagged": _why(meta),
        })

    # collapse near-identical mail from one sender (e.g. freshdesk auto-acks) into
    # one row so a chatty ticket system can't drown out the signal.
    groups = {}
    order = []
    for r in rows:
        key = (r["addr"], r["subject"][:60].lower())
        if key not in groups:
            groups[key] = dict(r)
            groups[key]["_count"] = 1
            order.append(key)
        else:
            g = groups[key]
            g["_count"] += 1
            if r["internal"] > g["internal"]:  # keep the most recent as representative
                g["internal"] = r["internal"]
                g["date"] = r["date"]

    out = []
    for key in order:
        g = groups[key]
        why = g["why_flagged"]
        if g["_count"] > 1:
            why += " (+%d more near-identical from this sender)" % (g["_count"] - 1)
        out.append({
            "from": g["from"],
            "subject": g["subject"],
            "date": g["date"],
            "why_flagged": why,
        })
    out.sort(key=lambda x: groups[(_addr(x["from"]), x["subject"][:60].lower())]["internal"],
             reverse=True)
    return out


# --------------------------------------------------------------------------------------
# M42 — scheduled sweep rendering
# --------------------------------------------------------------------------------------
def render_alert(items, window=DEFAULT_WINDOW):
    """Alert-only render for the cron. Returns "" when nothing slipped through.

    An empty inbox-against-case-queries is the healthy case and MUST render empty, so the
    wrapper prints nothing and the cron delivers nothing. Deliberately imperative-free
    (workspace rule #1): it reports what was seen and asks for nothing.
    """
    if not items:
        return ""
    lines = ["📬 Unmatched-mail review (M17) — RECORD ONLY, NO ACTION REQUIRED",
             "%d inbound message(s) in %s look return-related but matched no live case query:"
             % (len(items), window)]
    for i, it in enumerate(items, 1):
        lines.append("  %d. %s" % (i, it["from"]))
        lines.append("     subj: %s" % it["subject"])
        lines.append("     why : %s" % it["why_flagged"])
    lines.append("(read-only — nothing was opened, replied to, or labelled.)")
    lines.append("(full sweep: /opt/data/scripts/unmatched_review.log)")
    return "\n".join(lines)


def render_report(items, window=DEFAULT_WINDOW):
    """Full detail for the log — always rendered, healthy or not."""
    head = ("=== unmatched-mail review (M17) %s window=%s ==="
            % (datetime.now(timezone.utc).isoformat(), window))
    if not items:
        return head + "\nNo possible-missed case mail. Inbox clean against the live case queries."
    out = [head, "%d possible missed case email(s):" % len(items)]
    for i, it in enumerate(items, 1):
        out.append("%2d. from : %s" % (i, it["from"]))
        out.append("    subj : %s" % it["subject"])
        out.append("    date : %s" % it["date"])
        out.append("    why  : %s" % it["why_flagged"])
    return "\n".join(out)


def cron_main(window=DEFAULT_WINDOW):
    """M42 cron entry point. stdout = alert only (empty when healthy); stderr = full report.

    Exit 0 on success; 2 if the scan could NOT run — a sweep that failed must not be
    mistaken for a clean inbox, so it speaks up instead of staying silent.
    """
    try:
        items = review(window=window)
    except Exception as exc:
        sys.stderr.write("unmatched_review failed: %s: %s\n" % (type(exc).__name__, exc))
        print("⚠️ Unmatched-mail review could not run: %s: %s" % (type(exc).__name__, exc))
        return 2
    sys.stderr.write(render_report(items, window) + "\n")
    alert = render_alert(items, window)
    if alert:
        print(alert)
    return 0


# --------------------------------------------------------------------------------------
# Offline self-test (no network) — the pure classification + rendering logic
# --------------------------------------------------------------------------------------
def _selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        if not cond:
            ok = False
        print("[%s] %s" % ("PASS" if cond else "FAIL", name))

    # Which case queries reliably own their mail (and so suppress a candidate)?
    check("vendor-domain query suppresses", _is_suppressing_query("from:stridebank.com"))
    check("subject-scoped person query suppresses",
          _is_suppressing_query("from:sam.okafor.demo@gmail.com subject:(tablet OR Textra)"))
    check("bare person query does NOT suppress (the person-query slip)",
          not _is_suppressing_query("from:alex.moreau.demo@gmail.com"))
    check("keyword-only query treated as reliable", _is_suppressing_query("label:mer"))

    # Marketing/bulk must be pruned; genuine 1:1 dispute mail must not be.
    promo = {"list_unsub": "<https://x/unsub>", "precedence": "", "labels": []}
    bulk = {"list_unsub": None, "precedence": "bulk", "labels": []}
    real = {"list_unsub": None, "precedence": "", "labels": ["INBOX"]}
    check("List-Unsubscribe pruned", _is_marketing(promo))
    check("Precedence: bulk pruned", _is_marketing(bulk))
    check("1:1 dispute mail kept", not _is_marketing(real))

    # Why-flagged reports the keywords that actually hit.
    why = _why({"subject": "Your refund request", "snippet": "the warranty claim was denied"})
    check("keywords reported (%s)" % why,
          all(w in why for w in ("refund", "warranty", "denied")))

    # M42: silence when healthy, speech when something slipped.
    check("empty review renders an EMPTY alert (cron stays silent)",
          render_alert([]) == "")
    items = [{"from": "support@gohighlevelassist.freshdesk.com",
              "subject": "Re: refund ticket #1000001", "date": "Thu, 24 Jul 2026 10:00:00 +0000",
              "why_flagged": "return/refund keywords: refund"}]
    alert = render_alert(items)
    check("a slipped message produces an alert", bool(alert))
    check("alert names the sender", "freshdesk.com" in alert)
    check("alert stays record-only (workspace rule #1)", "NO ACTION REQUIRED" in alert)
    check("full report always describes the healthy case too",
          "Inbox clean" in render_report([]))

    print("PASS — unmatched_review self-test" if ok else "SELF-TEST FAILED")
    return 0 if ok else 1


def main():
    as_json = "--json" in sys.argv
    win = DEFAULT_WINDOW
    for a in sys.argv[1:]:
        if a.startswith("newer_than:") or a.startswith("older_than:"):
            win = a
    items = review(window=win)
    if as_json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window": win,
            "count": len(items),
            "items": items,
        }, indent=2, ensure_ascii=False))
        return
    print("=== unmatched-mail weekly review (M17)  %s  window=%s ==="
          % (datetime.now(timezone.utc).isoformat(), win))
    if not items:
        print("No possible-missed case mail. Inbox clean against the live case queries.")
        return
    print("%d possible missed case email(s) — REVIEW (read-only, nothing opened/sent):\n"
          % len(items))
    for i, it in enumerate(items, 1):
        print("%2d. from : %s" % (i, it["from"]))
        print("    subj : %s" % it["subject"])
        print("    date : %s" % it["date"])
        print("    why  : %s\n" % it["why_flagged"])


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--cron" in sys.argv:
        _win = next((a for a in sys.argv[1:] if a.startswith(("newer_than:", "older_than:"))),
                    DEFAULT_WINDOW)
        sys.exit(cron_main(_win))
    main()
