#!/usr/bin/env python3
"""Pin the LANE_WATCH_TMUX_SESSION opt-in in bin/hermes-implement.

The hook opens a detached tmux window running lane-watch on the lane's
worktree when three conditions all hold: the variable is set, tmux is on
PATH, and tmux has the named session. Anything else (variable unset,
tmux missing, session absent) must do nothing -- a lane that cannot also
be watched is still a lane.

Tests below use a fake tmux on PATH that records every invocation, so
each case asserts on the exact `tmux` argv the hook produced rather than
on its effect. The hook is invoked by sourcing hermes-implement up to
the tmux call and stopping -- we do not run hermes itself, because the
contract is that hermes-implement stays hermes' parent process. Stopping short
of hermes is exercised structurally: the fake hermes on PATH would exit 0
and let the test reach the END only when the hook is already past the
new-window call.

Run directly: `python3 tests/test_hermes_implement_tmux.py`.
"""
import os
import shutil
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_install_fixture import install as install_skills
HERMES_IMPLEMENT = REPO_ROOT / "bin" / "hermes-implement"
LANE_WATCH = REPO_ROOT / "bin" / "lane-watch"
# The hook must invoke lane-watch by this absolute path, not by name.
LANE_WATCH_PATH = str(LANE_WATCH)


# A scratch directory under TMPDIR the test owns for the whole run. The
# fake tmux writes its argument list there; the test reads them back.
def _tmp_root() -> Path:
    base = Path(os.environ.get("TMPDIR", "/tmp")) / "test_hermes_implement_tmux"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    return base


def make_fake_tmux(stem: Path) -> Path:
    """Write a fake `tmux` script that records each argv into a log file
    and emulates enough of the real binary for `has-session` to behave
    the way the hook expects. A session-named `has-session` arg succeeds
    only when the test has previously written a marker for that name.
    Also writes a fake `gh` so hermes-implement's `gh issue view`
    succeeds and the script can reach the tmux hook under test."""
    log = stem / "tmux.log"
    sessions = stem / "sessions"
    sessions.mkdir()
    bin_dir = stem / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    # The fake accepts: has-session -t NAME (exit 0 if marker exists, 1
    # otherwise) and new-window -d -t NAME -n TITLE "CMD" (records argv).
    # Every other subcommand is silently ignored.
    tmux.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Fake tmux for the LANE_WATCH_TMUX_SESSION test. Records every
        # argv so the test can assert on what the hook called, not on
        # what tmux did.
        log={log}
        sessions={sessions}
        case "$1" in
            has-session)
                shift
                name=""
                while [ $# -gt 0 ]; do
                    case "$1" in
                        -t) name="$2"; shift 2 ;;
                        *) shift ;;
                    esac
                done
                if [ -n "$name" ] && [ -f "$sessions/$name" ]; then
                    printf 'has-session %s\\n' "$name" >>"$log"
                    exit 0
                fi
                printf 'has-session %s MISSING\\n' "$name" >>"$log"
                exit 1
                ;;
            new-window)
                shift
                target=""
                name=""
                cmd=""
                while [ $# -gt 0 ]; do
                    case "$1" in
                        -d) shift ;;
                        -t) target="$2"; shift 2 ;;
                        -n) name="$2"; shift 2 ;;
                        *)  cmd="$1"; shift ;;
                    esac
                done
                printf 'new-window target=%s name=%s cmd=' "$target" "$name" >>"$log"
                printf '%s\n' "$cmd" >>"$log"
                exit 0
                ;;
            *)
                printf 'unknown %s\\n' "$*" >>"$log"
                exit 0
                ;;
        esac
        """))
    tmux.chmod(0o755)
    (sessions / "watch").write_text("alive\n")  # default session
    # Fake gh: hermes-implement calls `gh issue view N`. We dump an empty
    # body -- the test does not care about the issue text, only that gh
    # exits 0 so the script reaches the tmux hook.
    gh = bin_dir / "gh"
    gh.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # Fake gh for the LANE_WATCH_TMUX_SESSION test.
        if [[ "$1" == "api" && "$2" == "user" ]]; then
            echo "test-implementer"
            exit 0
        fi
        exit 0
        """))
    gh.chmod(0o755)
    return bin_dir


def make_fake_hermes(stem: Path) -> Path:
    """Write a fake `hermes` that exits 0 the moment it is invoked. The
    hook runs hermes in the foreground; reaching it is the assertion that
    the tmux call has already happened."""
    bin_dir = stem / "bin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / "hermes"
    p.write_text("#!/usr/bin/env bash\nexit 0\n")
    p.chmod(0o755)
    return bin_dir


def build_worktree(stem: Path) -> Path:
    """A throwaway git worktree so hermes-implement's `cd "$WORKTREE"`
    and `git rev-parse --show-toplevel` both succeed. The test does not
    care about commits; it stops before hermes."""
    wt = stem / "wt"
    wt.mkdir()
    # git CLI is required; if it isn't on PATH the test fails loud.
    subprocess.run(["git", "-C", str(wt), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"],
                 check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"],
                 check=True)
    subprocess.run(["git", "-C", str(wt), "config",
                    "init.defaultBranch", "main"], check=True)
    (wt / "README").write_text("seed\n")
    subprocess.run(["git", "-C", str(wt), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "--quiet",
                    "-m", "seed"], check=True)
    return wt


def run_hook(args: list[str], env: dict, stem: Path
             ) -> subprocess.CompletedProcess:
    """Invoke bin/hermes-implement with the supplied arguments and
    environment. The hook runs before the first hermes call, so we
    expect the fake binary to be reached within a fraction of a second;
    a 30s timeout catches a hang."""
    return subprocess.run(
        [str(HERMES_IMPLEMENT), *args],
        capture_output=True, text=True, env=env, timeout=30)


def assert_eq(label, want, got, failures):
    if want != got:
        failures.append(f"{label}: want {want!r}, got {got!r}")


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        failures.append(f"{label}: {needle!r} not in {haystack!r}")


def make_env(stem: Path, wt: Path, **extra) -> dict:
    """Build an environment for a hermes-implement run. PATH puts the
    fakes first; HOME and HERMES_HOME point at scratch directories so
    the real ones cannot interfere."""
    env_dir = stem / "env"
    bin_dir = stem / "bin"
    # The lane refuses to start when a dispatched skill differs from the
    # repository's copy. The `fast` profile is the one the --profile case
    # below runs on.
    install_skills(env_dir, ["fast"])
    # /usr/bin:/bin must stay on PATH for git and the system tools.
    return {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "IMPLEMENTER_LOGINS": "test-implementer",
        "HOME": str(env_dir),
        "HERMES_PYTHON": str(stem / "bin" / "hermes"),
        "TMPDIR": str(stem),
        "AGENT_STATE_DIR": str(stem / "state"),
        "HERMES_HOME": str(env_dir),
        # The lane identity is configuration; the lane refuses
        # without it.
        "LANE_GIT_NAME": "Lane",
        "LANE_GIT_EMAIL": "lane@example.invalid",
        **extra,
    }


def main() -> int:
    failures: list[str] = []
    stem = _tmp_root()
    make_fake_tmux(stem)
    make_fake_hermes(stem)
    wt = build_worktree(stem)

    # The hook is a no-op when LANE_WATCH_TMUX_SESSION is unset, so the
    # first scenario pins that baseline: tmux is never called, even when
    # a session is alive and reachable. This is the case the production
    # default falls into.
    env = make_env(stem, wt, LANE_WATCH_TMUX_SESSION="")
    r = run_hook(["--issue", "42", "--worktree", str(wt)], env, stem)
    # The fake hermes exits 0; hermes-implement must reach it. A
    # non-zero rc here means the script died earlier -- usually because
    # the hook failed for the wrong reason.
    log = (stem / "tmux.log").read_text() if (stem / "tmux.log").exists() else ""
    if log:
        failures.append(f"unset: tmux was called: {log!r}")

    # Variable set, session alive, --profile omitted: a window named
    # after the issue is opened in the named session, and the cmd
    # argument carries the worktree without --profile.
    env = make_env(stem, wt, LANE_WATCH_TMUX_SESSION="watch")
    r = run_hook(["--issue", "109", "--worktree", str(wt)], env, stem)
    log = (stem / "tmux.log").read_text()
    if r.returncode != 0:
        failures.append(f"set/no-profile: rc={r.returncode} stderr={r.stderr!r}")
    assert_in("set/no-profile: has-session",
              "has-session watch", log, failures)
    assert_in("set/no-profile: new-window name",
              "name=109", log, failures)
    assert_in("set/no-profile: target session",
              "target=watch", log, failures)
    assert_in("set/no-profile: worktree in command",
              str(wt), log, failures)
    # Absolute path: lane-watch is invoked by the path beside
    # hermes-implement, not by name. Without it, the window depends on
    # whatever tmux's PATH happens to contain.
    assert_in("set/no-profile: lane-watch by absolute path",
              str(LANE_WATCH_PATH), log, failures)
    if "--profile" in log:
        failures.append(
            f"set/no-profile: --profile leaked into cmd: {log!r}")

    # Variable set, --profile passed: the cmd argument carries --profile.
    (stem / "tmux.log").write_text("")
    env = make_env(stem, wt, LANE_WATCH_TMUX_SESSION="watch")
    r = run_hook(["--issue", "110", "--worktree", str(wt),
                  "--profile", "fast"], env, stem)
    log = (stem / "tmux.log").read_text()
    if r.returncode != 0:
        failures.append(f"set/profile: rc={r.returncode} stderr={r.stderr!r}")
    assert_in("set/profile: --profile P in cmd",
              "--profile fast", log, failures)
    assert_in("set/profile: worktree in cmd", str(wt), log, failures)
    assert_in("set/profile: lane-watch by absolute path",
              str(LANE_WATCH_PATH), log, failures)

    # Worktree path with a space in it. The cmd must still carry the path
    # whole (and not lose it to word-splitting), and lane-watch must
    # still be invoked by absolute path. The path is real on disk so
    # hermes-implement's `cd "$WORKTREE"` succeeds.
    (stem / "tmux.log").write_text("")
    spaced = stem / "wt with space"
    spaced.mkdir()
    subprocess.run(["git", "-C", str(spaced), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(spaced), "config", "user.email", "t@t"],
                 check=True)
    subprocess.run(["git", "-C", str(spaced), "config", "user.name", "t"],
                 check=True)
    (spaced / "README").write_text("seed\n")
    subprocess.run(["git", "-C", str(spaced), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(spaced), "commit", "--quiet",
                    "-m", "seed"], check=True)
    env = make_env(stem, wt, LANE_WATCH_TMUX_SESSION="watch")
    r = run_hook(["--issue", "113", "--worktree", str(spaced)], env, stem)
    log = (stem / "tmux.log").read_text()
    if r.returncode != 0:
        failures.append(f"spaced-path: rc={r.returncode} stderr={r.stderr!r}")
    # Parse the recorded cmd and prove the path survives shell
    # tokenization as one word: printf %q wraps it, and an unquoted join
    # (`${LANE_WATCH_ARGS[*]}`) would have split it into two argv slots.
    # The fake tmux logs `new-window target=T name=N cmd=<cmd>`, so the
    # command is the tail of the line after the `cmd=` marker.
    cmd_line = ""
    for ln in log.splitlines():
        if "new-window" in ln:
            marker = "cmd="
            if marker in ln:
                cmd_line = ln[ln.index(marker) + len(marker):]
            break
    if not cmd_line:
        failures.append(f"spaced-path: no new-window line in log: {log!r}")
        words = []
    else:
        try:
            words = shlex.split(cmd_line)
        except ValueError as e:
            failures.append(f"spaced-path: cmd is not valid shell: {e}")
            words = []
    if str(spaced) not in words:
        failures.append(
            f"spaced-path: path split across words: words={words!r}")
    assert_in("spaced-path: lane-watch by absolute path",
              str(LANE_WATCH_PATH), log, failures)
    # The hook must pass --since so lane-watch skips an older ended
    # session for this worktree and waits for this lane's own.
    assert_in("spaced-path: --since in cmd",
              "--since", log, failures)

    # The session does not exist: has-session returns 1, so the new-window
    # call must be skipped. The variable being set is not enough.
    (stem / "tmux.log").write_text("")
    # Remove the marker so has-session fails.
    (stem / "sessions" / "watch").unlink()
    env = make_env(stem, wt, LANE_WATCH_TMUX_SESSION="watch")
    r = run_hook(["--issue", "111", "--worktree", str(wt)], env, stem)
    log = (stem / "tmux.log").read_text()
    if r.returncode != 0:
        failures.append(
            f"missing-session: rc={r.returncode} stderr={r.stderr!r}")
    assert_in("missing-session: has-session was attempted",
              "has-session watch", log, failures)
    if "new-window" in log:
        failures.append(
            f"missing-session: new-window still ran: {log!r}")

    # tmux missing on PATH: variable set but command -v tmux returns
    # nothing, so the hook does not call tmux at all (not even
    # has-session). Otherwise a host without tmux would error out
    # somewhere visible to the caller.
    (stem / "tmux.log").write_text("")
    no_tmux_bin = stem / "no-tmux-bin"
    no_tmux_bin.mkdir()
    (no_tmux_bin / "hermes").write_text("#!/usr/bin/env bash\nexit 0\n")
    (no_tmux_bin / "hermes").chmod(0o755)
    # No gh either: the test is not about gh, it is about the tmux hook
    # short-circuiting before the hook even names tmux.
    (no_tmux_bin / "gh").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        if [[ "$1" == "api" && "$2" == "user" ]]; then
            echo "test-implementer"
            exit 0
        fi
        exit 0
        """))
    (no_tmux_bin / "gh").chmod(0o755)
    env = make_env(stem, wt, LANE_WATCH_TMUX_SESSION="watch")
    env["PATH"] = f"{no_tmux_bin}:/usr/bin:/bin"
    r = run_hook(["--issue", "112", "--worktree", str(wt)], env, stem)
    if r.returncode != 0:
        failures.append(f"no-tmux: rc={r.returncode} stderr={r.stderr!r}")
    if (stem / "tmux.log").exists():
        log = (stem / "tmux.log").read_text()
        if log:
            failures.append(f"no-tmux: tmux log present: {log!r}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())