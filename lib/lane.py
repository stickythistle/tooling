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
from contextlib import contextmanager
from dataclasses import dataclass
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


def run(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
    """Run one command and return its exit status, stdout and stderr.

    The child is waited for, and a signal to this process reaches it
    first.
    """
    try:
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env)
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


def _section(raw: dict, name: str, path: Path) -> dict:
    """One table of the settings."""
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

    pools = {}
    for name, size in _section(raw, "pools", path).items():
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise Refusal("pool",
                          f"pool {name} has size {size!r}, which is not a "
                          "positive whole number")
        pools[name] = size

    profiles = {}
    for name, body in _section(raw, "profiles", path).items():
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

    harnesses = {}
    for name, body in _section(raw, "harnesses", path).items():
        if not isinstance(body, dict):
            raise Refusal("harness", f"harness {name} is not a table")
        launch = body.get("launch")
        if not isinstance(launch, str) or not launch:
            raise Refusal("harness", f"harness {name} does not name a launch command")
        harnesses[name] = Harness(name, launch)

    repos = {}
    for name, body in _section(raw, "repos", path).items():
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
    that already carries the table keeps the shape it was made with.
    """
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


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
    return conn


def live_leases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The leases that are live."""
    return conn.execute(
        "SELECT * FROM leases WHERE state != ? ORDER BY id", (RELEASED,)
    ).fetchall()


def set_state(conn: sqlite3.Connection, lease_id: int, state: str,
              now: float | None = None) -> None:
    """Record a lease's state, and when it last changed.

    The time is only moved when the state actually changes: an age that
    restarts on every read says nothing about how long the lane has been
    in that state.
    """
    now = time.time() if now is None else now
    conn.execute(
        "UPDATE leases SET state = ?, state_since = ? WHERE id = ? AND state != ?",
        (state, now, lease_id, state),
    )


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
    if _branch_exists(req.clone, req.branch) and not _leased(conn, "branch",
                                                              req.branch):
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


def claim(conn: sqlite3.Connection, req: Request, size: int) -> int:
    """Take the lease, or refuse.

    The claim is one transaction, taken before anything outside the
    database happens: a check that reads and then writes lets two callers
    pass it, and a unique index cannot sum a pool's costs.
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
            "window, session, state, state_since, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (req.repo, req.issue, req.harness, req.profile, req.model,
             req.pool, req.cost, req.branch, req.base, "", req.worktree,
             str(req.clone), req.tmux_session, req.window, req.session,
             "busy", time.time(), time.time()),
        )
        conn.execute("COMMIT")
        return int(cursor.lastrowid)
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
    state.
    """
    for lease in live_leases(conn):
        if not lease["window_opened_at"]:
            continue
        if not window_exists(lease):
            set_state(conn, lease["id"], STOPPED)


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
        names the session, so a new window is opened in it.
        """
        # The window may open before the call that carries it is cut
        # short, so the flag goes up first.
        self.window_opened = True
        try:
            self._tmux("new-window", "-d", "-t", f"{self.req.tmux_session}:",
                       "-n", self.req.window, "-c", self.req.worktree)
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
        """Start the harness in the window.

        The text goes in with `-l`, and the Enter is a separate call: with
        `-l` a trailing Enter is typed as the five letters, not the key.
        """
        text = (self.req.launch
                .replace("{profile}", self.req.profile)
                .replace("{window}", self.req.window))
        try:
            self._tmux("send-keys", "-t",
                       f"{self.req.tmux_session}:{self.req.window}", "-l", text)
            self._tmux("send-keys", "-t",
                       f"{self.req.tmux_session}:{self.req.window}", "Enter")
        except CommandError as exc:
            raise StepFailure("launch", exc.detail()) from exc

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
    branch = args.branch or f"lane/{args.issue}"
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
        launch=harness.launch,
    )
    conn = open_db()
    try:
        # The leases are read before the claim, so a lane whose window
        # closed is stopped before the pool is counted, and a place
        # something else holds is refused before anything is claimed.
        reconcile(conn)
        refuse_taken_places(conn, req)
        lease_id = claim(conn, req, cfg.pools[profile.pool])
        Spawn(conn, cfg, req, lease_id).run()
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
        if args.as_json:
            for lease in leases:
                print(json.dumps(lease_record(lease, now)))
            return 0
        print(f"{'ID':>3}  {'REPO#ISSUE':<28} {'WINDOW':<20} "
              f"{'SESSION':<12} {'STATE':<9} AGE")
        for lease in leases:
            age = int(max(0.0, now - lease["state_since"]))
            label = f"{lease['repo']}#{lease['issue']}"
            print(f"{lease['id']:>3}  {label:<28} {lease['window']:<20} "
                  f"{lease['session']:<12} {lease['state']:<9} {age}s")
    finally:
        conn.close()
    return 0


def lease_record(lease: sqlite3.Row, now: float) -> dict:
    """One lease, as it is recorded."""
    record = {key: lease[key] for key in lease.keys()}
    record["state_age_seconds"] = int(max(0.0, now - lease["state_since"]))
    return record


def cmd_release(args: argparse.Namespace) -> int:
    """End a lease."""
    conn = open_db()
    try:
        reconcile(conn)
        lease = conn.execute("SELECT * FROM leases WHERE id = ?",
                             (args.lease,)).fetchone()
        if lease is None:
            raise Refusal("lease", f"no lease {args.lease}")
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
        # The label goes whether the lease was live or already released: a
        # released lease whose issue still carries the label is the state
        # the operator has to clean up, and releasing again is how they
        # ask for it.
        code, _, err = run(
            ["gh", "issue", "edit", str(lease["issue"]), "--repo",
             lease["repo"], "--remove-label", LABEL],
        )
        if code != 0:
            raise StepFailure("label", CommandError(
                ["gh", "issue", "edit"], code, err).detail())
    finally:
        conn.close()
    return 0


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
                       help="branch to cut (default: lane/<issue>)")
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

    release = commands.add_parser("release", help="end a lease")
    release.add_argument("lease", type=int, help="the lease number")

    args = parser.parse_args(argv)
    try:
        if args.command == "spawn":
            return cmd_spawn(args)
        if args.command == "list":
            return cmd_list(args)
        return cmd_release(args)
    except Refusal as exc:
        print(f"ERR {exc.key}: {exc.detail}", file=sys.stderr)
        return 1
    except StepFailure as exc:
        print(f"ERR {exc.step}: {exc.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
