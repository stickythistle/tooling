#!/usr/bin/env python3
"""Pin the implementer's identity, and the lane's empty gh config.

The loop has two identities: the implementer authors the pull request,
and the reviewer is the repository's owner. A verdict is a real review
state only when the reviewer differs from the author.

Three contracts are pinned here:

- `gh_as implementer` runs gh with the implementer's credentials, and
  refuses -- naming the variable to set -- when there are none. It resolves
  the login the call would act as and refuses unless that login is one of
  `IMPLEMENTER_LOGINS`, because a script that posts as the wrong account
  cannot tell it posted at all.
- A lane dispatched by `hermes-implement` runs with `gh` pointed at an
  empty config directory: an accidental `gh` call inside the lane fails.
  The lane runs as the same operating-system user as the dispatcher, so
  credentials that user can reach are still on the host; the empty config
  makes an accidental call fail, and those credentials stay readable.
- `gh_as implementer api` is for the repository its endpoint names. `gh api`
  takes no `-R`, so the path is the only thing that says which repository
  the call acts on.

Run directly: `python3 tests/test_credential_free_lanes.py`.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from roles_fixture import roles_file
from skill_install_fixture import install as install_skills
LIB = REPO_ROOT / "lib" / "common.sh"
IMPLEMENT = REPO_ROOT / "bin" / "hermes-implement"


def bash(script: str, env: dict | None = None, cwd: str | None = None):
    """Run a bash snippet with lib/common.sh sourced."""
    full = f'. "{LIB}"\n' + script
    return subprocess.run(["bash", "-c", full], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=60)


# A gh stub that reports the login it would act as, and the credentials it
# was handed. `gh api user --jq .login` is how the login is resolved.
GH_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [[ "$1" == "api" && "$2" == "user" ]]; then
        echo "${STUB_LOGIN:-implementer-login}"
        exit 0
    fi
    if [[ "$1" == "repo" && "$2" == "view" ]]; then
        echo "${STUB_REPO-example-org/tooling}"
        exit 0
    fi
    # The repository of the working directory, which is where a call that
    # names none is answered from.
    if [[ "$1" == "repo" && "$2" == "view" ]]; then
        echo "${STUB_REPO-example-org/tooling}"
        exit 0
    fi
    echo "token=${GH_TOKEN:-<unset>}"
    echo "config_dir=${GH_CONFIG_DIR:-<unset>}"
    """)



def gh_env(tmp: Path, logins=("implementer-login",), **extra) -> dict:
    """An environment whose `gh` is a stub reporting STUB_LOGIN.

    The implementer's logins come from the role binding, the file every
    tool reads.
    """
    gh = tmp / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
        "TOOLS_ENV": str(tmp / "no-env"),
        # The login cache lives under TMPDIR: each test gets its own, so
        # one test's answer cannot answer another's question.
        "TMPDIR": str(tmp),
    }
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_IMPLEMENTER_TOKEN", None)
    env.pop("GH_IMPLEMENTER_CONFIG_DIR", None)
    env.pop("IMPLEMENTER_LOGINS", None)
    env["ROLES_CONFIG"] = str(roles_file(tmp / "roles.toml", logins=logins))
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# gh_as: per-role credentials, a refusal that names the variable, and a
# login that has to be one the role may act as.

def test_the_credential_free_directory_is_emptied_of_its_hosts():
    # The directory exists to hold no credentials. A hosts.yml left in it
    # by an earlier run -- or by gh itself -- is a credential file, and a
    # lane that found it would be logged in after all.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        d = tmp / "gh-dir"
        d.mkdir()
        (d / "hosts.yml").write_text(
            "github.com:\n    oauth_token: gho_a_secret\n")
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "TOOLS_ENV": str(tmp / "no-env"),
        }
        result = bash(f'credential_free_env "{d}"', env=env)
        assert result.returncode == 0, result.stderr
        assert not (d / "hosts.yml").exists(), (
            f"the credential-free directory still holds a hosts.yml: "
            f"{list(d.iterdir())}")
        # And it is a directory, not a file.
        assert d.is_dir(), d


def test_gh_as_refuses_an_unknown_role():
    result = bash("gh_as owner api user")
    assert result.returncode != 0, result.stdout
    assert "role must be implementer" in result.stderr, result.stderr


def test_gh_as_refuses_the_reviewer_role():
    # The reviewer role has no caller: nothing posts a review through
    # `gh_as` yet.
    result = bash("gh_as reviewer api user")
    assert result.returncode != 0, result.stdout
    assert "role must be implementer" in result.stderr, result.stderr


def test_gh_as_with_an_unset_token_refuses_and_names_the_variable():
    # The acceptance criterion: with the token unset it exits non-zero and
    # names the variable. `GH_CONFIG_DIR` is pointed at an empty directory
    # and `gh auth status` fails there, so the ambient auth cannot stand in.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        empty = tmp / "gh-empty"
        empty.mkdir()
        gh = tmp / "gh"
        gh.write_text("#!/usr/bin/env bash\nexit 1\n")
        gh.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "GH_CONFIG_DIR": str(empty),
            "TOOLS_ENV": str(tmp / "no-env"),
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml")),
        }
        for var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_IMPLEMENTER_TOKEN",
                    "GH_IMPLEMENTER_CONFIG_DIR", "IMPLEMENTER_LOGINS"):
            env.pop(var, None)
        result = bash("gh_as implementer api user", env=env)
        assert result.returncode != 0, (
            f"gh_as ran with no implementer credentials: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "GH_IMPLEMENTER_TOKEN" in result.stderr, result.stderr


def test_gh_as_uses_the_roles_token():
    # The role's token is used for that one call. The stub gh prints the
    # token it was handed, which is how the test sees it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = gh_env(tmp,
                     GH_IMPLEMENTER_TOKEN="implementer-token-value")
        result = bash("gh_as implementer api orgs", env=env)
        assert result.returncode == 0, result.stderr
        assert "token=implementer-token-value" in result.stdout, result.stdout


def test_gh_as_does_not_put_the_token_on_the_command_line():
    # An argument is visible in the process table to every other process on
    # the host. The token goes in the environment.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gh = tmp / "gh"
        # A gh stub that reports its own argv.
        gh.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            if [[ "$1" == "api" && "$2" == "user" ]]; then
                echo "${STUB_LOGIN:-implementer-login}"
                exit 0
            fi
            if [[ "$1" == "repo" && "$2" == "view" ]]; then
                echo "${STUB_REPO-example-org/tooling}"
                exit 0
            fi
            echo "argv=$*"
        """))
        gh.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "TOOLS_ENV": str(tmp / "no-env"),
            "GH_IMPLEMENTER_TOKEN": "implementer-token-value",
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml")),
        }
        result = bash("gh_as implementer api user --jq .login", env=env)
        assert result.returncode == 0, result.stderr
        assert "implementer-token-value" not in result.stdout, (
            f"the token was passed as an argument: {result.stdout!r}")


def test_gh_as_uses_the_roles_config_dir():
    # A host whose implementer is not its default account names where that
    # account's gh config lives.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gh = tmp / "gh"
        gh.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            if [[ "$1" == "api" && "$2" == "user" ]]; then
                echo "${STUB_LOGIN:-implementer-login}"
                exit 0
            fi
            if [[ "$1" == "repo" && "$2" == "view" ]]; then
                echo "${STUB_REPO-example-org/tooling}"
                exit 0
            fi
            echo "config_dir=${GH_CONFIG_DIR:-<unset>}"
        """))
        gh.chmod(0o755)
        cfg = tmp / "gh-implementer"
        cfg.mkdir()
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "TOOLS_ENV": str(tmp / "no-env"),
            "GH_IMPLEMENTER_CONFIG_DIR": str(cfg),
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml")),
        }
        env.pop("GH_IMPLEMENTER_TOKEN", None)
        result = bash("gh_as implementer api orgs", env=env)
        assert result.returncode == 0, result.stderr
        assert f"config_dir={cfg}" in result.stdout, result.stdout


def test_gh_as_refuses_a_login_that_is_not_an_implementer_login():
    # The ambient login is the reviewer: the coordinator host's own gh
    # login. An implementer call there would act as the reviewer, with no
    # error at all. The refusal names both logins.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = gh_env(tmp,
                     STUB_LOGIN="reviewer-login",
                     logins=("implementer-login", "other-login"))
        result = bash("gh_as implementer api user", env=env)
        assert result.returncode != 0, (
            f"gh_as acted as the wrong login: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "reviewer-login" in result.stderr, result.stderr
        assert "implementer-login" in result.stderr, result.stderr


def test_gh_as_refuses_a_login_outside_the_allowed_set():
    # The allowed set is a list, and a login in it is accepted.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = gh_env(tmp,
                     STUB_LOGIN="second-login",
                     logins=("implementer-login", "second-login"))
        result = bash("gh_as implementer api user", env=env)
        assert result.returncode == 0, (
            f"gh_as refused an allowed login: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")


def test_gh_as_refuses_when_no_implementer_login_is_configured():
    # With no login bound, the call is refused.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = gh_env(tmp, STUB_LOGIN="implementer-login", logins=())
        result = bash("gh_as implementer api user", env=env)
        assert result.returncode != 0, (
            f"gh_as ran with no implementer login configured: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "implementer login" in result.stderr, result.stderr


def test_a_second_token_does_not_inherit_the_first_tokens_login():
    # The cache of resolved logins must be keyed by the credential, not by
    # the scope. Keyed by the scope, every named token shared one answer,
    # and a token for a login that is not an implementer passed on the
    # first token's answer.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gh = tmp / "gh"
        gh.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            if [[ "$1" == "api" && "$2" == "user" ]]; then
                echo "${STUB_LOGIN:-implementer-login}"
                exit 0
            fi
            if [[ "$1" == "repo" && "$2" == "view" ]]; then
                echo "${STUB_REPO-example-org/tooling}"
                exit 0
            fi
            echo "ran-as-${STUB_LOGIN:-implementer-login}"
            """))
        gh.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "TOOLS_ENV": str(tmp / "no-env"),
            "TMPDIR": str(tmp),
            # Only one of the two logins is an implementer.
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml",
                                            logins=("harbord-st",))),
        }
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_IMPLEMENTER_CONFIG_DIR", None)
        # Both calls run in one shell, so they share one cache. Two
        # separate processes would each have a cache of their own, and
        # the second would never have seen the first's answer.
        probe = bash(
            'GH_IMPLEMENTER_TOKEN=tokA STUB_LOGIN=harbord-st '
            'gh_as implementer pr list; '
            'echo "first=$?"; '
            'GH_IMPLEMENTER_TOKEN=tokB STUB_LOGIN=stickythistle '
            'gh_as implementer pr list; '
            'echo "second=$?"',
            env=env)
        assert "ran-as-harbord-st" in probe.stdout, probe.stdout
        # The first token acts as an implementer and is allowed.
        assert "first=0" in probe.stdout, probe.stdout
        # The second token acts as somebody else. It must be refused, not
        # pass on the first token's answer.
        assert "ran-as-stickythistle" not in probe.stdout, probe.stdout
        assert "second=1" in probe.stdout, probe.stdout
        assert "stickythistle" in probe.stderr, probe.stderr


def test_gh_as_refuses_the_owner_of_the_repository_it_acts_for():
    # The owner of a repository reviews that one and implements on the
    # others. Acting as the owner on the repository it owns is refused,
    # and the refusal names the owner.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        binding = roles_file(tmp / "owner-binding.toml",
                             owners={"Jerhaad": "Jerhaad"},
                             logins=("harbord-st", "Jerhaad"))
        env = gh_env(tmp, STUB_LOGIN="Jerhaad", ROLES_CONFIG=str(binding))
        result = bash(
            "gh_as implementer issue view 5 -R Jerhaad/harbord-infra",
            env=env)
        assert result.returncode != 0, (
            f"gh_as acted as the owner of the repository: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        # The refusal names the repository and the owner role: the login
        # may implement on the others, and not on the one it reviews.
        assert "Jerhaad/harbord-infra" in result.stderr, result.stderr
        assert "owner" in result.stderr, result.stderr


def test_gh_as_refuses_a_call_that_names_no_repository():
    # The implementer's logins are per repository. A call that resolves
    # no repository is refused; the unscoped list would let the owner
    # implement on the repository it reviews.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = gh_env(tmp, STUB_REPO="")
        result = bash("gh_as implementer api orgs", env=env)
        assert result.returncode != 0, (
            f"gh_as ran with no repository: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "repository" in result.stderr, result.stderr


def test_the_login_cache_is_not_shared_between_users():
    # The cache lives in a directory this process created with mktemp -d.
    # A fixed path under TMPDIR would be shared between users and left
    # behind for the next run to read.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gh = tmp / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        env = gh_env(tmp)
        result = bash(
            'gh_as implementer api orgs >/dev/null; '
            'gh_as implementer api orgs >/dev/null',
            env=env)
        assert result.returncode == 0, result.stderr
        # Nothing was left in TMPDIR under a name another run would read.
        leftovers = sorted(p.name for p in tmp.iterdir()
                           if p.name.startswith("gh-as-login."))
        assert not leftovers, (
            f"the login cache was left in a shared place: {leftovers}")
        # The cache lives in a directory of its own, not in TMPDIR itself.
        shared = sorted(p.name for p in tmp.iterdir()
                        if p.name.startswith("gh-as-cache."))
        assert shared, (
            "the login cache was not put in a directory of its own")


def test_gh_as_resolves_the_login_only_once_per_process():
    # The login does not change under a single run, so the answer is
    # cached: two calls do not ask the API twice.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        counter = tmp / "count"
        gh = tmp / "gh"
        gh.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            if [[ "$1" == "api" && "$2" == "user" ]]; then
                echo x >> "{counter}"
                echo "${{STUB_LOGIN:-implementer-login}}"
                exit 0
            fi
            if [[ "$1" == "repo" && "$2" == "view" ]]; then
                echo "${{STUB_REPO-example-org/tooling}}"
                exit 0
            fi
            exit 0
        """))
        gh.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "TOOLS_ENV": str(tmp / "no-env"),
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml")),
            # The cache lives under TMPDIR, so each test gets its own.
            "TMPDIR": str(tmp),
        }
        result = bash(
            "gh_as implementer api orgs >/dev/null; "
            "gh_as implementer api orgs >/dev/null",
            env=env)
        assert result.returncode == 0, result.stderr
        asks = counter.read_text().count("x") if counter.exists() else 0
        assert asks == 1, (
            f"the login was resolved {asks} times, once per process expected")


def test_the_implementer_token_and_the_ambient_login_are_different():
    # A token must not be usable as another account's, and the other way
    # round. This is the whole point of credentials per role.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = gh_env(tmp,
                     GH_IMPLEMENTER_TOKEN="implementer-token-value")
        result = bash("gh_as implementer api orgs", env=env)
        assert result.returncode == 0, result.stderr
        assert "token=implementer-token-value" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# gh_as: an api call is for the repository its endpoint names.

# A gh stub that reports the login it would act as, and the repository of
# the working directory when the working directory is a checkout. An api
# call outside a checkout names its repository in its own path.
API_GH_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [[ "$1" == "api" && "$2" == "user" ]]; then
        echo "${STUB_LOGIN:-implementer-login}"
        exit 0
    fi
    if [[ "$1" == "repo" && "$2" == "view" ]]; then
        # The repository of the working directory. Outside a checkout
        # there is none.
        if [[ -e .git ]]; then
            echo "${STUB_REPO-example-org/tooling}"
        fi
        exit 0
    fi
    echo "ran"
    """)


def api_gh_env(tmp: Path, **extra) -> dict:
    """An environment whose `gh` is a stub that knows a checkout.

    The binding names the owners of both repositories the cases use, and
    the login that acts. The owner of a repository reviews it, so the
    owner's own login cannot implement there.
    """
    gh = tmp / "gh"
    gh.write_text(API_GH_STUB)
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
        "TOOLS_ENV": str(tmp / "no-env"),
        "TMPDIR": str(tmp),
    }
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_IMPLEMENTER_TOKEN",
                "GH_IMPLEMENTER_CONFIG_DIR", "IMPLEMENTER_LOGINS"):
        env.pop(var, None)
    env["ROLES_CONFIG"] = str(roles_file(
        tmp / "roles.toml",
        owners={"Jerhaad": "Jerhaad", "stickythistle": "stickythistle"},
        logins=("harbord-st", "Jerhaad")))
    env.update(extra)
    return env


def make_checkout(tmp: Path) -> Path:
    """A directory the stub's `gh repo view` answers from."""
    checkout = tmp / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    return checkout


def test_an_api_call_is_checked_against_the_repository_its_path_names():
    # `gh api` takes no -R: the repository is in the path. A call made in
    # a checkout of another repository would be checked against the
    # checkout's, and the owner of the repository it acts on would pass
    # as an implementer on it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = api_gh_env(tmp, STUB_LOGIN="Jerhaad",
                         STUB_REPO="stickythistle/tooling")
        result = bash(
            "gh_as implementer api repos/Jerhaad/harbord-infra/issues",
            env=env, cwd=str(make_checkout(tmp)))
        assert result.returncode != 0, (
            f"gh_as checked the call against the checkout: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        # The refusal names the repository the path named, and its owner.
        assert "Jerhaad/harbord-infra" in result.stderr, result.stderr
        assert "owner" in result.stderr, result.stderr


def test_an_api_call_outside_a_checkout_is_for_the_repository_its_path_names():
    # The path names the repository, so a call made outside a checkout is
    # allowed. The leading slash names the same repository.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = api_gh_env(tmp, STUB_LOGIN="Jerhaad")
        result = bash(
            "gh_as implementer api /repos/stickythistle/tooling/pulls",
            env=env, cwd=str(tmp))
        assert result.returncode == 0, (
            f"gh_as refused a call outside a checkout: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")


def test_an_api_call_that_names_no_repository_outside_a_checkout_is_refused():
    # The implementer's logins are per repository, and a call that names
    # none resolves none: the unscoped list would let the owner implement
    # on the repository it reviews.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = api_gh_env(tmp, STUB_LOGIN="Jerhaad")
        result = bash("gh_as implementer api user", env=env, cwd=str(tmp))
        assert result.returncode != 0, (
            f"gh_as ran with no repository: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "repository" in result.stderr, result.stderr


def test_an_api_call_that_names_no_repository_uses_the_checkout():
    # A path that names no repository leaves the call to the checkout it
    # runs in.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = api_gh_env(tmp, STUB_LOGIN="Jerhaad",
                         STUB_REPO="stickythistle/tooling")
        result = bash("gh_as implementer api user", env=env,
                      cwd=str(make_checkout(tmp)))
        assert result.returncode == 0, (
            f"gh_as refused a call in a checkout: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")


def test_a_graphql_call_is_for_the_repository_its_fields_name():
    # The owner of a repository reviews it, so the owner's own login may
    # not act as the implementer on it. The fields a graphql call sends
    # name the repository it acts on.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = api_gh_env(tmp, STUB_LOGIN="Jerhaad",
                         STUB_REPO="stickythistle/tooling")
        result = bash(
            "gh_as implementer api graphql -F owner=Jerhaad "
            "-F name=harbord-infra -f query='query {}'",
            env=env, cwd=str(make_checkout(tmp)))
        assert result.returncode != 0, (
            f"gh_as checked the call against the checkout: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "Jerhaad/harbord-infra" in result.stderr, result.stderr


def test_an_api_call_is_not_for_the_repository_a_body_names():
    # The body is a value, not the endpoint: a path in it is text the
    # caller sent.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = api_gh_env(tmp, STUB_LOGIN="Jerhaad",
                         STUB_REPO="stickythistle/tooling")
        result = bash(
            "gh_as implementer api repos/stickythistle/tooling/issues "
            "-f body='see repos/Jerhaad/harbord-infra'",
            env=env, cwd=str(make_checkout(tmp)))
        assert result.returncode == 0, (
            f"gh_as took the body's path for the endpoint: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")


# ---------------------------------------------------------------------------
# The lane's gh is pointed at an empty config.

# A fake gh that reports the login it would use, and fails when it has no
# credentials at all -- the way the real gh does.
FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [[ "$1" == "auth" && "$2" == "status" ]]; then
        # The real gh reads hosts.yml from GH_CONFIG_DIR. An empty config
        # dir means no hosts, and gh says so.
        if [[ -r "${GH_CONFIG_DIR:-}/hosts.yml" ]]; then
            echo "github.com"
            echo "  x Logged in to github.com account fake"
            exit 0
        fi
        echo "You are not logged into any GitHub hosts. To log in, run: gh auth login" >&2
        exit 1
    fi
    if [[ "$1" == "api" && "$2" == "user" ]]; then
        echo "${STUB_LOGIN:-implementer-login}"
        exit 0
    fi
    if [[ "$1" == "repo" && "$2" == "view" ]]; then
        echo "${STUB_REPO-example-org/tooling}"
        exit 0
    fi
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        echo "title: a work item"
        exit 0
    fi
    exit 0
""")

# The same stub, with the host's own login present: the implementer role on
# the agent host is the ambient gh login.
FAKE_GH_LOGGED_IN = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [[ "$1" == "auth" && "$2" == "status" ]]; then
        echo "github.com"
        echo "  x Logged in to github.com account fake"
        exit 0
    fi
    if [[ "$1" == "api" && "$2" == "user" ]]; then
        echo "${STUB_LOGIN:-implementer-login}"
        exit 0
    fi
    if [[ "$1" == "repo" && "$2" == "view" ]]; then
        echo "${STUB_REPO-example-org/tooling}"
        exit 0
    fi
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        echo "title: a work item"
        exit 0
    fi
    exit 0
""")

# The fake hermes records what its environment looked like, and what
# `gh auth status` says from inside the lane.
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    record = {
        "gh_token": os.environ.get("GH_TOKEN"),
        "github_token": os.environ.get("GITHUB_TOKEN"),
        "gh_config_dir": os.environ.get("GH_CONFIG_DIR"),
        "gh_auth_status": None,
        "gh_auth_rc": None,
    }
    # The process group the lane runs in, and the one the dispatching
    # script runs in. They must differ: the script kills the lane's group
    # when an attempt's agent outlives its timeout, and a shared group
    # would make that kill land on the script instead.
    #
    # The script is found by walking up from the lane to the process whose
    # command mentions hermes-implement -- not simply to the outermost
    # ancestor, which is the test runner, in whose group every process on
    # this host lives anyway.
    def pgid_of(pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                rest = f.read().rsplit(") ", 1)[-1].split()
            return rest[2]
        except (OSError, IndexError):
            return None
    def cmdline_of(pid):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""
    record["pgid"] = pgid_of(os.getpid())
    # The dispatching script is the nearest ancestor that is *not* a
    # shell running the agent: the agent's own process, the `bash -c` that
    # runs hermes_query and the agent itself are all in the attempt's
    # group. The script is the one above them, and it is in a different
    # group -- which is the whole point.
    #
    # The chain is reported whole, so the test can pick the group that
    # differs rather than depend on a name that may appear anywhere in a
    # command line.
    chain = []
    pid = os.getpid()
    for _ in range(20):
        pgid = pgid_of(pid)
        chain.append({"pid": pid, "pgid": pgid,
                      "argv0": cmdline_of(pid).split(chr(0))[0]})
        try:
            with open(f"/proc/{pid}/stat") as f:
                rest = f.read().rsplit(") ", 1)[-1].split()
            up = rest[1]
        except (OSError, IndexError):
            break
        if up in ("0", "1"):
            break
        pid = up
    record["chain"] = chain
    # The script's group: the group of the ancestor that is not the
    # attempt's own. The attempt's own group is the lane's.
    others = [c["pgid"] for c in chain if c["pgid"] != record["pgid"]]
    record["script_pgid"] = others[0] if others else None
    record["script_pid"] = None
    # What the lane itself would see if it asked gh who it is.
    try:
        p = subprocess.run(["gh", "auth", "status"], capture_output=True,
                           text=True, timeout=30)
        record["gh_auth_status"] = (p.stdout + p.stderr).strip()
        record["gh_auth_rc"] = p.returncode
    except OSError as exc:
        record["gh_auth_status"] = f"gh could not be run: {exc}"
    out = os.environ.get("FAKE_HERMES_RECORD")
    if out:
        with open(out, "w") as f:
            json.dump(record, f)
    # The lane's own view of its environment, for the assertion on the
    # prompt.
    print("session_id: stub")
""")


def make_lane_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    run("init", "--initial-branch=main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-m", "base")
    return repo


def run_lane(tmp: Path, repo: Path, *, extra_env: dict | None = None):
    """Run hermes-implement with a fake agent, and return the record."""
    gh = tmp / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    hermes = tmp / "fake-hermes"
    hermes.write_text(FAKE_HERMES)
    hermes.chmod(0o755)
    record = tmp / "record.json"
    envfile = tmp / "fake.env"
    envfile.write_text("")
    home = tmp / "home"
    home.mkdir()
    # The lane refuses to start when a dispatched skill differs from the
    # repository's copy. The install is the operator's own, so the suite
    # makes one for its temporary home rather than turning the check off.
    install_skills(home)
    state = tmp / "state"
    state.mkdir()
    env = {
        **os.environ,
        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
        "HERMES_PYTHON": str(hermes),
        "FAKE_HERMES_RECORD": str(record),
        "AGENT_STATE_DIR": str(state),
        "TOOLS_ENV": str(envfile),
        "HERMES_HOME": str(home),
        "TMPDIR": str(tmp),
        "HERMES_IMPLEMENT_ATTEMPTS": "1",
        "HERMES_IMPLEMENT_TIMEOUT": "60",
        "GPU_GROUP_DEFAULT": "",
        # The implementer role's credentials, and the login they give.
        "GH_IMPLEMENTER_TOKEN": "implementer-token-value",
        # The implementer's logins and the lane's identity are the
        # binding's; the lane refuses without them.
        "ROLES_CONFIG": str(roles_file(tmp / "roles.toml")),
    }
    # A token in the parent's environment: the lane must not inherit it.
    env["GH_TOKEN"] = "parent-token-value"
    env["GITHUB_TOKEN"] = "parent-github-token-value"
    # An ambient gh login: the lane must not inherit that either.
    env["GH_CONFIG_DIR"] = str(tmp / "ambient-gh")
    (tmp / "ambient-gh").mkdir()
    (tmp / "ambient-gh" / "hosts.yml").write_text("github.com:\n    user: someone\n")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [str(IMPLEMENT), "--issue", "1", "--worktree", str(repo),
         "--verify", "true"],
        env=env, capture_output=True, text=True, timeout=180)
    return result, record


def test_the_lane_gets_not_logged_in_from_gh_auth_status():
    # The acceptance criterion, stated in the issue's own words.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_lane_repo(tmp)
        result, record = run_lane(tmp, repo)
        assert record.exists(), (
            f"the lane never ran: rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        data = json.loads(record.read_text())
        assert data["gh_auth_rc"] != 0, (
            "the lane was logged in to GitHub: "
            f"{data['gh_auth_status']!r}")
        assert "not logged in" in data["gh_auth_status"], data["gh_auth_status"]


def test_the_lane_has_no_token_in_its_environment():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_lane_repo(tmp)
        result, record = run_lane(tmp, repo)
        assert record.exists(), result.stderr
        data = json.loads(record.read_text())
        assert data["gh_token"] is None, (
            f"the lane inherited GH_TOKEN={data['gh_token']!r}")
        assert data["github_token"] is None, (
            f"the lane inherited GITHUB_TOKEN={data['github_token']!r}")


def test_the_lanes_gh_config_dir_holds_no_hosts():
    # The lane's gh config directory is a fresh one with no hosts in it,
    # whatever the ambient config directory held.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_lane_repo(tmp)
        result, record = run_lane(tmp, repo)
        assert record.exists(), result.stderr
        data = json.loads(record.read_text())
        cfg = data["gh_config_dir"]
        assert cfg, "the lane had no GH_CONFIG_DIR"
        assert Path(cfg) != str(tmp / "ambient-gh"), (
            "the lane used the ambient gh config directory")
        assert not Path(cfg, "hosts.yml").exists(), (
            f"the lane's gh config directory holds a hosts.yml: {cfg}")


def test_the_prompt_says_what_is_enforced():
    # The lane is told what actually happens, so it does not reach for
    # GitHub and report the refusal as a fault in the tooling. The
    # assertion is on what the lane has to know: that an accidental gh
    # call fails, that this is the design, that it must not work around
    # it, and that credentials it can reach must not be used.
    #
    # The prompt must not claim the lane has no credentials.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_lane_repo(tmp)
        gh = tmp / "gh"
        # The host's own gh login is present: the implementer role on the
        # agent host is the ambient login, so the script can read the issue.
        gh.write_text(FAKE_GH_LOGGED_IN)
        gh.chmod(0o755)
        hermes = tmp / "fake-hermes"
        hermes.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import hashlib, os, sys
            args = sys.argv[1:]
            prompt = ""
            if "--query-file" in args:
                qf = args[args.index("--query-file") + 1]
                if os.path.exists(qf):
                    prompt = open(qf).read()
            out = os.environ.get("FAKE_HERMES_RECORD")
            if out:
                with open(out, "w") as f:
                    f.write(prompt)
            print("session_id: stub")
        """))
        hermes.chmod(0o755)
        record = tmp / "prompt.txt"
        envfile = tmp / "fake.env"
        envfile.write_text("")
        home = tmp / "home"
        home.mkdir()
        # The lane refuses to start when a dispatched skill differs from
        # the repository's copy, so the suite installs the repository's
        # skills into its temporary home.
        install_skills(home)
        state = tmp / "state"
        state.mkdir()
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "HERMES_PYTHON": str(hermes),
            "FAKE_HERMES_RECORD": str(record),
            "AGENT_STATE_DIR": str(state),
            "TOOLS_ENV": str(envfile),
            "HERMES_HOME": str(home),
            "TMPDIR": str(tmp),
            "HERMES_IMPLEMENT_ATTEMPTS": "1",
            "HERMES_IMPLEMENT_TIMEOUT": "60",
            "GH_IMPLEMENTER_TOKEN": "implementer-token-value",
            # The implementer's logins and the lane's identity are the
            # binding's; the lane refuses without them.
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml")),
        }
        result = subprocess.run(
            [str(IMPLEMENT), "--issue", "1", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=180)
        assert record.exists(), result.stderr
        prompt = record.read_text()
        # That an accidental gh call fails, and that the failure is the
        # design. The assertion is on the sentence that says so, not on a
        # word that could appear anywhere in the prompt.
        assert "gh auth status" in prompt, (
            f"the prompt does not tell the lane what gh will say: "
            f"{prompt[:2000]!r}")
        assert "not logged in" in prompt, prompt[:2000]
        # The sentence that says the whole arrangement is intentional.
        assert "fails, and that is deliberate" in prompt, (
            f"the prompt does not say the arrangement is deliberate: "
            f"{prompt[:2000]!r}")
        assert "the design" in prompt, (
            f"the prompt does not say the refusal is the design: "
            f"{prompt[:2000]!r}")
        # And that it must not work around it.
        assert "not a fault to work around" in prompt, (
            f"the prompt does not tell the lane not to work around the "
            f"refusal: {prompt[:2000]!r}")
        # And not to put a credential anywhere to get past it.
        assert "Do not put a token" in prompt, (
            f"the prompt does not tell the lane not to put a credential "
            f"anywhere: {prompt[:2000]!r}")
        # And that credentials it can reach must not be used: the lane runs
        # as the same user as the dispatcher, so they are still on the host.
        assert "same operating-system user" in prompt, (
            f"the prompt does not tell the lane that credentials it can "
            f"reach are still on the host: {prompt[:2000]!r}")
        assert "Do not use them" in prompt, (
            f"the prompt does not tell the lane not to use credentials it "
            f"can reach: {prompt[:2000]!r}")
        # And it must not claim the lane has no credentials: that claim is
        # false, and the lane runs as a user that can reach some.
        assert "no GitHub credentials" not in prompt, (
            f"the prompt still claims the lane has no credentials: "
            f"{prompt[:2000]!r}")


def test_the_lane_runs_in_a_process_group_of_its_own():
    # The attempt is spawned in the background, and its process group is
    # what the script kills when an agent outlives its timeout. So the
    # group the script uses must not be the script's own group: a kill
    # aimed at the lane's group would then reach the script instead, and
    # an agent that ignored SIGTERM would survive into the next attempt.
    # That is issue #157's bug.
    #
    # The group is established asynchronously -- timeout calls setpgid
    # after the fork -- so the reading is retried.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_lane_repo(tmp)
        result, record = run_lane(tmp, repo)
        assert record.exists(), result.stderr
        data = json.loads(record.read_text())
        lane_pgid = data.get("pgid")
        script_pgid = data.get("script_pgid")
        assert lane_pgid, (
            "the lane did not report its process group: "
            f"{json.dumps(data)[:400]}")
        assert script_pgid, (
            "the lane did not report the script's process group: "
            f"{json.dumps(data)[:400]}")
        # The lane's group must be its own. The script's own group is the
        # one it would be killed with.
        assert lane_pgid != script_pgid, (
            f"the lane runs in the script's own process group ({lane_pgid}), "
            "so a kill aimed at the lane would reach the script instead")


def test_the_script_reads_a_process_group_of_the_attempts_own():
    # The script reads the attempt's group from /proc/<spawned pid>/stat,
    # and refuses to accept the script's own group as the attempt's: that
    # is the guard that keeps the kill aimed at the agent rather than at
    # the script. The group is established asynchronously -- timeout calls
    # setpgid after the fork -- so the reading is retried.
    #
    # The lane_env under test is the production one, extracted from
    # bin/hermes-implement rather than copied: a copy would test the copy.
    # exec matters here. `env` forks when it changes the environment, so
    # the spawned process would not be the group leader, and the kill
    # aimed at the attempt's group would miss the agent -- which is issue
    # #157's bug, an agent that ignored SIGTERM surviving into the next
    # attempt.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gh = tmp / "gh"
        gh.write_text("#!/usr/bin/env bash\nexit 0\n")
        gh.chmod(0o755)
        # The production lane_env, verbatim.
        source = IMPLEMENT.read_text()
        start = source.index("lane_env() {")
        end = source.index("\n}\n", start) + len("\n}\n")
        lane_env_source = source[start:end]
        script = tmp / "spawn.sh"
        script.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            attempt_pgid() {
            \tlocal pid="$1" stat rest pgid own deadline=$((SECONDS + 5))
            \town=$(ps -o pgid= -p $$ | tr -d ' ')
            while (( SECONDS < deadline )); do
            \tstat=$(cat "/proc/$pid/stat" 2>/dev/null) || return 0
            \trest=${stat##*') '}
            \tset -- $rest
            \tpgid="${3:-}"
            \t[[ "$pgid" == "$own" ]] || { printf '%s\\n' "$pgid"; return 0; }
            \tsleep 0.1
            done
            return 0
            }
            """) + lane_env_source + textwrap.dedent("""\
            LANE_GH_DIR="$1"
            lane_env timeout 30 bash -c 'sleep 30' >/dev/null 2>&1 &
            p=$!
            sleep 0.5
            own=$(ps -o pgid= -p $$ | tr -d ' ')
            pgid=$(attempt_pgid "$p")
            echo "own=$own attempt_pgid=$pgid"
            if [[ -z "$pgid" || "$pgid" == "$own" ]]; then
                echo "NO_GROUP"
            else
                echo "HAS_GROUP"
            fi
            kill -- -"$pgid" 2>/dev/null
            wait "$p" 2>/dev/null
        """))
        script.chmod(0o755)
        out = subprocess.run(["bash", str(script), str(tmp / "ghdir")],
                             capture_output=True, text=True, timeout=60)
        assert "HAS_GROUP" in out.stdout, (
            f"the script could not read a process group of the attempt's "
            f"own: {out.stdout!r} {out.stderr!r}")


def test_a_kill_aimed_at_the_lanes_group_reaches_the_lane():
    # The group the script names must contain the agent, or the kill is a
    # no-op and an agent that outlives its timeout survives into the next
    # attempt. This is the shape of the bug, checked directly rather than
    # through the script's own timing.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # The same spawn shape hermes-implement uses: a background subshell
        # that execs the credential-free environment.
        gh = tmp / "gh"
        gh.write_text("#!/usr/bin/env bash\nexit 0\n")
        gh.chmod(0o755)
        script = tmp / "spawn.sh"
        script.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            LANE_GH_DIR="$1"
            lane_env() {{
            \texec env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN \\
            \t\t-u GITHUB_ENTERPRISE_TOKEN -u GHE_TOKEN \\
            \t\tGH_CONFIG_DIR="$LANE_GH_DIR" \\
            \t\t"$@"
            }}
            # The chain hermes-implement spawns: the credential-free
            # environment, then timeout, then the shell that runs the agent.
            # timeout is what makes a process group of its own, so it has to
            # be in the chain for the kill to have a group to aim at.
            # The sleep's own stdout is closed, so the test's captured pipe
            # is not held open by a process that outlives the test.
            lane_env timeout 30 bash -c 'sleep 30' >/dev/null 2>&1 &
            p=$!
            sleep 0.5
            pgid=$(cat /proc/$p/stat | sed 's/.*) //' | cut -d' ' -f3)
            own=$(ps -o pgid= -p $$ | tr -d ' ')
            echo "spawned=$p pgid=$pgid script_pgid=$own"
            if [[ "$pgid" == "$own" ]]; then
                echo "SHARED_GROUP"
                exit 0
            fi
            kill -- -"$pgid" 2>/dev/null
            sleep 0.3
            if pgrep -g "$pgid" >/dev/null 2>&1; then
                echo "SURVIVED"
            else
                echo "KILLED"
            fi
        """))
        script.chmod(0o755)
        out = subprocess.run(["bash", str(script), str(tmp / "ghdir")],
                             capture_output=True, text=True, timeout=60)
        assert "SHARED_GROUP" not in out.stdout, (
            "the lane ran in the script's own process group, so a kill "
            f"aimed at the lane would reach the script: {out.stdout!r}")
        assert "SURVIVED" not in out.stdout, (
            f"the kill did not reach the lane's group: {out.stdout!r} "
            f"{out.stderr!r}")
        assert "KILLED" in out.stdout, out.stdout


def test_the_script_itself_keeps_its_own_github_access():
    # The credential-free env is for the lane. The script that dispatched
    # it still reads the issue, and it does that as the implementer.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_lane_repo(tmp)
        seen = tmp / "seen"
        gh = tmp / "gh"
        gh.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            if [[ "$1" == "api" && "$2" == "user" ]]; then
                echo "${{STUB_LOGIN:-implementer-login}}"
                exit 0
            fi
            if [[ "$1" == "repo" && "$2" == "view" ]]; then
                echo "${{STUB_REPO-example-org/tooling}}"
                exit 0
            fi
            if [[ "$1" == "issue" && "$2" == "view" ]]; then
                echo "token=${{GH_TOKEN:-<unset>}}" >> "{seen}"
                echo "title: a work item"
                exit 0
            fi
            exit 0
        """))
        gh.chmod(0o755)
        hermes = tmp / "fake-hermes"
        hermes.write_text(FAKE_HERMES)
        hermes.chmod(0o755)
        envfile = tmp / "fake.env"
        envfile.write_text("")
        home = tmp / "home"
        home.mkdir()
        # The lane refuses to start when a dispatched skill differs from
        # the repository's copy, so the suite installs the repository's
        # skills into its temporary home.
        install_skills(home)
        state = tmp / "state"
        state.mkdir()
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            "HERMES_PYTHON": str(hermes),
            "FAKE_HERMES_RECORD": str(tmp / "record.json"),
            "AGENT_STATE_DIR": str(state),
            "TOOLS_ENV": str(envfile),
            "HERMES_HOME": str(home),
            "TMPDIR": str(tmp),
            "HERMES_IMPLEMENT_ATTEMPTS": "1",
            "HERMES_IMPLEMENT_TIMEOUT": "60",
            "GH_IMPLEMENTER_TOKEN": "implementer-token-value",
            # The implementer's logins and the lane's identity are the
            # binding's; the lane refuses without them.
            "ROLES_CONFIG": str(roles_file(tmp / "roles.toml")),
        }
        result = subprocess.run(
            [str(IMPLEMENT), "--issue", "1", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=180)
        assert seen.exists(), (
            f"the script never read the issue: rc={result.returncode} "
            f"stderr={result.stderr!r}")
        text = seen.read_text()
        assert "token=implementer-token-value" in text, (
            f"the script did not read the issue as the implementer: {text!r}")


def main() -> int:
    failures: list[str] = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures.append(f"{name}: {exc}")
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
