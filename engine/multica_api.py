#!/usr/bin/env python3
"""
multica_api.py -- a tiny, dependency-free Multica HTTP client for the 24/7 VPS.

This is what removes the laptop dependency (Blueprint M20). The engine no longer needs the
Windows `multica` CLI: it talks straight to https://api.multica.ai with the bearer token stored
in the container's /opt/data/.env (MULTICA_TOKEN). Uses only the Python stdlib (urllib) so it
runs anywhere with no pip install.

Env: MULTICA_TOKEN (required), MULTICA_WORKSPACE_ID (default workspace),
     MULTICA_SERVER_URL (default https://api.multica.ai).

COMMAND LINE (M45) -- the missing tool that caused MER-76:

    python multica_api.py --get <issue>
    python multica_api.py --set <issue> "MR Phase=Tier1" "MR Intake Complete=true"
    python multica_api.py --smoke

Until M45 there was NO command-line way to set a property, while the skill instructs a
property write at least six times. An agent told "set MR Phase to Tier1" and given no
command that does it writes a SENTENCE into a comment instead -- which is exactly what
happened to MER-76 on 2026-07-28: the case read "Phase=Intake, Intake Complete=false" on
the board for a full day while a demand letter had already gone out and been answered.
Every automated consumer (dashboard, case_tick, every gate) was therefore wrong. Prose in
a comment is not a board write. This CLI is the fix, and set_properties() now READS BACK
every value it writes so "I set the property" is a checkable claim, not a narrative one.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("MULTICA_SERVER_URL", "https://api.multica.ai").rstrip("/") + "/api"
TOKEN = os.environ.get("MULTICA_TOKEN", "")
WORKSPACE = os.environ.get("MULTICA_WORKSPACE_ID", "")

# Last-resort token source: the desktop CLI's own config, written by `multica login`.
MULTICA_CLI_CONFIG = os.path.join(os.path.expanduser("~"), ".multica", "config.json")


class MulticaAPIError(RuntimeError):
    """An HTTP error from the Multica API. Subclasses RuntimeError so every pre-M35
    caller that does `except RuntimeError` keeps working unchanged; `.status` and
    `.body` are new and let onboarding tell 401-bad-token from 404-not-found."""

    def __init__(self, message, status=None, body="", method=None, path=None):
        RuntimeError.__init__(self, message)
        self.status = status
        self.body = body
        self.method = method
        self.path = path


class MulticaAuthError(MulticaAPIError):
    """No usable token, or the server rejected it (401/403).

    Its own type because this failure has bitten twice and both times looked like
    something else: a cron job silently 401'd for an unknown length of time, and a
    subprocess that inherited an empty environment failed with a confusing 'Bearer '
    request rather than 'your token is missing'. Callers that want to distinguish
    'I am not authenticated' from 'the board said no' catch this."""


class PropertyWriteVerificationError(MulticaAPIError):
    """A property write was accepted by the API but the board does not show the value.

    THE 2026-07-26 TRAP, MADE IMPOSSIBLE TO REPEAT. `PUT /api/issues/<id>` with a
    {"properties": {...}} body returns 200 OK and SILENTLY DISCARDS the properties --
    a success response for a write that never happened. No HTTP status can prove a
    value landed; only reading it back can. set_properties() therefore re-GETs the
    issue after every write and raises this when what the board holds is not what was
    intended.

    Attributes: .issue_id, .property_name, .intended, .actual, .mismatches
    (a list of (name, intended, actual) for every property that did not land).
    """

    def __init__(self, message, issue_id=None, mismatches=None):
        MulticaAPIError.__init__(self, message, status=None)
        self.issue_id = issue_id
        self.mismatches = list(mismatches or [])
        first = self.mismatches[0] if self.mismatches else (None, None, None)
        self.property_name, self.intended, self.actual = first


def _token():
    """The bearer token, resolved LAZILY -- per request, never once at import.

    The module global is still read first so an existing caller can override it by
    assignment, but an EMPTY global no longer poisons the process for its whole life:
    a subprocess that inherited a bare environment, or a cron job whose .env loaded
    late, now picks the token up on the next call instead of sending 'Bearer ' forever
    and getting an opaque 401.
    """
    tok = (TOKEN or "").strip() or (os.environ.get("MULTICA_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(MULTICA_CLI_CONFIG, "r", encoding="utf-8") as fh:
            tok = (json.load(fh).get("token") or "").strip()
    except Exception:
        tok = ""
    if tok:
        return tok
    raise MulticaAuthError(
        "MULTICA_TOKEN is not set -- no Multica call can be made.\n"
        "  Fix (pick one):\n"
        "    export MULTICA_TOKEN=<token>            # bash / the VPS container\n"
        "    $env:MULTICA_TOKEN = '<token>'          # PowerShell\n"
        "  The token lives in %s (after `multica login`) or in /opt/data/.env on the VPS."
        % MULTICA_CLI_CONFIG, status=None)


def _workspace(ws=None):
    """The workspace id for this call. Lazy for the same reason the token is."""
    return ws or WORKSPACE or (os.environ.get("MULTICA_WORKSPACE_ID") or "").strip()


def _req(method, path, params=None, body=None, headers=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    # Bodies are ALWAYS structured JSON built here. Never assemble a shell command string
    # and never interpolate a value into one: a related incident destroyed a dollar amount
    # ("$2,500" arrived as ",500") because content passed through a shell on the way to the
    # board. json.dumps is the only encoder in this path.
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + _token())
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        if e.code in (401, 403):
            raise MulticaAuthError(
                "Multica API %s %s -> %s: the token was REJECTED.\n"
                "  This is an authentication failure, not a missing issue. A job that keeps\n"
                "  running past this is doing nothing at all -- stop and fix the token.\n"
                "  Check MULTICA_TOKEN (env), or re-run `multica login` and copy the token\n"
                "  from %s. Server said: %s"
                % (method, path, e.code, MULTICA_CLI_CONFIG, detail),
                status=e.code, body=detail, method=method, path=path)
        raise MulticaAPIError(
            "Multica API %s %s -> %s: %s" % (method, path, e.code, detail),
            status=e.code, body=detail, method=method, path=path)
    except urllib.error.URLError as e:
        raise MulticaAPIError(
            "Multica API %s %s -> cannot reach %s: %s" % (method, path, BASE, e.reason),
            status=None, body=str(e.reason), method=method, path=path)


def property_defs(ws=None):
    """{property_id: {name, type, options{option_id: label}}}"""
    ws = _workspace(ws)
    data = _req("GET", "/properties", {"workspace_id": ws})
    defs = {}
    for p in data.get("properties", []):
        opts = {o["id"]: o.get("name") for o in ((p.get("config") or {}).get("options") or [])}
        defs[p["id"]] = {"name": p["name"], "type": p["type"], "options": opts, "raw": p}
    return defs


def name_to_defs(ws=None):
    """{property_name: def} -- for writing by name."""
    return {d["name"]: dict(d, id=pid) for pid, d in property_defs(ws).items()}


def list_issues(ws=None):
    """All issues; each gets it['mr'] = {property_name: display_value} with selects resolved."""
    ws = _workspace(ws)
    defs = property_defs(ws)
    data = _req("GET", "/issues", {"workspace_id": ws})
    issues = data.get("issues", data) if isinstance(data, dict) else data
    for it in issues:
        resolved = {}
        for pid, val in (it.get("properties") or {}).items():
            d = defs.get(pid)
            if not d:
                continue
            if d["type"] == "select":
                val = d["options"].get(val, val)
            resolved[d["name"]] = val
        it["mr"] = resolved
    return issues


def add_comment(issue_id, content, ws=None):
    ws = _workspace(ws)
    return _req("POST", "/issues/%s/comments" % issue_id, {"workspace_id": ws}, {"content": content})


# ---------------------------------------------------------------- writes (M37)

def get_issue(issue_id, ws=None):
    """One issue, raw (properties still keyed by property_id). Falls back to a list scan.

    `issue_id` may be a uuid OR a human identifier like "MER-76" -- the direct GET only
    accepts the uuid, so an identifier lands in the list scan and is matched there.
    An AUTH failure is re-raised immediately instead of being retried through the scan:
    a 401 is not a "maybe it's in the list" condition, and retrying it once hid the real
    error behind a second, identical one.
    """
    ws = _workspace(ws)
    try:
        data = _req("GET", "/issues/%s" % issue_id, {"workspace_id": ws})
        if isinstance(data, dict):
            return data.get("issue", data)
        return data
    except MulticaAuthError:
        raise
    except RuntimeError:
        for it in list_issues(ws):
            if it.get("id") == issue_id or it.get("identifier") == issue_id:
                return it
        raise


def update_issue(issue_id, fields, ws=None):
    """Update an issue.

    CRITICAL (verified 2026-07-25, the hard way): the Multica issue endpoint is **PUT**.
    PATCH and POST both return 405. A partial body is treated as a MERGE -- sending only
    {"due_date": ...} leaves title, status and all properties untouched.

    `fields` is the raw body, e.g. {"due_date": "2026-08-04"} or
    {"properties": {property_id: value}}. Prefer set_properties() for property writes:
    it resolves names -> ids and merges the existing property map, so the call is safe
    whether the server merges or replaces the properties object.
    """
    ws = _workspace(ws)
    return _req("PUT", "/issues/%s" % issue_id, {"workspace_id": ws}, fields)


TRUE_WORDS = ("true", "t", "yes", "y", "1", "on", "checked")
FALSE_WORDS = ("false", "f", "no", "n", "0", "off", "unchecked", "")


def coerce_bool(value):
    """Parse a checkbox value written by a human. Anything ambiguous raises.

    Deliberately NOT `bool(str)` -- that makes the string "false" TRUE, which on this board
    means a gate reads as satisfied when the operator typed the opposite of what they meant.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    w = str(value).strip().lower()
    if w in TRUE_WORDS:
        return True
    if w in FALSE_WORDS:
        return False
    raise ValueError("%r is not a checkbox value (use one of: %s / %s)"
                     % (value, ", ".join(TRUE_WORDS), ", ".join(w for w in FALSE_WORDS if w)))


def coerce_date(value):
    """YYYY-MM-DD, validated. date/datetime objects and full ISO stamps are accepted.

    A deadline that the board stores as junk is worse than one that was never set: every
    SLA gate reads it. So a value that is not a real calendar date raises here rather than
    landing on the board.
    """
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    s = str(value).strip()
    head = s[:10]
    try:
        y, m, d = head.split("-")
        import datetime as _dt
        _dt.date(int(y), int(m), int(d))
    except Exception:
        raise ValueError("%r is not a date -- expected YYYY-MM-DD (e.g. 2026-08-04)" % (value,))
    return head


def coerce_number(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip().replace(",", "")
    s = s[1:] if s[:1] in ("$", "€", "£") else s
    try:
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except ValueError:
        raise ValueError("%r is not a number" % (value,))


def _select_option_id(pdef, value):
    want = str(value).strip().lower()
    for oid, label in (pdef.get("options") or {}).items():
        if str(label).strip().lower() == want:
            return oid
        if str(oid) == str(value):
            return oid
    raise ValueError("property %r has no option %r\n  valid options: %s"
                     % (pdef.get("name"), value,
                        ", ".join(sorted(str(v) for v in (pdef.get("options") or {}).values()))))


def encode_property_value(pdef, value):
    """Turn a human value into what the API stores for that property type.

    Selects are stored as option_ids, so a label ("Tier2") is mapped back to its id.
    An unknown option raises ValueError -- the engine must never invent a phase that the
    board's schema does not define.

    Text is passed through UNTOUCHED: newlines, quotes and '$' survive, because the value
    is carried in a JSON body and never through a shell (see _req). A dollar amount that
    lost its digits to shell interpolation is a bug this module refuses to be able to have.
    """
    t = pdef.get("type")
    if t == "select":
        return None if value is None else _select_option_id(pdef, value)
    if t == "multi_select":
        if value in (None, ""):
            return []
        vals = value if isinstance(value, (list, tuple)) else str(value).split(",")
        return [_select_option_id(pdef, v) for v in vals if str(v).strip() != ""]
    if t in ("checkbox", "boolean"):
        return coerce_bool(value)
    if t == "date":
        return coerce_date(value)
    if t == "number":
        return coerce_number(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if t in ("text", "url") and value is not None:
        return value if isinstance(value, str) else str(value)
    return value


def resolve_property(defs, name):
    """Find a property definition by name, CASE-INSENSITIVELY, or raise with the list.

    Case-insensitive because the operator types the name from memory ("mr phase") and a
    KeyError on capitalisation is exactly the friction that sends an agent back to writing
    the value into a comment instead.
    """
    d = defs.get(name)
    if d:
        return name, d
    want = str(name).strip().lower()
    for real, dd in defs.items():
        if str(real).strip().lower() == want:
            return real, dd
    raise ValueError("this workspace has no property named %r\n  properties: %s"
                     % (name, ", ".join(sorted(defs))))


def _display(pdef, raw):
    """What a stored value looks like to a human (select ids -> labels)."""
    opts = (pdef or {}).get("options") or {}
    if isinstance(raw, list):
        return [opts.get(v, v) for v in raw]
    return opts.get(raw, raw)


def _same_value(intended_encoded, got, pdef=None):
    """Did the board store what we asked for? Normalized-string comparison.

    The board answers in more than one shape: get_issue returns a select as its raw option
    id, list_issues (the fallback path) resolves it to the label; a date comes back as
    '2026-08-05' or as '2026-08-05T00:00:00Z'. All of those count as a match. Nothing else
    does -- a near miss is a miss.
    """
    if isinstance(intended_encoded, bool):
        try:
            return coerce_bool(got) == intended_encoded
        except ValueError:
            return False
    if intended_encoded is None:
        return got in (None, "", [])
    if isinstance(intended_encoded, list):
        want = [str(v) for v in intended_encoded]
        have = [str(v) for v in (got or [])] if isinstance(got, (list, tuple)) else []
        labels = [str(_display(pdef, v)) for v in (got or [])] if isinstance(got, (list, tuple)) else []
        wl = [str(_display(pdef, v)) for v in intended_encoded]
        return want == have or wl == labels
    w, g = str(intended_encoded), "" if got is None else str(got)
    if w == g:
        return True
    if len(w) == 10 and w[:4].isdigit() and g.startswith(w):        # date vs datetime
        return True
    label = (pdef or {}).get("options", {}).get(intended_encoded)     # id written, label read
    if label is not None and str(label).strip().lower() == g.strip().lower():
        return True
    return False


def read_properties(issue, ws=None, defs=None):
    """{property_name: display_value} straight off the board, freshly read.

    This is the read half of the fix: it is what makes a claim about the board checkable.
    Always re-GETs -- never trusts a cached issue dict.
    """
    ws = _workspace(ws)
    defs = defs if defs is not None else name_to_defs(ws)
    by_id = {d["id"]: (name, d) for name, d in defs.items()}
    issue_id = issue["id"] if isinstance(issue, dict) else issue
    raw = get_issue(issue_id, ws) or {}
    out = {}
    if raw.get("mr"):                       # get_issue fell back to the list scan
        out.update(raw["mr"])
    for pid, val in (raw.get("properties") or {}).items():
        ent = by_id.get(pid)
        if ent:
            out[ent[0]] = _display(ent[1], val)
    return out, raw


def verify_properties(issue, intended_by_name, ws=None, defs=None):
    """Re-read the issue and prove every intended value actually landed.

    Returns [(name, intended_display, actual_display, ok), ...]. Raises nothing -- the
    caller decides whether a mismatch is fatal. set_properties decides that it is.
    """
    ws = _workspace(ws)
    defs = defs if defs is not None else name_to_defs(ws)
    issue_id = issue["id"] if isinstance(issue, dict) else issue
    raw = get_issue(issue_id, ws) or {}
    props = raw.get("properties") or {}
    resolved = raw.get("mr") or {}
    rows = []
    for name, val in intended_by_name.items():
        real, d = resolve_property(defs, name)
        enc = encode_property_value(d, val)
        got = props.get(d["id"]) if d["id"] in props else resolved.get(real)
        rows.append((real, _display(d, enc), _display(d, got), _same_value(enc, got, d)))
    return rows


def set_properties(issue, values_by_name, ws=None, defs=None, verify=True):
    """Set one or more properties BY NAME on an issue. Returns the API response.

    `issue` may be an issue dict (as returned by list_issues -- preferred, saves a round
    trip) or a bare issue id. Resolving by name is what keeps the engine portable across
    boards: a freshly provisioned workspace has different property ids but the same names.

    ⚠️ PROPERTIES HAVE THEIR OWN ENDPOINT -- verified by probe 2026-07-26:

        PUT /api/issues/<issue_id>/properties/<property_id>   {"value": <encoded>}

    Do NOT write properties through `PUT /api/issues/<id>` with a {"properties": {...}} body.
    That call returns **200 OK and silently discards the properties** -- it is not an error you
    can catch, the response looks like a success, and the board simply never changes. This
    exact silent no-op would have broken M37 phase advancement in live: case_tick would log
    "advanced", idempotency.reserve would record the advance as done (it reserves BEFORE the
    write), and the case would never actually move -- a ladder that reports climbing while
    standing still. Each property is therefore written individually against its own endpoint,
    and a failure raises instead of being swallowed.

    OK: READ-BACK VERIFICATION (M45, `verify=True` by default). After the writes, the issue
    is re-GET and every value compared against what was intended. A mismatch raises
    PropertyWriteVerificationError naming the property, the intended value and what the
    board actually holds. An HTTP 200 is not evidence; the re-read is. `verify=False` exists
    only for a caller that is about to do its own read-back (case_tick.advance does) and
    must never be used to make a failing write quiet.
    """
    ws = _workspace(ws)
    if not isinstance(issue, dict):
        issue = get_issue(issue, ws)
    defs = defs if defs is not None else name_to_defs(ws)
    # Resolve and encode EVERYTHING before writing ANYTHING. A bad option name in the third
    # pair must not leave the first two already on the board: half a phase change is worse
    # than none, because it looks like a completed move.
    plan = []
    for name, val in values_by_name.items():
        real, d = resolve_property(defs, name)
        plan.append((real, d, encode_property_value(d, val)))
    out = None
    for _real, d, enc in plan:
        out = _req("PUT", "/issues/%s/properties/%s" % (issue["id"], d["id"]),
                   {"workspace_id": ws}, {"value": enc})
    if verify:
        rows = verify_properties(issue, values_by_name, ws=ws, defs=defs)
        bad = [(n, i, a) for (n, i, a, ok) in rows if not ok]
        if bad:
            raise PropertyWriteVerificationError(
                "WRITE DID NOT LAND on %s. The API accepted the call and the board does not "
                "show the value -- this is the 200-OK-and-silently-discards trap. NOT SET: %s"
                % (issue.get("identifier") or issue.get("id"),
                   "; ".join("%s: intended %r, board has %r" % b for b in bad)),
                issue_id=issue.get("id"), mismatches=bad)
    return out


# ------------------------------------------------------- provisioning / creates (M35)
#
# ENDPOINTS -- discovered and verified 2026-07-25 (see references/onboarding.md):
#
#   GET  /api/workspaces                        -> a BARE JSON ARRAY of workspaces
#   POST /api/workspaces                        {"name","slug","issue_prefix"}
#   GET  /api/projects?workspace_id=<ws>        -> {"projects": [...]}
#   POST /api/projects?workspace_id=<ws>        {"title","description","status"}
#   GET  /api/properties?workspace_id=<ws>      -> {"properties": [...]}
#   POST /api/properties?workspace_id=<ws>      {"name","type","description","icon",
#                                                "config":{"options":[{"name","color"}]}}
#
# How they were verified WITHOUT writing anything to a live board:
#   1. Every GET was run read-only against the live API.
#   2. Every POST route was confirmed to EXIST by posting a body that cannot possibly
#      create anything: POST /api/workspaces {} -> 400 "name and slug are required";
#      POST /api/properties?workspace_id=<ws> {} -> 400 "name is required";
#      POST /api/projects?workspace_id=<ws> {} -> 400 "title is required".
#      A route that does not exist answers with plain-text "404 page not found"
#      (proved with POST /api/floopdoop), so a JSON 400/404 is proof of a real route.
#   3. The exact request BODY SHAPES above were captured by pointing the official
#      `multica` CLI at a local loopback stub with --server-url / $MULTICA_SERVER_URL
#      and logging what it sent. Nothing left the machine.
#
# Writes remain PUT for issues (see update_issue). Creates are POST.

PROPERTY_TYPES = ("text", "number", "select", "multi_select", "date", "checkbox", "url")


def _unwrap(resp, key):
    """The API is inconsistent about wrapping. Accept {key: obj}, {key: [..]}, or a bare value."""
    if isinstance(resp, dict):
        return resp.get(key, resp)
    return resp


def whoami():
    """Verify the connection AND the token in one read-only call.

    Returns the list of workspaces the token can see. Raises MulticaAPIError with a
    message a human can act on -- an unset/invalid token must fail LOUDLY here, before
    onboarding has created anything, so a run can never half-provision.
    """
    _token()          # raises MulticaAuthError with the actionable message if unset
    return list_workspaces()


def list_workspaces():
    """Every workspace the token can see. GET /api/workspaces returns a BARE ARRAY."""
    data = _req("GET", "/workspaces")
    if isinstance(data, dict):
        data = data.get("workspaces", [])
    return list(data or [])


def find_workspace(name=None, slug=None, ws_id=None):
    """Adopt-by-identity: first match on id, then slug, then case-insensitive name."""
    for w in list_workspaces():
        if ws_id and w.get("id") == ws_id:
            return w
    for w in list_workspaces() if slug or name else []:
        if slug and (w.get("slug") or "").lower() == str(slug).lower():
            return w
        if name and (w.get("name") or "").strip().lower() == str(name).strip().lower():
            return w
    return None


def create_workspace(name, slug, issue_prefix=None, description=None):
    """POST /api/workspaces. `slug` is PERMANENT (lowercase letters, digits, hyphens)."""
    body = {"name": name, "slug": slug}
    if issue_prefix:
        body["issue_prefix"] = issue_prefix
    if description:
        body["description"] = description
    return _unwrap(_req("POST", "/workspaces", None, body), "workspace")


def list_projects(ws=None):
    ws = _workspace(ws)
    data = _req("GET", "/projects", {"workspace_id": ws})
    return list(_unwrap(data, "projects") or [])


def find_project(title, ws=None):
    """Case-insensitive title match -- the adopt path for a re-run."""
    want = str(title).strip().lower()
    for p in list_projects(ws):
        if (p.get("title") or "").strip().lower() == want:
            return p
    return None


def create_project(title, description=None, status=None, icon=None, ws=None):
    """POST /api/projects?workspace_id=<ws>."""
    ws = _workspace(ws)
    body = {"title": title}
    if description:
        body["description"] = description
    if status:
        body["status"] = status
    if icon:
        body["icon"] = icon
    return _unwrap(_req("POST", "/projects", {"workspace_id": ws}, body), "project")


def list_properties(ws=None, include_archived=False):
    """Raw property definitions, exactly as the API returns them (unlike property_defs,
    which reshapes them for reading issue values). Provisioning needs the raw form."""
    ws = _workspace(ws)
    params = {"workspace_id": ws}
    if include_archived:
        params["include_archived"] = "true"
    data = _req("GET", "/properties", params)
    return list(_unwrap(data, "properties") or [])


def property_option_names(prop):
    """The select-option labels on a raw property definition, in board order."""
    return [o.get("name") for o in ((prop.get("config") or {}).get("options") or [])]


def create_property(name, type, options=None, description=None, icon=None, ws=None):
    """POST /api/properties?workspace_id=<ws>.

    `options` is a list of labels, or of (label, color) pairs / {"name","color"} dicts --
    select and multi_select only. Property NAME is the portable key: the engine resolves
    every property by name (see name_to_defs), so a freshly provisioned board with brand
    new ids behaves identically to King's. Never rename a property to "fix" something.
    """
    ws = _workspace(ws)
    if type not in PROPERTY_TYPES:
        raise ValueError("unknown property type %r (valid: %s)" % (type, ", ".join(PROPERTY_TYPES)))
    body = {"name": name, "type": type}
    if description:
        body["description"] = description
    if icon:
        body["icon"] = icon
    if options:
        if type not in ("select", "multi_select"):
            raise ValueError("property type %r cannot carry options" % type)
        norm = []
        for o in options:
            if isinstance(o, dict):
                norm.append({"name": o.get("name"), "color": o.get("color")})
            elif isinstance(o, (list, tuple)):
                norm.append({"name": o[0], "color": o[1] if len(o) > 1 else None})
            else:
                norm.append({"name": o, "color": None})
        body["config"] = {"options": [{k: v for k, v in d.items() if v} for d in norm]}
    return _unwrap(_req("POST", "/properties", {"workspace_id": ws}, body), "property")


# ------------------------------------------------------------------ issue create (M43)
#
#   POST /api/issues?workspace_id=<ws>
#        {"title", "description", "project_id", "status", "priority", "due_date"}
#
# HOW THIS WAS VERIFIED WITHOUT WRITING ANYTHING TO A LIVE BOARD (2026-07-26), the same
# method M35 used for /workspaces, /projects and /properties:
#
#   1. ROUTE EXISTS. POST /api/issues?workspace_id=<King's ws> with an EMPTY body {} --
#      a body that cannot possibly create anything -- answered:
#          400 {"error":"title is required"}
#      The control, POST /api/floopdoop?workspace_id=<ws> {}, answered the router's
#      plain-text  404 page not found . A JSON 400 naming a required field is therefore
#      proof of a real, reachable route; no issue was created. (Re-probing with
#      {"title": ""} also returned 400 "title is required" -- an empty title is refused,
#      so the probe could not create even by accident.)
#   2. BODY SHAPE. Captured by pointing the official `multica` CLI at a 127.0.0.1 stub
#      via --server-url and logging what it sent. Nothing left the machine:
#          POST /api/issues
#          {"description":"line one","due_date":"2026-08-10","priority":"high",
#           "project_id":"2222...","status":"todo","title":"STUB PROBE"}
#      The CLI carries the workspace in an `X-Workspace-Id` HEADER rather than the query
#      string, so create_issue sends BOTH the header and ?workspace_id= -- belt and braces,
#      and consistent with every other create in this module.
#
# ⚠️ PROPERTIES CANNOT BE SET INLINE. `multica issue create --help` has no property flag,
# the CLI sends no property field, and PUT /api/issues/<id> {"properties": ...} is the known
# 200-OK-and-silently-discards trap (see set_properties). So a case is opened in TWO steps:
# create, then set_properties. Step two failing is NOT cosmetic -- an issue with no `MR
# Phase` is INVISIBLE to case_tick/mer_engine, which select cases by phase. A half-created
# case would sit on the board looking fine while no engine ever touches it. That failure is
# therefore raised as PropertiesNotSetError carrying the orphan's id, never swallowed.


class PropertiesNotSetError(MulticaAPIError):
    """An issue WAS created but its properties were NOT written.

    This is the loud half-created state. `.issue` is the orphan that exists on the board
    with no MR Phase -- invisible to every engine module that selects by phase. The caller
    must surface the identifier so a human can finish or delete it; it must never be
    treated as a successful open.
    """

    def __init__(self, message, issue=None, cause=None):
        MulticaAPIError.__init__(self, message, status=None, body=str(cause or ""))
        self.issue = issue or {}
        self.cause = cause


def create_issue(title, description=None, project_id=None, status=None, priority=None,
                 due_date=None, properties=None, ws=None, defs=None):
    """POST /api/issues?workspace_id=<ws> -- open a new issue.

    `properties` is an optional {property_name: value} map written in a SECOND call via
    set_properties (properties cannot be created inline -- see the block above). If that
    second call fails, this raises PropertiesNotSetError carrying the created issue, so the
    caller cannot mistake a half-created, engine-invisible case for a successful open.

    This function does NOT check for duplicates. Duplicate refusal belongs to the caller
    BEFORE it gets here (dup_guard.guard) -- a create function that quietly deduped would
    hide the refusal instead of reporting it. See new_case.py.
    """
    ws = _workspace(ws)
    if not str(title or "").strip():
        raise ValueError("create_issue: title is required (the API refuses an empty title)")
    body = {"title": title}
    for k, v in (("description", description), ("project_id", project_id),
                 ("status", status), ("priority", priority), ("due_date", due_date)):
        if v not in (None, ""):
            body[k] = v
    resp = _req("POST", "/issues", {"workspace_id": ws}, body,
                headers={"X-Workspace-Id": ws})
    issue = _unwrap(resp, "issue") or {}
    if not isinstance(issue, dict) or not issue.get("id"):
        raise MulticaAPIError(
            "create_issue: the API accepted the POST but returned no issue id (%r). "
            "Cannot set properties on it -- check the board by hand before retrying, an "
            "issue may exist." % (resp,), status=None)
    if properties:
        try:
            set_properties(issue, properties, ws=ws, defs=defs)
        except Exception as exc:
            raise PropertiesNotSetError(
                "HALF-CREATED CASE. Issue %s (%s) was created but its properties %s could "
                "NOT be written (%s: %s). An issue with no 'MR Phase' is INVISIBLE to the "
                "engine -- case_tick and mer_engine select cases by phase and will never "
                "touch it. Fix or delete %s by hand; do not treat this as an open case."
                % (issue.get("identifier") or issue.get("id"), issue.get("id"),
                   sorted(properties), type(exc).__name__, exc,
                   issue.get("identifier") or issue.get("id")),
                issue=issue, cause=exc)
    return issue


# ------------------------------------------------------------------ CLI (M45)

USAGE = """multica_api.py  -  read and WRITE Multica issue properties from the command line.

  python multica_api.py --get <issue>
  python multica_api.py --set <issue> "MR Phase=Tier1" "MR Intake Complete=true"
  python multica_api.py --smoke

  <issue>   a uuid or a human identifier such as MER-76.

  --set     one or more "Name=Value" pairs. Property names resolve case-insensitively.
            Values are coerced by the property's declared type:
              select    -> matched to an option by label (unknown option lists the valid ones)
              checkbox  -> true/false, yes/no, 1/0, on/off
              date      -> YYYY-MM-DD, validated
              number    -> 12, 12.5, "$2,500"
              text/url  -> passed through byte for byte
            Every write is READ BACK off the board and printed. If the board does not show
            what was written the command FAILS (exit 3)  -  an HTTP 200 proves nothing.

  LONG OR MULTI-LINE VALUES  -  read the value from a file with a leading @:
              "MR Delay Explanation=@C:\\path\\to\\note.txt"
            The file is read as UTF-8 and sent verbatim inside a JSON body: newlines,
            quotes and '$2,500' survive intact, because nothing here ever touches a shell.
            To write a value that literally STARTS with '@', double it: "Note=@@handle".

  Options:  --workspace <id>   override MULTICA_WORKSPACE_ID
            --json             machine-readable output

  Exit codes: 0 ok | 1 usage/arguments | 2 API or auth failure | 3 read-back mismatch

WHY THIS EXISTS: writing "MR Phase: Tier1" as a sentence in a Multica COMMENT does not
change the board. Comments are prose; the dashboard, case_tick and every gate read
PROPERTIES. On 2026-07-28 MER-76 sat at "Phase=Intake, Intake Complete=false" for a day
after a demand letter had already been sent and answered, because there was no command
that set a property. Use --set. Then paste the read-back into your notes as proof.
"""


def _parse_value(raw):
    """A CLI value. Leading '@' reads the value from a file; '@@' escapes a literal '@'."""
    if raw.startswith("@@"):
        return raw[1:]
    if raw.startswith("@"):
        path = raw[1:]
        if not os.path.isfile(path):
            raise ValueError("value file not found: %s\n"
                             "  (a leading '@' means 'read the value from this file'; to write a\n"
                             "   literal value starting with '@', double it: '@@%s')" % (path, path))
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    return raw


def _parse_pairs(args):
    pairs = []
    for a in args:
        if "=" not in a:
            raise ValueError('expected "Name=Value", got %r' % a)
        name, raw = a.split("=", 1)
        pairs.append((name.strip(), _parse_value(raw)))
    return pairs


def _print_table(rows, headers):
    widths = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]
    line = "  ".join(str(headers[i]).ljust(widths[i]) for i in range(len(headers)))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        print("  ".join(str(r[i]).ljust(widths[i]) for i in range(len(headers))))


def _short(v, n=60):
    s = "" if v is None else str(v)
    s = s.replace("\r", "").replace("\n", "\\n")
    return s if len(s) <= n else s[:n - 1] + "..."


def main(argv):
    argv = list(argv)
    as_json = False
    ws = None
    if "--json" in argv:
        as_json = True
        argv.remove("--json")
    if "--workspace" in argv:
        i = argv.index("--workspace")
        try:
            ws = argv[i + 1]
        except IndexError:
            sys.stderr.write("--workspace needs a workspace id\n")
            return 1
        del argv[i:i + 2]

    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    # No arguments keeps the original behaviour: the read-only smoke test that proves
    # the VPS can reach the board. Reading is always safe; nothing here writes by default.
    cmd, rest = (argv[0], argv[1:]) if argv else ("--smoke", [])

    if cmd == "--smoke":
        print("BASE: %s | workspace: %s | token: %s"
              % (BASE, _workspace(ws), "set" if (TOKEN or os.environ.get("MULTICA_TOKEN")) else "from config"))
        issues = list_issues(ws)
        print("issues:", len(issues))
        for it in issues:
            if it.get("mr"):
                print(" ", it.get("identifier"), it["mr"])
        return 0

    if cmd == "--get":
        if len(rest) != 1:
            sys.stderr.write("usage: --get <issue>\n")
            return 1
        defs = name_to_defs(ws)
        values, raw = read_properties(rest[0], ws=ws, defs=defs)
        if as_json:
            print(json.dumps({"issue": raw.get("identifier") or rest[0],
                              "id": raw.get("id"), "properties": values},
                             indent=2, default=str))
            return 0
        print("%s  -  %s" % (raw.get("identifier") or rest[0], raw.get("title") or ""))
        print("id: %s" % raw.get("id"))
        rows = [(n, (defs.get(n) or {}).get("type", "?"), _short(values.get(n)))
                for n in sorted(defs)]
        _print_table(rows, ("PROPERTY", "TYPE", "VALUE"))
        return 0

    if cmd == "--set":
        if len(rest) < 2:
            sys.stderr.write('usage: --set <issue> "Name=Value" ["Name=Value" ...]\n')
            return 1
        target, pairs = rest[0], _parse_pairs(rest[1:])
        defs = name_to_defs(ws)
        values = {}
        for name, val in pairs:
            real, _d = resolve_property(defs, name)      # fail before any write on a bad name
            values[real] = val
        issue = get_issue(target, ws)
        if not isinstance(issue, dict) or not issue.get("id"):
            sys.stderr.write("no issue found for %r\n" % target)
            return 2
        ident = issue.get("identifier") or issue.get("id")
        try:
            set_properties(issue, values, ws=ws, defs=defs)
        except PropertyWriteVerificationError as e:
            sys.stderr.write("FAILED: %s\n" % e)
            return 3
        rows = verify_properties(issue, values, ws=ws, defs=defs)
        if as_json:
            print(json.dumps({"issue": ident, "id": issue.get("id"),
                              "verified": [{"property": n, "intended": i, "board": a, "ok": ok}
                                           for (n, i, a, ok) in rows]}, indent=2, default=str))
            return 0
        print("SET %s  -  read-back VERIFIED against the board:" % ident)
        _print_table([(n, _short(i), _short(a), "OK" if ok else "MISMATCH")
                      for (n, i, a, ok) in rows],
                     ("PROPERTY", "INTENDED", "BOARD NOW", ""))
        return 0

    sys.stderr.write("unknown command %r\n\n%s" % (cmd, USAGE))
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except MulticaAuthError as exc:
        sys.stderr.write("AUTH: %s\n" % exc)
        sys.exit(2)
    except MulticaAPIError as exc:
        sys.stderr.write("API: %s\n" % exc)
        sys.exit(2)
    except ValueError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        sys.exit(1)
