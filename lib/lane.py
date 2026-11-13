#!/usr/bin/env python3
"""Own lane state in one SQLite database, so two sessions cannot both take a lane.

A lane is one window running one harness against one issue, and the
lease is the only record of it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

# The label that says a lane is live on an issue.
LABEL = "lane:active"

# A released lease holds no pool unit and blocks no issue. A stopped
# one holds no pool unit either -- its window is gone -- but it still
# holds its issue, so a second lane cannot start there.
RELEASED = "released"
STOPPED = "stopped"

# The character a runaway harness repeats when it floods the pane.
FLOOD_CHAR = "⇒"

# The line hermes prints when it stops at its iteration budget.
BUDGET_LINE = "Iteration budget reached"

# The approval choice hermes offers.
APPROVAL_LINE = "Allow once"

# The spinner pi prints while it works.
WORKING_LINE = "Working"

# How many lines at the bottom of a pane carry the harness's status line,
# and so the model the lane runs on. The prompt and its rules sit below
# it, so the window is wider than the prompt's own.
FOOTER_LINES = 6

# How many lines at the bottom of a pane carry hermes's prompt.
PROMPT_LINES = 4

# How many lines at the bottom of a pane carry pi's spinner.
WORKING_LINES = 6

# How long a spawn waits after starting a harness before it reports the
# window: the settle before the coordinator types into the lane.
DEFAULT_READY_SECONDS = 8

# How long `lane list` waits between the two captures it reads a pi lane
# with: one capture cannot tell a spinner between frames from an idle lane.
DEFAULT_PI_SETTLE = 5.0

# How long a signalled process group has to go before it is killed.
KILL_GRACE = 10.0


class Refusal(Exception):
    """A condition the tool cannot honor.

    `key` names what refused -- `pool`, `lease`, `profile`, `config` --
    so the message says which thing to change.
    """

    def __init__(self, key: str, detail: str):
        super().__init__(detail)
        self.key = key
        self.detail = detail


class StepFailure(Exception):
    """A side effect of a spawn that failed.

    `step` names the step, so the message says which one broke.
    """

    def __init__(self, step: str, detail: str):
        super().__init__(detail)
        self.step = step
        self.detail = detail


class CommandError(Exception):
    """A command that exited non-zero."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        super().__init__(stderr)
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr

    def detail(self) -> str:
        """Why the command failed, in one line."""
        lines = [line for line in (self.stderr or "").splitlines() if line.strip()]
        if lines:
            return lines[0]
        return f"{' '.join(self.cmd)} exited {self.returncode}"


class _Forwarded(Exception):
    """A signal the tool received and passed to its child."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


@contextmanager
def _forwarded_signals(child: subprocess.Popen):
    """Pass a signal to `child` while it runs.

    A wrapper that dies without its child leaves it running: a `git
    worktree add` that outlives the tool holds the clone's lock.
    """
    def handler(signum, _frame):
        if child.poll() is None:
            try:
                child.send_signal(signum)
            except OSError:
                pass
        raise _Forwarded(signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def run(cmd: list[str], env: dict | None = None,
        cwd: Path | None = None) -> tuple[int, str, str]:
    """Run one command and return its exit status, stdout and stderr.

    The child is waited for, and a signal to this process reaches it
    first. `cwd` is the directory the child starts in.
    """
    try:
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env,
                                 cwd=str(cwd) if cwd is not None else None)
    except OSError as exc:
        raise CommandError(cmd, 127, str(exc)) from exc
    try:
        with _forwarded_signals(child):
            out, err = child.communicate()
    except _Forwarded as forwarded:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
        raise SystemExit(128 + forwarded.signum) from None
    return child.returncode, out, err


def tmux_env() -> dict:
    """The environment tmux runs with.

    The agent host keeps its sockets in its own directory. A tmux that
    finds no socket starts a second server, and a second server sees no
    windows, so every lane would read as gone.
    """
    env = dict(os.environ)
    env.setdefault("TMUX_TMPDIR", os.path.expanduser("~/.tmux-sockets"))
    return env


# ----- settings ------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """A profile: which pool it draws from, what it costs, what it runs on."""

    name: str
    pool: str
    cost: int
    model: str


@dataclass(frozen=True)
class Harness:
    """A harness: the command that starts it in a window."""

    name: str
    launch: str


@dataclass(frozen=True)
class Repo:
    """A repository and the checkout its worktrees are cut from."""

    name: str
    clone: Path


@dataclass(frozen=True)
class Config:
    """The operator's settings, read once and refused on anything missing."""

    path: Path
    tmux_session: str
    worktree_root: Path | None
    ready_seconds: float
    pools: dict[str, int]
    profiles: dict[str, Profile]
    harnesses: dict[str, Harness]
    repos: dict[str, Repo]

    def profile(self, name: str) -> Profile:
        """The named profile, or a refusal naming it."""
        found = self.profiles.get(name)
        if found is None:
            raise Refusal("profile",
                          f"{self.path} defines no profile named {name!r}")
        return found

    def harness(self, name: str) -> Harness:
        """The named harness, or a refusal naming it."""
        found = self.harnesses.get(name)
        if found is None:
            raise Refusal("harness",
                          f"{self.path} defines no harness named {name!r}")
        return found

    def repo(self, name: str) -> Repo:
        """The named repository, or a refusal naming it."""
        found = self.repos.get(name)
        if found is None:
            raise Refusal("repo",
                          f"{self.path} defines no repository named {name!r}")
        return found


def config_path() -> Path:
    """Where the settings live."""
    override = os.environ.get("LANE_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("AGENT_TOOLS_CONFIG")
    if base:
        return Path(base).expanduser() / "lanes.toml"
    return Path.home() / ".config" / "agent-tools" / "lanes.toml"


def db_path() -> Path:
    """Where the lane database lives."""
    override = os.environ.get("LANE_DB")
    if override:
        return Path(override).expanduser()
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "agent-tools" / "lanes.db"


def section(raw: dict, name: str, path: Path) -> dict:
    """One table of the settings.

    The service's own settings file carries tables of the same names,
    so the one reader reads both.
    """
    body = raw.get(name, {})
    if not isinstance(body, dict):
        raise Refusal("config", f"{path}: [{name}] is not a table")
    return body


def load_config(path: Path | None = None) -> Config:
    """The operator's settings.

    Anything the tool cannot act on is a refusal naming the key. A
    default would be a guess about which machine is running, and a guess
    about a pool is a guess about a cap.
    """
    path = Path(path) if path is not None else config_path()
    if not path.is_file():
        raise Refusal("config", f"{path} does not exist")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Refusal("config", f"{path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise Refusal("config", f"{path}: {exc}") from exc

    tmux_session = raw.get("tmux_session")
    if not isinstance(tmux_session, str) or not tmux_session:
        raise Refusal("config", f"{path} does not set tmux_session")
    root = raw.get("worktree_root")
    if root is not None and not isinstance(root, str):
        raise Refusal("config", f"{path}: worktree_root is not a path")
    ready = raw.get("ready_seconds", DEFAULT_READY_SECONDS)
    if isinstance(ready, bool) or not isinstance(ready, (int, float)):
        raise Refusal("config", f"{path}: ready_seconds is not a number")

    pools = parse_pools(raw, path)
    profiles = parse_profiles(raw, path, pools)

    harnesses = {}
    for name, body in section(raw, "harnesses", path).items():
        if not isinstance(body, dict):
            raise Refusal("harness", f"harness {name} is not a table")
        launch = body.get("launch")
        if not isinstance(launch, str) or not launch:
            raise Refusal("harness", f"harness {name} does not name a launch command")
        if "{session}" not in launch:
            raise Refusal("harness",
                          f"harness {name} does not fill {{session}}, so a "
                          f"lane started with it can never be resumed")
        harnesses[name] = Harness(name, launch)

    repos = {}
    for name, body in section(raw, "repos", path).items():
        if not isinstance(body, dict):
            raise Refusal("repo", f"repository {name} is not a table")
        clone = body.get("clone")
        if not isinstance(clone, str) or not clone:
            raise Refusal("repo", f"repository {name} does not name a clone")
        repos[name] = Repo(name, Path(clone).expanduser())

    return Config(
        path=path,
        tmux_session=tmux_session,
        worktree_root=Path(root).expanduser() if root else None,
        ready_seconds=float(ready),
        pools=pools,
        profiles=profiles,
        harnesses=harnesses,
        repos=repos,
    )


def parse_pools(raw: dict, path: Path) -> dict[str, int]:
    """The pools: how many units each one has.

    Only a positive whole number is a size; anything else is refused,
    because a default would be a guess about a cap.
    """
    pools = {}
    for name, size in section(raw, "pools", path).items():
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise Refusal("pool",
                          f"pool {name} has size {size!r}, which is not a "
                          "positive whole number")
        pools[name] = size
    return pools


def parse_profiles(raw: dict, path: Path,
                   pools: dict[str, int]) -> dict[str, Profile]:
    """The profiles: which pool each draws from, what it costs, what it runs.

    A profile may name only a pool the file defines: what a profile costs
    is known against the pool it draws from.
    """
    profiles = {}
    for name, body in section(raw, "profiles", path).items():
        if not isinstance(body, dict):
            raise Refusal("profile", f"profile {name} is not a table")
        pool = body.get("pool")
        cost = body.get("cost")
        model = body.get("model")
        if not isinstance(pool, str) or not pool:
            raise Refusal("profile", f"profile {name} does not name a pool")
        if pool not in pools:
            raise Refusal("pool",
                          f"profile {name} names pool {pool!r}, which {path} "
                          "does not define")
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 1:
            raise Refusal("profile",
                          f"profile {name} has cost {cost!r}, which is not a "
                          "positive whole number")
        if not isinstance(model, str) or not model:
            raise Refusal("profile", f"profile {name} does not name a model")
        try:
            re.compile(model)
        except re.error as exc:
            raise Refusal("profile",
                          f"profile {name} has model {model!r}, which is not "
                          f"a regular expression: {exc}") from exc
        profiles[name] = Profile(name, pool, cost, model)
    return profiles

# ----- the database --------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    issue INTEGER NOT NULL,
    harness TEXT NOT NULL,
    profile TEXT NOT NULL,
    model TEXT NOT NULL,
    pool TEXT NOT NULL,
    cost INTEGER NOT NULL,
    branch TEXT NOT NULL,
    base TEXT NOT NULL,
    base_sha TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL,
    clone TEXT NOT NULL,
    tmux_session TEXT NOT NULL,
    window TEXT NOT NULL,
    window_opened_at REAL NOT NULL DEFAULT 0,
    session TEXT NOT NULL,
    harness_session TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    state_since REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS leases_by_state ON leases(state);
CREATE INDEX IF NOT EXISTS leases_by_pool ON leases(pool);
"""


def _add_column(conn: sqlite3.Connection, table: str, column: str,
                spec: str) -> None:
    """Add a column that a database from an earlier version lacks.

    The schema is created with CREATE TABLE IF NOT EXISTS, so a database
    that already carries the table keeps the shape it was made with. Two
    openers can want the same column at once, and the second one's
    refusal by SQLite is harmless: the column it wanted is there.
    """
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """The lane database, created on first use.

    The journal mode stays as SQLite made it. Changing it needs a lock
    that a second process opening the database at the same moment does
    not hold.
    """
    path = Path(path) if path is not None else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    _add_column(conn, "leases", "window_opened_at", "REAL NOT NULL DEFAULT 0")
    _add_column(conn, "leases", "harness_session", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "leases", "finished_at", "REAL")
    _add_column(conn, "leases", "outcome", "TEXT")
    _add_column(conn, "leases", "pr", "INTEGER")
    _add_column(conn, "leases", "pr_state", "TEXT")
    return conn


def live_leases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The leases that are live."""
    return conn.execute(
        "SELECT * FROM leases WHERE state != ? ORDER BY id", (RELEASED,)
    ).fetchall()


def pool_used(conn: sqlite3.Connection, pool: str) -> int:
    """The units a pool has taken.

    A released lane holds no unit and a stopped one holds none either:
    its window is gone. This is the sum the claim applies, read the
    same way.
    """
    filling = conn.execute(
        "SELECT cost FROM leases WHERE state NOT IN (?, ?) AND pool = ?",
        (RELEASED, STOPPED, pool)).fetchall()
    return sum(row["cost"] for row in filling)


def set_state(conn: sqlite3.Connection, lease_id: int, state: str,
              now: float | None = None,
              opened_at: float | None = None) -> None:
    """Record a lease's state, and when it last changed.

    The time is only moved when the state actually changes: an age that
    restarts on every read says nothing about how long the lane has been
    in that state.

    `opened_at` is the window's opening time the caller read: the write
    lands only while the row still carries it. A reconcile that read the
    row before a window opened therefore leaves the lane that is starting
    alone.
    """
    now = time.time() if now is None else now
    if opened_at is None:
        conn.execute(
            "UPDATE leases SET state = ?, state_since = ? WHERE id = ? "
            "AND state != ?",
            (state, now, lease_id, state),
        )
        return
    conn.execute(
        "UPDATE leases SET state = ?, state_since = ? WHERE id = ? "
        "AND state != ? AND window_opened_at = ?",
        (state, now, lease_id, state, opened_at),
    )


# ----- the history ---------------------------------------------------------

# How a lease ended. The state says the lane is over; the outcome says
# what it was.
OUTCOME_MERGED = "merged"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_RELEASED = "released"


def record_history(conn: sqlite3.Connection, lease_id: int, outcome: str,
                   pr: int | None = None, pr_state: str | None = None,
                   finished_at: float | None = None) -> None:
    """Write what a lease's ending was.

    The pull request is written only when it is known; a lane released
    without one leaves the columns empty.
    """
    now = time.time() if finished_at is None else finished_at
    conn.execute(
        "UPDATE leases SET finished_at = ?, outcome = ?, "
        "pr = COALESCE(?, pr), pr_state = COALESCE(?, pr_state) "
        "WHERE id = ?", (now, outcome, pr, pr_state, lease_id))


def record_window_opened(conn: sqlite3.Connection, lease_id: int,
                         opened_at: float | None = None) -> float:
    """Record when a lease's window opened, and say when.

    The window is open from here on, so a reconcile that cannot see it
    knows the lane stopped.
    """
    now = time.time() if opened_at is None else opened_at
    conn.execute("UPDATE leases SET window_opened_at = ? WHERE id = ?",
                 (now, lease_id))
    return now


def undo_claim(conn: sqlite3.Connection, lease_id: int) -> None:
    """Take a claim back: the lease goes, as it never was."""
    conn.execute("DELETE FROM leases WHERE id = ?", (lease_id,))


# ----- the claim ------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """What a spawn asks for."""

    repo: str
    issue: int
    harness: str
    profile: str
    model: str
    pool: str
    cost: int
    branch: str
    base: str
    worktree: str
    clone: Path
    tmux_session: str
    window: str
    session: str
    harness_session: str
    launch: str


def refuse_taken_places(conn: sqlite3.Connection, req: Request) -> None:
    """Refuse a spawn whose place is already taken by something else.

    The claim refuses the places a live lease holds and names the
    holder. These are the places nothing leased: a worktree, a branch
    or a window somebody else made.
    """
    if Path(req.worktree).exists() and not _leased(conn, "worktree",
                                                   req.worktree):
        raise Refusal("worktree", f"worktree {req.worktree} already exists")
    # A branch still to be named takes no place: the default carries the
    # id of a lease not yet claimed.
    if req.branch and _branch_exists(req.clone, req.branch) \
            and not _leased(conn, "branch", req.branch):
        raise Refusal("branch",
                      f"branch {req.branch} already exists in {req.clone}")
    if window_exists_at(req.tmux_session, req.window) and not _leased(
            conn, "window", req.window, session=req.tmux_session):
        raise Refusal("window",
                      f"window {req.tmux_session}:{req.window} is already open")


def _leased(conn: sqlite3.Connection, column: str, value: str,
            session: str | None = None) -> bool:
    """Whether a live lease holds a place.

    The column names the field, and only the fields the leases record.
    """
    if column == "window":
        row = conn.execute(
            "SELECT id FROM leases WHERE state != ? AND tmux_session = ? "
            "AND window = ?", (RELEASED, session, value)).fetchone()
    else:
        row = conn.execute(
            f"SELECT id FROM leases WHERE state != ? AND {column} = ?",
            (RELEASED, value)).fetchone()
    return row is not None


def _branch_exists(clone: Path, branch: str) -> bool:
    """Whether the clone already carries a branch."""
    code, _, _ = run(["git", "-C", str(clone), "rev-parse", "--verify",
                      "--quiet", f"refs/heads/{branch}"])
    return code == 0


def default_branch(issue: int, lease_id: int) -> str:
    """The branch a lease works on, when none was given.

    The lease id keeps the next lane on an issue off an earlier part's
    branch, and off its pull request. A branch then names the lease that
    owns it.
    """
    return f"lane/{issue}-{lease_id}"


def claim(conn: sqlite3.Connection, req: Request,
          size: int) -> tuple[int, str]:
    """Take the lease, or refuse, and say which branch it works on.

    The claim is one transaction, taken before anything outside the
    database happens: a check that reads and then writes lets two callers
    pass it, and a unique index cannot sum a pool's costs.

    The default branch carries the id the INSERT produces, so the row
    goes in blank and is filled before the COMMIT: no reader sees a
    lease without one.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        holder = conn.execute(
            "SELECT id, window, session FROM leases WHERE state != ? "
            "AND repo = ? AND issue = ?",
            (RELEASED, req.repo, req.issue),
        ).fetchone()
        if holder is not None:
            raise Refusal(
                "lease",
                f"{req.repo}#{req.issue} is leased by lease {holder['id']} "
                f"(window {holder['window']}, session {holder['session']})")
        holder = conn.execute(
            "SELECT id, window, session FROM leases WHERE state != ? "
            "AND worktree = ?",
            (RELEASED, req.worktree),
        ).fetchone()
        if holder is not None:
            raise Refusal(
                "lease",
                f"worktree {req.worktree} is leased by lease {holder['id']} "
                f"(window {holder['window']}, session {holder['session']})")
        holder = conn.execute(
            "SELECT id, window, session FROM leases WHERE state != ? "
            "AND tmux_session = ? AND window = ?",
            (RELEASED, req.tmux_session, req.window),
        ).fetchone()
        if holder is not None:
            raise Refusal(
                "lease",
                f"window {req.tmux_session}:{req.window} is leased by lease "
                f"{holder['id']} (window {holder['window']}, "
                f"session {holder['session']})")
        filling = conn.execute(
            "SELECT id, window, session, cost FROM leases "
            "WHERE state NOT IN (?, ?) AND pool = ? ORDER BY id",
            (RELEASED, STOPPED, req.pool),
        ).fetchall()
        used = sum(row["cost"] for row in filling)
        if used + req.cost > size:
            holders = ", ".join(
                f"lease {row['id']} (window {row['window']}, "
                f"session {row['session']}) holds {row['cost']}"
                for row in filling) or "no lease"
            raise Refusal(
                "pool",
                f"pool {req.pool} is full: {used} of {size} units are taken "
                f"and this profile costs {req.cost}; {holders}")
        cursor = conn.execute(
            "INSERT INTO leases (repo, issue, harness, profile, model, pool, "
            "cost, branch, base, base_sha, worktree, clone, tmux_session, "
            "window, session, harness_session, state, state_since, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (req.repo, req.issue, req.harness, req.profile, req.model,
             req.pool, req.cost, req.branch, req.base, "", req.worktree,
             str(req.clone), req.tmux_session, req.window, req.session,
             req.harness_session, "busy", time.time(), time.time()),
        )
        lease_id = int(cursor.lastrowid)
        branch = req.branch
        if not branch:
            branch = default_branch(req.issue, lease_id)
            conn.execute("UPDATE leases SET branch = ? WHERE id = ?",
                         (branch, lease_id))
        conn.execute("COMMIT")
        return lease_id, branch
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ----- the window -----------------------------------------------------------

def window_target(lease: sqlite3.Row) -> str:
    """The tmux target of a lease's window."""
    return f"{lease['tmux_session']}:{lease['window']}"


def window_exists_at(session: str, window: str) -> bool:
    """Whether a window is open in a session.

    The window is the fact. The process behind it may have died and the
    window may still be open, and the other way round, so the window is
    what is asked about.
    """
    code, out, _ = run(
        ["tmux", "list-windows", "-t", f"{session}:", "-F", "#{window_name}"],
        env=tmux_env(),
    )
    if code != 0:
        return False
    return window in out.splitlines()


def window_exists(lease: sqlite3.Row) -> bool:
    """Whether the lease's window is still there."""
    return window_exists_at(lease["tmux_session"], lease["window"])


def capture_pane(lease: sqlite3.Row) -> str:
    """The lease's window, as text."""
    code, out, _ = run(
        ["tmux", "capture-pane", "-p", "-t", window_target(lease)],
        env=tmux_env(),
    )
    return out if code == 0 else ""


def launch_text(launch: str, profile: str, window: str, session: str) -> str:
    """The command that starts a harness.

    The session id is the harness's own name for the conversation, so
    the same command starts it and brings it back.
    """
    return (launch
            .replace("{profile}", profile)
            .replace("{window}", window)
            .replace("{session}", session))


def send_launch(target: str, text: str) -> None:
    """Type a command into a window and press Enter.

    The text goes in with `-l`, and the Enter is a separate call: with
    `-l` a trailing Enter is typed as the five letters, not the key.
    """
    for keys in (["-l", text], ["Enter"]):
        code, _, err = run(["tmux", "send-keys", "-t", target, *keys],
                           env=tmux_env())
        if code != 0:
            raise StepFailure("launch", CommandError(["tmux", "send-keys"],
                                                      code, err).detail())


def tail(lines: list[str], count: int) -> list[str]:
    """The last `count` lines that say something."""
    return [line for line in lines if line.strip()][-count:]


def model_in_footer(lines: list[str], model: str) -> bool:
    """Whether the pane's status line names the profile's model.

    A lane that fell back to another model says so in its status line,
    and that is the only place it says it.
    """
    pattern = re.compile(model)
    return any(pattern.search(line) for line in tail(lines, FOOTER_LINES))


def is_flood(capture: str) -> bool:
    """Whether the pane is full of one repeated character.

    The character on its own is no flood: a lane reading a file that
    quotes it sees it on the screen.
    """
    lines = [line for line in capture.splitlines() if line.strip()]
    repeats = [line for line in lines if set(line.strip()) == {FLOOD_CHAR}]
    return len(repeats) >= 3


def pane_state(harness: str, profile: str, model: str,
               captures: list[str]) -> str:
    """What a lane's pane says about it.

    A flood and a dialog waiting for an answer come first, then a lane
    that stopped working, then one on the wrong model, and only then
    whether it is waiting.
    """
    if is_flood(captures[-1]):
        return "flood"
    lines = captures[-1].splitlines()
    if not tail(lines, 1):
        # The pane has nothing on it: the harness has not drawn yet, and
        # a pane that says nothing says nothing about the model.
        return "busy"
    if harness == "pi":
        working = any(
            WORKING_LINE in line
            for capture in captures
            for line in tail(capture.splitlines(), WORKING_LINES)
        )
        if not model_in_footer(lines, model):
            return "fallback"
        return "busy" if working else "idle"
    if any(APPROVAL_LINE in line for line in lines[-20:]):
        return "approval"
    idle = any(f"{profile} ❯" in line for line in tail(lines, PROMPT_LINES))
    if idle and any(BUDGET_LINE in line for line in lines[-10:]):
        return "budget"
    if not model_in_footer(lines, model):
        return "fallback"
    return "idle" if idle else "busy"


def pi_settle() -> float:
    """How long two captures of a pi lane are apart."""
    raw = os.environ.get("LANE_PI_SETTLE")
    if raw is None:
        return DEFAULT_PI_SETTLE
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_PI_SETTLE


def reconcile(conn: sqlite3.Connection) -> None:
    """Mark a lease whose window is gone as stopped.

    Every command reads the leases first, so a closed window is noticed
    by the next command. A lease still opening its window keeps its
    state, and so does one whose window opened after this read.
    """
    for lease in live_leases(conn):
        if not lease["window_opened_at"]:
            continue
        if not window_exists(lease):
            set_state(conn, lease["id"], STOPPED,
                      opened_at=lease["window_opened_at"])


def read_states(conn: sqlite3.Connection, leases: list[sqlite3.Row]) -> None:
    """Read every live lease's state from its window and record it.

    The pane says what the lane is doing. A process match cannot say any
    of that, and `pgrep -f` matches the reader's own command line.
    """
    live = [lease for lease in leases if lease["state"] != RELEASED]
    first = {lease["id"]: capture_pane(lease) for lease in live}
    pi_leases = [lease for lease in live if lease["harness"] == "pi"]
    if pi_leases:
        # One capture cannot tell a spinner between frames from an idle
        # lane, so a pi lane is read twice.
        time.sleep(pi_settle())
    for lease in live:
        if lease["state"] == "stopped" and not window_exists(lease):
            # A stopped lane stays stopped.
            continue
        captures = [first[lease["id"]]]
        if lease["harness"] == "pi":
            captures.append(capture_pane(lease))
        state = pane_state(lease["harness"], lease["profile"],
                           lease["model"], captures)
        set_state(conn, lease["id"], state)


# ----- the pull request ----------------------------------------------------

@dataclass(frozen=True)
class PullRequest:
    """The pull request a branch carries."""

    number: int
    state: str
    head: str


# The states a pull request is in.
MERGED = "MERGED"
CLOSED = "CLOSED"


def pr_opened_at(record: dict) -> float | None:
    """When a pull request record says it opened, in epoch seconds.

    None when it says nothing, and an unknown age filters nothing out.
    """
    value = str(record.get("createdAt") or "")
    try:
        opened = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return opened.timestamp()


def find_pr(repo: str, branch: str,
            since: float | None = None) -> PullRequest | None:
    """The pull request whose head is the branch.

    None says there is none: the branch was never pushed, or its pull
    request was deleted.

    `since` is when a lease began: a pull request opened before it
    belongs to whoever held the branch name first.
    """
    code, out, err = run(["gh", "pr", "list", "-R", repo, "--head", branch,
                          "--state", "all", "--json",
                          "number,state,headRefOid,mergedAt,createdAt",
                          "--limit", "1"])
    if code != 0:
        raise StepFailure("pr", CommandError(["gh", "pr", "list"], code,
                                             err).detail())
    try:
        records = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise StepFailure("pr", f"gh pr list answered with something that is "
                                 f"not JSON: {exc}")
    if not records:
        return None
    record = records[0]
    opened = pr_opened_at(record)
    # `gh` lists newest first, so a record older than the lease leaves
    # nothing newer. Its stamp is whole seconds, so the lease rounds
    # down: a lane that pushed in the second it was claimed in pushed
    # after the claim.
    if since is not None and opened is not None and opened < int(since):
        return None
    return PullRequest(number=int(record.get("number") or 0),
                       state=str(record.get("state") or ""),
                       head=str(record.get("headRefOid") or ""))


# ----- the processes a lane leaves behind -----------------------------------

def process_alive(pid: int) -> bool:
    """Whether a process is still running.

    A zombie has exited and is only waiting to be reaped, and it holds
    nothing.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            data = handle.read()
    except OSError:
        return False
    end = data.rfind(")")
    if end < 0:
        return False
    fields = data[end + 2:].split()
    return bool(fields) and fields[0] != "Z"


def lane_processes(worktree: Path) -> list[tuple[int, int]]:
    """The processes working inside the worktree, with their groups.

    The working directory is the fact. A pattern over command lines
    matches this tool's own command and every process that merely names
    the path.
    """
    root = os.path.realpath(str(worktree))
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                continue
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        real = os.path.realpath(cwd)
        if real != root and not real.startswith(root + os.sep):
            continue
        try:
            group = os.getpgid(pid)
        except OSError:
            continue
        found.append((pid, group))
    return found


def stop_lane_processes(worktree: Path) -> list[str]:
    """Stop the processes the lane left running, and say which.

    The group goes, not the process: a reparented `gate-verify` holds
    the builder's bench lock, and a TERM to the gate takes its group
    down.
    """
    groups: dict[int, list[int]] = {}
    mine = os.getpgrp()
    for pid, group in lane_processes(worktree):
        # This tool's own group is skipped: signalling it kills this tool.
        if group <= 1 or group == mine:
            continue
        groups.setdefault(group, []).append(pid)
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + KILL_GRACE
    while time.time() < deadline:
        if not any(process_alive(pid)
                   for pids in groups.values() for pid in pids):
            break
        time.sleep(0.1)
    for group, pids in groups.items():
        if any(process_alive(pid) for pid in pids):
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
    return [f"stopped pid {pid} in group {group}"
            for group, pids in groups.items() for pid in pids]


# ----- the side effects of a spawn ------------------------------------------

class Spawn:
    """The side effects of one spawn, in order, each with its undo."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config,
                 req: Request, lease_id: int):
        self.conn = conn
        self.cfg = cfg
        self.req = req
        self.lease_id = lease_id
        self.undone: list[str] = []
        self.label_added = False
        self.worktree_cut = False
        self.branch_cut = False
        self.window_opened = False

    def _gh(self, *args: str) -> str:
        code, out, err = run(["gh", *args])
        if code != 0:
            raise CommandError(["gh", *args], code, err)
        return out

    def _git(self, directory: Path, *args: str) -> str:
        code, out, err = run(["git", "-C", str(directory), *args])
        if code != 0:
            raise CommandError(["git", "-C", str(directory), *args], code, err)
        return out

    def _tmux(self, *args: str) -> str:
        code, out, err = run(["tmux", *args], env=tmux_env())
        if code != 0:
            raise CommandError(["tmux", *args], code, err)
        return out

    # -- the steps

    def label(self) -> None:
        """Put `lane:active` on the issue.

        The label is created first: adding a label the repository does
        not have fails, and a lane whose issue carries no label cannot be
        found by anyone reading the issue.
        """
        try:
            self._gh("label", "create", LABEL, "--repo", self.req.repo,
                     "--force")
            # The add may land before the call that carries it is cut
            # short, so from here the label is on the issue.
            self.label_added = True
            self._gh("issue", "edit", str(self.req.issue), "--repo",
                     self.req.repo, "--add-label", LABEL)
        except CommandError as exc:
            raise StepFailure("label", exc.detail()) from exc

    def undo_label(self) -> None:
        """Take the label back off."""
        if not self.label_added:
            return
        try:
            self._gh("issue", "edit", str(self.req.issue), "--repo",
                     self.req.repo, "--remove-label", LABEL)
        except CommandError as exc:
            self.undone.append(f"label: {exc.detail()}")

    def worktree(self) -> None:
        """Cut the worktree the lane works in.

        The commit the worktree is cut from is recorded before the cut:
        a worktree reused later may sit on another commit, and the lease
        has to say which one it was given.
        """
        try:
            self._git(self.req.clone, "fetch", "origin")
            base = self._git(self.req.clone, "rev-parse", self.req.base).strip()
            self.conn.execute("UPDATE leases SET base_sha = ? WHERE id = ?",
                              (base, self.lease_id))
            # The cut may land before the call that carries it is cut
            # short, so the flags go up first. A branch the clone
            # already carried belongs to whoever cut it.
            self.worktree_cut = True
            self.branch_cut = not _branch_exists(self.req.clone,
                                                 self.req.branch)
            self._git(self.req.clone, "worktree", "add", "-b", self.req.branch,
                      self.req.worktree, self.req.base)
            head = self._git(self.req.worktree, "rev-parse", "HEAD").strip()
        except CommandError as exc:
            raise StepFailure("worktree", exc.detail()) from exc
        if head != base:
            raise StepFailure(
                "worktree",
                f"the worktree was cut at {head[:12]}, not at the base "
                f"{base[:12]}")

    def _worktree_known(self) -> bool:
        """Whether the clone knows about the worktree."""
        try:
            listed = self._git(self.req.clone, "worktree", "list",
                               "--porcelain")
        except CommandError:
            return False
        return str(self.req.worktree) in listed

    def undo_worktree(self) -> None:
        """Remove the worktree, and the branch it cut, if it is untouched.

        A branch the harness has committed to is work, so only a branch
        that still points where it was cut is deleted.
        """
        if self._worktree_known():
            try:
                self._git(self.req.clone, "worktree", "remove", "--force",
                          self.req.worktree)
            except CommandError as exc:
                self.undone.append(f"worktree: {exc.detail()}")
        if not self.branch_cut:
            return
        row = self.conn.execute(
            "SELECT base_sha FROM leases WHERE id = ?", (self.lease_id,)
        ).fetchone()
        base_sha = row["base_sha"] if row else ""
        if not base_sha:
            return
        try:
            head = self._git(self.req.clone, "rev-parse", self.req.branch).strip()
            if head == base_sha:
                self._git(self.req.clone, "branch", "-D", self.req.branch)
        except CommandError:
            pass

    def window(self) -> None:
        """Open the window the lane runs in.

        The target ends in a colon: `-t 0` names window 0, and `-t 0:`
        names the session, so a new window is opened in it. The lease
        travels with the window, so a launcher in the pane can ask
        whether it is still live.
        """
        # The window may open before the call that carries it is cut
        # short, so the flag goes up first.
        self.window_opened = True
        try:
            self._tmux("new-window", "-d", "-t", f"{self.req.tmux_session}:",
                       "-n", self.req.window, "-c", self.req.worktree,
                       "-e", f"LANE_LEASE={self.lease_id}")
        except CommandError as exc:
            raise StepFailure("window", exc.detail()) from exc
        # The window is open from here on, so a reconcile that cannot see
        # it knows the lane stopped.
        self.conn.execute(
            "UPDATE leases SET window_opened_at = ? WHERE id = ?",
            (time.time(), self.lease_id))

    def undo_window(self) -> None:
        """Close the window."""
        if not window_exists_at(self.req.tmux_session, self.req.window):
            return
        try:
            self._tmux("kill-window", "-t",
                       f"{self.req.tmux_session}:{self.req.window}")
        except CommandError as exc:
            self.undone.append(f"window: {exc.detail()}")

    def launch(self) -> None:
        """Start the harness in the window."""
        send_launch(f"{self.req.tmux_session}:{self.req.window}",
                    launch_text(self.req.launch, self.req.profile,
                                self.req.window, self.req.harness_session))

    def undo_steps(self) -> list:
        """The undos for what this spawn started, in reverse.

        A flag says this spawn started the thing, so the undo never
        touches what it did not make.
        """
        steps = []
        if self.window_opened:
            steps.append(self.undo_window)
        if self.worktree_cut:
            steps.append(self.undo_worktree)
        if self.label_added:
            steps.append(self.undo_label)
        return steps

    def run(self) -> None:
        """Do the side effects, and undo them if one fails.

        The undo runs in reverse, and the window takes the harness with
        it. Anything that ends the spawn early undoes what was done.
        """
        try:
            self.label()
            self.worktree()
            self.window()
            self.launch()
            # The harness needs a moment to come up: the wait keeps the
            # coordinator's first keystroke out of the shell.
            time.sleep(self.cfg.ready_seconds)
        except BaseException as exc:
            for step in self.undo_steps():
                step()
            self.conn.execute("DELETE FROM leases WHERE id = ?",
                               (self.lease_id,))
            if not isinstance(exc, StepFailure):
                raise
            detail = exc.detail
            if self.undone:
                detail = f"{detail} (undoing: {'; '.join(self.undone)})"
            raise StepFailure(exc.step, detail) from exc


# ----- bringing a stopped lane back ----------------------------------------

def claim_resume(conn: sqlite3.Connection, lease: sqlite3.Row,
                  profile: Profile, size: int) -> None:
    """Take a pool unit back for a stopped lane, or refuse.

    The claim is one transaction, as the spawn's is: two resumes racing
    for the last unit of a pool get one winner.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM leases WHERE id = ?",
                           (lease["id"],)).fetchone()
        if row is None:
            raise Refusal("lease", f"no lease {lease['id']}")
        if row["state"] != STOPPED:
            raise Refusal("state", f"lease {row['id']} is {row['state']}; "
                                   f"only a stopped lane can be resumed")
        if not row["harness_session"]:
            raise Refusal("session", f"lease {row['id']} records no session "
                                     f"id, so it cannot be resumed")
        filling = conn.execute(
            "SELECT id, window, session, cost FROM leases "
            "WHERE state NOT IN (?, ?) AND pool = ? AND id != ? ORDER BY id",
            (RELEASED, STOPPED, profile.pool, row["id"])).fetchall()
        used = sum(holder["cost"] for holder in filling)
        if used + profile.cost > size:
            holders = ", ".join(
                f"lease {holder['id']} (window {holder['window']}, "
                f"session {holder['session']}) holds {holder['cost']}"
                for holder in filling) or "no lease"
            raise Refusal(
                "pool",
                f"pool {profile.pool} is full: {used} of {size} units are "
                f"taken and this profile costs {profile.cost}; {holders}")
        # The window opens after the claim, so a reconcile that runs in
        # between leaves the lane alone, as it does for a spawn in flight.
        conn.execute(
            "UPDATE leases SET state = ?, state_since = ?, "
            "window_opened_at = 0 WHERE id = ?",
            ("busy", time.time(), row["id"]))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def close_window(lease: sqlite3.Row) -> str | None:
    """Close the lease's window, and say why it could not be closed."""
    if not window_exists_at(lease["tmux_session"], lease["window"]):
        return None
    code, _, err = run(["tmux", "kill-window", "-t", window_target(lease)],
                       env=tmux_env())
    if code != 0:
        return CommandError(["tmux", "kill-window"], code, err).detail()
    return None


def resume_lane(conn: sqlite3.Connection, cfg: Config, lease: sqlite3.Row,
                profile: Profile, harness: Harness) -> None:
    """Start the harness again in a new window on the lane's worktree.

    The lease takes its pool unit back first, so two resumes racing for
    the last unit get one winner. A failure closes the window and stops
    the lease.
    """
    claim_resume(conn, lease, profile, cfg.pools[profile.pool])
    window_opened = False
    try:
        worktree = Path(lease["worktree"])
        if not worktree.exists():
            raise Refusal("worktree", f"the worktree {worktree} is gone, so "
                                      f"lease {lease['id']} cannot be "
                                      f"resumed")
        target = window_target(lease)
        if window_exists_at(lease["tmux_session"], lease["window"]):
            raise Refusal("window", f"window {target} is already open")
        # The window may open before the call that carries it is cut
        # short, so the flag goes up first.
        window_opened = True
        code, _, err = run(["tmux", "new-window", "-d", "-t",
                            f"{lease['tmux_session']}:", "-n", lease["window"],
                            "-c", str(worktree), "-e",
                            f"LANE_LEASE={lease['id']}"], env=tmux_env())
        if code != 0:
            raise StepFailure("window", CommandError(["tmux", "new-window"],
                                                      code, err).detail())
        conn.execute("UPDATE leases SET window_opened_at = ? WHERE id = ?",
                     (time.time(), lease["id"]))
        send_launch(target, launch_text(harness.launch, lease["profile"],
                                        lease["window"],
                                        lease["harness_session"]))
    except BaseException:
        if window_opened:
            undone = close_window(lease)
            if undone:
                print(f"WARN window: {undone}", file=sys.stderr)
        set_state(conn, lease["id"], STOPPED)
        conn.execute("UPDATE leases SET window_opened_at = 0 WHERE id = ?",
                     (lease["id"],))
        raise


# ----- taking a lane down --------------------------------------------------

# The states a lane may be in when it is taken down: it is waiting for
# an instruction, or its window is gone.
FINISHABLE = ("idle", STOPPED)


def tree_name(path: Path) -> str:
    """The builder's name for a worktree.

    The name `lib/common.sh`'s tree_name gives, so the tree the builder
    holds is the tree this asks to prune.
    """
    name = re.sub(r"[^A-Za-z0-9_]", "_", os.path.basename(str(path)))
    name = re.sub(r"_+", "_", name)
    return name[:-1] if name.endswith("_") else name


def prune_tool() -> Path:
    """The prune, beside this tool.

    The sibling, not one on PATH: the PATH copy may be another
    checkout's, and the prune it does is on that checkout's builder.
    """
    return Path(__file__).resolve().parent.parent / "bin" / "remote-prune"


def worktree_status(worktree: Path) -> str:
    """What the worktree has changed."""
    code, out, err = run(["git", "-C", str(worktree), "status", "--porcelain"])
    if code != 0:
        raise Refusal("worktree",
                      f"{worktree} is not a worktree any more: "
                      f"{CommandError(['git', 'status'], code, err).detail()}; "
                      f"release the lease instead")
    return out


def worktree_head(worktree: Path) -> str:
    """The commit the worktree is at."""
    code, out, err = run(["git", "-C", str(worktree), "rev-parse", "HEAD"])
    if code != 0:
        raise Refusal("worktree",
                      f"{worktree} has no HEAD: "
                      f"{CommandError(['git', 'rev-parse'], code, err).detail()}")
    return out.strip()


def remote_head(clone: Path, branch: str) -> str:
    """The commit the remote branch is at, or empty when it is gone."""
    code, out, err = run(["git", "-C", str(clone), "ls-remote", "origin",
                          f"refs/heads/{branch}"])
    if code != 0:
        raise StepFailure("git", CommandError(["git", "ls-remote"], code,
                                              err).detail())
    words = out.split()
    return words[0] if words else ""


def check_finished(lease: sqlite3.Row, pr: PullRequest,
                   abandon: bool) -> Path:
    """Refuse unless the lane can be taken down.

    Everything that can refuse is asked before anything is removed.
    """
    if abandon:
        if pr.state != CLOSED:
            raise Refusal("pr", f"pull request {pr.number} is {pr.state}, not "
                                f"CLOSED; a merged pull request is finished "
                                f"with `lane finish`")
    elif pr.state != MERGED:
        hint = ("; a closed pull request is taken down with `lane finish "
                "--abandon`" if pr.state == CLOSED else "")
        raise Refusal("pr", f"pull request {pr.number} is {pr.state}, not "
                            f"MERGED{hint}")
    if lease["state"] not in FINISHABLE:
        raise Refusal("state", f"lease {lease['id']} is {lease['state']}; "
                                f"only an idle or a stopped lane can be "
                                f"taken down")
    worktree = Path(lease["worktree"])
    status = worktree_status(worktree)
    if status.strip():
        raise Refusal("worktree", f"{worktree} is dirty:\n{status.strip()}")
    head = worktree_head(worktree)
    if abandon:
        remote = remote_head(Path(lease["clone"]), lease["branch"])
        if not remote:
            raise Refusal("head", f"the remote branch {lease['branch']} is "
                                  f"gone, so there is no head to match")
        if head != remote:
            raise Refusal("head", f"the worktree is at {head} and the remote "
                                  f"branch {lease['branch']} at {remote}")
    else:
        if not pr.head:
            raise Refusal("head", f"pull request {pr.number} records no head")
        if head != pr.head:
            raise Refusal("head", f"the worktree is at {head} and pull request "
                                  f"{pr.number}'s head is {pr.head}")
    return worktree


def take_down(conn: sqlite3.Connection, lease: sqlite3.Row,
              worktree: Path, pr: PullRequest | None = None,
              outcome: str = OUTCOME_RELEASED) -> None:
    """The steps, in order, stopping at the first one that fails. Each step
    says what it did, so a failure leaves the account of what already
    went.

    The window goes first, then the processes it left running.

    `outcome` says how the lane ended, and is written with the ending:
    a finished lane is history, and what it was is a fact.
    """
    clone = Path(lease["clone"])
    branch = lease["branch"]
    if window_exists(lease):
        code, _, err = run(["tmux", "kill-window", "-t",
                            window_target(lease)], env=tmux_env())
        if code != 0:
            raise StepFailure("window", CommandError(["tmux", "kill-window"],
                                                      code, err).detail())
        print(f"closed window {window_target(lease)}")
    for line in stop_lane_processes(worktree):
        print(line)
    code, _, err = run(["git", "-C", str(clone), "worktree", "remove",
                        str(worktree)])
    if code != 0:
        raise StepFailure("worktree", CommandError(["git", "worktree",
                                                     "remove"], code,
                                                    err).detail())
    print(f"removed worktree {worktree}")
    code, _, err = run(["git", "-C", str(clone), "branch", "-D", branch])
    if code != 0:
        raise StepFailure("branch", CommandError(["git", "branch", "-D"], code,
                                                  err).detail())
    print(f"deleted branch {branch}")
    if not remote_head(clone, branch):
        # The remote branch is gone, so this tracking ref is stale. Only
        # this one: a prune that dropped every stale ref in the clone
        # would take other lanes' work with it.
        code, _, err = run(["git", "-C", str(clone), "update-ref", "-d",
                            f"refs/remotes/origin/{branch}"])
        if code != 0:
            raise StepFailure("branch", CommandError(["git", "update-ref",
                                                      "-d"], code, err).detail())
        print(f"deleted refs/remotes/origin/{branch}")
    tree = tree_name(worktree)
    # The prune reads the repository it runs in, so it runs in the
    # clone: the worktree is gone by now, and the caller's own directory
    # is whatever the caller happened to be in.
    code, _, err = run([str(prune_tool()), "--prune", "--tree", tree],
                       cwd=clone)
    if code != 0:
        # The lane is gone either way, so a prune that failed is
        # reported and not fatal.
        print(f"WARN caches: "
              f"{CommandError(['remote-prune'], code, err).detail()}",
              file=sys.stderr)
        print(f"the builder caches for {tree} were not pruned")
    else:
        print(f"pruned the builder caches for {tree}")
    release_lease(conn, lease, outcome=outcome,
                  pr=pr.number if pr else None,
                  pr_state=pr.state if pr else None)
    print(f"released lease {lease['id']}")


# ----- the commands ---------------------------------------------------------

def repo_name(repo: str) -> str:
    """The name of a repository, as its worktrees and windows are named."""
    return repo.rsplit("/", 1)[-1] or repo


def cmd_spawn(args: argparse.Namespace) -> int:
    """Start a lane."""
    cfg = load_config()
    profile = cfg.profile(args.profile)
    harness = cfg.harness(args.harness)
    repo = cfg.repo(args.repo)
    session = args.session or os.environ.get("LANE_SESSION")
    if not session:
        raise Refusal("session",
                      "no claiming session: pass --session or set LANE_SESSION")
    # The default branch carries the lease id, so it stays blank until
    # the claim names it.
    branch = args.branch or ""
    base = args.base or "origin/main"
    window = args.window or f"{repo_name(repo.name)}-{args.issue}"
    if args.worktree:
        worktree = Path(args.worktree).expanduser()
    else:
        if cfg.worktree_root is None:
            raise Refusal("worktree_root",
                          f"{cfg.path} does not set worktree_root, and no "
                          "--worktree was given")
        worktree = cfg.worktree_root / f"{repo_name(repo.name)}-{args.issue}"
    # The worktree is compared by its real path: two callers naming the
    # same tree through a symlink are the same tree.
    worktree = Path(os.path.realpath(str(worktree)))

    req = Request(
        repo=repo.name,
        issue=args.issue,
        harness=harness.name,
        profile=profile.name,
        model=profile.model,
        pool=profile.pool,
        cost=profile.cost,
        branch=branch,
        base=base,
        worktree=str(worktree),
        clone=repo.clone,
        tmux_session=cfg.tmux_session,
        window=window,
        session=session,
        harness_session=uuid.uuid4().hex,
        launch=harness.launch,
    )
    conn = open_db()
    try:
        # The leases are read before the claim, so a lane whose window
        # closed is stopped before the pool is counted, and a place
        # something else holds is refused before anything is claimed.
        reconcile(conn)
        refuse_taken_places(conn, req)
        lease_id, branch = claim(conn, req, cfg.pools[profile.pool])
        Spawn(conn, cfg, replace(req, branch=branch), lease_id).run()
    finally:
        conn.close()
    print(f"lease {lease_id}")
    print(f"target {req.tmux_session}:{req.window}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Show the lanes."""
    conn = open_db()
    try:
        reconcile(conn)
        leases = conn.execute("SELECT * FROM leases ORDER BY id").fetchall()
        read_states(conn, leases)
        leases = conn.execute("SELECT * FROM leases ORDER BY id").fetchall()
        now = time.time()
        records = []
        for lease in leases:
            pr = None
            if lease["state"] != RELEASED:
                # A released lease holds no branch and no pull request.
                # Asking again costs a call per finished lane, and the
                # list grows without bound.
                try:
                    pr = find_pr(lease["repo"], lease["branch"],
                                 lease["created_at"])
                except StepFailure as exc:
                    print(f"ERR pr: lease {lease['id']}: {exc.detail}",
                          file=sys.stderr)
            records.append((lease, lease_record(lease, now, pr)))
        if args.as_json:
            for _, record in records:
                print(json.dumps(record))
            return 0
        print(f"{'ID':>3}  {'REPO#ISSUE':<28} {'WINDOW':<20} "
              f"{'SESSION':<12} {'STATE':<9} {'PR':>5} {'PRSTATE':<8} AGE")
        for lease, record in records:
            age = int(max(0.0, now - lease["state_since"]))
            label = f"{lease['repo']}#{lease['issue']}"
            number = str(record["pr"]) if record["pr"] else "-"
            state = record["pr_state"] or "-"
            print(f"{lease['id']:>3}  {label:<28} {lease['window']:<20} "
                  f"{lease['session']:<12} {lease['state']:<9} {number:>5} "
                  f"{state:<8} {age}s")
    finally:
        conn.close()
    return 0


def lease_record(lease: sqlite3.Row, now: float,
                 pr: PullRequest | None) -> dict:
    """One lease, as it is recorded."""
    record = {key: lease[key] for key in lease.keys()}
    record["state_age_seconds"] = int(max(0.0, now - lease["state_since"]))
    record["pr"] = pr.number if pr else lease["pr"]
    record["pr_state"] = pr.state if pr else lease["pr_state"]
    return record


def release_lease(conn: sqlite3.Connection, lease: sqlite3.Row,
                   outcome: str | None = None, pr: int | None = None,
                   pr_state: str | None = None) -> None:
    """End a lease: close its window, mark it released, take the label off.

    The label goes even for a released lease: a label left on one is the
    operator's to clean up, and releasing again is how they ask.

    `outcome` says how the lease ended, and is written with the ending.
    A pull request is written when it is known. Releasing a lease that
    is already released writes nothing: its ending happened once.
    """
    if lease["state"] != RELEASED:
        if window_exists(lease):
            code, _, err = run(
                ["tmux", "kill-window", "-t", window_target(lease)],
                env=tmux_env(),
            )
            if code != 0:
                raise StepFailure("window", CommandError(
                    ["tmux", "kill-window"], code, err).detail())
        set_state(conn, lease["id"], RELEASED)
        if outcome:
            record_history(conn, lease["id"], outcome, pr, pr_state)
    code, _, err = run(
        ["gh", "issue", "edit", str(lease["issue"]), "--repo",
         lease["repo"], "--remove-label", LABEL],
    )
    if code != 0:
        raise StepFailure("label", CommandError(
            ["gh", "issue", "edit"], code, err).detail())


def cmd_resume(args: argparse.Namespace) -> int:
    """Bring a stopped lane back."""
    cfg = load_config()
    conn = open_db()
    try:
        lease = conn.execute("SELECT * FROM leases WHERE id = ?",
                             (args.lease,)).fetchone()
        if lease is None:
            raise Refusal("lease", f"no lease {args.lease}")
        profile = cfg.profile(lease["profile"])
        harness = cfg.harness(lease["harness"])
        resume_lane(conn, cfg, lease, profile, harness)
    finally:
        conn.close()
    print(f"lease {args.lease}")
    print(f"target {lease['tmux_session']}:{lease['window']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Say whether a lease is live.

    The check reads the database and nothing else: a launcher asks it
    before it starts, and the answer must not depend on a window.
    """
    conn = open_db()
    try:
        lease = conn.execute("SELECT * FROM leases WHERE id = ?",
                             (args.lease,)).fetchone()
    finally:
        conn.close()
    if lease is None:
        raise Refusal("lease", f"lease {args.lease} is missing")
    if lease["state"] == RELEASED:
        raise Refusal("lease", f"lease {args.lease} is released")
    print(f"lease {args.lease} is {lease['state']}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """End a lease."""
    conn = open_db()
    try:
        reconcile(conn)
        lease = conn.execute("SELECT * FROM leases WHERE id = ?",
                             (args.lease,)).fetchone()
        if lease is None:
            raise Refusal("lease", f"no lease {args.lease}")
        release_lease(conn, lease, outcome=OUTCOME_RELEASED)
    finally:
        conn.close()
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    """Take a finished lane down."""
    if args.abandon and args.merged:
        raise Refusal("usage", "--abandon and --merged cannot be combined")
    conn = open_db()
    try:
        reconcile(conn)
        read_states(conn, conn.execute(
            "SELECT * FROM leases ORDER BY id").fetchall())
        if args.merged:
            return finish_merged(conn)
        if args.lease is None:
            raise Refusal("usage", "which lease? give a lease number, or "
                                   "--merged for every lane whose pull "
                                   "request merged")
        lease = conn.execute("SELECT * FROM leases WHERE id = ?",
                             (args.lease,)).fetchone()
        if lease is None:
            raise Refusal("lease", f"no lease {args.lease}")
        pr = find_pr(lease["repo"], lease["branch"], lease["created_at"])
        if pr is None:
            raise Refusal("pr", f"no pull request for branch {lease['branch']}")
        worktree = check_finished(lease, pr, args.abandon)
        outcome = OUTCOME_ABANDONED if args.abandon else OUTCOME_MERGED
        take_down(conn, lease, worktree, pr, outcome)
    finally:
        conn.close()
    return 0


def finish_merged(conn: sqlite3.Connection) -> int:
    """Take down every live lane whose pull request merged.

    Each lane stands on its own: one that refuses is reported, and the
    rest still go.
    """
    failures = 0
    for lease in live_leases(conn):
        try:
            pr = find_pr(lease["repo"], lease["branch"], lease["created_at"])
            if pr is None or pr.state != MERGED:
                continue
            worktree = check_finished(lease, pr, False)
            take_down(conn, lease, worktree, pr, OUTCOME_MERGED)
        except (Refusal, StepFailure) as exc:
            failures += 1
            print(f"ERR lease {lease['id']}: {exc.detail}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lane",
        description="Own lane state, so two sessions cannot both take a lane.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    spawn = commands.add_parser("spawn", help="start a lane")
    spawn.add_argument("--repo", required=True, help="owner/name")
    spawn.add_argument("--issue", required=True, type=int, help="issue number")
    spawn.add_argument("--harness", required=True, help="harness to run")
    spawn.add_argument("--profile", required=True, help="profile to run it on")
    spawn.add_argument("--branch", default=None,
                       help="branch to cut (default: lane/<issue>-<lease>)")
    spawn.add_argument("--base", default=None,
                       help="ref to cut from (default: origin/main)")
    spawn.add_argument("--worktree", default=None,
                       help="worktree to work in (default: <root>/<repo>-<issue>)")
    spawn.add_argument("--window", default=None,
                       help="window name (default: <repo>-<issue>)")
    spawn.add_argument("--session", default=None,
                       help="the claiming coordinator (default: $LANE_SESSION)")

    listing = commands.add_parser("list", help="show the lanes")
    listing.add_argument("--json", action="store_true", dest="as_json",
                         help="one JSON object per lease")

    resume = commands.add_parser("resume", help="restart a stopped lane")
    resume.add_argument("lease", type=int, help="the lease number")

    check = commands.add_parser("check", help="say whether a lease is live")
    check.add_argument("lease", type=int, help="the lease number")

    release = commands.add_parser("release", help="end a lease")
    release.add_argument("lease", type=int, help="the lease number")

    finish = commands.add_parser("finish", help="take a finished lane down")
    finish.add_argument("lease", type=int, nargs="?", help="the lease number")
    finish.add_argument("--merged", action="store_true",
                        help="take down every lane whose pull request merged")
    finish.add_argument("--abandon", action="store_true",
                        help="take down a lane whose pull request was closed")

    args = parser.parse_args(argv)
    try:
        if args.command == "spawn":
            return cmd_spawn(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "resume":
            return cmd_resume(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "finish":
            return cmd_finish(args)
        return cmd_release(args)
    except Refusal as exc:
        print(f"ERR {exc.key}: {exc.detail}", file=sys.stderr)
        return 1
    except StepFailure as exc:
        print(f"ERR {exc.step}: {exc.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
