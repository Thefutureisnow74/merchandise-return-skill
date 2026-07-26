#!/usr/bin/env python3
"""Build the 'Multica Dash Board' HTML message from live data and print it.
Used by the Telegram `dashboard` / `/dashboard` command (and callable by Lisa).

Reads the open-board snapshot from multica_state.json (schema 2: every needs-King
item carries a King-Action packet {verb, detail, deadline, why, severity, blocking}),
plus today's Google Calendar and recently-done.

FIXED CONTRACT — the whole point of this rewrite:
  • 🔴 CRITICAL / ONLY YOU items sort to the top by severity, then deadline.
  • Each 🔴 line is a COMPLETE instruction: verb → the specific artifact (address /
    amount / link) → deadline → why it blocks. No truncated titles, no "maybe".
  • If a needs-King item has no detail, the line says "⚠️ detail missing — open MER-X"
    so the gap is LOUD, never silently dropped. That visibility is what gets it filled.
Physical King-only actions (mail the shoes) can no longer sit in a soft "glance" bucket
— the classifier promotes them to 🔴. This is the MER-2 fix."""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/data/scripts")
import gmail_transport as _g  # noqa: E402
import mer_config             # noqa: E402  (M32 — identity comes from the profile, not a literal)

ENV = "/opt/data/.env"
STATE = "/opt/data/multica_state.json"
CT = timezone(timedelta(hours=-5))

env = {}
for line in open(ENV):
    s = line.strip()
    if s and not s.startswith("#") and "=" in s:
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
MTOK = env.get("MULTICA_TOKEN", "")
MBASE = env.get("MULTICA_SERVER_URL", "https://api.multica.ai").rstrip("/") + "/api"

SEV_RANK = {"critical": 0, "high": 1, "normal": 2, None: 3}


def mapi(path, ws=None):
    h = {"Authorization": "Bearer " + MTOK, "Content-Type": "application/json"}
    if ws:
        h["X-Workspace-Id"] = ws
    return json.loads(urllib.request.urlopen(urllib.request.Request(MBASE + path, headers=h), timeout=20).read())


def esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(t, n):
    """Truncate on a word boundary with an ellipsis so a cut line doesn't read as
    broken mid-word (the actionable head — address, email, path — leads the detail)."""
    t = (t or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def bar(n, mx, width=10):
    filled = 0 if mx == 0 else max(1, round(n / mx * width)) if n else 0
    return "█" * filled + "░" * (width - filled)


def deadline_str(dl):
    """Human deadline with urgency, e.g. 'DUE TODAY', 'overdue 2d', '2026-07-24 (in 2d)'."""
    if not dl:
        return None
    try:
        d = datetime.strptime(str(dl)[:10], "%Y-%m-%d").date()
    except Exception:
        return str(dl)
    left = (d - date.today()).days
    if left < 0:
        return "OVERDUE by %dd (%s)" % (abs(left), dl)
    if left == 0:
        return "DUE TODAY (%s)" % dl
    if left <= 2:
        return "%s (in %dd)" % (dl, left)
    return str(dl)


def sort_key(i):
    a = i.get("king_action") or {}
    dl = a.get("deadline") or i.get("due_date") or "9999-99-99"
    return (SEV_RANK.get(i.get("severity"), 3), str(dl))


def today_events():
    try:
        tok = _g.access_token()
        now = datetime.now(CT)
        tmin = urllib.parse.quote(now.strftime("%Y-%m-%dT00:00:00-05:00"))
        tmax = urllib.parse.quote(now.strftime("%Y-%m-%dT23:59:59-05:00"))
        cal = urllib.parse.quote(mer_config.calendar_id())
        u = ("https://www.googleapis.com/calendar/v3/calendars/%s/events?timeMin=%s&timeMax=%s"
             "&singleEvents=true&orderBy=startTime" % (cal, tmin, tmax))
        r = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"Authorization": "Bearer " + tok}), timeout=20).read())
        out = []
        for e in r.get("items", []):
            st = e.get("start", {})
            when = "all day" if st.get("date") else (st.get("dateTime", "")[11:16])
            out.append((when, e.get("summary", "")))
        return out
    except Exception:
        return []


def recently_done(workspaces):
    done = []
    for ws in workspaces:
        try:
            for it in mapi("/issues?limit=100&workspace_id=" + ws, ws).get("issues", []) or []:
                if it.get("status") in ("done", "closed", "completed"):
                    done.append((it.get("updated_at", ""), it.get("identifier"), it.get("title") or ""))
        except Exception:
            continue
    done.sort(reverse=True)
    return done[:4]


def render_king_line(i):
    """A COMPLETE, self-contained instruction. verb → detail → deadline → why."""
    a = i.get("king_action") or {}
    ident = i.get("identifier") or "?"
    sev = i.get("severity")
    dot = "🔴" if sev == "critical" else ("🟠" if sev == "high" else "🔵")
    verb = esc((a.get("verb") or i.get("title") or "").strip()[:80])
    out = [" %s <b>%s</b> <i>(%s)</i>" % (dot, verb, ident)]
    detail = a.get("detail")
    if detail:
        out.append("     → %s" % esc(clip(detail, 220)))
    else:
        out.append("     → ⚠️ <i>detail missing — open %s and add a KING-ACTION block</i>" % ident)
    dl = deadline_str(a.get("deadline") or i.get("due_date"))
    if dl:
        out.append("     ⏰ %s" % esc(dl))
    why = a.get("why")
    if why:
        out.append("     <i>why: %s</i>" % esc(clip(why, 180)))
    return "\n".join(out)


def main():
    if not os.path.exists(STATE):
        print("Board snapshot not ready yet — try again in a minute."); return
    s = json.load(open(STATE))
    items = s.get("items", [])

    needs = sorted([i for i in items if i.get("needs_king")], key=sort_key)
    critical = [i for i in needs if i.get("severity") == "critical"]
    other_king = [i for i in needs if i.get("severity") != "critical"]
    review = [i for i in items if i.get("review_candidate")]
    deadl = [i for i in items if i.get("due_date") and not i.get("needs_king")]
    motion = [i for i in items if i.get("assignee_type") == "agent" and not i.get("needs_king")]
    parked = [i for i in items if not i.get("needs_king") and not i.get("due_date")
              and i.get("assignee_type") != "agent"]
    ws_names = sorted(set(i["workspace"] for i in items))
    try:
        w = mapi("/workspaces"); wl = w if isinstance(w, list) else w.get("workspaces", [])
        ws_ids = [x.get("id") for x in wl if "[RETIRED]" not in (x.get("name") or "").upper()]
    except Exception:
        ws_ids = []

    now = datetime.now(CT)
    L = []
    L.append("\U0001f4ca <b>MULTICA DASH BOARD</b> — %s" % now.strftime("%a %b %d, %-I:%M%p CT"))
    mx = max(len(needs), len(motion), len(deadl), len(parked), 1)
    L.append("<pre>")
    L.append("Critical   %s %d" % (bar(len(critical), mx), len(critical)))
    L.append("Needs you  %s %d" % (bar(len(needs), mx), len(needs)))
    L.append("In motion  %s %d" % (bar(len(motion), mx), len(motion)))
    L.append("Waiting    %s %d" % (bar(len(deadl), mx), len(deadl)))
    L.append("Parked     %s %d" % (bar(len(parked), mx), len(parked)))
    L.append("</pre>")

    if critical:
        L.append("\U0001f534 <b>CRITICAL — DO THESE FIRST</b>")
        for i in critical:
            L.append(render_king_line(i))
    if other_king:
        L.append("\n\U0001f7e0 <b>NEEDS YOU</b>")
        for i in other_king:
            L.append(render_king_line(i))
    if review:
        L.append("\n⚠️ <b>MIGHT NEED YOU</b> (glance)")
        for i in review:
            L.append(" • %s <i>(%s)</i>" % (esc(i["title"][:55]), i["identifier"]))
    if deadl:
        L.append("\n⏳ <b>WAITING / DEADLINES</b>")
        for i in sorted(deadl, key=lambda x: x["due_date"]):
            L.append(" • %s — <b>%s</b> <i>(%s)</i>" % (esc(i["title"][:45]),
                                                        deadline_str(i["due_date"]), i["identifier"]))
    L.append("\n\U0001f7e1 <b>IN MOTION</b> — %d agent tasks across %d workspaces" % (len(motion), len(ws_names)))
    L.append("⚪ <b>PARKED</b> — %d backlog" % len(parked))

    ev = today_events()
    if ev:
        L.append("\n\U0001f4c5 <b>TODAY</b>")
        for when, summ in ev:
            L.append(" • %s — %s" % (when, esc(summ[:50])))

    rd = recently_done(ws_ids)
    if rd:
        L.append("\n✅ <b>RECENTLY DONE</b>")
        for _, ident, title in rd:
            L.append(" • %s — %s" % (ident, esc(title[:45])))

    L.append("\n<i>Reply to steer me: “call PPG” · “status CASE-1” · “show backlog”</i>")
    print("\n".join(L))


if __name__ == "__main__":
    main()
