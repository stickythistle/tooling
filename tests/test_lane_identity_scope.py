#!/usr/bin/env python3
"""Pin which commits the identity verifier is responsible for.

The verifier re-authors the commits the lane wrote. It used to re-author
every commit in `$base..HEAD`, which is wrong twice over:

- A lane that rebased onto a newer default branch has that branch's
  commits in the range. Re-authoring them gives the branch rewritten
  copies of commits already on main -- the same tree, new shas, and a
  history duplicated on the pull request.
- A lane whose job is to merge a branch with foreign authors pulls
  thousands of somebody else's commits into the range, and a rebase
  flattens the merge on top of that.

The lane's own commits are the ones on its first-parent chain, minus the
ones the default branch already has.

The suite drives the real `verify_lane_identity` from `lib/common.sh`
over real repositories.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON = REPO_ROOT / "lib" / "common.sh"


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def git_out(cwd, *args):
    return git(cwd, *args).stdout.strip()


def verify(worktree: Path, base: str, default: str = "main",
           name: str = "Lane", email: str = "lane@example.invalid"):
    """Run the real verifier over a worktree.

    The identity is scoped to the call, the way the verifier scopes it
    to its own rewrite: an exported identity would leak into every
    later command in the shell.
    """
    script = f'''
. "{COMMON}"
LANE_GIT_NAME="{name}"
LANE_GIT_EMAIL="{email}"
verify_lane_identity "{worktree}" "{base}" "" "{default}"
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True,
                            text=True)
    return result


class Repo:
    """A repository with a lane branch, and a default branch that moves."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "repo"
        self.path.mkdir()
        git(self.path, "init", "-q", "-b", "main")
        git(self.path, "config", "user.email", "upstream@example.invalid")
        git(self.path, "config", "user.name", "Upstream")
        (self.path / "a.md").write_text("base\n")
        git(self.path, "add", ".")
        git(self.path, "commit", "-q", "-m", "base")

    def commit(self, message: str, name: str = "a.md", body: str = "x\n"):
        (self.path / name).write_text(body)
        git(self.path, "add", ".")
        git(self.path, "commit", "-q", "-m", message)

    def branch(self, name: str):
        """Check out `name`, creating it from the current HEAD if new."""
        try:
            git(self.path, "checkout", "-q", name)
        except subprocess.CalledProcessError:
            git(self.path, "checkout", "-q", "-b", name)

    def sha(self, ref: str) -> str:
        return git_out(self.path, "rev-parse", ref)

    def log(self, fmt: str = "%H %an <%ae> %s") -> list[str]:
        return [line for line in
                git_out(self.path, "log", f"--format={fmt}").splitlines()
                if line]


def case_a_lane_that_rebased_onto_a_newer_main(failures):
    """The issue's case: the lane rebases onto a newer main and adds one
    commit. Only that commit is checked, and main's commits keep their
    shas."""
    with tempfile.TemporaryDirectory() as t:
        repo = Repo(Path(t))
        repo.branch("lane")
        lane_base = repo.sha("HEAD")
        # main moves on, authored by somebody else.
        repo.branch("main")
        repo.commit("main moves on", "m.md")
        main_tip = repo.sha("main")
        # The lane rebases onto it and adds its own commit.
        repo.branch("lane")
        git(repo.path, "rebase", "-q", "main")
        repo.commit("the lane's own commit", "w.md")
        lane_commit = repo.sha("HEAD")

        result = verify(repo.path, lane_base)
        if result.returncode != 0:
            fail(failures, "rebase: exit", result.returncode)
            fail(failures, "rebase: stderr", result.stderr[-500:])
            return
        # Only the lane's own commit was rewritten.
        branch_shas = repo.log("%H")
        if main_tip not in branch_shas:
            fail(failures, "rebase: main's commit lost its sha",
                 repo.log("%H %s"))
            return
        if lane_commit in branch_shas:
            fail(failures, "rebase: the lane's commit was not re-authored")
            return
        # The lane's commit carries the expected identity.
        head = git_out(repo.path, "log", "-1", "--format=%an <%ae>")
        if head != "Lane <lane@example.invalid>":
            fail(failures, "rebase: the lane's commit identity", head)
        # And only the lane's commit is named as re-authored.
        named = [line for line in result.stderr.splitlines()
                 if line.startswith("re-authored:")]
        if len(named) != 1:
            fail(failures, "rebase: how many commits were re-authored",
                 named)


def case_a_lane_that_merged_a_branch_with_foreign_authors(failures):
    """The comment's case: the lane merges a branch with foreign authors
    and adds one commit of its own. Only the lane's two commits get
    checked, and the merge survives."""
    with tempfile.TemporaryDirectory() as t:
        repo = Repo(Path(t))
        repo.branch("lane")
        lane_base = repo.sha("HEAD")
        # A branch with foreign authors.
        repo.branch("upstream-work")
        repo.commit("upstream commit 1", "u1.txt")
        repo.commit("upstream commit 2", "u2.txt")
        upstream_tip = repo.sha("HEAD")
        upstream_first = git_out(repo.path, "rev-parse", "upstream-work~1")
        # The lane merges it and adds its own commit.
        repo.branch("lane")
        git(repo.path, "merge", "-q", "--no-ff", "upstream-work",
            "-m", "Merge upstream/main")
        repo.commit("the lane's own commit", "w.md")

        result = verify(repo.path, lane_base)
        if result.returncode != 0:
            fail(failures, "merge: exit", result.returncode)
            fail(failures, "merge: stderr", result.stderr[-500:])
            return
        # Upstream's commits kept their shas.
        shas = repo.log("%H")
        if upstream_tip not in shas:
            fail(failures, "merge: upstream's tip lost its sha", shas)
            return
        if upstream_first not in shas:
            fail(failures, "merge: upstream's first commit lost its sha",
                 shas)
            return
        # The merge survived as a merge.
        merges = git_out(repo.path, "log", "--merges",
                         "--format=%s").splitlines()
        if not any("Merge upstream/main" in m for m in merges):
            fail(failures, "merge: the merge did not survive", merges)
            return
        # The lane's own two commits were re-authored.
        named = [line for line in result.stderr.splitlines()
                 if line.startswith("re-authored:")]
        if len(named) != 2:
            fail(failures, "merge: how many commits were re-authored",
                 named)
            return
        # The merge commit is the lane's: it is on the first-parent
        # chain and the lane authored it, so its identity is checked
        # and must carry the expected one.
        merge_line = git_out(repo.path, "log", "--merges",
                             "--format=%an <%ae> %s")
        if "Lane <lane@example.invalid>" not in merge_line:
            fail(failures, "merge: the merge commit's identity", merge_line)
        # And the verifier is idempotent: a second run over the
        # rewritten branch finds everything correct and re-authors
        # nothing. A rewrite that left a commit's parent unrewritten
        # would re-author the same commits on every run.
        again = verify(repo.path, lane_base)
        if again.returncode != 0:
            fail(failures, "merge: the second run exited",
                 again.returncode)
            return
        if "re-authored" in again.stderr:
            fail(failures, "merge: the verifier is not idempotent",
                 again.stderr[-300:])


def case_the_tree_is_unchanged(failures):
    """The rewrite is a re-author, not a redraft: the tree the branch
    points at is byte-identical before and after."""
    with tempfile.TemporaryDirectory() as t:
        repo = Repo(Path(t))
        repo.branch("lane")
        lane_base = repo.sha("HEAD")
        repo.commit("the lane's own commit", "w.md")
        before = git_out(repo.path, "rev-parse", "HEAD^{tree}")
        result = verify(repo.path, lane_base)
        if result.returncode != 0:
            fail(failures, "tree: exit", result.returncode)
            return
        after = git_out(repo.path, "rev-parse", "HEAD^{tree}")
        if before != after:
            fail(failures, "tree: the tree changed", f"{before} -> {after}")


def case_a_lane_with_the_right_identity_is_left_alone(failures):
    """A lane with the expected identity is not rewritten at all."""
    with tempfile.TemporaryDirectory() as t:
        repo = Repo(Path(t))
        repo.branch("lane")
        lane_base = repo.sha("HEAD")
        # The lane commits with the expected identity.
        git(repo.path, "config", "user.email", "lane@example.invalid")
        git(repo.path, "config", "user.name", "Lane")
        repo.commit("the lane's own commit", "w.md")
        before = repo.sha("HEAD")
        result = verify(repo.path, lane_base)
        if result.returncode != 0:
            fail(failures, "clean: exit", result.returncode)
            return
        if repo.sha("HEAD") != before:
            fail(failures, "clean: the head moved")
        if "re-authored" in result.stderr:
            fail(failures, "clean: something was re-authored",
                 result.stderr[:200])


def case_the_refuse_mode_still_refuses(failures):
    """A lane whose commit carries somebody else's identity is refused
    when the operator asks for refusal, and the refusal names the
    commit."""
    with tempfile.TemporaryDirectory() as t:
        repo = Repo(Path(t))
        repo.branch("lane")
        lane_base = repo.sha("HEAD")
        repo.commit("the lane's own commit", "w.md")
        script = f'''
. "{COMMON}"
export LANE_GIT_IDENTITY=refuse
LANE_GIT_NAME="Lane"
LANE_GIT_EMAIL="lane@example.invalid"
verify_lane_identity "{repo.path}" "{lane_base}" "" main
'''
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True)
        if result.returncode == 0:
            fail(failures, "refuse: exited 0")
            return
        if "refusing" not in result.stderr:
            fail(failures, "refuse: the refusal is not named",
                 result.stderr[:200])


def main() -> int:
    if not COMMON.is_file():
        print(f"FAIL common.sh not found: {COMMON}")
        return 1
    failures: list = []
    for case in CASES:
        try:
            case(failures)
        except Exception as exc:  # noqa: BLE001
            fail(failures, f"{case.__name__} raised",
                 f"{type(exc).__name__}: {exc}")
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS lane identity: a lane that rebased onto a newer main; a "
          "lane that merged a branch with foreign authors; the tree is "
          "unchanged; a clean lane is left alone; the refuse mode")
    return 0


CASES = (case_a_lane_that_rebased_onto_a_newer_main,
         case_a_lane_that_merged_a_branch_with_foreign_authors,
         case_the_tree_is_unchanged,
         case_a_lane_with_the_right_identity_is_left_alone,
         case_the_refuse_mode_still_refuses)


if __name__ == "__main__":
    sys.exit(main())
