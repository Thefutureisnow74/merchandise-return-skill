#!/usr/bin/env python3
"""Auto-sync Multica issue due-dates -> the profile owner's Google Calendar.

Idempotent: exactly one all-day event per dated OPEN issue, tagged with the issue
id. Date changed -> event moves. Issue closed or date removed -> event deleted.
Only ever touches events IT created (tagged multica_id) — never the user's own events.
Uses the VPS's existing Google token (calendar scope) via gmail_transport.
Run with --dry-run to preview.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import gmail_transport as _g  # noqa: E402  (self-refreshing Google access token)
import mer_config             # noqa: E402  (M32 — identity comes from the profile, not a literal)

ENV = "/opt/data/.env"
MAP_PATH = "/opt/data/multica_calendar_map.json"
CAL = mer_config.calendar_id()          # M32: profile's calendar (defaults to the profile email)
DRY = "--dry-run" in sys.argv

env = {}
for line in open(ENV):
    s = line.strip()
    if s and not s.startswith("#") and "=" in s:
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
MTOK = env.get("MULTICA_TOKEN", "")
MBASE = env.get("MULTICA_SERVER_URL", "https://api.multica.ai").rstrip("/") + "/api"
# Active workspaces (retired ones excluded on purpose).
#
# The user's OWN return workspace always syncs. Any EXTRA workspace is opt-in via env and has NO
# default: these used to carry the author's private workspace UUIDs as hardcoded fallbacks, which
# meant every stranger who installed the package received them. A workspace id is account-specific
# — there is no sensible default for somebody else's board, so an unset var means "not mine, skip".
WORKSPACES = {"Merchandise Return": mer_config.multica_workspace_id()}
for _label, _var in (("AMBS Studio", "MULTICA_WS_AMBS_STUDIO"),
                     ("Lisa Rebuild", "MULTICA_WS_LISA_REBUILD")):
    _wsid = env.get(_var, "").strip()
    if _wsid:
        WORKSPACES[_label] = _wsid
WORKSPACES = {k: v for k, v in WORKSPACES.items() if v}
OPEN_EXCLUDE = {"done", "cancelled", "closed"}
CAL_BASE = "https://www.googleapis.com/calendar/v3/calendars/%s/events" % urllib.parse.quote(CAL)


def m_workspaces():
    """All ACTIVE workspaces, discovered live (never hardcoded). Skips [RETIRED]."""
    req = urllib.request.Request(MBASE + "/workspaces", headers={
        "Authorization": "Bearer " + MTOK, "Content-Type": "application/json"})
    w = json.loads(urllib.request.urlopen(req, timeout=25).read())
    wl = w if isinstance(w, list) else w.get("workspaces", [])
    return [(x.get("name"), x.get("id")) for x in wl
            if "[RETIRED]" not in (x.get("name") or "").upper()]


def m_get(ws):
    q = urllib.parse.urlencode({"limit": 200, "workspace_id": ws})
    req = urllib.request.Request(MBASE + "/issues?" + q, headers={
        "Authorization": "Bearer " + MTOK, "X-Workspace-Id": ws, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read()).get("issues", []) or []


def gcal(method, path="", body=None):
    tok = _g.access_token()
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CAL_BASE + path, method=method, data=d, headers={
        "Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=25)
    return json.loads(r.read()) if r.status != 204 else {}


def event_body(issue, ws_name):
    due = issue["due_date"]
    end = (datetime.strptime(due, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    ident = issue.get("identifier", "")
    title = (issue.get("title") or "").replace("\n", " ")[:90]
    return {
        "summary": "\U0001f4cc [%s] %s" % (ident, title),
        "description": "Auto-synced from Multica (%s). Status: %s.\nhttps://multica.ai" % (
            ws_name, issue.get("status", "")),
        "start": {"date": due}, "end": {"date": end},
        "extendedProperties": {"private": {"multica_id": issue.get("id", ""), "multica_ident": ident}},
    }


def main():
    the_map = {}
    if os.path.exists(MAP_PATH):
        try: the_map = json.load(open(MAP_PATH))
        except Exception: the_map = {}

    # collect current dated open issues
    dated = {}
    for name, ws in m_workspaces():
        if not ws: continue
        try: issues = m_get(ws)
        except Exception as e:
            print("read %s FAILED: %s" % (name, e)); continue
        for it in issues:
            if it.get("status") in OPEN_EXCLUDE or not it.get("due_date"): continue
            dated[it["id"]] = (it, name)

    created = updated = deleted = skipped = 0
    # upsert
    for iid, (it, name) in dated.items():
        prev = the_map.get(iid)
        if prev and prev.get("due_date") == it["due_date"]:
            skipped += 1; continue
        body = event_body(it, name)
        if prev and prev.get("event_id"):
            print("%s UPDATE %s -> %s" % (it["identifier"], prev.get("due_date"), it["due_date"]))
            if not DRY:
                gcal("PATCH", "/" + prev["event_id"], body)
            the_map[iid] = {"event_id": prev["event_id"], "due_date": it["due_date"], "ident": it["identifier"]}
            updated += 1
        else:
            print("%s CREATE @ %s" % (it["identifier"], it["due_date"]))
            if not DRY:
                ev = gcal("POST", "", body)
                the_map[iid] = {"event_id": ev.get("id"), "due_date": it["due_date"], "ident": it["identifier"]}
            created += 1
    # cleanup — events for issues no longer dated/open
    for iid in list(the_map.keys()):
        if iid not in dated:
            ev = the_map[iid]
            print("%s DELETE (closed/undated)" % ev.get("ident", iid))
            if not DRY and ev.get("event_id"):
                try: gcal("DELETE", "/" + ev["event_id"])
                except Exception as e: print("  delete err: %s" % e)
            del the_map[iid]
            deleted += 1

    if not DRY:
        json.dump(the_map, open(MAP_PATH, "w"), indent=2)
    print("calendar_sync%s: +%d created, %d updated, %d deleted, %d unchanged" % (
        " [DRY]" if DRY else "", created, updated, deleted, skipped))


if __name__ == "__main__":
    main()
