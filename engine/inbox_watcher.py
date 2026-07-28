#!/usr/bin/env python3
"""
inbox_watcher.py - report-only Gmail watcher for live Multica cases.

WHY THIS EXISTS
Multica has no email watcher. A case can wait on a vendor reply forever because
nothing reads the inbox - proven 2026-07-20, when Stride Bank replied and
declined to research the trace, and NOTHING surfaced it; MER-1 still said
"result due ~Jul 31" while the counterparty had actually stopped and asked a
question. This closes that gap.

HOW
Runs on the VPS (always-on, unlike a laptop). Reads the profile owner's mailbox
with the gmail.readonly scope ALREADY present on the profile's OAuth token file
(mer_config.google_token_file(); scope + live read both verified 2026-07-20 on
King's instance), reusing gmail_transport.access_token() - the same self-refreshing
credential the send path uses. Writes new-inbound findings to
/opt/data/multica_inbound.json, which king_nag.py folds into the daily nag.

REPORT-ONLY - HARD BOUNDARY
It never sends, replies, labels, archives, or touches a Multica issue. Reading
vendor mail is one step from replying to it; that is how Stride got emailed
twice (2026-07-18). This script has no send path, by design. Keep it that way.

DEDUP
Per-case set of seen Gmail message IDs in /opt/data/inbox_watcher_state.json.
The very first run establishes a silent baseline (records what is already there,
reports nothing) so it does not flood the nag with two weeks of back-traffic.
After that it reports only genuinely new inbound mail, once each.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# import the working send-path module purely for its self-refreshing access_token()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_transport as g  # noqa: E402
import heartbeat             # noqa: E402  (M46 — liveness ledger; writes a file, not the board)
import mer_config            # noqa: E402  (M32 — identity comes from the profile, not a literal)
import multica_api as mc     # noqa: E402  (M33 — the board is the case list)
import case_queries          # noqa: E402  (M33 — one shared query resolver, no local table)

def _data_dir():
    """Where this watcher's state lives. Config first, host default second, never a literal
    at a call site — the package ships to machines that have no /opt/data."""
    d = os.environ.get("MER_DATA_DIR")
    if d:
        return d
    if os.path.isdir("/opt/data"):
        return "/opt/data"
    base = os.environ.get("XDG_STATE_HOME") or os.environ.get("LOCALAPPDATA") \
        or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "merchandise-return")


STATE_FILE = os.environ.get("MER_INBOX_WATCHER_STATE") \
    or os.path.join(_data_dir(), "inbox_watcher_state.json")
OUT_FILE = os.environ.get("MER_INBOX_OUT") \
    or os.path.join(_data_dir(), "multica_inbound.json")
LOOKBACK = "newer_than:14d"     # bound the query so seen-sets stay small
MAX_RESULTS = 10                # per case, per run
MAX_REPORT = 15                 # cap total surfaced items so a chatty case can't flood

# M33 — CASE DISCOVERY IS NO LONGER HAND-MAINTAINED HERE.
# This file used to carry its own copy of the identifier->Gmail-query table, with a comment
# instructing whoever edited it to keep it in sync with mer_engine's copy. That is a drift bug
# with a countdown on it (the two copies had ALREADY diverged), and it meant a brand-new user's
# cases were invisible to this watcher. Both tables are deleted. The open cases and their queries
# now come from the board, through the single shared resolver in case_queries.py.
#
# The precision lessons the old table encoded are preserved IN THE RESOLVER, not in a literal:
#   * a free-mail address is watched as a full address, never widened to `from:gmail.com`;
#   * subject-scoping (which is what stopped Kim's trading mail — "4-Hour Futures Backtest
#     Results" — from leaking into her tablet case on 2026-07-20) is read from that case's own
#     `MAIL SUBJECT:` block on the board, so it is now per-case DATA that the case owner can
#     widen without a code change;
#   * a case with no derivable vendor address/domain is SKIPPED and logged, never given a broad
#     fallback query.
#
# STILL REPORT-ONLY — AND NOW STRUCTURALLY SO (M10).
#
# This file once reached `nudge` through `case_queries` (nudge in turn imports the send queue), so
# a read-only watcher had a live send function sitting in its own process, one attribute lookup
# away. "Nothing calls it" is not a boundary; it is a promise. That import was inverted out, and
# `--selftest` now WALKS THE TRANSITIVE IMPORT GRAPH of this module and fails if `nudge`,
# `send_queue` or `mer_send` reappears anywhere in it. If you add an import here and the self-test
# goes red, the fix is to move the function you wanted, not to widen the allowlist.
#
# `gmail_transport` IS imported, and is the one thing on this path that can put mail on the wire.
# It is here only for its self-refreshing access_token(); the watcher never calls send_mime(). That
# is the known, bounded exception — do not make it worse by adding a second one.


def load_case_queries():
    """{display label -> Gmail query} for every open case on the board, plus the skipped ones.

    Returns (queries, skipped). `queries` is keyed by identifier so the dedup state file stays
    stable across title/vendor edits; the human-readable label is carried separately.
    """
    from collections import OrderedDict
    queries, labels, skipped = OrderedDict(), {}, []
    for cq in case_queries.resolve_cases(mc.list_issues()):
        if cq.skipped:
            skipped.append(cq)
            continue
        queries[cq.identifier] = cq.query
        labels[cq.identifier] = cq.label
    return queries, labels, skipped


def _migrate_state_keys(seen, identifiers):
    """Adopt pre-M33 state keys ('MER-1 (GHL / Stride)') onto their bare identifier.

    Without this, the first run after the refactor would see an empty seen-set for every case and
    report two weeks of already-known mail as new — a Telegram flood and a false 'new reply' alarm.
    """
    for ident in identifiers:
        if ident in seen:
            continue
        for old in list(seen):
            if old == ident or old.startswith(ident + " ") or old.startswith(ident + "("):
                seen[ident] = seen.pop(old)
                break
    return seen


def _api(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


def list_messages(query, token):
    q = urllib.parse.quote("%s %s" % (query, LOOKBACK))
    url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages"
           "?q=%s&maxResults=%d" % (q, MAX_RESULTS))
    return _api(url, token).get("messages", []) or []


def headers_of(mid, token):
    url = ("https://gmail.googleapis.com/gmail/v1/users/me/messages/%s"
           "?format=metadata&metadataHeaders=Subject&metadataHeaders=Date"
           "&metadataHeaders=From" % mid)
    m = _api(url, token)
    return {x["name"]: x["value"] for x in m["payload"]["headers"]}


def _notify(items):
    """Ping King on Telegram the moment a vendor/client reply lands on a live case,
    so the 3-hourly run surfaces new mail within hours instead of at the next nag."""
    env = {}
    try:
        for line in open(os.path.join(_data_dir(), ".env")):
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return
    tok = env.get("TELEGRAM_BOT_TOKEN")
    # M32: the chat id is identity — the profile wins, .env is the fallback. The bot TOKEN
    # stays in .env and never goes in a profile.
    if mer_config.notify_channel() == "none":
        return
    uid = mer_config.notify_telegram_chat_id() or env.get("TELEGRAM_USER_ID")
    if not tok or not uid:
        return
    lines = ["\U0001f4e8 New mail on live cases:"]
    for m in items:
        lines.append("- %s: %s" % (m.get("case", "?"), (m.get("subject") or "(no subject)")))
    payload = json.dumps({"chat_id": uid, "text": "\n".join(lines)}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok, data=payload,
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception as exc:
        # M46: was `except Exception: pass`. An undelivered "new mail on a live case" ping is
        # exactly as bad as no watcher at all, and it used to leave no trace anywhere.
        print("inbox_watcher: telegram notify FAILED: %s" % exc)
        heartbeat.log_error("mer-inbox-watcher", "notify failed: %s" % exc)
        heartbeat.alert("⚠️ inbox_watcher could not deliver its new-mail notification: %s"
                        % exc, key="inbox-watcher-notify-fail", cooldown=6 * 3600)


def main(note=None):
    note = note or (lambda text: None)
    first_run = not os.path.exists(STATE_FILE)
    state = {}
    if not first_run:
        try:
            with open(STATE_FILE) as fh:
                state = json.load(fh)
        except Exception:
            state = {}
    seen = state.get("seen", {})  # case identifier -> [gmail message ids seen in-window]

    queries, labels, skipped = load_case_queries()
    seen = _migrate_state_keys(seen, list(queries))
    if skipped:
        # Visible, never silent: these cases are open on the board but cannot be watched.
        print("inbox_watcher: %d open case(s) NOT watchable from board data:" % len(skipped))
        case_queries.log_skips(skipped)
        # M46 — the COUNTER INVARIANT, asserted here too. `mer_engine` owns the 24h per-case
        # timer (it holds the state file that can measure age); this watcher owns the
        # every-run statement that the two counts must be equal. Two independent assertions
        # of the same invariant, from two jobs on different clocks, so one job being wedged
        # cannot make an unmonitored case invisible.
        note("%d of %d tracked case(s) are not being watched for vendor mail: %s"
             % (len(skipped), len(queries) + len(skipped),
                ", ".join(cq.identifier for cq in skipped)))

    token = g.access_token()
    new_items = []
    query_failures = []

    for ident, query in queries.items():
        case = labels.get(ident, ident)      # human label for the report/nag
        prior = set(seen.get(ident, []))
        current_ids = []
        try:
            msgs = list_messages(query, token)
        except Exception as exc:
            print("query FAILED for %s: %s" % (case, exc))
            query_failures.append("%s: %s" % (ident, exc))
            # keep whatever we knew before so we don't lose the baseline
            current_ids = list(prior)
            seen[ident] = current_ids
            continue
        for msg in msgs:
            mid = msg["id"]
            current_ids.append(mid)
            if mid in prior or first_run:
                continue
            try:
                h = headers_of(mid, token)
            except Exception:
                continue
            frm = h.get("From", "")
            if mer_config.email().lower() in frm.lower():
                continue  # our own sent copy in the thread - not an inbound reply
            new_items.append({
                "case": case,
                "from": frm,
                "date": h.get("Date"),
                "subject": (h.get("Subject") or "")[:140],
                "gmail_id": mid,
            })
        seen[ident] = current_ids

    truncated = max(0, len(new_items) - MAX_REPORT)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "first_run_baseline": first_run,
        "new_mail": new_items[:MAX_REPORT],
        "truncated": truncated,
    }
    for _d in {os.path.dirname(OUT_FILE), os.path.dirname(STATE_FILE)}:
        if _d:
            os.makedirs(_d, exist_ok=True)   # a fresh non-VPS host has no state dir yet
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    state["seen"] = seen
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)

    if not first_run and new_items:
        _notify(new_items[:MAX_REPORT])
    if first_run:
        print("inbox_watcher: first run - baseline recorded, nothing reported.")
    else:
        print("inbox_watcher: %d new inbound message(s) across %d cases -> %s"
              % (len(new_items), len(queries), OUT_FILE))

    # M46 — a run that could not read the mailbox is NOT a quiet week. This is the exact lie
    # the engine has told four times: every query fails, nothing is reported, and the output
    # is indistinguishable from "no vendor replied".
    if query_failures:
        allfail = len(query_failures) == len(queries) and queries
        head = ("EVERY case query failed (%d/%d)" % (len(query_failures), len(queries))
                if allfail else
                "%d of %d case queries failed" % (len(query_failures), len(queries)))
        print("inbox_watcher: !! " + head)
        note(head)
        heartbeat.alert("\U0001f6a8 inbox_watcher: %s — no vendor reply is being detected%s\n\n%s"
                        % (head,
                           ". This is the signature of an expired Gmail OAuth token."
                           if allfail else "",
                           "\n".join(query_failures[:8])),
                        key="inbox-watcher-query-fail:%s" % ("all" if allfail else "partial"),
                        cooldown=3 * 3600)
    return 0


# ------------------------------------------------------------------------------- selftest

_BANNED_IMPORTS = ("nudge", "send_queue", "mer_send")


def _local_modules():
    """Module name -> path, for the engine modules that sit beside this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    return {f[:-3]: os.path.join(here, f) for f in os.listdir(here)
            if f.endswith(".py") and not f.startswith("_")}


def import_graph(root, mods=None):
    """Every engine module reachable from `root` by import, transitively.

    Deliberately static (AST), not dynamic: a runtime check would only see what happened to be
    imported on the path the test took, and the point is to constrain what CAN be reached.
    """
    import ast
    mods = mods or _local_modules()
    seen, stack = set(), [root]
    while stack:
        name = stack.pop()
        if name in seen or name not in mods:
            continue
        seen.add(name)
        try:
            tree = ast.parse(open(mods[name], encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    stack.append(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                stack.append(node.module.split(".")[0])
    seen.discard(root)
    return seen


def _selftest():
    """Offline. No network, no mailbox, no board, and — provably — no send path."""
    fails = []

    def ck(name, cond, detail=""):
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               "" if cond else "  <- " + str(detail)))
        if not cond:
            fails.append(name)

    print("inbox_watcher --selftest  (offline)")
    print("-" * 70)

    print("A. dependency inversion — a READ-ONLY watcher may not reach a send path")
    graph = import_graph("inbox_watcher")
    for banned in _BANNED_IMPORTS:
        ck("cannot reach %s (transitively)" % banned, banned not in graph,
           "reachable via: %s" % sorted(graph))
    # CONTROL: a green result above must mean "the watcher is clean", not "the walker is blind".
    # mer_engine legitimately owns the send path, so the same walker MUST find it there.
    ck("the walker is not vacuously green (it finds send_queue in mer_engine)",
       "send_queue" in import_graph("mer_engine"), sorted(import_graph("mer_engine")))
    ck("import graph is small and known",
       graph <= {"gmail_transport", "mer_config", "multica_api", "case_queries",
                 "businessday", "llm_providers", "heartbeat", "king_nag"}, sorted(graph))
    # heartbeat/king_nag are new (M46) and are the reason this allowlist grew. They may write
    # a local ledger and push a Telegram message; they may NOT reach a mail-send or a board
    # write, which is what the banned-import check above already proves for the whole graph.
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    body = src.split("def _selftest")[0]
    # Comments discuss the boundary; only code can cross it.
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    for verb in ("send_mime(", "add_comment(", "set_property(", "update_issue("):
        ck("never calls %s" % verb, verb not in code)

    print("B. state-key migration (a rename must not replay two weeks of mail)")
    seen = {"MER-1 (GHL / Stride)": ["a", "b"], "MER-3": ["c"]}
    out = _migrate_state_keys(dict(seen), ["MER-1", "MER-3", "MER-9"])
    ck("legacy label key adopted onto the bare identifier", out.get("MER-1") == ["a", "b"], out)
    ck("already-bare key untouched", out.get("MER-3") == ["c"], out)
    ck("an unknown identifier gets no invented history", "MER-9" not in out, out)

    print("C. portability — no host path, no identity baked in")
    import ast
    import re as _re
    tree = ast.parse(src)
    exempt = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name in ("_data_dir", "_selftest")]
    strays = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and n.value.startswith("/opt/")
              and not any(a <= n.lineno <= b for a, b in exempt)]
    ck("no host path outside _data_dir()", not strays, strays)
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    ck("no email address literal in code",
       not _re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", code))
    ck("state paths are env-overridable",
       "MER_INBOX_WATCHER_STATE" in body and "MER_INBOX_OUT" in body)

    print("D. config resolution works with nothing configured")
    import tempfile
    prev = os.environ.get("MER_DATA_DIR")
    os.environ["MER_DATA_DIR"] = tempfile.mkdtemp(prefix="inbox_watcher_selftest_")
    ck("_data_dir honours MER_DATA_DIR", _data_dir() == os.environ["MER_DATA_DIR"])
    os.environ.pop("MER_DATA_DIR")
    ck("_data_dir falls back to a real writable location without config",
       isinstance(_data_dir(), str) and _data_dir() != "")
    if prev is not None:
        os.environ["MER_DATA_DIR"] = prev

    print("-" * 70)
    if fails:
        print("SELF-TEST FAILED: %s" % ", ".join(fails))
        return 1
    print("PASS — inbox_watcher self-test green (offline; report-only proven structurally).")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    # M46 — every run writes {name, ts, ok, err}. A watcher that stops running, or runs and
    # cannot read the mailbox, is now a detectable state instead of a silent one.
    sys.exit(heartbeat.guard("mer-inbox-watcher", main, expect_seconds=21600.0))
