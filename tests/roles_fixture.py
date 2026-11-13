#!/usr/bin/env python3
"""A role binding for a test, and the environment that points at it.

The tools read the operator's role binding from one file, so a test that
drives one writes that file and points `$ROLES_CONFIG` at it. Every value
here is a placeholder the case names: nothing is taken from the host, and
nothing is derived from the repository a test happens to build.

The binding is where the lane's identity and the implementer's logins come
from, and a lane refuses to start without it.
"""
from __future__ import annotations

import os
from pathlib import Path

# The logins the fixture binds when the case names none.
DEFAULT_LOGINS = ("implementer-login",)

# The name and the email the fixture binds when the case names none.
DEFAULT_IDENTITY = ("Lane", "lane@example.invalid")

# The owners the fixture binds when the case names none: the owner of
# the repository the stub gh reports.
DEFAULT_OWNERS = {"example-org": "the-owner"}

# The variables the tools used to read. They are removed from a test's
# environment: a host that exports them would answer for the binding.
ROLE_VARIABLES = ("LANE_GIT_NAME", "LANE_GIT_EMAIL", "IMPLEMENTER_LOGINS",
                  "REVIEWER_LOGIN")


def quoted(value: str) -> str:
    """A TOML string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def roles_file(path: Path, *, owners=DEFAULT_OWNERS, logins=DEFAULT_LOGINS,
               identity=DEFAULT_IDENTITY, identity_by_repo=None, models=None,
               teammate=False, policy="round-robin", candidates=None,
               backends=None, owner_model=None) -> Path:
    """Write a roles.toml, with every value the case names.

    A table a case leaves out is left out: the file under test is the one
    the case wrote, so a missing key is a missing key.
    """
    lines: list[str] = []
    if owners is not None:
        lines.append("[owners]")
        for owner, login in owners.items():
            lines.append(f"{quoted(owner)} = {quoted(login)}")
        lines.append("")
    lines.append("[implementer]")
    lines.append("logins = [" + ", ".join(quoted(l) for l in logins) + "]")
    name, email = identity
    lines.append(f"identity = {{ name = {quoted(name)}, "
                 f"email = {quoted(email)} }}")
    lines.append("")
    if identity_by_repo:
        for repo, (repo_name, repo_email) in identity_by_repo.items():
            lines.append(f"[implementer.identity_by_repo.{quoted(repo)}]")
            lines.append(f"name = {quoted(repo_name)}")
            lines.append(f"email = {quoted(repo_email)}")
            lines.append("")
    if models is not None:
        lines.append("[models]")
        for model, family in models.items():
            lines.append(f"{quoted(model)} = {quoted(family)}")
        lines.append("")
    if teammate:
        lines.append("[teammate]")
        lines.append(f"policy = {quoted(policy)}")
        if candidates is not None:
            lines.append("candidates = [")
            for candidate in candidates:
                lines.append("  { backend = "
                             f"{quoted(candidate['backend'])}, "
                             f"model = {quoted(candidate['model'])} }},")
            lines.append("]")
        lines.append("")
        # The backends are sub-tables of the teammate's, so they come
        # after it and before the next top-level table.
        for kind, body in (backends or {}).items():
            lines.append(f"[teammate.backends.{quoted(kind)}]")
            if "command" in body:
                lines.append(f"command = {quoted(body['command'])}")
            if "timeout" in body:
                lines.append(f"timeout = {body['timeout']}")
            lines.append("")
    if owner_model is not None:
        lines.append("[owner_model]")
        lines.append(f"model = {quoted(owner_model)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def env_for(path: Path, **extra) -> dict:
    """An environment that points at the binding and holds no role.

    The role variables are removed: a host that exports them would
    otherwise answer for the file.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ROLE_VARIABLES + ("ROLES_CONFIG", "AGENT_TOOLS_CONFIG")}
    env["ROLES_CONFIG"] = str(path)
    env.update(extra)
    return env
