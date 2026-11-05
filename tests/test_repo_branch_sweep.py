#!/usr/bin/env python3
"""Pin repo-branch-sweep's report and its prune against deleting live work.

A branch with no commit of its own is a lane that has not pushed yet, not a
branch whose work landed. It used to be reported under "landed, safe to
delete", and --prune then ran `git worktree remove --force` on the lane's
worktree, which took its uncommitted changes with it.

Three shapes are pinned against real git in fresh `git init` repositories:

  * a just-created lane branch with uncommitted edits is empty, and --prune
    leaves the branch and its worktree alone;
  * a landed branch with a clean worktree is pruned;
  * a landed branch whose worktree is dirty is skipped and named.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP = REPO_ROOT / "bin" / "repo-branch-sweep"


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=check)


def run_sweep(repo, *args):
    return subprocess.run([str(SWEEP), str(repo), *args],
                          capture_output=True, text=True)


def init_repo(tmpdir):
    """A repository with a bare origin, so the sweep has a trunk to read."""
    origin = Path(tmpdir) / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--quiet", "--initial-branch=main")

    repo = Path(tmpdir) / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "remote", "add", "origin", str(origin))

    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "base")
    git(repo, "push", "--quiet", "-u", "origin", "main")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main")
    return repo, origin


def worktree_for(repo, path, branch):
    """A worktree on a new branch, the shape a lane creates."""
    git(repo, "worktree", "add", "--quiet", str(path), "-b", branch)
    return Path(path)


def land(repo, branch, subject):
    """Land a branch's work on the trunk by subject.

    The trunk moves on first and the branch's work is squashed in, so the
    branch keeps a commit of its own that the trunk does not have by SHA.
    That is the shape the sweep matches on: a squash-merge rewrites the
    commits, and only the subjects survive.
    """
    git(repo, "checkout", "--quiet", "main")
    (repo / "trunk.txt").write_text("trunk moved on\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "trunk moved on")

    git(repo, "merge", "--squash", "--quiet", branch)
    git(repo, "commit", "--quiet", "-m", subject)
    git(repo, "push", "--quiet", "origin", "main")
    git(repo, "fetch", "--quiet", "origin")
    git(repo, "checkout", "--quiet", "main")


def branch_exists(repo, branch):
    r = git(repo, "show-ref", "--verify", "--quiet",
            f"refs/heads/{branch}", check=False)
    return r.returncode == 0


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_not_in(label, needle, haystack, failures):
    if needle in haystack:
        fail(failures, label, f"{needle!r} in {haystack!r}")


def assert_eq(label, want, got, failures):
    if want != got:
        fail(failures, label, f"want {want!r}, got {got!r}")


def assert_true(label, cond, failures, detail=""):
    if not cond:
        fail(failures, label, detail)


def case_empty_lane_branch_is_reported_empty_and_survives_prune(failures):
    # A lane's branch sits at zero commits between `worktree add` and its
    # first commit. It has landed nowhere, so --prune must not touch it.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, _ = init_repo(tmpdir)
        wt = worktree_for(repo, Path(tmpdir) / "wt-lane", "feat/oci-image")
        (wt / "wip.txt").write_text("work in progress\n")

        report = run_sweep(repo)
        out = report.stdout + report.stderr

        assert_in("empty branch is named as empty", "feat/oci-image", out, failures)
        assert_in("empty branch has its own section", "empty", out, failures)
        assert_not_in("empty branch is not called landed",
                      "feat/oci-image (no commits of its own)\n\n== still carrying work",
                      out, failures)

        pruned = run_sweep(repo, "--prune")
        pout = pruned.stdout + pruned.stderr

        assert_true("prune keeps the empty branch",
                    branch_exists(repo, "feat/oci-image"), failures,
                    "the branch was deleted")
        assert_true("prune keeps the lane's worktree", wt.exists(), failures,
                    f"{wt} was removed")
        assert_true("prune keeps the uncommitted file",
                    (wt / "wip.txt").exists(), failures,
                    "the uncommitted file was discarded")
        assert_not_in("prune does not report the empty branch as pruned",
                      "Deleted branch feat/oci-image", pout, failures)


def case_landed_branch_with_clean_worktree_is_pruned(failures):
    # The ordinary case still works: a landed branch and its clean worktree
    # are removed.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, _ = init_repo(tmpdir)
        wt = worktree_for(repo, Path(tmpdir) / "wt-landed", "feat/landed")

        (wt / "feat.txt").write_text("feat\n")
        git(wt, "add", ".")
        git(wt, "commit", "--quiet", "-m", "feat work")

        land(repo, "feat/landed", "feat work")

        report = run_sweep(repo)
        assert_in("landed branch is reported", "feat/landed", report.stdout,
                  failures)

        pruned = run_sweep(repo, "--prune")
        pout = pruned.stdout + pruned.stderr

        assert_true("prune removes the landed branch",
                    not branch_exists(repo, "feat/landed"), failures,
                    "the branch survived")
        assert_true("prune removes the clean worktree", not wt.exists(), failures,
                    f"{wt} survived")
        assert_in("prune says it deleted the branch",
                  "Deleted branch feat/landed", pout, failures)


def case_landed_branch_with_dirty_worktree_is_skipped_and_named(failures):
    # A worktree with uncommitted changes is a lane still working. It is
    # skipped and named, not deleted.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, _ = init_repo(tmpdir)
        wt = worktree_for(repo, Path(tmpdir) / "wt-dirty", "feat/dirty")

        (wt / "feat.txt").write_text("feat\n")
        git(wt, "add", ".")
        git(wt, "commit", "--quiet", "-m", "feat work")

        land(repo, "feat/dirty", "feat work")

        # The lane's uncommitted edit, made after the work landed.
        (wt / "wip.txt").write_text("still working\n")
        (wt / "feat.txt").write_text("changed but not committed\n")

        pruned = run_sweep(repo, "--prune")
        pout = pruned.stdout + pruned.stderr

        assert_in("dirty worktree is named", "feat/dirty", pout, failures)
        assert_in("dirty worktree is reported as skipped", "skipped", pout, failures)
        assert_in("dirty worktree's path is named", str(wt), pout, failures)

        assert_true("dirty worktree survives", wt.exists(), failures,
                    f"{wt} was removed")
        assert_true("dirty worktree's file survives",
                    (wt / "wip.txt").exists(), failures,
                    "the uncommitted file was discarded")
        assert_true("dirty worktree's modified file survives",
                    (wt / "feat.txt").exists()
                    and (wt / "feat.txt").read_text()
                    == "changed but not committed\n",
                    failures,
                    "the modified file was discarded")
        assert_true("the branch of a dirty worktree survives",
                    branch_exists(repo, "feat/dirty"), failures,
                    "the branch was deleted")


def main():
    if not SWEEP.is_file():
        return 1
    failures: list = []
    for case in CASES:
        case(failures)
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS repo-branch-sweep: an empty branch is empty and survives "
          "--prune; a landed branch with a clean worktree is pruned; "
          "a landed branch with a dirty worktree is skipped and named "
          "(issue #167)")
    return 0


CASES = (case_empty_lane_branch_is_reported_empty_and_survives_prune,
         case_landed_branch_with_clean_worktree_is_pruned,
         case_landed_branch_with_dirty_worktree_is_skipped_and_named)

if __name__ == "__main__":
    sys.exit(main())
