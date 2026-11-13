#!/usr/bin/env python3
"""The lane record's service: one owner for the record, and a dashboard.

The record is the leases table in `lib/lane.py`, and its rules stay
there. This module adds the token, the role and the page.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import lane

# The roles a token can carry, from the strongest to the weakest.
WRITER = "writer"
REPORTER = "reporter"
READER = "reader"

# Which role is the stronger one. A digest listed twice is one
# credential, and it should not matter which list it is in.
ROLE_RANK = {WRITER: 3, REPORTER: 2, READER: 1}

# The port the service listens on when the settings name none.
DEFAULT_PORT = 8787

# The forge the dashboard links to when the settings name none.
DEFAULT_WEB = "https://github.com"

# How many finished lanes the history shows when the caller names no
# limit, and the most it will show.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class BadRequest(Exception):
    """A request the service cannot read.

    A refusal is the record saying no; this is the service saying it
    could not read what it was sent.
    """
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# ----- the settings --------------------------------------------------------

@dataclass(frozen=True)
class Token:
    """A token's digest, and the role it carries.

    The settings hold the digest and never the token: a settings file
    that leaks hands out nothing that can be used.
    """

    digest: str
    role: str


@dataclass(frozen=True)
class Settings:
    """The service's settings."""

    path: Path
    host: str
    port: int
    database: Path
    pools: dict[str, int]
    profiles: dict[str, lane.Profile]
    tokens: tuple[Token, ...]
    cert: Path | None
    key: Path | None
    web: str


def settings_path() -> Path:
    """Where the service's settings live."""
    override = os.environ.get("LANE_KEEPER_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "agent-tools" / "lane-keeper.toml"


def load_settings(path: Path | None = None) -> Settings:
    """The service's settings, read once and refused on anything missing.

    The pools and the profiles are the tables `lanes.toml` carries, read
    by the same reader, so a pool's cap is the same number wherever it
    is written down.
    """
    path = Path(path) if path is not None else settings_path()
    if not path.is_file():
        raise lane.Refusal("config", f"{path} does not exist")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise lane.Refusal("config", f"{path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise lane.Refusal("config", f"{path}: {exc}") from exc

    listen = lane.section(raw, "listen", path)
    host = listen.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        raise lane.Refusal("config", f"{path}: [listen] names no host")
    port = listen.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise lane.Refusal("config", f"{path}: [listen] port {port!r} is not "
                                    "a port number")

    database = raw.get("database")
    if not isinstance(database, str) or not database:
        raise lane.Refusal("config", f"{path} does not set database")

    pools = lane.parse_pools(raw, path)
    if not pools:
        raise lane.Refusal("config", f"{path} defines no pool")
    profiles = lane.parse_profiles(raw, path, pools)

    tokens = []
    for entry in raw.get("tokens", []):
        if not isinstance(entry, dict):
            raise lane.Refusal("config", f"{path}: a token is not a table")
        role = entry.get("role")
        if role not in ROLE_RANK:
            raise lane.Refusal("config", f"{path}: token role {role!r} is "
                                        "none of writer, reporter, reader")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}",
                                                           digest):
            raise lane.Refusal("config", f"{path}: a token's sha256 is not a "
                                        "64-character hex digest")
        tokens.append(Token(digest=digest.lower(), role=role))
    if not tokens:
        raise lane.Refusal("config", f"{path} defines no token")

    tls = lane.section(raw, "tls", path)
    cert = tls.get("cert")
    key = tls.get("key")
    if (cert is None) != (key is None):
        raise lane.Refusal("config", f"{path}: [tls] names a certificate "
                                    "without a key, or a key without a "
                                    "certificate")
    for name, value in (("cert", cert), ("key", key)):
        if value is not None and (not isinstance(value, str) or not value):
            raise lane.Refusal("config", f"{path}: [tls] {name} is not a path")

    web = lane.section(raw, "web", path).get("base", DEFAULT_WEB)
    if not isinstance(web, str) or not web:
        raise lane.Refusal("config", f"{path}: [web] base is not a URL")

    return Settings(
        path=path,
        host=host,
        port=port,
        database=Path(database).expanduser(),
        pools=pools,
        profiles=profiles,
        tokens=tuple(tokens),
        cert=Path(cert).expanduser() if cert else None,
        key=Path(key).expanduser() if key else None,
        web=web.rstrip("/"),
    )


# ----- the tokens ----------------------------------------------------------

def role_for(settings: Settings, header: str | None) -> str | None:
    """The role the bearer token carries, or None when it carries none.

    The token is compared by its digest.
    """
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    digest = hashlib.sha256(parts[1].strip().encode("utf-8")).hexdigest()
    role = None
    for token in settings.tokens:
        if not hmac.compare_digest(token.digest, digest):
            continue
        if role is None or ROLE_RANK[token.role] > ROLE_RANK[role]:
            role = token.role
    return role


# ----- the record ----------------------------------------------------------

class Store:
    """The record's database, one connection per thread.

    A SQLite connection belongs to the thread that made it, and the
    service answers on a thread per request.
    """

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        """This thread's connection to the record."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = lane.open_db(self.path)
            self._local.conn = conn
        return conn


class Keeper:
    """What the handler needs: the settings and the record."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.database)

    def now(self) -> dict:
        """The lanes that are live, and what each pool has taken."""
        conn = self.store.connection()
        moment = time.time()
        leases = [lane.lease_record(row, moment, None)
                  for row in lane.live_leases(conn)]
        pools = [{"pool": name,
                  "used": lane.pool_used(conn, name),
                  "size": size}
                 for name, size in self.settings.pools.items()]
        return {"now": moment, "leases": leases, "pools": pools}

    def history(self, limit: int) -> list[dict]:
        """The lanes that have ended, the newest first."""
        conn = self.store.connection()
        rows = conn.execute(
            "SELECT * FROM leases WHERE state = ? "
            "ORDER BY finished_at DESC, id DESC LIMIT ?",
            (lane.RELEASED, limit)).fetchall()
        moment = time.time()
        return [lane.lease_record(row, moment, None) for row in rows]


# ----- the page ------------------------------------------------------------

def human_age(seconds: float) -> str:
    """How long, in the units a person reads."""
    seconds = int(max(0.0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def clock(timestamp: float | None) -> str:
    """The time of day, as the page shows it."""
    if not timestamp:
        return "-"
    return time.strftime("%H:%M", time.localtime(timestamp))


def stamp(timestamp: float | None) -> str:
    """The day and the time, as the page shows it."""
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def issue_link(settings: Settings, repo: str, issue: int) -> str:
    """The issue, linked."""
    return (f"<a href=\"{html.escape(f'{settings.web}/{repo}/issues/{issue}')}\">"
            f"{html.escape(repo)}#{issue}</a>")


def pull_link(settings: Settings, repo: str, number: int | None,
              state: str | None) -> str:
    """The pull request, linked, with the state it is in."""
    if not number:
        return "<span class=\"muted\">-</span>"
    link = (f"<a href=\"{html.escape(f'{settings.web}/{repo}/pull/{number}')}\">"
            f"#{number}</a>")
    if state:
        link += f" <span class=\"muted\">{html.escape(str(state))}</span>"
    return link


def pool_bars(settings: Settings, pools: list[dict]) -> str:
    """One bar per pool: how much of it is taken."""
    bars = []
    for pool in pools:
        size = int(pool["size"])
        used = int(pool["used"])
        percent = int(used * 100 / size) if size else 0
        percent = max(0, min(100, percent))
        bars.append(
            "<div class=\"pool\">"
            f"<span class=\"name\">{html.escape(str(pool['pool']))}</span>"
            "<span class=\"bar\"><span class=\"fill\" "
            f"style=\"width:{percent}%\"></span></span>"
            f"<span class=\"muted\">{used} of {size}</span>"
            "</div>")
    return "".join(bars)


def now_section(settings: Settings, leases: list[dict],
                pools: list[dict]) -> str:
    """The lanes that are live."""
    rows = []
    for lease in leases:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(lease['id']))}</td>"
            f"<td>{issue_link(settings, str(lease['repo']), int(lease['issue']))}</td>"
            f"<td>{html.escape(str(lease['window']))}</td>"
            f"<td>{html.escape(str(lease['harness']))}"
            f"<span class=\"muted\">/{html.escape(str(lease['profile']))}</span></td>"
            "<td>"
            f"<span class=\"state\">{html.escape(str(lease['state']))}</span>"
            f"<span class=\"muted\">for {human_age(lease['state_age_seconds'])}</span>"
            f"<span class=\"muted\">as of {clock(lease['state_since'])}</span>"
            "</td>"
            f"<td>{pull_link(settings, str(lease['repo']), lease['pr'], lease['pr_state'])}</td>"
            "</tr>")
    if not rows:
        rows.append("<tr><td colspan=\"6\" class=\"muted\">no lane is live"
                    "</td></tr>")
    return (
        "<section id=\"now\">"
        "<h2>Now</h2>"
        f"<div class=\"pools\">{pool_bars(settings, pools)}</div>"
        "<table><thead><tr><th>ID</th><th>REPO#ISSUE</th><th>WINDOW</th>"
        "<th>HARNESS</th><th>STATE</th><th>PR</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</section>")


def history_section(settings: Settings, leases: list[dict]) -> str:
    """The lanes that have ended, the newest first."""
    rows = []
    for lease in leases:
        started = lease["created_at"]
        finished = lease["finished_at"]
        duration = ""
        if started and finished:
            duration = human_age(finished - started)
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(lease['id']))}</td>"
            f"<td>{issue_link(settings, str(lease['repo']), int(lease['issue']))}</td>"
            f"<td>{stamp(started)}</td>"
            f"<td>{stamp(finished)}</td>"
            f"<td>{html.escape(duration)}</td>"
            f"<td>{html.escape(str(lease['outcome'] or '-'))}</td>"
            f"<td>{pull_link(settings, str(lease['repo']), lease['pr'], lease['pr_state'])}</td>"
            "</tr>")
    if not rows:
        rows.append("<tr><td colspan=\"7\" class=\"muted\">no lane has ended"
                    "</td></tr>")
    return (
        "<section id=\"history\">"
        "<h2>History</h2>"
        "<table><thead><tr><th>ID</th><th>REPO#ISSUE</th><th>STARTED</th>"
        "<th>FINISHED</th><th>DURATION</th><th>OUTCOME</th><th>PR</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</section>")


STYLE = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 1.5rem; background: #fbfbfd; color: #16181d;
       font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 1.1rem; margin: 0; }
h2 { font-size: 0.8rem; margin: 0 0 0.6rem; text-transform: uppercase;
     letter-spacing: 0.08em; color: #5a6070; }
header { margin-bottom: 1.5rem; }
header p { margin: 0.3rem 0 0; }
section { margin-bottom: 2rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.35rem 0.7rem 0.35rem 0;
         border-bottom: 1px solid #d8dbe2; white-space: nowrap; }
th { font-weight: 600; color: #5a6070; }
a { color: #2b5fd9; text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: #6b7180; margin-left: 0.4rem; }
.state { font-weight: 600; }
.pools { margin-bottom: 0.8rem; }
.pool { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.3rem; }
.pool .name { width: 9rem; }
.pool .bar { flex: 1; height: 0.55rem; background: #e3e6ec; border-radius: 0.3rem;
             overflow: hidden; }
.pool .fill { display: block; height: 100%; background: #3b6fd4; }
.pool .muted { margin-left: 0; width: 6rem; }
@media (prefers-color-scheme: dark) {
  body { background: #14161b; color: #e6e8ec; }
  th, td { border-color: #2a2e37; }
  th, h2 { color: #9aa1ae; }
  a { color: #7ea6ff; }
  .muted { color: #9aa1ae; }
  .pool .bar { background: #262a33; }
  .pool .fill { background: #4d7ce0; }
}
"""


def dashboard(keeper: Keeper, limit: int) -> str:
    """The page: what is live now, and what has ended."""
    moment = keeper.now()
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta http-equiv=\"refresh\" content=\"30\">\n"
        "<title>lane-keeper</title>\n"
        f"<style>{STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        "<header><h1>lane-keeper</h1>"
        "<p class=\"muted\">The lane record, as it is now. This page "
        "refreshes itself every 30 seconds, and it changes nothing.</p>"
        "</header>\n"
        f"{now_section(keeper.settings, moment['leases'], moment['pools'])}\n"
        f"{history_section(keeper.settings, keeper.history(limit))}\n"
        "</body>\n"
        "</html>\n")


# ----- the handler ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """One request from a client.

    A missing or unknown token is 401, a token whose role forbids the
    request is 403, and a record that refuses it is 409 with the key
    and the detail `lane` prints today.
    """

    protocol_version = "HTTP/1.1"
    keeper: Keeper

    def log_message(self, *_a: object, **_k: object) -> None:
        """The access log stays empty.

        The dashboard reads the page every thirty seconds, and a log of
        every read says nothing.
        """

    def log_error(self, fmt: str, *args: object) -> None:
        """What went wrong, on the error stream."""
        print(f"lane-keeper: {fmt % args}", file=sys.stderr)

    def do_GET(self) -> None:  # noqa: N802 -- http.server contract
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 -- http.server contract
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802 -- http.server contract
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802 -- http.server contract
        self._handle("DELETE")

    # -- the request

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        try:
            # The body is read before anything else, so a connection kept
            # alive starts the next request at the right byte.
            body = self._body()
            role = role_for(self.keeper.settings,
                            self.headers.get("Authorization"))
            if role is None:
                self._json(401, {"key": "token",
                                 "detail": "the request names no token, or "
                                           "one this service does not know"})
                return
            if not self._allowed(role, method, path):
                self._json(403, {"key": "role",
                                 "detail": f"the {role} role may not "
                                           f"{method} {path}"})
                return
            if method == "GET":
                self._get(path, query)
            elif method == "POST":
                self._post(path, body)
            else:
                self._json(405, {"key": "method",
                                 "detail": f"{method} is not something this "
                                           "service does"})
        except BadRequest as exc:
            self._json(400, {"key": "request", "detail": exc.detail})
        except lane.Refusal as exc:
            self._json(409, {"key": exc.key, "detail": exc.detail})
        except Exception as exc:  # noqa: BLE001 -- the service must answer
            self.log_error("%s", exc)
            self._json(500, {"key": "error",
                             "detail": "the service could not answer"})

    def _allowed(self, role: str, method: str, path: str) -> bool:
        """Whether the role may make this request.

        Every role may read. A reporter says what a lane's pane shows
        and changes nothing else. A writer may do anything.
        """
        if method == "GET":
            return True
        if role == WRITER:
            return True
        if role == REPORTER:
            return bool(re.fullmatch(r"/api/lease/\d+/state", path))
        return False

    def _body(self) -> dict:
        """The request's body, as a table."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest(f"the body is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BadRequest("the body is not a table")
        return payload

    # -- the reads

    def _get(self, path: str, query: dict) -> None:
        if path == "/":
            self._html(200, dashboard(self.keeper, self._limit(query)))
            return
        if path == "/api/now":
            self._json(200, self.keeper.now())
            return
        if path == "/api/history":
            self._json(200, {"leases": self.keeper.history(self._limit(query))})
            return
        self._json(404, {"key": "path", "detail": f"{path} is not an endpoint"})

    def _limit(self, query: dict) -> int:
        """How many finished lanes the caller asks for."""
        raw = query.get("limit", [None])[0]
        if raw is None:
            return DEFAULT_LIMIT
        try:
            limit = int(raw)
        except ValueError:
            raise BadRequest(f"limit {raw!r} is not a number") from None
        return max(1, min(MAX_LIMIT, limit))

    # -- the writes

    def _post(self, path: str, body: dict) -> None:
        if path in ("/", "/api/now", "/api/history"):
            self._json(405, {"key": "method",
                             "detail": f"{path} is read only"})
            return
        if path == "/api/claim":
            self._claim(body)
            return
        match = re.fullmatch(r"/api/lease/(\d+)/([a-z]+)", path)
        if match is None:
            self._json(404, {"key": "path",
                             "detail": f"{path} is not an endpoint"})
            return
        lease_id = int(match.group(1))
        action = match.group(2)
        if action == "state":
            self._state(lease_id, body)
        elif action == "opened":
            self._opened(lease_id, body)
        elif action == "release":
            self._release(lease_id, body)
        elif action == "resume":
            self._resume(lease_id, body)
        elif action == "finish":
            self._finish(lease_id, body)
        elif action == "undo":
            self._undo(lease_id, body)
        else:
            self._json(404, {"key": "path",
                             "detail": f"{action} is not something a lease "
                                       "does"})

    def _claim(self, body: dict) -> None:
        """Take a lease, with the claim's own rules."""
        settings = self.keeper.settings
        name = _field(body, "profile")
        profile = settings.profiles.get(name)
        if profile is None:
            raise lane.Refusal("profile", f"no profile named {name!r}")
        issue = body.get("issue")
        if isinstance(issue, bool) or not isinstance(issue, int):
            raise lane.Refusal("request", "the claim names no issue")
        req = lane.Request(
            repo=_field(body, "repo"),
            issue=issue,
            harness=_field(body, "harness"),
            profile=profile.name,
            model=profile.model,
            pool=profile.pool,
            cost=profile.cost,
            # An empty branch is the record's to name: the default
            # carries the id of the lease, and the claim fills it in.
            branch=_optional(body, "branch"),
            base=_field(body, "base"),
            worktree=_field(body, "worktree"),
            clone=Path(_field(body, "clone")),
            tmux_session=_field(body, "tmux_session"),
            window=_field(body, "window"),
            session=_field(body, "session"),
            harness_session=_optional(body, "harness_session"),
            launch="",
        )
        conn = self.keeper.store.connection()
        lease_id, branch = lane.claim(conn, req,
                                      settings.pools[profile.pool])
        self._json(200, {"lease": lease_id, "branch": branch})

    def _state(self, lease_id: int, body: dict) -> None:
        """Record what a lane's pane says about it."""
        conn = self.keeper.store.connection()
        lease = self._lease(conn, lease_id)
        if lease["state"] == lane.RELEASED:
            raise lane.Refusal("lease", f"lease {lease_id} is released")
        state = _field(body, "state")
        opened_at = _number(body, "opened_at")
        lane.set_state(conn, lease_id, state, opened_at=opened_at)
        self._json(200, {"lease": lease_id, "state": state})

    def _opened(self, lease_id: int, body: dict) -> None:
        """Record that a lane's window is open."""
        conn = self.keeper.store.connection()
        self._lease(conn, lease_id)
        opened = lane.record_window_opened(conn, lease_id,
                                           _number(body, "opened_at"))
        self._json(200, {"lease": lease_id, "window_opened_at": opened})

    def _release(self, lease_id: int, body: dict) -> None:
        """End a lease, and write how it ended."""
        conn = self.keeper.store.connection()
        lease = self._lease(conn, lease_id)
        outcome = self._end(conn, lease, lane.OUTCOME_RELEASED,
                            _integer(body, "pr"), _optional(body, "pr_state"))
        self._json(200, {"lease": lease_id, "outcome": outcome})

    def _resume(self, lease_id: int, body: dict) -> None:
        """Take a pool unit back for a stopped lane."""
        conn = self.keeper.store.connection()
        lease = self._lease(conn, lease_id)
        profile = self.keeper.settings.profiles.get(lease["profile"])
        if profile is None:
            raise lane.Refusal("profile",
                               f"lease {lease_id} names profile "
                               f"{lease['profile']!r}, which this service does "
                               "not define")
        lane.claim_resume(conn, lease, profile,
                          self.keeper.settings.pools[profile.pool])
        self._json(200, {"lease": lease_id, "state": "busy"})

    def _finish(self, lease_id: int, body: dict) -> None:
        """Record that a lane has finished, and how it ended."""
        conn = self.keeper.store.connection()
        lease = self._lease(conn, lease_id)
        outcome = body.get("outcome")
        if outcome not in (lane.OUTCOME_MERGED, lane.OUTCOME_ABANDONED):
            raise lane.Refusal("request",
                               f"outcome {outcome!r} is neither merged nor "
                               "abandoned")
        outcome = self._end(conn, lease, outcome,
                            _integer(body, "pr"), _optional(body, "pr_state"))
        self._json(200, {"lease": lease_id, "outcome": outcome})

    def _end(self, conn, lease, outcome: str, pr: int | None,
             pr_state: str | None) -> str:
        """Mark a lease ended, and write how it ended -- once.

        A lane that is already over keeps the ending it was given: a
        second report of an ending leaves the first alone.
        """
        if lease["state"] == lane.RELEASED:
            return lease["outcome"] or outcome
        lane.set_state(conn, lease["id"], lane.RELEASED)
        lane.record_history(conn, lease["id"], outcome, pr, pr_state)
        return outcome

    def _undo(self, lease_id: int, body: dict) -> None:
        """Undo the claim: the lease row goes."""
        conn = self.keeper.store.connection()
        self._lease(conn, lease_id)
        lane.undo_claim(conn, lease_id)
        self._json(200, {"lease": lease_id, "undone": True})

    def _lease(self, conn, lease_id: int):
        """The lease a request names, or a refusal naming it."""
        lease = conn.execute("SELECT * FROM leases WHERE id = ?",
                             (lease_id,)).fetchone()
        if lease is None:
            raise lane.Refusal("lease", f"no lease {lease_id}")
        return lease

    # -- the answers

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _field(body: dict, name: str) -> str:
    """A field of the body that the request cannot go without."""
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise lane.Refusal("request", f"the request names no {name}")
    return value


def _optional(body: dict, name: str) -> str:
    """A field of the body that may be absent."""
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise lane.Refusal("request", f"{name} is not a string")
    return value


def _number(body: dict, name: str) -> float | None:
    """A field of the body that is a number, or nothing."""
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise lane.Refusal("request", f"{name} is not a number")
    return float(value)


def _integer(body: dict, name: str) -> int | None:
    """A field of the body that is a whole number, or nothing."""
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise lane.Refusal("request", f"{name} is not a whole number")
    return value


# ----- the service ---------------------------------------------------------

def make_server(settings: Settings,
               keeper: Keeper | None = None) -> ThreadingHTTPServer:
    """The server, listening.

    With a certificate and a key the server speaks HTTPS: the socket is
    wrapped before anything is answered on it.
    """
    keeper = keeper if keeper is not None else Keeper(settings)
    # The record is created and brought up to date before the server
    # answers, so two requests cannot both add a column.
    keeper.store.connection().close()
    handler = type("BoundHandler", (Handler,), {"keeper": keeper})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    server.daemon_threads = True
    if settings.cert and settings.key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(settings.cert), str(settings.key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve(settings: Settings) -> int:
    """Run the service."""
    server = make_server(settings)
    scheme = "https" if settings.cert and settings.key else "http"
    print(f"==> lane-keeper on {scheme}://{settings.host}:"
          f"{server.server_address[1]}, record {settings.database}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lane-keeper",
        description="Own the lane record, and show it.",
    )
    parser.add_argument("--config", default=None,
                        help="the settings file (default $LANE_KEEPER_CONFIG)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(Path(args.config) if args.config else None)
    except lane.Refusal as exc:
        print(f"ERR {exc.key}: {exc.detail}", file=sys.stderr)
        return 2
    return serve(settings)


if __name__ == "__main__":
    sys.exit(main())
