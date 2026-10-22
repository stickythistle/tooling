#!/usr/bin/env python3
"""Pin hermes-implement's GPU lock across the retry loop.

The contract being tested: when a GPU group is configured, hermes-implement
takes the lock once before attempt 1, holds it across every attempt (the
spawned hermes children inherit fd 8 so the lock survives even when one of
them exits non-zero), and releases it only after the last attempt completes.
A second hermes-implement call must not be able to acquire the same lock
until the first finishes.

Run directly: `python3 tests/test_hermes_implement.py`.
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "hermes-implement"
LIB = REPO_ROOT / "lib" / "common.sh"

# A fake `gh` that ignores every command except `issue view`. The script's
# one network call is `gh issue view`, which prints an issue body to stdout.
# We use it to make hermes-implement see the issue text the same way it
# would on a real machine.
FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Whatever the operator asks gh to do, return a no-op success unless
    # it is the one command hermes-implement issues: `gh issue view`.
    # `gh issue view` is what populates $WORK/issue.txt; the body never
    # matters to the lock test.
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        cat <<'BODY'
title:   GPU lock regression
state:   OPEN
BODY
        exit 0
    fi
    exit 0
""")

# Each call logs PID, timestamp, whether fd 8 was inherited (proves
# hermes-implement passed the GPU lock fd to its children), and the
# prompt it received via --query-file. The prompt's sha256 is part of
# the test's assertion -- a hermes that was passed `-z "PROMPT"` would
# get the prompt as argv, but the helper hermes-implement uses reads
# it from `--query-file PATH`, so a stub without `--query-file` on
# argv never sees the prompt at all.
#
# Optional FAKE_HERMES_PLAN: a colon-separated list of actions, one
# per attempt. With the variable unset or empty, attempt 1 times
# out and attempt 2 exits 0. Each action is one of:
#   do_nothing     exit 0, leave worktree untouched
#   timeout        exit 124, leave worktree untouched
#   commit         empty commit, leave worktree clean
#   commit_dirty   empty commit, then write STUB_DIRTY_FILE
#   finish         commit, write HANDOVER.md in the run directory
#   finish_clean   commit_dirty + finish + remove the sentinel
#   hang_ignore_term  record this pid, ignore SIGTERM, sleep 300: an
#                    agent that survives `timeout`'s signal and keeps its
#                    session lease (issue #157)
#   refuse         print the SESSION_NOT_OWNED refusal on stderr and
#                    exit 1, the way hermes refuses a --continue whose
#                    session is still owned
# Attempts past the plan's end behave like do_nothing.
#
# Every call also logs `prev_alive=<bool>`: whether the pid in
# $FAKE_HERMES_PIDFILE, written by an earlier hang_ignore_term, was
# still alive at the moment this call started. It is how the tests see
# the lease race from the outside.
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env python3
    import hashlib, os, signal, subprocess, sys, time
    args = sys.argv[1:]
    log = os.environ.get("FAKE_HERMES_LOG", "/tmp/fake_hermes.log")

    # fd 8 is the GPU group lock. If hermes-implement did not inherit
    # fd 8 to its child, /proc/self/fd/8 does not exist and the lock is
    # gone for the duration of this hermes. If it does exist, this hermes
    # holds the lock until it exits, and the parent's exec 8>&- after
    # the loop is a no-op for this hold -- the file description stays
    # open through the child's lifetime.
    fd8_held = os.path.exists("/proc/self/fd/8")

    # --query-file is the path of the prompt; the helper writes the
    # prompt there and the stub reads it back. If hermes-implement had
    # gone through `-z "$prompt"` instead, this index would never match
    # and the stub would not know what to record.
    query_path = None
    has_query_file = "--query-file" in args
    if has_query_file:
        query_path = args[args.index("--query-file") + 1]
    query_sha = ""
    query_bytes = 0
    if query_path and os.path.exists(query_path):
        with open(query_path, "rb") as qf:
            data = qf.read()
        query_bytes = len(data)
        query_sha = hashlib.sha256(data).hexdigest()

    is_continue = "--continue" in args
    pid = os.getpid()
    ts = time.monotonic()

    # Is the agent a previous call left still running? Its pid is in the
    # pidfile hang_ignore_term wrote.
    prev_alive = False
    pidfile = os.environ.get("FAKE_HERMES_PIDFILE", "")
    if pidfile:
        try:
            with open(pidfile) as pf:
                prev = int((pf.read() or "").strip())
            os.kill(prev, 0)          # signal 0: existence only
            prev_alive = True
        except (FileNotFoundError, ValueError, ProcessLookupError,
                PermissionError):
            prev_alive = False

    # Each invocation consumes one entry from FAKE_HERMES_PLAN; the
    # counter is recorded before the action runs so the log line also
    # tells the test which step the run reached. With the plan unset
    # (the legacy case the original GPU-lock test relies on) attempt
    # 1 times out and attempt 2 exits 0.
    plan_raw = os.environ.get("FAKE_HERMES_PLAN", "")
    state_dir = os.environ.get("FAKE_HERMES_STATE", "/tmp/fake_hermes.state")
    step = 0
    try:
        with open(state_dir) as f:
            step = int((f.read() or "0").strip() or "0")
    except (FileNotFoundError, ValueError):
        step = 0
    with open(state_dir, "w") as f:
        f.write(str(step + 1))
    if plan_raw:
        actions = plan_raw.split(":")
    elif step == 0:
        # Legacy: attempt 1 times out, attempt 2 does nothing. The
        # original GPU-lock test pins this shape.
        actions = ["timeout", "do_nothing"]
    else:
        actions = ["do_nothing"]
    if step < len(actions):
        action = actions[step]
    else:
        action = "do_nothing"

    with open(log, "a") as f:
        f.write(f"call pid={pid} continue={is_continue} fd8={fd8_held} "
                f"has_query_file={has_query_file} "
                f"query_bytes={query_bytes} "
                f"query_sha={query_sha} prev_alive={prev_alive} "
                f"action={action} t={ts:.6f}\\n")
        f.flush()

    # Run the action against the worktree the script handed the lane.
    # The fake sets safe GIT_AUTHOR_* defaults so `git commit` works
    # without the lane having exported them.
    wt = os.getcwd()
    work = os.environ.get("TMPDIR", "")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "stub",
           "GIT_AUTHOR_EMAIL": "stub@stub",
           "GIT_COMMITTER_NAME": "stub",
           "GIT_COMMITTER_EMAIL": "stub@stub"}
    def run_git(*a):
        subprocess.check_call(["git", "-C", wt, *a], env=env)
    if action == "timeout":
        sys.exit(124)
    if action == "refuse":
        # Exactly what hermes writes when a --continue arrives while the
        # session still has a live owner. The script reads both lines, so
        # their shape is part of the contract under test.
        sys.stderr.write("hermes-refusal-reason: SESSION_NOT_OWNED\\n")
        sys.stderr.write(
            "Session s1 already has a live owner (cli, pid 4242, "
            "lease age 1m). The session is held by a running hermes.\\n")
        sys.exit(1)
    if action == "hang_ignore_term":
        # The pid goes to a file so the next call can report whether this
        # one was still alive when it started (prev_alive) -- the situation
        # issue #157 reports.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        hang_pidfile = os.environ.get("FAKE_HERMES_PIDFILE", "")
        if hang_pidfile:
            with open(hang_pidfile, "w") as pf:
                pf.write(str(os.getpid()))
        time.sleep(300)
        sys.exit(0)
    if action in ("commit", "commit_dirty", "finish", "finish_clean"):
        run_git("commit", "--allow-empty", "-m", "stub commit")
    if action == "commit_with_c_flag":
        # Shape 1: an agent that overrides identity on the command line
        # with `git -c user.name=...`. Env vars in the agent's environment
        # (hermes-implement exports them) win over -c, so this commit
        # must come out authored by the expected identity, NOT by Wrong.
        # A bare subprocess call: run_git would stomp on the env vars
        # the lane set, hiding the shape. `-c` must come before the
        # subcommand -- `git commit -c user.name=X -m msg` is a git
        # error rather than the override an agent would actually write.
        subprocess.check_call(
            ["git", "-C", wt,
              "-c", "user.name=Wrong Identity",
              "-c", "user.email=wrong@example.invalid",
              "commit", "--allow-empty",
              "-m", "shape-c-flag"])
    if action == "commit_with_author":
        # Shape 2: an agent that overrides the author with --author.
        # The committer still picks up the env var, but the author does
        # not -- the verifier must catch this and re-author.
        subprocess.check_call(
            ["git", "-C", wt,
              "commit", "--allow-empty",
              "--author=Wrong Author <wrong@example.invalid>",
              "-m", "shape-author-flag"])
    if action == "commit_with_env_override":
        # Shape 3: an agent that sets GIT_AUTHOR_NAME/EMAIL on its own
        # git invocation. This bypasses step 1 entirely and is what
        # the verifier was written for.
        shape_env = {**os.environ,
                     "GIT_AUTHOR_NAME": "Wrong Identity",
                     "GIT_AUTHOR_EMAIL": "wrong@example.invalid"}
        subprocess.check_call(
            ["git", "-C", wt,
               "commit", "--allow-empty", "-m", "shape-env-override"],
            env=shape_env)
    if action == "commit_with_config_override":
        # Shape 4: an agent that sets user.name/email in the worktree
        # config. With env vars set the commit is still right, but
        # verifying this shape exercises the same backstop as shape 1.
        subprocess.check_call(["git", "-C", wt,
                               "config", "user.name", "Wrong Identity"])
        subprocess.check_call(["git", "-C", wt,
                               "config", "user.email", "wrong@example.invalid"])
        subprocess.check_call(
            ["git", "-C", wt, "commit", "--allow-empty", "-m", "shape-config-override"])
    if action in ("commit_dirty", "finish_clean"):
        # Untracked so it shows up in `git status --short` but the
        # lane is not committing it on this attempt.
        with open(os.path.join(wt, "STUB_DIRTY_FILE"), "w") as f:
            f.write("left dirty by fake\\n")
    if action in ("finish", "finish_clean"):
        if work:
            os.makedirs(work, exist_ok=True)
            with open(os.path.join(work, "HANDOVER.md"), "w") as f:
                f.write("stub handover\\n")
    if action in ("finish_clean",):
        # finish_clean clears the sentinel commit_dirty left behind so
        # the next iteration's `finished` check sees an empty tree.
        sentinel = os.path.join(wt, "STUB_DIRTY_FILE")
        if os.path.exists(sentinel):
            os.remove(sentinel)
    sys.exit(0)
""")


def setup_env(tmp: Path, **extra) -> dict:
    """Build the env hermes-implement will run under: a fake `gh` on
    PATH, a fake hermes at HERMES_PYTHON, a scratch state directory,
    and a GPU_GROUP_DEFAULT so the lock is configured. `**extra`
    overrides the defaults (HERMES_IMPLEMENT_ATTEMPTS, FAKE_HERMES_PLAN)."""
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

    # Starts absent, so the first call of any run reads prev_alive=False.
    pidfile = tmp / "fake_hermes.pid"

    (tmp / "fake.env").write_text("")
    (tmp / "hermes_home").mkdir()
    (tmp / "tmpdir").mkdir()
    # Strip identity-pinning env vars so the suite is robust to a
    # parent process that has a lane identity pinned: a gate runs
    # inside a lane, and GIT_AUTHOR_* / GIT_COMMITTER_* / LANE_GIT_*
    # reaching the test from there would leak the parent identity
    # into the work the tests are checking. Tests that want to pin an
    # identity set those keys explicitly via **extra.
    parent = {k: v for k, v in os.environ.items()
              if not (k.startswith("GIT_AUTHOR_")
                      or k.startswith("GIT_COMMITTER_")
                      or k.startswith("LANE_GIT_"))}
    base = {**parent,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            # The fake HERMES_PYTHON script is its own python interpreter
            # (shebang), so the script's -m hermes_cli.main invocation is
            # answered by the fake rather than the real hermes package.
            "HERMES_PYTHON": str(fake_hermes),
            # Tight per-attempt timeout. The fake exits 124 on attempt 1
            # before this fires, but it's here in case a future change
            # makes the fake wait instead.
            "HERMES_IMPLEMENT_TIMEOUT": "30",
            # Two attempts only -- the test cares about attempt 1 timing
            # out and attempt 2 succeeding; further attempts are noise.
            "HERMES_IMPLEMENT_ATTEMPTS": "2",
            "AGENT_STATE_DIR": str(state),
            "GPU_GROUP_DEFAULT": "test-gpu-group",
            "TMPDIR": str(tmp / "tmpdir"),
            "FAKE_HERMES_LOG": str(log),
            "FAKE_HERMES_PIDFILE": str(pidfile),
            "FAKE_HERMES_STATE": str(tmp / "fake_hermes.state"),
            # hermes-implement sources its env file; let an empty one
            # stand in for the operator's so HOST_BUILDER etc. do not
            # leak into the test.
            "TOOLS_ENV": str(tmp / "fake.env"),
            # hermes-implement reads the verification evidence DB if it
            # exists at $HERMES_HOME/verification_evidence.db. Point it
            # at a path that does not have one.
            "HERMES_HOME": str(tmp / "hermes_home")}
    base.update(extra)
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


def test_hermes_implement_holds_gpu_lock_across_retry():
    """hermes-implement must take the GPU group lock once, hold it across
    every attempt (including ones that exit non-zero), and release it only
    after the last attempt. The fake hermes exits 124 on attempt 1 and 0
    on attempt 2; both calls must record fd 8 held, and a concurrent
    caller running while the loop is in flight must block on the same
    lock."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp)
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        # The first hermes-implement call. It will run attempt 1 (which
        # exits 124) and attempt 2 (which exits 0). Both must run while
        # fd 8 is held, and the lock must be released at the end.
        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

        # Neither attempt committed, so the run must say so on stderr. The
        # lock release after the loop once redirected the rest of the
        # script's stderr to /dev/null, and this line was the first casualty.
        assert "FAILED:" in proc.stderr, (
            f"no FAILED line after attempts that never committed: "
            f"stderr={proc.stderr!r}")

        # Both attempts must have run.
        calls = log.read_text().splitlines()
        # Each fake call appends one line. We expect 2 calls (attempt 1
        # and attempt 2).
        assert len(calls) == 2, (
            f"expected 2 hermes invocations, got {len(calls)}: {calls!r}")
        # Both calls must have inherited fd 8 (the GPU lock).
        for line in calls:
            m = re.match(r"call pid=(\d+) continue=(\S+) fd8=(\S+) "
                         r"has_query_file=(\S+) query_bytes=(\d+) "
                         r"query_sha=(\S*) prev_alive=(\S+) action=(\S+) "
                         r"t=(\S+)", line)
            assert m, f"unparseable log line: {line!r}"
            assert m.group(3) == "True", (
                f"hermes child did not inherit fd 8 from hermes-implement: "
                f"{line!r}")
            # The agent must have been called with --query-file, and the
            # prompt file must have been readable. A hermes-implement that
            # passed the prompt as `-z "$prompt"` would fail this: the
            # stub never sees `--query-file` and query_bytes stays 0.
            assert m.group(4) == "True", (
                f"hermes-implement should call agent with --query-file; "
                f"this means the prompt went over argv (-z) instead: "
                f"{line!r}")
            assert int(m.group(5)) > 0, (
                f"hermes-implement's --query-file pointed at an empty or "
                f"missing file: {line!r}")
        # First call is attempt 1 (no --continue, legacy action=timeout),
        # second is attempt 2 (with --continue, legacy action=do_nothing).
        assert "continue=False" in calls[0], calls[0]
        assert "action=timeout" in calls[0], calls[0]
        assert "continue=True" in calls[1], calls[1]


def _read_attempt_prompts(run_dir: Path) -> list[str]:
    """Read each attempt's prompt the script wrote under $WORK. Returns
    the bodies in attempt order, so a test that wants to assert on the
    resume prompt knows which file to look at."""
    out = []
    for path in sorted(run_dir.glob("attempt*.prompt"),
                       key=lambda p: int(re.match(r"attempt(\d+)", p.stem).group(1))):
        out.append(path.read_text())
    return out


def test_hermes_implement_resumes_after_one_commit_with_dirty_tree():
    """Central pin: attempt 1 commits and dirties the tree; attempt 2
    must run with --continue and the resume prompt must name the
    commit and the dirty file. The regression: a retry loop that
    ended on the first committed item."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        # attempt 1: commit + leave dirty sentinel.
                        # attempt 2: commit on top, write HANDOVER.md,
                        # remove sentinel. attempt 3 is a no-op the
                        # loop must NOT reach.
                        FAKE_HERMES_PLAN="commit_dirty:finish_clean:do_nothing")
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        # The pin is also the post-loop shape: no FAILED line, since
        # attempt 2 wrote HANDOVER.md and the tree was left clean.
        assert "FAILED:" not in proc.stderr, (
            f"run that finished cleanly reported FAILED: "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        assert len(calls) == 2, (
            f"expected 2 invocations, got {len(calls)}: {calls!r}")
        # Attempt 1 (no --continue) ran commit_dirty; attempt 2
        # (--continue) ran finish_clean.
        assert "continue=False" in calls[0]
        assert "action=commit_dirty" in calls[0], calls[0]
        assert "continue=True" in calls[1]
        assert "action=finish_clean" in calls[1], calls[1]

        # The resume prompt attempt 2 received names the commit and
        # the dirty file. The script writes each attempt's prompt
        # under $WORK and prints the run directory on its last
        # stdout line; the test reads it from there.
        run_dir = Path(proc.stdout.strip().splitlines()[-1])
        prompts = _read_attempt_prompts(run_dir)
        assert len(prompts) == 2, prompts
        resume = prompts[1]
        # The commit attempt 1 left shows up under 'Committed since base'.
        assert "stub commit" in resume, (
            f"resume prompt does not name attempt 1's commit: "
            f"{resume!r}")
        # The dirty sentinel attempt 1 left shows up under
        # 'Uncommitted files' (a `??` line in `git status --short`).
        assert "STUB_DIRTY_FILE" in resume, (
            f"resume prompt does not name the uncommitted file: "
            f"{resume!r}")
        assert "HANDOVER.md: missing" in resume, resume
        # The prompt must describe the work and forbid discarding it:
        # finished() cannot tell a tree cleaned by `git stash` from
        # one cleaned by finishing the work. See the parallel
        # assertion in tests/test_lane_settled.py.
        assert "Never discard, stash, or reset work" in resume, (
            f"resume prompt does not forbid discarding the work to "
            f"get a clean tree: {resume!r}")
        assert "deliverable is a clean tree" not in resume, (
            f"resume prompt still frames a clean tree as the "
            f"deliverable: {resume!r}")

        # Two stub commits (one per attempt), the sentinel is gone,
        # the tree is clean.
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=True).stdout
        assert dirty == "", (
            f"worktree still dirty after finish: {dirty!r}")
        commits = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True).stdout
        assert commits.count("stub commit") == 2, (
            f"expected base + 2 stub commits, got: {commits!r}")


def test_hermes_implement_resumes_after_timeout_with_dirty_tree():
    """A 124 is just another non-zero exit: the resume policy is the
    same whether the attempt died on its own timer or was killed by
    the script's `timeout` wrapper."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        # attempt 1: exit 124. attempt 2: finish.
                        # attempt 3: no-op the loop must NOT reach.
                        FAKE_HERMES_PLAN="timeout:finish:do_nothing")
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")
        assert "FAILED:" not in proc.stderr, (
            f"timed-out attempt that resumed cleanly reported FAILED: "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        # Attempts 1 and 2 ran, attempt 3 was a no-op after attempt 2
        # reached the finish state -- the loop's break on `finished`
        # stops before it.
        assert len(calls) == 2, (
            f"expected exactly 2 invocations under a 124 then finish plan, "
            f"got {len(calls)}: {calls!r}")
        assert "continue=False" in calls[0]
        assert "action=timeout" in calls[0], calls[0]
        assert "continue=True" in calls[1]
        assert "action=finish" in calls[1], calls[1]


def test_hermes_implement_does_not_retry_when_finished():
    """A finished attempt (clean tree + non-empty HANDOVER.md) has
    reached the run's goal: attempt 2 must not run. The
    committed-but-no-handover case was the regression the lane
    reported; the assertion checks clean + present together."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        FAKE_HERMES_PLAN="finish:do_nothing:do_nothing")
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")
        assert "FAILED:" not in proc.stderr
        assert "WARNING" not in proc.stderr

        calls = log.read_text().splitlines()
        assert len(calls) == 1, (
            f"a finished attempt must not trigger a retry, got "
            f"{len(calls)} invocations: {calls!r}")
        assert "continue=False" in calls[0]
        assert "action=finish" in calls[0], calls[0]


def test_hermes_implement_reports_uncommitted_files_when_attempts_exhausted():
    """At the cap with uncommitted work left, the run's final stderr
    line must name the uncommitted files. The lane reported an
    auto-commit stranded unverified work; the test pins the loop
    leaves it stranded."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        # Three attempts, no finish -- the cap is the
                        # only thing that stops the loop. Each
                        # attempt commits then re-dirties the
                        # sentinel, so `finished` stays false.
                        FAKE_HERMES_PLAN=(
                            "commit_dirty:commit_dirty:commit_dirty"))
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        assert len(calls) == 3, (
            f"expected 3 invocations under ATTEMPTS=3, got {len(calls)}: "
            f"{calls!r}")
        # The script must not pile on a final "fix the failing test"
        # commit when the loop gives up. base + 3 commits = 4 lines,
        # every stub commit body starts with 'stub commit'.
        commits = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True).stdout
        assert commits.count("stub commit") == 3, (
            f"expected 3 stub commits (one per attempt), got: "
            f"{commits!r}")
        assert "STUB_DIRTY_FILE" in proc.stderr, (
            f"final stderr does not name the uncommitted file: "
            f"stderr={proc.stderr!r}")
        assert "FAILED: 3 attempts" in proc.stderr, (
            f"final FAILED line missing or wrong: stderr={proc.stderr!r}")
        assert "?? STUB_DIRTY_FILE" in proc.stderr, (
            f"untracked file not flagged on the FAILED line: "
            f"stderr={proc.stderr!r}")




        # After the script finishes, fd 8 must be released -- a fresh
        # process must be able to flock the lock without blocking.
        flock_check = subprocess.run(
            ["bash", "-c",
             f'set -eu; . "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock ""; echo acquired; exec 8>&-'],
            env=env, capture_output=True, text=True, timeout=10)
        assert flock_check.returncode == 0, (
            f"GPU lock not released after hermes-implement exit: "
            f"stderr={flock_check.stderr!r}")
        assert "acquired" in flock_check.stdout, (
            f"post-exit acquire did not print 'acquired': "
            f"stdout={flock_check.stdout!r}")


def test_lane_identity_env_vars_win_over_c_flag_override():
    """Shape 1: an agent that calls `git -c user.name=Wrong
    -c user.email=Wrong commit` must produce a commit authored by the
    expected identity. Env vars in the lane's environment win over
    -c on the command line."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="1",
                        # Force the expected identity to a name the host
                        # config does not use, so the verifier has a
                        # specific value to compare against.
                        LANE_GIT_NAME="Expected",
                        LANE_GIT_EMAIL="expected@example.invalid",
                        FAKE_HERMES_PLAN="commit_with_c_flag")
        repo = make_repo(tmp)
        # make_repo sets user.email/name in the worktree's local config.
        # We want the override (LANE_GIT_*) to win, so wipe them.
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.email"], check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.name"], check=True)

        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        # Read back the commit's identity. The shape-c-flag marker is
        # what the fake wrote; we look for it specifically so a stray
        # base commit cannot satisfy the assertion.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H %an <%ae> | %cn <%ce>",
             "--grep=^shape-c-flag$"],
            capture_output=True, text=True, check=True).stdout
        assert "Expected <expected@example.invalid>" in log, (
            f"agent's commit is not authored by the expected identity: "
            f"{log!r}")
        # The verifier should not have rewritten anything: no
        # `re-authored` line, no log entry. Two short revisions.
        assert "re-authored" not in proc.stderr, (
            f"verifier rewrote a commit that was already right: "
            f"stderr={proc.stderr!r}")


def test_lane_identity_verifier_rewrites_author_flag_commit():
    """Shape 2: an agent that calls `git commit --author='X <x@x>'`
    bypasses the env vars on the author. The verifier must re-author
    the commit, leaving the tree unchanged, and record the rewrite."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="1",
                        LANE_GIT_NAME="Expected",
                        LANE_GIT_EMAIL="expected@example.invalid",
                        FAKE_HERMES_PLAN="commit_with_author")
        repo = make_repo(tmp)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.email"], check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.name"], check=True)

        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        # The agent's marker commit is now authored by the expected
        # identity: the verifier caught the override and re-authored.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H %an <%ae> | %cn <%ce>",
             "--grep=^shape-author-flag$"],
            capture_output=True, text=True, check=True).stdout
        assert "Expected <expected@example.invalid>" in log, (
            f"verifier did not re-author the commit: {log!r}")
        # The rewrite is logged to stderr: an old-sha -> new-sha line.
        assert "re-authored:" in proc.stderr, (
            f"verifier did not log the rewrite: stderr={proc.stderr!r}")
        # The run directory holds the handover, and the verifier wrote
        # the rewrite into it. A reader who comes across $WORK and
        # sees HANDOVER.md is owed the rewrite list, not just stderr.
        run_dir = Path(proc.stdout.strip().splitlines()[-1])
        handover = (run_dir / "HANDOVER.md").read_text()
        assert "re-authored:" in handover, (
            f"rewrite not recorded in HANDOVER.md: {handover!r}")
        # The verifier rewrote only the lane commit and kept its
        # tree. HEAD's tree equals the base's tree (the agent's
        # commit is --allow-empty); a verifier that redrafted would
        # leave them different.
        head_tree = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True).stdout.strip()
        base_tree = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD~1^{tree}"],
            capture_output=True, text=True, check=True).stdout.strip()
        assert head_tree == base_tree, (
            f"verifier changed the tree: head={head_tree} base={base_tree}")


def test_lane_identity_verifier_rewrites_env_override_commit():
    """Shape 3: an agent that sets GIT_AUTHOR_NAME/EMAIL on its own
    `git commit` invocation bypasses the lane's env vars entirely. The
    verifier must re-author the commit; the rewrite is logged."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="1",
                        LANE_GIT_NAME="Expected",
                        LANE_GIT_EMAIL="expected@example.invalid",
                        FAKE_HERMES_PLAN="commit_with_env_override")
        repo = make_repo(tmp)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.email"], check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.name"], check=True)

        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H %an <%ae> | %cn <%ce>",
             "--grep=^shape-env-override$"],
            capture_output=True, text=True, check=True).stdout
        assert "Expected <expected@example.invalid>" in log, (
            f"verifier did not re-author the env-override commit: "
            f"{log!r}")
        assert "re-authored:" in proc.stderr, (
            f"verifier did not log the rewrite: stderr={proc.stderr!r}")


def test_lane_identity_env_vars_win_over_config_override():
    """Shape 4: an agent that sets `git config user.name X` in the
    worktree does not affect the commit when env vars are set."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="1",
                        LANE_GIT_NAME="Expected",
                        LANE_GIT_EMAIL="expected@example.invalid",
                        FAKE_HERMES_PLAN="commit_with_config_override")
        repo = make_repo(tmp)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.email"], check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.name"], check=True)

        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H %an <%ae> | %cn <%ce>",
             "--grep=^shape-config-override$"],
            capture_output=True, text=True, check=True).stdout
        assert "Expected <expected@example.invalid>" in log, (
            f"agent's commit is not authored by the expected identity: "
            f"{log!r}")
        assert "re-authored" not in proc.stderr, (
            f"verifier rewrote a commit that was already right: "
            f"stderr={proc.stderr!r}")


def test_lane_identity_refuses_with_author_override_when_setting_says_so():
    """LANE_GIT_IDENTITY=refuse: the verifier must name the offending
    commit on stderr and exit non-zero."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="1",
                        LANE_GIT_NAME="Expected",
                        LANE_GIT_EMAIL="expected@example.invalid",
                        LANE_GIT_IDENTITY="refuse",
                        FAKE_HERMES_PLAN="commit_with_author")
        repo = make_repo(tmp)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.email"], check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.name"], check=True)

        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        # The verifier prints the refusal on stderr; hermes-implement
        # keeps running, so the script returns 0. The pin is on the
        # message itself, not on the exit code.
        assert "refusing:" in proc.stderr, (
            f"verifier did not print a refusal: stderr={proc.stderr!r}")
        assert "Wrong Author" in proc.stderr, (
            f"refusal does not name the offending identity: "
            f"stderr={proc.stderr!r}")


def test_lane_identity_refuses_before_agent_when_unresolvable():
    """When neither LANE_GIT_NAME/EMAIL nor the worktree's user.name/email
    resolves to a value, hermes-implement must refuse with a named
    error before the agent starts. The agent is never spawned."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # setup_env clears LANE_GIT_* by default (no extra), so the
        # helper falls back to the worktree's user.name/email. Strip
        # both before invoking.
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="1",
                        FAKE_HERMES_PLAN="commit_with_author")
        repo = make_repo(tmp)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.email"], check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.name"], check=True)
        # Clear any global identity inherited by the test process so
        # `git -C <wt> config --get user.name` returns empty. We use a
        # clean HOME and PATH that point at empty config dirs.
        clean_home = tmp / "isolated-home"
        clean_home.mkdir()
        env["HOME"] = str(clean_home)
        env["XDG_CONFIG_HOME"] = str(clean_home)
        env["GIT_CONFIG_GLOBAL"] = str(clean_home / ".gitconfig")
        env["GIT_CONFIG_SYSTEM"] = str(clean_home / "system-gitconfig")

        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        # Exit code 3 is the lane-implement refusal code; the agent
        # was never spawned. The fake hermes would have committed
        # otherwise -- that commit must not exist.
        assert proc.returncode == 3, (
            f"expected exit 3 (lane refused), got {proc.returncode}: "
            f"stderr={proc.stderr!r}")
        assert "cannot pin lane identity" in proc.stderr, (
            f"refusal message missing: stderr={proc.stderr!r}")
        # No commit was made.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H %s"],
            capture_output=True, text=True, check=True).stdout
        assert "shape-author-float" not in log, (
            f"agent committed despite no resolvable identity: {log!r}")


def test_lane_identity_verifier_does_not_leak_pinned_env_to_caller():
    """verify_lane_identity scopes its pinned identity to the rebase
    command alone, so a caller's own GIT_AUTHOR_NAME is not overwritten.
    The test sets a sentinel, runs the verifier on a commit that
    triggers the rebase, and asserts the sentinel survives."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = tmp / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                        check=True, capture_output=True)
        run("init", "--initial-branch=main")
        run("config", "user.email", "real@real")
        run("config", "user.name", "Real")
        (repo / "a.txt").write_text("one\n")
        run("add", "a.txt")
        run("commit", "-m", "base")
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True,
                              text=True).stdout.strip()
        # Author override triggers the rebase path of the verifier.
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty",
             "--author=Wrong Author <wrong@example.invalid>",
             "-m", "shape-author-flag"],
            check=True, capture_output=True)
        # A sentinel the caller's own code may have set, that the
        # verifier must not clobber. LANE_GIT_* and GIT_AUTHOR_* would
        # affect git's behaviour if leaked; the test sets all of them.
        env = {**os.environ,
               "LANE_GIT_NAME": "Caller",
               "LANE_GIT_EMAIL": "caller@x",
               "GIT_AUTHOR_NAME": "Caller",
               "GIT_AUTHOR_EMAIL": "caller@x",
               "GIT_COMMITTER_NAME": "Caller",
               "GIT_COMMITTER_EMAIL": "caller@x"}
        # Run a no-op `true` in front of the verifier so the pinned
        # env vars are still set in the shell when we read them.
        # The verifier is sourced from the test's checkout.
        script = (
            f'set -e; '
            f'. "{LIB}"; '
            # The verifier refuses (Wrong Author vs the worktree's
            # user.name Real). Either refusal code (1) or the
            # rc-specific one for identity mismatch is fine -- the
            # point is that the test's caller-side env is intact.
            f'verify_lane_identity "{repo}" "{base}" || true; '
            # Now read the env vars and emit them so the test can
            # check the caller-side state.
            f'printf "AUTHOR_NAME=%s\\nAUTHOR_EMAIL=%s\\nCOMMITTER_NAME=%s\\nCOMMITTER_EMAIL=%s\\nLANE_NAME=%s\\nLANE_EMAIL=%s\\n" '
            f'"$GIT_AUTHOR_NAME" "$GIT_AUTHOR_EMAIL" "$GIT_COMMITTER_NAME" "$GIT_COMMITTER_EMAIL" "$LANE_GIT_NAME" "$LANE_GIT_EMAIL"'
        )
        proc = subprocess.run(["bash", "-c", script],
                               env=env, capture_output=True, text=True)
        out = proc.stdout
        # Every env var set by the caller must still hold the
        # caller's value -- the verifier must not have exported
        # GIT_AUTHOR_* into this shell.
        assert "AUTHOR_NAME=Caller" in out, (
            f"verifier clobbered caller's GIT_AUTHOR_NAME: {out!r}")
        assert "AUTHOR_EMAIL=caller@x" in out, (
            f"verifier clobbered caller's GIT_AUTHOR_EMAIL: {out!r}")
        assert "COMMITTER_NAME=Caller" in out, (
            f"verifier clobbered caller's GIT_COMMITTER_NAME: {out!r}")
        assert "COMMITTER_EMAIL=caller@x" in out, (
            f"verifier clobbered caller's GIT_COMMITTER_EMAIL: {out!r}")
        assert "LANE_NAME=Caller" in out, (
            f"verifier clobbered caller's LANE_GIT_NAME: {out!r}")
        assert "LANE_EMAIL=caller@x" in out, (
            f"verifier clobbered caller's LANE_GIT_EMAIL: {out!r}")


def _pid_alive(pid: int) -> bool:
    """Is this pid still a live process? Signal 0 asks without delivering."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fast_clocks(env: dict) -> dict:
    """The attempt clock and the two waits below it, in seconds rather than
    minutes, so a test that really hangs resolves in seconds."""
    env["HERMES_IMPLEMENT_TIMEOUT"] = "2"
    env["HERMES_IMPLEMENT_KILL_AFTER"] = "1"
    env["HERMES_IMPLEMENT_SETTLE"] = "1"
    return env


def test_timed_out_attempt_leaves_no_live_process_before_the_next_attempt():
    """Issue #157, row 1: an attempt whose agent ignores SIGTERM must not
    still be running when the next attempt resumes the session.

    The stub sleeps through SIGTERM, which is what `timeout` signals. Left
    alone it outlives the attempt and keeps the lease, and the resume is
    refused with SESSION_NOT_OWNED. The fix kills the attempt's whole
    process group and waits for it to empty; the stub's prev_alive on
    attempt 2's log line is where the test sees that."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = _fast_clocks(setup_env(
            tmp,
            HERMES_IMPLEMENT_ATTEMPTS="2",
            # attempt 1 hangs and ignores SIGTERM; attempt 2 finishes the
            # run. A third call would be an attempt the loop must not make.
            FAKE_HERMES_PLAN="hang_ignore_term:finish_clean:do_nothing"))
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"
        pidfile = tmp / "fake_hermes.pid"

        proc = subprocess.run(
            [str(BIN), "--issue", "157", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        assert len(calls) == 2, (
            f"expected 2 invocations (one hung attempt, one finishing "
            f"attempt), got {len(calls)}: {calls!r}")
        assert "action=hang_ignore_term" in calls[0], calls[0]
        # The pin: attempt 2 started only once attempt 1's agent was
        # really gone. prev_alive=True here is the bug in the issue -- a
        # resume launched on top of a live session owner.
        assert "prev_alive=False" in calls[1], (
            f"attempt 2 started while attempt 1's agent was still alive: "
            f"{calls[1]!r}")

        # And the hung agent is dead when the run returns, not merely by
        # the time attempt 2 started.
        assert pidfile.exists(), (
            f"the hung stub never wrote its pidfile: {pidfile}")
        hung_pid = int(pidfile.read_text().strip())
        assert not _pid_alive(hung_pid), (
            f"the timed-out attempt's agent (pid {hung_pid}) outlived the "
            f"run and still holds the session lease")

        # A SIGTERM-ignoring agent is what --kill-after is for, so the run
        # says the attempt was killed rather than leaving the reader to
        # infer it from a bare exit code.
        assert "hermes timed out" in proc.stderr, (
            f"the run did not report the attempt as timed out: "
            f"{proc.stderr!r}")


def test_refused_resume_does_not_spend_an_attempt():
    """Issue #157, row 3: SESSION_NOT_OWNED is this run's own timing going
    wrong, not a failed attempt. Two refusals under a one-attempt budget
    must still leave the run able to finish."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = _fast_clocks(setup_env(
            tmp,
            # One attempt: if a refusal spent one, this run could not
            # finish and the test would see a FAILED run instead.
            HERMES_IMPLEMENT_ATTEMPTS="1",
            FAKE_HERMES_PLAN="refuse:refuse:finish_clean:do_nothing"))
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "157", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")
        assert "FAILED:" not in proc.stderr, (
            f"a run whose only two refusals were recoverable reported "
            f"FAILED: {proc.stderr!r}")

        calls = log.read_text().splitlines()
        assert len(calls) == 3, (
            f"expected 3 invocations (two refusals then a finish), got "
            f"{len(calls)}: {calls!r}")
        # All three are attempt 1: a refusal spent no attempt, so no call
        # carried --continue and the attempt number never advanced.
        assert all("continue=False" in c for c in calls), (
            f"a refusal spent an attempt: {calls!r}")
        assert "refused (session still owned)" in proc.stderr, (
            f"the script did not report the retry: {proc.stderr!r}")


def test_three_refusals_in_a_row_stop_the_run():
    """Issue #157, row 4: a lease that will not free stops the run with a
    message naming the session and its owner, instead of burning every
    remaining attempt on refusals."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = _fast_clocks(setup_env(
            tmp,
            HERMES_IMPLEMENT_ATTEMPTS="4",
            FAKE_HERMES_PLAN="refuse:refuse:refuse:refuse"))
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "157", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 6, (
            f"expected exit 6 (the lease never freed), got "
            f"{proc.returncode}: stderr={proc.stderr!r}")
        assert "FAILED: session s1 pid 4242 still owned after 3 refusals" \
                in proc.stderr, (
            f"the stop message must name the session and its owner, taken "
            f"from the refusal text: {proc.stderr!r}")

        calls = log.read_text().splitlines()
        # Exactly three: the fourth refusal is never made. A run that kept
        # going would show four.
        assert len(calls) == 3, (
            f"expected exactly 3 invocations before the run stopped, got "
            f"{len(calls)}: {calls!r}")


def test_refusal_cap_still_runs_the_identity_check():
    """A run that stops at the refusal cap must still exit 6 *and* leave
    the commit authored by the expected identity.

    A commit the agent made with `git commit --author=Wrong...` would
    otherwise stay authored by Wrong, because verify_lane_identity -- the
    backstop that re-authors it -- never ran. Attempt 1 commits under the
    wrong identity, then attempts 2+ are refused until the cap.

    The author field is asserted, not the whole log line: the committer
    picks up the lane's exported GIT_COMMITTER_* either way, so an
    assertion that merely greps for the expected name passes against the
    unfixed script too.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = _fast_clocks(setup_env(
            tmp,
            HERMES_IMPLEMENT_ATTEMPTS="2",
            LANE_GIT_NAME="Expected",
            LANE_GIT_EMAIL="expected@example.invalid",
            FAKE_HERMES_PLAN="commit_with_author:refuse:refuse:refuse:refuse"))
        repo = make_repo(tmp)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.email"], check=True)
        subprocess.run(["git", "-C", str(repo), "config",
                        "--unset", "user.name"], check=True)

        proc = subprocess.run(
            [str(BIN), "--issue", "157", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 6, (
            f"expected exit 6 (the lease never freed), got "
            f"{proc.returncode}: stderr={proc.stderr!r}")
        assert "still owned after 3 refusals" in proc.stderr, (
            f"the run did not stop at the refusal cap: "
            f"stderr={proc.stderr!r}")

        # The commit attempt 1 left, and only that one: the refusals make
        # no commit of their own.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%an <%ae> | %cn <%ce>",
             "--grep=^shape-author-flag$"],
            capture_output=True, text=True, check=True).stdout
        assert log.strip(), (
            f"the agent's commit is missing; the run reauthored nothing "
            f"to read: stderr={proc.stderr!r}")
        author, _, committer = log.strip().partition("|")
        assert "Expected <expected@example.invalid>" in author, (
            f"the refusal-cap path skipped verify_lane_identity, so the "
            f"commit is still authored by the agent's --author "
            f"(author={author.strip()!r} committer={committer.strip()!r})")
        assert "re-authored:" in proc.stderr, (
            f"the verifier did not run on the refusal-cap path: "
            f"stderr={proc.stderr!r}")


def test_a_non_refusal_resets_the_refusal_count():
    """Issue #157, row 5: refusals are counted in a row. An attempt that
    got past the refusal resets the count, so a later refusal is retried
    rather than counting as the third."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = _fast_clocks(setup_env(
            tmp,
            HERMES_IMPLEMENT_ATTEMPTS="2",
            FAKE_HERMES_PLAN="refuse:commit_dirty:refuse:finish_clean"))
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "157", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        assert len(calls) == 4, (
            f"expected 4 invocations (refuse, commit, refuse, finish), got "
            f"{len(calls)}: {calls!r}")
        # The second refusal was retried rather than being the third in a
        # row: with the count unreset the run would have stopped with exit
        # 6 before the fourth call.
        assert "FAILED:" not in proc.stderr, (
            f"a reset refusal count still stopped the run: {proc.stderr!r}")
        assert "action=finish_clean" in calls[3], calls[3]


def main() -> int:
    failures: list[tuple[str, str]] = []
    for name, fn in [
        ("hermes-implement holds the GPU lock across a retry and "
         "inherits fd 8 to its hermes children",
         test_hermes_implement_holds_gpu_lock_across_retry),
        ("hermes-implement resumes with --continue after one commit "
         "and a dirty tree",
         test_hermes_implement_resumes_after_one_commit_with_dirty_tree),
        ("hermes-implement resumes after an attempt 1 that exits 124 "
         "with a dirty tree",
         test_hermes_implement_resumes_after_timeout_with_dirty_tree),
        ("hermes-implement does not retry when attempt 1 already "
         "finished (clean tree + non-empty HANDOVER.md)",
         test_hermes_implement_does_not_retry_when_finished),
        ("hermes-implement names the uncommitted files on the final "
         "FAILED line and does not commit leftover work itself",
         test_hermes_implement_reports_uncommitted_files_when_attempts_exhausted),
        ("a timed-out attempt leaves no live agent before the next "
         "attempt resumes the session",
         test_timed_out_attempt_leaves_no_live_process_before_the_next_attempt),
        ("a SESSION_NOT_OWNED refusal does not spend an attempt",
         test_refused_resume_does_not_spend_an_attempt),
        ("three refusals in a row stop the run and name the session "
         "owner",
         test_three_refusals_in_a_row_stop_the_run),
        ("a run stopped at the refusal cap still re-authors a lane "
         "commit made with `git commit --author=...`",
         test_refusal_cap_still_runs_the_identity_check),
        ("a non-refusal resets the refusal count",
         test_a_non_refusal_resets_the_refusal_count),
        ("hermes-implement pins the lane identity when the agent uses "
         "`git -c user.name=... -c user.email=...`",
         test_lane_identity_env_vars_win_over_c_flag_override),
        ("hermes-implement re-authors a lane commit when the agent "
         "uses `git commit --author=...`",
         test_lane_identity_verifier_rewrites_author_flag_commit),
        ("hermes-implement re-authors a lane commit when the agent "
         "sets GIT_AUTHOR_NAME/EMAIL on its own git invocation",
         test_lane_identity_verifier_rewrites_env_override_commit),
        ("hermes-implement pins the lane identity when the agent "
         "sets `git config user.name` in the worktree",
         test_lane_identity_env_vars_win_over_config_override),
        ("hermes-implement refuses with a named message when "
         "LANE_GIT_IDENTITY=refuse",
         test_lane_identity_refuses_with_author_override_when_setting_says_so),
        ("hermes-implement refuses before the agent starts when no "
         "identity resolves",
         test_lane_identity_refuses_before_agent_when_unresolvable),
        ("verify_lane_identity does not leak its pinned identity into "
         "the caller's shell",
         test_lane_identity_verifier_does_not_leak_pinned_env_to_caller),
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
    print("PASS hermes-implement holds the GPU lock across every attempt, "
          "and resumes with --continue until the tree is clean and "
          "HANDOVER.md is present")
    return 0


if __name__ == "__main__":
    sys.exit(main())