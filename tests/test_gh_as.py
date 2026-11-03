#!/usr/bin/env python3
"""Pin gh_as: one gh call, one role's credentials, and no others.

`gh_as` is the seam between a role and the account it acts as. The two
things that can go wrong are both about identity: a call that runs with
the wrong account's token, and a call that runs at all when the role's
token is unset -- falling back to whatever credentials gh happens to
have, which posts a review as the implementer.

The suite drives the real helper with a stub `gh` on PATH. The stub
records the environment it was called with, so the assertion is over
what the call actually saw and not over what the helper looks like it
does. No network.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON = REPO_ROOT / "lib" / "common.sh"

# A stub `gh` that records its argv and the GH_TOKEN it was given.
STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DATA/gh.log"
printf '%s\n' "${GH_TOKEN:-unset}" >> "$STUB_DATA/gh.token"
exit "${STUB_EXIT:-0}"
"""


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def stub_dir(tmp: Path) -> Path:
    """A directory holding the stub `gh` and its data file."""
    data = tmp / "data"
    data.mkdir()
    (data / "gh.log").write_text("")
    (data / "gh.token").write_text("")
    bindir = tmp / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(STUB)
    gh.chmod(0o755)
    return data


def run_helper(tmp: Path, data: Path, script: str,
               env_extra: dict | None = None):
    """Run a snippet with the real common.sh sourced and the stub gh first
    on PATH."""
    env = dict(os.environ)
    env["PATH"] = f"{tmp / 'bin'}:{env['PATH']}"
    env["STUB_DATA"] = str(data)
    # The helper sources the operator's env file, which on this machine
    # carries real tokens. A leaked token would make the suite's
    # assertions about something else.
    env["TOOLS_ENV"] = str(tmp / "no-such-env")
    for name in ("REVIEWER_GH_TOKEN", "IMPLEMENTER_GH_TOKEN",
                 "REVIEWER_LOGIN", "IMPLEMENTER_LOGIN"):
        env.pop(name, None)
    if env_extra:
        env.update(env_extra)
    # `bash -c SCRIPT NAME ARG...`: the script's own positional parameters
    # start after NAME, so the repository root reaches it as $1.
    return subprocess.run(["bash", "-c", script, "_", str(REPO_ROOT)],
                          capture_output=True, text=True, env=env,
                          cwd=str(REPO_ROOT))


def case_the_roles_own_token_is_the_one_used(failures):
    """Each role's call runs with that role's token."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        data = stub_dir(tmp)
        result = run_helper(
            tmp, data,
            '. "$1/lib/common.sh"\n'
            'gh_as reviewer api user\n'
            'gh_as implementer api user\n',
            {"REVIEWER_GH_TOKEN": "token-for-the-reviewer",
             "IMPLEMENTER_GH_TOKEN": "token-for-the-implementer"})
        if result.returncode != 0:
            fail(failures, "role-token exit", result.returncode)
            return
        tokens = data.joinpath("gh.token").read_text().splitlines()
        if tokens != ["token-for-the-reviewer",
                      "token-for-the-implementer"]:
            fail(failures, "role-token: the tokens the calls ran with",
                 tokens)


def case_the_token_does_not_leak_into_the_shell(failures):
    """The token is set for that one call. The shell that called it has
    no GH_TOKEN of its own, so a later gh call cannot borrow it."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        data = stub_dir(tmp)
        result = run_helper(
            tmp, data,
            '. "$1/lib/common.sh"\n'
            'gh_as reviewer api user\n'
            'gh api user\n',
            {"REVIEWER_GH_TOKEN": "token-for-the-reviewer"})
        if result.returncode != 0:
            fail(failures, "no-leak exit", result.returncode)
            return
        tokens = data.joinpath("gh.token").read_text().splitlines()
        if tokens != ["token-for-the-reviewer", "unset"]:
            fail(failures, "no-leak: the second call inherited the token",
                 tokens)


def case_an_unset_token_refuses_and_names_the_variable(failures):
    """A role with no token refuses, and says which variable to set. A gh
    call that fell back to the stored credentials would post as whichever
    account happens to be logged in."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        data = stub_dir(tmp)
        for role, var in (("reviewer", "REVIEWER_GH_TOKEN"),
                          ("implementer", "IMPLEMENTER_GH_TOKEN")):
            result = run_helper(
                tmp, data,
                f'. "$1/lib/common.sh"\ngh_as {role} api user\n')
            if result.returncode == 0:
                fail(failures, f"unset-token[{role}]: exited 0")
                continue
            if var not in result.stderr:
                fail(failures, f"unset-token[{role}]: the refusal does not "
                     f"name {var}", result.stderr.strip()[:200])
            # The stub gh must not have run at all.
            logged = data.joinpath("gh.log").read_text().strip()
            if logged:
                fail(failures, f"unset-token[{role}]: gh ran anyway",
                     logged[:120])


def case_an_unknown_role_refuses(failures):
    """A role nobody defined is refused, not treated as the default."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        data = stub_dir(tmp)
        result = run_helper(
            tmp, data,
            '. "$1/lib/common.sh"\n'
            'gh_as nobody api user\n')
        if result.returncode == 0:
            fail(failures, "unknown-role: exited 0")
            return
        if "no such role" not in result.stderr:
            fail(failures, "unknown-role: the refusal does not say what is "
                 "wrong", result.stderr.strip()[:200])


def case_no_command_is_refused(failures):
    """`gh_as` with no gh command is a usage error, not an empty call."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        data = stub_dir(tmp)
        result = run_helper(
            tmp, data,
            '. "$1/lib/common.sh"\ngh_as reviewer\n',
            {"REVIEWER_GH_TOKEN": "a-token"})
        if result.returncode == 0:
            fail(failures, "no-command: exited 0")
            return
        if data.joinpath("gh.log").read_text().strip():
            fail(failures, "no-command: gh ran anyway")


def case_the_exit_code_is_the_commands(failures):
    """A failed call is a failed call: the caller's `if` must see it."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        data = stub_dir(tmp)
        result = run_helper(
            tmp, data,
            '. "$1/lib/common.sh"\n'
            'gh_as reviewer api user\n'
            'echo "rc=$?"\n',
            {"REVIEWER_GH_TOKEN": "a-token", "STUB_EXIT": "7"})
        if "rc=7" not in result.stdout:
            fail(failures, "exit-code: the call's status was not the "
                 "caller's", result.stdout.strip()[:200])


def case_the_role_login_comes_from_the_env(failures):
    """The login is the operator's, and an unset one is a refusal rather
    than an empty string to compare against."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        data = stub_dir(tmp)
        result = run_helper(
            tmp, data,
            '. "$1/lib/common.sh"\ngh_role_login reviewer\n',
            {"REVIEWER_LOGIN": "the-reviewer"})
        if result.returncode != 0:
            fail(failures, "login exit", result.returncode)
            return
        if result.stdout.strip() != "the-reviewer":
            fail(failures, "login: the login printed",
                 result.stdout.strip())

        result = run_helper(
            tmp, data,
            '. "$1/lib/common.sh"\ngh_role_login reviewer\n')
        if result.returncode == 0:
            fail(failures, "login-unset: exited 0")
            return
        if "REVIEWER_LOGIN" not in result.stderr:
            fail(failures, "login-unset: the refusal does not name the "
                 "variable", result.stderr.strip()[:200])


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
    print("PASS gh_as: the role's own token; no leak into the shell; unset "
          "token refused; unknown role; no command; exit code; the login")
    return 0


CASES = (case_the_roles_own_token_is_the_one_used,
         case_the_token_does_not_leak_into_the_shell,
         case_an_unset_token_refuses_and_names_the_variable,
         case_an_unknown_role_refuses,
         case_no_command_is_refused,
         case_the_exit_code_is_the_commands,
         case_the_role_login_comes_from_the_env)


if __name__ == "__main__":
    sys.exit(main())
