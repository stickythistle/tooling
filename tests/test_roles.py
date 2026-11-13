#!/usr/bin/env python3
"""Pin the role binding: one file, one module, every tool asking it.

The roles are the owner who reviews, the implementer who authors the pull
request and the teammate who checks the work. Who fills them is the
operator's business, so they live in one file and every tool reads it
through one module. A tool that reads its own environment variable has two
sources of truth, and they disagree the day the operator moves a host.

What is pinned here:

- an owner who implements on the other repositories is asked per
  repository, and the repository it owns is refused for it;
- the owners are mapped, never derived from the repository's name;
- a repository may bind its own commit identity, and every other
  repository takes the default;
- the teammate's candidates are the ones whose family differs from the
  implementer's, and the ones that also differ from the owner's come
  first;
- a missing file, a missing key, an unknown policy and a model with no
  family are refusals naming what to change;
- the shell assignments a bash caller evals, and the tools that read them.

Run directly: `python3 tests/test_roles.py`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import roles as roles_module  # noqa: E402
from roles_fixture import env_for, roles_file  # noqa: E402

MERGE_CHECK = REPO_ROOT / "bin" / "merge-check"
PR_LOOP_GATE = REPO_ROOT / "bin" / "pr-loop-gate"
ROLES_PY = REPO_ROOT / "lib" / "roles.py"
COMMON = REPO_ROOT / "lib" / "common.sh"

REVIEWER = "the-reviewer"
IMPLEMENTER = "example-lane"
IDENTITY = ("Lane Bot", "lane@example.invalid")


def fail(failures: list[str], msg: str, detail: str = "") -> None:
    failures.append(f"{msg}: {detail}" if detail else msg)


# --------------------------------------------------------------------------
# The owners, and the logins that may implement.

# The owner of a repository may implement on the others, and reviews the
# one it owns.
OWNERS = {"stickythistle": "stickythistle", "Jerhaad": "Jerhaad"}


def case_the_binding_loads_with_an_owner_who_implements(failures: list[str]) -> None:
    # The load keeps no cross-role check. An owner who implements on the
    # other repositories is a recorded decision, not a broken file.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", owners=OWNERS,
                          logins=("harbord-st", "Jerhaad"))
        try:
            roles_module.load(path)
        except roles_module.Refusal as exc:
            fail(failures, "owner implements: the binding refused", exc.detail)
            return


def case_the_owner_may_implement_on_another_repository(failures: list[str]) -> None:
    # The owner of one repository reviews that one, and implements on the
    # others. The two roles are told apart per repository.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", owners=OWNERS,
                          logins=("harbord-st", "Jerhaad"))
        loaded = roles_module.load(path)
        logins = loaded.implementer_logins("stickythistle/tooling")
        if logins != ["harbord-st", "Jerhaad"]:
            fail(failures, "owner implements: the logins", str(logins))


def case_the_owner_is_not_an_implementer_on_its_own_repository(
        failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", owners=OWNERS,
                          logins=("harbord-st", "Jerhaad"))
        loaded = roles_module.load(path)
        logins = loaded.implementer_logins("Jerhaad/harbord-infra")
        if logins != ["harbord-st"]:
            fail(failures, "owner's repository: the logins", str(logins))


def case_a_repository_whose_owner_is_the_only_login_refuses(
        failures: list[str]) -> None:
    # With the owner as the only login there is nobody to implement, and
    # the refusal names the repository and its owner.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", owners=OWNERS,
                          logins=("Jerhaad",))
        loaded = roles_module.load(path)
        try:
            loaded.implementer_logins("Jerhaad/harbord-infra")
        except roles_module.Refusal as exc:
            for wanted in ("Jerhaad/harbord-infra", "Jerhaad"):
                if wanted not in exc.detail:
                    fail(failures,
                         "only the owner: the refusal does not name it",
                         exc.detail)
            return
        fail(failures, "only the owner: it answered")


def case_the_owner_is_mapped_and_never_derived(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml",
                          owners={"example-org": "the-owner"})
        loaded = roles_module.load(path)
        if loaded.owner_login("example-org/tooling") != "the-owner":
            fail(failures, "owners: the mapped owner",
                 loaded.owner_login("example-org/tooling"))
        # The repository's own name is no login: an organisation whose
        # reviewer is another account still works.
        if loaded.owner_login("example-org/tooling") == "example-org":
            fail(failures, "owners: the owner was derived from the name")


def case_an_unmapped_owner_is_refused(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", owners={"example-org": "the-owner"})
        loaded = roles_module.load(path)
        try:
            loaded.owner_login("other-org/tooling")
        except roles_module.Refusal as exc:
            if "other-org/tooling" not in exc.detail:
                fail(failures, "unmapped owner: the refusal does not name the "
                     "repository", exc.detail)
            return
        fail(failures, "unmapped owner: it answered")


# --------------------------------------------------------------------------
# The lane identity.

def case_a_repository_may_bind_its_own_identity(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml",
                          identity_by_repo={
                              "example-org/dco-repo": ("Example Human",
                                                       "human@example.invalid")})
        loaded = roles_module.load(path)
        identity = loaded.lane_identity("example-org/dco-repo")
        if (identity.name, identity.email) != ("Example Human",
                                               "human@example.invalid"):
            fail(failures, "per-repo identity: the override",
                 f"{identity.name} <{identity.email}>")


def case_a_repository_without_one_takes_the_default(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", identity=IDENTITY,
                          identity_by_repo={
                              "example-org/dco-repo": ("Example Human",
                                                       "human@example.invalid")})
        loaded = roles_module.load(path)
        identity = loaded.lane_identity("example-org/other")
        if (identity.name, identity.email) != IDENTITY:
            fail(failures, "default identity: the identity",
                 f"{identity.name} <{identity.email}>")


# --------------------------------------------------------------------------
# The teammate's candidates.

FAMILIES = {"model-a": "a", "model-b": "b", "model-c": "c"}
CANDIDATES = [{"backend": "pi:one", "model": "model-a"},
              {"backend": "pi:two", "model": "model-b"},
              {"backend": "pi:three", "model": "model-c"}]


def case_the_candidates_are_ordered_by_family(failures: list[str]) -> None:
    # The implementer runs in family A, and the owner reviews in family B.
    # A candidate in the implementer's family is skipped; of the rest, the
    # one that differs from the owner's comes first.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", models=FAMILIES,
                          teammate=True, candidates=CANDIDATES,
                          owner_model="model-b")
        loaded = roles_module.load(path)
        eligible = loaded.teammate_candidates("model-a")
        names = [c.backend for c in eligible]
        if names != ["pi:three", "pi:two"]:
            fail(failures, "teammate: the candidates in preference order",
                 str(names))


def case_every_candidate_in_the_implementers_family_is_refused(
        failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", models=FAMILIES,
                          teammate=True,
                          candidates=[{"backend": "pi:one", "model": "model-a"},
                                      {"backend": "pi:two", "model": "model-a"}],
                          owner_model="model-b")
        loaded = roles_module.load(path)
        eligible = loaded.teammate_candidates("model-a")
        if eligible:
            fail(failures, "teammate: candidates in the implementer's family",
                 str([c.backend for c in eligible]))


def case_a_candidate_with_no_family_is_refused(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", models=FAMILIES,
                          teammate=True,
                          candidates=[{"backend": "pi:one", "model": "model-x"}],
                          owner_model="model-b")
        try:
            roles_module.load(path)
        except roles_module.Refusal as exc:
            if "model-x" not in exc.detail:
                fail(failures, "no family: the refusal does not name the model",
                     exc.detail)
            return
        fail(failures, "no family: the binding loaded")


# --------------------------------------------------------------------------
# What the file must say.

def case_a_missing_file_is_refused(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = tmp / "absent.toml"
        try:
            roles_module.load(path)
        except roles_module.Refusal as exc:
            if str(path) not in exc.detail:
                fail(failures, "missing file: the refusal does not name the "
                     "path", exc.detail)
            return
        fail(failures, "missing file: it loaded")


def case_a_file_without_an_implementer_is_refused(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = tmp / "roles.toml"
        path.write_text('[owners]\n"example-org" = "the-owner"\n')
        try:
            roles_module.load(path)
        except roles_module.Refusal as exc:
            if "implementer" not in exc.detail:
                fail(failures, "no implementer: the refusal does not name the "
                     "key", exc.detail)
            return
        fail(failures, "no implementer: the binding loaded")


def case_an_unknown_policy_is_refused(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", teammate=True, policy="whenever")
        try:
            roles_module.load(path)
        except roles_module.Refusal as exc:
            if "whenever" not in exc.detail:
                fail(failures, "policy: the refusal does not name it",
                     exc.detail)
            return
        fail(failures, "policy: the binding loaded")


# --------------------------------------------------------------------------
# The shell assignments a bash caller evals.

def case_the_shell_assignments_quote_the_identity(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        path = roles_file(tmp / "roles.toml", logins=(IMPLEMENTER,),
                          identity_by_repo={
                              "example-org/dco-repo": ("Example Human",
                                                       "human@example.invalid")})
        env = env_for(path)
        result = subprocess.run(
            ["python3", str(ROLES_PY), "shell", "example-org/dco-repo"],
            env=env, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            fail(failures, "shell: it refused", result.stderr)
            return
        # The assignments eval to the override identity, and a name with a
        # space survives the eval.
        (tmp / "assignments").write_text(result.stdout)
        probe = subprocess.run(
            ["bash", "-c", f'eval "$(cat "{tmp}/assignments")"; '
             'printf "%s|%s|%s\\n" "$IMPLEMENTER_LOGINS" "$LANE_GIT_NAME" '
             '"$LANE_GIT_EMAIL"'],
            env=env, capture_output=True, text=True, timeout=60)
        if probe.returncode != 0:
            fail(failures, "shell: the assignments did not eval", probe.stderr)
            return
        logins, name, email = probe.stdout.strip().split("|")
        if name != "Example Human" or email != "human@example.invalid":
            fail(failures, "shell: the identity", probe.stdout.strip())
        if logins != IMPLEMENTER:
            fail(failures, "shell: the implementer logins", logins)


def case_the_shell_assignments_refuse_a_missing_file(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = env_for(tmp / "absent.toml")
        result = subprocess.run(
            ["python3", str(ROLES_PY), "shell"],
            env=env, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            fail(failures, "shell: it ran with no file", result.stdout)
            return
        if str(tmp / "absent.toml") not in result.stderr:
            fail(failures, "shell: the refusal does not name the path",
                 result.stderr)


# --------------------------------------------------------------------------
# merge-check reads the binding from the file.

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    if args and args[0] == "pr":
        canned = json.load(open(os.environ["FAKE_GH_DATA"]))
        print(json.dumps({"statusCheckRollup":
                          canned.get("statusCheckRollup", [])}))
        sys.exit(0)
    canned = json.load(open(os.environ["FAKE_GH_DATA"]))
    path = args[1] if len(args) > 1 else ""
    if path.endswith("/reviews"):
        print(json.dumps(canned.get("reviews", [])))
    elif path.endswith("/issues/5/comments"):
        print(json.dumps(canned.get("issue_comments", [])))
    elif path.endswith("/pulls/5/comments"):
        print(json.dumps(canned.get("inline_comments", [])))
    else:
        print(json.dumps(canned.get("pr", {})))
    """)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_checkout(tmp: Path) -> tuple[Path, str]:
    """A clone with a feature branch, and the head sha.

    The origin is a path, so the repository the driver reads is the last
    two components of it: the temporary directory's name and `origin`.
    """
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main",
                    str(origin)], check=True)
    seed = tmp / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "config", "user.email", IDENTITY[1])
    git(seed, "config", "user.name", IDENTITY[0])
    (seed / "a.txt").write_text("one\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "base")
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/main")
    git(seed, "checkout", "-q", "-b", "feature")
    (seed / "a.txt").write_text("one\ntwo\n")
    (seed / "b.txt").write_text("new\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "Fix the thing\n\nSigned-off-by: "
                              "Lane Bot <lane@example.invalid>\n")
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/feature")
    git(seed, "push", "-q", str(origin), "HEAD:refs/pull/5/head")
    head = git(seed, "rev-parse", "HEAD")
    clone = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone, head


def case_merge_check_reads_the_binding_from_the_file(failures: list[str]) -> None:
    # No role variable is in the environment: the identity, the reviewer
    # and the implementer's logins all come from the file.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head = make_checkout(tmp)
        owner = tmp.name
        path = roles_file(tmp / "roles.toml",
                          owners={owner: REVIEWER},
                          logins=(IMPLEMENTER,),
                          identity=IDENTITY)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        gh = fakebin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        data = tmp / "gh.json"
        data.write_text(json.dumps({
            "pr": {"head": {"sha": head}, "base": {"ref": "main"}, "labels": []},
            "statusCheckRollup": [],
            "reviews": [{"state": "APPROVED", "commit_id": head, "id": 1,
                         "submitted_at": "2026-01-02T00:00:00Z",
                         "user": {"login": REVIEWER}}],
            # The implementer's own comment is the loop's, not a third
            # party's: the logins come from the file.
            "issue_comments": [{"user": {"login": IMPLEMENTER},
                                "created_at": "2026-01-03T00:00:00Z",
                                "body": "on it"}],
            "inline_comments": [],
        }))
        env = env_for(path,
                      PATH=f"{fakebin}:{os.environ.get('PATH', '')}",
                      FAKE_GH_DATA=str(data),
                      FAKE_GH_LOG=str(tmp / "gh.log"),
                      TOOLS_ENV=str(tmp / "no-env"))
        result = subprocess.run([str(MERGE_CHECK), "5"], cwd=str(clone),
                                env=env, capture_output=True, text=True,
                                timeout=120)
        if result.returncode != 0:
            fail(failures, "merge-check: a ready pull request was refused",
                 f"rc={result.returncode} stderr={result.stderr}")
            return
        if "READY" not in result.stdout:
            fail(failures, "merge-check: the output does not say READY",
                 result.stdout)


CASES = (
    case_the_binding_loads_with_an_owner_who_implements,
    case_the_owner_may_implement_on_another_repository,
    case_the_owner_is_not_an_implementer_on_its_own_repository,
    case_a_repository_whose_owner_is_the_only_login_refuses,
    case_the_owner_is_mapped_and_never_derived,
    case_an_unmapped_owner_is_refused,
    case_a_repository_may_bind_its_own_identity,
    case_a_repository_without_one_takes_the_default,
    case_the_candidates_are_ordered_by_family,
    case_every_candidate_in_the_implementers_family_is_refused,
    case_a_candidate_with_no_family_is_refused,
    case_a_missing_file_is_refused,
    case_a_file_without_an_implementer_is_refused,
    case_an_unknown_policy_is_refused,
    case_the_shell_assignments_quote_the_identity,
    case_the_shell_assignments_refuse_a_missing_file,
    case_merge_check_reads_the_binding_from_the_file,
)


def main() -> int:
    failures: list[str] = []
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
    print("PASS roles: one file, one module; an owner who implements "
          "elsewhere is asked per repository; the owners are mapped; a "
          "repository may bind its own identity; the teammate's candidates "
          "are ordered by family; the shell assignments eval to the binding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
