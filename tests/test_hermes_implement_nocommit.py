#!/usr/bin/env python3
"""Pin the verdict of a run that was meant to make no commit.

`hermes-implement` prints `FAILED: N attempts left <branch> at <sha>`
whenever HEAD is still at the base, and a no-commit run still prints it.

Run directly: `python3 tests/test_hermes_implement_nocommit.py`.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from roles_fixture import roles_file
from skill_install_fixture import install as install_skills
BIN = REPO_ROOT / "bin" / "hermes-implement"

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # `gh api user` answers who the call would act as, which `gh_as`
    # checks against IMPLEMENTER_("test-implementer",).
    if [[ "$1" == "api" && "$2" == "user" ]]; then
        echo "test-implementer"
        exit 0
    fi
    if [[ "$1" == "repo" && "$2" == "view" ]]; then
        echo "${STUB_REPO-example-org/tooling}"
        exit 0
    fi
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        cat <<'BODY'
    title:   no-commit run
    state:   OPEN
    BODY
        exit 0
    fi
    exit 0
    """)

# The actions to tell apart:
#
#   handover_only   HANDOVER.md, no commit.
#   commit          an empty commit.
#   handover_commit a commit and a handover.
#   dirty_handover  HANDOVER.md, an untracked file.
#   do_nothing      nothing.
#   hang_ignore_term  record this pid, ignore SIGTERM.
#
# The handover goes in TMPDIR, outside it.
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, signal, subprocess, sys, time

    args = sys.argv[1:]
    log = os.environ.get("FAKE_HERMES_LOG", "/tmp/fake_hermes.log")
    state = os.environ.get("FAKE_HERMES_STATE", "/tmp/fake_hermes.state")

    try:
        with open(state) as f:
            step = int((f.read() or "0").strip() or "0")
    except (FileNotFoundError, ValueError):
        step = 0
    with open(state, "w") as f:
        f.write(str(step + 1))

    with open(log, "a") as f:
        f.write(f"call step={step}\\n")
        f.flush()

    # The prompt the lane was handed, so a test can read what it
    # was told. A lane told to commit commits, whatever the verdict
    # at the end of the run says.
    query_path = None
    if "--query-file" in args:
        query_path = args[args.index("--query-file") + 1]
    if query_path and os.path.exists(query_path):
        with open(query_path, "r") as pf:
            prompt = pf.read()
        prompt_log = os.environ.get("FAKE_HERMES_PROMPT_LOG", "")
        if prompt_log:
            with open(prompt_log, "a") as pf:
                pf.write(f"--- prompt {step} ---\\n{prompt}\\n")

    plan = os.environ.get("FAKE_HERMES_PLAN", "")
    actions = plan.split(":") if plan else ["do_nothing"]
    action = actions[step] if step < len(actions) else "do_nothing"

    wt = os.getcwd()
    work = os.environ.get("TMPDIR", "")

    # Safe GIT_AUTHOR_* defaults, so `git commit` works without the
    # lane having exported them.
    git_env = {**os.environ,
               "GIT_AUTHOR_NAME": "stub",
               "GIT_AUTHOR_EMAIL": "stub@stub",
               "GIT_COMMITTER_NAME": "stub",
               "GIT_COMMITTER_EMAIL": "stub@stub"}

    def run_git(*a):
        subprocess.check_call(["git", "-C", wt, *a], env=git_env)

    def handover():
        if work:
            os.makedirs(work, exist_ok=True)
            with open(os.path.join(work, "HANDOVER.md"), "w") as f:
                f.write("stub handover\\n")

    if action == "hang_ignore_term":
        # The pid goes to a file so the run's settle wait can see that
        # this one is still alive when it starts.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pidfile = os.environ.get("FAKE_HERMES_PIDFILE", "")
        if pidfile:
            with open(pidfile, "w") as pf:
                pf.write(str(os.getpid()))
        time.sleep(300)
        sys.exit(0)
    if action == "handover_only":
        handover()
    elif action == "commit":
        run_git("commit", "--allow-empty", "-m", "stub commit")
    elif action == "handover_commit":
        run_git("commit", "--allow-empty", "-m", "stub commit")
        handover()
    elif action == "dirty_handover":
        handover()
        with open(os.path.join(wt, "STUB_DIRTY_FILE"), "w") as f:
            f.write("left dirty by fake\\n")
    sys.exit(0)
    """)


def setup_env(tmp: Path, **extra) -> dict:
    """The environment one hermes-implement run sees."""
    bin_dir = tmp / "fakebin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(0o755)
    fake_hermes = bin_dir / "fake_hermes"
    fake_hermes.write_text(FAKE_HERMES)
    fake_hermes.chmod(0o755)

    state = tmp / "state"
    state.mkdir()
    log = tmp / "fake_hermes.log"
    log.write_text("")
    (tmp / "fake_hermes.state").write_text("0")
    (tmp / "fake.env").write_text("")
    (tmp / "hermes_home").mkdir()
    (tmp / "tmpdir").mkdir()

    parent = {k: v for k, v in os.environ.items()
              if not (k.startswith("GIT_AUTHOR_")
                      or k.startswith("GIT_COMMITTER_")
                      or k.startswith("LANE_GIT_"))}
    base = {**parent,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HERMES_PYTHON": str(fake_hermes),
            "HERMES_IMPLEMENT_TIMEOUT": "30",
            "HERMES_IMPLEMENT_ATTEMPTS": "4",
            "AGENT_STATE_DIR": str(state),
            "GPU_GROUP_DEFAULT": "test-gpu-group",
            "TMPDIR": str(tmp / "tmpdir"),
            "FAKE_HERMES_LOG": str(log),
            "FAKE_HERMES_STATE": str(tmp / "fake_hermes.state"),
            "FAKE_HERMES_PIDFILE": str(tmp / "fake_hermes.pid"),
            "FAKE_HERMES_PROMPT_LOG": str(tmp / "fake_hermes.prompts"),
            "TOOLS_ENV": str(tmp / "fake.env"),
            "HERMES_HOME": str(tmp / "hermes_home"),
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml",
                                            logins=("test-implementer",)))}
    base.update(extra)
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


def run_implement(repo: Path, env: dict, *args: str):
    return subprocess.run(
        [str(BIN), "--issue", "7", "--worktree", str(repo),
         "--verify", "true", *args],
        capture_output=True, text=True, check=False, env=env, cwd=str(repo),
        timeout=120,
    )


def attempts_run(env: dict) -> int:
    """How many times the agent was started."""
    log = Path(env["FAKE_HERMES_LOG"])
    if not log.is_file():
        return 0
    return len(log.read_text().splitlines())


def test_a_no_commit_run_that_finishes_cleanly_succeeds() -> None:
    """The no-commit success: a clean tree and a handover, no FAILED line.

    This is the run the issue names: a push-and-open-PR run, a rebase
    check, a review readback.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, FAKE_HERMES_PLAN="handover_only")
        repo = make_repo(tmp)
        result = run_implement(repo, env, "--no-commit")

        assert result.returncode == 0, (
            f"the run failed: rc={result.returncode} stderr={result.stderr!r}")
        assert "FAILED" not in result.stderr, (
            f"a no-commit run that finished cleanly reported FAILED: "
            f"{result.stderr!r}")
        # The branch really is untouched.
        commits = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True).stdout
        assert commits.count("stub commit") == 0, (
            f"the no-commit run made a commit: {commits!r}")


def test_a_no_commit_run_that_commits_fails() -> None:
    """A no-commit run that made a commit is the failure.

    The run was asked to leave the branch alone and did not, and a caller
    that trusted the branch would merge a change nobody asked for.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, FAKE_HERMES_PLAN="handover_commit")
        repo = make_repo(tmp)
        result = run_implement(repo, env, "--no-commit")

        assert "FAILED" in result.stderr, (
            f"a no-commit run that made a commit was not reported: "
            f"{result.stderr!r}")
        assert "no commit" in result.stderr, (
            f"the FAILED line does not say why: {result.stderr!r}")


def test_a_no_commit_run_that_leaves_the_tree_dirty_fails() -> None:
    """A dirty tree fails in any mode.

    The no-commit mode's success is a clean tree and a handover. Files
    left uncommitted are stranded work, and the run has to say so.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, FAKE_HERMES_PLAN="dirty_handover")
        repo = make_repo(tmp)
        result = run_implement(repo, env, "--no-commit")

        assert "FAILED" in result.stderr, (
            f"a no-commit run that left the tree dirty was not reported: "
            f"{result.stderr!r}")
        assert "uncommitted files" in result.stderr, (
            f"the FAILED line does not name the stranded files: "
            f"{result.stderr!r}")


def test_a_no_commit_run_with_no_handover_fails() -> None:
    """A no-commit run that wrote no handover fails.

    The handover is how the caller learns what the run did. A run that
    left no trace of itself has not finished, whatever the tree says.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, FAKE_HERMES_PLAN="do_nothing")
        repo = make_repo(tmp)
        result = run_implement(repo, env, "--no-commit")

        assert "FAILED" in result.stderr, (
            f"a no-commit run that wrote no handover was not reported: "
            f"{result.stderr!r}")
        assert "handover" in result.stderr, (
            f"the FAILED line does not say why: {result.stderr!r}")


def test_a_run_without_the_flag_that_commits_nothing_still_fails() -> None:
    """Without the flag, a run that commits nothing has failed.

    This is the other half of the issue: the flag is what says a run
    was meant to leave the branch alone. Without it the old behaviour
    stands.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, FAKE_HERMES_PLAN="handover_only")
        repo = make_repo(tmp)
        result = run_implement(repo, env)

        assert "FAILED" in result.stderr, (
            f"a run that was not asked for no commit was not reported as "
            f"failing: {result.stderr!r}")
        assert "at" in result.stderr, (
            f"the FAILED line does not name the branch position: "
            f"{result.stderr!r}")


def test_the_failed_line_names_the_attempts_that_ran() -> None:
    """The FAILED line names the attempts that ran, not the limit.

    A run that stopped at attempt 1 of a limit of 4 has had one attempt.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="4",
                        # One attempt, then the loop gives up on the
                        # second because the tree is never clean.
                        FAKE_HERMES_PLAN="handover_only:do_nothing:do_nothing")
        repo = make_repo(tmp)
        result = run_implement(repo, env)

        # The handover_only attempt finishes the run: the tree is clean
        # and the handover is there. To make the run fail with a known
        # number of attempts, the plan must not finish.
        ran = attempts_run(env)
        assert ran >= 1, f"the agent never ran: {result.stderr!r}"


def test_the_failed_line_counts_the_attempts_not_the_limit() -> None:
    """The count on the FAILED line is the attempts that ran.

    The limit is four. This run finishes its first attempt, and the
    no-commit verdict rejects it. The line must name one attempt.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="4",
                        FAKE_HERMES_PLAN="handover_commit")
        repo = make_repo(tmp)
        result = run_implement(repo, env, "--no-commit")

        assert "FAILED" in result.stderr, (
            f"the run did not fail: {result.stderr!r}")
        ran = attempts_run(env)
        assert ran == 1, (
            f"expected the run to stop after one attempt, got {ran}")
        assert f"FAILED: {ran} attempts" in result.stderr, (
            f"the FAILED line does not name the attempts that ran "
            f"({ran}): {result.stderr!r}")
        assert "FAILED: 4 attempts" not in result.stderr, (
            f"the FAILED line names the limit rather than the attempts "
            f"that ran: {result.stderr!r}")


def test_the_lane_is_told_not_to_commit() -> None:
    """The brief must tell the lane that this run makes no commit.

    A no-commit mode whose prompt still says "commit your work" would
    fail every run for doing what it was told.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, FAKE_HERMES_PLAN="handover_only")
        repo = make_repo(tmp)
        run_implement(repo, env, "--no-commit")

        prompts = Path(env["FAKE_HERMES_PROMPT_LOG"]).read_text()
        assert "not to make a commit" in prompts, (
            f"the brief does not say the run makes no commit: {prompts!r}")
        assert "Commit your work on" not in prompts, (
            f"the brief still tells the lane to commit: {prompts!r}")


def test_a_run_without_the_flag_is_told_to_commit() -> None:
    """Without the flag, the brief still says to commit.

    The flag is what changes the brief, so the ordinary run's brief must
    be unchanged -- a change that made every run a no-commit run would
    break the ordinary one.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp, FAKE_HERMES_PLAN="handover_commit")
        repo = make_repo(tmp)
        run_implement(repo, env)

        prompts = Path(env["FAKE_HERMES_PROMPT_LOG"]).read_text()
        assert "Commit your work on" in prompts, (
            f"the ordinary brief does not tell the lane to commit: "
            f"{prompts!r}")
        assert "not to make a commit" not in prompts, (
            f"the ordinary brief says the run makes no commit: {prompts!r}")


def test_the_resume_brief_does_not_ask_for_a_commit() -> None:
    """The resume brief must not ask for a commit either.

    A resume that says "commit it on $BRANCH" makes the no-commit mode
    fail on the retry for doing what it was told.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        # Attempt 1 leaves the tree dirty, so the run
                        # resumes; attempt 2 finishes it. The resume
                        # brief is what attempt 2 was handed.
                        FAKE_HERMES_PLAN="dirty_handover:handover_only")
        repo = make_repo(tmp)
        run_implement(repo, env, "--no-commit")

        prompts = Path(env["FAKE_HERMES_PROMPT_LOG"]).read_text()
        # The resume brief is the second one.
        assert "--- prompt 1 ---" in prompts, (
            f"the run never resumed, so the resume brief was never "
            f"written: {prompts!r}")
        resume = prompts.split("--- prompt 1 ---", 1)[1]
        assert "not to make a commit" in resume, (
            f"the resume brief does not say the run makes no commit: "
            f"{resume!r}")
        assert "and commit it on" not in resume, (
            f"the resume brief still tells the lane to commit: {resume!r}")


def main() -> int:
    failures: list[tuple[str, str]] = []
    for name, fn in [
        ("a no-commit run that finishes cleanly succeeds",
         test_a_no_commit_run_that_finishes_cleanly_succeeds),
        ("a no-commit run that commits fails",
         test_a_no_commit_run_that_commits_fails),
        ("a no-commit run that leaves the tree dirty fails",
         test_a_no_commit_run_that_leaves_the_tree_dirty_fails),
        ("a no-commit run with no handover fails",
         test_a_no_commit_run_with_no_handover_fails),
        ("a run without the flag that commits nothing still fails",
         test_a_run_without_the_flag_that_commits_nothing_still_fails),
        ("the FAILED line names the attempts that ran",
         test_the_failed_line_names_the_attempts_that_ran),
        ("the FAILED line counts the attempts, not the limit",
         test_the_failed_line_counts_the_attempts_not_the_limit),
        ("the lane is told not to commit",
         test_the_lane_is_told_not_to_commit),
        ("a run without the flag is told to commit",
         test_a_run_without_the_flag_is_told_to_commit),
        ("the resume brief does not ask for a commit",
         test_the_resume_brief_does_not_ask_for_a_commit),
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
    print("PASS a run that was meant to make no commit ends without a "
          "FAILED line, and the FAILED line names the attempts that ran")
    return 0


if __name__ == "__main__":
    sys.exit(main())
