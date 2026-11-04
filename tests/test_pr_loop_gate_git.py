#!/usr/bin/env python3
"""Pin pr-loop-gate on patches that real git produced.

The scenarios in `test_pr_loop_gate.py` are written by hand, and a
hand-written patch can be written to agree with whatever the code under
test does. Every patch here comes out of a real `git diff`, in a real
repository, from real commits, pinning the rule: carry the approval when
the blob shas are equal, refuse it when a blob changed.

  * A genuine rebase carries the approval: the replayed file's content,
    and so its blob sha, is unchanged even though the tree moved.
  * A file the base edited between the merge bases is NOT equal: the
    replayed file carries the base's edit too, so REVIEW.
  * The impostor -- same changed text, different part of the file -- is
    NOT equal: the file ends up with different content.

No network. A stub `gh` answers from data this test writes, with the
blob shas `git rev-parse` reports for the same commits.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "bin" / "pr-loop-gate"

sys.path.insert(0, str(REPO_ROOT / "lib"))
import pr_gate  # noqa: E402

REVIEWER = "stickythistle"

# The stub `gh` and the helpers around it are the ones the main suite
# uses, so both suites drive the same gate the same way.
sys.path.insert(0, str(REPO_ROOT / "tests"))
import test_pr_loop_gate as harness  # noqa: E402


def git(repo: Path, *args: str) -> str:
    """Run one git command in `repo` and return its stdout, trimmed."""
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True).strip()


def write(repo: Path, name: str, text: str) -> None:
    """Write a file in the repository."""
    (repo / name).write_text(text)


def commit(repo: Path, message: str) -> str:
    """Commit whatever is in the worktree and return the commit sha."""
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def compare_files(repo: Path, base: str, commit_sha: str) -> list:
    """Return the compare response for `base...commit_sha`.

    The patch is the one `git diff` prints, from where the API's patch
    field starts; the sha is the file's blob sha at that commit.
    """
    out = []
    names = git(repo, "diff", "--name-only", f"{base}...{commit_sha}").splitlines()
    for name in names:
        diff = git(repo, "diff", f"{base}...{commit_sha}", "--", name)
        start = diff.find("@@")
        patch = diff[start:] if start >= 0 else ""
        blob = git(repo, "rev-parse", f"{commit_sha}:{name}")
        status = "modified"
        try:
            git(repo, "cat-file", "-e", f"{base}:{name}")
        except subprocess.CalledProcessError:
            status = "added"
        out.append({"filename": name, "status": status, "sha": blob,
                    "patch": patch})
    return out


def tree_of(repo: Path, commit_sha: str) -> str:
    """Return the tree sha of a commit."""
    return git(repo, "rev-parse", f"{commit_sha}^{{tree}}")


def run_gate(data: Path, bin_dir: Path):
    """Run the gate against the data directory."""
    return harness.run_gate(data, bin_dir, reviewer=REVIEWER)


# The file the scenarios edit. It has two functions with the same body,
# so a change to either one produces the same changed lines.
BODY = ("    a = 1\n    b = 2\n    c = 3\n    d = 4\n"
        "    return False\n    # end 1\n    # end 2\n    # end 3\n\n")
SOURCE = "def guest_access():\n" + BODY + "def admin_access():\n" + BODY


def make_repo(tmp: Path) -> Path:
    """A repository with one commit holding the two-function file."""
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Gate Test")
    git(repo, "config", "user.email", "gate@example.invalid")
    write(repo, "access.py", SOURCE)
    commit(repo, "base")
    return repo


def scenario(tmp: Path, name: str, repo: Path, base: str, approved: str,
             head: str, reviews, checks=None):
    """Run the gate over one scenario and return its result.

    The data directory carries the compare responses for both commits,
    with the blob shas git reports, so the gate compares what the
    commits really contain.
    """
    bin_dir = tmp / f"bin_{name}"
    bin_dir.mkdir()
    harness.write_stub_gh(bin_dir)

    data = tmp / f"data_{name}"
    data.mkdir()
    trees = {head: tree_of(repo, head), approved: tree_of(repo, approved)}
    compares = {
        f"{base}...{approved}": compare_files(repo, base, approved),
        f"{base}...{head}": compare_files(repo, base, head),
    }
    harness.install(data, harness.pr(head=head, base=base), reviews, [],
                    checks or [harness.check_run()], trees, compares)
    return run_gate(data, bin_dir), data


def main() -> int:
    failures = []

    def expect(label, result, expected):
        if result.returncode != 0 or result.stdout.strip() != expected:
            failures.append(
                f"{label}: expected {expected!r}, got rc={result.returncode} "
                f"stdout={result.stdout.strip()!r} "
                f"stderr={result.stderr.strip()!r}")

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)

        # ---- the genuine rebase -------------------------------------
        #
        # The base moves on, leaving the pull request's file alone. The
        # file's content is unchanged by the replay, so its blob sha is
        # equal, and the approval carries.
        repo = make_repo(tmp / "rebase")
        base = git(repo, "rev-parse", "HEAD")
        write(repo, "access.py",
              SOURCE.replace("return False", "return True", 1))
        approved = commit(repo, "allow guest access")

        git(repo, "checkout", "-q", "--detach", base)
        write(repo, "notes.txt", "the base moved\n")
        new_base = commit(repo, "base moves")

        git(repo, "checkout", "-q", approved)
        git(repo, "rebase", "-q", new_base)
        head = git(repo, "rev-parse", "HEAD")

        reviews = [harness.review(701, "APPROVED", approved,
                                   "2026-10-20T00:00:00Z")]
        result, _ = scenario(tmp, "rebase", repo, new_base, approved, head,
                              reviews)
        expect("rebase", result, f"REAPPROVE {head}")

        # The trees differ -- the base brought a file with it -- so the
        # tree comparison alone would not have carried the approval: the
        # gate reaches the file comparison, and the file's own blob sha
        # is what carries it.
        if tree_of(repo, head) == tree_of(repo, approved):
            failures.append(
                "rebase: the trees are equal, so this scenario does not "
                "reach the patch comparison")
        if git(repo, "rev-parse", f"{approved}:access.py") != \
                git(repo, "rev-parse", f"{head}:access.py"):
            failures.append(
                "rebase: the file's blob sha moved even though the base "
                "left the file alone, so the scenario is not the replay "
                "it claims to be")

        # ---- the base touched the file -------------------------------
        #
        # The base moved on and edited the very file the pull request
        # changed. The replayed file carries the base's edit too, so its
        # content is not what the reviewer approved: REVIEW.
        repo = make_repo(tmp / "base_touched")
        base = git(repo, "rev-parse", "HEAD")
        write(repo, "access.py",
              SOURCE.replace("return False", "return True", 1))
        approved = commit(repo, "allow guest access")

        git(repo, "checkout", "-q", "--detach", base)
        write(repo, "access.py", SOURCE.replace("    a = 1", "    a = 111"))
        new_base = commit(repo, "base moves and edits the same file")

        git(repo, "checkout", "-q", approved)
        git(repo, "rebase", "-q", new_base)
        head = git(repo, "rev-parse", "HEAD")

        reviews = [harness.review(701, "APPROVED", approved,
                                   "2026-10-20T00:00:00Z")]
        result, _ = scenario(tmp, "base_touched", repo, new_base, approved,
                              head, reviews)
        expect("base_touched", result, f"REVIEW {head} {approved}")

        # The blob really did move -- the base's edit rode into the
        # replayed file -- so the refusal is the changed content.
        if git(repo, "rev-parse", f"{approved}:access.py") == \
                git(repo, "rev-parse", f"{head}:access.py"):
            failures.append(
                "base_touched: the file's blob sha is unchanged, so this "
                "scenario does not pin the refused blob")

        # ---- the impostor --------------------------------------------
        #
        # The reviewer approved a change to `guest_access`; the head
        # changes `admin_access` instead. Same changed text, same hunk
        # distance from context, base holding still -- and the file ends
        # up with different content, so the blob shas refuse it.
        repo = make_repo(tmp / "impostor")
        base = git(repo, "rev-parse", "HEAD")

        write(repo, "access.py",
              SOURCE.replace("return False", "return True", 1))
        approved = commit(repo, "allow guest access")

        git(repo, "checkout", "-q", "--detach", base)
        split = SOURCE.index("def admin_access")
        write(repo, "access.py",
              SOURCE[:split] + SOURCE[split:].replace("return False",
                                                      "return True", 1))
        head = commit(repo, "allow admin access instead")

        reviews = [harness.review(701, "APPROVED", approved,
                                   "2026-10-20T00:00:00Z")]
        result, _ = scenario(tmp, "impostor", repo, base, approved, head,
                              reviews)
        expect("impostor", result, f"REVIEW {head} {approved}")

        # The same scenario through the pure function, so the decision
        # is pinned as well as the gate.
        pr_obj = harness.pr(head=head, base=base)
        trees = {head: tree_of(repo, head), approved: tree_of(repo, approved)}
        got = pr_gate.decide(pr_obj, list(reviews), [],
                             [harness.check_run()], trees, REVIEWER, 4,
                             frozenset([harness.IMPLEMENTER]), False)
        if got != f"REVIEW {head} {approved}":
            failures.append(
                f"impostor: decide() returned {got!r}, expected "
                f"{f'REVIEW {head} {approved}'!r}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
