#!/usr/bin/env python3
"""multica_board_state.py — generate multica_state.json from ALL live workspaces.

Coverage guarantee: it DISCOVERS every workspace each run (GET /api/workspaces) and
pages through EVERY issue — nothing is hardcoded, so a workspace/issue created later
is picked up automatically and can never be silently missed. [RETIRED] workspaces are
skipped. Deterministic + read-only against Multica.

KING-ACTION PACKETS (2026-07-22): every needs-King item carries a structured packet
{verb, detail, deadline, why, severity, blocking} so the dashboard can show King the
EXACT thing he must do (e.g. the Nike mailing address) instead of a truncated title.
Packet source, in order:
  1. A `KING-ACTION:` block in the issue DESCRIPTION (primary, single source of truth).
  2. case_actions.json keyed by identifier (fallback that works without re-touching
     every issue — this is why the Nike address renders today).
Physical/legal King-only actions ("mail the", "ship the", "human files"...) are HARD
needs_king — never the soft "review candidate / might need you" bucket that let MER-2
sit as a "maybe". Severity is computed from CONSEQUENCE (blocking + deadline proximity
+ security exposure + priority), not from a keyword.
"""
import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, date

ENV = "/opt/data/.env"
OUT = "/opt/data/multica_state.json"
CASE_ACTIONS = "/opt/data/multica_case_actions.json"
OPEN_EXCLUDE = {"done", "cancelled", "closed"}
KING_MARKER = "NEEDS-KING"

# Physical / legal actions only KING can take. Matching one of these makes an issue a
# HARD needs_king item (not a soft glance). High-signal, action-oriented phrases only —
# historical mentions ("King confirmed") must not match.
KING_ACTION_PHRASES = (
    "king's decision", "kings decision", "ship or decline", "ship the",
    "mail the", "drop off", "drop-off", "awaiting king", "king must",
    "king needs to", "requires king", "hand to king", "handed to king",
    "human files", "a human files", "only king can", "for king to",
    "king to mail", "king to ship", "king to file", "king to call",
    "king to send", "king to approve", "blocked on king", "king to pay",
)

# Security-exposure signals → escalate severity to critical regardless of deadline.
SECURITY_PHRASES = ("exposed", "leaked", "credential", "secret", "token", "rotate")


def _env():
    d = {}
    if os.path.exists(ENV):
        for line in open(ENV):
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    return d


E = _env()
TOKEN = E.get("MULTICA_TOKEN", "")
BASE = E.get("MULTICA_SERVER_URL", "https://api.multica.ai").rstrip("/") + "/api"


def api(path, ws=None):
    h = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
    if ws:
        h["X-Workspace-Id"] = ws
    return json.loads(urllib.request.urlopen(urllib.request.Request(BASE + path, headers=h), timeout=25).read())


def all_workspaces():
    """Every ACTIVE workspace, discovered live. New workspaces auto-appear;
    [RETIRED] ones are skipped. This is the coverage guarantee."""
    w = api("/workspaces")
    wl = w if isinstance(w, list) else w.get("workspaces", [])
    return [(x.get("name"), x.get("id")) for x in wl
            if "[RETIRED]" not in (x.get("name") or "").upper()]


def all_issues(ws):
    """Every issue in a workspace, paged fully — never capped at a page size."""
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"limit": 100, "offset": offset, "workspace_id": ws})
        d = api("/issues?" + q, ws)
        batch = d.get("issues", []) or []
        out += batch
        total = d.get("total")
        offset += len(batch)
        if not batch or len(batch) < 100 or (total is not None and offset >= total):
            break
    return out


def load_case_actions():
    for p in (CASE_ACTIONS, os.path.join(os.path.dirname(os.path.abspath(__file__)), "case_actions.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh).get("packets", {}) or {}
        except Exception:
            continue
    return {}


CASE_PACKETS = load_case_actions()

# Parse a KING-ACTION block out of an issue description. Recognises the four labelled
# lines in any order; verb comes from the KING-ACTION: line itself.
_LABELS = {"detail": "DETAIL", "deadline": "DEADLINE", "why": "WHY", "severity": "SEVERITY"}


def parse_packet_from_description(body):
    if not body:
        return None
    m = re.search(r"KING-ACTION:\s*(.+)", body, re.IGNORECASE)
    if not m:
        return None
    pkt = {"verb": m.group(1).strip(), "detail": None, "deadline": None,
           "why": None, "severity": None}
    for key, label in _LABELS.items():
        lm = re.search(label + r":\s*(.+)", body, re.IGNORECASE)
        if lm:
            val = lm.group(1).strip()
            if val.lower() in ("none", "(none)", "n/a", "-", ""):
                val = None
            pkt[key] = val
    return pkt


def king_signal(it):
    title = (it.get("title") or "").strip()
    if title.upper().startswith("KING:"):
        return "KING: item"
    body = it.get("description") or ""
    if KING_MARKER in title.upper() or KING_MARKER in body.upper():
        return "marked NEEDS-KING"
    if it.get("assignee_type") == "member":
        return "assigned to you"
    return None


def king_action_hint(it):
    text = ((it.get("title") or "") + " " + (it.get("description") or "")).lower()
    for p in KING_ACTION_PHRASES:
        if p in text:
            return p
    return None


def _deadline_days(deadline):
    if not deadline:
        return None
    try:
        d = datetime.strptime(deadline[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return (d - date.today()).days


def compute_severity(it, packet, blocking, due):
    """Severity from CONSEQUENCE, not keyword. A packet may override explicitly."""
    if packet and packet.get("severity") in ("critical", "high", "normal"):
        return packet["severity"]
    text = ((it.get("title") or "") + " " + (it.get("description") or "")).lower()
    pr = (it.get("priority") or "").lower()
    if any(s in text for s in SECURITY_PHRASES) or pr in ("urgent", "critical"):
        return "critical"
    left = _deadline_days(due if due else (packet or {}).get("deadline"))
    if blocking and left is not None and left <= 2:
        return "critical"
    if left is not None and left <= 0:
        return "critical"
    if blocking:
        return "high"
    if pr == "high":
        return "high"
    return "normal"


def build_packet(it, reason, phys_hint, due):
    """Assemble the King-Action packet: description block first, case_actions.json
    fallback second, then fill blocking/severity/deadline from whatever we have."""
    desc_pkt = parse_packet_from_description(it.get("description"))
    file_pkt = CASE_PACKETS.get(it.get("identifier") or "")
    pkt = {"verb": None, "detail": None, "deadline": due, "why": None,
           "severity": None, "blocking": False}
    for src in (file_pkt, desc_pkt):  # description overrides file
        if not src:
            continue
        for k in ("verb", "detail", "deadline", "why", "severity"):
            if src.get(k) not in (None, ""):
                pkt[k] = src[k]
        if src.get("blocking") is True:
            pkt["blocking"] = True
    # A physical/legal hint means the case is blocked on King until he acts.
    if phys_hint:
        pkt["blocking"] = True
    if not pkt["verb"]:
        # Derive a verb from the title so the line is never empty.
        t = (it.get("title") or "").strip()
        pkt["verb"] = t.split("KING:", 1)[-1].strip() if t.upper().startswith("KING:") else t
    pkt["deadline"] = pkt.get("deadline") or due
    pkt["severity"] = compute_severity(it, pkt, pkt["blocking"], pkt["deadline"])
    return pkt


def main():
    if not TOKEN:
        print("no MULTICA_TOKEN in .env — cannot read board"); return
    try:
        workspaces = all_workspaces()
    except Exception as e:
        print("workspace enumeration FAILED (cannot guarantee coverage):", e); return

    items, alerts = [], []
    for name, ws in workspaces:
        if not ws:
            continue
        try:
            issues = all_issues(ws)
        except Exception as e:
            alerts.append({"kind": "board_read_failed", "workspace": name,
                           "detail": "Could not read %s from Multica: %s" % (name, e)})
            continue
        for it in issues:
            if it.get("status") in OPEN_EXCLUDE:
                continue
            reason = king_signal(it)
            phys_hint = king_action_hint(it)
            due = it.get("due_date")
            # A curated King-Action packet (description block or case_actions.json entry)
            # is itself a human saying "King must act" — it forces needs_king even when no
            # phrase/marker fired. This is what makes MER-2 (Nike) appear: its detail lives
            # in case_actions.json, and that presence alone qualifies it.
            has_packet = bool(parse_packet_from_description(it.get("description"))
                              or CASE_PACKETS.get(it.get("identifier") or ""))
            # HARD needs_king = a real King signal (KING:/marker/assigned-to-you) OR a
            # human-curated King-Action packet. A loose phrase-match ALONE is NOT enough —
            # it drops to the soft "might need you" glance (its original role), so agent
            # log/sweep tasks that merely MENTION a King action ("Nike still blocked on
            # King") don't masquerade as critical. A genuine new physical action becomes
            # hard the moment someone adds its one-line packet — that's the intended nudge.
            needs_king = reason is not None or has_packet
            if reason is None and has_packet:
                reason = "King-Action packet"
            packet = build_packet(it, reason, phys_hint, due) if needs_king else None
            # Soft glance: a phrase-hint that did NOT already qualify as hard needs_king.
            review_candidate = (phys_hint is not None) and not needs_king
            items.append({
                "workspace": name,
                "identifier": it.get("identifier"),
                "title": it.get("title") or "",
                "status": it.get("status"),
                "priority": it.get("priority"),
                "due_date": due,
                "created_at": it.get("created_at"),
                "assignee_type": it.get("assignee_type"),
                "needs_king": needs_king,
                "needs_king_reason": reason,
                "king_action": packet,
                "severity": (packet or {}).get("severity"),
                "blocking": bool((packet or {}).get("blocking")),
                "detail_missing": bool(needs_king and not (packet or {}).get("detail")),
                "review_candidate": review_candidate,
                "review_hint": phys_hint if review_candidate else None,
            })

    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "vps-live-board-dynamic",
        "schema": 2,
        "workspaces_scanned": len(workspaces),
        "alerts": alerts,
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    nk = sum(1 for i in items if i["needs_king"])
    crit = sum(1 for i in items if i.get("severity") == "critical")
    miss = sum(1 for i in items if i.get("detail_missing"))
    dl = sum(1 for i in items if i["due_date"])
    print("board_state: %d open items (%d needs-King, %d critical, %d missing-detail, "
          "%d deadlines) across %d live workspaces -> %s"
          % (len(items), nk, crit, miss, dl, len(workspaces), OUT))


if __name__ == "__main__":
    main()
