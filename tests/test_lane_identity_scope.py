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
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON = REPO_ROOT / "lib" / "common.sh"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from roles_fixture import roles_file  # noqa: E402


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def git_out(cwd, *args):
    return git(cwd, *args).stdout.strip()


def binding(tmp: Path, name: str = "Lane",
            email: str = "lane@example.invalid") -> Path:
    """A role binding naming the lane's identity."""
    return roles_file(tmp / "roles.toml", identity=(name, email))


def verify(worktree: Path, base: str, default: str = "main",
           roles: Path | None = None):
    """Run the real verifier over a worktree.

    The identity is the binding's, and the binding is named by the
    environment: an exported identity would leak into every later
    command in the shell.
    """
    env = dict(os.environ)
    if roles is not None:
        env["ROLES_CONFIG"] = str(roles)
    script = f'''
. "{COMMON}"
verify_lane_identity "{worktree}" "{base}" "" "{default}"
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True,
                            text=True, env=env)
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

        result = verify(repo.path, lane_base, roles=binding(Path(t)))
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

        result = verify(repo.path, lane_base, roles=binding(Path(t)))
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
        again = verify(repo.path, lane_base, roles=binding(Path(t)))
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
        result = verify(repo.path, lane_base, roles=binding(Path(t)))
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
        result = verify(repo.path, lane_base, roles=binding(Path(t)))
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
verify_lane_identity "{repo.path}" "{lane_base}" "" main
'''
        env = dict(os.environ)
        env["ROLES_CONFIG"] = str(binding(Path(t)))
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, env=env)
        if result.returncode == 0:
            fail(failures, "refuse: exited 0")
            return
        if "refusing" not in result.stderr:
            fail(failures, "refuse: the refusal is not named",
                 result.stderr[:200])


def lane_env(env: dict, tmp: Path) -> dict:
    """The environment a lane-identity case runs under.

    `common.sh` sources the operator's env file, which may pin an
    identity on a host that has one, and it is sourced after the case's
    own environment is in place -- so the case would test the host, not
    the function.

    TOOLS_ENV therefore names an empty file of the case's own, and the
    binding under test is the only thing that can set the identity.
    """
    clean = {k: v for k, v in os.environ.items()
             if not (k.startswith("GIT_AUTHOR_")
                     or k.startswith("GIT_COMMITTER_")
                     or k.startswith("LANE_GIT_"))}
    fake_env = tmp / "fake.env"
    fake_env.write_text("")
    return {**clean, "TOOLS_ENV": str(fake_env), **env}


def expected_identity(env: dict, tmp: Path) -> subprocess.CompletedProcess:
    """Run the real `lane_expected_identity` under `env`.

    The env carries the binding under test. Everything else the shell
    would inherit is cleared, so a parent process that has a binding
    cannot leak one into the case.
    """
    script = f'''
. "{COMMON}"
lane_expected_identity
'''
    return subprocess.run(["bash", "-c", script], env=lane_env(env, tmp),
                          capture_output=True, text=True)


def export_identity(env: dict, tmp: Path) -> subprocess.CompletedProcess:
    """Run the real `export_lane_identity_env` under `env`.

    Reports the pinned identity on stdout, so a case can tell a pinned
    lane from one that started without an identity.
    """
    script = f'''
. "{COMMON}"
export_lane_identity_env || exit $?
printf 'AUTHOR=%s <%s>\\n' "$GIT_AUTHOR_NAME" "$GIT_AUTHOR_EMAIL"
'''
    return subprocess.run(["bash", "-c", script], env=lane_env(env, tmp),
                          capture_output=True, text=True)


def case_the_binding_names_the_identity(failures):
    """The binding names the identity, and the identity resolves.

    Nothing in the environment says what it is: the binding is the
    operator's, and it is the only source.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = binding(tmp)
        env = {"ROLES_CONFIG": str(path)}
        result = expected_identity(env, tmp)
        if result.returncode != 0:
            fail(failures, "bound: exit", result.returncode)
            fail(failures, "bound: stderr", result.stderr[-300:])
            return
        if result.stdout.strip() != "Lane|lane@example.invalid":
            fail(failures, "bound: the identity", result.stdout.strip())
            return
        # The exporter pins it, and the lane starts with it.
        pinned = export_identity(env, tmp)
        if pinned.returncode != 0:
            fail(failures, "bound: export exit", pinned.returncode)
            fail(failures, "bound: export stderr", pinned.stderr[-300:])
            return
        if "AUTHOR=Lane <lane@example.invalid>" not in pinned.stdout:
            fail(failures, "bound: the pinned identity", pinned.stdout)


def case_a_binding_without_a_name_is_refused(failures):
    """A binding that names no name is refused, and the refusal names
    the key."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = tmp / "roles.toml"
        path.write_text('[implementer]\nlogins = ["lane"]\n'
                        'identity = { email = "lane@example.invalid" }\n')
        result = expected_identity({"ROLES_CONFIG": str(path)}, tmp)
        if result.returncode != 1:
            fail(failures, "no name: exit", result.returncode)
            return
        if "name" not in result.stderr:
            fail(failures, "no name: the refusal does not name the key",
                 result.stderr[-300:])


def case_a_binding_with_an_empty_name_is_refused(failures):
    """A binding whose name is empty is refused, and the refusal names
    the key."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = tmp / "roles.toml"
        path.write_text('[implementer]\nlogins = ["lane"]\n'
                        'identity = { name = "", email = "lane@example.invalid" }\n')
        result = expected_identity({"ROLES_CONFIG": str(path)}, tmp)
        if result.returncode != 1:
            fail(failures, "empty name: exit", result.returncode)
            return
        if "name" not in result.stderr:
            fail(failures, "empty name: the refusal does not name the key",
                 result.stderr[-300:])


def case_a_missing_binding_is_refused(failures):
    """No binding: refused, and the refusal names the path."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = tmp / "absent.toml"
        result = expected_identity({"ROLES_CONFIG": str(path)}, tmp)
        if result.returncode != 1:
            fail(failures, "no binding: exit", result.returncode)
            return
        if str(path) not in result.stderr:
            fail(failures, "no binding: the refusal does not name the path",
                 result.stderr[-300:])


def case_the_checkouts_own_configuration_is_not_the_identity(failures):
    """The issue's acceptance row: with no binding and the checkout's own
    `user.name` set, the identity still refuses.

    A wrong host configuration -- a scratch identity left behind by a CI
    workflow -- must not pin every lane to itself and pass its own check.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = Repo(tmp)
        # The wrong host configuration, set in the worktree.
        git(repo.path, "config", "user.name", "gates")
        git(repo.path, "config", "user.email", "gates@localhost")
        result = expected_identity({"ROLES_CONFIG": str(tmp / "absent.toml")},
                                   tmp)
        if result.returncode != 1:
            fail(failures, "worktree config: exit", result.returncode)
            fail(failures, "worktree config: stderr", result.stderr[-300:])
            return
        # The wrong configuration must not leak into stdout either: the
        # function refuses, so it prints no identity at all.
        if "gates" in result.stdout:
            fail(failures, "worktree config: the wrong configuration was used",
                 result.stdout)
        # And the exporter refuses too, so no lane starts with it.
        pinned = export_identity({"ROLES_CONFIG": str(tmp / "absent.toml")}, tmp)
        if pinned.returncode != 1:
            fail(failures, "worktree config: the export exit",
                 pinned.returncode)
            return
        if "AUTHOR=" in pinned.stdout:
            fail(failures, "worktree config: a lane was pinned anyway",
                 pinned.stdout)


def case_the_refusal_starts_no_lane(failures):
    """A refusal stops the caller: the exporter returns non-zero and pins
    nothing, so a lane that would have committed as the wrong host
    configuration never starts."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = Repo(tmp)
        git(repo.path, "config", "user.name", "gates")
        git(repo.path, "config", "user.email", "gates@localhost")
        pinned = export_identity({"ROLES_CONFIG": str(tmp / "absent.toml")}, tmp)
        if pinned.returncode == 0:
            fail(failures, "no lane: the export exited 0")
            return
        if "AUTHOR=" in pinned.stdout:
            fail(failures, "no lane: a lane was pinned anyway", pinned.stdout)


def case_the_identity_functions_take_no_worktree(failures):
    """The identity is configuration alone: neither function takes a
    worktree, and neither reads one.

    A worktree argument with no use is an argument a reader has to work
    out the meaning of, and it invites a caller to believe the identity
    comes from the tree. The functions are called with no arguments and
    the source declares no worktree parameter.
    """
    source = COMMON.read_text()
    for fn in ("lane_expected_identity", "export_lane_identity_env"):
        line = next((ln for ln in source.splitlines()
                     if ln.strip().startswith(f"{fn}()")), None)
        if line is None:
            fail(failures, f"no worktree: {fn} is not defined")
            continue
        if "<worktree>" in line:
            fail(failures, f"no worktree: {fn} still declares a worktree",
                 line.strip())
        if "wt" in line and fn == "lane_expected_identity":
            fail(failures, f"no worktree: {fn} still names a worktree",
                 line.strip())

    # Called with no argument, the identity still resolves.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = {"ROLES_CONFIG": str(binding(tmp))}
        result = expected_identity(env, tmp)
    if result.returncode != 0:
        fail(failures, "no worktree: exit", result.returncode)
        fail(failures, "no worktree: stderr", result.stderr[-300:])
        return
    if result.stdout.strip() != "Lane|lane@example.invalid":
        fail(failures, "no worktree: the identity", result.stdout.strip())


def case_no_caller_passes_a_worktree(failures):
    """No caller hands either function a worktree.

    The functions take a repository -- a slug, never a path -- because
    the binding names the identity per repository. A caller that passed
    a worktree would be asking the identity where to come from, which is
    the tree's business.
    """
    callers = {
        "lib/common.sh": ("lane_expected_identity", "export_lane_identity_env"),
        "bin/hermes-implement": ("export_lane_identity_env",),
        "pipeline/implement.sh": ("lane_expected_identity",
                                  "export_lane_identity_env"),
    }
    worktree_vars = ("wt", "WORKTREE", "worktree")
    for rel, fns in callers.items():
        path = REPO_ROOT / rel
        if not path.is_file():
            fail(failures, f"no caller: {rel} not found")
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for fn in fns:
                # The definition itself is not a caller.
                if stripped.startswith(f"{fn}()"):
                    continue
                # The call inside the other function's definition is a
                # call, and it is checked here.
                for match in re.finditer(re.escape(fn), stripped):
                    rest = stripped[match.end():].lstrip()
                    if not rest.startswith("("):
                        # A shell call passes its arguments after a
                        # space: `fn "$arg"`.
                        arg = rest.split(None, 1)
                        if len(arg) == 2:
                            name = arg[0].strip('"\'')
                            if name.startswith("$") and name[1:] in worktree_vars:
                                fail(failures,
                                     f"no caller: {rel}:{lineno} passes a "
                                     "worktree",
                                     stripped)
                        continue
                    # Take the argument list, which may hold a nested
                    # pair of brackets.
                    depth = 0
                    args = ""
                    for ch in rest[1:]:
                        if ch == "(":
                            depth += 1
                        elif ch == ")":
                            if depth == 0:
                                break
                            depth -= 1
                        args += ch
                    for arg in args.split():
                        name = arg.strip('"\'')
                        if name.startswith("$") and name[1:] in worktree_vars:
                            fail(failures,
                                 f"no caller: {rel}:{lineno} passes a worktree",
                                 stripped)


def case_the_example_points_at_the_binding(failures):
    """`hosts.env.example` points at the role binding.

    The lane identity and the implementer's logins are the operator's
    binding now, so an operator following the template has to be told
    where to write them.
    """
    example = REPO_ROOT / "hosts.env.example"
    if not example.is_file():
        fail(failures, "example: hosts.env.example not found")
        return
    text = example.read_text()
    if "roles.toml.example" not in text:
        fail(failures, "example: the binding's example is not named")
    # The variables are gone: a tool that read one of them would be
    # reading a second source of truth.
    for var in ("LANE_GIT_NAME", "LANE_GIT_EMAIL", "IMPLEMENTER_LOGINS"):
        if var in text:
            fail(failures, f"example: {var} is still named")


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
          "unchanged; a clean lane is left alone; the refuse mode; the "
          "identity is the binding's and never inferred; a missing or "
          "incomplete binding refuses; the identity functions take no "
          "worktree; the example points at the binding")
    return 0


CASES = (case_a_lane_that_rebased_onto_a_newer_main,
         case_a_lane_that_merged_a_branch_with_foreign_authors,
         case_the_tree_is_unchanged,
         case_a_lane_with_the_right_identity_is_left_alone,
         case_the_refuse_mode_still_refuses,
         case_the_binding_names_the_identity,
         case_a_binding_without_a_name_is_refused,
         case_a_binding_with_an_empty_name_is_refused,
         case_a_missing_binding_is_refused,
         case_the_checkouts_own_configuration_is_not_the_identity,
         case_the_refusal_starts_no_lane,
         case_the_identity_functions_take_no_worktree,
         case_no_caller_passes_a_worktree,
         case_the_example_points_at_the_binding)


if __name__ == "__main__":
    sys.exit(main())
