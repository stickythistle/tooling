#!/usr/bin/env python3
"""Pin bin/squash against issue #68's moving-origin bug."""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SQUASH = REPO_ROOT / "bin" / "squash"
IDENTITY = {"GIT_AUTHOR_NAME": "squash-test",
            "GIT_AUTHOR_EMAIL": "squash-test@example.com",
            "GIT_COMMITTER_NAME": "squash-test",
            "GIT_COMMITTER_EMAIL": "squash-test@example.com"}
# The identity the repository's git config carries, and the one the sign-off
# has to name. The two agree here, because a commit signed by somebody other
# than its author is no DCO sign-off.
NAME = IDENTITY["GIT_AUTHOR_NAME"]
EMAIL = IDENTITY["GIT_AUTHOR_EMAIL"]
SIGNOFF = f"Signed-off-by: {NAME} <{EMAIL}>"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def run_squash(cwd, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), **IDENTITY}
    return subprocess.run([str(SQUASH), *args], cwd=str(cwd),
                          capture_output=True, text=True, env=env)


def init_repo(tmpdir):
    repo = Path(tmpdir) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", IDENTITY["GIT_AUTHOR_EMAIL"])
    git(repo, "config", "user.name", NAME)
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def make_branch_with_commits(repo, n):
    git(repo, "checkout", "--quiet", "-b", "feat")
    shas = []
    for i in range(n):
        (repo / f"feat_{i}.txt").write_text(f"feat {i}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", f"feat {i}")
        shas.append(head(repo))
    return shas


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_eq(label, want, got, failures):
    if want != got:
        fail(failures, label, f"want {want!r}, got {got!r}")


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_rc(label, want, r, failures):
    if r.returncode != want:
        fail(failures, label, f"got {r.returncode}, stderr={r.stderr!r}")


def case_moving_base_preserves_tree(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        base_sha = head(repo)
        shas = make_branch_with_commits(repo, n=3)
        pre = shas[-1]

        git(repo, "checkout", "--quiet", base_sha)
        (repo / "base_added.txt").write_text("base added this\n")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "base moved")
        git(repo, "update-ref", "refs/heads/base", head(repo))
        git(repo, "checkout", "--quiet", "feat")

        merge_base = git(repo, "merge-base", "base", "HEAD").stdout.strip()
        assert_eq("setup: merge-base is the fork, not base's tip", base_sha, merge_base, failures)

        r = run_squash(repo, "--base", "base", "-m", "squashed")
        if r.returncode != 0:
            assert_rc("moving-base exit code", 0, r, failures)
            return

        on_top = git(repo, "rev-list", "--count", f"{merge_base}..HEAD").stdout.strip()
        assert_eq("squash produced one commit", "1", on_top, failures)

        diff_out = git(repo, "diff", pre, "HEAD").stdout
        if diff_out.strip():
            fail(failures, "tree changed after squash", f"git diff {pre[:8]} HEAD: {diff_out!r}")

        # Parent must be the fork, not base's tip -- a `reset --soft $BASE`
        # would silently delete base_added.txt against its parent.
        diff_parent = git(repo, "diff", "HEAD^", "HEAD").stdout
        if "base_added.txt" in diff_parent:
            fail(failures, "squash commit deletes base's new file", f"git diff HEAD^ HEAD: {diff_parent!r}")


def case_dirty_index_refuses(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=2)

        (repo / "staged_only.txt").write_text("not yet committed\n")
        git(repo, "add", "staged_only.txt")
        pre = head(repo)

        r = run_squash(repo, "--base", "main", "-m", "should refuse")

        assert_rc("dirty index exit code", 1, r, failures)
        assert_in("dirty index named", "refusing", r.stderr, failures)

        assert_eq("HEAD unchanged after refusal", pre, head(repo), failures)

        staged = git(repo, "diff", "--cached", "--name-only").stdout.strip()
        if "staged_only.txt" not in staged:
            fail(failures, "staged change swept away", f"diff --cached --name-only: {staged!r}")


def case_dirty_working_tree_refuses(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=2)

        (repo / "feat_0.txt").write_text("dirty edit\n")
        pre = head(repo)

        r = run_squash(repo, "--base", "main", "-m", "should refuse")

        assert_rc("dirty tree exit code", 1, r, failures)
        assert_in("dirty tree named", "refusing", r.stderr, failures)

        assert_eq("HEAD unchanged after refusal", pre, head(repo), failures)

        wt = (repo / "feat_0.txt").read_text()
        assert_eq("dirty edit preserved", "dirty edit\n", wt, failures)


def case_no_message_refuses(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=2)

        pre = head(repo)
        r = run_squash(repo, "--base", "main")

        assert_rc("no-message exit code", 2, r, failures)
        assert_in("no-message names -m/-F", "-m", r.stderr, failures)
        assert_in("no-message names -F", "-F", r.stderr, failures)

        assert_eq("HEAD unchanged after refusal", pre, head(repo), failures)


def case_missing_F_file_refuses(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=2)

        pre = head(repo)
        r = run_squash(repo, "--base", "main", "-F", "/does/not/exist")

        assert_rc("missing -F exit code", 2, r, failures)
        assert_in("missing -F named", "not readable", r.stderr, failures)

        assert_eq("HEAD unchanged after missing-F refusal", pre, head(repo), failures)


def case_relative_F_from_subdir_uses_subdir_file(failures):
    # A relative -F FILE run from a subdirectory must be resolved against the
    # caller's CWD, not the repository root. Without the fix, the file path is
    # silently re-anchored under the root after `cd`, so a basename collision
    # commits the wrong message. See issue #86.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=2)

        nested = repo / "nested"
        nested.mkdir()
        (nested / "message.txt").write_text("intended squash message\n")
        (repo / "message.txt").write_text("wrong root message\n")

        r = run_squash(nested, "--base", "main", "-F", "message.txt")

        assert_rc("subdir -F exit code", 0, r, failures)

        msg = git(repo, "log", "-1", "--format=%B").stdout
        assert_eq("subdir -F uses subdir file, not root shadow",
                  "intended squash message\n\n", msg, failures)


def case_relative_F_missing_at_caller_refuses(failures):
    # When the caller's directory has no matching file, squash must refuse
    # regardless of whether the root happens to hold a file with the same
    # name. Without the fix, the relative path is re-anchored at the root and
    # the root's shadow file is silently used. See issue #86.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=2)

        nested = repo / "nested"
        nested.mkdir()
        (repo / "message.txt").write_text("root shadow\n")
        pre = head(repo)

        r = run_squash(nested, "--base", "main", "-F", "message.txt")

        assert_rc("missing-at-caller -F exit code", 2, r, failures)
        assert_in("missing-at-caller -F named", "not readable", r.stderr, failures)
        assert_eq("HEAD unchanged after missing-at-caller refusal",
                  pre, head(repo), failures)


def case_absolute_F_used_verbatim(failures):
    # An absolute -F FILE must be used as given, even when the caller's CWD
    # contains a file with the same basename. The fix should leave absolute
    # paths untouched. See issue #86.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=2)

        nested = repo / "nested"
        nested.mkdir()
        abs_msg = repo / "absolute.txt"
        abs_msg.write_text("absolute path message\n")
        (nested / "absolute.txt").write_text("caller shadow\n")

        r = run_squash(nested, "--base", "main", "-F", str(abs_msg))

        assert_rc("absolute -F exit code", 0, r, failures)
        msg = git(repo, "log", "-1", "--format=%B").stdout
        assert_eq("absolute -F uses absolute path, not caller shadow",
                  "absolute path message\n\n", msg, failures)


def case_head_at_fork_refuses_a_message(failures):
    # A message needs a commit to sit on, so the fork point refuses one
    # instead of claiming a success it cannot deliver. See issue #175.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        pre = head(repo)

        msg = Path(tmpdir) / "msg.txt"
        msg.write_text("a message for nothing\n")

        r = run_squash(repo, "--base", "main", "-F", str(msg))

        assert_rc("head-at-fork exit code", 1, r, failures)
        assert_in("head-at-fork named", "no commit to put the message on", r.stderr, failures)

        assert_eq("HEAD unchanged at fork", pre, head(repo), failures)


def case_head_at_fork_without_message_exits_zero(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        pre = head(repo)

        r = run_squash(repo, "--base", "main")

        assert_rc("head-at-fork exit code", 0, r, failures)
        assert_in("head-at-fork named", "nothing to squash", r.stderr, failures)

        assert_eq("HEAD unchanged at fork", pre, head(repo), failures)


def case_single_commit_amends_the_message(failures):
    # A finishing run squashes with the coordinator's subject and body. A
    # branch already one commit on the fork still has to carry that message,
    # or the exit status says the job is done while the commit disagrees.
    # See issue #175.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        shas = make_branch_with_commits(repo, n=1)
        pre = shas[0]
        pre_tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()

        msg = Path(tmpdir) / "msg.txt"
        msg.write_text("the coordinator's subject\n\nand its body\n")

        r = run_squash(repo, "--base", "main", "-F", str(msg))

        assert_rc("single-commit amend exit code", 0, r, failures)
        assert_in("single-commit amend says what it did", "amended", r.stdout + r.stderr, failures)

        new_head = head(repo)
        if new_head == pre:
            fail(failures, "single-commit amend left HEAD on the old commit")
            return

        got = git(repo, "log", "-1", "--format=%B").stdout
        assert_eq("single-commit amend carries the new message",
                  "the coordinator's subject\n\nand its body\n\n", got, failures)

        post_tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
        assert_eq("single-commit amend leaves the tree alone", pre_tree, post_tree, failures)

        on_top = git(repo, "rev-list", "--count", f"{pre}..HEAD").stdout.strip()
        assert_eq("single-commit amend adds no commit", "1", on_top, failures)


def case_single_commit_keeps_the_author(failures):
    # The amend replaces the message, not the credit: the lane that wrote
    # the commit stays its author.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=1)

        pre_author = git(repo, "log", "-1", "--format=%an <%ae>").stdout.strip()

        msg = Path(tmpdir) / "msg.txt"
        msg.write_text("a new subject\n")

        r = run_squash(repo, "--base", "main", "-F", str(msg))

        assert_rc("single-commit amend exit code", 0, r, failures)

        post_author = git(repo, "log", "-1", "--format=%an <%ae>").stdout.strip()
        assert_eq("amend keeps the author", pre_author, post_author, failures)


def case_single_commit_with_same_message_is_a_noop(failures):
    # A re-run after a successful squash reads as success: the commit
    # already carries the message, so nothing moves.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        shas = make_branch_with_commits(repo, n=1)
        pre = shas[0]

        msg = Path(tmpdir) / "msg.txt"
        msg.write_text("the coordinator's subject\n")

        first = run_squash(repo, "--base", "main", "-F", str(msg))
        assert_rc("first amend exit code", 0, first, failures)
        after_first = head(repo)

        second = run_squash(repo, "--base", "main", "-F", str(msg))

        assert_rc("re-run exit code", 0, second, failures)
        assert_in("re-run says nothing to squash", "nothing to squash",
                  second.stdout + second.stderr, failures)

        assert_eq("re-run leaves HEAD alone", after_first, head(repo), failures)

        got = git(repo, "log", "-1", "--format=%B").stdout
        assert_eq("re-run keeps the message",
                  "the coordinator's subject\n\n", got, failures)


def case_single_commit_without_message_exits_zero(failures):
    # Nothing to squash and nothing to say: the tool leaves the commit
    # alone and says so.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        shas = make_branch_with_commits(repo, n=1)
        pre = shas[0]
        pre_msg = git(repo, "log", "-1", "--format=%B").stdout

        r = run_squash(repo, "--base", "main")

        assert_rc("single-commit no-message exit code", 0, r, failures)
        assert_in("single-commit no-message named", "nothing to squash",
                  r.stdout + r.stderr, failures)

        assert_eq("HEAD unchanged on already-squashed branch", pre, head(repo), failures)
        assert_eq("message unchanged on already-squashed branch", pre_msg,
                  git(repo, "log", "-1", "--format=%B").stdout, failures)


def case_commit_failure_restores_head(failures):
    # Without the ERR trap, a hook rejection here exits with HEAD on the fork.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=3)
        pre = head(repo)

        hook = repo / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/usr/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        r = run_squash(repo, "--base", "main", "-m", "should fail")

        assert_rc("commit-failure exit code", 1, r, failures)
        assert_eq("HEAD restored to PRE after commit failure", pre, head(repo), failures)

        hook.unlink(missing_ok=True)


def signoff_lines(message):
    """The sign-off lines a commit message carries."""
    return [line for line in message.splitlines()
            if line.lower().startswith("signed-off-by:")]


def read_or_empty(path):
    """The file's content, or an empty string."""
    try:
        return path.read_text()
    except OSError:
        return ""


def move_main_and_publish(repo):
    """Move main forward and publish its tip as origin/main."""
    git(repo, "checkout", "--quiet", "main")
    (repo / "main_added.txt").write_text("main moved\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "main moved")
    tip = head(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", tip)
    git(repo, "checkout", "--quiet", "feat")
    return tip


def case_signoff_adds_one_signoff(failures):
    # The DCO wants exactly one sign-off, naming the commit's author.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=3)

        r = run_squash(repo, "--base", "main", "-s", "-m", "squashed")

        assert_rc("signoff exit code", 0, r, failures)

        count = git(repo, "rev-list", "--count", "main..HEAD").stdout.strip()
        assert_eq("signoff produced one commit", "1", count, failures)

        assert_eq("the commit carries the caller's subject", "squashed",
                  git(repo, "log", "-1", "--format=%s").stdout.strip(), failures)

        message = git(repo, "log", "-1", "--format=%B").stdout
        lines = signoff_lines(message)
        assert_eq("exactly one sign-off", 1, len(lines), failures)
        if lines:
            assert_eq("the sign-off names the configured identity", SIGNOFF,
                      lines[0], failures)


def case_signoff_one_commit_keeps_one_signoff(failures):
    # The DCO wants one sign-off, and the message already carries it.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        git(repo, "checkout", "--quiet", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        old_msg = Path(tmpdir) / "old.txt"
        old_msg.write_text(f"already signed off\n\n{SIGNOFF}\n")
        git(repo, "commit", "--quiet", "-F", str(old_msg))
        pre = head(repo)

        new_msg = Path(tmpdir) / "new.txt"
        new_msg.write_text(f"the coordinator's subject\n\nand its body\n\n{SIGNOFF}\n")

        r = run_squash(repo, "--base", "main", "-s", "-F", str(new_msg))

        assert_rc("one-commit signoff exit code", 0, r, failures)

        message = git(repo, "log", "-1", "--format=%B").stdout
        lines = signoff_lines(message)
        assert_eq("still exactly one sign-off", 1, len(lines), failures)
        if lines:
            assert_eq("the sign-off is the one that was there", SIGNOFF,
                      lines[0], failures)
        assert_in("the message was replaced", "the coordinator's subject",
                  message, failures)
        if "already signed off" in message:
            fail(failures, "the old message is still on the commit",
                 f"message: {message!r}")

        count = git(repo, "rev-list", "--count", f"{pre}..HEAD").stdout.strip()
        assert_eq("the amend added no commit", "1", count, failures)


def case_signoff_refuses_another_person(failures):
    # A sign-off by somebody else is left alone and the tool refuses,
    # naming both: the DCO wants one signer.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=3)
        pre = head(repo)
        other = "Someone Else <else@example.com>"

        r = run_squash(repo, "--base", "main", "-s", "-m",
                       f"a subject\n\nSigned-off-by: {other}")

        assert_rc("another sign-off exit code", 1, r, failures)
        assert_in("the other sign-off is named", other, r.stderr, failures)
        assert_in("our identity is named", f"{NAME} <{EMAIL}>", r.stderr, failures)
        assert_eq("HEAD unchanged after the refusal", pre, head(repo), failures)


def case_onto_moves_the_commit(failures):
    # `--onto` moves the squashed commit onto a base that moved, so the tree
    # keeps what the base added.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        make_branch_with_commits(repo, n=3)
        main_tip = move_main_and_publish(repo)

        r = run_squash(repo, "--base", "origin/main", "--onto", "origin/main",
                       "-m", "squashed")

        assert_rc("onto exit code", 0, r, failures)

        count = git(repo, "rev-list", "--count", "origin/main..HEAD").stdout.strip()
        assert_eq("onto produced one commit", "1", count, failures)

        parent = git(repo, "rev-parse", "HEAD^").stdout.strip()
        assert_eq("the commit sits on origin/main's tip", main_tip, parent, failures)

        files = git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        if "main_added.txt" not in files:
            fail(failures, "the move lost what main added", f"tree: {files}")
        for i in range(3):
            if f"feat_{i}.txt" not in files:
                fail(failures, "the move lost a file of the branch", f"tree: {files}")

        assert_eq("the branch's file is unchanged", "feat 0\n",
                  read_or_empty(repo / "feat_0.txt"), failures)
        assert_eq("main's file came with the move", "main moved\n",
                  read_or_empty(repo / "main_added.txt"), failures)


def case_onto_conflict_refuses(failures):
    # A conflict aborts the rebase: HEAD stays on the squashed commit, and the
    # reader is told which paths to resolve.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = init_repo(tmpdir)
        fork = head(repo)
        git(repo, "checkout", "--quiet", "-b", "feat")
        (repo / "both.txt").write_text("the branch\n")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "branch side")
        git(repo, "checkout", "--quiet", "main")
        (repo / "both.txt").write_text("the base\n")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "base side")
        main_tip = head(repo)
        git(repo, "update-ref", "refs/remotes/origin/main", main_tip)
        git(repo, "checkout", "--quiet", "feat")

        r = run_squash(repo, "--base", "origin/main", "--onto", "origin/main",
                       "-m", "squashed")

        assert_rc("onto conflict exit code", 1, r, failures)
        assert_in("onto conflict names the path", "both.txt", r.stderr, failures)

        assert_eq("HEAD carries the squashed message", "squashed",
                  git(repo, "log", "-1", "--format=%s").stdout.strip(), failures)
        assert_eq("HEAD sits on the fork point", fork,
                  git(repo, "rev-parse", "HEAD^").stdout.strip(), failures)

        git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
        for name in ("rebase-merge", "rebase-apply"):
            if (git_dir / name).exists():
                fail(failures, f"a rebase is still in progress ({name})")


def main():
    if not SQUASH.is_file():
        return 1
    failures: list = []
    for case in CASES:
        case(failures)
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS squash: moving-base -> 1 commit + tree unchanged; "
          "dirty index/tracked/missing-F refuse; "
          "head-at-fork refuses a message, no-message exits 0; "
          "one-commit amends the message, keeps the author, "
          "same message is a no-op (issue #175); "
          "commit failure restores HEAD; "
          "relative -F resolves from caller's CWD (issue #86); "
          "-s signs the commit off once and refuses another person's sign-off; "
          "--onto moves the commit onto a newer base, and a conflict aborts")
    return 0


CASES = (case_moving_base_preserves_tree, case_dirty_index_refuses,
         case_dirty_working_tree_refuses, case_no_message_refuses,
         case_missing_F_file_refuses, case_relative_F_from_subdir_uses_subdir_file,
         case_relative_F_missing_at_caller_refuses, case_absolute_F_used_verbatim,
         case_head_at_fork_refuses_a_message,
         case_head_at_fork_without_message_exits_zero,
         case_single_commit_amends_the_message,
         case_single_commit_keeps_the_author,
         case_single_commit_with_same_message_is_a_noop,
         case_single_commit_without_message_exits_zero,
         case_commit_failure_restores_head,
         case_signoff_adds_one_signoff,
         case_signoff_one_commit_keeps_one_signoff,
         case_signoff_refuses_another_person,
         case_onto_moves_the_commit,
         case_onto_conflict_refuses)

if __name__ == "__main__":
    sys.exit(main())
