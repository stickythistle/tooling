#!/usr/bin/env python3
"""Pin that a retried attempt resumes this run's session, by name.

`hermes-implement` resumes an attempt with `--continue`. A bare
`--continue` does not name a session: the CLI resolves it to the
terminal's breadcrumb, or to the most recent session in the workspace,
or to the most recent session anywhere. Two runs in the same worktree
can therefore resume each other's session, and the attempt answers with
the wrong context.

So the run names its session before the first attempt and every attempt
resumes that name. The name is resolved by the CLI's
`--continue <name> --create-if-missing`: the first attempt creates the
session, and the rest continue it. Naming beats reading the id back off
the agent's stderr, because the CLI prints the id once the conversation
ends, and a retry is most often after a timeout.

The fake agent is the boundary: it reports a session id the way the
real CLI does -- only when the attempt ends cleanly -- and logs the
`--continue` it was given so the test can read back what the retry
actually asked for.

Run directly: `python3 tests/test_hermes_implement_session.py`.
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_install_fixture import install as install_skills
BIN = REPO_ROOT / "bin" / "hermes-implement"

# A fake `gh` that answers the one command hermes-implement issues.
FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # `gh api user` answers who the call would act as, which `gh_as`
    # checks against IMPLEMENTER_LOGINS.
    if [[ "$1" == "api" && "$2" == "user" ]]; then
        echo "test-implementer"
        exit 0
    fi
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        cat <<'BODY'
    title:   session resume regression
    state:   OPEN
    BODY
        exit 0
    fi
    exit 0
    """)

# The fake agent.
#
# It reports a session id on stderr the way the real CLI does: a blank
# line, then the id on a line of its own. FAKE_HERMES_SESSION_IDS is a
# colon-separated list of ids, one per attempt; an entry of `-` means
# the attempt reports no id at all.
#
# The id is written only when the attempt ends cleanly. The real CLI
# prints it once the conversation ends, so an attempt the timeout killed
# leaves no id on its stderr -- and reading the id back after the
# attempt is what the run must not depend on.
#
# It logs the `--continue` it was given. The value matters as much as
# the flag's presence: a bare `--continue` is the thing the run must
# not do.
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys, time

    args = sys.argv[1:]
    log = os.environ.get("FAKE_HERMES_LOG", "/tmp/fake_hermes.log")
    state = os.environ.get("FAKE_HERMES_STATE", "/tmp/fake_hermes.state")

    # The value of --continue, if one was given. `--continue` with no
    # value is the bare form the run must avoid. `has_continue` says
    # whether the flag was there at all: an attempt that passed no flag
    # (attempt 1) and an attempt that passed a bare `--continue` both
    # have no value, and they are different things.
    #
    # A bare `--continue` and `--continue ''` are different argv: the
    # first has the flag as its last element, the second has an empty
    # string after it. The fake tells them apart, because the CLI does:
    # an empty value names an empty session, which it refuses.
    has_continue = "--continue" in args
    continue_value = ""
    continue_is_bare = False
    if has_continue:
        i = args.index("--continue")
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            continue_value = args[i + 1]
        else:
            continue_is_bare = True
    has_create_if_missing = "--create-if-missing" in args

    try:
        with open(state) as f:
            step = int((f.read() or "0").strip() or "0")
    except (FileNotFoundError, ValueError):
        step = 0
    with open(state, "w") as f:
        f.write(str(step + 1))

    with open(log, "a") as f:
        f.write(f"call step={step} has_continue={has_continue} "
                f"continue_bare={continue_is_bare} "
                f"create_if_missing={has_create_if_missing} "
                f"continue_value={continue_value!r}\\n")
        f.flush()

    # The plan says what each attempt does. The default is a timeout on
    # attempt 1 and a clean exit on attempt 2, which is the shape the
    # existing suite uses.
    plan = os.environ.get("FAKE_HERMES_PLAN", "")
    actions = plan.split(":") if plan else (["timeout", "do_nothing"]
                                            if step == 0 else ["do_nothing"])
    action = actions[step] if step < len(actions) else "do_nothing"

    # A killed attempt reports nothing: the CLI prints the id once the
    # conversation ends, and neither the SIGTERM nor the SIGKILL that
    # follows it runs that path.
    if action == "timeout":
        sys.exit(124)
    if action == "exit1":
        sys.exit(1)

    # The session id this attempt reports.
    ids = os.environ.get("FAKE_HERMES_SESSION_IDS", "")
    entries = ids.split(":") if ids else []
    sid = entries[step] if step < len(entries) else "-"
    if sid and sid != "-":
        # Exactly what cli.py writes: a blank line, then the id.
        sys.stderr.write(f"\\nsession_id: {sid}\\n")
        # A second id on the same stderr, the way a resumed session
        # reports the session it opened after the one it was handed.
        # The run must take the LAST: that is the session it is in.
        extra = os.environ.get("FAKE_HERMES_SECOND_SESSION_ID", "")
        if extra:
            sys.stderr.write(f"\\nsession_id: {extra}\\n")
        sys.stderr.flush()
    sys.exit(0)
    """)


def setup_env(tmp: Path, extra: dict | None = None) -> dict:
    """The environment one hermes-implement run sees."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    fake_hermes = tmp / "fake_hermes"
    fake_hermes.write_text(FAKE_HERMES)
    fake_hermes.chmod(0o755)
    state = tmp / "state"
    state.mkdir(exist_ok=True)
    (tmp / "hermes_home").mkdir(exist_ok=True)
    (tmp / "tmpdir").mkdir(exist_ok=True)
    # Strip identity-pinning env vars so the suite is robust to a
    # parent process that has a lane identity pinned.
    parent = {k: v for k, v in os.environ.items()
              if not (k.startswith("GIT_AUTHOR_")
                      or k.startswith("GIT_COMMITTER_")
                      or k.startswith("LANE_GIT_"))}
    base = {**parent,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HERMES_PYTHON": str(fake_hermes),
            "HERMES_IMPLEMENT_TIMEOUT": "30",
            "HERMES_IMPLEMENT_ATTEMPTS": "3",
            "AGENT_STATE_DIR": str(state),
            "GPU_GROUP_DEFAULT": "test-gpu-group",
            "TMPDIR": str(tmp / "tmpdir"),
            "FAKE_HERMES_LOG": str(tmp / "fake_hermes.log"),
            "FAKE_HERMES_STATE": str(tmp / "fake_hermes.state"),
            "TOOLS_ENV": str(tmp / "fake.env"),
            # The login the implementer role may act as. `gh_as` resolves
            # the login the call would act as and refuses unless it is
            # one of these.
            "IMPLEMENTER_LOGINS": "test-implementer",
            "HERMES_HOME": str(tmp / "hermes_home")}
    base.update(extra or {})
    # The lane refuses to start when a dispatched skill differs from the
    # repository's copy. The install is the operator's own, so the suite
    # makes one for its temporary home rather than turning the check off.
    install_skills(tmp / "hermes_home")
    return base


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    run("init", "--initial-branch=main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-m", "base")
    return repo


def run_implement(repo: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BIN), "--worktree", str(repo), "--issue", "7"],
        capture_output=True, text=True, check=False, env=env, cwd=str(repo),
    )


def log_lines(tmp: Path) -> list[str]:
    log = tmp / "fake_hermes.log"
    if not log.is_file():
        return []
    return log.read_text().splitlines()


def continue_values(tmp: Path) -> list[str]:
    """The `--continue` value each call was given, in order."""
    out = []
    for line in log_lines(tmp):
        m = re.search(r"continue_value=(.*)$", line)
        if m:
            value = m.group(1).strip()
            if value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            out.append(value)
    return out


def session_name(tmp: Path) -> str:
    """The session name the run recorded in its run directory."""
    runs = list((tmp / "state" / "hermes-implement-runs").glob("*"))
    assert len(runs) == 1, f"expected one run directory, got {runs}"
    return (runs[0] / "session-id").read_text().strip()


def test_every_attempt_names_the_session():
    """Every attempt, the first one included, must name the session.

    The name is fixed before the first attempt, so a retry that finds
    no session id to read back still resumes this run's session."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, {"FAKE_HERMES_PLAN": "timeout:do_nothing"})
        repo = make_repo(tmp)
        run_implement(repo, env)

        values = continue_values(tmp)
        assert len(values) >= 2, f"expected two attempts, got {values}"
        name = session_name(tmp)
        assert name, "the run recorded no session name"
        for i, value in enumerate(values):
            assert value == name, (
                f"attempt {i + 1} must name the session {name!r}, got "
                f"{value!r}")


def test_the_name_is_created_if_missing():
    """The named session must be created, not refused.

    The CLI exits 1 on a `--continue <name>` that resolves to nothing,
    unless `--create-if-missing` is there. The first attempt is the one
    that creates it."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, {"FAKE_HERMES_PLAN": "timeout:do_nothing"})
        repo = make_repo(tmp)
        run_implement(repo, env)

        for line in log_lines(tmp):
            if "has_continue=True" not in line:
                continue
            assert "create_if_missing=True" in line, (
                f"the run did not ask for the session to be created: "
                f"{line!r}")


def test_a_timeout_leaves_no_session_id_and_the_retry_still_resumes():
    """The case the id-scraping version could not survive.

    The CLI prints the session id once the conversation ends, so an
    attempt the timeout killed leaves no id on its stderr. The retry
    must still resume this run's session, by name."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, {
            "FAKE_HERMES_SESSION_IDS": "session-alpha:session-alpha",
            "FAKE_HERMES_PLAN": "timeout:do_nothing",
        })
        repo = make_repo(tmp)
        run_implement(repo, env)

        # The killed attempt really did report nothing.
        runs = list((tmp / "state" / "hermes-implement-runs").glob("*"))
        stderr = (runs[0] / "attempt1.stderr").read_text()
        assert "session_id:" not in stderr, (
            f"the fake agent reported an id on a timeout: {stderr[:120]!r}")

        values = continue_values(tmp)
        assert len(values) >= 2, f"expected two attempts, got {values}"
        name = session_name(tmp)
        assert values[1] == name, (
            f"the retry after a timeout must resume the named session "
            f"{name!r}, got {values[1]!r}")


def test_no_attempt_ever_passes_a_bare_continue():
    """A bare `--continue` names no session at all.

    The CLI resolves it to the terminal's breadcrumb, or the most
    recent session in the workspace, or the most recent session
    anywhere -- so two runs in one worktree resume each other's
    session."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, {"FAKE_HERMES_PLAN": "timeout:do_nothing"})
        repo = make_repo(tmp)
        run_implement(repo, env)

        for line in log_lines(tmp):
            if "has_continue=True" not in line:
                continue
            assert "continue_bare=False" in line, (
                f"the run passed a bare --continue: {line!r}")


def test_no_attempt_passes_an_empty_continue_value():
    """An empty value is not the bare form.

    It names an empty session, which the CLI resolves as a title and
    refuses."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, {"FAKE_HERMES_PLAN": "timeout:do_nothing"})
        repo = make_repo(tmp)
        run_implement(repo, env)

        for line in log_lines(tmp):
            if "has_continue=True" not in line:
                continue
            m = re.search(r"continue_value=(.*)$", line)
            assert m, f"the log line has no continue value: {line!r}"
            value = m.group(1).strip()
            if value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            assert value != "", (
                f"the run passed --continue with an empty value, which "
                f"names an empty session: {line!r}")


def test_the_session_name_is_recorded_in_the_run_directory():
    """The session name must be recorded, so a post-mortem can name it.

    The run directory is under $AGENT_STATE_DIR; the name file sits
    beside the attempt's transcripts."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, {"FAKE_HERMES_PLAN": "timeout:do_nothing"})
        repo = make_repo(tmp)
        run_implement(repo, env)

        runs = list((tmp / "state" / "hermes-implement-runs").glob("*"))
        assert len(runs) == 1, f"expected one run directory, got {runs}"
        sid_file = runs[0] / "session-id"
        assert sid_file.is_file(), (
            f"the run directory has no session-id file: "
            f"{sorted(p.name for p in runs[0].iterdir())}")
        name = sid_file.read_text().strip()
        assert name.startswith("hermes-implement-"), (
            f"the recorded session name is wrong: {name!r}")


def test_two_runs_get_different_session_names():
    """Two runs must not share a session.

    The name carries the run directory, so two runs resume two
    different sessions."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, {"FAKE_HERMES_PLAN": "timeout:do_nothing"})
        repo = make_repo(tmp)
        run_implement(repo, env)
        first = session_name(tmp)

        # A second run, in a second worktree and with a second run
        # directory, as a second lane would be.
        other = tmp / "other"
        other.mkdir()
        env2 = setup_env(other, {"FAKE_HERMES_PLAN": "timeout:do_nothing"})
        repo2 = make_repo(other)
        run_implement(repo2, env2)
        runs = list((other / "state" / "hermes-implement-runs").glob("*"))
        assert len(runs) == 1, f"expected one run directory, got {runs}"
        second = (runs[0] / "session-id").read_text().strip()

        assert first != second, (
            f"two runs named the same session: {first!r}")


def main() -> int:
    failures: list[tuple[str, str]] = []
    for name, fn in [
        ("every attempt names the session",
         test_every_attempt_names_the_session),
        ("the named session is created if it is missing",
         test_the_name_is_created_if_missing),
        ("a timed-out attempt leaves no session id, and the retry still "
         "resumes the named session",
         test_a_timeout_leaves_no_session_id_and_the_retry_still_resumes),
        ("no attempt passes a bare --continue",
         test_no_attempt_ever_passes_a_bare_continue),
        ("no attempt passes an empty --continue value",
         test_no_attempt_passes_an_empty_continue_value),
        ("the session name is recorded in the run directory",
         test_the_session_name_is_recorded_in_the_run_directory),
        ("two runs get different session names",
         test_two_runs_get_different_session_names),
    ]:
        try:
            fn()
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures.append((name, repr(e)))
            print(f"FAIL {name}: {e!r}")
        else:
            print(f"ok   {name}")
    if failures:
        return 1
    print("PASS a retried attempt resumes this run's session by name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
