#!/usr/bin/env python3
"""Pin the review script's one-review-per-profile lock."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skills" / "hermes-review" / "scripts" / "hermes-scrutinize.sh"

FAKE_AGENT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import hashlib, os, re, sys, time
    args = sys.argv[1:]
    # hermes-scrutinize.sh now passes the prompt through
    # `--query-file PATH` rather than `-z "$prompt"`. The stub has to
    # pull it from the file pointed at and write findings there. (The
    # previous `-z` path is gone -- a stub that still expects `-z`
    # would IndexError on its first call.)
    has_query_file = "--query-file" in args
    query_sha = ""
    if has_query_file:
        qf = args[args.index("--query-file") + 1]
        with open(qf, "rb") as fh:
            data = fh.read()
        query_sha = hashlib.sha256(data).hexdigest()
    findings = None
    if has_query_file:
        # `Write your findings to <path>` is in the prompt body.
        with open(args[args.index("--query-file") + 1], "r") as fp:
            prompt = fp.read()
        m = re.search(r"Write your findings to (\\S+)", prompt)
        if m:
            findings = m.group(1)
    profile = (args[args.index("-p") + 1] if "-p" in args else "default")
    log = sys.argv[0] + ".log"
    # First three space-separated tokens are the fixed parse contract:
    # `kind profile ts`. Extra attributes follow as a single token
    # (no spaces inside the value) so the existing `line.split()`
    # does not fragment them into separate cells. Commas separate the
    # key=value pairs.
    query_attrs = (f"has_query_file={has_query_file},"
                   f"query_sha={query_sha}")
    with open(log, "a") as f:
        f.write(f"start {profile} {time.monotonic()} {query_attrs}\\n")
    time.sleep(1.5)
    if findings is not None:
        with open(findings, "w") as f:
            f.write("no findings\\n")
    with open(log, "a") as f:
        f.write(f"end {profile} {time.monotonic()}\\n")
    # --query-file PATH --oneshot -Q: simulate -Q by writing the
    # `session_id: <id>` line to stdout before the answer, the way the
    # real agent does. The helper strips that line, so the script's
    # caller never sees it.
    print("session_id: stub-session-123")
    print("0 findings")
""")


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
    # Uncommitted, so the script reviews the working tree with no remote.
    (repo / "a.txt").write_text("two\n")
    return repo


def run_pair(profiles: tuple[str, str]) -> list[tuple[str, str, float]]:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp)
        agent = tmp / "agent"
        agent.write_text(FAKE_AGENT)
        agent.chmod(0o755)
        env = dict(os.environ, HERMES_PYTHON=str(agent), TMPDIR=str(tmp))
        procs = [subprocess.Popen([str(SCRIPT), "--profile", p], cwd=repo,
                                  env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for p in profiles]
        for p in procs:
            out, err = p.communicate(timeout=30)
            assert p.returncode == 0, f"exit {p.returncode}: {err}"
            assert Path(out.strip()).name == "FINDINGS.md", out
        # Read the log only after every review has exited: leaving the
        # TemporaryDirectory deletes it, and a reviewer still waiting for
        # the slot dies with it.

        # A log line is `<kind> <profile> <ts>` plus optional `key=value`
        # attributes with no spaces inside. A 3-token cap keeps the
        # trailing attributes grouped; the test needs the kind and the
        # timestamp.
        def parse_line(line):
            parts = line.split(None, 3)
            return parts[0], parts[1], float(parts[2])
        events = []
        for line in (tmp / "agent.log").read_text().splitlines():
            kind, profile, ts = parse_line(line)
            events.append((kind, profile, ts))
        return events


def run_n(profiles: tuple[str, ...],
          slots_env: dict[str, str] | None = None
          ) -> list[tuple[str, str, float]]:
    """Run N reviews in parallel with optional REVIEW_SLOTS_<PROFILE> entries."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp)
        agent = tmp / "agent"
        agent.write_text(FAKE_AGENT)
        agent.chmod(0o755)
        env = dict(os.environ, HERMES_PYTHON=str(agent), TMPDIR=str(tmp))
        if slots_env:
            env.update(slots_env)
        procs = [subprocess.Popen([str(SCRIPT), "--profile", p], cwd=repo,
                                  env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for p in profiles]
        for p in procs:
            out, err = p.communicate(timeout=60)
            assert p.returncode == 0, f"exit {p.returncode}: {err}"
            assert Path(out.strip()).name == "FINDINGS.md", out
        events = []
        # `<kind> <profile> <ts>` plus optional key=value attributes. Cap
        # the split so trailing attributes stay grouped in a single
        # trailing slot; only the first three fields matter.
        for line in (tmp / "agent.log").read_text().splitlines():
            parts = line.split(None, 3)
            kind, profile, ts = parts[0], parts[1], float(parts[2])
            events.append((kind, profile, ts))
        return events


def overlapped(events) -> bool:
    kinds = [k for k, _, _ in sorted(events, key=lambda e: e[2])]
    return kinds != ["start", "end", "start", "end"]


def any_overlap(events) -> bool:
    """True iff at least two reviews are running simultaneously."""
    sorted_e = sorted(events, key=lambda e: e[2])
    first_end = next((ts for k, _, ts in sorted_e if k == "end"), None)
    if first_end is None:
        return False
    starts_before = sum(1 for k, _, ts in sorted_e
                        if k == "start" and ts < first_end)
    return starts_before >= 2


def all_overlap(events) -> bool:
    """True iff every review starts before any review ends."""
    sorted_e = sorted(events, key=lambda e: e[2])
    first_end = next((ts for k, _, ts in sorted_e if k == "end"), None)
    if first_end is None:
        return False
    starts = [ts for k, _, ts in sorted_e if k == "start"]
    return bool(starts) and all(ts < first_end for ts in starts)


def test_same_profile_runs_one_at_a_time():
    events = run_pair(("rev", "rev"))
    assert not overlapped(events), \
        f"expected no overlap on a 1-slot profile, got {events}"


def test_different_profiles_run_together():
    assert overlapped(run_pair(("rev-a", "rev-b")))


def test_three_slot_profile_runs_three_together():
    # All three reviews hit slot acquisition in parallel and take slots 1, 2,
    # 3 from the per-profile lock pool. The assertion is "all starts before
    # any end" rather than "any two overlap": the latter passes with two slots
    # too, which would mistake a 2-slot profile for a 3-slot one.
    assert all_overlap(run_n(("rev", "rev", "rev"),
                             {"REVIEW_SLOTS_REV": "3"}))


def test_one_slot_profile_serializes_two():
    # No REVIEW_SLOTS_REV set, so the slot count falls back to 1; the second
    # review waits the 2-second poll for slot 1 to free, which costs the test
    # that gap but proves the second did not start until the first ended.
    events = run_n(("rev", "rev"))
    assert not any_overlap(events), \
        f"expected no overlap on a 1-slot profile, got {events}"


def test_waiting_message_prints_once():
    # Hold slot 1 externally for long enough that the loop inside
    # acquire_review_slot retries several times: every two-second poll that
    # finds no slot has to log the wait exactly once, not once per poll --
    # otherwise the stream of identical messages looks like a hung loop to a
    # reader tailing stderr. Six seconds of holding costs three polls
    # (t=0, t=2, t=4) before the holder releases, so a repeated line would
    # show three times here instead of once.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp)
        agent = tmp / "agent"
        agent.write_text(FAKE_AGENT)
        agent.chmod(0o755)
        # acquire_review_slot moved its lock files to AGENT_STATE_DIR so two
        # callers with different TMPDIRs contend on the same slot. Pin that
        # here too -- this test fails if the lock file's location changes.
        state_dir = tmp / "state"
        env = dict(os.environ, HERMES_PYTHON=str(agent), AGENT_STATE_DIR=str(state_dir))
        slot = state_dir / "hermes-review-rev-1.lock"
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.touch()
        # flock the lock from a separate process so hermes-scrutinize.sh's
        # own flock -n 9 fails for as long as we need it to.
        holder = subprocess.Popen(["flock", str(slot), "sleep", "6"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            p = subprocess.run([str(SCRIPT), "--profile", "rev"],
                               cwd=repo, env=env, capture_output=True,
                               text=True, timeout=30)
        finally:
            holder.wait()
        assert p.returncode == 0, f"exit {p.returncode}: {p.stderr}"
        waiting = [line for line in p.stderr.splitlines()
                   if "waiting for a review slot" in line]
        assert len(waiting) == 1, \
            f"expected exactly one 'waiting' line, got {len(waiting)}: {p.stderr!r}"


def test_lock_path_no_longer_depends_on_tmpdir():
    # The lock moved from ${TMPDIR:-/tmp} to AGENT_STATE_DIR precisely so two
    # callers with different TMPDIRs contend on the same slot. A lane sets
    # its own TMPDIR, and the previous code put the lock file there, so two
    # callers could each have held the lock for "slot 1" and run together.
    # This test pins the new behaviour: a holder that locks the state-dir
    # lock file from one TMPDIR blocks a reviewer whose TMPDIR is different.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp)
        agent = tmp / "agent"
        agent.write_text(FAKE_AGENT)
        agent.chmod(0o755)
        state_dir = tmp / "state"
        state_dir.mkdir()
        # Caller A: its own TMPDIR (could be a lane), the new shared state
        # directory, and a holder process keeping slot 1 taken.
        env_a = dict(os.environ, HERMES_PYTHON=str(agent),
                     AGENT_STATE_DIR=str(state_dir),
                     TMPDIR=str(tmp / "tmpdir-a"))
        (tmp / "tmpdir-a").mkdir()
        slot = state_dir / "hermes-review-rev-1.lock"
        slot.touch()
        holder = subprocess.Popen(["flock", str(slot), "sleep", "6"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            # Caller B uses a *different* TMPDIR; without the AGENT_STATE_DIR
            # move it would have its own lock file and not contend.
            env_b = dict(os.environ, HERMES_PYTHON=str(agent),
                         AGENT_STATE_DIR=str(state_dir),
                         TMPDIR=str(tmp / "tmpdir-b"))
            (tmp / "tmpdir-b").mkdir()
            # Long timeout so the holder would still be alive when B's
            # reviewer tries to run; if B finds the slot it has been a
            # silent regression.
            p = subprocess.run([str(SCRIPT), "--profile", "rev",
                                "--timeout", "30"],
                               cwd=repo, env=env_b, capture_output=True,
                               text=True, timeout=25)
        finally:
            holder.wait()
        # The reviewer should have waited for the lock and failed when its
        # own timeout fired; the exact exit code from `timeout` is 124.
        # What matters for the regression is that B did NOT complete its
        # review before the holder released -- which the holder.log and
        # agent.log timestamps would show. We assert on a wait line
        # appearing in stderr instead: that line only exists when
        # acquire_review_slot's retry loop ran, which only runs when the
        # second caller found the slot taken.
        waiting = [line for line in p.stderr.splitlines()
                   if "waiting for a review slot" in line]
        assert waiting, (
            f"reviewer with TMPDIR=tmpdir-b did not contend for the slot "
            f"under tmpdir-a's TMPDIR: nothing was waiting on stderr. "
            f"stdout={p.stdout!r} stderr={p.stderr!r}")


def run_one(*, slots: str | None = None, timeout: str | None = None,
            timeout_env: dict[str, str] | None = None) -> int:
    """Run one review with the given slot/timeout setup; return its exit code."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp)
        agent = tmp / "agent"
        agent.write_text(FAKE_AGENT)
        agent.chmod(0o755)
        env = dict(os.environ, HERMES_PYTHON=str(agent), TMPDIR=str(tmp))
        if timeout_env:
            env.update(timeout_env)
        args = [str(SCRIPT), "--profile", "rev"]
        if slots:
            env["REVIEW_SLOTS_REV"] = slots
        if timeout is not None:
            args.extend(["--timeout", timeout])
        p = subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True)
        return p.returncode


def test_timeout_precedence_per_profile_overrides_default():
    # REVIEW_TIMEOUT_REV=1 with no --timeout: the per-profile value wins over
    # HERMES_REVIEW_TIMEOUT (which would otherwise be 900), and the agent's
    # 1.5s sleep exceeds 1s. `timeout(1)` returns 124.
    rc = run_one(timeout_env={"REVIEW_TIMEOUT_REV": "1"})
    assert rc == 124, f"expected 124 from REVIEW_TIMEOUT_REV=1, got {rc}"


def test_timeout_precedence_explicit_overrides_per_profile():
    # Same REVIEW_TIMEOUT_REV=1, but --timeout 10 is given: the explicit
    # value outranks the per-profile value, the agent's 1.5s sleep fits, and
    # the script returns 0. Without the precedence this would also exit 124.
    rc = run_one(timeout_env={"REVIEW_TIMEOUT_REV": "1"}, timeout="10")
    assert rc == 0, f"expected 0 from --timeout 10 overriding REVIEW_TIMEOUT_REV=1, got {rc}"


def test_two_scrutinize_calls_on_same_gpu_group_serialize():
    # End-to-end of the GPU lock from the script's perspective: two
    # reviewers with *different* profiles but the same GPU group serialise.
    # Different profiles means the review-slot lock does not serialise them
    # (test_different_profiles_run_together already pins that), so the
    # only thing holding them apart is the GPU group lock. The second's
    # start must wait for the first's end.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp)
        agent = tmp / "agent"
        agent.write_text(FAKE_AGENT)
        agent.chmod(0o755)
        state_dir = tmp / "state"
        # Two profiles -- rev-a and rev-b -- both mapped to the same GPU
        # group. The review-slot lock has them in separate pools, so they
        # would otherwise run together.
        env = dict(os.environ,
                   HERMES_PYTHON=str(agent),
                   AGENT_STATE_DIR=str(state_dir),
                   GPU_GROUP_REV_A="logan-3099",
                   GPU_GROUP_REV_B="logan-3099",
                   TMPDIR=str(tmp))
        profiles = ("rev-a", "rev-b")
        procs = [subprocess.Popen([str(SCRIPT), "--profile", p],
                                  cwd=repo, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for p in profiles]
        for p in procs:
            out, err = p.communicate(timeout=60)
            assert p.returncode == 0, f"exit {p.returncode}: {err}"
            assert Path(out.strip()).name == "FINDINGS.md", out
        events = []
        # See run_pair for the log format.
        for line in (tmp / "agent.log").read_text().splitlines():
            parts = line.split(None, 3)
            kind, profile, ts = parts[0], parts[1], float(parts[2])
            events.append((kind, profile, ts))
        # Two reviewers on one GPU group must not overlap: the second
        # `start` lands after the first `end`. The lock wait happens
        # *outside* the agent's sleep, so the second's `start` is at least
        # 2 seconds after the first's `end` -- the polling interval
        # acquire_gpu_lock waits before retrying.
        sorted_e = sorted(events, key=lambda e: e[2])
        first_end = next((ts for k, _, ts in sorted_e if k == "end"), None)
        starts_before = sum(1 for k, _, ts in sorted_e
                            if k == "start" and ts < first_end)
        assert starts_before == 1, (
            f"two reviewers on one GPU group overlapped (only the GPU "
            f"lock could have serialised them; the review-slot lock "
            f"treats them as different profiles): {events}")


# ---------------------------------------------------------------------------
# hermes-review-pr driver: a review phase runs only against the PR head its
# worktree was built from, and every artifact names that head.

DRIVER = REPO_ROOT / "bin" / "hermes-review-pr"

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Returns the headRefName/baseRefName pair as a tab-separated line, the
    # shape `gh pr view ... --jq '[.headRefName, .baseRefName] | @tsv'` prints.
    # The PR number is ignored: the test drives refs, not a real PR id.
    echo -e "feature\\tmain"
""")


# Writes different findings on every call, so a second review of an unchanged
# head still has something to commit. Only the review prompt names a findings
# file; the readback's reply goes to stdout.
FAKE_REVIEW_AGENT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import hashlib, os, re, sys, time
    args = sys.argv[1:]
    # hermes-review-pr now passes the prompt through `--query-file PATH`
    # rather than `-z "$prompt"`. Reading it via the helper-backed file
    # path gives the stub the same content the real agent would see.
    has_query_file = "--query-file" in args
    qf = None
    if has_query_file:
        qf = args[args.index("--query-file") + 1]
    prompt = ""
    if qf and os.path.exists(qf):
        with open(qf, "r") as f:
            prompt = f.read()
    m = re.search(r"Write your findings to (\\S+)", prompt)
    if m:
        findings = m.group(1)
        with open(findings, "w") as f:
            # The PID plus the monotonic clock makes each invocation's
            # content different without depending on randomness or the wall
            # clock.
            f.write(f"no findings from pid {os.getpid()} at {time.monotonic()}\\n")
    time.sleep(0.2)
    # -Q: prepend the session_id marker; the helper strips it on the
    # way back, so the script's caller never sees it on stdout.
    print("session_id: stub-session-123")
    print("0 findings")
""")


def make_review_pr_repo(tmp: Path) -> tuple[Path, Path, Path]:
    """Create a remote (bare) and a local clone pointing at it, with a
    `feature` branch diverged from `main`. Returns (local_repo, remote_repo,
    bin_dir)."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()

    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet",
                    "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    # git init --bare refuses --initial-branch on older git; set HEAD to main
    # so origin/HEAD points at refs/heads/main and the worktree creation lands.
    subprocess.run(["git", "-C", str(remote), "symbolic-ref",
                    "HEAD", "refs/heads/main"],
                   check=True, capture_output=True)

    repo = tmp / "repo"
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main",
                    str(repo)],
                   check=True, capture_output=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("remote", "add", "origin", str(remote))
    (repo / "a.txt").write_text("version one\n")
    run("add", "a.txt")
    run("commit", "-m", "base")
    run("push", "--quiet", "origin", "main:main")
    # `feature` diverges from `main` so the diff the reviewer sees is non-empty
    # (an empty diff makes hermes-scrutinize.sh refuse with "no changes").
    run("checkout", "--quiet", "-b", "feature")
    (repo / "a.txt").write_text("version one\nfeature line\n")
    run("commit", "-a", "-m", "feature commit")
    run("push", "--quiet", "origin", "feature:feature")
    return repo, remote, bin_dir


def run_review_pr(repo: Path, *, pr: int = 1, phase: str = "review",
                  answers: Path | None = None,
                  extra_env: dict[str, str] | None = None,
                  profile: str = "rev",
                  reuse_tmp: Path | None = None
                  ) -> tuple[subprocess.CompletedProcess, Path]:
    """Invoke the hermes-review-pr driver once against the test fixture.

    Returns (result, wt_root). Calls sharing `reuse_tmp` share the review
    worktree, as two phases of one review do.
    """
    if reuse_tmp is not None:
        tmp = reuse_tmp
        cleanup = False
    else:
        tmp = Path(tempfile.mkdtemp(prefix="hermes-review-pr-"))
        cleanup = True
    try:
        gh = tmp / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        agent = tmp / "agent"
        agent.write_text(FAKE_REVIEW_AGENT)
        agent.chmod(0o755)
        wt_root = tmp / "wt"
        wt_root.mkdir(exist_ok=True)
        priors = tmp / "priors.txt"
        priors.write_text("priors\n")
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "HERMES_PYTHON": str(agent),
            "REVIEW_WT_ROOT": str(wt_root),
            "REVIEW_PRIORS": str(priors),
            "REVIEW_PROFILE": profile,
            "TMPDIR": str(tmp),
        }
        if extra_env:
            env.update(extra_env)
        args = [str(DRIVER), "--pr", str(pr), "--phase", phase]
        if answers is not None:
            args.extend(["--answers", str(answers)])
        result = subprocess.run(args, cwd=repo, env=env,
                                capture_output=True, text=True, timeout=60)
        return result, wt_root
    finally:
        if cleanup:
            shutil.rmtree(tmp, ignore_errors=True)


def push_new_feature_commit(repo: Path) -> str:
    """Advance the `feature` branch by one commit and return the new SHA."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    run("checkout", "--quiet", "feature")
    (repo / "a.txt").write_text("version one\nfeature line\nfeature line two\n")
    run("commit", "-a", "-m", "feature commit two")
    run("push", "--quiet", "origin", "feature:feature")
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "feature"],
                          check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def artifact_commit_message(wt: Path) -> str:
    """Last commit message in the review worktree, where REVIEW_NOTES.md is
    parked."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(wt), "log", "-1", "--format=%s%n%n%b"],
                          check=True, capture_output=True, text=True,
                          env=env).stdout


def test_second_review_against_moved_head_is_refused():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _remote, _bin = make_review_pr_repo(tmp)
        first, wt_root = run_review_pr(repo, reuse_tmp=tmp)
        assert first.returncode == 0, f"first review failed: {first.stderr}"
        # The driver parks REVIEW_NOTES.md inside review-pr-N under the root.
        wt = wt_root / "review-pr-1"
        assert (wt / "REVIEW_NOTES.md").exists(), first.stderr
        new_sha = push_new_feature_commit(repo)
        # Sanity: the local fetch actually advanced; without this the test
        # would be indistinguishable from the no-update case.
        local_origin_feature = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "origin/feature"],
            check=True, capture_output=True, text=True).stdout.strip()
        assert local_origin_feature == new_sha
        second, _ = run_review_pr(repo, reuse_tmp=tmp)
        assert second.returncode != 0, (
            f"second review succeeded but the head moved; the old source would "
            f"be attributed to the new SHA. stdout={second.stdout!r} "
            f"stderr={second.stderr!r}")
        assert "stale source" in second.stderr, second.stderr
        # And the second run was a refusal, not a continuation: exit 1.
        assert second.returncode == 1


def test_review_continues_when_head_has_not_moved():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _remote, _bin = make_review_pr_repo(tmp)
        first, _wt_root = run_review_pr(repo, reuse_tmp=tmp)
        assert first.returncode == 0, f"first review failed: {first.stderr}"
        second, _ = run_review_pr(repo, reuse_tmp=tmp)
        assert second.returncode == 0, (
            f"second review against an unchanged head refused: {second.stderr}")


def force_push_feature_to_ancestor(repo: Path) -> str:
    """Force-push origin/feature back to its first commit (an ancestor of the
    current tip), as an author dropping their last commit does, and return
    the new SHA."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    # The first feature commit is HEAD~ on feature: feature-c1 was the first
    # one pushed, then feature-c2 was added on top. Rewind by one.
    run("checkout", "--quiet", "feature")
    run("reset", "--hard", "--quiet", "HEAD~")
    run("push", "--quiet", "--force-with-lease", "origin", "feature:feature")
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "feature"],
                          check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def test_force_pushed_ancestor_head_is_refused():
    # The rewound head is an ancestor of the worktree's, so only an exact
    # comparison with the built-from head refuses it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _remote, _bin = make_review_pr_repo(tmp)
        first, wt_root = run_review_pr(repo, reuse_tmp=tmp)
        assert first.returncode == 0, f"first review failed: {first.stderr}"
        new_remote_sha = force_push_feature_to_ancestor(repo)
        wt = wt_root / "review-pr-1"
        wt_head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        is_ancestor = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor",
             new_remote_sha, wt_head],
            check=False, capture_output=True, text=True)
        assert is_ancestor.returncode == 0, (
            "fixture setup is wrong: the rewound remote head should be an "
            f"ancestor of the worktree HEAD ({wt_head}); got {new_remote_sha}")
        second, _ = run_review_pr(repo, reuse_tmp=tmp)
        assert second.returncode != 0, (
            f"second review succeeded after a backwards force-push; the old "
            f"source would be attributed to the rebased PR head. "
            f"stdout={second.stdout!r} stderr={second.stderr!r}")
        assert "stale source" in second.stderr, second.stderr
        assert second.returncode == 1


def test_review_after_readback_records_pr_head_not_readback_commit():
    # By the review phase the worktree's HEAD is the readback commit; both
    # artifacts must still name the PR head.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _remote, _bin = make_review_pr_repo(tmp)
        # Capture the PR head SHA before any driver run: both artifact
        # commits must contain this exact value.
        pr_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "origin/feature"],
            check=True, capture_output=True, text=True).stdout.strip()
        readback, wt_root = run_review_pr(repo, reuse_tmp=tmp, phase="readback")
        assert readback.returncode == 0, f"readback failed: {readback.stderr}"
        wt = wt_root / "review-pr-1"
        assert (wt / "READBACK.md").exists(), readback.stderr
        readback_msg = artifact_commit_message(wt)
        assert pr_head[:7] in readback_msg, (
            f"READBACK.md artifact should name the PR head ({pr_head[:7]}), "
            f"not the readback commit. got: {readback_msg!r}")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        readback_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, env=env).stdout.strip()
        assert readback_sha[:7] != pr_head[:7], (
            "fixture degenerate: readback commit equals PR head")
        assert readback_sha[:7] not in readback_msg, (
            f"READBACK.md artifact must not name the readback commit itself "
            f"({readback_sha[:7]}); it should name the PR head ({pr_head[:7]}). "
            f"got: {readback_msg!r}")
        review, _ = run_review_pr(repo, reuse_tmp=tmp, phase="review")
        assert review.returncode == 0, f"review failed: {review.stderr}"
        review_msg = artifact_commit_message(wt)
        assert pr_head[:7] in review_msg, (
            f"REVIEW_NOTES.md artifact should name the PR head ({pr_head[:7]}), "
            f"not the review commit. got: {review_msg!r}")
        # The review commit itself sits on top of the readback commit, which
        # sits on top of the PR head. The PR head is HEAD^^ here.
        assert pr_head[:7] != review_msg.split("\n")[0][:7], (
            "subject line should not be the PR head's short SHA")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)
