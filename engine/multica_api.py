#!/usr/bin/env python3
"""
multica_api.py — a tiny, dependency-free Multica HTTP client for the 24/7 VPS.

This is what removes the laptop dependency (Blueprint M20). The engine no longer needs the
Windows `multica` CLI: it talks straight to https://api.multica.ai with the bearer token stored
in the container's /opt/data/.env (MULTICA_TOKEN). Uses only the Python stdlib (urllib) so it
runs anywhere with no pip install.

Env: MULTICA_TOKEN (required), MULTICA_WORKSPACE_ID (default workspace),
     MULTICA_SERVER_URL (default https://api.multica.ai).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("MULTICA_SERVER_URL", "https://api.multica.ai").rstrip("/") + "/api"
TOKEN = os.environ.get("MULTICA_TOKEN", "")
WORKSPACE = os.environ.get("MULTICA_WORKSPACE_ID", "")


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


def _req(method, path, params=None, body=None, headers=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        raise MulticaAPIError(
            "Multica API %s %s -> %s: %s" % (method, path, e.code, detail),
            status=e.code, body=detail, method=method, path=path)
    except urllib.error.URLError as e:
        raise MulticaAPIError(
            "Multica API %s %s -> cannot reach %s: %s" % (method, path, BASE, e.reason),
            status=None, body=str(e.reason), method=method, path=path)


def property_defs(ws=None):
    """{property_id: {name, type, options{option_id: label}}}"""
    ws = ws or WORKSPACE
    data = _req("GET", "/properties", {"workspace_id": ws})
    defs = {}
    for p in data.get("properties", []):
        opts = {o["id"]: o.get("name") for o in ((p.get("config") or {}).get("options") or [])}
        defs[p["id"]] = {"name": p["name"], "type": p["type"], "options": opts, "raw": p}
    return defs


def name_to_defs(ws=None):
    """{property_name: def} — for writing by name."""
    return {d["name"]: dict(d, id=pid) for pid, d in property_defs(ws).items()}


def list_issues(ws=None):
    """All issues; each gets it['mr'] = {property_name: display_value} with selects resolved."""
    ws = ws or WORKSPACE
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
    ws = ws or WORKSPACE
    return _req("POST", "/issues/%s/comments" % issue_id, {"workspace_id": ws}, {"content": content})


# ---------------------------------------------------------------- writes (M37)

def get_issue(issue_id, ws=None):
    """One issue, raw (properties still keyed by property_id). Falls back to a list scan."""
    ws = ws or WORKSPACE
    try:
        data = _req("GET", "/issues/%s" % issue_id, {"workspace_id": ws})
        if isinstance(data, dict):
            return data.get("issue", data)
        return data
    except RuntimeError:
        for it in list_issues(ws):
            if it.get("id") == issue_id or it.get("identifier") == issue_id:
                return it
        raise


def update_issue(issue_id, fields, ws=None):
    """Update an issue.

    CRITICAL (verified 2026-07-25, the hard way): the Multica issue endpoint is **PUT**.
    PATCH and POST both return 405. A partial body is treated as a MERGE — sending only
    {"due_date": ...} leaves title, status and all properties untouched.

    `fields` is the raw body, e.g. {"due_date": "2026-08-04"} or
    {"properties": {property_id: value}}. Prefer set_properties() for property writes:
    it resolves names -> ids and merges the existing property map, so the call is safe
    whether the server merges or replaces the properties object.
    """
    ws = ws or WORKSPACE
    return _req("PUT", "/issues/%s" % issue_id, {"workspace_id": ws}, fields)


def encode_property_value(pdef, value):
    """Turn a human value into what the API stores for that property type.

    Selects are stored as option_ids, so a label ("Tier2") is mapped back to its id.
    An unknown option raises ValueError — the engine must never invent a phase that the
    board's schema does not define.
    """
    if pdef.get("type") == "select":
        if value is None:
            return None
        want = str(value).strip().lower()
        for oid, label in (pdef.get("options") or {}).items():
            if str(label).strip().lower() == want:
                return oid
            if str(oid) == str(value):
                return oid
        raise ValueError("property %r has no option %r (options: %s)"
                         % (pdef.get("name"), value, sorted((pdef.get("options") or {}).values())))
    if pdef.get("type") in ("checkbox", "boolean"):
        return bool(value) if not isinstance(value, str) else str(value).lower() in ("true", "1", "yes")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def set_properties(issue, values_by_name, ws=None, defs=None):
    """Set one or more properties BY NAME on an issue. Returns the API response.

    `issue` may be an issue dict (as returned by list_issues — preferred, saves a round
    trip) or a bare issue id. Resolving by name is what keeps the engine portable across
    boards: a freshly provisioned workspace has different property ids but the same names.

    ⚠️ PROPERTIES HAVE THEIR OWN ENDPOINT — verified by probe 2026-07-26:

        PUT /api/issues/<issue_id>/properties/<property_id>   {"value": <encoded>}

    Do NOT write properties through `PUT /api/issues/<id>` with a {"properties": {...}} body.
    That call returns **200 OK and silently discards the properties** — it is not an error you
    can catch, the response looks like a success, and the board simply never changes. This
    exact silent no-op would have broken M37 phase advancement in live: case_tick would log
    "advanced", idempotency.reserve would record the advance as done (it reserves BEFORE the
    write), and the case would never actually move — a ladder that reports climbing while
    standing still. Each property is therefore written individually against its own endpoint,
    and a failure raises instead of being swallowed.
    """
    ws = ws or WORKSPACE
    if not isinstance(issue, dict):
        issue = get_issue(issue, ws)
    defs = defs if defs is not None else name_to_defs(ws)
    out = None
    for name, val in values_by_name.items():
        d = defs.get(name)
        if not d:
            raise ValueError("workspace has no property named %r" % name)
        out = _req("PUT", "/issues/%s/properties/%s" % (issue["id"], d["id"]),
                   {"workspace_id": ws}, {"value": encode_property_value(d, val)})
    return out


# ------------------------------------------------------- provisioning / creates (M35)
#
# ENDPOINTS — discovered and verified 2026-07-25 (see references/onboarding.md):
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
    message a human can act on — an unset/invalid token must fail LOUDLY here, before
    onboarding has created anything, so a run can never half-provision.
    """
    if not TOKEN.strip():
        raise MulticaAPIError(
            "MULTICA_TOKEN is not set. Onboarding cannot verify the connection.\n"
            "  Fix: export MULTICA_TOKEN=<your token>  (find it in ~/.multica/config.json\n"
            "  after `multica login`, or in /opt/data/.env on the VPS).", status=None)
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
    ws = ws or WORKSPACE
    data = _req("GET", "/projects", {"workspace_id": ws})
    return list(_unwrap(data, "projects") or [])


def find_project(title, ws=None):
    """Case-insensitive title match — the adopt path for a re-run."""
    want = str(title).strip().lower()
    for p in list_projects(ws):
        if (p.get("title") or "").strip().lower() == want:
            return p
    return None


def create_project(title, description=None, status=None, icon=None, ws=None):
    """POST /api/projects?workspace_id=<ws>."""
    ws = ws or WORKSPACE
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
    ws = ws or WORKSPACE
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

    `options` is a list of labels, or of (label, color) pairs / {"name","color"} dicts —
    select and multi_select only. Property NAME is the portable key: the engine resolves
    every property by name (see name_to_defs), so a freshly provisioned board with brand
    new ids behaves identically to King's. Never rename a property to "fix" something.
    """
    ws = ws or WORKSPACE
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
#   1. ROUTE EXISTS. POST /api/issues?workspace_id=<King's ws> with an EMPTY body {} —
#      a body that cannot possibly create anything — answered:
#          400 {"error":"title is required"}
#      The control, POST /api/floopdoop?workspace_id=<ws> {}, answered the router's
#      plain-text  404 page not found . A JSON 400 naming a required field is therefore
#      proof of a real, reachable route; no issue was created. (Re-probing with
#      {"title": ""} also returned 400 "title is required" — an empty title is refused,
#      so the probe could not create even by accident.)
#   2. BODY SHAPE. Captured by pointing the official `multica` CLI at a 127.0.0.1 stub
#      via --server-url and logging what it sent. Nothing left the machine:
#          POST /api/issues
#          {"description":"line one","due_date":"2026-08-10","priority":"high",
#           "project_id":"2222…","status":"todo","title":"STUB PROBE"}
#      The CLI carries the workspace in an `X-Workspace-Id` HEADER rather than the query
#      string, so create_issue sends BOTH the header and ?workspace_id= — belt and braces,
#      and consistent with every other create in this module.
#
# ⚠️ PROPERTIES CANNOT BE SET INLINE. `multica issue create --help` has no property flag,
# the CLI sends no property field, and PUT /api/issues/<id> {"properties": …} is the known
# 200-OK-and-silently-discards trap (see set_properties). So a case is opened in TWO steps:
# create, then set_properties. Step two failing is NOT cosmetic — an issue with no `MR
# Phase` is INVISIBLE to case_tick/mer_engine, which select cases by phase. A half-created
# case would sit on the board looking fine while no engine ever touches it. That failure is
# therefore raised as PropertiesNotSetError carrying the orphan's id, never swallowed.


class PropertiesNotSetError(MulticaAPIError):
    """An issue WAS created but its properties were NOT written.

    This is the loud half-created state. `.issue` is the orphan that exists on the board
    with no MR Phase — invisible to every engine module that selects by phase. The caller
    must surface the identifier so a human can finish or delete it; it must never be
    treated as a successful open.
    """

    def __init__(self, message, issue=None, cause=None):
        MulticaAPIError.__init__(self, message, status=None, body=str(cause or ""))
        self.issue = issue or {}
        self.cause = cause


def create_issue(title, description=None, project_id=None, status=None, priority=None,
                 due_date=None, properties=None, ws=None, defs=None):
    """POST /api/issues?workspace_id=<ws> — open a new issue.

    `properties` is an optional {property_name: value} map written in a SECOND call via
    set_properties (properties cannot be created inline — see the block above). If that
    second call fails, this raises PropertiesNotSetError carrying the created issue, so the
    caller cannot mistake a half-created, engine-invisible case for a successful open.

    This function does NOT check for duplicates. Duplicate refusal belongs to the caller
    BEFORE it gets here (dup_guard.guard) — a create function that quietly deduped would
    hide the refusal instead of reporting it. See new_case.py.
    """
    ws = ws or WORKSPACE
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
            "Cannot set properties on it — check the board by hand before retrying, an "
            "issue may exist." % (resp,), status=None)
    if properties:
        try:
            set_properties(issue, properties, ws=ws, defs=defs)
        except Exception as exc:
            raise PropertiesNotSetError(
                "HALF-CREATED CASE. Issue %s (%s) was created but its properties %s could "
                "NOT be written (%s: %s). An issue with no 'MR Phase' is INVISIBLE to the "
                "engine — case_tick and mer_engine select cases by phase and will never "
                "touch it. Fix or delete %s by hand; do not treat this as an open case."
                % (issue.get("identifier") or issue.get("id"), issue.get("id"),
                   sorted(properties), type(exc).__name__, exc,
                   issue.get("identifier") or issue.get("id")),
                issue=issue, cause=exc)
    return issue


if __name__ == "__main__":
    # smoke test: prove the VPS can read the workspace
    print("BASE:", BASE, "| workspace:", WORKSPACE, "| token set:", bool(TOKEN))
    issues = list_issues()
    print("issues:", len(issues))
    for it in issues:
        if it.get("mr"):
            print(" ", it["identifier"], it["mr"])
