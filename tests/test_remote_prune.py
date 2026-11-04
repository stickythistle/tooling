#!/usr/bin/env python3
"""Pin remote-prune's reporting and its two fences against accidental deletion.

A bench dir is matched to its worktree by `tree_name`, because the worktree is
the only place `git cherry` can ask whether the branch landed. One with no
worktree behind it is left alone, as is the trunk and any name outside the
alphabet `tree_name` emits.

The fake ssh answers the idle threshold rather than calling `stat`, so the
decision is pinned without depending on the local clock.

The dry-run report is what an operator reads before `--prune`, so every label it
prints is pinned against the bench it was given.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_PRUNE = REPO_ROOT / "bin" / "remote-prune"


# ---------------------------------------------------------------------------
# Project setup
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str, check: bool = True,
        capture: bool = True) -> subprocess.CompletedProcess:
    """A git invocation that defaults to failing the test on error."""
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=capture, text=check or capture, check=check)


def setup_project(tmpdir: Path) -> tuple[Path, Path]:
    """Build a fake project: main repo + bare origin + two worktrees.

    Returns (project, origin). The worktrees represent the two shapes the
    prune cares about: `wt-landed` whose branch is already on main
    (cherry returns empty), and `wt-unlanded` whose branch carries one
    extra commit (cherry returns `+`).
    """
    project = tmpdir / "proj"
    project.mkdir()
    origin = tmpdir / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--quiet", "--initial-branch=main")
    git(project, "init", "--quiet")
    for k, v in [("user.email", "test@test"), ("user.name", "test")]:
        git(project, "config", k, v)
    # `git init` defaults to `master` on this host; the rest of the
    # tooling assumes main, so rename up front rather than carrying the
    # old name through every assertion.
    git(project, "checkout", "-q", "-b", "main")
    # A manifest the reader accepts: the prune resolves the role from
    # this file via lib/manifest.py.
    (project / "gates.toml").write_text(
        '[[task]]\nname = "bench"\nrole = "builder"\n'
        'command = "echo bench"\n')
    git(project, "remote", "add", "origin", str(origin))
    (project / "README").write_text("seed\n")
    git(project, "add", "README")
    git(project, "commit", "--quiet", "-m", "seed")
    git(project, "push", "--quiet", "origin", "main")

    wt_landed = tmpdir / "wt-landed"
    git(project, "worktree", "add", "--quiet", str(wt_landed), "-b",
        "landed-branch", "main")
    # landed-branch has the same content as main; `git cherry` will be empty.
    git(wt_landed, "push", "--quiet", "origin", "landed-branch")

    wt_unlanded = tmpdir / "wt-unlanded"
    git(project, "worktree", "add", "--quiet", str(wt_unlanded), "-b",
        "unlanded-branch", "main")
    (wt_unlanded / "EXTRA").write_text("work\n")
    git(wt_unlanded, "add", "EXTRA")
    git(wt_unlanded, "commit", "--quiet", "-m", "extra work")
    git(wt_unlanded, "push", "--quiet", "origin", "unlanded-branch")

    # refresh origin/main so the cherry call has a fresh upstream ref
    git(project, "fetch", "--quiet", "origin")
    return project.resolve(), origin.resolve()


def make_fakebin(tmpdir: Path, idle_days: int = 0,
                 idle_bytes: int = 0,
                 landed_bytes: int = 1234567,
                 bench_rc: int = 0,
                 ghost_in_bench: bool = True,
                 extra_bench: tuple = ()) -> Path:
    """A fake ssh answering the bench listing and the size queries.

    `idle_days=0` makes every unlanded tree old by the predicate, so the idle
    path runs without depending on the clock. `landed_bytes` is what the
    bench-info query returns as the size, pinned so a regression that zeroes
    the sum without changing a label is caught. `bench_rc` non-zero exercises
    the unreachable branch.

    The bench-info query (`bash -s -- <name>`) returns `<mtime> <size>`; the
    mtime is `date +%s`, i.e. now, which is strictly after the test branch was
    created, so the fresh-branch signal does not fire. The idle query
    (`bash -s -- <name> <days>`) is the two-arg form.
    """
    fake = tmpdir / "fakebin"
    fake.mkdir()
    bench_lines = ["wt_landed", "wt_unlanded", *extra_bench]
    if ghost_in_bench:
        # A bench dir with no matching worktree behind it. The predicate
        # must classify it as uncertain, never delete it.
        bench_lines.append("ghost_tree")
    bench_block = "\n".join(f'echo "{l}"' for l in bench_lines) + "\n"
    # The incremental-check heredoc returns idle_bytes when the script's
    # [ -d ] test would have passed and the age threshold is met; with
    # idle_days=0, the script's age check is always true, so the [ -d ]
    # test alone decides. We force the "yes, drop it" path here.
    inc_block = (
        'cat > /dev/null\n'
        f'echo "{idle_bytes}"\n'
        'exit 0\n'
    )
    script = f"""#!/bin/bash
HOST=$1; shift
CMD="$*"
case "$HOST" in
  fakehost)
    if [[ "$CMD" == "ls -1 \\$HOME/bench"* ]]; then
{bench_block}      exit {bench_rc}
    elif [[ "$CMD" == "flock \\$HOME/bench/.lock"* ]]; then
      cat
    elif [ "$1" = "bash" ] && [ "$2" = "-s" ]; then
      shift 3
      if [ "$#" -eq 1 ]; then
        # bench-info query: one arg (the dir name). mtime defaults to now
        # (newer than the branch's creation, so the fresh-branch signal
        # does not fire); the test can pin an old mtime via
        # FAKE_BENCH_MTIME to exercise that signal instead.
        echo "${{FAKE_BENCH_MTIME:-$(date +%s)}} {landed_bytes}"
      else
        # idle query: two args (dir name, days).
{inc_block}      fi
    fi
    ;;
esac
"""
    p = fake / "ssh"
    p.write_text(script)
    p.chmod(0o755)
    return fake


def make_bench_fake(tmpdir: Path, bench_names: tuple,
                    mtime_expr: str, size: int = 777) -> Path:
    """A fake ssh that lists exactly `bench_names` and answers the
    bench-info query with a caller-chosen mtime. `mtime_expr` is a shell
    word (e.g. `$(date +%s)` or a literal) so a test can pin the bench
    dir's mtime relative to a branch's creation. The idle query answers
    non-zero so no incremental dir is ever proposed.
    """
    fake = tmpdir / "benchfake"
    fake.mkdir()
    block = "\n".join(f'echo "{n}"' for n in bench_names) + "\n"
    script = f"""#!/bin/bash
HOST=$1; shift
CMD="$*"
case "$HOST" in
  fakehost)
    if [[ "$CMD" == "ls -1 \\$HOME/bench"* ]]; then
{block}    elif [[ "$CMD" == "flock \\$HOME/bench/.lock"* ]]; then
      cat
    elif [ "$1" = "bash" ] && [ "$2" = "-s" ]; then
      shift 3
      if [ "$#" -eq 1 ]; then
        echo "{mtime_expr} {size}"
      else
        cat > /dev/null
        exit 1
      fi
    fi
    ;;
esac
"""
    (fake / "ssh").write_text(script)
    (fake / "ssh").chmod(0o755)
    return fake


def add_fresh_branch_worktree(project: Path, tmpdir: Path,
                              dirname: str, dirty: bool) -> Path:
    """A worktree on a fresh branch cut from main, with no commits ahead.

    `dirname` is in the alphabet tree_name emits, so the bench dir a
    builder would make for it is named exactly `dirname`. `dirty` leaves
    uncommitted edits in the tree.
    """
    wt = tmpdir / dirname
    git(project, "worktree", "add", "--quiet", str(wt), "-b",
        dirname + "_branch", "main")
    if dirty:
        (wt / "WIP").write_text("mid-implementation\n")
    return wt.resolve()


def write_lane_final(state_root: Path, worktree: Path,
                     sha: str) -> None:
    """Plant the lane-final record so `lane-settled` reads the tree as
    settled at `sha`. A settled, clean tree is the shape where the
    fresh-branch signal is the only thing that keeps its bench dir."""
    h = hashlib.sha256(str(worktree).encode()).hexdigest()
    target = state_root / "lane-final" / h
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"sha={sha}\nlast_edit_at={time.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")


def run_prune(project: Path, fakebin: Path, *args: str,
              env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the real remote-prune against a hermetic environment."""
    env_file = project / "tools.env"
    env_file.write_text("")
    env = {
        "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
        "HOST_BUILDER": "fakehost",
        "TOOLS_ENV": str(env_file),
        "HOME": str(project.parent),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run([str(REMOTE_PRUNE), *args, str(project)],
                          capture_output=True, text=True, env=env)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def fail(failures: list, msg: str, detail: str = "") -> None:
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_in(label: str, needle: str, haystack: str,
              failures: list) -> None:
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_not_in(label: str, needle: str, haystack: str,
                  failures: list) -> None:
    if needle in haystack:
        fail(failures, label, f"{needle!r} should not appear in {haystack!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_partitions_bench(failures: list) -> None:
    """A bench of landed, unlanded and ghost dirs is reported by shape.

    Each shape gets its own block and size line. Pinning the numbers catches a
    sum that silently drops a candidate or counts one twice.
    """
    with tempfile.TemporaryDirectory() as t:
        project, _ = setup_project(Path(t))
        # idle_days=0 makes every unlanded tree qualify as idle.
        # The fake returns 99999 for `du -sb <dir>` and 4096 for the
        # incremental check; both are echoed back in the report's byte
        # line, so the assertions pin real numbers, not just labels.
        fakebin = make_fakebin(Path(t), idle_days=0, idle_bytes=4096,
                               landed_bytes=99999)
        r = run_prune(project, fakebin, env_extra={
            "REMOTE_PRUNE_IDLE_DAYS": "0",
        })

        if r.returncode != 0:
            fail(failures, "dry run exit code", f"got {r.returncode}, stderr={r.stderr!r}")
        out = r.stdout

        assert_in("dry run banner", "==> dry run: fakehost", out, failures)
        assert_in("landed block header", "landed, safe to remove", out, failures)
        assert_in("landed name", "wt_landed", out, failures)
        # The fake returns 99999 from `du -sb`. Pin the raw byte count so
        # a regression that zeroes the sum without changing the labels
        # still fails.
        assert_in("landed byte sum",
                  "reclaimable from landed: 99999", out, failures)

        assert_in("uncertain block header", "left alone, no local worktree", out, failures)
        assert_in("ghost name", "ghost_tree", out, failures)

        assert_in("idle block header", "idle >= 0d, incremental cache droppable", out, failures)
        assert_in("idle name", "wt_unlanded", out, failures)
        assert_in("idle byte sum",
                  "reclaimable from incremental: 4096", out, failures)

        # The dangerous word -- the only thing that tells the reader this
        # run did not delete anything.
        assert_in("no deletion in dry run", "dry run", out, failures)
        # The "would go" wording for the report is what they look at to
        # decide whether to re-run with --prune.
        assert_in("re-run hint",
                  "re-run with --prune", out, failures)


def test_prune_acquires_lock_and_names_targets(failures: list) -> None:
    """`--prune` acquires the same lock every `remote-task` run holds.

    The remote lock is the only thing that stops two agents on two
    machines from deleting each other's build. Pin that the script
    reaches for it, then names each dir it is about to remove, in the
    same flocked session.

    We can't introspect the heredoc that flew across ssh in the test
    environment without the fake recording it. The fake here records
    the heredoc verbatim, and the assertion is on its contents.
    """
    with tempfile.TemporaryDirectory() as t:
        project, _ = setup_project(Path(t))
        # A separate fake that captures the prune heredoc to a file we
        # can read back, plus the full ssh argv so we can pin the lock
        # command (the lock is in argv, the body of the heredoc runs
        # *under* it).
        fake = Path(t) / "fakebin"
        fake.mkdir()
        log_file = Path(t) / "prune-heredoc.txt"
        argv_file = Path(t) / "prune-argv.txt"
        script = f"""#!/bin/bash
echo "$*" > "{argv_file}"
HOST=$1; shift
CMD="$*"
case "$HOST" in
  fakehost)
    if [[ "$CMD" == "ls -1 \\$HOME/bench"* ]]; then
      echo "wt_landed"
      echo "ghost_tree"
    elif [[ "$CMD" == "flock \\$HOME/bench/.lock"* ]]; then
      cat > "{log_file}"
    elif [ "$1" = "bash" ] && [ "$2" = "-s" ]; then
      shift 3
      if [ "$#" -eq 1 ]; then
        # bench-info query: mtime now, size 9999.
        echo "$(date +%s) 9999"
      else
        cat > /dev/null
        echo "0"
      fi
    fi
    ;;
esac
"""
        (fake / "ssh").write_text(script)
        (fake / "ssh").chmod(0o755)

        r = run_prune(project, fake, "--prune")
        if r.returncode != 0:
            fail(failures, "prune exit code",
                 f"got {r.returncode}, stderr={r.stderr!r}")

        assert_in("lock acquired", "acquiring $HOME/bench/.lock", r.stdout, failures)

        if not log_file.exists():
            fail(failures, "prune heredoc never reached the fake ssh",
                 f"stdout={r.stdout!r}")
            return
        body = log_file.read_text()
        # Only the landed branch's tree should be in the rm list. Ghost
        # is left alone -- this is the dangerous half, and a regression
        # that lets "uncertain" through would silently delete an unknown
        # bench dir.
        assert_in("prune names landed tree", "wt_landed", body, failures)
        assert_not_in("prune does not name ghost",
                      "ghost_tree", body, failures)
        # The lock lives in the wrapping ssh argv, not the heredoc body
        # -- the heredoc is the script that runs under the lock. Pin
        # both halves so a regression that removes either one is caught.
        if argv_file.exists():
            argv = argv_file.read_text()
            assert_in("prune ssh argv is flocked",
                      "flock", argv, failures)


def test_dry_run_does_not_acquire_lock(failures: list) -> None:
    """A dry run must not call flock -- only `--prune` ever deletes files.

    A reader who runs the tool without --prune and sees the lock
    acquired in the log has no way to tell whether something was
    deleted. Pin the absence so the dry-run-vs-prune boundary is
    observable, not just implied by the absence of `rm -rf`.
    """
    with tempfile.TemporaryDirectory() as t:
        project, _ = setup_project(Path(t))
        # The fake ssh records every flock invocation to a file; reading
        # it back shows whether the dry run reached the lock.
        fake = Path(t) / "fakebin"
        fake.mkdir()
        flock_hits = Path(t) / "flock-hits.txt"
        flock_hits.write_text("")
        script = f"""#!/bin/bash
HOST=$1; shift
CMD="$*"
case "$HOST" in
  fakehost)
    if [[ "$CMD" == "ls -1 \\$HOME/bench"* ]]; then
      echo "wt_landed"
    elif [[ "$CMD" == "du -sb "* ]]; then
      echo "1"
    elif [[ "$CMD" == "flock \\$HOME/bench/.lock"* ]]; then
      echo "FLOCK_INVOKED" >> "{flock_hits}"
    elif [ "$1" = "bash" ] && [ "$2" = "-s" ]; then
      cat > /dev/null
      exit 1
    fi
    ;;
esac
"""
        (fake / "ssh").write_text(script)
        (fake / "ssh").chmod(0o755)

        r = run_prune(project, fake)
        if r.returncode != 0:
            fail(failures, "dry-run exit code",
                 f"got {r.returncode}, stderr={r.stderr!r}")
        body = flock_hits.read_text().strip()
        if body:
            fail(failures, "dry run acquired flock",
                 f"flock was invoked: {body!r}")
        assert_in("dry run banner", "==> dry run", r.stdout, failures)


def test_unreachable_host_reports_exit_3(failures: list) -> None:
    """An ssh failure must surface as exit 3, not exit 0 with empty bench.

    gate-verify reports exit 3 on its own line so it cannot be read as a
    pass. A tool that fails to measure and exits 0 is a worse bug than
    one that refuses to run -- the operator trusts a green run, and a
    wrong "nothing to do" is the dangerous lie the exit-3 convention
    exists to keep off the green list.
    """
    with tempfile.TemporaryDirectory() as t:
        project, _ = setup_project(Path(t))
        fakebin = make_fakebin(Path(t), bench_rc=7)
        r = run_prune(project, fakebin)
        if r.returncode != 3:
            fail(failures, "ssh failure exit code",
                 f"want 3, got {r.returncode}, stderr={r.stderr!r}")
        assert_in("ssh failure named", "cannot reach fakehost", r.stderr, failures)
        assert_in("ssh exit code named", "exit 7", r.stderr, failures)


def test_empty_bench_reports_nothing_to_do(failures: list) -> None:
    """A builder with no bench dirs exits 0 with a named "nothing" line."""
    with tempfile.TemporaryDirectory() as t:
        project, _ = setup_project(Path(t))
        # bench_rc=0 but bench_lines=[]
        fake = Path(t) / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(
            '#!/bin/bash\nHOST=$1; shift\n[[ "$1" == "ls -1 \\$HOME/bench"* ]]\n'
        )
        (fake / "ssh").chmod(0o755)
        r = run_prune(project, fake)
        if r.returncode != 0:
            fail(failures, "empty bench exit code",
                 f"got {r.returncode}, stderr={r.stderr!r}")
        assert_in("empty bench named",
                  "nothing to prune", r.stdout, failures)


def test_fresh_incremental_is_not_dropped(failures: list) -> None:
    """A worktree whose incremental dir was touched recently stays alone.

    A fresh mtime means a build is still running, and dropping the
    incremental cache underneath it is the race the lock is supposed to
    stop -- but the predicate itself must not propose it. The fake ssh
    returns exit 1 (no idle dir) for the incremental check, simulating
    the "touched today" answer.
    """
    with tempfile.TemporaryDirectory() as t:
        project, _ = setup_project(Path(t))
        # idle_bytes=4096 but with rc=1, the script won't pick it up.
        fake = Path(t) / "fakebin"
        fake.mkdir()
        script = """#!/bin/bash
HOST=$1; shift
CMD="$*"
case "$HOST" in
  fakehost)
    if [[ "$CMD" == "ls -1 \\$HOME/bench"* ]]; then
      echo "wt_unlanded"
    elif [[ "$CMD" == "du -sb "* ]]; then
      echo "1"
    elif [ "$1" = "bash" ] && [ "$2" = "-s" ]; then
      cat > /dev/null
      exit 1
    fi
    ;;
esac
"""
        (fake / "ssh").write_text(script)
        (fake / "ssh").chmod(0o755)

        r = run_prune(project, fake)
        if r.returncode != 0:
            fail(failures, "fresh-incremental exit code",
                 f"got {r.returncode}, stderr={r.stderr!r}")
        # The unlanded tree's incremental dir is fresh: nothing should
        # appear under the idle block. The landed/uncertain blocks may
        # be empty too -- wt_unlanded is the only dir -- so the report
        # should say so explicitly.
        assert_in("no idle block when nothing qualifies",
                  "nothing to prune", r.stdout, failures)
        assert_not_in("unlanded tree not under idle",
                      "idle >=", r.stdout, failures)


def test_trunk_checkout_is_not_landed(failures: list) -> None:
    """The trunk checkout answers `git cherry origin/main HEAD` trivially --
    every commit on main is on main -- so it reads as landed. Its bench dir is
    the warmest cache on the builder, and removing it on every run turns each
    trunk build cold. "Landed" means a branch whose work reached the trunk,
    which the trunk is not."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project, _ = setup_project(tmp)
        # The primary worktree's own bench dir, as a builder that has ever run
        # remote-task from the main checkout would carry.
        fake = make_fakebin(tmp, extra_bench=(project.name,))
        r = run_prune(project, fake)
        if r.returncode != 0:
            fail(failures, "trunk dry run exit", f"got {r.returncode}, {r.stderr!r}")
        landed = r.stdout.split("landed, safe to remove")[-1].split("==>")[0]
        if project.name in landed:
            fail(failures, "trunk listed as landed",
                 f"{project.name!r} is in the removable list: {landed!r}")
        assert_in("trunk named as held back", "trunk checkout", r.stdout, failures)


def test_hostile_bench_name_is_refused(failures: list) -> None:
    """Bench names are interpolated into remote command strings and this tool
    runs `rm -rf`. The listing comes from the builder's filesystem, so a
    directory nobody here created would otherwise decide what that string
    says. tree_name only ever emits [A-Za-z0-9._-]; anything else is refused
    rather than measured or deleted."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project, _ = setup_project(tmp)
        hostile = "evil; touch /tmp/pwned"
        fake = make_fakebin(tmp, extra_bench=(hostile,))
        r = run_prune(project, fake)
        if r.returncode != 0:
            fail(failures, "hostile name exit", f"got {r.returncode}, {r.stderr!r}")
        landed = r.stdout.split("landed, safe to remove")[-1].split("==>")[0] \
            if "landed, safe to remove" in r.stdout else ""
        if "evil" in landed:
            fail(failures, "hostile name reached the removable list", landed)
        assert_in("hostile name refused by name",
                  "not one this tooling produces", r.stdout, failures)



def test_fresh_branch_dirty_keeps_bench(failures: list) -> None:
    """The issue's bug: a fresh branch with uncommitted edits and no commits
    ahead of main reads "landed" from `git cherry`, and its bench dir went in
    the `landed` set and was removed whole. A dirty tree carries work, so the
    dir must stay under `--prune`.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project, _ = setup_project(tmp)
        add_fresh_branch_worktree(project, tmp, "wt_fresh", dirty=True)
        fake = make_bench_fake(tmp, ("wt_fresh",), "$(date +%s)")
        r = run_prune(project, fake, "--prune")
        if r.returncode != 0:
            fail(failures, "fresh-dirty exit", f"got {r.returncode} {r.stderr!r}")
        # It is carried, not landed.
        assert_in("fresh-dirty named carrying",
                  "carrying unlanded work", r.stdout, failures)
        assert_in("fresh-dirty name under carrying", "wt_fresh", r.stdout, failures)
        landed = r.stdout.split("landed, safe to remove")[-1].split("==>")[0]             if "landed, safe to remove" in r.stdout else ""
        assert_not_in("fresh-dirty not in landed list",
                      "wt_fresh", landed, failures)
        # --prune must not list it for rm either: the only rm list is the
        # landed one, and it is empty here.
        assert_not_in("fresh-dirty not rm'd",
                      "rm -rf $wt_fresh", r.stdout, failures)


def test_fresh_branch_clean_and_settled_keeps_bench(failures: list) -> None:
    """The other half of the bug with the dirty-tree signal removed: a clean,
    settled fresh branch still carries no commits, and the bench dir's mtime
    predates the branch. The fresh-branch signal is the only one left to keep
    it.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project, _ = setup_project(tmp)
        wt = add_fresh_branch_worktree(project, tmp, "wt_freshc", dirty=False)
        # Settle the tree so the lane-held and dirty signals are both false:
        # the fresh-branch signal is the only thing left to keep the dir.
        state = tmp / "state"
        state.mkdir()
        tip = git(wt, "rev-parse", "HEAD").stdout.strip()
        write_lane_final(state, wt, tip)
        # mtime pinned to the epoch: strictly older than the branch created
        # seconds ago, so the bench dir predates the branch.
        fake = make_bench_fake(tmp, ("wt_freshc",), "1")
        r = run_prune(project, fake, env_extra={"AGENT_STATE_DIR": str(state)})
        if r.returncode != 0:
            fail(failures, "fresh-clean exit", f"got {r.returncode} {r.stderr!r}")
        assert_in("fresh-clean named carrying",
                  "carrying unlanded work", r.stdout, failures)
        assert_in("fresh-clean name under carrying", "wt_freshc", r.stdout, failures)


def test_fresh_branch_dirty_and_new_bench_keeps_bench(failures: list) -> None:
    """Signal isolation: the bench dir's mtime postdates the branch (so the
    fresh-branch signal is false) and a live lane holds the tree (so the
    lane-held signal is true).
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project, _ = setup_project(tmp)
        wt = add_fresh_branch_worktree(project, tmp, "wt_livelane", dirty=True)
        state = tmp / "state"
        state.mkdir()
        # A live pid (the test's own) in the lane-active marker: lane-settled
        # reports the tree as held and exits 1.
        import hashlib
        h = hashlib.sha256(str(wt).encode()).hexdigest()
        act = state / "lane-active" / h
        act.parent.mkdir(parents=True, exist_ok=True)
        act.write_text(str(os.getpid()) + "\n")
        fake = make_bench_fake(tmp, ("wt_livelane",), "$(date +%s)")
        r = run_prune(project, fake, env_extra={"AGENT_STATE_DIR": str(state)})
        if r.returncode != 0:
            fail(failures, "live-lane exit", f"got {r.returncode} {r.stderr!r}")
        assert_in("live-lane named carrying",
                  "carrying unlanded work", r.stdout, failures)
        assert_in("live-lane name under carrying", "wt_livelane", r.stdout, failures)


def test_merged_clean_branch_still_lands(failures: list) -> None:
    """The counter-case that must not be over-corrected: a clean, settled
    branch whose commits are on main and whose bench dir's mtime postdates
    the branch. All three carry-work signals are false, so the dir still
    goes in the `landed` set.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project, _ = setup_project(tmp)
        wt = add_fresh_branch_worktree(project, tmp, "wt_merged", dirty=False)
        state = tmp / "state"
        state.mkdir()
        tip = git(wt, "rev-parse", "HEAD").stdout.strip()
        write_lane_final(state, wt, tip)
        # mtime now: newer than the branch's creation, so the fresh-branch
        # signal is false.
        fake = make_bench_fake(tmp, ("wt_merged",), "$(date +%s)")
        r = run_prune(project, fake, "--prune",
                      env_extra={"AGENT_STATE_DIR": str(state)})
        if r.returncode != 0:
            fail(failures, "merged exit", f"got {r.returncode} {r.stderr!r}")
        assert_in("merged named landed", "landed, safe to remove", r.stdout, failures)
        landed = r.stdout.split("landed, safe to remove")[-1].split("==>")[0]
        assert_in("merged name under landed", "wt_merged", landed, failures)
        if "carrying unlanded work" in r.stdout:
            carrying = r.stdout.split("carrying unlanded work")[-1].split("==>")[0]
            assert_not_in("merged not under carrying",
                          "wt_merged", carrying, failures)


def main() -> int:
    failures: list = []
    test_dry_run_partitions_bench(failures)
    test_prune_acquires_lock_and_names_targets(failures)
    test_dry_run_does_not_acquire_lock(failures)
    test_unreachable_host_reports_exit_3(failures)
    test_empty_bench_reports_nothing_to_do(failures)
    test_fresh_incremental_is_not_dropped(failures)
    test_trunk_checkout_is_not_landed(failures)
    test_hostile_bench_name_is_refused(failures)
    test_fresh_branch_dirty_keeps_bench(failures)
    test_fresh_branch_clean_and_settled_keeps_bench(failures)
    test_fresh_branch_dirty_and_new_bench_keeps_bench(failures)
    test_merged_clean_branch_still_lands(failures)

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())