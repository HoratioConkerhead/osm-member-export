"""
Export young people's details from every OSM section you can see, into one spreadsheet.

Usage:
    python osm_export.py discover   # list the fields available (no personal data shown)
    python osm_export.py export     # download the ticked fields for all sections to a CSV

Settings live in osm_config.json (created on first run).
Uses only the Python standard library.
"""

import csv
import datetime as dt
import fnmatch
import http.server
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

BASE = "https://www.onlinescoutmanager.co.uk"
HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "osm_config.json"
TOKEN_FILE = HERE / ".osm_token.json"
FIELDS_FILE = HERE / "fields_available.txt"
EXPORT_DIR = HERE / "exports"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPE = "section:member:read"

# The only OSM requests this script is allowed to make: all of them read data, none change it.
READ_ONLY_CALLS = {
    ("/ext/generic/startup/", "getDataPayload"),
    ("/ext/members/contact/grid/", "getMembers"),
    ("/ext/members/contact/", "getListOfMembers"),
    ("/ext/customdata/", "getData"),
}

# Patrol ids OSM uses for the adult leaders' "patrol" in each section.
LEADER_PATROL_IDS = {-2}

DEFAULT_CONFIG = {
    "client_id": "PASTE CLIENT ID HERE",
    "client_secret": "PASTE CLIENT SECRET HERE",
    "skip_section_types": ["adults", "waiting"],
    "include_leaders": False,
    # Field names as listed by "discover". Use * for "everything in this group".
    "fields": [
        "Member > First name",
        "Member > Last name",
        "Member > Date of birth",
        "Member > Age",
        "* > Gender",
        "Primary Contact 1 > *",
        "Primary Contact 2 > *",
    ],
}

# Fields every member record has, outside OSM's custom data groups.
# Each maps to the possible key names OSM uses for it.
CORE_FIELDS = {
    "Member > First name": ("first_name", "firstname"),
    "Member > Last name": ("last_name", "lastname"),
    "Member > Date of birth": ("date_of_birth", "dob"),
    "Member > Age": ("age",),
    "Member > Six / Patrol": ("patrol",),
    "Member > Started section": ("started", "startedsection"),
    "Member > Joined Scouting": ("joined",),
    "Member > OSM member id": ("member_id", "scoutid"),
}


class OSMError(Exception):
    pass


# --------------------------------------------------------------------------- config


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        raise OSMError(
            f"Created {CONFIG_FILE.name}. Open it, paste in your OSM client id and secret, "
            "then run this again."
        )
    config = {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
    if "PASTE" in config["client_id"] or "PASTE" in config["client_secret"]:
        raise OSMError(f"Please put your OSM client id and secret into {CONFIG_FILE.name}.")
    return config


# --------------------------------------------------------------------------- auth


def _token_request(form):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(f"{BASE}/oauth/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            token = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return None, e.read().decode(errors="replace")[:300]
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    return token, None


def _browser_login(config):
    """Authorization-code login: opens OSM in the browser and catches the redirect."""
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Logged in to OSM. You can close this tab.</h2>")

        def log_message(self, *args):
            pass

    url = f"{BASE}/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
    })
    print("Opening OSM in your browser to log in...")
    webbrowser.open(url)
    with http.server.HTTPServer(("localhost", 8765), Handler) as server:
        server.timeout = 300
        server.handle_request()

    if result.get("state") != state or "code" not in result:
        raise OSMError(f"Browser login did not complete: {result.get('error', 'no code returned')}")
    token, err = _token_request({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    })
    if not token:
        raise OSMError(f"Could not swap login code for a token: {err}")
    return token


def get_token(config):
    if TOKEN_FILE.exists():
        token = json.loads(TOKEN_FILE.read_text())
        if token.get("expires_at", 0) > time.time():
            return token["access_token"]
        if token.get("refresh_token"):
            token, _ = _token_request({
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
            })
            if token:
                TOKEN_FILE.write_text(json.dumps(token))
                return token["access_token"]

    # Personal apps can usually log straight in with their id and secret.
    token, err = _token_request({
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "scope": SCOPE,
    })
    if not token:
        print(f"Direct login not accepted ({err.strip()[:120]}); trying browser login.")
        token = _browser_login(config)
    TOKEN_FILE.write_text(json.dumps(token))
    return token["access_token"]


# --------------------------------------------------------------------------- API calls


class OSM:
    def __init__(self, access_token):
        self.access_token = access_token

    def call(self, path, params=None, form=None):
        if (path, (params or {}).get("action")) not in READ_ONLY_CALLS:
            raise OSMError(f"Refusing {path}: not on the read-only list.")
        url = f"{BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        for attempt in range(2):
            req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
            req.add_header("Authorization", f"Bearer {self.access_token}")
            req.add_header("User-Agent", "ScoutTools-osm_export/1.0")
            if data:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    headers, body = resp.headers, resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if e.headers.get("X-Blocked"):
                    raise OSMError("OSM has BLOCKED this app. Stop and contact OSM support.")
                if e.code == 429 and attempt == 0:
                    wait = int(e.headers.get("Retry-After", 60))
                    print(f"  OSM rate limit reached; waiting {wait}s...")
                    time.sleep(wait)
                    continue
                raise OSMError(f"OSM returned HTTP {e.code} for {path}")
            break

        if headers.get("X-Blocked"):
            raise OSMError("OSM has BLOCKED this app. Stop and contact OSM support.")
        if headers.get("X-Deprecated"):
            print(f"  Note: OSM says {path} is deprecated ({headers['X-Deprecated']}).")
        remaining = headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < 10:
            wait = int(headers.get("X-RateLimit-Reset", 60)) + 1
            print(f"  Close to OSM's hourly limit; pausing {wait}s...")
            time.sleep(wait)
        time.sleep(0.3)  # be gentle

        if body.startswith("var data_holder"):
            body = body.split("=", 1)[1].strip().rstrip(";")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise OSMError(f"Unexpected (non-JSON) reply from OSM for {path}")

    def sections(self, skip_types):
        """Every section you can see, with its current term."""
        payload = self.call("/ext/generic/startup/", {"action": "getDataPayload"})
        globals_ = (payload.get("data") or {}).get("globals") or {}
        roles, terms = globals_.get("roles"), globals_.get("terms") or {}
        if not roles:
            raise OSMError("Could not read your section list from OSM.")
        today = dt.date.today().isoformat()
        found = []
        for role in roles:
            if role.get("section") in skip_types:
                continue
            sid = str(role["sectionid"])
            section_terms = terms.get(sid) or []
            current = [t for t in section_terms if t["startdate"] <= today <= t["enddate"]]
            term = (current or sorted(section_terms, key=lambda t: t["startdate"]))[-1:] or [None]
            found.append({
                "id": sid,
                "name": f"{role.get('groupname', '')}: {role.get('sectionname', '')}".strip(": "),
                "short_name": role.get("sectionname", sid),
                "type": role.get("section"),
                "term_id": term[0]["termid"] if term[0] else None,
            })
        return found

    def members(self, section, sample_only=False):
        """Return (list of {field: value}, list of field names) for one section."""
        try:
            return self._members_from_grid(section)
        except OSMError as e:
            print(f"  Bulk download not available ({e}); fetching members one by one.")
            return self._members_one_by_one(section, sample_only)

    def _members_from_grid(self, section):
        reply = self.call(
            "/ext/members/contact/grid/",
            {"action": "getMembers"},
            {"section_id": section["id"], "term_id": section["term_id"]},
        )
        data, meta = reply.get("data"), reply.get("meta") or {}
        structure = meta.get("structure") if isinstance(meta, dict) else None
        if not isinstance(data, dict) or "members" not in data or not structure:
            raise OSMError("unexpected reply shape")

        columns = []  # (group_id, column_id, field name)
        for group in structure:
            for col in group.get("columns") or []:
                columns.append((str(group["group_id"]), str(col["column_id"]),
                                f"{group['name']} > {col['label']}"))

        members = data["members"].values() if isinstance(data["members"], dict) else data["members"]
        rows = []
        for m in members:
            row = core_values(m)
            row["_patrol_id"] = m.get("patrol_id", m.get("patrolid"))
            custom = m.get("custom_data") or {}
            for gid, cid, name in columns:
                row[name] = (custom.get(gid) or {}).get(cid, "")
            rows.append(row)
        return rows, list(CORE_FIELDS) + [c[2] for c in columns]

    def _members_one_by_one(self, section, sample_only):
        listing = self.call("/ext/members/contact/", {
            "action": "getListOfMembers", "sort": "lastname",
            "sectionid": section["id"], "termid": section["term_id"], "section": section["type"],
        })
        items = listing.get("items") or []
        rows, names = [], list(CORE_FIELDS)
        for m in items[:1] if sample_only else items:
            row = core_values(m)
            row["_patrol_id"] = m.get("patrolid")
            detail = self.call("/ext/customdata/", {
                "action": "getData", "section_id": section["id"], "associated_id": m["scoutid"],
                "associated_type": "member", "context": "members", "group_order": "section",
            })
            for group in detail.get("data") or []:
                for col in group.get("columns") or []:
                    name = f"{group['name']} > {col['label']}"
                    row[name] = col.get("value") or ""
                    if name not in names:
                        names.append(name)
            rows.append(row)
        return rows, names


def core_values(member):
    row = {}
    for name, keys in CORE_FIELDS.items():
        row[name] = next((member[k] for k in keys if member.get(k) not in (None, "")), "")
    return row


# --------------------------------------------------------------------------- commands


def discover(config):
    osm = OSM(get_token(config))
    sections = osm.sections(config["skip_section_types"])
    print(f"\nSections found ({len(sections)}):")
    for s in sections:
        print(f"  - {s['name']}  [{s['type']}]")

    all_fields = []
    for s in sections:
        if not s["term_id"]:
            continue
        _, names = osm.members(s, sample_only=True)
        all_fields += [n for n in names if n not in all_fields]

    FIELDS_FILE.write_text("\n".join(all_fields) + "\n", encoding="utf-8")
    print(f"\nFields available ({len(all_fields)}), also saved to {FIELDS_FILE.name}:")
    for name in all_fields:
        print(f"  {name}")


def expand_fields(patterns, available):
    """Turn config entries (which may use * wildcards, any case) into real field names."""
    chosen = []
    for pattern in patterns:
        matches = [n for n in available if fnmatch.fnmatchcase(n.lower(), pattern.lower())]
        if not matches:
            print(f"  Warning: no OSM field matches '{pattern}' (see {FIELDS_FILE.name})")
        chosen += [m for m in matches if m not in chosen]
    return chosen


def export(config):
    osm = OSM(get_token(config))
    all_rows, available = [], []
    for s in osm.sections(config["skip_section_types"]):
        if not s["term_id"]:
            print(f"Skipping {s['name']} (no terms set up)")
            continue
        rows, names = osm.members(s)
        available += [n for n in names if n not in available]
        kept = [r for r in rows
                if config["include_leaders"] or r.get("_patrol_id") not in LEADER_PATROL_IDS]
        print(f"{s['name']}: {len(kept)} members")
        all_rows += [{"Section": s["short_name"], **r} for r in kept]

    wanted = expand_fields(config["fields"], available)
    out_rows = [{"Section": r["Section"], **{f: r.get(f, "") for f in wanted}} for r in all_rows]
    out_rows.sort(key=lambda r: (r["Section"], str(r.get("Member > Last name", ""))))
    EXPORT_DIR.mkdir(exist_ok=True)
    out_file = EXPORT_DIR / f"members_{dt.date.today().isoformat()}.csv"
    with out_file.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Section", *wanted])
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"\nSaved {len(out_rows)} members to {out_file}")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("discover", "export"):
        print(__doc__)
        return 1
    try:
        config = load_config()
        (discover if command == "discover" else export)(config)
    except OSMError as e:
        print(f"\nProblem: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
