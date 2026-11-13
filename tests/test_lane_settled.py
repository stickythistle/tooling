#!/usr/bin/env python3
"""Pin the lane-settled predicate's three-way answer, and pin the resume
prompt hermes-implement hands a timed-out session.

The predicate has three states and two inert ones, and the difference between
them is what a caller gating a tree relies on. The bug the issue opens with
was a lane amending over a person twelve seconds after the resume loop had
decided the lane was done. The same answer the predicate gives -- "settled",
"still running", or "drifted" -- has to distinguish a tree the lane never
touched from one it just left, and a tree it just left from one somebody else
has been editing since.

The second half drives bin/hermes-implement with a fake hermes that records
the prompts of each attempt. A timeout ends attempt 1 with HEAD still at BASE
and no marker on the branch saying so; the prompt itself must do that work --
and must say a new commit is required, restate the issue and --extra text, and
not include the false-on-timeout compile-claim that used to live there.

Run directly: `python3 tests/test_lane_settled.py`. No test runner -- stdlib
subprocess + a fresh git repo per scenario is enough.
"""
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from roles_fixture import roles_file
from skill_install_fixture import install as install_skills
LANE_SETTLED = REPO_ROOT / "bin" / "lane-settled"
HERMES_IMPLEMENT = REPO_ROOT / "bin" / "hermes-implement"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """A git invocation that fails the test on error and returns the
    CompletedProcess for inspection. `cwd` is the git working tree."""
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True, check=True)


def make_worktree(tmpdir: Path) -> Path:
    """Stand up a throwaway git repo with one commit. The worktree path
    lane-settled resolves is what the predicate reads, so the repo's
    `rev-parse --show-toplevel` is the value the test pins against."""
    repo = tmpdir / "wt"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet")
    # user/email must be set; an unset identity leaves commits failing in CI
    # for a reason nothing in the test reports.
    for k, v in [("user.email", "test@test"), ("user.name", "test"),
                 ("init.defaultBranch", "main")]:
        git(repo, "config", k, v)
    (repo / "README").write_text("seed\n")
    git(repo, "add", "README")
    git(repo, "commit", "--quiet", "-m", "seed")
    return repo


def write_record(state_root: Path, worktree: Path,
                 sha: str, last_edit: str) -> None:
    """Plant the lane-final record lane-settled reads. Mirrors what
    hermes-implement writes: a sha256 of the worktree path as the filename,
    because a path contains separators and cannot be one."""
    import hashlib
    target = state_root / "lane-final" / hashlib.sha256(
        str(worktree).encode()).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"sha={sha}\nlast_edit_at={last_edit}\nrecorded_at=2026-09-12T00:00:00\n")


def write_active(state_root: Path, worktree: Path, pid: str) -> None:
    """Plant the lane-active marker with `pid` as the live (or dead)
    process id. Mirrors what hermes-implement writes at start."""
    import hashlib
    target = state_root / "lane-active" / hashlib.sha256(
        str(worktree).encode()).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pid + "\n")


def run_predicate(worktree: Path, state_root: Path) -> subprocess.CompletedProcess:
    """Invoke bin/lane-settled with AGENT_STATE_DIR pointed at a scratch
    directory so real lane records cannot leak into the test."""
    env = {**os.environ, "AGENT_STATE_DIR": str(state_root)}
    return subprocess.run(
        [str(LANE_SETTLED), str(worktree)],
        capture_output=True, text=True, env=env)


def assert_eq(label: str, want, got, failures: list) -> None:
    if want != got:
        failures.append(f"{label}: want {want!r}, got {got!r}")


def assert_in(label: str, needle: str, haystack: str, failures: list) -> None:
    if needle not in haystack:
        failures.append(f"{label}: {needle!r} not in {haystack!r}")


def assert_not_in(label: str, needle: str, haystack: str, failures: list) -> None:
    if needle in haystack:
        failures.append(f"{label}: {needle!r} unexpectedly in {haystack!r}")


# ----- hermes-implement resume-prompt cases -------------------------------
#
# The rest of this file is lane-settled. These helpers and the function
# `hermes_implement_resume_cases` exercise bin/hermes-implement instead, but
# live here because (a) the lane-settled-tests gate already covers both
# `bin/hermes-implement` and `tests/test_lane_settled.py` in its `when`, and
# (b) hermes-implement's loop condition `HEAD != BASE` is the same predicate
# lane-settled protects -- a wrong resume prompt is the same family of bug
# the rest of this file pins against. The test is run by the same gate, so
# the prompt's "tests for hermes-implement live in test_lane_settled.py"
# affordance is honest: editing them runs the lane-settled tests gate.


def make_branch_worktree(tmpdir: Path, name: str) -> Path:
    """Stand up a throwaway git repo with one commit on a branch named
    `retry-new-commit`. The script under test runs `git rev-parse
    --abbrev-ref HEAD` and uses that as $BRANCH -- the resume prompt
    interpolates it -- so the test pins against the same name the
    production branch carries."""
    repo = tmpdir / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("seed\n")
    git(repo, "add", "README")
    git(repo, "commit", "--quiet", "-m", "seed")
    git(repo, "checkout", "--quiet", "-b", "retry-new-commit")
    return repo


def write_executable(path: Path, body: str) -> None:
    """Drop a shell script and chmod 0755 it. The script is invoked as
    if it were the hermes binary, so it must be on PATH-style resolution:
    the test sets HERMES_PYTHON to its absolute path."""
    path.write_text(body)
    path.chmod(path.stat().st_mode
               | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_fake_hermes(bin_dir: Path, *, commit_on_first: bool) -> Path:
    """Build a fake hermes that records every prompt it is handed and,
    depending on `commit_on_first`, either commits to the worktree on
    its first call or exits 124 (a `timeout(1)` death code) without
    touching the tree. The path of the worktree comes in via
    $FAKE_HERMES_WT; the prompt log goes to $FAKE_HERMES_LOG.

    hermes-implement now hands the prompt through `--query-file PATH`
    (the helper in lib/common.sh), so the fake reads the file
    `--query-file` points at and records its content. Reading `-z`
    here would silently record an empty string -- the test would
    fail for the right reason in the wrong place."""
    if commit_on_first:
        body = r"""#!/usr/bin/env bash
# Fake hermes used by tests/test_lane_settled.py.
# On call #1 it commits a marker file inside the worktree AND writes
# $TMPDIR/HANDOVER.md so the lane's two-part "finished" check
# (clean tree + non-empty HANDOVER.md) both turn true. Without the
# handover the lane would retry the same successful first attempt,
# which is the bug the rewrite exercises.
set -eu
log="$FAKE_HERMES_LOG"
wt="$FAKE_HERMES_WT"
work="${TMPDIR:-}"
prompt=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --query-file) prompt=$(cat "$2"); shift 2 ;;
    *)  shift ;;
  esac
done
echo "===call===" >>"$log"
echo "$prompt"        >>"$log"
echo "===end==="       >>"$log"
if ! [[ -f "$log.first_done" ]]; then
  touch "$log.first_done"
  if [[ -n "$wt" ]] && [[ -d "$wt" ]]; then
    f="$wt/io_marker.txt"
    echo "committed by fake hermes" >"$f"
    git -C "$wt" add io_marker.txt 2>/dev/null || true
    git -C "$wt" -c user.email=fake@x -c user.name=fake \
        commit -m "fake commit" 2>/dev/null || true
  fi
  if [[ -n "$work" ]] && [[ -d "$work" ]]; then
    echo "stub handover" >"$work/HANDOVER.md"
  fi
fi
exit 0
"""
    else:
        body = r"""#!/usr/bin/env bash
# Fake hermes used by tests/test_lane_settled.py.
# On call #1 it exits 124 without committing; on later calls it
# records its prompt to the log so the test can read what the
# resume loop said. Exit 124 is what `timeout(1)` returns when it
# kills the child -- the value the lane records when a timed-out
# attempt ends without a commit. The prompt is read from the file
# `--query-file` points at, not from argv (`-z`), because hermes-
# implement now goes through the helper in lib/common.sh.
set -eu
log="$FAKE_HERMES_LOG"
prompt=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --query-file) prompt=$(cat "$2"); shift 2 ;;
    *)  shift ;;
  esac
done
if ! [[ -f "$log.first_done" ]]; then
  touch "$log.first_done"
  echo "===call===" >>"$log"
  echo "$prompt"       >>"$log"
  echo "===end==="      >>"$log"
  exit 124
fi
echo "===call===" >>"$log"
echo "$prompt"        >>"$log"
echo "===end==="       >>"$log"
exit 124
"""
    p = bin_dir / "fake-hermes"
    write_executable(p, body)
    return p


def make_fake_gh(bin_dir: Path) -> Path:
    """Drop a fake `gh issue view` that prints a stub. hermes-implement
    runs `gh issue view $ISSUE` and writes the result to $WORK/issue.txt;
    without a stub `gh` it would either fail or reach out to GitHub, both
    of which are wrong for a test that runs offline."""
    body = r"""#!/usr/bin/env bash
# Fake gh used by tests/test_lane_settled.py.
# `gh api user` answers who the call would act as, which `gh_as` checks
# against IMPLEMENTER_LOGINS.
if [[ "$1" == "api" && "$2" == "user" ]]; then
    echo "test-implementer"
    exit 0
fi
if [[ "$1" == "repo" && "$2" == "view" ]]; then
    echo "${STUB_REPO-example-org/tooling}"
    exit 0
fi
cat <<'ISSUE'
title: stub
number: 0
state: OPEN
body: stub issue used by the hermes-implement resume-prompt test
ISSUE
"""
    p = bin_dir / "gh"
    write_executable(p, body)
    return p


def run_hermes_implement(worktree: Path, extra: str,
                         log_path: Path, bin_dir: Path) -> subprocess.CompletedProcess:
    """Invoke bin/hermes-implement against `worktree`. The fake
    hermes lives at `bin_dir/fake-hermes`; its log is `log_path`. The
    The full environment is built so the script never touches the host's
    ~/.hermes or ~/.local/state directories."""
    home = log_path.parent / "home"
    install_skills(home)
    env = {
        # The host's HOME still has to be a real directory -- bash
        # uses it for `~` expansion in some code paths -- but every
        # state file the script reads or creates is redirected below.
        "HOME": "/tmp",
        # Prepend bin_dir so the fake `gh` and `fake-hermes` resolve
        # before the host's real `gh`. hermes-implement only invokes
        # `gh`, `lane-settled` is not on its PATH, and the hermes
        # binary itself is named by HERMES_PYTHON.
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        # Override the hermes binary.
        "HERMES_PYTHON": str(bin_dir / "fake-hermes"),
        # State + evidence: the script defaults these to $HOME/.local
        # and $HOME/.hermes respectively, so point them at a tmpdir.
        "AGENT_STATE_DIR": str(log_path.parent / "state"),
        "HERMES_HOME": str(log_path.parent / "home"),
        # Wiring for the fake hermes itself.
        "FAKE_HERMES_LOG": str(log_path),
        "FAKE_HERMES_WT": str(worktree),
        # Keep the run bounded: a real hermes times out after 90 minutes;
        # the fake exits 124 immediately, so a short ceiling here is only
        # a safety net.
        "HERMES_IMPLEMENT_TIMEOUT": "5",
        "HERMES_IMPLEMENT_ATTEMPTS": "3",
        "ROLES_CONFIG": str(roles_file(log_path.parent / "roles.toml",
                                        logins=("test-implementer",))),
    }
    cmd = [
        str(HERMES_IMPLEMENT),
        "--issue", "0",
        "--worktree", str(worktree),
        # --verify is a command the script interpolates into the
        # prompt; it is not invoked unless the lane commits and reaches
        # the post-loop stray-write check, but pass it so the prompt
        # looks like a real one.
        "--verify", "true",
        "--extra", extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def parse_prompts(log_text: str) -> list[str]:
    """Pull each recorded prompt out of the fake-hermes log. The fake
    writes one prompt per call, fenced by the marker pair; split on
    `===call===` and trim up to the matching `===end===`."""
    out_list = []
    for chunk in log_text.split(marker := "===call===")[1:]:
        end = chunk.find("===end===")
        if end == -1:
            continue
        out_list.append(chunk[:end].rstrip("\n"))
    return out_list


def hermes_implement_resume_cases(failures: list) -> None:
    """Drive bin/hermes-implement with a fake hermes and pin the
    resume prompt: the base short sha, the per-iteration state
    summary (committed + uncommitted + HANDOVER.md presence), and
    the --extra text the operator supplied. The second case makes
    the opposite point: a finished attempt records exactly one
    prompt."""

    def _timeout_then_resume(tmp: Path) -> None:
        wt = make_branch_worktree(tmp, "wt-timeout")
        bin_dir = tmp / "bin-timeout"
        bin_dir.mkdir()
        log_path = tmp / "fake-timeout.log"
        make_fake_hermes(bin_dir, commit_on_first=False)
        make_fake_gh(bin_dir)

        base_short = git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
        extra_text = "PIN-EXTRA-12345: pay attention to this line"

        r = run_hermes_implement(wt, extra_text, log_path, bin_dir)
        # The script's exit code is unreliable -- its EXIT trap always
        # runs and exits 0 -- so the assertion reads what the fake
        # recorded, not what hermes-implement itself returned.
        if not log_path.exists():
            failures.append(
                f"timeout case: fake-hermes was never invoked. "
                f"stderr={r.stderr!r} stdout={r.stdout!r}")
            return
        prompts = parse_prompts(log_path.read_text())
        # One prompt for attempt 1 plus at least one resume.
        if len(prompts) < 2:
            failures.append(
                f"timeout case: expected at least 2 prompts "
                f"(attempt 1 + 1 resume), got {len(prompts)}: "
                f"{log_path.read_text()!r}")
            return
        resume = prompts[1]

        # Resume (prompts[1]) must tell the model what the previous
        # attempt left: the base short sha, the empty 'Committed
        # since base' section, the 'Uncommitted files' header, and
        # the missing HANDOVER.md marker.
        assert_in("timeout case: resume names BASE short sha",
                  base_short, resume, failures)
        assert_in("timeout case: resume says nothing committed",
                  "Committed since base:", resume, failures)
        assert_in("timeout case: committed section is empty",
                  "(none)", resume, failures)
        assert_in("timeout case: resume names uncommitted files header",
                  "Uncommitted files:", resume, failures)
        assert_in("timeout case: resume marks HANDOVER.md missing",
                  "HANDOVER.md: missing", resume, failures)
        assert_in("timeout case: resume carries the --extra text",
                  extra_text, resume, failures)
        assert_in("timeout case: resume names the issue",
                  "issue is #0", resume, failures)
        # The compile-claim the old prose carried was false on a
        # timeout -- the rewrite drops it.
        assert_not_in("timeout case: resume omits the compile-claim",
                      "code that did not compile", resume, failures)
        # The prompt must describe the work and forbid discarding it;
        # rationale is in test_hermes_implement.py's central pin.
        assert_in("timeout case: resume forbids discarding the work",
                  "Never discard, stash, or reset work", resume, failures)
        assert_not_in("timeout case: resume does not frame a clean tree as the deliverable",
                      "deliverable is a clean tree", resume, failures)

    def _first_call_commits(tmp: Path) -> None:
        wt = make_branch_worktree(tmp, "wt-commit")
        bin_dir = tmp / "bin-commit"
        bin_dir.mkdir()
        log_path = tmp / "fake-commit.log"
        make_fake_hermes(bin_dir, commit_on_first=True)
        make_fake_gh(bin_dir)

        extra_text = "PIN-EXTRA-67890: this branch already has a commit"

        r = run_hermes_implement(wt, extra_text, log_path, bin_dir)
        if not log_path.exists():
            failures.append(
                f"commit case: fake-hermes was never invoked. "
                f"stderr={r.stderr!r} stdout={r.stdout!r}")
            return
        prompts = parse_prompts(log_path.read_text())

        # First call committed; the loop must break on HEAD != BASE
        # without running attempt 2.
        assert_eq("commit case: number of recorded prompts",
                  1, len(prompts), failures)
        if prompts:
            first = prompts[0]
            # Sanity: the first prompt is the operator's normal one --
            # it does NOT need to mention BASE_SHORT (the fix is to
            # REMAINING, not to PROMPT). The check that matters here
            # is "no second call happened".
            assert_in("commit case: first prompt names the issue file",
                      "issue.txt", first, failures)

    base = Path(os.environ.get("TMPDIR", tempfile.gettempdir()))
    with tempfile.TemporaryDirectory(dir=base) as td:
        tmp = Path(td)
        _timeout_then_resume(tmp)
        _first_call_commits(tmp)


def main() -> int:
    failures: list = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wt = make_worktree(tmp / "real")
        # AGENT_STATE_DIR is what the predicate reads, so a directory under
        # `tmp` stands in for it.
        state = tmp / "agent-tools"

        # 1. No record, no marker. Older tree, lane finished before this check
        # existed. Must be inert: exit 0, with a message that names why.
        r = run_predicate(wt, state)
        assert_eq("no record exit code", 0, r.returncode, failures)
        assert_in("no record message",
                  "no final record", r.stderr, failures)

        # 2. Record present, no marker, current tip matches. The happy path:
        # the lane finished and the tree has not moved since.
        head = git(wt, "rev-parse", "HEAD").stdout.strip()
        write_record(state, wt, head, "2026-09-12T00:00:00")
        r = run_predicate(wt, state)
        assert_eq("settled exit code", 0, r.returncode, failures)
        assert_in("settled message", "settled", r.stdout, failures)

        # 3. Record present, current tip differs. Drift: the lane finished but
        # someone (a person rebasing, an autopilot pushing) has since moved
        # the tip. Must surface the recorded and current shas on stderr so a
        # caller has the information to decide what to do.
        (wt / "NEW").write_text("drifted\n")
        git(wt, "add", "NEW")
        git(wt, "commit", "--quiet", "-m", "drift")
        r = run_predicate(wt, state)
        assert_eq("drift exit code", 3, r.returncode, failures)
        assert_in("drift names recorded sha", head, r.stderr, failures)
        assert_in("drift names current sha",
                  git(wt, "rev-parse", "HEAD").stdout.strip(),
                  r.stderr, failures)
        assert_in("drift message", "drifted", r.stderr, failures)

        # 4. Live marker (our own pid). kill -0 succeeds, so the predicate must
        # report the lane is still running. Restoring the tip keeps this case
        # about the marker alone, not drift.
        git(wt, "reset", "--hard", head)
        write_active(state, wt, str(os.getpid()))
        r = run_predicate(wt, state)
        assert_eq("active exit code", 1, r.returncode, failures)
        assert_in("active message", "still running", r.stderr, failures)
        # The pid must be in the message -- "the lane is running" without the
        # pid leaves a reader to guess which one.
        assert_in("active names pid", str(os.getpid()), r.stderr, failures)
        write_active(state, wt, "")

        # 5. Stale marker (a pid that almost certainly isn't alive). A crash
        # without cleanup is a real failure mode; reporting "settled" in that
        # case is the dangerous wrong answer. Must fail (exit 1) with a
        # message that distinguishes "stale marker" from "lane still running"
        # -- one means clear the file, the other means wait.
        write_active(state, wt, "999999999")
        r = run_predicate(wt, state)
        assert_eq("stale exit code", 1, r.returncode, failures)
        assert_in("stale message", "stale", r.stderr, failures)

        # 6. Path that is not a git worktree. The predicate must say so on
        # stderr (a silent exit would mask the wrong invocation).
        bogus = tmp / "not-a-repo"
        bogus.mkdir()
        r = run_predicate(bogus, state)
        # exit 2 (usage / input problem), with the path named on stderr.
        assert_eq("not-a-worktree exit code", 2, r.returncode, failures)
        assert_in("not-a-worktree message",
                  "not inside a git worktree", r.stderr, failures)

        # 7. Corrupted record. A file present but unreadable sha must not
        # produce a misleading "settled". Pin that the predicate refuses --
        # silently reading sha=<empty> and comparing it against HEAD would
        # be the wrong answer.
        import hashlib
        rec = state / "lane-final" / hashlib.sha256(
            str(wt).encode()).hexdigest()
        rec.write_text("not a record at all\n")
        r = run_predicate(wt, state)
        # Either the missing-sha exit or a non-settled exit; the contract is
        # "never silently reports settled on a corrupt record".
        if r.returncode == 0 and "settled" in r.stdout:
            failures.append(
                "corrupt record: predicate reported settled without a sha")
        # Cleanup so the test does not leave a stale-active behind in any
        # future reuse of this tmpdir (the TemporaryDirectory is removed
        # anyway, but the assertion is on the contract, not on cleanup).
        (state / "lane-active" / hashlib.sha256(
            str(wt).encode()).hexdigest()).unlink(missing_ok=True)

        # 7. A lane that is running right now: marker with a live pid, and no
        # final record, because the record is only written when the lane
        # exits. This is the state every live lane is in, and answering
        # "settled" for it makes the predicate useless for the one question
        # it exists to answer.
        state2 = tmp / "agent-tools-live"
        live = subprocess.Popen(["sleep", "60"])
        try:
            write_active(state2, wt, str(live.pid))
            r = run_predicate(wt, state2)
            assert_eq("live lane, no record: exit code", 1, r.returncode, failures)
            assert_in("live lane, no record: message",
                      "still running", r.stderr, failures)
        finally:
            live.terminate()
            live.wait()

        # 8. The same marker once that process is gone, still with no record:
        # a lane that died before its trap ran. There is no tip to compare
        # against, so the caller is owed the fact rather than a verdict.
        r = run_predicate(wt, state2)
        assert_eq("dead lane, no record: exit code", 1, r.returncode, failures)
        assert_in("dead lane, no record: message",
                  "without recording a final", r.stderr, failures)

        # 9. No marker and no record at all is the inert case, and must stay
        # distinguishable from both of the above.
        r = run_predicate(wt, tmp / "agent-tools-empty")
        assert_eq("no marker, no record: exit code", 0, r.returncode, failures)

        # 10. The record is found by hashing a path, so both halves have to
        # hash the same string. A caller naming the tree with a trailing slash
        # or through `..` must still land on the record the lane wrote, or the
        # predicate silently reports "settled" for a tree it has no record of.
        state3 = tmp / "agent-tools-paths"
        head = git(wt, "rev-parse", "HEAD").stdout.strip()
        write_record(state3, wt, head, "2026-09-12T00:00:00")
        for spelling in (f"{wt}/", f"{wt}/../{wt.name}"):
            r = run_predicate(Path(spelling), state3)
            assert_eq(f"non-canonical path {spelling!r}: exit code",
                      0, r.returncode, failures)
            assert_in(f"non-canonical path {spelling!r}: found the record",
                      "settled at", r.stdout, failures)

    # hermes-implement resume-prompt cases. Run inside its own tmpdir so
    # the fake `gh`, fake hermes, and the worktree they touch cannot leak
    # into each other or into the lane-settled scenarios.
    hermes_implement_resume_cases(failures)

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
