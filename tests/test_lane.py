#!/usr/bin/env python3
"""Pin the lane tool: one claim per lane, and a spawn that fails leaves nothing behind.

The tool exists because several coordinator sessions start lanes against
the same account, and nothing recorded which lanes were running. Two
sessions that each believe the last pool unit is free both start a lane,
and the cap is gone. So the claims are the contract: a lease is taken in
one transaction before anything outside the database happens, and a
refusal names the pool or the lease that refused.

Every case here runs the tool against a stub `tmux`, a stub `gh` and a
stub harness, each logging what it was asked to do, so the assertions are
over what the tool actually did. The real `git` cuts the worktrees,
against a throwaway origin: the worktree step is the one the tool cannot
fake its way through.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The repository every case spawns against.
REPO = "stickythistle/tooling"


# ----- the world a case runs in ---------------------------------------------

def make_origin(tmp: Path) -> Path:
    """A bare origin with one commit on `main`, and a clone of it.

    The real git cuts the worktrees, so the origin is local and the
    fetch never leaves the machine.
    """
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True)
    seed = tmp / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email",
                    "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"],
                   check=True)
    (seed / "README").write_text("test\n")
    subprocess.run(["git", "-C", str(seed), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "init"],
                   check=True)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin",
                    str(origin)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"],
                   check=True)
    clone = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


def write_stub_tmux(bin_dir: Path) -> None:
    """A tmux that keeps its windows in a file and their panes in others.

    `$STUB_DATA/windows` holds the windows that exist, one name per line,
    and `$STUB_DATA/pane_<window>` holds a window's text. Every call is
    logged to `$STUB_DATA/tmux.log` and to `$STUB_DATA/calls.log`, which
    every stub writes to in one order, and the steps that order the
    spawn are appended to `$STUB_DATA/steps.log`.

    Text typed into a window is run, so the harness the tool started is a
    process the test can see.
    """
    body = r'''#!/usr/bin/env bash
printf 'tmux %s\n' "$*" >> "$STUB_DATA/tmux.log"
printf 'tmux %s\n' "$*" >> "$STUB_DATA/calls.log"
cmd="$1"; shift
target=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-t" ]; then target="$arg"; fi
    prev="$arg"
done
case "$cmd" in
new-window)
    if [ -e "$STUB_DATA/fail_new_window" ]; then
        echo "tmux: simulated failure" >&2
        exit 1
    fi
    # A test can hold the window open: the lease is claimed by then and
    # the window is not, which is the state a second caller has to read.
    # The hold is for the first window only.
    if [ -f "$STUB_DATA/hold_new_window" ]; then
        rm -f "$STUB_DATA/hold_new_window"
        while [ ! -f "$STUB_DATA/release_new_window" ]; do sleep 0.05; done
    fi
    name=""; dir=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -n) name="$2"; shift 2;;
            -c) dir="$2"; shift 2;;
            *) shift;;
        esac
    done
    if [ -n "$dir" ] && [ ! -d "$dir" ]; then
        echo "tmux: no such directory: $dir" >&2
        exit 1
    fi
    present=missing
    [ -d "$dir" ] && present=present
    printf 'new-window %s worktree=%s\n' "$name" "$present" >> "$STUB_DATA/steps.log"
    printf '%s\n' "$name" >> "$STUB_DATA/windows"
    if [ -f "$STUB_DATA/kill_on_new_window" ]; then
        kill -TERM $PPID
        exit 143
    fi
    ;;
send-keys)
    literal=0; text=""
    while [ $# -gt 0 ]; do
        case "$1" in
            -t) shift 2;;
            -l) literal=1; shift;;
            *) text="$1"; shift;;
        esac
    done
    if [ "$literal" = 1 ]; then
        printf 'launch %s\n' "$text" >> "$STUB_DATA/steps.log"
        sh -c "$text" >> "$STUB_DATA/pane.log" 2>&1 || true
    else
        printf 'enter %s\n' "$text" >> "$STUB_DATA/steps.log"
    fi
    ;;
list-windows)
    if [ -f "$STUB_DATA/windows" ]; then cat "$STUB_DATA/windows"; fi
    ;;
capture-pane)
    name="${target#*:}"
    if [ -f "$STUB_DATA/pane_$name" ]; then cat "$STUB_DATA/pane_$name"; fi
    ;;
kill-window)
    name="${target#*:}"
    if [ -f "$STUB_DATA/windows" ]; then
        grep -v -x "$name" "$STUB_DATA/windows" > "$STUB_DATA/windows.tmp" || true
        mv "$STUB_DATA/windows.tmp" "$STUB_DATA/windows"
    fi
    printf 'kill-window %s\n' "$name" >> "$STUB_DATA/steps.log"
    ;;
esac
exit 0
'''
    path = bin_dir / "tmux"
    path.write_text(body)
    path.chmod(0o755)


def write_stub_gh(bin_dir: Path) -> None:
    """A gh that answers the label calls and logs them.

    A test asks `issue edit` to fail by creating `$STUB_DATA/fail_issue_edit`.
    `pr list` answers with the pull request the case wrote to
    `$STUB_DATA/pr_<branch>.json`, or with an empty list when it wrote
    none.
    """
    body = r'''#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "$STUB_DATA/gh.log"
printf 'gh %s\n' "$*" >> "$STUB_DATA/calls.log"
if [ "$1 $2" = "pr list" ]; then
    branch=""
    prev=""
    for arg in "$@"; do
        if [ "$prev" = "--head" ]; then branch="$arg"; fi
        prev="$arg"
    done
    name=$(printf '%s' "$branch" | tr '/' '_')
    if [ -f "$STUB_DATA/pr_$name.json" ]; then
        cat "$STUB_DATA/pr_$name.json"
    else
        printf '[]\n'
    fi
    exit 0
fi
if [ "$1 $2" = "label create" ]; then
    printf 'label-create %s\n' "$*" >> "$STUB_DATA/steps.log"
    exit 0
fi
wt=""
if [ -f "$STUB_DATA/worktree_path" ]; then wt="$(cat "$STUB_DATA/worktree_path")"; fi
present=missing
if [ -n "$wt" ] && [ -d "$wt" ]; then present=present; fi
prev=""
for arg in "$@"; do
    case "$prev" in
        --add-label)
            printf 'label-add worktree=%s\n' "$present" >> "$STUB_DATA/steps.log"
            if [ -f "$STUB_DATA/kill_on_issue_edit" ]; then
                kill -TERM $PPID
                exit 143
            fi
            ;;
        --remove-label) printf 'label-remove\n' >> "$STUB_DATA/steps.log";;
    esac
    prev="$arg"
done
if [ -e "$STUB_DATA/fail_issue_edit" ]; then
    echo "gh: simulated failure" >&2
    exit 1
fi
exit 0
'''
    path = bin_dir / "gh"
    path.write_text(body)
    path.chmod(0o755)


def write_stub_git(bin_dir: Path) -> None:
    """A git that logs its calls and can stop the tool it belongs to.

    Everything but the call a test asks about is the real git, so the
    worktree step is still the real one.
    """
    real = shutil.which("git") or "/usr/bin/git"
    body = r'''#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$STUB_DATA/git.log"
printf 'git %s\n' "$*" >> "$STUB_DATA/calls.log"
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
    case "${args[$i]}" in
        -C) i=$((i+2)); continue;;
        *) break;;
    esac
done
sub="${args[$i]:-}"
case "$sub" in
fetch)
    if [ -f "$STUB_DATA/kill_on_fetch" ]; then
        kill -TERM $PPID
        exit 143
    fi
    ;;
worktree)
    if [ "${args[$((i+1))]:-}" = "add" ]; then
        "''' + real + r'''" "$@"
        status=$?
        if [ -f "$STUB_DATA/kill_on_worktree_add" ]; then
            kill -TERM $PPID
            exit 143
        fi
        exit $status
    fi
    ;;
esac
exec "''' + real + r'''" "$@"
'''
    path = bin_dir / "git"
    path.write_text(body)
    path.chmod(0o755)


def write_stub_remote_prune(path: Path) -> None:
    """A remote-prune that logs the tree it was asked to prune.

    A test asks it to fail by creating `$STUB_DATA/fail_remote_prune`.
    """
    body = r'''#!/usr/bin/env bash
printf 'remote-prune %s\n' "$*" >> "$STUB_DATA/remote-prune.log"
printf 'remote-prune %s\n' "$*" >> "$STUB_DATA/calls.log"
if [ -f "$STUB_DATA/fail_remote_prune" ]; then
    echo "remote-prune: simulated failure" >&2
    exit 1
fi
exit 0
'''
    path.write_text(body)
    path.chmod(0o755)


def mirror_tool(root: Path) -> Path:
    """A copy of the tool, with a stub `remote-prune` beside it.

    `lane finish` prunes the builder's caches by calling the
    `remote-prune` that sits beside it, so the tool is copied into the
    case's own directory: the stub beside that copy is the one the tool
    calls, and the real one never runs.
    """
    tool = root / "tool"
    (tool / "bin").mkdir(parents=True)
    (tool / "lib").mkdir()
    shutil.copy(REPO_ROOT / "bin" / "lane", tool / "bin" / "lane")
    shutil.copy(REPO_ROOT / "lib" / "lane.py", tool / "lib" / "lane.py")
    write_stub_remote_prune(tool / "bin" / "remote-prune")
    return tool / "bin" / "lane"


def write_stub_harness(bin_dir: Path, name: str) -> None:
    """A harness that logs the arguments it was started with."""
    body = (
        "#!/usr/bin/env bash\n"
        "printf '%s: %s\\n' \"$(basename \"$0\")\" \"$*\" "
        ">> \"$STUB_DATA/harness.log\"\n"
        "exit 0\n"
    )
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def config_text(cfg: dict) -> str:
    """The settings file, as TOML."""
    lines = [f'tmux_session = "{cfg["tmux_session"]}"',
             f'worktree_root = "{cfg["worktree_root"]}"',
             f'ready_seconds = {cfg["ready_seconds"]}',
             ""]
    lines.append("[pools]")
    for name, size in cfg["pools"].items():
        lines.append(f"{name} = {size}")
    lines.append("")
    for name, body in cfg["profiles"].items():
        lines.append(f"[profiles.{name}]")
        for key, value in body.items():
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            else:
                lines.append(f"{key} = {value}")
        lines.append("")
    for name, body in cfg["harnesses"].items():
        lines.append(f"[harnesses.{name}]")
        for key, value in body.items():
            lines.append(f'{key} = "{value}"')
        lines.append("")
    for name, body in cfg["repos"].items():
        lines.append(f'[repos."{name}"]')
        for key, value in body.items():
            lines.append(f'{key} = "{value}"')
        lines.append("")
    return "\n".join(lines)


class Env:
    """One case's world: stubs, a throwaway origin, and the settings."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "stub-bin"
        self.bin.mkdir(parents=True)
        self.data = tmp / "data"
        self.data.mkdir(parents=True)
        self.config = tmp / "lanes.toml"
        self.db = tmp / "lanes.db"
        self.worktree_root = tmp / "wt"
        self.worktree_root.mkdir(parents=True)
        self.clone = make_origin(tmp)
        self.origin = tmp / "origin.git"
        self.lane = mirror_tool(tmp)
        write_stub_tmux(self.bin)
        write_stub_gh(self.bin)
        write_stub_harness(self.bin, "hermes-stub")
        write_stub_harness(self.bin, "pi-stub")
        self.write_config(self.default_config())

    def default_config(self) -> dict:
        """The settings every case starts from."""
        return {
            "tmux_session": "0",
            "worktree_root": str(self.worktree_root),
            "ready_seconds": 0,
            "pools": {"minimax": 4, "logan": 2},
            "profiles": {
                "minimax": {"pool": "minimax", "cost": 1, "model": "MiniMax"},
                "flashnext512": {"pool": "logan", "cost": 2,
                                 "model": "flash-next-512k"},
            },
            "harnesses": {
                "hermes": {"launch": "hermes-stub -p {profile}"},
                "pi": {"launch": "pi-stub {window} {profile}"},
            },
            "repos": {REPO: {"clone": str(self.clone)}},
        }

    def write_config(self, cfg: dict) -> None:
        """Write the settings, and tell the stub gh where the worktree goes."""
        self.config.write_text(config_text(cfg))
        (self.data / "worktree_path").write_text(
            str(self.worktree_root / "tooling-5"))

    def sibling(self, name: str) -> "Env":
        """Another world, for a case that needs a second one."""
        directory = self.tmp / name
        directory.mkdir()
        return Env(directory)

    def env(self, extra: dict | None = None) -> dict:
        """The environment the tool runs in."""
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["STUB_DATA"] = str(self.data)
        env["LANE_DB"] = str(self.db)
        env["LANE_CONFIG"] = str(self.config)
        env["LANE_PI_SETTLE"] = "0"
        env.pop("LANE_SESSION", None)
        if extra:
            env.update(extra)
        return env

    def run(self, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
        """Run the tool."""
        return subprocess.run([str(self.lane), *args], capture_output=True,
                              text=True, env=self.env(extra))

    def spawn(self, issue: int, harness: str = "hermes",
              profile: str = "minimax", session: str = "coord-a",
              extra: tuple = ()) -> subprocess.CompletedProcess:
        """Spawn a lane."""
        return self.run("spawn", "--repo", REPO, "--issue", str(issue),
                        "--harness", harness, "--profile", profile,
                        "--session", session, *extra)

    def leases(self) -> list[dict]:
        """Every lease the tool has recorded."""
        result = self.run("list", "--json")
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def steps(self) -> list[str]:
        """The ordered steps the stubs recorded."""
        path = self.data / "steps.log"
        if not path.exists():
            return []
        return path.read_text().splitlines()

    def log(self, name: str) -> str:
        """One stub's log."""
        path = self.data / name
        return path.read_text() if path.exists() else ""

    def set_pane(self, window: str, text: str) -> None:
        """What a window shows."""
        (self.data / f"pane_{window}").write_text(text)

    def remove_window(self, window: str) -> None:
        """A window that is gone."""
        path = self.data / "windows"
        if not path.exists():
            return
        kept = [line for line in path.read_text().splitlines() if line != window]
        path.write_text("".join(f"{line}\n" for line in kept))


# ----- the helpers ----------------------------------------------------------

def git_out(directory: Path, *args: str) -> str:
    """What git says about a directory."""
    proc = subprocess.run(["git", "-C", str(directory), *args],
                          capture_output=True, text=True)
    return proc.stdout.strip()


def set_pr(env: Env, branch: str, number: int, state: str, head: str) -> None:
    """The pull request the stubbed gh answers with."""
    name = branch.replace("/", "_")
    (env.data / f"pr_{name}.json").write_text(json.dumps([
        {"number": number, "state": state, "headRefOid": head,
         "mergedAt": None}]))


def lane_worktree(env: Env, issue: int) -> Path:
    """The worktree a lane for the issue works in."""
    return env.worktree_root / f"tooling-{issue}"


def spawn_merged(env: Env, issue: int, number: int = 12) -> str:
    """A lane whose pull request merged, and the head it merged at.

    An empty string says the lane could not be set up.
    """
    spawn = env.spawn(issue)
    if spawn.returncode != 0:
        return ""
    worktree = lane_worktree(env, issue)
    head = git_out(worktree, "rev-parse", "HEAD")
    set_pr(env, f"lane/{issue}", number, "MERGED", head)
    env.set_pane(f"tooling-{issue}", HERMES_IDLE)
    return head


# ----- the cases ------------------------------------------------------------

def test_race(env: Env) -> list[str]:
    """Two spawns for the last pool unit: one wins, one is refused."""
    cfg = env.default_config()
    cfg["pools"]["minimax"] = 1
    env.write_config(cfg)
    # Five callers at once: the database is opened by all of them before
    # any of them has claimed anything, which is where a lock contention
    # shows up.
    procs = []
    for number in range(5):
        args = ["spawn", "--repo", REPO, "--issue", str(201 + number),
                "--harness", "hermes", "--profile", "minimax",
                "--session", "coord-a"]
        procs.append(subprocess.Popen([str(env.lane), *args],
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True,
                                      env=env.env()))
    results = [proc.communicate() for proc in procs]
    codes = [proc.returncode for proc in procs]
    failures = []
    if sorted(codes) != [0, 1, 1, 1, 1]:
        failures.append(f"race: expected one spawn to win and the rest to "
                        f"be refused, got {codes} "
                        f"{[r[1].strip() for r in results]}")
    refused = [r[1] for r, code in zip(results, codes) if code != 0]
    for stderr in refused:
        if "pool" not in stderr or "minimax" not in stderr:
            failures.append(f"race: the refusal did not name the pool: "
                            f"{stderr.strip()!r}")
    leases = env.leases()
    if len(leases) != 1:
        failures.append(f"race: expected one lease in the database, got "
                        f"{len(leases)}")
    return failures


def test_pool_sum(env: Env) -> list[str]:
    """A pool of two units: a lane that costs one refuses a lane that costs two."""
    cfg = env.default_config()
    cfg["pools"] = {"logan": 2}
    cfg["profiles"] = {
        "minimax": {"pool": "logan", "cost": 1, "model": "MiniMax"},
        "flashnext512": {"pool": "logan", "cost": 2, "model": "flash-next-512k"},
    }
    env.write_config(cfg)
    failures = []
    first = env.spawn(5, profile="minimax")
    if first.returncode != 0:
        failures.append(f"pool sum: the first spawn failed: {first.stderr.strip()!r}")
    second = env.spawn(6, profile="flashnext512")
    if second.returncode == 0:
        failures.append("pool sum: the second spawn was allowed past a full pool")
    elif "logan" not in second.stderr:
        failures.append(f"pool sum: the refusal did not name the pool: "
                        f"{second.stderr.strip()!r}")
    elif "lease 1" not in second.stderr:
        failures.append(f"pool sum: the refusal did not name the lease "
                        f"filling the pool: {second.stderr.strip()!r}")
    return failures


def test_pool_across_harnesses(env: Env) -> list[str]:
    """One pool across both harnesses: a pi lane cannot take a hermes lane's unit."""
    cfg = env.default_config()
    cfg["pools"]["minimax"] = 1
    env.write_config(cfg)
    failures = []
    first = env.spawn(5, harness="hermes", profile="minimax")
    if first.returncode != 0:
        failures.append(f"pool across harnesses: the hermes spawn failed: "
                        f"{first.stderr.strip()!r}")
    second = env.spawn(6, harness="pi", profile="minimax")
    if second.returncode == 0:
        failures.append("pool across harnesses: the pi spawn took a unit the "
                        "hermes lane already held")
    elif "minimax" not in second.stderr:
        failures.append(f"pool across harnesses: the refusal did not name "
                        f"the pool: {second.stderr.strip()!r}")
    return failures


def test_same_issue(env: Env) -> list[str]:
    """A live lease on an issue refuses a second lane on it, and only it."""
    failures = []
    first = env.spawn(5)
    if first.returncode != 0:
        failures.append(f"same issue: the first spawn failed: {first.stderr.strip()!r}")
    second = env.spawn(5)
    if second.returncode == 0:
        failures.append("same issue: a second lane took an issue that was "
                        "already leased")
    else:
        for expected in ("lease 1", "tooling-5", "coord-a"):
            if expected not in second.stderr:
                failures.append(f"same issue: the refusal did not name "
                                f"{expected!r}: {second.stderr.strip()!r}")
        if env.log("tmux.log").count("new-window") != 1:
            failures.append("same issue: the refused spawn opened a window")
    third = env.spawn(6)
    if third.returncode != 0:
        failures.append(f"same issue: another issue in the same repository "
                        f"was refused: {third.stderr.strip()!r}")
    return failures


def test_same_worktree(env: Env) -> list[str]:
    """A live lease on a worktree refuses a second lane in it, through a symlink too."""
    worktree = env.worktree_root / "shared"
    failures = []
    first = env.spawn(5, extra=("--worktree", str(worktree)))
    if first.returncode != 0:
        failures.append(f"same worktree: the first spawn failed: "
                        f"{first.stderr.strip()!r}")
    second = env.spawn(6, extra=("--worktree", str(worktree)))
    if second.returncode == 0:
        failures.append("same worktree: a second lane took a leased worktree")
    elif "lease 1" not in second.stderr:
        failures.append(f"same worktree: the refusal did not name the holder: "
                        f"{second.stderr.strip()!r}")
    link = env.tmp / "link"
    link.symlink_to(worktree)
    third = env.spawn(7, extra=("--worktree", str(link)))
    if third.returncode == 0:
        failures.append("same worktree: a symlink to a leased worktree was "
                        "allowed, so the worktree was compared by name")
    elif "lease 1" not in third.stderr:
        failures.append(f"same worktree: the symlink was refused for "
                        f"another reason than the lease: "
                        f"{third.stderr.strip()!r}")
    return failures


def test_released_frees(env: Env) -> list[str]:
    """A released lease holds no pool unit and blocks no issue."""
    failures = []
    first = env.spawn(5)
    if first.returncode != 0:
        failures.append(f"released frees: the first spawn failed: "
                        f"{first.stderr.strip()!r}")
    lease = env.leases()[0]["id"]
    released = env.run("release", str(lease))
    if released.returncode != 0:
        failures.append(f"released frees: the release failed: "
                        f"{released.stderr.strip()!r}")
    other = env.spawn(6)
    if other.returncode != 0:
        failures.append(f"released frees: another issue was refused after the "
                        f"release: {other.stderr.strip()!r}")
    again = env.spawn(5, extra=("--branch", "lane/5-again", "--worktree",
                                str(env.worktree_root / "again")))
    if again.returncode != 0:
        failures.append(f"released frees: the same issue was refused after "
                        f"the release: {again.stderr.strip()!r}")
    return failures


def test_step_failure(env: Env) -> list[str]:
    """A window that cannot be opened undoes the worktree and the label."""
    failures = []
    (env.data / "fail_new_window").write_text("")
    result = env.spawn(5)
    if result.returncode == 0:
        failures.append("step failure: the spawn reported success")
    elif "window" not in result.stderr:
        failures.append(f"step failure: the failure did not name the step: "
                        f"{result.stderr.strip()!r}")
    leases = env.leases()
    if leases:
        failures.append(f"step failure: the lease survived: {leases}")
    worktree = env.worktree_root / "tooling-5"
    if worktree.exists():
        failures.append("step failure: the worktree was left behind")
    if "--remove-label" not in env.log("gh.log"):
        failures.append("step failure: the label was not taken back off")
    return failures


def test_label_failure(env: Env) -> list[str]:
    """A label that cannot be set stops the spawn before the worktree."""
    failures = []
    (env.data / "fail_issue_edit").write_text("")
    result = env.spawn(5)
    if result.returncode == 0:
        failures.append("label failure: the spawn reported success")
    elif "label" not in result.stderr:
        failures.append(f"label failure: the failure did not name the step: "
                        f"{result.stderr.strip()!r}")
    leases = env.leases()
    if leases:
        failures.append(f"label failure: the lease survived: {leases}")
    if (env.worktree_root / "tooling-5").exists():
        failures.append("label failure: the worktree was cut anyway")
    if "new-window" in env.log("tmux.log"):
        failures.append("label failure: a window was opened anyway")
    if "kill-window" in env.log("tmux.log"):
        failures.append("label failure: the undo closed a window the spawn "
                        "never opened")
    return failures


def test_config(env: Env) -> list[str]:
    """A settings file the tool cannot act on is a refusal naming the key."""
    failures = []

    unknown = env.spawn(5, profile="nope")
    if unknown.returncode == 0:
        failures.append("config: an unknown profile was accepted")
    elif "profile" not in unknown.stderr or "nope" not in unknown.stderr:
        failures.append(f"config: the refusal did not name the profile: "
                        f"{unknown.stderr.strip()!r}")

    unknown_harness = env.spawn(5, harness="nope")
    if unknown_harness.returncode == 0:
        failures.append("config: an unknown harness was accepted")
    elif "harness" not in unknown_harness.stderr:
        failures.append(f"config: the refusal did not name the harness: "
                        f"{unknown_harness.stderr.strip()!r}")

    unknown_repo = env.run("spawn", "--repo", "other/repo", "--issue", "5",
                           "--harness", "hermes", "--profile", "minimax",
                           "--session", "coord-a")
    if unknown_repo.returncode == 0:
        failures.append("config: an unknown repository was accepted")
    elif "repo" not in unknown_repo.stderr:
        failures.append(f"config: the refusal did not name the repository: "
                        f"{unknown_repo.stderr.strip()!r}")

    cfg = env.default_config()
    cfg["profiles"]["minimax"]["pool"] = "nowhere"
    env.write_config(cfg)
    undefined_pool = env.spawn(5)
    if undefined_pool.returncode == 0:
        failures.append("config: a profile naming an undefined pool was accepted")
    elif "pool" not in undefined_pool.stderr or "nowhere" not in undefined_pool.stderr:
        failures.append(f"config: the refusal did not name the pool: "
                        f"{undefined_pool.stderr.strip()!r}")

    missing = env.run("spawn", "--repo", REPO, "--issue", "5", "--harness",
                      "hermes", "--profile", "minimax", "--session", "coord-a",
                      extra={"LANE_CONFIG": str(env.tmp / "absent.toml")})
    if missing.returncode == 0:
        failures.append("config: a missing settings file was accepted")
    elif "absent.toml" not in missing.stderr:
        failures.append(f"config: the refusal did not name the path: "
                        f"{missing.stderr.strip()!r}")
    return failures


def test_order(env: Env) -> list[str]:
    """The label, the worktree, the window, the harness: in that order."""
    failures = []
    result = env.spawn(5)
    if result.returncode != 0:
        return [f"order: the spawn failed: {result.stderr.strip()!r}"]
    steps = env.steps()
    if len(steps) < 5:
        failures.append(f"order: expected five steps, got {steps}")
        return failures
    if not steps[0].startswith("label-create"):
        failures.append(f"order: the label was not the first step: {steps}")
    if steps[1] != "label-add worktree=missing":
        failures.append(f"order: the label was added after the worktree, or "
                        f"not at all: {steps}")
    if not steps[2].startswith("new-window tooling-5"):
        failures.append(f"order: the window was not the third step: {steps}")
    elif "worktree=present" not in steps[2]:
        failures.append(f"order: the window was opened before the worktree "
                        f"existed: {steps}")
    if not steps[3].startswith("launch hermes-stub -p minimax"):
        failures.append(f"order: the harness was not started by the fourth "
                        f"step: {steps}")
    if steps[4] != "enter Enter":
        failures.append(f"order: the Enter was not its own step: {steps}")
    if "hermes-stub: -p minimax" not in env.log("harness.log"):
        failures.append(f"order: the harness was not started with the "
                        f"profile: {env.log('harness.log')!r}")
    if "lease" not in result.stdout or "target 0:tooling-5" not in result.stdout:
        failures.append(f"order: the spawn did not report the lease and the "
                        f"target: {result.stdout!r}")
    return failures


def test_stopped(env: Env) -> list[str]:
    """A lane whose window is gone is stopped."""
    failures = []
    result = env.spawn(5)
    if result.returncode != 0:
        return [f"stopped: the spawn failed: {result.stderr.strip()!r}"]
    env.remove_window("tooling-5")
    leases = env.leases()
    if not leases:
        return ["stopped: the lease was not recorded"]
    if leases[0]["state"] != "stopped":
        failures.append(f"stopped: the lease reads {leases[0]['state']!r} "
                        f"rather than 'stopped'")
    return failures


HERMES_IDLE = """\
some output
minimax ❯
⚕ MiniMax-M3 · 42% · ~/wt
"""

HERMES_BUSY = """\
some output
⚕ MiniMax-M3 · 42% · ~/wt
"""

HERMES_BUDGET = """\
⚠ Iteration budget reached (40/40) — stopping.
minimax ❯
⚕ MiniMax-M3 · 42% · ~/wt
"""

HERMES_APPROVAL = """\
Hermes needs your approval
  1. Allow once
  2. Deny
⚕ MiniMax-M3 · 42% · ~/wt
"""

HERMES_FALLBACK = """\
minimax ❯
⚕ Llama-3.2-3B · 42% · ~/wt
"""

HERMES_FLOOD = """\
⇒⇒⇒⇒⇒⇒
⇒⇒⇒⇒⇒⇒
⇒⇒⇒⇒⇒⇒
minimax ❯
⚕ MiniMax-M3 · 42% · ~/wt
"""

PI_CODE = """\
FLOOD_TOKEN = "⇒⇒⇒⇒⇒⇒"
>
⚕ MiniMax-M3 · 42%
"""

HERMES_ONE_FLOOD_LINE = """\
⇒⇒⇒⇒⇒⇒
minimax ❯
⚕ MiniMax-M3 · 42% · ~/wt
"""

PI_IDLE = """\
some output
>
⚕ MiniMax-M3 · 42%
"""

PI_BUSY = """\
Working...
>
⚕ MiniMax-M3 · 42%
"""

PI_FALLBACK = """\
>
⚕ Llama-3.2-3B · 42%
"""

PI_FLOOD = """\
⇒⇒⇒⇒⇒⇒
⇒⇒⇒⇒⇒⇒
⇒⇒⇒⇒⇒⇒
⚕ MiniMax-M3 · 42%
"""


def test_states(env: Env) -> list[str]:
    """A lane's state is what its pane says."""
    cfg = env.default_config()
    cfg["pools"]["minimax"] = 20
    env.write_config(cfg)
    cases = [
        ("hermes", HERMES_IDLE, "idle"),
        ("hermes", HERMES_BUSY, "busy"),
        ("hermes", HERMES_BUDGET, "budget"),
        ("hermes", HERMES_APPROVAL, "approval"),
        ("hermes", HERMES_FALLBACK, "fallback"),
        ("hermes", HERMES_FLOOD, "flood"),
        ("hermes", HERMES_ONE_FLOOD_LINE, "idle"),
        ("pi", PI_CODE, "idle"),
        ("pi", PI_IDLE, "idle"),
        ("pi", PI_BUSY, "busy"),
        ("pi", PI_FALLBACK, "fallback"),
        ("pi", PI_FLOOD, "flood"),
    ]
    failures = []
    for number, (harness, pane, expected) in enumerate(cases):
        issue = 300 + number
        window = f"tooling-{issue}"
        result = env.spawn(issue, harness=harness)
        if result.returncode != 0:
            failures.append(f"states: the {harness} spawn failed: "
                            f"{result.stderr.strip()!r}")
            continue
        env.set_pane(window, pane)
        leases = {lease["window"]: lease for lease in env.leases()}
        lease = leases.get(window)
        if lease is None:
            failures.append(f"states: the {harness} lease for {window} is gone")
            continue
        if lease["state"] != expected:
            failures.append(f"states: the {harness} lane reads "
                            f"{lease['state']!r} rather than {expected!r} "
                            f"for this pane:\n{pane}")
    return failures


def test_release_twice(env: Env) -> list[str]:
    """Releasing a released lease retries the label and succeeds."""
    failures = []
    result = env.spawn(5)
    if result.returncode != 0:
        return [f"release twice: the spawn failed: {result.stderr.strip()!r}"]
    lease = env.leases()[0]["id"]
    first = env.run("release", str(lease))
    if first.returncode != 0:
        failures.append(f"release twice: the first release failed: "
                        f"{first.stderr.strip()!r}")
    removals = env.log("gh.log").count("--remove-label")
    if removals != 1:
        failures.append(f"release twice: the label was removed {removals} "
                        f"times, not once")
    second = env.run("release", str(lease))
    if second.returncode != 0:
        failures.append(f"release twice: the second release failed: "
                        f"{second.stderr.strip()!r}")
    removals = env.log("gh.log").count("--remove-label")
    if removals != 2:
        failures.append(f"release twice: the label was removed {removals} "
                        f"times, not twice")
    if "kill-window tooling-5" not in env.steps():
        failures.append(f"release twice: the window was not closed: "
                        f"{env.steps()}")
    return failures


def test_read_under_a_write_lock(env: Env) -> list[str]:
    """A spawn while another caller holds the write lock waits for it.

    The lock is held the way a claim holds it, on a database the tool is
    about to open: two sessions opening the database at the same moment is
    the case the tool exists for.
    """
    failures = []
    holder = sqlite3.connect(env.db, isolation_level=None, timeout=30)
    holder.execute("PRAGMA busy_timeout = 30000")
    holder.execute("CREATE TABLE IF NOT EXISTS placeholder (a)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO placeholder VALUES (1)")
    spawner = subprocess.Popen(
        [str(env.lane), "spawn", "--repo", REPO, "--issue", "5", "--harness",
         "hermes", "--profile", "minimax", "--session", "coord-a"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env.env())
    time.sleep(0.5)
    holder.execute("COMMIT")
    holder.close()
    _, err = spawner.communicate()
    if spawner.returncode != 0:
        failures.append(f"read under a write lock: the spawn failed: "
                        f"{err.strip()!r}")
    return failures


def test_stopped_holds_no_pool_unit(env: Env) -> list[str]:
    """A lane whose window is gone gives its pool unit back, and keeps its issue.

    The pool counts lanes that can be running, and a stopped lane still
    holds its issue, so a second lane cannot start there.
    """
    failures = []

    world = env.sibling("stopped")
    cfg = world.default_config()
    cfg["pools"]["minimax"] = 1
    world.write_config(cfg)
    first = world.spawn(1)
    if first.returncode != 0:
        return [f"stopped pool: the first spawn failed: "
                f"{first.stderr.strip()!r}"]
    world.remove_window("tooling-1")
    world.leases()
    other = world.spawn(2)
    if other.returncode != 0:
        failures.append(f"stopped pool: a stopped lane still held its pool "
                        f"unit: {other.stderr.strip()!r}")
    same = world.spawn(1)
    if same.returncode == 0:
        failures.append("stopped pool: a stopped lane let a second lane take "
                        "its issue")
    elif "lease 1" not in same.stderr:
        failures.append(f"stopped pool: the refusal did not name the holder: "
                        f"{same.stderr.strip()!r}")

    world = env.sibling("idle")
    cfg = world.default_config()
    cfg["pools"]["minimax"] = 1
    world.write_config(cfg)
    first = world.spawn(1)
    if first.returncode != 0:
        failures.append(f"stopped pool: the idle spawn failed: "
                        f"{first.stderr.strip()!r}")
    else:
        world.set_pane("tooling-1", HERMES_IDLE)
        other = world.spawn(2)
        if other.returncode == 0:
            failures.append("stopped pool: an idle lane gave its pool unit up")
        elif "minimax" not in other.stderr:
            failures.append(f"stopped pool: the refusal did not name the "
                            f"pool: {other.stderr.strip()!r}")

    world = env.sibling("released")
    cfg = world.default_config()
    cfg["pools"]["minimax"] = 1
    world.write_config(cfg)
    first = world.spawn(1)
    if first.returncode != 0:
        failures.append(f"stopped pool: the released spawn failed: "
                        f"{first.stderr.strip()!r}")
    else:
        world.run("release", str(world.leases()[0]["id"]))
        again = world.spawn(1, extra=("--branch", "lane/1-again",
                                      "--worktree",
                                      str(world.worktree_root / "again")))
        if again.returncode != 0:
            failures.append(f"stopped pool: a released lane still held its "
                            f"issue: {again.stderr.strip()!r}")
    return failures


def test_spawn_reconciles(env: Env) -> list[str]:
    """A spawn notices a closed window without a list in between."""
    cfg = env.default_config()
    cfg["pools"]["minimax"] = 1
    env.write_config(cfg)
    failures = []
    first = env.spawn(1)
    if first.returncode != 0:
        return [f"spawn reconciles: the first spawn failed: "
                f"{first.stderr.strip()!r}"]
    env.remove_window("tooling-1")
    second = env.spawn(2)
    if second.returncode != 0:
        failures.append(f"spawn reconciles: a closed window still held its "
                        f"pool unit: {second.stderr.strip()!r}")
    return failures


def test_same_window(env: Env) -> list[str]:
    """Two live leases cannot share a window name.

    A window name is a tmux target, and an ambiguous target is a tool
    acting on whichever lane tmux picks.
    """
    failures = []
    first = env.spawn(5, extra=("--window", "shared"))
    if first.returncode != 0:
        return [f"same window: the first spawn failed: "
                f"{first.stderr.strip()!r}"]
    second = env.spawn(6, extra=("--window", "shared"))
    if second.returncode == 0:
        failures.append("same window: a second lease took a live window name")
    elif "lease 1" not in second.stderr:
        failures.append(f"same window: the refusal did not name the holder: "
                        f"{second.stderr.strip()!r}")
    return failures


def test_old_database(env: Env) -> list[str]:
    """A database from an earlier version is brought up to date.

    The schema is created with CREATE TABLE IF NOT EXISTS, so a database
    that already carries the table keeps the shape it was made with, and
    the columns added since are added on the way in.
    """
    failures = []
    first = env.spawn(5)
    if first.returncode != 0:
        return [f"old database: the first spawn failed: "
                f"{first.stderr.strip()!r}"]
    older = sqlite3.connect(env.db, isolation_level=None, timeout=30)
    older.execute("ALTER TABLE leases DROP COLUMN window_opened_at")
    older.close()
    listing = env.run("list", "--json")
    if listing.returncode != 0:
        failures.append(f"old database: the read failed: "
                        f"{listing.stderr.strip()!r}")
    again = env.spawn(6)
    if again.returncode != 0:
        failures.append(f"old database: the spawn failed: "
                        f"{again.stderr.strip()!r}")
    return failures


def leases_in(env: Env) -> int:
    """How many leases the database holds, read without the tool."""
    try:
        conn = sqlite3.connect(f"file:{env.db}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return 0
    try:
        return conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def test_claim_in_flight(env: Env) -> list[str]:
    """A lease still opening its window keeps its pool unit.

    The claim comes before the window, so a second caller that reconciles
    in between has to see the first lane as starting.
    """
    cfg = env.default_config()
    cfg["pools"]["minimax"] = 1
    env.write_config(cfg)
    failures = []
    (env.data / "hold_new_window").write_text("")
    first = subprocess.Popen(
        [str(env.lane), "spawn", "--repo", REPO, "--issue", "1", "--harness",
         "hermes", "--profile", "minimax", "--session", "coord-a"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env.env())
    deadline = time.time() + 30
    while time.time() < deadline and not leases_in(env):
        time.sleep(0.05)
    second = env.spawn(2)
    (env.data / "release_new_window").write_text("")
    _, err = first.communicate()
    if second.returncode == 0:
        failures.append("claim in flight: a lane whose window was not open "
                        "yet gave its pool unit up")
    elif "minimax" not in second.stderr:
        failures.append(f"claim in flight: the refusal did not name the "
                        f"pool: {second.stderr.strip()!r}")
    if first.returncode != 0:
        failures.append(f"claim in flight: the first spawn failed: "
                        f"{err.strip()!r}")
    return failures


def branch_exists(world: Env, branch: str) -> bool:
    """Whether the clone still carries a branch."""
    proc = subprocess.run(["git", "-C", str(world.clone), "rev-parse",
                           "--verify", "--quiet", f"refs/heads/{branch}"],
                          capture_output=True, text=True)
    return proc.returncode == 0


def test_interrupted(env: Env) -> list[str]:
    """A spawn that is interrupted undoes what it did.

    A signal is an ending like any other: the lane that was half built
    leaves no lease, no window, no worktree and no label behind, and the
    exit status still says how the spawn ended.
    """
    failures = []

    def spawn(world: Env, issue: int) -> subprocess.CompletedProcess:
        proc = subprocess.Popen(
            [str(world.lane), "spawn", "--repo", REPO, "--issue", str(issue),
             "--harness", "hermes", "--profile", "minimax", "--session",
             "coord-a"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=world.env())
        out, err = proc.communicate()
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    # Interrupted during the label: the label goes back off.
    world = env.sibling("label")
    (world.data / "kill_on_issue_edit").write_text("")
    result = spawn(world, 1)
    if result.returncode != 143:
        failures.append(f"interrupted: the exit status was "
                        f"{result.returncode}, not 143")
    if world.leases():
        failures.append(f"interrupted: the lease survived the label: "
                        f"{world.leases()}")
    if "--remove-label" not in world.log("gh.log"):
        failures.append(f"interrupted: the label was not taken back off: "
                        f"{world.log('gh.log')!r}")

    # Interrupted during the worktree: no worktree, no branch, no label.
    world = env.sibling("worktree")
    write_stub_git(world.bin)
    (world.data / "kill_on_fetch").write_text("")
    result = spawn(world, 1)
    if result.returncode != 143:
        failures.append(f"interrupted: the exit status was "
                        f"{result.returncode}, not 143")
    if world.leases():
        failures.append(f"interrupted: the lease survived the worktree: "
                        f"{world.leases()}")
    if (world.worktree_root / "tooling-1").exists():
        failures.append("interrupted: the worktree was left behind")
    if branch_exists(world, "lane/1"):
        failures.append("interrupted: the branch was left behind")
    if "--remove-label" not in world.log("gh.log"):
        failures.append(f"interrupted: the label was not taken back off: "
                        f"{world.log('gh.log')!r}")

    # Interrupted after the worktree was cut: the worktree and the branch
    # go, and the label with them.
    world = env.sibling("worktree-cut")
    write_stub_git(world.bin)
    (world.data / "kill_on_worktree_add").write_text("")
    result = spawn(world, 1)
    if result.returncode != 143:
        failures.append(f"interrupted: the exit status was "
                        f"{result.returncode}, not 143")
    if world.leases():
        failures.append(f"interrupted: the lease survived the worktree: "
                        f"{world.leases()}")
    if (world.worktree_root / "tooling-1").exists():
        failures.append("interrupted: the worktree was left behind")
    if branch_exists(world, "lane/1"):
        failures.append("interrupted: the branch was left behind")
    if "--remove-label" not in world.log("gh.log"):
        failures.append(f"interrupted: the label was not taken back off: "
                        f"{world.log('gh.log')!r}")

    # Interrupted after the window opened: the window closes, and the
    # worktree and the label go with it.
    world = env.sibling("window")
    write_stub_git(world.bin)
    (world.data / "kill_on_new_window").write_text("")
    result = spawn(world, 1)
    if result.returncode != 143:
        failures.append(f"interrupted: the exit status was "
                        f"{result.returncode}, not 143")
    if world.leases():
        failures.append(f"interrupted: the lease survived the window: "
                        f"{world.leases()}")
    windows = (world.data / "windows")
    if windows.exists() and "tooling-1" in windows.read_text():
        failures.append("interrupted: the window was left open")
    if (world.worktree_root / "tooling-1").exists():
        failures.append("interrupted: the worktree was left behind")
    if branch_exists(world, "lane/1"):
        failures.append("interrupted: the branch was left behind")
    if "--remove-label" not in world.log("gh.log"):
        failures.append(f"interrupted: the label was not taken back off: "
                        f"{world.log('gh.log')!r}")
    # The undo runs backwards: the window closes before the worktree it
    # holds goes, and the label comes off last.
    calls = world.log("calls.log")
    order = [calls.find(name) for name in
             ("kill-window", "worktree remove", "--remove-label")]
    if -1 in order or order != sorted(order):
        failures.append(f"interrupted: the undo ran in the wrong order: "
                        f"{calls!r}")
    return failures


def test_refuses_taken_places(env: Env) -> list[str]:
    """A spawn whose place is taken by something unleased is refused.

    The claim refuses the places a live lease holds and names the
    holder. These are the places nothing leased: a worktree cut by
    hand, a branch from an earlier run, a window a person opened.
    """
    failures = []

    world = env.sibling("worktree")
    taken = world.worktree_root / "tooling-1"
    taken.mkdir(parents=True)
    (taken / "notes").write_text("mine\n")
    result = world.spawn(1)
    if result.returncode == 0:
        failures.append("taken places: a spawn took a worktree path that "
                        "was already there")
    elif "worktree" not in result.stderr:
        failures.append(f"taken places: the refusal did not name the "
                        f"worktree: {result.stderr.strip()!r}")
    if world.leases():
        failures.append(f"taken places: the refused spawn left a lease: "
                        f"{world.leases()}")
    if "--add-label" in world.log("gh.log"):
        failures.append("taken places: the refused spawn added the label")
    if "new-window" in world.log("tmux.log"):
        failures.append("taken places: the refused spawn opened a window")
    if not (taken / "notes").exists():
        failures.append("taken places: the worktree path was touched")

    world = env.sibling("branch")
    subprocess.run(["git", "-C", str(world.clone), "branch", "lane/1"],
                   check=True, capture_output=True)
    result = world.spawn(1)
    if result.returncode == 0:
        failures.append("taken places: a spawn took a branch that was "
                        "already there")
    elif "branch" not in result.stderr:
        failures.append(f"taken places: the refusal did not name the branch: "
                        f"{result.stderr.strip()!r}")
    if world.leases():
        failures.append(f"taken places: the refused spawn left a lease: "
                        f"{world.leases()}")
    if "--add-label" in world.log("gh.log"):
        failures.append("taken places: the refused spawn added the label")
    if "new-window" in world.log("tmux.log"):
        failures.append("taken places: the refused spawn opened a window")
    if not branch_exists(world, "lane/1"):
        failures.append("taken places: the branch was touched")

    world = env.sibling("window")
    (world.data / "windows").write_text("tooling-1\n")
    result = world.spawn(1)
    if result.returncode == 0:
        failures.append("taken places: a spawn took a window name that was "
                        "already open")
    elif "window" not in result.stderr:
        failures.append(f"taken places: the refusal did not name the window: "
                        f"{result.stderr.strip()!r}")
    if world.leases():
        failures.append(f"taken places: the refused spawn left a lease: "
                        f"{world.leases()}")
    if "--add-label" in world.log("gh.log"):
        failures.append("taken places: the refused spawn added the label")
    if "new-window" in world.log("tmux.log"):
        failures.append("taken places: the refused spawn opened a window")
    if "tooling-1" not in (world.data / "windows").read_text():
        failures.append("taken places: the window was closed")
    return failures


def test_undo_leaves_a_leased_branch(env: Env) -> list[str]:
    """A failed spawn leaves a branch another lane holds alone.

    The branch is refused before the claim when nothing holds it. A
    lane's own branch is held, so the refusal falls to the cut, and the
    undo has to leave that branch alone. The worktree goes first, because
    git protects a checked-out branch from deletion.
    """
    failures = []
    first = env.spawn(1)
    if first.returncode != 0:
        return [f"leased branch: the first spawn failed: "
                f"{first.stderr.strip()!r}"]
    subprocess.run(["git", "-C", str(env.clone), "worktree", "remove",
                    "--force", str(env.worktree_root / "tooling-1")],
                   check=True, capture_output=True)
    second = env.spawn(2, extra=("--branch", "lane/1"))
    if second.returncode == 0:
        failures.append("leased branch: a second lane took a branch that "
                        "was already there")
    if not branch_exists(env, "lane/1"):
        failures.append("leased branch: the branch of the first lane was "
                        "deleted")
    leases = env.leases()
    if len(leases) != 1 or leases[0]["issue"] != 1:
        failures.append(f"leased branch: the leases are {leases}")
    return failures


def test_finish_merged(env: Env) -> list[str]:
    """A merged lane comes down: window, worktree, branch, caches, lease."""
    failures = []
    if not spawn_merged(env, 5):
        return ["finish merged: the spawn failed"]
    worktree = lane_worktree(env, 5)
    result = env.run("finish", "1")
    if result.returncode != 0:
        return [f"finish merged: the finish failed: {result.stderr.strip()!r}"]
    if "tooling-5" in (env.data / "windows").read_text():
        failures.append("finish merged: the window is still open")
    if worktree.exists():
        failures.append("finish merged: the worktree is still there")
    if branch_exists(env, "lane/5"):
        failures.append("finish merged: the branch is still there")
    if "remote-prune --prune --tree tooling_5" not in env.log("calls.log"):
        failures.append(f"finish merged: the builder caches were not pruned "
                        f"under the tree's name: {env.log('calls.log')!r}")
    leases = env.leases()
    if not leases or leases[0]["state"] != "released":
        failures.append(f"finish merged: the lease was not released: {leases}")
    if "--remove-label" not in env.log("gh.log"):
        failures.append("finish merged: the label was not taken off")
    return failures


def test_finish_tracking_ref(env: Env) -> list[str]:
    """A merged lane whose remote branch is gone takes its tracking ref too.

    Another lane's tracking ref stays: a prune that dropped every stale
    ref in the clone would take other lanes' work with it.
    """
    failures = []
    head = spawn_merged(env, 5)
    if not head:
        return ["finish tracking ref: the spawn failed"]
    subprocess.run(["git", "-C", str(env.clone), "push", "-q", "origin",
                    "lane/5"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(env.origin), "update-ref", "-d",
                    "refs/heads/lane/5"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(env.clone), "update-ref",
                    "refs/remotes/origin/other", head], check=True,
                   capture_output=True)
    refs = git_out(env.clone, "for-each-ref", "--format=%(refname)",
                   "refs/remotes/origin")
    if "refs/remotes/origin/lane/5" not in refs:
        return [f"finish tracking ref: the clone carries no tracking ref: "
                f"{refs}"]
    result = env.run("finish", "1")
    if result.returncode != 0:
        return [f"finish tracking ref: the finish failed: "
                f"{result.stderr.strip()!r}"]
    refs = git_out(env.clone, "for-each-ref", "--format=%(refname)",
                   "refs/remotes/origin")
    if "refs/remotes/origin/lane/5" in refs:
        failures.append("finish tracking ref: the tracking ref of the "
                        "finished branch is still there")
    if "refs/remotes/origin/other" not in refs:
        failures.append(f"finish tracking ref: another lane's tracking ref "
                        f"was dropped: {refs}")
    return failures


def test_finish_processes(env: Env) -> list[str]:
    """A process still working in the lane's worktree goes with the lane."""
    failures = []
    if not spawn_merged(env, 5):
        return ["finish processes: the spawn failed"]
    inside = subprocess.Popen(["sleep", "600"], cwd=str(lane_worktree(env, 5)),
                              start_new_session=True)
    elsewhere = subprocess.Popen(["sleep", "600"], cwd=str(env.tmp),
                                 start_new_session=True)
    try:
        result = env.run("finish", "1")
        if result.returncode != 0:
            return [f"finish processes: the finish failed: "
                    f"{result.stderr.strip()!r}"]
        if inside.poll() is None:
            failures.append("finish processes: the process working in the "
                            "worktree is still running")
        if str(inside.pid) not in result.stdout:
            failures.append(f"finish processes: the process was not reported: "
                            f"{result.stdout!r}")
        if elsewhere.poll() is not None:
            failures.append("finish processes: a process outside the worktree "
                            "was stopped")
    finally:
        for process in (inside, elsewhere):
            if process.poll() is None:
                process.kill()
    return failures


def untouched(env: Env, issue: int, failures: list[str], why: str) -> None:
    """Nothing about the lane was touched."""
    if not lane_worktree(env, issue).exists():
        failures.append(f"{why}: the worktree is gone")
    if not branch_exists(env, f"lane/{issue}"):
        failures.append(f"{why}: the branch is gone")
    if f"tooling-{issue}" not in (env.data / "windows").read_text():
        failures.append(f"{why}: the window was closed")
    leases = env.leases()
    if not leases or leases[0]["state"] == "released":
        failures.append(f"{why}: the lease was released")
    if "--remove-label" in env.log("gh.log"):
        failures.append(f"{why}: the label was taken off")
    if "remote-prune" in env.log("calls.log"):
        failures.append(f"{why}: the builder caches were pruned")


def test_finish_open_pr(env: Env) -> list[str]:
    """An open pull request leaves the lane alone."""
    failures = []
    spawn = env.spawn(5)
    if spawn.returncode != 0:
        return ["finish open pr: the spawn failed"]
    head = git_out(lane_worktree(env, 5), "rev-parse", "HEAD")
    set_pr(env, "lane/5", 12, "OPEN", head)
    env.set_pane("tooling-5", HERMES_IDLE)
    result = env.run("finish", "1")
    if result.returncode == 0:
        failures.append("finish open pr: the lane was taken down with an "
                        "open pull request")
    elif "OPEN" not in result.stderr:
        failures.append(f"finish open pr: the refusal did not name the pull "
                        f"request's state: {result.stderr.strip()!r}")
    untouched(env, 5, failures, "finish open pr")
    return failures


def test_finish_busy(env: Env) -> list[str]:
    """A lane that is still working is left alone."""
    failures = []
    if not spawn_merged(env, 5):
        return ["finish busy: the spawn failed"]
    env.set_pane("tooling-5", HERMES_BUSY)
    result = env.run("finish", "1")
    if result.returncode == 0:
        failures.append("finish busy: the lane was taken down while it was "
                        "still working")
    elif "busy" not in result.stderr:
        failures.append(f"finish busy: the refusal did not name the state: "
                        f"{result.stderr.strip()!r}")
    untouched(env, 5, failures, "finish busy")
    return failures


def test_finish_dirty(env: Env) -> list[str]:
    """A worktree with changes in it is left alone."""
    failures = []
    if not spawn_merged(env, 5):
        return ["finish dirty: the spawn failed"]
    worktree = lane_worktree(env, 5)
    (worktree / "notes").write_text("work in progress\n")
    result = env.run("finish", "1")
    if result.returncode == 0:
        failures.append("finish dirty: the lane was taken down with changes "
                        "in the worktree")
    elif str(worktree) not in result.stderr:
        failures.append(f"finish dirty: the refusal did not name the dirty "
                        f"worktree: {result.stderr.strip()!r}")
    untouched(env, 5, failures, "finish dirty")
    return failures


def test_finish_head_mismatch(env: Env) -> list[str]:
    """A worktree that is ahead of the pull request's head is refused."""
    failures = []
    if not spawn_merged(env, 5):
        return ["finish head mismatch: the spawn failed"]
    worktree = lane_worktree(env, 5)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@example.com",
                    "-c", "user.name=T", "commit", "--allow-empty", "-q",
                    "-m", "wip"], check=True, capture_output=True)
    ahead = git_out(worktree, "rev-parse", "HEAD")
    result = env.run("finish", "1")
    if result.returncode == 0:
        failures.append("finish head mismatch: the lane was taken down with "
                        "commits the pull request does not carry")
    elif ahead not in result.stderr:
        failures.append(f"finish head mismatch: the refusal did not name the "
                        f"head it found: {result.stderr.strip()!r}")
    untouched(env, 5, failures, "finish head mismatch")
    return failures


def test_finish_no_pr(env: Env) -> list[str]:
    """A branch with no pull request is left alone."""
    failures = []
    spawn = env.spawn(5)
    if spawn.returncode != 0:
        return ["finish no pr: the spawn failed"]
    env.set_pane("tooling-5", HERMES_IDLE)
    result = env.run("finish", "1")
    if result.returncode == 0:
        failures.append("finish no pr: the lane was taken down with no pull "
                        "request")
    elif "lane/5" not in result.stderr:
        failures.append(f"finish no pr: the refusal did not name the branch: "
                        f"{result.stderr.strip()!r}")
    untouched(env, 5, failures, "finish no pr")
    return failures


def test_finish_abandon(env: Env) -> list[str]:
    """A closed pull request takes the lane down and keeps the branch."""
    failures = []
    spawn = env.spawn(5)
    if spawn.returncode != 0:
        return ["finish abandon: the spawn failed"]
    worktree = lane_worktree(env, 5)
    head = git_out(worktree, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(env.clone), "push", "-q", "origin",
                    "lane/5"], check=True, capture_output=True)
    set_pr(env, "lane/5", 13, "CLOSED", head)
    env.set_pane("tooling-5", HERMES_IDLE)
    result = env.run("finish", "--abandon", "1")
    if result.returncode != 0:
        return [f"finish abandon: the finish failed: {result.stderr.strip()!r}"]
    if worktree.exists():
        failures.append("finish abandon: the worktree is still there")
    if branch_exists(env, "lane/5"):
        failures.append("finish abandon: the local branch is still there")
    if not git_out(env.clone, "ls-remote", "origin", "refs/heads/lane/5"):
        failures.append("finish abandon: the remote branch was deleted")
    leases = env.leases()
    if not leases or leases[0]["state"] != "released":
        failures.append(f"finish abandon: the lease was not released: {leases}")
    return failures


def test_finish_abandon_ahead(env: Env) -> list[str]:
    """A lane ahead of the remote branch is left alone."""
    failures = []
    spawn = env.spawn(5)
    if spawn.returncode != 0:
        return ["finish abandon ahead: the spawn failed"]
    worktree = lane_worktree(env, 5)
    head = git_out(worktree, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(env.clone), "push", "-q", "origin",
                    "lane/5"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@example.com",
                    "-c", "user.name=T", "commit", "--allow-empty", "-q",
                    "-m", "wip"], check=True, capture_output=True)
    ahead = git_out(worktree, "rev-parse", "HEAD")
    set_pr(env, "lane/5", 13, "CLOSED", head)
    env.set_pane("tooling-5", HERMES_IDLE)
    result = env.run("finish", "--abandon", "1")
    if result.returncode == 0:
        failures.append("finish abandon ahead: the lane was taken down with "
                        "work the remote does not carry")
    elif ahead not in result.stderr:
        failures.append(f"finish abandon ahead: the refusal did not name the "
                        f"head it found: {result.stderr.strip()!r}")
    untouched(env, 5, failures, "finish abandon ahead")
    return failures


def test_finish_abandon_merged(env: Env) -> list[str]:
    """`--abandon` on a merged pull request points at the plain finish."""
    failures = []
    if not spawn_merged(env, 5):
        return ["finish abandon merged: the spawn failed"]
    result = env.run("finish", "--abandon", "1")
    if result.returncode == 0:
        failures.append("finish abandon merged: a merged pull request was "
                        "abandoned")
    elif "lane finish" not in result.stderr:
        failures.append(f"finish abandon merged: the refusal did not point "
                        f"at the plain finish: {result.stderr.strip()!r}")
    untouched(env, 5, failures, "finish abandon merged")
    return failures


def test_finish_prune_fails(env: Env) -> list[str]:
    """A prune that fails is reported, and the lane still comes down."""
    failures = []
    if not spawn_merged(env, 5):
        return ["finish prune fails: the spawn failed"]
    (env.data / "fail_remote_prune").write_text("")
    result = env.run("finish", "1")
    if result.returncode != 0:
        failures.append(f"finish prune fails: the finish failed: "
                        f"{result.stderr.strip()!r}")
    if "caches" not in result.stderr:
        failures.append(f"finish prune fails: the failure was not reported: "
                        f"{result.stderr.strip()!r}")
    if lane_worktree(env, 5).exists():
        failures.append("finish prune fails: the worktree is still there")
    leases = env.leases()
    if not leases or leases[0]["state"] != "released":
        failures.append(f"finish prune fails: the lease was not released: "
                        f"{leases}")
    return failures


def test_finish_merged_lanes(env: Env) -> list[str]:
    """`--merged` takes down every lane whose pull request merged."""
    failures = []
    for issue in (5, 6, 7):
        if not spawn_merged(env, issue, number=10 + issue):
            return [f"finish merged lanes: the spawn for {issue} failed"]
    set_pr(env, "lane/7", 17, "OPEN",
           git_out(lane_worktree(env, 7), "rev-parse", "HEAD"))
    result = env.run("finish", "--merged")
    if result.returncode != 0:
        failures.append(f"finish merged lanes: the finish failed: "
                        f"{result.stderr.strip()!r}")
    for issue in (5, 6):
        if lane_worktree(env, issue).exists():
            failures.append(f"finish merged lanes: the worktree for {issue} "
                            f"is still there")
        if branch_exists(env, f"lane/{issue}"):
            failures.append(f"finish merged lanes: the branch for {issue} is "
                            f"still there")
    if not lane_worktree(env, 7).exists():
        failures.append("finish merged lanes: the open lane's worktree is gone")
    if not branch_exists(env, "lane/7"):
        failures.append("finish merged lanes: the open lane's branch is gone")
    leases = {lease["id"]: lease for lease in env.leases()}
    for number in (1, 2):
        if leases.get(number, {}).get("state") != "released":
            failures.append(f"finish merged lanes: lease {number} was not "
                            f"released: {leases}")
    if leases.get(3, {}).get("state") == "released":
        failures.append("finish merged lanes: the open lane was released")
    return failures


def test_list_pr(env: Env) -> list[str]:
    """The list says which lanes are finished."""
    failures = []
    if not spawn_merged(env, 5):
        return ["list pr: the spawn failed"]
    spawn = env.spawn(6)
    if spawn.returncode != 0:
        return ["list pr: the second spawn failed"]
    set_pr(env, "lane/6", 21, "OPEN",
           git_out(lane_worktree(env, 6), "rev-parse", "HEAD"))
    env.set_pane("tooling-6", HERMES_IDLE)
    leases = {lease["id"]: lease for lease in env.leases()}
    if leases.get(1, {}).get("pr") != 12:
        failures.append(f"list pr: the merged lane does not show its pull "
                        f"request: {leases}")
    if leases.get(1, {}).get("pr_state") != "MERGED":
        failures.append(f"list pr: the merged lane does not read as merged: "
                        f"{leases}")
    if leases.get(2, {}).get("pr") != 21:
        failures.append(f"list pr: the open lane does not show its pull "
                        f"request: {leases}")
    if leases.get(2, {}).get("pr_state") != "OPEN":
        failures.append(f"list pr: the open lane does not read as open: "
                        f"{leases}")
    listing = env.run("list")
    if "MERGED" not in listing.stdout:
        failures.append(f"list pr: the list does not show the pull request's "
                        f"state: {listing.stdout!r}")
    return failures


CASES = [
    ("race", test_race),
    ("read-under-a-write-lock", test_read_under_a_write_lock),
    ("pool-sum", test_pool_sum),
    ("pool-across-harnesses", test_pool_across_harnesses),
    ("same-issue", test_same_issue),
    ("same-worktree", test_same_worktree),
    ("released-frees", test_released_frees),
    ("stopped-pool", test_stopped_holds_no_pool_unit),
    ("spawn-reconciles", test_spawn_reconciles),
    ("same-window", test_same_window),
    ("claim-in-flight", test_claim_in_flight),
    ("old-database", test_old_database),
    ("interrupted", test_interrupted),
    ("taken-places", test_refuses_taken_places),
    ("leased-branch", test_undo_leaves_a_leased_branch),
    ("step-failure", test_step_failure),
    ("label-failure", test_label_failure),
    ("config", test_config),
    ("order", test_order),
    ("stopped", test_stopped),
    ("states", test_states),
    ("release-twice", test_release_twice),
    ("finish-merged", test_finish_merged),
    ("finish-tracking-ref", test_finish_tracking_ref),
    ("finish-processes", test_finish_processes),
    ("finish-open-pr", test_finish_open_pr),
    ("finish-busy", test_finish_busy),
    ("finish-dirty", test_finish_dirty),
    ("finish-head-mismatch", test_finish_head_mismatch),
    ("finish-no-pr", test_finish_no_pr),
    ("finish-abandon", test_finish_abandon),
    ("finish-abandon-ahead", test_finish_abandon_ahead),
    ("finish-abandon-merged", test_finish_abandon_merged),
    ("finish-prune-fails", test_finish_prune_fails),
    ("finish-merged-lanes", test_finish_merged_lanes),
    ("list-pr", test_list_pr),
]


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for name, case in CASES:
            directory = tmp / name
            directory.mkdir()
            env = Env(directory)
            try:
                failures.extend(case(env))
            except AssertionError as exc:
                failures.append(f"{name}: unexpected assertion error: {exc}")
    if failures:
        print("FAIL:")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"PASS {len(CASES)} lane cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
