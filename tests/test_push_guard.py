#!/usr/bin/env python3
"""Pin bin/push-guard: a push that deletes files main added after the branch's
last push is refused, and ordinary pushes are not.

A bare repository in a temp dir stands in for the remote; no network.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH_GUARD = REPO_ROOT / "bin" / "push-guard"
HOOK = REPO_ROOT / "hooks" / "pre-push"

IDENTITY = {"GIT_AUTHOR_NAME": "push-guard-test",
            "GIT_AUTHOR_EMAIL": "push-guard-test@example.com",
            "GIT_COMMITTER_NAME": "push-guard-test",
            "GIT_COMMITTER_EMAIL": "push-guard-test@example.com"}


def git(cwd, *args, env=None, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, env=env,
                          check=check)


def env_with_identity(extra=None):
    e = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), **IDENTITY}
    if extra:
        e.update(extra)
    return e


def init_repo(parent):
    """Make a fresh non-bare repo with one base commit. Returns the repo
    path."""
    repo = parent / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def head_of(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def setup_remote(repo):
    """Create a bare remote, wire it up, push main, and populate
    refs/remotes/origin/main. Returns the bare repo path."""
    remote = repo.parent / "remote.git"
    if remote.exists():
        shutil.rmtree(remote)
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main:main")
    git(repo, "update-ref",
        "refs/remotes/origin/main",
        head_of(repo))
    return remote


def run_guard(stdin_text, cwd=None, env_extra=None):
    """Run push-guard in `cwd` with one pre-push stdin line."""
    env = env_with_identity(env_extra)
    return subprocess.run([str(PUSH_GUARD), "origin"],
                         input=stdin_text, capture_output=True,
                         text=True, env=env,
                         cwd=str(cwd) if cwd else None)


def push_line(local_ref, local_sha, remote_ref, remote_sha):
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_eq(label, want, got, failures):
    if want != got:
        fail(failures, label, f"want {want!r}, got {got!r}")


def assert_not_eq(label, unwanted, got, failures):
    if unwanted == got:
        fail(failures, label, f"want anything but {unwanted!r}, got {got!r}")


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_not_in(label, needle, haystack, failures):
    if needle in haystack:
        fail(failures, label, f"{needle!r} should not be in {haystack!r}")


def assert_rc(label, want, r, failures):
    if r.returncode != want:
        fail(failures, label,
             f"got {r.returncode}, stderr={r.stderr!r} stdout={r.stdout!r}")


def printed_rebase_command(stderr):
    """The rebase command the refusal prints, verbatim.

    The refusal puts it in parentheses: "(git fetch origin && git rebase
    origin/main)". Taking the text out of the message, rather than
    rebuilding it here, is the point: a test that ran its own command
    would pass even if the advice named the wrong ref.
    """
    m = re.search(r"\(git fetch [^)]*\)", stderr)
    return m.group(0)[1:-1] if m else ""


def run_printed_command(repo, command):
    """Run the command the refusal printed, verbatim, in the repo."""
    return subprocess.run(["bash", "-c", command],
                          capture_output=True, text=True,
                          env=env_with_identity(), cwd=str(repo))


# ---------------------------------------------------------------------------
# Helpers used by the cases to build the exact scenario the issue describes.
# ---------------------------------------------------------------------------


def scenario_with_branch_and_lost_main_files(tmp):
    """Build the scenario case 1 uses: branch pushed at P, main gains
    x.txt and y.txt (and a bit more history), branch squashed by
    resetting onto the new main while the index still holds P's tree.
    Returns (repo, p_sha, bad_sha)."""
    repo = init_repo(tmp)
    setup_remote(repo)

    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a")
    git(repo, "push", "-q", "origin", "main:main")
    git(repo, "update-ref",
        "refs/remotes/origin/main",
        head_of(repo))

    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "feat.txt").write_text("feat\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat on top")
    p_sha = head_of(repo)
    # Use --set-upstream so refs/remotes/origin/feat gets populated for
    # later push tests (git rev-parse --verify can find the local ref).
    git(repo, "push", "-q", "-u", "origin", "feat")
    git(repo, "update-ref",
        "refs/remotes/origin/feat",
        p_sha)

    git(repo, "checkout", "-q", "main")
    (repo / "x.txt").write_text("x\n")
    (repo / "y.txt").write_text("y\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main moves")
    git(repo, "push", "-q", "origin", "main:main")
    git(repo, "update-ref",
        "refs/remotes/origin/main",
        head_of(repo))

    git(repo, "checkout", "-q", "feat")
    git(repo, "reset", "-q", "--soft", "origin/main")
    git(repo, "commit", "-q", "-m", "bad squash")
    bad_sha = head_of(repo)

    return repo, p_sha, bad_sha


def scenario_with_branch_rebased_onto_main_tip(tmp):
    """Build the scenario the base-regression refusal needs: main gains a
    commit, the branch is rebased onto main's tip and pushed there, so the
    branch's previous push forked at main's tip.

    Returns (repo, p_sha, main_tip)."""
    repo = init_repo(tmp)
    setup_remote(repo)

    (repo / "m.txt").write_text("m\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main moves")
    main_tip = head_of(repo)
    git(repo, "push", "-q", "origin", "main:main")
    git(repo, "update-ref",
        "refs/remotes/origin/main",
        main_tip)

    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "feat.txt").write_text("feat\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat on top")
    # Rebasing onto main's tip is what puts the branch's fork point at
    # main's tip; a branch forked earlier would not show a regression.
    git(repo, "rebase", "--quiet", "origin/main")
    p_sha = head_of(repo)
    git(repo, "push", "-q", "origin", "feat")
    git(repo, "update-ref",
        "refs/remotes/origin/feat",
        p_sha)

    return repo, p_sha, main_tip


def scenario_with_regressed_base(tmp):
    """Build the first stale-base shape: the branch was pushed on main's tip, and
    the rebuilt commit sits on main's *previous* commit.

    `git reset --soft origin/main` followed by `git commit --amend`
    rewrites main's tip commit instead of adding one, so the result is
    parented on main's previous commit and carries a copy of main's
    latest change on top of the PR's own. Nothing is deleted, so the
    gained/deleted check stays silent on it.

    Returns (repo, p_sha, bad_sha, main_tip)."""
    repo, p_sha, main_tip = scenario_with_branch_rebased_onto_main_tip(tmp)

    git(repo, "checkout", "-q", "feat")
    # Reset onto main's previous commit, keeping the branch's work in the
    # index: the rebuilt commit carries main's latest change plus the
    # PR's own, and its parent is main's previous commit.
    git(repo, "reset", "--quiet", "--soft", f"{main_tip}~1")
    git(repo, "commit", "-q", "-m", "rebuilt on older main")
    bad_sha = head_of(repo)

    return repo, p_sha, bad_sha, main_tip


def scenario_with_duplicated_patch(tmp):
    """Build the second stale-base shape: the branch carries a copy of a
    commit main already has -- same patch, new sha, which is what the
    identity verifier's re-author leaves behind.

    The copy is made with `git commit-tree` under a different author and
    date, so it is a distinct commit with the same patch rather than the
    same commit reachable twice.

    Returns (repo, dup_sha, twin_sha): dup_sha is the copy the branch
    carries, twin_sha is main's original."""
    repo = init_repo(tmp)
    setup_remote(repo)

    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main work")
    main_sha = head_of(repo)
    git(repo, "push", "-q", "origin", "main:main")
    git(repo, "update-ref",
        "refs/remotes/origin/main",
        main_sha)

    # A re-authored copy of main's tip: same tree and same parent, a
    # different author and date, so a different sha.
    dup_tree = git(repo, "rev-parse", f"{main_sha}^{{tree}}").stdout.strip()
    dup_parent = git(repo, "rev-parse", f"{main_sha}^").stdout.strip()
    dup_sha = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", dup_tree, "-p", dup_parent,
         "-m", "main work"],
        capture_output=True, text=True, check=True,
        env={**env_with_identity(),
             "GIT_AUTHOR_NAME": "someone-else",
             "GIT_AUTHOR_EMAIL": "someone-else@example.com",
             "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
             "GIT_COMMITTER_NAME": "someone-else",
             "GIT_COMMITTER_EMAIL": "someone-else@example.com",
             "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}).stdout.strip()

    # The branch rebases onto the copy, so the copy lands in the pushed
    # range under its new sha.
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "c.txt").write_text("c\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "branch work")
    git(repo, "rebase", "--quiet", "--onto", dup_sha, main_sha, "feat")

    return repo, dup_sha, main_sha


def scenario_with_stale_local_main(tmp):
    """Build the regressed-base scenario, and then move the *local* main
    branch back so it lags the remote.

    This is the lane worktree shape: the clone's local branch predates a
    history rewrite on the remote, or was never updated at all. The
    guard judges against refs/remotes/origin/main, so advice that named
    the local branch would send the lane onto old history -- the very
    thing the base-regression check refuses.

    Returns (repo, p_sha, bad_sha, main_tip)."""
    repo, p_sha, bad_sha, main_tip = scenario_with_regressed_base(tmp)

    # Move the local branch back to main's previous commit, leaving
    # origin/main at main's tip.
    git(repo, "checkout", "-q", "main")
    git(repo, "reset", "--quiet", "--hard", f"{main_tip}~1")
    git(repo, "checkout", "-q", "feat")

    return repo, p_sha, bad_sha, main_tip


def case_issue68_reverted_files_refused(failures):
    """Case 1 of the issue: a squash that loses files main added since
    the branch's previous push must be refused, with the lost files named."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("issue68 refused", 1, r, failures)
        assert_in("issue68 names x.txt", "x.txt", r.stderr, failures)
        assert_in("issue68 names y.txt", "y.txt", r.stderr, failures)
        assert_in("issue68 names feat ref", "feat", r.stderr, failures)
        assert_in("issue68 explains ack",
                  "PUSH_GUARD_ACK=1",
                  r.stderr, failures)


def case_ack_overrides_refusal(failures):
    """Case 2: same push as case 1 with PUSH_GUARD_ACK=1 passes, but the
    same files are still named so the override is conscious."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo, env_extra={"PUSH_GUARD_ACK": "1"})

        assert_rc("ack exit code", 0, r, failures)
        assert_in("ack still names x.txt", "x.txt", r.stderr, failures)
        assert_in("ack acknowledges",
                  "acknowledging",
                  r.stderr, failures)


def case_correct_rebase_keeps_gained_files(failures):
    """Case 3: rebasing onto the new main while keeping its newly added
    files means the push deletes nothing main added, and so it passes."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "a")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        git(repo, "checkout", "-q", "main")
        (repo / "x.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # The correct rebase: rebase feat onto the new main; the
        # resulting tip still has x.txt.
        git(repo, "checkout", "-q", "feat")
        git(repo, "rebase", "--quiet", "origin/main")
        rebased_sha = head_of(repo)

        line = push_line("refs/heads/feat", rebased_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("rebase exit", 0, r, failures)
        # x.txt is what we're protecting; the message should not name it.
        assert_not_in("rebase does not warn", "x.txt", r.stderr, failures)


def case_intended_deletion_of_old_file_passes(failures):
    """Case 4: the branch deletes a file that existed on main before
    its last push. That is an intended deletion predating the branch,
    and main did not add it after, so the intersection is empty and
    the push passes."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        # main has base.txt and old.txt -- the latter will be the file
        # the branch deletes.
        (repo / "old.txt").write_text("old\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "old file on main")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        # main moves, but with an unrelated new file. old.txt stays put.
        git(repo, "checkout", "-q", "main")
        (repo / "new_on_main.txt").write_text("new\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # feat deletes old.txt (it existed before the last push, so the
        # deletion is intended).
        git(repo, "checkout", "-q", "feat")
        git(repo, "rm", "-q", "old.txt")
        git(repo, "commit", "-q", "-m", "drop old")
        new_sha = head_of(repo)

        line = push_line("refs/heads/feat", new_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("intended-deletion exit", 0, r, failures)
        # The new file main added is not deleted by this branch, so it
        # should not be flagged.
        assert_not_in("intended-deletion silent on main-new file",
                      "new_on_main.txt", r.stderr, failures)


def case_file_added_twice_on_main_not_flagged(failures):
    """Case 7: a path main added twice (add, delete, re-add) counts once, so a
    push that deletes only an unrelated old file passes. Each list is deduped
    before intersecting.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        # old.txt predates the branch; deleting it is intended and must
        # not be flagged. It also keeps the deleted list non-empty so
        # the check's intersection actually runs.
        (repo / "old.txt").write_text("old\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "old file on main")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        # main adds dup.txt, deletes it, re-adds it: the path appears
        # twice in the gained range, once per adding commit.
        git(repo, "checkout", "-q", "main")
        (repo / "dup.txt").write_text("one\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "add dup")
        git(repo, "rm", "-q", "dup.txt")
        git(repo, "commit", "-q", "-m", "drop dup")
        (repo / "dup.txt").write_text("two\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "re-add dup")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # feat drops old.txt (intended: it predates the branch) but
        # keeps the re-added dup.txt, so the push deletes nothing
        # main gained.
        git(repo, "checkout", "-q", "feat")
        git(repo, "rm", "-q", "old.txt")
        git(repo, "commit", "-q", "-m", "drop old")
        new_sha = head_of(repo)

        line = push_line("refs/heads/feat", new_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("dup-added-twice exit", 0, r, failures)
        assert_not_in("dup-added-twice names no path",
                      "dup.txt", r.stderr, failures)


def case_deleted_path_with_space_refused_whole(failures):
    """Case 8: the deleted path main gained contains a space. The
    refusal must name the path whole -- the unquoted print used to
    word-split it into pieces."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        git(repo, "checkout", "-q", "main")
        (repo / "dir with space").mkdir()
        (repo / "dir with space" / "notes file.txt").write_text("n\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "feat")
        git(repo, "reset", "-q", "--soft", "origin/main")
        git(repo, "commit", "-q", "-m", "bad squash")
        bad_sha = head_of(repo)

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("space path refused", 1, r, failures)
        assert_in("space path named whole",
                  "  dir with space/notes file.txt\n", r.stderr, failures)
        # The whole path was printed once; the word-split pieces must
        # not appear as their own lines.
        assert_not_in("space path not split",
                      "  dir\n", r.stderr, failures)


def case_harmless_pushes_pass(failures):
    """Case 5: new branches, branch deletions, and pushes of main itself
    each pass without engaging the check."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        # New branch push (remote sha is all zeros).
        git(repo, "checkout", "-q", "-b", "fresh")
        (repo / "fresh.txt").write_text("fresh\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "fresh")
        fresh_sha = head_of(repo)
        line_new = push_line(
            "refs/heads/fresh", fresh_sha,
            "refs/heads/fresh",
            "0" * 40)

        # Branch deletion push (local sha is all zeros).
        git(repo, "checkout", "-q", "main")
        line_del = push_line(
            "refs/heads/fresh", "0" * 40,
            "refs/heads/fresh", fresh_sha)

        # Push of main itself, even with deletions in it, is not the
        # check's concern.
        (repo / "to_drop.txt").write_text("drop\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "dropped on main")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))
        git(repo, "rm", "-q", "to_drop.txt")
        git(repo, "commit", "-q", "-m", "drop on main")
        main_sha = head_of(repo)
        line_main = push_line(
            "refs/heads/main", main_sha,
            "refs/heads/main",
            git(repo, "rev-parse",
                "refs/remotes/origin/main").stdout.strip())

        stdin_text = line_new + line_del + line_main
        r = run_guard(stdin_text, cwd=repo)
        assert_rc("harmless pushes exit", 0, r, failures)


def case_head_as_source_refused(failures):
    """Case 9: `HEAD` as the source of a push to a branch is refused."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        # Same bad squash as case 1, but the source is the literal
        # `HEAD` -- what `git push origin HEAD:branch` produces.
        line = push_line("HEAD", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("HEAD source refused", 1, r, failures)
        assert_in("HEAD source names x.txt",
                  "x.txt", r.stderr, failures)
        assert_in("HEAD source names y.txt",
                  "y.txt", r.stderr, failures)
        # Destination branch (not source) is what the user needs to see
        # so they know which branch is being guarded.
        assert_in("HEAD source names destination feat",
                  "refs/heads/feat", r.stderr, failures)
        assert_in("HEAD source explains ack",
                  "PUSH_GUARD_ACK=1", r.stderr, failures)


def case_sha_as_source_refused(failures):
    """Case 10: a bare SHA as the source of a push to a branch is refused."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        line = push_line(bad_sha, bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("SHA source refused", 1, r, failures)
        assert_in("SHA source names x.txt",
                  "x.txt", r.stderr, failures)
        assert_in("SHA source names y.txt",
                  "y.txt", r.stderr, failures)
        assert_in("SHA source names destination feat",
                  "refs/heads/feat", r.stderr, failures)


def case_end_to_end_push_refused(failures):
    """Case 6: the bad-squash branch actually pushed through a real
    `git push` with the pre-push hook installed; the remote ref must
    not move."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "a")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        git(repo, "checkout", "-q", "main")
        (repo / "x.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "feat")
        git(repo, "reset", "-q", "--soft", "origin/main")
        git(repo, "commit", "-q", "-m", "bad squash")

        # Wire the pre-push hook at this checkout's real hooks/.
        git(repo, "config",
            "core.hooksPath",
            str(REPO_ROOT / "hooks"))

        env = env_with_identity()
        before = git(repo, "ls-remote", "origin", "feat").stdout.strip()
        # The bad squash's parent is origin/main, while origin/feat is
        # at the previous push: a `--force` (or `--force-with-lease`)
        # is what would land this on the remote in the real bug. Without
        # that flag, git's own fast-forward check rejects first and the
        # hook never runs.
        r = subprocess.run(
            ["git", "-C", str(repo), "push", "--force-with-lease",
             "origin", "feat"],
            capture_output=True, text=True, env=env)
        after = git(repo, "ls-remote", "origin", "feat").stdout.strip()

        assert_rc("e2e push refused", 1, r, failures)
        assert_in("e2e push stderr names x.txt",
                  "x.txt", r.stderr, failures)
        assert_eq("e2e remote ref did not move", before, after, failures)


def case_end_to_end_head_source_push_refused(failures):
    """Case 11: case 9 through a real `git push origin HEAD:refs/heads/feat`;
    the remote ref must not move."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "a")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        git(repo, "checkout", "-q", "main")
        (repo / "x.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # Build the same bad squash case 6 builds, on the feat branch.
        # `git push origin HEAD:refs/heads/feat` then sends that commit
        # to the remote -- with HEAD as the local_ref, exactly as it
        # would appear in a real `git push` invocation.
        git(repo, "checkout", "-q", "feat")
        git(repo, "reset", "-q", "--soft", "origin/main")
        git(repo, "commit", "-q", "-m", "bad squash")
        # Make refs/remotes/origin/feat match the remote so
        # --force-with-lease does not reject before the hook runs.
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        # Wire the pre-push hook at this checkout's real hooks/.
        git(repo, "config",
            "core.hooksPath",
            str(REPO_ROOT / "hooks"))

        env = env_with_identity()
        before = git(repo, "ls-remote", "origin", "feat").stdout.strip()
        r = subprocess.run(
            ["git", "-C", str(repo), "push", "--force-with-lease",
             "origin", "HEAD:refs/heads/feat"],
            capture_output=True, text=True, env=env)
        after = git(repo, "ls-remote", "origin", "feat").stdout.strip()

        assert_rc("e2e HEAD source push refused", 1, r, failures)
        assert_in("e2e HEAD source push names x.txt",
                  "x.txt", r.stderr, failures)
        assert_eq("e2e HEAD source remote ref did not move",
                  before, after, failures)


def case_base_regression_refused(failures):
    """The base moved backwards: the branch
    was pushed on main's tip and the rebuilt commit sits on main's
    previous commit, so the push is refused and both bases are named."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha, main_tip = scenario_with_regressed_base(
            Path(t))

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("base regression refused", 1, r, failures)
        assert_in("base regression names the ref",
                  "refs/heads/feat", r.stderr, failures)
        assert_in("base regression names the old base",
                  main_tip, r.stderr, failures)
        assert_in("base regression names the new base",
                  git(repo, "merge-base", "origin/main",
                      bad_sha).stdout.strip(),
                  r.stderr, failures)
        assert_in("base regression explains the recovery",
                  "git rebase origin/main", r.stderr, failures)
        assert_in("base regression explains the recovery",
                  "git fetch origin", r.stderr, failures)
        assert_in("base regression explains the recovery",
                  "squash", r.stderr, failures)
        assert_in("base regression explains ack",
                  "PUSH_GUARD_ACK=1", r.stderr, failures)
        # The message says what happened to the branch. Main need not
        # have moved at all, so a message blaming main for moving is
        # wrong even when main did move.
        assert_in("base regression states what happened",
                  "forks from an older main commit",
                  r.stderr, failures)
        assert_not_in("base regression does not blame main for moving",
                      "main has moved on",
                      r.stderr, failures)


def case_base_regression_ack_overrides(failures):
    """The base moved backwards, with PUSH_GUARD_ACK=1: the refusal becomes a
    warning, and both bases are still named so the override is conscious."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha, main_tip = scenario_with_regressed_base(
            Path(t))

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo, env_extra={"PUSH_GUARD_ACK": "1"})

        assert_rc("base regression ack exit code", 0, r, failures)
        assert_in("base regression ack names the old base",
                  main_tip, r.stderr, failures)
        assert_in("base regression ack acknowledges",
                  "acknowledging", r.stderr, failures)


def case_forward_rebase_still_passes(failures):
    """Moving forward is intended. A branch rebased onto a
    newer main forks later than the push it replaces, so the push is
    allowed."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, p_sha, _ = scenario_with_branch_rebased_onto_main_tip(tmp)

        # main moves on again, and the branch rebases onto it.
        git(repo, "checkout", "-q", "main")
        (repo / "n.txt").write_text("n\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves again")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "feat")
        git(repo, "rebase", "--quiet", "origin/main")
        rebased_sha = head_of(repo)

        line = push_line("refs/heads/feat", rebased_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("forward rebase exit", 0, r, failures)
        assert_not_in("forward rebase is not called a regression",
                      "backwards", r.stderr, failures)
        assert_not_in("forward rebase names no duplicate",
                      "already has", r.stderr, failures)


def case_duplicate_patch_refused(failures):
    """The pushed range carries a copy of a commit main
    already has -- same patch, new sha. The refusal names the duplicated
    commit and its twin on main."""
    with tempfile.TemporaryDirectory() as t:
        repo, dup_sha, twin_sha = scenario_with_duplicated_patch(Path(t))
        branch_tip = head_of(repo)

        # A new branch: there is no previous push, so only the patch
        # check can fire.
        line = push_line("refs/heads/feat", branch_tip,
                         "refs/heads/feat", "0" * 40)
        r = run_guard(line, cwd=repo)

        assert_rc("duplicate patch refused", 1, r, failures)
        assert_in("duplicate patch names the ref",
                  "refs/heads/feat", r.stderr, failures)
        assert_in("duplicate patch names the branch commit",
                  dup_sha, r.stderr, failures)
        assert_in("duplicate patch names the twin on main",
                  twin_sha, r.stderr, failures)
        assert_in("duplicate patch explains the recovery",
                  "git rebase origin/main", r.stderr, failures)
        assert_in("duplicate patch explains the recovery",
                  "git fetch origin", r.stderr, failures)
        assert_in("duplicate patch explains the recovery",
                  "squash", r.stderr, failures)
        assert_in("duplicate patch explains ack",
                  "PUSH_GUARD_ACK=1", r.stderr, failures)


def case_duplicate_patch_ack_overrides(failures):
    """The pushed range carries a duplicate, with PUSH_GUARD_ACK=1: the refusal becomes a
    warning, and the duplicated commit is still named."""
    with tempfile.TemporaryDirectory() as t:
        repo, dup_sha, twin_sha = scenario_with_duplicated_patch(Path(t))
        branch_tip = head_of(repo)

        line = push_line("refs/heads/feat", branch_tip,
                         "refs/heads/feat", "0" * 40)
        r = run_guard(line, cwd=repo, env_extra={"PUSH_GUARD_ACK": "1"})

        assert_rc("duplicate patch ack exit code", 0, r, failures)
        assert_in("duplicate patch ack names the branch commit",
                  dup_sha, r.stderr, failures)
        assert_in("duplicate patch ack acknowledges",
                  "acknowledging", r.stderr, failures)


def case_brand_new_branch_without_duplicate_passes(failures):
    """A branch with no duplicated patch passes, new branch or
    not. The patch check runs on every push, so a branch that never
    pushed and carries its own work must still be allowed."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        git(repo, "checkout", "-q", "-b", "fresh")
        (repo / "fresh.txt").write_text("fresh\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "fresh work")
        fresh_sha = head_of(repo)

        line = push_line("refs/heads/fresh", fresh_sha,
                         "refs/heads/fresh", "0" * 40)
        r = run_guard(line, cwd=repo)

        assert_rc("new branch exit", 0, r, failures)
        assert_not_in("new branch names no duplicate",
                      "already has", r.stderr, failures)
        assert_not_in("new branch is not called a regression",
                      "backwards", r.stderr, failures)


def case_base_regression_recovery_pushes_cleanly(failures):
    """The recovery the refusal prints has to work, and the command it
    prints has to be the one that works.

    The fixture's local main lags origin/main, as a lane worktree's does
    after a history rewrite on the remote. The test takes the rebase
    command out of the refusal message and runs it verbatim: advice
    naming the local branch would rebase onto old history and leave the
    branch in the state that was just refused.

    The rebase is what fixes it: the three-way merge sees main's change
    present on both sides -- the rebuilt commit carried a copy of it --
    and treats it as already applied, so the rebuilt commit is left
    adding only the PR's own work.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, p_sha, bad_sha, main_tip = scenario_with_stale_local_main(tmp)

        # The push before the recovery is refused, so the recovery is
        # answering a real refusal.
        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        before = run_guard(line, cwd=repo)
        assert_rc("recovery fixture starts refused", 1, before, failures)

        # The advice must name the ref the guard judged against, not the
        # local branch, which lags the remote here.
        local_main = git(repo, "rev-parse", "main").stdout.strip()
        assert_not_eq("local main lags origin/main in the fixture",
                      local_main,
                      git(repo, "rev-parse", "origin/main").stdout.strip(),
                      failures)
        command = printed_rebase_command(before.stderr)
        assert_eq("the advice prints a fetch and a rebase",
                  True,
                  bool(command) and "git fetch" in command
                  and "git rebase" in command,
                  failures)
        assert_in("the advice names the remote's base ref",
                  "git rebase origin/main", before.stderr, failures)
        assert_not_in("the advice does not name the local branch",
                      "git rebase main", before.stderr, failures)

        # Step 1 of the printed recovery: run it verbatim.
        git(repo, "checkout", "-q", "feat")
        run = run_printed_command(repo, command)
        assert_rc("the printed recovery runs", 0, run, failures)
        rebased_sha = head_of(repo)
        # It rebased onto the remote's tip, not onto the stale local
        # branch: the branch's parent is origin/main's tip.
        assert_eq("the printed recovery rebases onto origin/main",
                  main_tip,
                  git(repo, "rev-parse", "HEAD^").stdout.strip(),
                  failures)

        # Step 2 of the printed recovery: squash from the fork point.
        git(repo, "reset", "--quiet", "--soft",
            git(repo, "merge-base", "origin/main", "HEAD").stdout.strip())
        git(repo, "commit", "-q", "-m", "rebuilt on the current main")
        squashed_sha = head_of(repo)

        line = push_line("refs/heads/feat", squashed_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("recovered branch pushes cleanly", 0, r, failures)
        assert_not_in("recovered branch is not a regression",
                      "backwards", r.stderr, failures)
        assert_not_in("recovered branch carries no duplicate",
                      "already has", r.stderr, failures)
        # The rebuilt commit now sits on main's tip, not on the older
        # commit the regression left it on.
        assert_eq("recovered branch forks from main's tip",
                  main_tip,
                  git(repo, "merge-base", "origin/main",
                      squashed_sha).stdout.strip(),
                  failures)
        # main's change survived the recovery exactly once: the branch
        # no longer carries its own copy of it, so it does not show up
        # in the diff against main, and main's file is still there.
        assert_eq("recovered branch adds only its own work",
                  ["feat.txt"],
                  sorted(git(repo, "diff", "--name-only",
                             "origin/main", squashed_sha).stdout.split()),
                  failures)
        assert_in("recovered branch still has main's file",
                  "m.txt",
                  git(repo, "ls-tree", "--name-only",
                      squashed_sha).stdout,
                  failures)


def case_duplicate_patch_recovery_pushes_cleanly(failures):
    """The recovery the refusal prints has to work on the duplicate
    fixture too, and the command it prints has to be the one that works:
    rebase onto the remote's base drops the commit whose patch is
    already upstream, then squash from the fork point.

    The fixture's local main lags origin/main, so advice naming the
    local branch would rebase onto old history."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, dup_sha, twin_sha = scenario_with_duplicated_patch(tmp)
        branch_tip = head_of(repo)

        # Make the local branch lag the remote, as a lane worktree's
        # does after a history rewrite on the remote.
        main_sha = git(repo, "rev-parse", "origin/main").stdout.strip()
        git(repo, "checkout", "-q", "main")
        git(repo, "reset", "--quiet", "--hard", f"{main_sha}~1")
        git(repo, "checkout", "-q", "feat")

        line = push_line("refs/heads/feat", branch_tip,
                         "refs/heads/feat", "0" * 40)
        before = run_guard(line, cwd=repo)
        assert_rc("duplicate fixture starts refused", 1, before, failures)

        local_main = git(repo, "rev-parse", "main").stdout.strip()
        assert_not_eq("local main lags origin/main in the fixture",
                      local_main,
                      git(repo, "rev-parse", "origin/main").stdout.strip(),
                      failures)
        command = printed_rebase_command(before.stderr)
        assert_in("the advice names the remote's base ref",
                  "git rebase origin/main", before.stderr, failures)
        assert_not_in("the advice does not name the local branch",
                      "git rebase main", before.stderr, failures)

        git(repo, "checkout", "-q", "feat")
        run = run_printed_command(repo, command)
        assert_rc("the printed recovery runs", 0, run, failures)
        git(repo, "reset", "--quiet", "--soft",
            git(repo, "merge-base", "origin/main", "HEAD").stdout.strip())
        git(repo, "commit", "-q", "-m", "rebuilt without the duplicate")
        squashed_sha = head_of(repo)

        line = push_line("refs/heads/feat", squashed_sha,
                         "refs/heads/feat", "0" * 40)
        r = run_guard(line, cwd=repo)

        assert_rc("recovered duplicate pushes cleanly", 0, r, failures)
        assert_not_in("recovered duplicate is still a duplicate",
                      "already has", r.stderr, failures)
        # The duplicate is gone: no commit in the branch's range is
        # marked '-' by cherry any more.
        assert_not_in("recovered branch has no duplicate left",
                      "- ",
                      git(repo, "cherry", "origin/main", squashed_sha).stdout,
                      failures)


def main():
    failures: list = []
    if not PUSH_GUARD.is_file():
        failures.append(f"missing tool: {PUSH_GUARD}")
    if not HOOK.is_file():
        failures.append(f"missing hook: {HOOK}")
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1

    case_issue68_reverted_files_refused(failures)
    case_ack_overrides_refusal(failures)
    case_correct_rebase_keeps_gained_files(failures)
    case_intended_deletion_of_old_file_passes(failures)
    case_file_added_twice_on_main_not_flagged(failures)
    case_deleted_path_with_space_refused_whole(failures)
    case_harmless_pushes_pass(failures)
    case_head_as_source_refused(failures)
    case_sha_as_source_refused(failures)
    case_end_to_end_push_refused(failures)
    case_end_to_end_head_source_push_refused(failures)
    # The two stale-base shapes the gained/deleted check
    # cannot see, because neither of them deletes anything.
    case_base_regression_refused(failures)
    case_base_regression_ack_overrides(failures)
    case_forward_rebase_still_passes(failures)
    case_duplicate_patch_refused(failures)
    case_duplicate_patch_ack_overrides(failures)
    case_brand_new_branch_without_duplicate_passes(failures)
    # The recovery the refusal prints has to be the one that works.
    case_base_regression_recovery_pushes_cleanly(failures)
    case_duplicate_patch_recovery_pushes_cleanly(failures)
    # Mutation tests last so the on-disk push-guard is restored before
    # main returns. They re-run earlier cases with the mutated tool and
    # expect the opposite verdict.

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS push-guard: deleted/gained intersection refused; "
          "ack overrides; rebase passes; intended-deletion passes; "
          "harmless refs pass; e2e push refused; "
          "base regression refused; duplicate patch refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
