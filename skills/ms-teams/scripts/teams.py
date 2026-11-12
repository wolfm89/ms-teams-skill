#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Wolfgang Moser
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""ms-teams: read and write Microsoft Teams through the user's Power Platform
Teams connection (no custom Graph app registration or admin consent needed).
Python 3 standard library only.

Auth: tokens come from `az account get-access-token` on every run; nothing is stored.
Connection metadata and team/channel/member caches are stored per tenant under
`~/.cache/ms-teams/<tenantId>/`. No config file is required (an optional watchlist
file at `~/.config/ms-teams/watchlist.json` can be used for `digest`).
Writes (send, reply, edit, delete, react, unreact, create-chat, create-meeting) are
dry-runs unless --send is given.
"""

import argparse
import base64
import concurrent.futures
import contextlib
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-teams"
DEFAULT_WATCHLIST = Path(
    os.environ.get(
        "MS_TEAMS_WATCHLIST",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ms-teams" / "watchlist.json",
    )
).expanduser()
PP_API = "https://api.powerapps.com/providers/Microsoft.PowerApps"
RES_PP = "https://service.powerapps.com/"
RES_FLOW = "https://service.flow.microsoft.com/"
RES_HUB = "https://apihub.azure.com"
GRAPH = "https://graph.microsoft.com"
SELF_CHAT = "48:notes"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DEBUG = bool(os.environ.get("TEAMS_DEBUG"))

_ACTIVE_TENANT: str | None = os.environ.get("MS_TEAMS_TENANT")
_CACHE_DIR: Path = CACHE_ROOT
_REFRESH = False


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


class TeamsError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(f"HTTP {status}: {msg}")
        self.status = status


# ---------------------------------------------------------------- http / auth
_tokens: dict[tuple[str, str], str] = {}


def set_refresh(flag: bool) -> None:
    global _REFRESH
    _REFRESH = flag


def az_bin() -> str:
    return shutil.which("az") or "az"


def set_active_tenant(tenant: str | None) -> None:
    global _ACTIVE_TENANT
    if tenant:
        _ACTIVE_TENANT = tenant.strip()


def az_token(resource: str, tenant: str | None = None) -> str:
    t = tenant if tenant is not None else _ACTIVE_TENANT
    key = (resource, t or "")
    if key in _tokens:
        return _tokens[key]
    cmd = [az_bin(), "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"]
    if t:
        cmd.extend(["--tenant", t])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)  # nosec B603  # noqa: S603
    except FileNotFoundError:
        die("az CLI not found. Install Azure CLI and run `az login --allow-no-subscriptions`.")
    except subprocess.SubprocessError as e:
        die(f"az token request failed: {e}")
    if r.returncode != 0:
        hint = f" --tenant {t}" if t else ""
        die(
            "could not get a token from az: "
            + r.stderr.strip()[:300]
            + f"\nRun `az login{hint} --allow-no-subscriptions` and retry (see `doctor`)."
        )
    _tokens[key] = r.stdout.strip()
    return _tokens[key]


def jwt_claims(token: str) -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    p = parts[1]
    pad = "=" * ((4 - len(p) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(p + pad).decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def current_identity(tenant: str | None = None) -> dict[str, Any]:
    tok = az_token(RES_HUB, tenant=tenant)
    c = jwt_claims(tok)
    tid = str(c.get("tid") or "")
    uid = str(c.get("oid") or "")
    upn = str(c.get("upn") or c.get("unique_name") or c.get("preferred_username") or "")
    name = str(c.get("name") or upn)
    if not tid or not uid:
        die("could not read tenant/user identity from az access token. Run `az login --allow-no-subscriptions`.")
    return {
        "tenantId": tid,
        "environment": f"Default-{tid}",
        "user": {"id": uid, "displayName": name, "upn": upn},
    }


def _retry_after_seconds(err_headers: Any, raw: bytes | None, attempt: int) -> int:
    if err_headers:
        ra = err_headers.get("Retry-After")
        if ra and str(ra).strip().isdigit():
            return min(int(str(ra).strip()) + 1, 30)
    m = re.search(r"in (\d+) seconds", (raw or b"").decode("utf-8", "replace"))
    return min(int(m.group(1)) + 1 if m else 2 * (attempt + 1), 30)


def http(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 60,
) -> tuple[int, bytes]:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise TeamsError(400, f"refusing non-HTTPS URL: {url}")
    data = body if body is not None else (b"" if method in ("POST", "PUT", "PATCH") else None)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})  # noqa: S310
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310  # noqa: S310
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code == 429 and attempt < 4:
                time.sleep(_retry_after_seconds(getattr(e, "headers", None), raw, attempt))
                continue
            if e.code in (502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return e.code, raw
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            reason = getattr(e, "reason", None) or str(e)
            raise TeamsError(503, f"network error: {reason}") from e
    raise TeamsError(503, "request failed after retries")


def jparse(raw: bytes | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")[:500]}


def err_msg(j: Any) -> str:
    e = j.get("error") if isinstance(j, dict) else None
    if isinstance(e, dict):
        return str(e.get("message") or e.get("code"))
    if isinstance(j, dict):
        return str(j.get("message") or j)[:300]
    return str(j)[:300]


class Conn:
    """Teams connector runtime: dedicated operations (`op`) and Graph passthrough (`graph`)."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base: str = cfg["runtimeBase"]

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": "Bearer " + az_token(RES_HUB), "User-Agent": "ms-teams-skill"}
        h.update(extra or {})
        return h

    def op_raw(
        self,
        method: str,
        path: str,
        body: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        extra = dict(extra_headers or {})
        if body is not None:
            extra.setdefault("Content-Type", "application/json")
        h = self._headers(extra)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        if DEBUG:
            print(f"[op] {method} {path}", file=sys.stderr)
        status, raw = http(method, self.base + path, h, data)
        if status >= 400:
            raise TeamsError(status, err_msg(jparse(raw)))
        return raw

    def op(
        self,
        method: str,
        path: str,
        body: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        return jparse(self.op_raw(method, path, body=body, extra_headers=extra_headers))

    def graph(
        self,
        method: str,
        uri: str,
        body: Any = None,
        custom_headers: list[str] | None = None,
    ) -> tuple[int, Any]:
        """Graph request via the connector's HttpRequest operation. Returns (status, json)."""
        extra = {"Uri": uri, "Method": method}
        for idx, ch in enumerate((custom_headers or [])[:5], 1):
            extra[f"CustomHeader{idx}"] = ch
        data = None
        if method != "GET":
            extra["ContentType"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b"{}"
        if DEBUG:
            print(f"[graph] {method} {uri}", file=sys.stderr)
        status, raw = http("POST", self.base + "/httprequest", self._headers(extra), data if data is not None else b"")
        return status, jparse(raw)

    def graph_json(
        self,
        method: str,
        path: str,
        body: Any = None,
        version: str = "v1.0",
        custom_headers: list[str] | None = None,
    ) -> Any:
        uri = path if path.startswith("http") else f"{GRAPH}/{version}/{path.lstrip('/')}"
        status, j = self.graph(method, uri, body, custom_headers=custom_headers)
        if status >= 400:
            raise TeamsError(status, err_msg(j))
        return j

    def pages(self, path: str, max_pages: int = 20, version: str = "v1.0") -> Any:
        uri = path
        for _ in range(max_pages):
            j = self.graph_json("GET", uri, version=version)
            yield j
            uri = j.get("@odata.nextLink")
            if not uri:
                break


# ---------------------------------------------------------------- cache & session discovery
def write_private_bytes(path: str | Path, data: bytes) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        with contextlib.suppress(OSError):
            fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return p


def tenant_cache_dir(tenant_id: str | None = None) -> Path:
    if tenant_id:
        return CACHE_ROOT / tenant_id
    return _CACHE_DIR


def cached(name: str, ttl: int, fn: Any) -> Any:
    d = tenant_cache_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = d / name
    if not _REFRESH and p.exists() and time.time() - p.stat().st_mtime < ttl:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    v = fn()
    write_private_bytes(p, json.dumps(v, ensure_ascii=False).encode("utf-8"))
    return v


def pp_get(path, params=""):
    h = {"Authorization": "Bearer " + az_token(RES_PP)}
    s, raw = http("GET", f"{PP_API}/{path}?api-version=2016-11-01{params}", h)
    return s, jparse(raw)


def pp_post(path, body, params=""):
    h = {"Authorization": "Bearer " + az_token(RES_PP), "Content-Type": "application/json"}
    s, raw = http("POST", f"{PP_API}/{path}?api-version=2016-11-01{params}", h, json.dumps(body).encode())
    return s, jparse(raw)


def pp_put(path, body, params=""):
    h = {"Authorization": "Bearer " + az_token(RES_FLOW), "Content-Type": "application/json"}
    s, raw = http("PUT", f"{PP_API}/{path}?api-version=2016-11-01{params}", h, json.dumps(body).encode())
    if s >= 400:
        h["Authorization"] = "Bearer " + az_token(RES_PP)
        s, raw = http("PUT", f"{PP_API}/{path}?api-version=2016-11-01{params}", h, json.dumps(body).encode())
    return s, jparse(raw)


def conn_status(c):
    sts = (c.get("properties") or {}).get("statuses", [])
    return sts[0].get("status", "?") if sts else "?"


def runtime_base(c):
    for tl in (c.get("properties") or {}).get("testLinks", []):
        m = re.match(r"^(.*?/connections/[0-9a-fA-F]+)", tl.get("requestUri", ""))
        if m:
            return m.group(1)
    return None


def _owned_by(conn_obj, user):
    props = conn_obj.get("properties") or {}
    owner = props.get("createdBy") or {}
    if owner.get("id"):
        return str(owner["id"]).lower() == str(user["id"]).lower()
    upn = str(user.get("upn") or "").lower()
    return bool(upn) and upn in (
        str(owner.get("userPrincipalName") or "").lower(),
        str(props.get("displayName") or "").lower(),
    )


def find_connection(env, user):
    """The signed-in user's own Teams connection. Connections shared by others are ignored on purpose:
    messages are sent as the connection's owner."""
    flt = "&$filter=environment%20eq%20%27" + env + "%27"
    s, j = pp_get("apis/shared_teams/connections", flt)
    if s >= 400:
        die(f"could not list Power Platform connections: {err_msg(j)}")
    mine = [c for c in j.get("value", []) if _owned_by(c, user)]
    mine.sort(key=lambda c: conn_status(c) == "Connected", reverse=True)
    return mine[0] if mine else None


def create_connection(env):
    cid = uuid.uuid4().hex
    flt = "&$filter=environment%20eq%20%27" + env + "%27"
    body = {"properties": {"environment": {"id": f"/providers/Microsoft.PowerApps/environments/{env}", "name": env}}}
    s, j = pp_put(f"apis/shared_teams/connections/{cid}", body, flt)
    if s >= 400:
        return None
    return j if j.get("name") else {"name": cid, "properties": j.get("properties") or {}}


def load_watchlist(path: str | Path | None = None, tenant_id: str | None = None) -> dict[str, Any]:
    p = Path(path).expanduser() if path else DEFAULT_WATCHLIST
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        die(f"invalid JSON in watchlist file {p}: {e}", 2)
    if isinstance(data, list):
        return {"watch": data}
    if not isinstance(data, dict):
        die(f"watchlist file {p} must contain a JSON object or array", 2)
    if tenant_id and isinstance(data.get("tenants"), dict):
        t_cfg = data["tenants"].get(tenant_id) or (data["tenants"].get(_ACTIVE_TENANT) if _ACTIVE_TENANT else None)
        if isinstance(t_cfg, list):
            return {"watch": t_cfg}
        if isinstance(t_cfg, dict):
            return t_cfg
        if not data.get("watch"):
            print(
                f"warning: {p} has a 'tenants' block but no entry for tenant {tenant_id}; ignoring it.",
                file=sys.stderr,
            )
            return {}
    return data


def connect() -> tuple[dict[str, Any], Conn]:
    global _CACHE_DIR, _custom_emojis_cache
    ident = current_identity()
    tid = ident["tenantId"]
    _CACHE_DIR = CACHE_ROOT / tid
    _custom_emojis_cache = None
    session_file = _CACHE_DIR / "session.json"
    cfg: dict[str, Any] = dict(ident)
    if session_file.exists():
        try:
            cached_sess = json.loads(session_file.read_text(encoding="utf-8"))
            if cached_sess.get("tenantId") == tid and cached_sess.get("runtimeBase"):
                cfg.update(cached_sess)
        except ValueError:
            pass
    if not cfg.get("runtimeBase"):
        c = find_connection(ident["environment"], ident["user"])
        if not c or conn_status(c) != "Connected":
            die("Teams connection is not set up or not Connected for this tenant. Run: teams.py onboard")
        base = runtime_base(c)
        if not base:
            die("could not derive runtime URL from the Teams connection. Run: teams.py onboard")
        cfg["connectionId"] = c["name"]
        cfg["runtimeBase"] = base
        write_private_bytes(session_file, json.dumps(cfg, indent=2, ensure_ascii=False).encode("utf-8"))
    wl = load_watchlist(tenant_id=tid)
    if wl.get("watch"):
        cfg["watch"] = wl["watch"]
    return cfg, Conn(cfg)


# ---------------------------------------------------------------- text helpers
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^\w\s@.\-/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def pick(items, query, key, what):
    q = norm(query)
    if not q:
        die(f"no {what} matching '{query}'", 2)
    exact = [i for i in items if norm(key(i)) == q]
    if len(exact) == 1:
        return exact[0]
    cands = [i for i in items if q in norm(key(i))]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        die(f"no {what} matching '{query}'", 2)
    names = "\n  ".join(key(i) for i in cands[:15])
    die(f"'{query}' matches several {what}s, be more specific:\n  {names}", 2)


def find_one(items: list[Any], query: str, key: Callable[[Any], str]) -> tuple[Any, str | None]:
    """Exact (normalized) match first, then a unique substring match. Returns (item, error)."""
    q = norm(query)
    if not q:
        return None, f"no match for '{query}'"
    exact = [i for i in items if norm(key(i)) == q]
    if len(exact) == 1:
        return exact[0], None
    cands = [i for i in items if q in norm(key(i))]
    if len(cands) == 1:
        return cands[0], None
    return None, (f"no match for '{query}'" if not cands else f"'{query}' is ambiguous ({len(cands)} matches)")


def pick_by_id_or_name(items, query, name_key, what):
    by_id = [i for i in items if i.get("id") == query]
    if len(by_id) == 1:
        return by_id[0]
    return pick(items, query, name_key, what)


def html_to_text(s):
    s = s or ""
    s = re.sub(r"<br\s*/?>|</p>|</div>|</h\d>|</li>|</tr>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<at[^>]*>(.*?)</at>", r"@\1", s, flags=re.DOTALL)
    s = re.sub(r'<customemoji[^>]*\balt="([^"]+)"[^>]*>.*?</customemoji>', r":\1:", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'<emoji[^>]*\balt="([^"]+)"[^>]*>.*?</emoji>', r"\1", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<attachment[^>]*></attachment>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def html_links(s):
    return list(dict.fromkeys(re.findall(r'href="([^"]+)"', s or "")))


def _six_digit_fraction(m: re.Match[str]) -> str:
    return "." + m.group(1).ljust(6, "0")[:6]


def parse_ts(s):
    if not s:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    # Graph sends 3 to 7 fractional digits; datetime.fromisoformat() before Python 3.11 needs exactly 3 or 6.
    s = re.sub(r"\.(\d+)", _six_digit_fraction, s)
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def msg_ts(m, field="createdDateTime"):
    return parse_ts(m.get(field) or m.get("createdDateTime") or m.get("lastModifiedDateTime"))


def parse_since(s):
    m = re.fullmatch(r"(\d+)([hdw])", s.strip())
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"h": dt.timedelta(hours=n), "d": dt.timedelta(days=n), "w": dt.timedelta(weeks=n)}[u]
        return dt.datetime.now(dt.timezone.utc) - delta
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        die(f"bad --since '{s}' (use 7d, 48h, 2w or 2026-10-06)", 2)
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def local(ts):
    return parse_ts(ts).astimezone().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- teams / channels / users
def joined_teams(conn):
    return cached(
        "teams.json",
        3600,
        lambda: [
            {"id": t["id"], "name": t["displayName"], "description": t.get("description")}
            for t in conn.op("GET", "/beta/me/joinedTeams").get("value", [])
        ],
    )


def associated_teams(conn):
    return cached(
        "associated-teams.json",
        3600,
        lambda: [
            {"id": t["id"], "name": t["displayName"]}
            for t in conn.op("GET", "/v1.0/me/teamwork/associatedTeams").get("value", [])
        ],
    )


def resolve_team(conn, ref):
    if GUID.match(ref):
        return ref, next((t["name"] for t in joined_teams(conn) if t["id"] == ref), ref)
    t = pick(joined_teams(conn), ref, lambda x: x["name"], "team")
    return t["id"], t["name"]


def team_channels(conn, team_id):
    return cached(
        f"channels-{team_id}.json",
        86400,
        lambda: [
            {"id": c["id"], "name": c["displayName"], "type": c.get("membershipType"), "webUrl": c.get("webUrl")}
            for c in conn.op("GET", f"/beta/teams/{team_id}/allChannels").get("value", [])
        ],
    )


def resolve_channel(conn, ref, default_team=None):
    """ref: 'Team/Channel' fragments, '<teamGuid>/<channelId>', or a bare channel
    name when default_team is given."""
    if "/" in ref:
        tref, cref = ref.split("/", 1)
        if GUID.match(tref) and cref.startswith("19:"):
            return {"teamId": tref, "channelId": cref, "name": cref}
        team_id, team_name = resolve_team(conn, tref)
    elif default_team:
        team_id, team_name, cref = default_team[0], default_team[1], ref
    else:
        die(f"channel target '{ref}' must be 'Team/Channel' (e.g. 'channel:Engineering/General').", 2)
    ch = pick(team_channels(conn, team_id), cref, lambda c: c["name"], "channel")
    return {"teamId": team_id, "channelId": ch["id"], "name": ch["name"], "team": team_name}


def team_members(conn, team_id):
    def fetch():
        j = conn.op("GET", f"/v1.0/teams/{team_id}/members")
        return [
            {
                "userId": m.get("userId"),
                "name": m.get("displayName"),
                "email": m.get("email"),
                "roles": m.get("roles") or [],
            }
            for m in j.get("value", [])
            if m.get("userId")
        ]

    return cached(f"members-{team_id}.json", 86400, fetch)


def known_users(conn, cfg, extra_team=None):
    teams = list((cfg or {}).get("homeTeams") or [])
    if extra_team and extra_team not in teams:
        teams.append(extra_team)
    users = {}
    d = tenant_cache_dir()
    if d.exists():
        for p in d.glob("members-*.json"):
            try:
                for m in json.loads(p.read_text(encoding="utf-8")):
                    if m.get("userId"):
                        users[m["userId"]] = m
            except (OSError, ValueError):
                pass
    for t in teams:
        for m in team_members(conn, t):
            users[m["userId"]] = m
    return users


def tenant_users(query):
    headers = {"Authorization": "Bearer " + az_token(GRAPH)}
    if "@" in query:
        email = query.replace("'", "''")
        flt = urllib.parse.quote(f"mail eq '{email}' or userPrincipalName eq '{email}'", safe=":@.")
        url = f"{GRAPH}/v1.0/users?$filter={flt}&$select=id,displayName,mail,userPrincipalName&$top=10"
    else:
        clean_q = query.replace('"', "")
        q = urllib.parse.quote(f'"displayName:{clean_q}"', safe='":')
        url = f"{GRAPH}/v1.0/users?$search={q}&$select=id,displayName,mail,userPrincipalName&$top=10"
        headers["ConsistencyLevel"] = "eventual"
    status, raw = http("GET", url, headers)
    if status >= 400:
        return []
    return [
        {
            "userId": u["id"],
            "name": u["displayName"],
            "email": u.get("mail") or u.get("userPrincipalName"),
            "source": "tenant",
        }
        for u in jparse(raw).get("value", [])
    ]


def find_users(conn, cfg, query, team_id=None, tenant=True):
    q_raw = (query or "").strip()
    q = norm(q_raw)
    if not q and "@" not in q_raw:
        return []
    users = list(known_users(conn, cfg, team_id).values())
    if "@" in q_raw:
        hit = [u for u in users if (u.get("email") or "").lower() == q_raw.lower()]
        if hit:
            return hit
    if q:
        exact = [u for u in users if norm(u["name"]) == q]
        if len(exact) == 1:
            return exact
    toks = [t for t in re.split(r"[\s,.]+", q) if t]
    hits = [u for u in users if toks and all(t in norm((u["name"] or "") + " " + (u.get("email") or "")) for t in toks)]
    if not hits and tenant and q:
        hits = tenant_users(q_raw)
    return hits


def resolve_single_user(conn, cfg, query, team_id=None):
    if GUID.match(query):
        return {"userId": query, "name": query, "email": None}
    hits = find_users(conn, cfg, query, team_id)
    if not hits:
        die(f"no user matches '{query}'", 2)
    if len(hits) > 1:
        lst = "\n  ".join(f"{u['name']} <{u.get('email')}>" for u in hits[:10])
        die(f"'{query}' is ambiguous, use the full name or email:\n  {lst}", 2)
    return hits[0]


# ---------------------------------------------------------------- targets
def parse_target(conn, _cfg, t):
    if t == "self":
        return {"kind": "chat", "chatId": SELF_CHAT, "label": "self-chat", "msgs": f"me/chats/{SELF_CHAT}/messages"}
    if t.startswith("chat:"):
        cid = t[5:].strip()
        if not cid:
            die("chat target cannot be empty (use 'self' or 'chat:<chatId>')", 2)
        return {"kind": "chat", "chatId": cid, "label": f"chat {cid}", "msgs": f"me/chats/{cid}/messages"}
    if t.startswith("channel:"):
        cref = t[8:].strip()
        if not cref:
            die("channel target cannot be empty (use 'channel:<Team/Channel>')", 2)
        ch = resolve_channel(conn, cref)
        label = f"{ch.get('team', ch['teamId'])} / {ch['name']}"
        return {
            "kind": "channel",
            **ch,
            "label": label,
            "msgs": f"teams/{ch['teamId']}/channels/{ch['channelId']}/messages",
        }
    die("target must be 'self', 'chat:<chatId>' or 'channel:<Team/Channel>'", 2)


def parse_channel_target(conn, cfg, t):
    if t == "self" or t.startswith("chat:"):
        die("channel target required (use 'channel:<Team/Channel>')", 2)
    return parse_target(conn, cfg, t if t.startswith("channel:") else f"channel:{t}")


def parse_chat_target(conn, cfg, t):
    if t.startswith("channel:"):
        die("chat target required (use 'self' or 'chat:<chatId>')", 2)
    return parse_target(conn, cfg, t if (t == "self" or t.startswith("chat:")) else f"chat:{t}")


def msg_path(target, message_id, reply_id=None):
    p = f"{target['msgs']}/{message_id}"
    if reply_id:
        if target["kind"] != "channel":
            die("replies are only addressable in channels", 2)
        p += f"/replies/{reply_id}"
    return p


# ---------------------------------------------------------------- mentions / custom emojis / message building
MENTION_RE = re.compile(r"@\[([^\]]+)\]")
SHORTCODE_RE = re.compile(r"(?<![/\w]):([a-zA-Z0-9_-]{2,40}):(?![/\w])")
CUSTOM_EMOJI_TAG_RE = re.compile(
    r'<customemoji[^>]*\bid="([^"]+)"[^>]*\balt="([^"]+)"[^>]*\bsource="([^"]+)"[^>]*>',
    re.IGNORECASE,
)
HOSTED_B64_RE = re.compile(r"/hostedContents/([A-Za-z0-9+/=_-]+)/\$value")
ASYNCGW_HOST_RE = re.compile(r"https://([a-z0-9.-]*asyncgw\.teams\.microsoft\.com)/", re.IGNORECASE)

_custom_emojis_cache: dict[str, dict[str, str]] | None = None


def load_custom_emojis():
    global _custom_emojis_cache
    if _custom_emojis_cache is None:
        p = tenant_cache_dir() / "custom-emojis.json"
        try:
            _custom_emojis_cache = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            _custom_emojis_cache = {}
    return dict(_custom_emojis_cache or {})


def save_custom_emojis(emojis):
    global _custom_emojis_cache
    _custom_emojis_cache = dict(emojis)
    d = tenant_cache_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private_bytes(
        d / "custom-emojis.json", json.dumps(_custom_emojis_cache, indent=2, ensure_ascii=False).encode("utf-8")
    )


def _learned_host(emojis):
    """Most common asyncgw host among emojis whose host is known (hosts are per tenant region)."""
    hosts = [e["host"] for e in emojis.values() if e.get("host")]
    return max(set(hosts), key=hosts.count) if hosts else None


def _decode_hosted_b64(url):
    m = HOSTED_B64_RE.search(url or "")
    if not m:
        return None, None
    b64 = m.group(1)
    pad = "=" * ((4 - len(b64) % 4) % 4)
    try:
        dec = base64.b64decode(b64 + pad).decode("utf-8", "replace")
    except Exception:
        return None, None
    obj_id = None
    id_m = re.search(r"id=([^,]+)", dec)
    if id_m and id_m.group(1):
        obj_id = id_m.group(1)
    else:
        obj_m = re.search(r"/objects/([^/]+)/views/", dec)
        if obj_m:
            obj_id = obj_m.group(1)
    host_m = ASYNCGW_HOST_RE.search(dec)
    return obj_id, (host_m.group(1) if host_m else None)


def harvest_custom_emojis(raw_msg):
    """Learn custom emojis (name -> object id + asyncgw host) from a message body and its reactions."""
    seen: dict[str, dict[str, Any]] = {}
    body = ((raw_msg.get("body") or {}).get("content")) or ""
    for b64_id, alt_name, src in CUSTOM_EMOJI_TAG_RE.findall(body):
        obj_id, host = _decode_hosted_b64(src)
        if not obj_id:
            try:
                pad = "=" * ((4 - len(b64_id) % 4) % 4)
                obj_id = base64.b64decode(b64_id + pad).decode("utf-8", "replace")
            except Exception:
                obj_id = None
        if obj_id and alt_name:
            seen[alt_name] = {"objectId": obj_id, "host": host}
    for r in raw_msg.get("reactions") or []:
        if r.get("reactionType") == "custom" and r.get("displayName"):
            obj_id, host = _decode_hosted_b64(r.get("reactionContentUrl") or "")
            if obj_id:
                seen[r["displayName"]] = {"objectId": obj_id, "host": host}
    if not seen:
        return {}
    cur = load_custom_emojis()
    fallback = _learned_host({**cur, **seen})
    found = {
        name: {"objectId": info["objectId"], "host": info["host"] or fallback}
        for name, info in seen.items()
        if info["host"] or fallback
    }
    if any(cur.get(k) != v for k, v in found.items()):
        cur.update(found)
        save_custom_emojis(cur)
    return found


def expand_custom_emojis(html_in):
    emojis = load_custom_emojis()
    if not emojis or ":" not in (html_in or ""):
        return html_in

    def repl_shortcode(m):
        name = m.group(1)
        info = emojis.get(name)
        if not info or not info.get("host"):
            return m.group(0)
        obj_id = info["objectId"]
        b64_id = base64.b64encode(obj_id.encode("utf-8")).decode("ascii")
        esc_name = html.escape(name, quote=True)
        src = f"https://{info['host']}/v1/objects/{object_id_clean(obj_id)}/views/imgt2_anim"
        return f'<span><customemoji id="{b64_id}" alt="{esc_name}" source="{src}"></customemoji></span>'

    parts = re.split(
        r"(<(?:code|pre)\b[^>]*>.*?</(?:code|pre)>|<[^>]+>)",
        html_in,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for i in range(0, len(parts), 2):
        parts[i] = SHORTCODE_RE.sub(repl_shortcode, parts[i])
    return "".join(parts)


def object_id_clean(obj_id):
    return urllib.parse.quote(obj_id or "", safe="-_.")


def resolve_mention(conn, cfg, spec, target):
    s = spec.strip()
    low = s.lower()
    if low.startswith("tag:"):
        if target["kind"] != "channel":
            die("tag mentions only work in channel posts", 2)
        tags = conn.op("GET", f"/v1.0/teams/{target['teamId']}/tags").get("value", [])
        t = pick_by_id_or_name(tags, s[4:], lambda x: x["displayName"], "tag")
        return t["displayName"], {"tag": {"id": t["id"], "displayName": t["displayName"]}}
    if low.startswith("channel:"):
        if target["kind"] != "channel":
            die("channel mentions only work in channel posts", 2)
        ch = resolve_channel(conn, s[8:], default_team=(target["teamId"], target.get("team", target["teamId"])))
        return ch["name"], {
            "conversation": {
                "id": ch["channelId"],
                "displayName": ch["name"],
                "conversationIdentityType": "channel",
            }
        }
    if low == "team":
        if target["kind"] != "channel":
            die("team mentions only work in channel posts", 2)
        name = target.get("team", "team")
        return name, {
            "conversation": {
                "id": target["teamId"],
                "displayName": name,
                "conversationIdentityType": "team",
            }
        }
    q = s[5:] if low.startswith("user:") else s
    u = resolve_single_user(conn, cfg, q, target.get("teamId"))
    return u["name"], {"user": {"id": u["userId"], "displayName": u["name"], "userIdentityType": "aadUser"}}


def load_card_arg(args):
    raw = None
    if getattr(args, "card_file", None):
        raw = sys.stdin.read() if args.card_file == "-" else Path(args.card_file).read_text(encoding="utf-8")
    elif getattr(args, "card", None):
        raw = args.card
    if not raw:
        return None
    try:
        card = json.loads(raw)
    except ValueError as e:
        die(f"invalid Adaptive Card JSON: {e}", 2)
    if not isinstance(card, dict) or card.get("type") != "AdaptiveCard":
        die('Adaptive Card JSON must be an object with "type": "AdaptiveCard"', 2)
    return card


def get_body_arg(args, allow_empty=False):
    if getattr(args, "html_file", None):
        return (sys.stdin.read() if args.html_file == "-" else Path(args.html_file).read_text(encoding="utf-8")), True
    if getattr(args, "html", None):
        return args.html, True
    if getattr(args, "text", None):
        return "<p>" + html.escape(args.text).replace("\n", "<br>") + "</p>", False
    if allow_empty:
        return "", False
    die("provide --html, --html-file (use - for stdin), --text, or --card/--card-file", 2)


def build_message(conn, cfg, html_in, target, subject=None, importance=None, card=None, parse_mentions=True):
    mentions: list[dict[str, Any]] = []
    keyed: dict[str, int] = {}

    def repl(m):
        name, mentioned = resolve_mention(conn, cfg, m.group(1), target)
        key = json.dumps(mentioned, sort_keys=True)
        if key not in keyed:
            keyed[key] = len(mentions)
            mentions.append({"id": keyed[key], "mentionText": str(name), "mentioned": mentioned})
        return f'<at id="{keyed[key]}">{html.escape(str(name))}</at>'

    content = MENTION_RE.sub(repl, html_in or "") if parse_mentions else (html_in or "")
    if parse_mentions:
        content = expand_custom_emojis(content)
    attachments = []
    if card is not None:
        att_id = str(uuid.uuid4())
        marker = f'<attachment id="{att_id}"></attachment>'
        if marker not in content:
            content = (content + "<br>" + marker) if content else marker
        attachments.append(
            {
                "id": att_id,
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": json.dumps(card, ensure_ascii=False),
            }
        )
    msg: dict[str, Any] = {"body": {"contentType": "html", "content": content}}
    if subject:
        msg["subject"] = subject
    if importance and importance != "normal":
        msg["importance"] = importance
    if mentions:
        msg["mentions"] = mentions
    if attachments:
        msg["attachments"] = attachments
    return msg


def mention_kind(mentioned):
    for k in ("user", "tag", "conversation", "application"):
        if mentioned.get(k):
            if k == "conversation":
                return mentioned[k].get("conversationIdentityType") or "conversation"
            return k
    return "?"


def preview(label, action, msg, extra=None):
    out = {
        "action": action,
        "target": label,
        "text": html_to_text(msg["body"]["content"]),
        "mentions": [{"type": mention_kind(m["mentioned"]), "name": m["mentionText"]} for m in msg.get("mentions", [])],
    }
    if msg.get("subject"):
        out["subject"] = msg["subject"]
    if msg.get("importance"):
        out["importance"] = msg["importance"]
    if msg.get("attachments"):
        out["attachments"] = [{"id": a["id"], "contentType": a["contentType"]} for a in msg["attachments"]]
    out.update(extra or {})
    out["html"] = msg["body"]["content"]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print("\nDRY RUN - nothing was sent. Re-run with --send after confirmation.", file=sys.stderr)


def verify_mentions(sent, resp):
    want = {m["id"]: m["mentionText"] for m in sent.get("mentions", [])}
    got = {int(m["id"]): m for m in resp.get("mentions", []) if m.get("id") is not None}
    missing = [name for i, name in want.items() if i not in got]
    return [want[i] for i in want if i in got], missing


def report_sent(resp, sent, undo_hint):
    ok, missing = verify_mentions(sent, resp)
    out = {
        "id": resp.get("id"),
        "created": resp.get("createdDateTime"),
        "webUrl": resp.get("webUrl"),
        "mentionsResolved": ok,
        "mentionsMissing": missing,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if missing:
        print(f"\nWARNING: these mentions were NOT created: {missing}. Undo with:\n  {undo_hint}", file=sys.stderr)


# ---------------------------------------------------------------- message normalisation
def reaction_groups(reactions, names):
    groups: dict[tuple[Any, Any], list[str]] = {}
    for r in reactions or []:
        key = (r.get("reactionType"), r.get("displayName"))
        uid = ((r.get("user") or {}).get("user") or {}).get("id")
        groups.setdefault(key, []).append(names.get(uid, {}).get("name") or (uid or "?")[:8])
    return [{"emoji": k[0], "name": k[1], "by": v} for k, v in groups.items()]


def norm_attachments(attachments):
    out = []
    for a in attachments or []:
        item = {"id": a.get("id"), "contentType": a.get("contentType")}
        if a.get("name"):
            item["name"] = a["name"]
        if a.get("contentUrl"):
            item["contentUrl"] = a["contentUrl"]
        if a.get("contentType") == "application/vnd.microsoft.card.adaptive" and a.get("content"):
            try:
                c = json.loads(a["content"]) if isinstance(a["content"], str) else a["content"]
                texts = [str(b["text"]) for b in (c.get("body") or []) if isinstance(b, dict) and b.get("text")]
                if texts:
                    item["cardText"] = " | ".join(texts[:3])[:300]
            except (ValueError, TypeError, AttributeError):
                pass
        out.append(item)
    return out


def norm_msg(m, names, me_id):
    harvest_custom_emojis(m)
    frm = m.get("from") or {}
    u = frm.get("user") or {}
    author = (
        u.get("displayName")
        or (names.get(u.get("id"), {}).get("name"))
        or (frm.get("application") or {}).get("displayName")
    )
    mentions = []
    for x in m.get("mentions", []) or []:
        mm = x.get("mentioned") or {}
        ident = mm.get("user") or mm.get("tag") or mm.get("conversation") or mm.get("application") or {}
        mentions.append({"type": mention_kind(mm), "name": x.get("mentionText"), "id": ident.get("id")})
    reacts = reaction_groups(m.get("reactions"), names)
    atts = norm_attachments(m.get("attachments"))
    body = (m.get("body") or {}).get("content", "")
    text = html_to_text(body)
    if not text and atts:
        labels = [a.get("name") or a.get("cardText") or a.get("contentType") or "attachment" for a in atts]
        text = "[attachment: " + ", ".join(labels) + "]"
    out = {
        "id": m.get("id"),
        "created": m.get("createdDateTime"),
        "modified": m.get("lastModifiedDateTime"),
        "author": author,
        "authorId": u.get("id"),
        "byMe": bool(me_id and u.get("id") == me_id),
        "subject": m.get("subject"),
        "importance": m.get("importance"),
        "text": text,
        "links": html_links(body),
        "mentions": mentions,
        "mentionsMe": any(x["type"] == "user" and x["id"] == me_id for x in mentions),
        "reactions": reacts,
        "reactionNames": sorted(
            {r["name"] for r in reacts if r["name"]}
            | {r["emoji"] for r in reacts if r["emoji"] and r["emoji"] != "custom"}
        ),
        "reactionsByMe": sorted(
            {
                (r.get("displayName") or r.get("reactionType"))
                for r in (m.get("reactions") or [])
                if me_id and ((r.get("user") or {}).get("user") or {}).get("id") == me_id
            }
        ),
        "webUrl": m.get("webUrl"),
    }
    if atts:
        out["attachments"] = atts
    reps = m.get("replies")
    if reps is not None:
        out["replies"] = [norm_msg(r, names, me_id) for r in sorted(reps, key=lambda r: r.get("createdDateTime") or "")]
        out["replyCount"] = len(out["replies"])
    return out


def fmt_msg(m, indent=""):
    imp = f"  [{m['importance']}]" if m.get("importance") and m["importance"] != "normal" else ""
    lines = [
        f"{indent}[{local(m['created'])}] {m['author'] or '?'}{imp}  id={m['id']}"
        + (f"  replies={m['replyCount']}" if m.get("replyCount") else "")
    ]
    if m.get("subject"):
        lines.append(f"{indent}  subject: {m['subject']}")
    lines.extend(f"{indent}  {ln}" for ln in (m["text"] or "").split("\n") if ln)
    if m["links"]:
        lines.append(f"{indent}  links: " + " ".join(m["links"]))
    if m.get("attachments"):
        att_s = ", ".join(
            a.get("name") or a.get("cardText") or a.get("contentType") or "attachment" for a in m["attachments"]
        )
        lines.append(f"{indent}  attachments: {att_s}")
    if m["mentions"]:
        lines.append(f"{indent}  mentions: " + ", ".join(f"@{x['name']} ({x['type']})" for x in m["mentions"]))
    if m["reactions"]:
        lines.append(
            f"{indent}  reactions: "
            + "; ".join(
                f"{r['emoji']} {r['name'] or ''} by {', '.join(r['by'])}".replace("custom  ", "custom ")
                for r in m["reactions"]
            )
        )
    for r in m.get("replies", []):
        sub = fmt_msg(r, indent + "      ").split("\n")
        sub[0] = indent + "    ↳ " + sub[0].lstrip()
        lines.extend(sub)
    return "\n".join(lines)


# ---------------------------------------------------------------- commands: teams / channels / users / apps
def cmd_teams(args):
    _, conn = connect()
    items = associated_teams(conn) if args.associated else joined_teams(conn)
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return
    for t in items:
        print(f"{t['name']}\t{t['id']}")


def cmd_team_info(args):
    _, conn = connect()
    tid, _ = resolve_team(conn, args.team)
    info = conn.op("GET", f"/beta/teams/{tid}")
    print(json.dumps(info, indent=2, ensure_ascii=False))


def cmd_channels(args):
    _, conn = connect()
    team_id, name = resolve_team(conn, args.team)
    chans = team_channels(conn, team_id)
    if args.filter:
        chans = [c for c in chans if norm(args.filter) in norm(c["name"])]
    if args.json:
        print(json.dumps({"team": name, "teamId": team_id, "channels": chans}, indent=2, ensure_ascii=False))
        return
    print(f"# {name} ({team_id})")
    for c in chans:
        print(f"{c['name']}\t{c['type']}\t{c['id']}")


def cmd_channel_info(args):
    cfg, conn = connect()
    target = parse_channel_target(conn, cfg, args.target)
    info = conn.op("GET", f"/beta/teams/{target['teamId']}/channels/{target['channelId']}")
    print(json.dumps(info, indent=2, ensure_ascii=False))


def cmd_members(args):
    _, conn = connect()
    team_id, _ = resolve_team(conn, args.team)
    members = team_members(conn, team_id)
    if args.json:
        print(json.dumps(members, indent=2, ensure_ascii=False))
        return
    for m in members:
        roles = ",".join(m.get("roles") or []) or "member"
        print(f"{m['name']}\t{m['email']}\t{m['userId']}\t{roles}")


def cmd_channel_members(args):
    cfg, conn = connect()
    target = parse_channel_target(conn, cfg, args.target)
    out: list[dict[str, Any]] = []
    for j in conn.pages(f"teams/{target['teamId']}/channels/{target['channelId']}/members"):
        out.extend(
            {
                "userId": m.get("userId"),
                "name": m.get("displayName"),
                "email": m.get("email"),
                "roles": m.get("roles") or [],
            }
            for m in j.get("value", [])
        )
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    for m in out:
        roles = ",".join(m["roles"]) or "member"
        print(f"{m['name']}\t{m['email']}\t{m['userId']}\t{roles}")


def cmd_tags(args):
    _, conn = connect()
    team_id, _ = resolve_team(conn, args.team)
    tags = conn.op("GET", f"/v1.0/teams/{team_id}/tags").get("value", [])
    if args.json:
        print(json.dumps(tags, indent=2, ensure_ascii=False))
        return
    for t in tags:
        print(f"{t['displayName']}\t{t.get('memberCount')} members\t{t['id']}")


def cmd_tag_members(args):
    _, conn = connect()
    team_id, _ = resolve_team(conn, args.team)
    tags = conn.op("GET", f"/v1.0/teams/{team_id}/tags").get("value", [])
    tag = pick_by_id_or_name(tags, args.tag, lambda x: x["displayName"], "tag")
    tid = urllib.parse.quote(tag["id"], safe="")
    members = conn.op("GET", f"/v1.0/teams/{team_id}/tags/{tid}/members").get("value", [])
    if args.json:
        print(
            json.dumps(
                {"tag": tag["displayName"], "tagId": tag["id"], "members": members}, indent=2, ensure_ascii=False
            )
        )
        return
    for m in members:
        print(f"{m.get('displayName')}\t{m.get('userId')}\t{m.get('id')}")


def cmd_tabs(args):
    cfg, conn = connect()
    target = parse_channel_target(conn, cfg, args.target)
    j = conn.graph_json("GET", f"teams/{target['teamId']}/channels/{target['channelId']}/tabs?$expand=teamsApp")
    tabs = [
        {
            "id": t.get("id"),
            "name": t.get("displayName"),
            "app": (t.get("teamsApp") or {}).get("displayName"),
            "webUrl": t.get("webUrl"),
            "contentUrl": (t.get("configuration") or {}).get("contentUrl"),
        }
        for t in j.get("value", [])
    ]
    if args.json:
        print(json.dumps(tabs, indent=2, ensure_ascii=False))
        return
    for t in tabs:
        print(f"{t['name']}\t{t['app'] or ''}\t{t['webUrl'] or ''}")


def cmd_files(args):
    cfg, conn = connect()
    target = parse_channel_target(conn, cfg, args.target)
    folder = conn.graph_json("GET", f"teams/{target['teamId']}/channels/{target['channelId']}/filesFolder")
    out = {
        "id": folder.get("id"),
        "name": folder.get("name"),
        "webUrl": folder.get("webUrl"),
        "size": folder.get("size"),
        "driveId": (folder.get("parentReference") or {}).get("driveId"),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_apps(args):
    cfg, conn = connect()
    t = args.target
    if t == "self" or t.startswith("chat:"):
        die("installedApps is only supported for teams (the connector blocks /chats/... paths)", 2)
    if t.startswith("channel:"):
        target = parse_channel_target(conn, cfg, t)
        tid = target["teamId"]
    else:
        tid, _ = resolve_team(conn, t)
    path = f"teams/{tid}/installedApps?$expand=teamsAppDefinition"
    apps = []
    for j in conn.pages(path):
        for a in j.get("value", []):
            d = a.get("teamsAppDefinition") or {}
            apps.append(
                {
                    "id": a.get("id"),
                    "teamsAppId": d.get("teamsAppId"),
                    "name": d.get("displayName"),
                    "version": d.get("version"),
                }
            )
    if args.json:
        print(json.dumps(apps, indent=2, ensure_ascii=False))
        return
    for a in apps:
        print(f"{a['name']}\t{a['version'] or ''}\t{a['teamsAppId'] or ''}")


def cmd_find_user(args):
    cfg, conn = connect()
    tid = resolve_team(conn, args.team)[0] if args.team else None
    hits = find_users(conn, cfg, args.query, tid)
    if args.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
    else:
        for u in hits:
            print(f"{u['name']}\t{u.get('email')}\t{u['userId']}\t{u.get('source', 'team')}")
    if not hits:
        sys.exit(2)


# ---------------------------------------------------------------- commands: chats / sections
def chat_list(conn, chat_type=None, members=False, pages=5, limit=200):
    q = ["$top=50"]
    if chat_type:
        q.append("$filter=chatType%20eq%20%27" + chat_type + "%27")
    q.append("$expand=members" if members else "$select=id,chatType,topic,lastUpdatedDateTime")
    out = []
    for j in conn.pages("me/chats?" + "&".join(q), max_pages=pages):
        out.extend(j.get("value", []))
        if len(out) >= limit:
            break
    return out[:limit]


def cmd_chats(args):
    _, conn = connect()
    chats = chat_list(conn, args.type, args.members, args.pages, args.limit)
    if args.json:
        print(json.dumps(chats, indent=2, ensure_ascii=False))
        return
    for c in chats:
        who = (
            ", ".join(m.get("displayName") or "?" for m in c.get("members", []))
            if args.members
            else (c.get("topic") or "")
        )
        print(f"{c['chatType']}\t{c['id']}\t{who}")


def cmd_find_chat(args):
    cfg, conn = connect()
    q = norm(args.query)
    toks = [t for t in re.split(r"[\s,.]+", q) if t]
    if not toks:
        die(f"no chat matching '{args.query}'", 2)
    me = cfg.get("user", {}).get("id")
    hits = []
    for c in chat_list(conn, "oneOnOne", True, args.pages, 1000):
        others = [m for m in c.get("members", []) if m.get("userId") != me]
        if any(
            all(t in norm((m.get("displayName") or "") + " " + (m.get("email") or "")) for t in toks) for m in others
        ):
            hits.append(
                {"chatId": c["id"], "with": [{"name": m.get("displayName"), "email": m.get("email")} for m in others]}
            )
    print(json.dumps(hits, indent=2, ensure_ascii=False))
    if not hits:
        sys.exit(2)


def cmd_chat_members(args):
    cfg, conn = connect()
    target = parse_chat_target(conn, cfg, args.target)
    j = conn.graph_json("GET", f"me/chats/{target['chatId']}/members")
    members = [
        {
            "userId": m.get("userId"),
            "name": m.get("displayName"),
            "email": m.get("email"),
            "roles": m.get("roles") or [],
        }
        for m in j.get("value", [])
    ]
    if args.json:
        print(json.dumps(members, indent=2, ensure_ascii=False))
        return
    for m in members:
        print(f"{m['name']}\t{m['email'] or ''}\t{m['userId'] or ''}")


MAX_CHAT_USERS = 20


def cmd_create_chat(args):
    cfg, conn = connect()
    raw_specs = [u.strip() for u in re.split(r"[,;]+", args.users) if u.strip()]
    if not raw_specs:
        die("provide at least one user in --users", 2)
    if len(raw_specs) > MAX_CHAT_USERS:
        die(f"the Teams connector supports at most {MAX_CHAT_USERS} users per chat (and no guest users)", 2)
    resolved = [resolve_single_user(conn, cfg, s) for s in raw_specs]
    body = {"members": ";".join(u.get("email") or u["userId"] for u in resolved)}
    if args.topic:
        body["topic"] = args.topic
    if not args.send:
        print(
            json.dumps(
                {
                    "action": "create-chat",
                    "topic": args.topic,
                    "members": [{"name": u["name"], "email": u.get("email"), "userId": u["userId"]} for u in resolved],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        print("\nDRY RUN - chat was not created. Re-run with --send after confirmation.", file=sys.stderr)
        return
    resp = conn.op("POST", "/beta/chats", body)
    print(json.dumps(resp, indent=2, ensure_ascii=False))


def cmd_sections(args):
    _, conn = connect()
    secs = conn.op("GET", "/beta/me/teamwork/sections").get("value", [])
    if args.json:
        print(json.dumps(secs, indent=2, ensure_ascii=False))
        return
    for s in secs:
        print(f"{s.get('displayName')}\t{s.get('sectionType') or ''}\t{s.get('id')}")


def cmd_section_items(args):
    _, conn = connect()
    secs = conn.op("GET", "/beta/me/teamwork/sections").get("value", [])
    sec = pick_by_id_or_name(secs, args.section, lambda x: x.get("displayName", ""), "section")
    sid = urllib.parse.quote(sec["id"], safe="")
    items = conn.op("GET", f"/beta/me/teamwork/sections/{sid}/items").get("value", [])
    if args.json:
        print(
            json.dumps(
                {"section": sec.get("displayName"), "sectionId": sec["id"], "items": items},
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    for it in items:
        print(f"{it.get('itemType') or ''}\t{it.get('id')}")


# ---------------------------------------------------------------- commands: read / search / delta
def fetch_messages(conn, target, names, me_id, since=None, limit=30, pages=10, expand_replies=True, include_all=False):
    expand = target["kind"] == "channel" and expand_replies
    top = 20 if expand else 50
    uri = f"{target['msgs']}?$top={top}" + ("&$expand=replies" if expand else "")
    res = []
    for j in conn.pages(uri, max_pages=pages):
        page = j.get("value", [])
        for m in page:
            if m.get("deletedDateTime") or (m.get("messageType") != "message" and not include_all):
                continue
            if (
                since
                and msg_ts(m, "createdDateTime") < since
                and (
                    not expand
                    or not any(
                        not r.get("deletedDateTime") and msg_ts(r, "createdDateTime") >= since
                        for r in (m.get("replies") or [])
                    )
                )
            ):
                continue
            res.append(norm_msg(m, names, me_id))
        if since and page and all(msg_ts(m, "lastModifiedDateTime") < since for m in page):
            break
        if not since and len(res) >= limit:
            break
    res.sort(key=lambda x: x["created"] or "", reverse=True)
    return res[:limit]


def cmd_read(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.target)
    me_id = cfg.get("user", {}).get("id")
    names = known_users(conn, cfg, target.get("teamId"))
    since = parse_since(args.since) if args.since else None
    res = fetch_messages(
        conn,
        target,
        names,
        me_id,
        since=since,
        limit=args.limit,
        pages=args.pages,
        expand_replies=not args.no_replies,
        include_all=args.all,
    )
    if args.mentioning_me:
        res = [m for m in res if m["mentionsMe"] or any(r.get("mentionsMe") for r in m.get("replies", []))]
    if args.json:
        print(
            json.dumps(
                {"target": target["label"], "me": me_id, "count": len(res), "messages": res},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"# {target['label']} - {len(res)} message(s), newest first")
        for m in res:
            print(fmt_msg(m))
            print()


def cmd_thread(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.target)
    if target["kind"] != "channel":
        die("thread is for channel messages; use read for chats", 2)
    me_id = cfg.get("user", {}).get("id")
    names = known_users(conn, cfg, target.get("teamId"))
    root = conn.graph_json("GET", msg_path(target, args.message_id))
    reps = []
    for j in conn.pages(msg_path(target, args.message_id) + "/replies?$top=50", max_pages=10):
        reps.extend(j.get("value", []))
    root["replies"] = reps
    m = norm_msg(root, names, me_id)
    print(json.dumps(m, indent=2, ensure_ascii=False) if args.json else fmt_msg(m))


def cmd_message(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.target)
    me_id = cfg.get("user", {}).get("id")
    names = known_users(conn, cfg, target.get("teamId"))
    raw = conn.graph_json("GET", msg_path(target, args.message_id, args.reply_id))
    m = norm_msg(raw, names, me_id)
    print(json.dumps(m, indent=2, ensure_ascii=False) if args.json else fmt_msg(m))


def matches_search(msg, pattern, author_q):
    def check_one(item):
        if author_q and author_q not in norm(item.get("author") or ""):
            return False
        if not pattern:
            return True
        haystack = "\n".join(
            [
                item.get("subject") or "",
                item.get("text") or "",
                " ".join(item.get("links") or []),
                " ".join(a.get("name") or a.get("cardText") or "" for a in (item.get("attachments") or [])),
            ]
        )
        return bool(pattern.search(haystack))

    if check_one(msg):
        return True
    return any(check_one(r) for r in (msg.get("replies") or []))


def cmd_search(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.target)
    me_id = cfg.get("user", {}).get("id")
    names = known_users(conn, cfg, target.get("teamId"))
    since = parse_since(args.since) if args.since else None
    try:
        pattern = re.compile(args.query if args.regex else re.escape(args.query), re.IGNORECASE)
    except re.error as e:
        die(f"invalid regex '{args.query}': {e}", 2)
    author_q = norm(args.author) if args.author else None
    msgs = fetch_messages(
        conn,
        target,
        names,
        me_id,
        since=since,
        limit=args.pages * 50,
        pages=args.pages,
        expand_replies=True,
        include_all=False,
    )
    hits = [m for m in msgs if matches_search(m, pattern, author_q)][: args.limit]
    if args.json:
        print(
            json.dumps(
                {"target": target["label"], "query": args.query, "count": len(hits), "messages": hits},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"# {target['label']} - {len(hits)} match(es) for '{args.query}'")
        for m in hits:
            print(fmt_msg(m))
            print()


def cmd_delta(args):
    cfg, conn = connect()
    if args.target == "self" or args.target.startswith("chat:"):
        die("delta is supported for channel messages only", 2)
    target = parse_channel_target(conn, cfg, args.target)
    me_id = cfg.get("user", {}).get("id")
    names = known_users(conn, cfg, target.get("teamId"))
    uri = args.delta_link or f"teams/{target['teamId']}/channels/{target['channelId']}/messages/delta?$top=50"
    msgs: list[dict[str, Any]] = []
    delta_link: str | None = None
    next_link: str | None = None
    for _ in range(args.pages):
        j = conn.graph_json("GET", uri)
        msgs.extend(
            norm_msg(m, names, me_id)
            for m in j.get("value", [])
            if not m.get("deletedDateTime") and m.get("messageType") == "message"
        )
        delta_link = j.get("@odata.deltaLink") or delta_link
        next_link = j.get("@odata.nextLink")
        if not next_link:
            break
        uri = next_link
    print(
        json.dumps(
            {
                "target": target["label"],
                "count": len(msgs),
                "deltaLink": delta_link,
                "nextLink": next_link,
                "messages": msgs,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def cmd_graph(args):
    _, conn = connect()
    uri = args.path if args.path.startswith("http") else f"{GRAPH}/{args.version}/{args.path.lstrip('/')}"
    status, j = conn.graph("GET", uri, custom_headers=args.header)
    print(json.dumps(j, indent=2, ensure_ascii=False))
    if status >= 400:
        sys.exit(1)


def cmd_op(args):
    _, conn = connect()
    path = "/" + args.path.lstrip("/")
    j = conn.op("GET", path)
    print(json.dumps(j, indent=2, ensure_ascii=False))


def cmd_custom_emojis(args):
    cfg, conn = connect()
    if args.scan:
        target = parse_target(conn, cfg, args.scan)
        me_id = cfg.get("user", {}).get("id")
        names = known_users(conn, cfg, target.get("teamId"))
        fetch_messages(conn, target, names, me_id, limit=args.pages * 50, pages=args.pages, expand_replies=True)
    emojis = load_custom_emojis()
    if args.json:
        print(json.dumps(emojis, indent=2, ensure_ascii=False))
        return
    for name, info in sorted(emojis.items()):
        print(f":{name}:\t{info['objectId']}\t{info.get('host') or ''}")


# ---------------------------------------------------------------- commands: meetings / transcripts / recordings
def _is_numeric_meeting_id(ref):
    digits = re.sub(r"\D", "", ref or "")
    return bool(re.fullmatch(r"[\d\s-]+", ref or "") and len(digits) >= 6)


def lookup_meeting(conn, cfg, ref):
    if ref.startswith("chat:"):
        target = parse_chat_target(conn, cfg, ref)
        chat = conn.graph_json("GET", f"me/chats/{target['chatId']}?$select=id,topic,onlineMeetingInfo")
        join_url = (chat.get("onlineMeetingInfo") or {}).get("joinWebUrl")
        if not join_url:
            die(f"chat {target['chatId']} does not contain onlineMeetingInfo.joinWebUrl", 2)
        ref = join_url
    if ref.startswith(("http://", "https://")):
        ltype, lval = "JoinWebUrl", ref
    elif _is_numeric_meeting_id(ref):
        ltype, lval = "JoinMeetingId", re.sub(r"\D", "", ref)
    else:
        ltype, lval = "MeetingId", ref
    enc = urllib.parse.quote(lval, safe="")
    return conn.op("GET", f"/v1.0/me/onlineMeetings/lookup?lookupType={ltype}&lookupValue={enc}")


def resolve_meeting_id(conn, cfg, ref):
    if ref.startswith(("http://", "https://", "chat:")) or _is_numeric_meeting_id(ref):
        m = lookup_meeting(conn, cfg, ref)
        mid = m.get("id")
        if not mid:
            die(f"could not resolve meeting ID for '{ref}'", 2)
        return urllib.parse.quote(urllib.parse.unquote(mid), safe="")
    return urllib.parse.quote(urllib.parse.unquote(ref), safe="")


def cmd_meeting(args):
    cfg, conn = connect()
    m = lookup_meeting(conn, cfg, args.meeting)
    print(json.dumps(m, indent=2, ensure_ascii=False))


def cmd_transcripts(args):
    cfg, conn = connect()
    mid = resolve_meeting_id(conn, cfg, args.meeting)
    items = conn.op("GET", f"/v1.0/me/onlineMeetings/{mid}/transcripts").get("value", [])
    if args.json:
        print(json.dumps({"meetingId": mid, "transcripts": items}, indent=2, ensure_ascii=False))
        return
    for t in items:
        print(f"{t.get('id')}\t{t.get('createdDateTime') or ''}")


def cmd_transcript(args):
    cfg, conn = connect()
    mid = resolve_meeting_id(conn, cfg, args.meeting)
    tid = urllib.parse.quote(args.transcript_id, safe="")
    raw = conn.op_raw("GET", f"/v1.0/me/onlineMeetings/{mid}/transcripts/{tid}/content")
    if args.out:
        p = write_private_bytes(args.out, raw)
        print(json.dumps({"out": str(p), "bytes": len(raw)}, ensure_ascii=False))
    else:
        sys.stdout.write(raw.decode("utf-8", "replace"))


def cmd_recordings(args):
    cfg, conn = connect()
    mid = resolve_meeting_id(conn, cfg, args.meeting)
    items = conn.op("GET", f"/v1.0/me/onlineMeetings/{mid}/recordings").get("value", [])
    if args.json:
        print(json.dumps({"meetingId": mid, "recordings": items}, indent=2, ensure_ascii=False))
        return
    for r in items:
        print(f"{r.get('id')}\t{r.get('createdDateTime') or ''}")


def cmd_recording(args):
    cfg, conn = connect()
    mid = resolve_meeting_id(conn, cfg, args.meeting)
    rid = urllib.parse.quote(args.recording_id, safe="")
    raw = conn.op_raw("GET", f"/v1.0/me/onlineMeetings/{mid}/recordings/{rid}/content")
    p = write_private_bytes(args.out, raw)
    print(json.dumps({"out": str(p), "bytes": len(raw)}, ensure_ascii=False))


def cmd_adhoc_calls(args):
    _, conn = connect()
    op_name = "getAllRecordings" if args.kind == "recordings" else "getAllTranscripts"
    j = conn.op("GET", f"/v1.0/me/adhocCalls/{op_name}")
    print(json.dumps(j, indent=2, ensure_ascii=False))


def cmd_ai_insights(args):
    cfg, conn = connect()
    mid = resolve_meeting_id(conn, cfg, args.meeting)
    path = f"/v1.0/copilot/me/onlineMeetings/{mid}/aiInsights"
    if args.insight_id:
        path += "/" + urllib.parse.quote(args.insight_id, safe="")
    j = conn.op("GET", path)
    print(json.dumps(j, indent=2, ensure_ascii=False))


def cmd_create_meeting(args):
    _, conn = connect()
    body = {
        "subject": args.subject,
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "body": {"content": args.content or "", "contentType": "html"},
        "timeZone": args.time_zone,
        "start": {"dateTime": args.start},
        "end": {"dateTime": args.end},
    }
    if args.attendees:
        body["requiredAttendees"] = ";".join(x.strip() for x in re.split(r"[,;]+", args.attendees) if x.strip())
    if args.optional_attendees:
        body["optionalAttendees"] = ";".join(
            x.strip() for x in re.split(r"[,;]+", args.optional_attendees) if x.strip()
        )
    if not args.send:
        print(
            json.dumps({"action": "create-meeting", "calendar": args.calendar_id, **body}, indent=2, ensure_ascii=False)
        )
        print("\nDRY RUN - meeting was not created. Re-run with --send after confirmation.", file=sys.stderr)
        return
    cal = urllib.parse.quote(args.calendar_id, safe="")
    resp = conn.op("POST", f"/v1.0/me/calendars/{cal}/events", body)
    print(json.dumps(resp, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- commands: digest
GUID_IN_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def local_midnight(d):
    return dt.datetime.combine(d, dt.time.min).astimezone()


def last_working_day(today):
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def parse_window(s):
    """Return (aware start, label). Default: local midnight of the last working day (Mon-Fri)."""
    today = dt.datetime.now().astimezone().date()
    s = (s or "last-workday").strip().lower()
    if s in ("last-workday", "lwd"):
        d = last_working_day(today)
        return local_midnight(d), f"since {d:%a %Y-%m-%d} 00:00 (last working day) until now"
    if s == "today":
        return local_midnight(today), f"today ({today:%a %Y-%m-%d}) since 00:00"
    if s == "yesterday":
        d = today - dt.timedelta(days=1)
        return local_midnight(d), f"since {d:%a %Y-%m-%d} 00:00 until now"
    if re.fullmatch(r"\d+[hdw]", s):
        return parse_since(s), f"last {s}"
    try:
        d = dt.date.fromisoformat(s)
    except ValueError:
        die(f"bad --since '{s}' (use last-workday, today, yesterday, 24h, 3d, 1w or YYYY-MM-DD)", 2)
    return local_midnight(d), f"since {d:%a %Y-%m-%d} 00:00 until now"


DEFAULT_MAX_TEXT = 500
LINE_MAX = 1900
CHAT_TAIL = 6
# Microsoft's own meeting agent (https://learn.microsoft.com/microsoftteams/facilitator-teams); its automated
# notes in meeting chats are not conversation.
BOT_AUTHORS = {"facilitator"}
ACK_RE = re.compile(
    r"^(ok(ay|i)?|thanks?|thx|thank you|got it|sounds good|great|perfect|awesome|"
    r"yes|yep|sure|done|ack|👍|❤️|🙏|✅)[\s.!]*$",
    re.IGNORECASE,
)
COMPACT = {"on": True}


def digest_msg(m, max_text, url=False):
    text = m["text"] or "[no text: image, GIF or attachment]"
    if len(text) > max_text:
        text = text[:max_text].rstrip() + "…"
    out = {"time": local(m["created"]), "author": m["author"], "byMe": m["byMe"], "text": text}
    if m.get("subject"):
        out["subject"] = m["subject"]
    if m.get("importance") and m["importance"] != "normal":
        out["importance"] = m["importance"]
    links = m.get("links") or []
    if COMPACT["on"]:
        links = [x for x in links if len(x) <= 200]
    if links:
        out["links"] = links[:5]
    if m.get("attachments"):
        out["attachments"] = [a.get("name") or a.get("cardText") or a.get("contentType") for a in m["attachments"][:3]]
    if m.get("mentions"):
        out["mentions"] = [{"type": x["type"], "name": x["name"]} for x in m["mentions"]]
    if m.get("mentionsMe"):
        out["mentionsMe"] = True
    if m.get("reactions"):
        out["reactions"] = [
            (
                {"emoji": r["emoji"], "by": r["by"]}
                if COMPACT["on"] and r["emoji"] != "custom"
                else {"emoji": r["emoji"], "name": r["name"], "by": r["by"]}
            )
            for r in m["reactions"]
        ]
    if m.get("reactionsByMe"):
        out["reactionsByMe"] = m["reactionsByMe"]
    if url and m.get("webUrl"):
        out["webUrl"] = m["webUrl"]
    return out


def window_messages(conn, path, since, names, me_id, expand, pages):
    """Messages created in the window, plus older channel threads that got replies in the window.
    Each item is a normalized message with `_older` (root before window) and `_reps` (replies in window)."""
    uri = f"{path}?$top={20 if expand else 50}" + ("&$expand=replies" if expand else "")
    raw = []
    for j in conn.pages(uri, max_pages=pages):
        page = j.get("value", [])
        for m in page:
            if m.get("deletedDateTime") or m.get("messageType") != "message":
                continue
            if expand:
                m["replies"] = [
                    r
                    for r in (m.get("replies") or [])
                    if not r.get("deletedDateTime") and r.get("messageType") == "message"
                ]
            raw.append(m)
        if page and all(msg_ts(m, "lastModifiedDateTime") < since for m in page):
            break
    out = []
    for m in raw:
        n = norm_msg(m, names, me_id)
        in_window = parse_ts(n["created"]) >= since
        reps = [r for r in n.get("replies", []) if parse_ts(r["created"]) >= since]
        if not in_window and not reps:
            continue
        n["_older"], n["_reps"] = not in_window, reps
        out.append(n)
    out.sort(key=lambda x: x["created"] or "")
    return out


def thread_items(n):
    all_reps = n.get("replies") if n.get("replies") is not None else (n.get("_reps") or [])
    return sorted([n, *all_reps], key=lambda x: x.get("created") or "")


def open_hints(n):
    """Deterministic hints on a channel thread: a person mention of me, or a team/channel/tag mention,
    that I have not answered (reply or reaction) afterwards."""
    items = thread_items(n)
    hints = {}
    for i, it in enumerate(items):
        later_mine = any(x.get("byMe") or x.get("reactionsByMe") for x in items[i + 1 :])
        if it.get("byMe") or later_mine:
            continue
        if it.get("mentionsMe") and not it.get("reactionsByMe"):
            hints["openForMe"] = True
        if any(x["type"] in ("channel", "tag", "team") for x in (it.get("mentions") or [])) and not it.get(
            "reactionsByMe"
        ):
            hints["teamMention"] = True
    return hints


def user_name(conn, names, cache, uid):
    if uid in cache:
        return cache[uid]
    name = (names.get(uid) or {}).get("name")
    if not name:
        try:
            name = conn.graph_json("GET", f"users/{uid}?$select=displayName").get("displayName")
        except TeamsError:
            name = None
    cache[uid] = name or uid
    return cache[uid]


def digest_chats(conn, since, names, me_id, args, errors):
    uri = (
        "me/chats?$expand=lastMessagePreview&$orderby=lastMessagePreview/createdDateTime%20desc"
        "&$top=50&$select=id,chatType,topic"
    )
    active = []
    try:
        for j in conn.pages(uri, max_pages=args.chat_pages):
            stop = False
            for c in j.get("value", []):
                lm = (c.get("lastMessagePreview") or {}).get("createdDateTime")
                if lm and parse_ts(lm) >= since:
                    if not (args.no_meeting_chats and c.get("chatType") == "meeting"):
                        active.append(c)
                else:
                    stop = True
            if stop:
                break
    except TeamsError as e:
        errors.append({"source": "chat list", "error": str(e)})
        return []
    cache: dict[str, str] = {}

    def work(c):
        try:
            msgs = window_messages(conn, f"me/chats/{c['id']}/messages", since, names, me_id, False, 3)
        except TeamsError as e:
            return None, {"source": f"chat {c['id']}", "error": str(e)}
        if not msgs or all(m["authorId"] is None and not m["mentionsMe"] for m in msgs):
            return None, None
        others = [m for m in msgs if not m["byMe"] and m["author"]]
        if c.get("topic"):
            label = c["topic"]
        elif c["chatType"] == "oneOnOne":
            ids = [g for g in GUID_IN_TEXT.findall(c["id"]) if g != me_id]
            other = ids[0] if ids else None
            hit = next((m["author"] for m in others if m["authorId"] == other), None)
            label = hit or (user_name(conn, names, cache, other) if other else "?")
        else:
            who = list(dict.fromkeys(m["author"] for m in others))
            label = "chat with " + ", ".join(who) if who else "chat (only my messages)"
        trailing = 0
        for m in reversed(msgs):
            if m["byMe"]:
                break
            trailing += 1
        tail = [
            m
            for m in (msgs[len(msgs) - trailing :] if trailing else [])
            if (m["author"] or "").lower() not in BOT_AUTHORS
        ]
        open_hint = None
        if tail:
            texts = [(m["text"] or "").strip() for m in tail]
            if any(m["mentionsMe"] for m in tail) or any("?" in t for t in texts):
                open_hint = "question"
            elif not all(not t or ACK_RE.match(t) for t in texts):
                open_hint = "check"
        shown = [m for m in msgs if (m["author"] or "").lower() not in BOT_AUTHORS and (m["text"] or m["mentionsMe"])]
        shown = shown[-CHAT_TAIL:]
        if not shown:
            return None, None
        res = {
            "type": c["chatType"],
            "with" if c["chatType"] == "oneOnOne" else "name": label,
            "lastMessageByMe": msgs[-1]["byMe"],
            "messagesAtEndNotByMe": trailing,
        }
        if open_hint:
            res["open"] = open_hint
        res["messages"] = [digest_msg(m, args.max_text) for m in shown]
        return res, None

    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res, err in pool.map(work, active):
            if err:
                errors.append(err)
            elif res:
                out.append(res)
    return out


def fit_line(obj, verbose):
    """Serialize one digest line; shrink text/links/optional fields so it stays under LINE_MAX."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if verbose or len(s) <= LINE_MAX:
        return s
    obj = dict(obj)
    if obj.get("links") and len(s) > LINE_MAX:
        obj["links"] = obj["links"][:2]
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if len(s) > LINE_MAX and obj.get("text"):
        cut = len(s) - LINE_MAX + 1
        obj["text"] = obj["text"][: max(0, len(obj["text"]) - cut)].rstrip() + "…"
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for drop_key in ("attachments", "links", "reactions", "subject", "errors"):
        if len(s) <= LINE_MAX:
            break
        if drop_key in obj:
            obj.pop(drop_key, None)
            s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s


def digest_lines(out_groups, chats, verbose):
    """One JSON line per post, reply, chat and chat message, so each line stays short."""
    lines: list[dict[str, Any]] = []

    def emit_channel(group, subgroup, ch):
        base = {"group": group, "team": ch["team"], "channel": ch["channel"]}
        if subgroup:
            base["subgroup"] = subgroup
        for m in ch.get("messages", []):
            replies = m.get("repliesInWindow", [])
            post = {k: v for k, v in m.items() if k != "repliesInWindow"}
            hint = "mention" if m.get("openForMe") else "team" if m.get("teamMention") else None
            if hint:
                post["open"] = hint
            lines.append({"kind": "post", **base, **post})
            lines.extend(
                {"kind": "reply", "channel": ch["channel"], "to": f"{m['time']} {m['author']}", **r} for r in replies
            )

    for g in out_groups:
        for ch in g.get("channels", []):
            emit_channel(g["name"], None, ch)
        for sg in g.get("subgroups", []):
            for ch in sg["channels"]:
                emit_channel(g["name"], sg["name"], ch)
    for c in chats:
        label = c.get("with") or c.get("name")
        meta = {k: v for k, v in c.items() if k != "messages"}
        lines.append({"kind": "chat", **meta})
        lines.extend({"kind": "chatmsg", "chat": label, **m} for m in c["messages"])
    return [fit_line(x, verbose) for x in lines]


def _watch_from_cli_channels(cli_channels):
    by_team: dict[str, list[str]] = {}
    for spec in cli_channels or []:
        s = spec.removeprefix("channel:")
        if "/" not in s:
            die(f"--channel '{spec}' must be 'Team/Channel'", 2)
        t, c = s.split("/", 1)
        by_team.setdefault(t.strip(), []).append(c.strip())
    if not by_team:
        return []
    return [{"group": "Channels", "channels": [{"team": t, "names": cs} for t, cs in by_team.items()]}]


def cmd_digest(args):
    cfg, conn = connect()
    COMPACT["on"] = not args.verbose
    if args.verbose and args.max_text == DEFAULT_MAX_TEXT:
        args.max_text = 100000
    if args.watchlist:
        wl = load_watchlist(args.watchlist, tenant_id=cfg["tenantId"])
        watch = wl.get("watch") or []
    elif args.channel:
        watch = _watch_from_cli_channels(args.channel)
    else:
        watch = cfg.get("watch") or []
    if not watch and args.no_chats:
        die(
            "no channels to check (pass --channel 'Team/Channel', "
            f"create {DEFAULT_WATCHLIST}, or omit --no-chats); see references/digest.md",
            2,
        )
    since, label = parse_window(args.since)
    me_id = cfg.get("user", {}).get("id")
    errors: list[dict[str, Any]] = []
    jobs: list[tuple[int, int | None, str, str]] = []
    teams: dict[str, dict[str, Any] | None] = {}

    for gi, g in enumerate(watch):
        for entry in g.get("channels", []):
            jobs += [(gi, None, entry["team"], n) for n in entry["names"]]
        for si, sg in enumerate(g.get("subgroups", [])):
            for entry in sg.get("channels", []):
                jobs += [(gi, si, entry["team"], n) for n in entry["names"]]

    joined = joined_teams(conn) if jobs else []
    for ref in dict.fromkeys(j[2] for j in jobs):
        t: Any
        err: str | None
        if GUID.match(ref):
            t, err = {"id": ref, "name": ref}, None
        else:
            t, err = find_one(joined, ref, lambda x: x["name"])
        if err:
            errors.append({"source": f"team {ref}", "error": err})
            teams[ref] = None
        else:
            teams[ref] = {
                "id": t["id"],
                "name": t["name"],
                "channels": team_channels(conn, t["id"]),
                "names": known_users(conn, cfg, t["id"]),
            }

    def run(job):
        _, _, ref, frag = job
        t = teams.get(ref)
        if not t:
            return job, None, None
        ch, err = find_one(t["channels"], frag, lambda c: c["name"])
        if err:
            return job, None, {"source": f"{t['name']} / {frag}", "error": err}
        try:
            msgs = window_messages(
                conn,
                f"teams/{t['id']}/channels/{ch['id']}/messages",
                since,
                t["names"],
                me_id,
                True,
                args.channel_pages,
            )
        except TeamsError as e:
            return job, None, {"source": f"{t['name']} / {ch['name']}", "error": str(e)}
        if not msgs:
            return job, None, None
        src = {"team": t["name"], "channel": ch["name"], "messages": []}
        for m in msgs:
            d = digest_msg(m, args.max_text, url=True)
            if m["_older"]:
                d["olderThreadWithNewReplies"] = True
            if m["_reps"]:
                d["repliesInWindow"] = [digest_msg(r, args.max_text) for r in m["_reps"]]
            d.update(open_hints(m))
            src["messages"].append(d)
        return job, src, None

    groups = [
        {
            "name": g["group"],
            "channels": [],
            "subgroups": [{"name": sg["name"], "channels": []} for sg in g.get("subgroups", [])],
        }
        for g in watch
    ]
    read_ok = 0
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for job, src, job_err in pool.map(run, jobs):
                if job_err:
                    errors.append(job_err)
                    continue
                read_ok += 1 if teams.get(job[2]) else 0
                if src:
                    (
                        groups[job[0]]["channels"]
                        if job[1] is None
                        else groups[job[0]]["subgroups"][job[1]]["channels"]
                    ).append(src)

    out_groups = []
    for g in groups:
        g["subgroups"] = [sg for sg in g["subgroups"] if sg["channels"]]
        if not g["subgroups"]:
            del g["subgroups"]
        if g["channels"] or g.get("subgroups"):
            if not g["channels"]:
                del g["channels"]
            out_groups.append(g)

    chats = [] if args.no_chats else digest_chats(conn, since, known_users(conn, cfg), me_id, args, errors)
    header = {
        "kind": "header",
        "tenantId": cfg["tenantId"],
        "window": {
            "label": label,
            "since": since.isoformat(),
            "until": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        },
        "me": cfg.get("user", {}).get("displayName"),
        "channelsChecked": read_ok,
    }
    if errors:
        header["errors"] = errors
    if args.format == "json":
        result = dict(header, groups=out_groups)
        result.pop("kind")
        if not args.no_chats:
            result["chats"] = chats
        lines = [json.dumps(result, ensure_ascii=False, separators=(",", ":"))]
    else:
        lines = [fit_line(header, args.verbose), *digest_lines(out_groups, chats, args.verbose)]
    text = "\n".join(lines) + "\n"
    if args.out:
        p = write_private_bytes(args.out, text.encode())
        print(
            json.dumps(
                {
                    "out": str(p),
                    "lines": len(lines),
                    "bytes": len(text.encode()),
                    "window": header["window"]["label"],
                    "channelsChecked": read_ok,
                    "chats": len(chats),
                    "errors": len(errors),
                },
                ensure_ascii=False,
            )
        )
    else:
        sys.stdout.write(text)


# ---------------------------------------------------------------- commands: write
def own_message_check(conn, cfg, target, message_id, reply_id):
    me = cfg.get("user", {}).get("id")
    m = conn.graph_json("GET", msg_path(target, message_id, reply_id))
    uid = ((m.get("from") or {}).get("user") or {}).get("id")
    if not me or uid != me:
        die("refusing: this message was not written by you (edit/delete only work on your own messages).", 3)
    return m


def cmd_send(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.to)
    card = load_card_arg(args)
    body_html, parse_mentions = get_body_arg(args, allow_empty=card is not None)
    msg = build_message(
        conn,
        cfg,
        body_html,
        target,
        subject=args.subject,
        importance=args.importance,
        card=card,
        parse_mentions=parse_mentions,
    )
    if not args.send:
        preview(target["label"], "send", msg)
        return
    resp = conn.graph_json("POST", target["msgs"], msg)
    report_sent(resp, msg, f"teams.py delete --to {args.to} --message-id {resp.get('id')} --send")


def cmd_reply(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.to)
    if target["kind"] != "channel":
        die("reply is for channel threads", 2)
    card = load_card_arg(args)
    body_html, parse_mentions = get_body_arg(args, allow_empty=card is not None)
    msg = build_message(
        conn, cfg, body_html, target, importance=args.importance, card=card, parse_mentions=parse_mentions
    )
    if not args.send:
        preview(target["label"], f"reply to {args.message_id}", msg)
        return
    resp = conn.graph_json("POST", msg_path(target, args.message_id) + "/replies", msg)
    report_sent(
        resp, msg, f"teams.py delete --to {args.to} --message-id {args.message_id} --reply-id {resp.get('id')} --send"
    )


def cmd_edit(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.to)
    path = msg_path(target, args.message_id, args.reply_id)
    card = load_card_arg(args)
    body_html, parse_mentions = get_body_arg(args, allow_empty=card is not None)
    msg = build_message(
        conn,
        cfg,
        body_html,
        target,
        subject=args.subject,
        importance=args.importance,
        card=card,
        parse_mentions=parse_mentions,
    )
    own_message_check(conn, cfg, target, args.message_id, args.reply_id)
    extra = {"replyId": args.reply_id} if args.reply_id else None
    if not args.send:
        preview(target["label"], f"edit {args.message_id}", msg, extra=extra)
        return
    conn.graph_json("PATCH", path, msg)
    out = {"edited": args.message_id}
    if args.reply_id:
        out["replyId"] = args.reply_id
    print(json.dumps(out))


def cmd_delete(args):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.to)
    path = msg_path(target, args.message_id, args.reply_id)
    m = own_message_check(conn, cfg, target, args.message_id, args.reply_id)
    text = html_to_text((m.get("body") or {}).get("content", ""))
    if not args.send:
        prev = {"action": "delete (soft)", "target": target["label"], "messageId": args.message_id}
        if args.reply_id:
            prev["replyId"] = args.reply_id
        prev["text"] = text[:300]
        print(json.dumps(prev, indent=2, ensure_ascii=False))
        print("\nDRY RUN - nothing was deleted. Re-run with --send after confirmation.", file=sys.stderr)
        return
    conn.graph_json("POST", path + "/softDelete", {})
    out = {"deleted": args.message_id}
    if args.reply_id:
        out["replyId"] = args.reply_id
    print(json.dumps(out))


def cmd_react(args, remove=False):
    cfg, conn = connect()
    target = parse_target(conn, cfg, args.to)
    if target["kind"] != "channel":
        die("reactions can only be set on channel messages (the connector blocks the chat reaction path).", 2)
    emoji = (args.emoji or "").strip()
    if not emoji or emoji.isascii():
        die("reaction must be a unicode emoji character (e.g. 👍, ❤️, 👀, ✅), not an ASCII name", 2)
    verb = "unsetReaction" if remove else "setReaction"
    if not args.send:
        print(
            json.dumps(
                {
                    "action": verb,
                    "target": target["label"],
                    "messageId": args.message_id,
                    "replyId": args.reply_id,
                    "emoji": emoji,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print("\nDRY RUN - nothing was changed. Re-run with --send after confirmation.", file=sys.stderr)
        return
    conn.graph_json("POST", msg_path(target, args.message_id, args.reply_id) + "/" + verb, {"reactionType": emoji})
    print(json.dumps({verb: emoji, "messageId": args.message_id}, ensure_ascii=False))


# ---------------------------------------------------------------- onboarding / doctor
def open_browser(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    if sys.platform == "win32" and hasattr(os, "startfile"):
        try:
            os.startfile(url)  # nosec B606  # noqa: S606 - url is validated as https above
            return True
        except OSError:
            pass
    env = dict(os.environ, TEAMS_BROWSER_URL=url)
    cmds = []
    if sys.platform == "darwin" and shutil.which("open"):
        cmds.append(["open", url])
    elif shutil.which("powershell.exe"):
        cmds.append(["powershell.exe", "-NoProfile", "-Command", "Start-Process $env:TEAMS_BROWSER_URL"])
    elif shutil.which("xdg-open"):
        cmds.append(["xdg-open", url])
    for cmd in cmds:
        try:
            subprocess.run(cmd, env=env, check=True, timeout=30)  # nosec B603  # noqa: S603
            return True
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def cmd_onboard(args):
    global _CACHE_DIR

    def step(n, msg):
        print(f"[{n}] {msg}", flush=True)

    step(1, "az login & tenant identity")
    ident = current_identity()
    tid = ident["tenantId"]
    env = ident["environment"]
    user = ident["user"]
    _CACHE_DIR = CACHE_ROOT / tid
    print(f"    OK  {user['displayName']} <{user['upn']}> (tenant {tid})")

    step(2, f"Power Platform environment {env}")
    step(3, "Teams connection")
    c = find_connection(env, user)
    if not c:
        print("    no existing Teams connection found; creating one via Power Platform API...")
        c = create_connection(env)
        if not c:
            die(
                "could not create a Teams connection automatically.\n"
                f"Create one in your browser at: https://make.powerautomate.com/environments/{env}/connections\n"
                "(or make.powerautomate.com -> '... More' in the left sidebar -> Connections -> "
                "+ New connection -> Microsoft Teams),\n"
                "then re-run onboard."
            )
    cid = c["name"]
    st = conn_status(c)
    print(f"    connection {cid}: {st}")
    if st != "Connected":
        _, j = pp_post(
            f"apis/shared_teams/connections/{cid}/getConsentLink",
            {"redirectUrl": "https://global.consent.azure-apim.net/redirect"},
            "&$filter=environment%20eq%20%27" + env + "%27",
        )
        link = j.get("consentLink")
        if not link:
            die(f"could not get a consent link: {err_msg(j)}")
        print("    FIX  opening the consent page in your browser. Sign in and approve.")
        print("         (A final page saying 'did not receive an expected state query parameter' is harmless.)")
        if not open_browser(link):
            print("         Could not open a browser automatically. Open this URL yourself:\n         " + link)
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(5)
            c = find_connection(env, user)
            if c and conn_status(c) == "Connected":
                break
        else:
            die("connection still not Connected after 5 minutes. Re-run onboard after approving.")
        print("    OK  connection is Connected")

    step(4, "connector runtime URL and session cache")
    base = runtime_base(c)
    if not base:
        die("could not derive the runtime URL from the connection (no testLinks).")
    cfg = {**ident, "connectionId": cid, "runtimeBase": base}
    conn = Conn(cfg)
    try:
        conn.op("GET", "/beta/me/teamwork")
    except TeamsError as e:
        die(f"runtime call failed ({e}). Connection may need re-authorization; re-run onboard.")
    _CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for f in _CACHE_DIR.glob("*.json"):
        if f.name != "custom-emojis.json":
            f.unlink()
    write_private_bytes(_CACHE_DIR / "session.json", json.dumps(cfg, indent=2, ensure_ascii=False).encode("utf-8"))
    print(f"    OK  runtime reachable (cached in {_CACHE_DIR / 'session.json'})")

    for tref in args.team or []:
        tid_res, tname = resolve_team(conn, tref)
        print(f"    OK  {len(team_members(conn, tid_res))} members cached for team {tname}")

    if args.smoke:
        step(5, "smoke test (message to your self-chat)")
        target = parse_target(conn, cfg, "self")
        msg = build_message(
            conn, cfg, f"<p>[ms-teams smoke test] hello @[{user['displayName']}], onboarding works.</p>", target
        )
        resp = conn.graph_json("POST", target["msgs"], msg)
        ok, missing = verify_mentions(msg, resp)
        print(f"    sent message {resp.get('id')}; mention resolved: {bool(ok) and not missing}")
    print("\nDone. Try: teams.py doctor")


def cmd_doctor(_args):
    bad = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except SystemExit:
            ok, detail = False, "failed (see message above)"
        except Exception as e:
            ok, detail = False, str(e)
        print(f"{'OK ' if ok else 'FIX'} {name}: {detail}")
        if not ok:
            bad.append(name)
        return ok

    ident: dict[str, Any] = {}

    def az_check():
        ident.update(current_identity())
        u = ident["user"]
        return True, f"{u['upn'] or u['displayName']} (tenant {ident['tenantId']})"

    if not check("az login", az_check):
        sys.exit(1)

    found: list[dict[str, Any]] = []

    def conn_check():
        c = find_connection(ident["environment"], ident["user"])
        if not c:
            return False, "missing -> run `teams.py onboard` to create and authorize"
        found.append(c)
        st = conn_status(c)
        hint = "" if st == "Connected" else " -> run `teams.py onboard` to re-authorize"
        return st == "Connected", f"{st} ({c['name']}){hint}"

    if not check("Teams connection", conn_check):
        sys.exit(1)

    base = runtime_base(found[0])
    if not check("runtime URL", lambda: (bool(base), base or "missing testLinks")):
        sys.exit(1)

    _, conn = connect()
    check("runtime call", lambda: (bool(conn.op("GET", "/beta/me/teamwork")), "ok"))
    check(
        "read self-chat",
        lambda: (conn.graph("GET", f"{GRAPH}/v1.0/me/chats/{SELF_CHAT}/messages?$top=1")[0] == 200, "ok"),
    )
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- argparse
def add_body_args(p):
    g = p.add_argument_group(
        "message body (use @[Name], @[tag:on-call], @[channel:Team/Channel], @[team] in HTML for mentions)"
    )
    g.add_argument("--html")
    g.add_argument("--html-file", help="file with HTML, or - for stdin")
    g.add_argument("--text", help="plain text (literal, no mention parsing)")
    g.add_argument("--card", help="Adaptive Card JSON string to attach")
    g.add_argument("--card-file", help="file with Adaptive Card JSON, or - for stdin")


def use_utf8_stdio():
    """Teams content is full of non-ASCII text; a piped Windows stdout defaults to a legacy code page."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def main():
    use_utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="teams.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--tenant", help="Microsoft Entra tenant ID or domain (default: active `az` tenant or $MS_TEAMS_TENANT)"
    )
    ap.add_argument(
        "--refresh", action="store_true", help="ignore the cached team, channel and member lists and fetch them again"
    )
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("onboard", help="create / repair the Power Platform Teams connection for this tenant")
    p.add_argument("--team", action="append", help="team (name fragment) to pre-cache members for")
    p.add_argument("--smoke", action="store_true", help="post one test message to your self-chat")
    p.set_defaults(fn=cmd_onboard)

    p = sp.add_parser("doctor", help="check az login, Teams connection, and Graph passthrough")
    p.set_defaults(fn=cmd_doctor)

    p = sp.add_parser("teams", help="list joined or associated teams")
    p.add_argument("--associated", action="store_true", help="include teams where you are a shared-channel member")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_teams)

    p = sp.add_parser("team-info", help="get details for a team")
    p.add_argument("team")
    p.set_defaults(fn=cmd_team_info)

    p = sp.add_parser("channels", help="list channels of a team")
    p.add_argument("--team", required=True)
    p.add_argument("--filter")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_channels)

    p = sp.add_parser("channel-info", help="get details for a channel")
    p.add_argument("target", help="channel:<Team/Channel> or <Team/Channel>")
    p.set_defaults(fn=cmd_channel_info)

    p = sp.add_parser("members", help="list team members")
    p.add_argument("--team", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_members)

    p = sp.add_parser("channel-members", help="list direct members of a channel")
    p.add_argument("target", help="channel:<Team/Channel> or <Team/Channel>")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_channel_members)

    p = sp.add_parser("tags", help="list team tags")
    p.add_argument("--team", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tags)

    p = sp.add_parser("tag-members", help="list members of a team tag")
    p.add_argument("--team", required=True)
    p.add_argument("--tag", required=True, help="tag name fragment or ID")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tag_members)

    p = sp.add_parser("tabs", help="list tabs in a channel")
    p.add_argument("target", help="channel:<Team/Channel> or <Team/Channel>")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tabs)

    p = sp.add_parser("files", help="get the SharePoint filesFolder metadata for a channel")
    p.add_argument("target", help="channel:<Team/Channel> or <Team/Channel>")
    p.set_defaults(fn=cmd_files)

    p = sp.add_parser("apps", help="list installed apps in a team")
    p.add_argument("target", help="team name/GUID or channel:<Team/Channel>")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_apps)

    p = sp.add_parser("find-user", help="resolve a name or email to a user")
    p.add_argument("query")
    p.add_argument("--team")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_find_user)

    p = sp.add_parser("chats", help="list your chats")
    p.add_argument("--type", choices=["oneOnOne", "group", "meeting"])
    p.add_argument("--members", action="store_true", help="expand members (smaller pages)")
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_chats)

    p = sp.add_parser("find-chat", help="find your one-on-one chat with a person")
    p.add_argument("query")
    p.add_argument("--pages", type=int, default=20)
    p.set_defaults(fn=cmd_find_chat)

    p = sp.add_parser("chat-members", help="list members of a chat")
    p.add_argument("target", help="self, chat:<id>, or <chatId>")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_chat_members)

    p = sp.add_parser("create-chat", help="create a 1:1 or group chat (dry-run unless --send)")
    p.add_argument("--users", required=True, help="comma/semicolon-separated user names, emails, or IDs")
    p.add_argument("--topic", help="topic/title for a group chat")
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_create_chat)

    p = sp.add_parser("sections", help="list your custom Teams sidebar sections")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_sections)

    p = sp.add_parser("section-items", help="list items in a Teams sidebar section")
    p.add_argument("section", help="section name fragment or ID")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_section_items)

    p = sp.add_parser("read", help="read messages: self | chat:<id> | channel:<Team/Channel>")
    p.add_argument("target")
    p.add_argument("--since", help="e.g. 7d, 48h, 2w, 2026-10-06")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--pages", type=int, default=10)
    p.add_argument("--mentioning-me", action="store_true")
    p.add_argument("--no-replies", action="store_true")
    p.add_argument("--all", action="store_true", help="include system messages")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_read)

    p = sp.add_parser("thread", help="read one channel message with all replies")
    p.add_argument("target")
    p.add_argument("message_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_thread)

    p = sp.add_parser("message", help="read a single message or reply")
    p.add_argument("target")
    p.add_argument("message_id")
    p.add_argument("--reply-id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_message)

    p = sp.add_parser("search", help="search messages and replies in a channel or chat")
    p.add_argument("target")
    p.add_argument("query", help="substring or --regex pattern")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--author", help="filter by author name fragment")
    p.add_argument("--since", help="e.g. 7d, 30d, 2026-10-01")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--pages", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sp.add_parser("delta", help="incremental sync for channel messages via Graph delta query")
    p.add_argument("target", help="channel:<Team/Channel>")
    p.add_argument("--delta-link", help="deltaLink or nextLink URL from a previous run")
    p.add_argument("--pages", type=int, default=5)
    p.set_defaults(fn=cmd_delta)

    p = sp.add_parser("graph", help="raw read-only Graph GET through the connector")
    p.add_argument("path")
    p.add_argument("--version", choices=["v1.0", "beta"], default="v1.0")
    p.add_argument("--header", action="append", help="custom header 'Name: Value' (up to 5)")
    p.set_defaults(fn=cmd_graph)

    p = sp.add_parser("op", help="raw read-only connector operation GET")
    p.add_argument("path", help="connector path, e.g. /beta/me/joinedTeams")
    p.set_defaults(fn=cmd_op)

    p = sp.add_parser("custom-emojis", help="list or scan custom tenant emojis for :shortcode: use in HTML")
    p.add_argument("--scan", help="target to scan for custom emojis (e.g. self or channel:<Team/Channel>)")
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_custom_emojis)

    p = sp.add_parser("digest", help="read-only JSON digest of watched channels and active chats for a time window")
    p.add_argument(
        "--since", help="last-workday (default) | today | yesterday | 24h/3d/1w | YYYY-MM-DD (local midnight)"
    )
    p.add_argument("--channel", action="append", help="channel to include, e.g. 'Engineering/General' (repeatable)")
    p.add_argument("--watchlist", help=f"optional watchlist JSON file (default: {DEFAULT_WATCHLIST})")
    p.add_argument("--no-chats", action="store_true", help="skip 1:1, group and meeting chats")
    p.add_argument("--chat-pages", type=int, default=4)
    p.add_argument("--channel-pages", type=int, default=10)
    p.add_argument("--no-meeting-chats", action="store_true", help="skip meeting chats")
    p.add_argument("--max-text", type=int, default=DEFAULT_MAX_TEXT, help="characters kept per message")
    p.add_argument("--verbose", action="store_true", help="no compaction: full text, long links, reaction names")
    p.add_argument(
        "--format",
        choices=["lines", "json"],
        default="lines",
        help="lines (default): one JSON object per line (header, post/reply, chat/chatmsg); json: one object",
    )
    p.add_argument("--out", help="write the output to this file (mode 0600) and print only a short summary")
    p.add_argument("--workers", type=int, default=2, help="parallel reads (more triggers rate limits)")
    p.set_defaults(fn=cmd_digest)

    p = sp.add_parser("send", help="post a message or Adaptive Card (dry-run unless --send)")
    p.add_argument("--to", required=True)
    p.add_argument("--subject")
    p.add_argument("--importance", choices=["normal", "high", "urgent"], default="normal")
    add_body_args(p)
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_send)

    p = sp.add_parser("reply", help="reply in a channel thread (dry-run unless --send)")
    p.add_argument("--to", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--importance", choices=["normal", "high", "urgent"], default="normal")
    add_body_args(p)
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_reply)

    p = sp.add_parser("edit", help="edit one of your own messages (dry-run unless --send)")
    p.add_argument("--to", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--reply-id")
    p.add_argument("--subject")
    p.add_argument("--importance", choices=["normal", "high", "urgent"])
    add_body_args(p)
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_edit)

    p = sp.add_parser("delete", help="soft-delete one of your own messages (dry-run unless --send)")
    p.add_argument("--to", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--reply-id")
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_delete)

    for name, rem in (("react", False), ("unreact", True)):
        p = sp.add_parser(name, help=("remove" if rem else "set") + " your unicode reaction on a channel message")
        p.add_argument("--to", required=True)
        p.add_argument("--message-id", required=True)
        p.add_argument("--reply-id")
        p.add_argument("--emoji", required=True, help="unicode emoji character, e.g. 👍, ❤️, 👀, ✅")
        p.add_argument("--send", action="store_true")
        p.set_defaults(fn=lambda a, r=rem: cmd_react(a, r))

    p = sp.add_parser("meeting", help="look up an online meeting by join URL, meeting ID, or chat:<id>")
    p.add_argument("meeting", help="joinWebUrl, meetingId, joinMeetingId, or chat:<id>")
    p.set_defaults(fn=cmd_meeting)

    p = sp.add_parser("transcripts", help="list transcripts for an online meeting")
    p.add_argument("meeting", help="meetingId, joinWebUrl, or chat:<id>")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_transcripts)

    p = sp.add_parser("transcript", help="fetch VTT content of a meeting transcript")
    p.add_argument("meeting", help="meetingId, joinWebUrl, or chat:<id>")
    p.add_argument("transcript_id")
    p.add_argument("--out", help="save VTT content to file (mode 0600)")
    p.set_defaults(fn=cmd_transcript)

    p = sp.add_parser("recordings", help="list recordings for an online meeting")
    p.add_argument("meeting", help="meetingId, joinWebUrl, or chat:<id>")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_recordings)

    p = sp.add_parser("recording", help="download a meeting recording to a file (mode 0600)")
    p.add_argument("meeting", help="meetingId, joinWebUrl, or chat:<id>")
    p.add_argument("recording_id")
    p.add_argument("--out", required=True, help="output file path")
    p.set_defaults(fn=cmd_recording)

    p = sp.add_parser("adhoc-calls", help="list transcripts or recordings from ad-hoc calls")
    p.add_argument("--kind", choices=["transcripts", "recordings"], default="transcripts")
    p.set_defaults(fn=cmd_adhoc_calls)

    p = sp.add_parser("ai-insights", help="list or fetch Copilot AI insights for an online meeting")
    p.add_argument("meeting", help="meetingId, joinWebUrl, or chat:<id>")
    p.add_argument("--insight-id")
    p.set_defaults(fn=cmd_ai_insights)

    p = sp.add_parser("create-meeting", help="schedule an online Teams meeting (dry-run unless --send)")
    p.add_argument("--subject", required=True)
    p.add_argument("--start", required=True, help="ISO datetime, e.g. 2026-11-15T10:00:00")
    p.add_argument("--end", required=True, help="ISO datetime, e.g. 2026-11-15T10:30:00")
    p.add_argument("--time-zone", default="UTC", help="time zone name (default UTC)")
    p.add_argument("--attendees", help="comma/semicolon-separated required attendee emails")
    p.add_argument("--optional-attendees", help="comma/semicolon-separated optional attendee emails")
    p.add_argument("--content", help="HTML body for the meeting invite")
    p.add_argument("--calendar-id", default="Calendar")
    p.add_argument("--send", action="store_true")
    p.set_defaults(fn=cmd_create_meeting)

    args = ap.parse_args()
    if args.tenant:
        set_active_tenant(args.tenant)
    set_refresh(args.refresh)
    try:
        args.fn(args)
    except TeamsError as e:
        hint = ""
        if e.status in (401, 403):
            hint = "\nHint: run `teams.py doctor`; the connection or az login may need repair."
        die(f"{e}{hint}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
