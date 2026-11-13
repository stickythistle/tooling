#!/usr/bin/env python3
"""Pin bin/pr-push: the push is made against the remote's own tip, and only
a tip this branch held may be replaced."""
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_PUSH = REPO_ROOT / "bin" / "pr-push"
IDENTITY = {"GIT_AUTHOR_NAME": "pr-push-test",
            "GIT_AUTHOR_EMAIL": "pr-push-test@example.com",
            "GIT_COMMITTER_NAME": "pr-push-test",
            "GIT_COMMITTER_EMAIL": "pr-push-test@example.com"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def run_pr_push(cwd, *args, path=None):
    env = {"PATH": path or "/usr/bin:/bin", "HOME": str(Path.home()), **IDENTITY}
    return subprocess.run([str(PR_PUSH), *args], cwd=str(cwd),
                          capture_output=True, text=True, env=env)


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def remote_sha(origin, branch):
    """The sha the remote holds for a branch, or nothing when it has none."""
    out = subprocess.run(["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
                         capture_output=True, text=True, check=True).stdout
    parts = out.split()
    return parts[0] if parts else ""


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_eq(label, want, got, failures):
    if want != got:
        fail(failures, label, f"want {want!r}, got {got!r}")


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_absent(label, needle, haystack, failures):
    if needle in haystack:
        fail(failures, label, f"{needle!r} in {haystack!r}")


def assert_rc(label, want, r, failures):
    if r.returncode != want:
        fail(failures, label, f"got {r.returncode}, stderr={r.stderr!r}")


def init_repo(tmpdir):
    """A work repository and the bare origin it pushes to."""
    git(tmpdir, "init", "-q", "--bare", "origin.git")
    origin = Path(tmpdir) / "origin.git"
    # The origin's default branch, whatever this git calls a default.
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    repo = Path(tmpdir) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", IDENTITY["GIT_AUTHOR_EMAIL"])
    git(repo, "config", "user.name", IDENTITY["GIT_AUTHOR_NAME"])
    git(repo, "remote", "add", "origin", str(origin))
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "push", "-q", "origin", "main")
    return repo, origin


def install_hook(repo, origin, record=None):
    """A pre-push hook that runs a stub push-guard and records that it ran."""
    hooks = Path(repo).parent / "hooks"
    hooks.mkdir(exist_ok=True)
    record = record or (Path(repo).parent / "hook.log")
    guard = hooks / "push-guard"
    guard.write_text(f'#!/bin/sh\necho "guard ran" >> "{record}"\nexit 0\n')
    guard.chmod(0o755)
    hook = hooks / "pre-push"
    hook.write_text(f'#!/bin/sh\necho "hook ran" >> "{record}"\n'
                    f'exec "{guard}" "$@"\n')
    hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    return record


def install_recording_git(tmpdir, repo, origin, branch, compete=None):
    """A git that records what the tool asked it to do.

    With `compete`, it also moves the remote's branch once the lease has
    been read, as somebody else's push would. A remote that moves in
    between is what the lease is for, and this makes that move happen at
    exactly that point.
    """
    bindir = Path(tmpdir) / "fakebin"
    bindir.mkdir()
    log = Path(tmpdir) / "git.log"
    marker = Path(tmpdir) / "moved"
    real = shutil.which("git") or "/usr/bin/git"
    wrapper = bindir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        f'if [ "$1" = "ls-remote" ] && [ "$3" = "refs/heads/{branch}" ] '
        f'&& [ ! -f "{marker}" ] && [ -n "{compete or ""}" ]; then\n'
        f'\tout=$(/usr/bin/git "$@")\n'
        f'\t: > "{marker}"\n'
        f'\t/usr/bin/git --git-dir="{origin}" update-ref '
        f'refs/heads/{branch} {compete}\n'
        "\tprintf '%s' \"$out\"\n"
        "\texit 0\n"
        "fi\n"
        f'exec "{real}" "$@"\n')
    wrapper.chmod(0o755)
    return bindir, log


def push_argv(log):
    """The command line the tool pushed with."""
    if not log.is_file():
        return ""
    for line in log.read_text().splitlines():
        if line.startswith("push "):
            return line
    return ""


def make_branch(repo, name="feat"):
    """A branch with one commit, pushed to the origin."""
    git(repo, "checkout", "--quiet", "-b", name)
    (repo / f"{name}.txt").write_text(f"{name}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", name)
    git(repo, "push", "--quiet", "-u", "origin", name)
    return head(repo)


def rewrite_branch(repo):
    """Rewrite the branch's tip, as a squash or an amend of what it pushed.

    The old commit stays in the branch's reflog: the branch held it.
    """
    (repo / "more.txt").write_text("more\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "--amend", "-m", "rewritten")
    return head(repo)


def other_writer_pushes(repo, origin, branch="feat"):
    """Somebody else pushes to the branch: the remote's tip is their commit."""
    git(repo, "checkout", "--quiet", "-b", "theirs", branch)
    (repo / "theirs.txt").write_text("theirs\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Other Writer",
        "-c", "user.email=other@example.com",
        "commit", "--quiet", "-m", "their commit")
    sha = head(repo)
    git(repo, "push", "--quiet", "origin", f"{sha}:refs/heads/{branch}")
    git(repo, "checkout", "--quiet", branch)
    return sha


def competing_commit(repo, origin, branch="feat"):
    """A commit the origin holds, on top of the branch's tip.

    Somebody else pushed it; the push it belongs to is named `other`, so the
    object is in the origin's store and the branch itself is untouched.
    """
    git(repo, "checkout", "--quiet", "-b", "other", branch)
    (repo / "other.txt").write_text("other\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "other commit")
    sha = head(repo)
    git(repo, "push", "--quiet", "origin", f"{sha}:refs/heads/other")
    git(repo, "checkout", "--quiet", branch)
    return sha


def case_push_of_a_new_branch(failures):
    # A branch the remote has never seen: the push creates it, with no
    # lease to hold.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        git(repo, "checkout", "--quiet", "-b", "newbie")
        (repo / "newbie.txt").write_text("newbie\n")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "newbie")
        local = head(repo)
        install_hook(repo, origin)
        bindir, log = install_recording_git(tmpdir, repo, origin, "newbie")

        r = run_pr_push(repo, path=f"{bindir}:/usr/bin:/bin")

        assert_rc("new branch exit code", 0, r, failures)
        assert_eq("the remote holds the new branch", local,
                  remote_sha(origin, "newbie"), failures)
        assert_absent("the push of a new branch carried no lease",
                      "--force-with-lease", push_argv(log), failures)


def case_push_when_the_remote_tip_is_in_our_history(failures):
    # The remote's tip is in our history: the push is a fast-forward, and a
    # plain push makes it.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        make_branch(repo)
        (repo / "more.txt").write_text("more\n")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "more")
        local = head(repo)
        base = git(repo, "merge-base", "origin/main", "HEAD").stdout.strip()
        install_hook(repo, origin)
        bindir, log = install_recording_git(tmpdir, repo, origin, "feat")

        r = run_pr_push(repo, path=f"{bindir}:/usr/bin:/bin")

        assert_rc("ancestor exit code", 0, r, failures)
        assert_eq("the remote holds the new head", local,
                  remote_sha(origin, "feat"), failures)
        assert_absent("the push over an ancestor carried no lease",
                      "--force-with-lease", push_argv(log), failures)

        assert_in("the new sha is printed", local, r.stdout, failures)
        assert_in("the merge base is printed", base, r.stdout, failures)
        assert_in("the diffstat is printed", "more.txt", r.stdout, failures)


def case_push_of_a_rewritten_branch(failures):
    # The remote's tip is a commit this branch held and rewrote: it may be
    # replaced, with the lease at the remote's own value.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        make_branch(repo)
        old = remote_sha(origin, "feat")
        rewrite_branch(repo)
        local = head(repo)
        install_hook(repo, origin)
        bindir, log = install_recording_git(tmpdir, repo, origin, "feat")

        r = run_pr_push(repo, path=f"{bindir}:/usr/bin:/bin")

        assert_rc("rewritten branch exit code", 0, r, failures)
        assert_eq("the remote holds the rewritten head", local,
                  remote_sha(origin, "feat"), failures)
        assert_in("the push carried the lease at the remote's value",
                  f"--force-with-lease=feat:{old}", push_argv(log), failures)


def case_push_refuses_a_commit_this_branch_never_held(failures):
    # Another writer pushed to this branch before pr-push ran. Their commit
    # is one this branch never held, so the push is refused and names it.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        make_branch(repo)
        theirs = other_writer_pushes(repo, origin)
        before = remote_sha(origin, "feat")
        install_hook(repo, origin)

        r = run_pr_push(repo)

        if r.returncode == 0:
            fail(failures, "the push over another writer's commit was allowed",
                 f"stdout={r.stdout!r} stderr={r.stderr!r}")
        assert_in("the refusal names their commit", theirs[:7], r.stderr, failures)
        assert_in("the refusal names their author", "Other Writer", r.stderr, failures)
        assert_in("the refusal names their subject", "their commit", r.stderr, failures)
        assert_eq("nothing was pushed", before, remote_sha(origin, "feat"), failures)


def case_push_refuses_when_the_remote_moved(failures):
    # The remote's branch moves between the check and the push: git's lease
    # refuses, and nothing is pushed.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        make_branch(repo)
        rewrite_branch(repo)
        theirs = competing_commit(repo, origin)
        install_hook(repo, origin)
        bindir, log = install_recording_git(tmpdir, repo, origin, "feat",
                                            compete=theirs)

        r = run_pr_push(repo, path=f"{bindir}:/usr/bin:/bin")

        if r.returncode == 0:
            fail(failures, "the push over a moved remote branch was allowed",
                 f"stdout={r.stdout!r} stderr={r.stderr!r}")
        assert_in("git's lease refused it", "stale info", r.stderr, failures)
        assert_eq("nothing was pushed", theirs,
                  remote_sha(origin, "feat"), failures)


def case_refuses_a_detached_head(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        make_branch(repo)
        before = remote_sha(origin, "feat")
        git(repo, "checkout", "--quiet", "--detach")
        install_hook(repo, origin)

        r = run_pr_push(repo)

        assert_rc("detached head exit code", 1, r, failures)
        assert_in("detached head named", "detached", r.stderr, failures)
        assert_eq("nothing was pushed", before, remote_sha(origin, "feat"), failures)


def case_refuses_the_default_branch(failures):
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        before = remote_sha(origin, "main")
        install_hook(repo, origin)

        r = run_pr_push(repo)

        assert_rc("default branch exit code", 1, r, failures)
        assert_in("default branch named", "default branch", r.stderr, failures)
        assert_eq("nothing was pushed", before, remote_sha(origin, "main"), failures)


def case_refuses_without_the_push_guard(failures):
    # The pre-push hook is the secret scan, so a push that skips it goes
    # out unscanned.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        make_branch(repo)
        before = remote_sha(origin, "feat")

        r = run_pr_push(repo)
        assert_rc("no hooks path exit code", 1, r, failures)
        assert_in("no hooks path names push-guard", "push-guard", r.stderr, failures)

        hooks = Path(tmpdir) / "hooks"
        hooks.mkdir()
        (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
        (hooks / "pre-push").chmod(0o755)
        git(repo, "config", "core.hooksPath", str(hooks))

        r = run_pr_push(repo)
        assert_rc("hook without push-guard exit code", 1, r, failures)
        assert_in("hook without push-guard named", "push-guard", r.stderr, failures)

        assert_eq("nothing was pushed", before, remote_sha(origin, "feat"), failures)


def case_the_hook_runs(failures):
    # The hook is what the tool refuses to push without, so it has to run.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo, origin = init_repo(tmpdir)
        make_branch(repo)
        record = install_hook(repo, origin)
        (repo / "more.txt").write_text("more\n")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "more")

        r = run_pr_push(repo)

        assert_rc("push exit code", 0, r, failures)
        if not record.is_file():
            fail(failures, "the pre-push hook did not run",
                 f"no record at {record}")
        else:
            recorded = record.read_text()
            if "hook ran" not in recorded:
                fail(failures, "the pre-push hook recorded nothing",
                     f"record: {recorded!r}")
            if "guard ran" not in recorded:
                fail(failures, "the pre-push hook did not run push-guard",
                     f"record: {recorded!r}")


def main():
    if not PR_PUSH.is_file():
        return 1
    failures: list = []
    for case in CASES:
        case(failures)
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS pr-push: a new branch and a tip in our history are plain "
          "pushes; a tip this branch held is replaced under a lease at the "
          "remote's value; a tip this branch never held is refused by name; "
          "a remote that moves in between is refused by git's lease; "
          "a detached HEAD, the default branch and a missing push-guard "
          "hook are refused before the push; "
          "the pre-push hook runs")
    return 0


CASES = (case_push_of_a_new_branch,
         case_push_when_the_remote_tip_is_in_our_history,
         case_push_of_a_rewritten_branch,
         case_push_refuses_a_commit_this_branch_never_held,
         case_push_refuses_when_the_remote_moved,
         case_refuses_a_detached_head,
         case_refuses_the_default_branch,
         case_refuses_without_the_push_guard,
         case_the_hook_runs)


if __name__ == "__main__":
    sys.exit(main())
