#!/usr/bin/env python3
"""The loop's roles live in one file every tool reads.

Who fills each role is the operator's business: `~/.config/agent-tools/roles.toml`,
or the file `$ROLES_CONFIG` names. The owners are mapped, never derived.

Run directly: `python3 lib/roles.py shell [REPO]`.
"""
from __future__ import annotations

import os
import shlex
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The settings file's name, under the operator's configuration directory.
CONFIG = "roles.toml"

# The policies a teammate selection may name. An unknown policy is a
# refusal: picking one for the operator would decide who reviews.
POLICIES = ("round-robin", "fixed")


class Refusal(Exception):
    """A condition the tools cannot honor.

    `key` names what refused, so the message says which thing to change.
    """

    def __init__(self, key: str, detail: str):
        super().__init__(detail)
        self.key = key
        self.detail = detail


@dataclass(frozen=True)
class Identity:
    """The name and email a commit carries."""

    name: str
    email: str


@dataclass(frozen=True)
class Candidate:
    """One teammate: the backend it runs on and the model it runs."""

    backend: str
    model: str


@dataclass(frozen=True)
class Roles:
    """The operator's bindings, read from one file."""

    path: Path
    owners: dict[str, str]
    logins: tuple[str, ...]
    identity: Identity
    identity_by_repo: dict[str, Identity]
    models: dict[str, str]
    policy: str
    candidates: tuple[Candidate, ...]
    owner_model: str

    def owner_login(self, repo: str) -> str:
        """The login that reviews for `repo`.

        The owner half of `owner/name` is the key. An unmapped owner is a
        refusal naming the repository: guessing would approve pull requests
        as the wrong person.
        """
        owner = repo.split("/", 1)[0] if repo else ""
        login = self.owners.get(owner)
        if not login:
            raise Refusal(
                "owners",
                f"{self.path}: no owner login for {repo!r}: add one under "
                f"[owners] for {owner!r}")
        return login

    def implementer_logins(self, repo: str) -> list[str]:
        """The logins the implementer role may act as, for `repo`.

        The repository's owner reviews it, so the owner is left out of the
        list here and stays in it for the repositories it owns none of.
        """
        owner = self.owner_login(repo)
        logins = [login for login in self.logins if login != owner]
        if not logins:
            raise Refusal(
                "implementer",
                f"{self.path}: no implementer login for {repo!r}: its owner "
                f"{owner!r} is the only login")
        return logins

    def lane_identity(self, repo: str | None = None) -> Identity:
        """The identity a lane's commits must carry: the repository's own
        binding when it has one, else the default. A repository whose
        commits are signed off by a person names that person here.
        """
        if repo:
            override = self.identity_by_repo.get(repo)
            if override is not None:
                return override
        return self.identity

    def family(self, model: str) -> str:
        """The family a model belongs to.

        A model with no family is a refusal: the separation rule is about
        families, and a model with none cannot be told apart from any
        other.
        """
        found = self.models.get(model)
        if not found:
            raise Refusal(
                "models",
                f"{self.path}: no family for model {model!r}: add one under "
                "[models]")
        return found

    def teammate_candidates(self, implementer_model: str) -> list[Candidate]:
        """The teammates eligible for a run, in preference order: the
        implementer's family is skipped, because the teammate is a second
        pair of eyes. A candidate whose family differs from the owner's
        comes first; within each group the file's order holds.
        """
        implementer_family = self.family(implementer_model)
        # The owner's family is a preference, not a rule: a binding that
        # names no owner model still has eligible candidates.
        owner_family = self.models.get(self.owner_model) if self.owner_model else None
        eligible = [c for c in self.candidates
                    if self.family(c.model) != implementer_family]
        return sorted(eligible,
                      key=lambda c: 0 if owner_family is None
                      or self.family(c.model) != owner_family else 1)


def config_path() -> Path:
    """Where the bindings live."""
    override = os.environ.get("ROLES_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("AGENT_TOOLS_CONFIG")
    if base:
        return Path(base).expanduser() / CONFIG
    return Path.home() / ".config" / "agent-tools" / CONFIG


def _table(raw: dict, name: str, path: Path) -> dict:
    """One table of the file. A missing table is an empty one."""
    body = raw.get(name, {})
    if not isinstance(body, dict):
        raise Refusal("config", f"{path}: [{name}] is not a table")
    return body


def _identity(body: dict, what: str, path: Path) -> Identity:
    """A name and an email, both present and both non-empty."""
    if not isinstance(body, dict):
        raise Refusal("config", f"{path}: {what} is not a table")
    name = body.get("name")
    email = body.get("email")
    for field, value in (("name", name), ("email", email)):
        if not isinstance(value, str) or not value:
            raise Refusal("config", f"{path}: {what} does not name a {field}")
    return Identity(name, email)


def load(path: Path | None = None) -> Roles:
    """The operator's bindings.

    Anything the tools cannot act on is a refusal naming the key. There is
    no default: the file says who fills each role, and a guess about that
    is a guess about who reviews a pull request.
    """
    path = Path(path) if path is not None else config_path()
    if not path.is_file():
        raise Refusal("config", f"{path} does not exist")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Refusal("config", f"{path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise Refusal("config", f"{path}: {exc}") from exc

    owners = {}
    for owner, login in _table(raw, "owners", path).items():
        if not isinstance(login, str) or not login:
            raise Refusal("owners",
                          f"{path}: [owners] {owner!r} does not name a login")
        owners[owner] = login

    if "implementer" not in raw:
        raise Refusal("implementer", f"{path}: [implementer] is missing")
    implementer = _table(raw, "implementer", path)
    if "logins" not in implementer:
        raise Refusal("implementer",
                      f"{path}: [implementer] does not name logins")
    logins = implementer["logins"]
    if not isinstance(logins, list):
        raise Refusal("implementer",
                      f"{path}: [implementer] logins is not a list")
    for login in logins:
        if not isinstance(login, str) or not login:
            raise Refusal("implementer",
                          f"{path}: [implementer] logins holds an empty login")

    if "identity" not in implementer:
        raise Refusal("implementer",
                      f"{path}: [implementer] does not name an identity")
    identity = _identity(implementer["identity"], "[implementer] identity", path)

    identity_by_repo = {}
    for repo, body in _table(implementer, "identity_by_repo", path).items():
        identity_by_repo[repo] = _identity(
            body, f"[implementer.identity_by_repo.{repo!r}]", path)

    models = {}
    for model, family in _table(raw, "models", path).items():
        if not isinstance(family, str) or not family:
            raise Refusal("models",
                          f"{path}: [models] {model!r} does not name a family")
        models[model] = family

    teammate = _table(raw, "teammate", path)
    policy = ""
    if "teammate" in raw:
        if "policy" not in teammate:
            raise Refusal("teammate",
                          f"{path}: [teammate] does not name a policy")
        policy = teammate["policy"]
    if policy and policy not in POLICIES:
        raise Refusal(
            "teammate",
            f"{path}: [teammate] policy {policy!r} is not one of "
            f"{', '.join(POLICIES)}")
    raw_candidates = teammate.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise Refusal("teammate", f"{path}: [teammate] candidates is not a list")
    candidates = []
    for entry in raw_candidates:
        if not isinstance(entry, dict):
            raise Refusal("teammate",
                          f"{path}: [teammate] candidates holds an entry that "
                          "is not a table")
        backend = entry.get("backend")
        model = entry.get("model")
        for field, value in (("backend", backend), ("model", model)):
            if not isinstance(value, str) or not value:
                raise Refusal(
                    "teammate",
                    f"{path}: [teammate] candidate does not name a {field}")
        if model not in models:
            raise Refusal(
                "teammate",
                f"{path}: [teammate] candidate {model!r} has no family: add "
                "one under [models]")
        candidates.append(Candidate(backend, model))

    owner_model = ""
    owner_table = _table(raw, "owner_model", path)
    if "model" in owner_table:
        model = owner_table["model"]
        if not isinstance(model, str) or not model:
            raise Refusal("owner_model",
                          f"{path}: [owner_model] does not name a model")
        owner_model = model

    return Roles(
        path=path,
        owners=owners,
        logins=tuple(logins),
        identity=identity,
        identity_by_repo=identity_by_repo,
        models=models,
        policy=policy,
        candidates=tuple(candidates),
        owner_model=owner_model,
    )


# ----- the answers, for a caller that wants them without holding the file ---

def owner_login(repo: str) -> str:
    """The login that reviews for `repo`."""
    return load().owner_login(repo)


def implementer_logins(repo: str) -> list[str]:
    """The logins the implementer role may act as, for `repo`."""
    return load().implementer_logins(repo)


def lane_identity(repo: str | None = None) -> Identity:
    """The identity a lane's commits must carry."""
    return load().lane_identity(repo)


def family(model: str) -> str:
    """The family a model belongs to."""
    return load().family(model)


def teammate_candidates(implementer_model: str) -> list[Candidate]:
    """The teammates eligible for a run, in preference order."""
    return load().teammate_candidates(implementer_model)


def shell_assignments(roles: Roles, repo: str | None = None) -> str:
    """The bindings as shell assignments, quoted for `eval`.

    The three a bash caller needs: the implementer's logins for the
    repository, and the name and email a lane's commits carry. The logins
    are empty without a repository: nobody may act.
    """
    identity = roles.lane_identity(repo)
    logins = " ".join(roles.implementer_logins(repo) if repo else [])
    return "\n".join([
        f"IMPLEMENTER_LOGINS={shlex.quote(logins)}",
        f"LANE_GIT_NAME={shlex.quote(identity.name)}",
        f"LANE_GIT_EMAIL={shlex.quote(identity.email)}",
    ])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("shell", "owner"):
        sys.exit("usage: roles.py shell [REPO] | roles.py owner REPO")
    repo = argv[1].strip() if len(argv) > 1 else ""
    try:
        roles = load()
        if argv[0] == "owner":
            print(roles.owner_login(repo))
            return 0
        print(shell_assignments(roles, repo or None))
    except Refusal as exc:
        # The message on stderr and a non-zero exit: the bash caller
        # stops on it, and the operator sees what to change.
        sys.exit(exc.detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
