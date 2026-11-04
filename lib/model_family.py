#!/usr/bin/env python3
"""Refuse a review by the model that wrote the code.

A reviewer earns its round trip by being a different model family from
whatever wrote the code, so its false negatives are uncorrelated with
the author's. A reviewer that is the author's own family reviews the
code the same way the author would, and agrees with it, and the loop
believes it.

So the reviewer and the implementer must not be the same family, and the
tool refuses before the model runs rather than after the findings come
back. A refusal before the run costs a second; a verdict that is worth
nothing costs the whole loop.

**The family is declared, not guessed.** A model name says what the
model is called, not what family it belongs to: `muse-glimmer-30b-dual`
and `muse-glimmer-1m` are one family, `gemma4-12b` and `gemma4-27b` are
another, and no rule about suffixes reads that out of the name without
also getting `qwen3.6` and `qwen3.8` wrong. So the operator declares the
families:

    REVIEWER_MODEL_FAMILY=muse-glimmer
    IMPLEMENTER_MODEL_FAMILY=qwen3

Two declarations that are equal are the same family. Two that are
unequal are different families, whatever the model names say: the
operator knows which models they are, and a tool that second-guesses
them is worse than one that takes their word for it.

A declaration that is missing is a refusal, not a guess. The profile's
model name is read from its own `config.yaml` so the refusal can say
which model it could not place, and so a caller that wants the default
can ask for it.

The profile's model is read from the file, so the check never starts a
model and never reaches the network.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

USAGE = ("usage: model_family.py --reviewer PROFILE --implementer PROFILE")


def die(message: str) -> "NoReturn":
    import sys
    sys.stderr.write(message + "\n")
    sys.exit(2)


def profiles_dir() -> Path:
    """Where the profiles live. `HERMES_PROFILES_DIR` overrides it, for a
    test and for an operator whose profiles are elsewhere."""
    override = os.environ.get("HERMES_PROFILES_DIR")
    if override:
        return Path(override)
    return Path.home() / ".hermes" / "profiles"


def read_model(profile_dir: Path) -> str | None:
    """The default model of a profile, from its own config.yaml.

    The file is read as text rather than parsed as YAML: this tool is
    stdlib-only, and the model line is a flat `default: <name>` under a
    `model:` key. A file it cannot read is a profile it cannot judge,
    and that is a refusal rather than a guess.
    """
    config = profile_dir / "config.yaml"
    if not config.is_file():
        return None
    in_model = False
    for raw in config.read_text(errors="replace").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            # A top-level key. The model block ends here.
            in_model = line.split(":", 1)[0].strip() == "model"
            continue
        if not in_model:
            continue
        stripped = line.strip()
        if stripped.startswith("default:") or stripped.startswith("model:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    return None


def profile_model(profile: str) -> str:
    """The model a profile runs, or an empty string when it cannot be
    read."""
    return read_model(profiles_dir() / profile) or ""


def declared_family(profile: str, override_var: str) -> str:
    """The family the operator declared for a profile."""
    return (os.environ.get(override_var, "") or "").strip()


def check(reviewer: str, implementer: str) -> str | None:
    """A refusal reason, or None when the two are different families.

    The check refuses on what it can prove and stays quiet on what it
    cannot: a family the operator has not declared is a family nobody
    has said, and refusing to run is better than pretending the two
    differ.
    """
    if reviewer == implementer:
        return (f"the reviewer and the implementer are the same profile "
                f"({reviewer}); a reviewer that is the author reviews the "
                f"code the way the author would")

    reviewer_family = declared_family(reviewer, "REVIEWER_MODEL_FAMILY")
    implementer_family = declared_family(
        implementer, "IMPLEMENTER_MODEL_FAMILY")

    if not reviewer_family:
        return (f"no model family is declared for the reviewer's profile "
                f"'{reviewer}'"
                + (f", which runs {profile_model(reviewer)}"
                   if profile_model(reviewer) else "")
                + ". Set REVIEWER_MODEL_FAMILY to the family it belongs to.")
    if not implementer_family:
        return (f"no model family is declared for the implementer's profile "
                f"'{implementer}'"
                + (f", which runs {profile_model(implementer)}"
                   if profile_model(implementer) else "")
                + ". Set IMPLEMENTER_MODEL_FAMILY to the family it belongs "
                  "to.")

    if reviewer_family == implementer_family:
        return (f"the reviewer ({reviewer}: {reviewer_family}) and the "
                f"implementer ({implementer}: {implementer_family}) are "
                f"declared to be the same model family; a reviewer from "
                f"the author's own family agrees with the author")
    return None


def main() -> int:
    import sys

    args = sys.argv[1:]
    reviewer = ""
    implementer = ""
    i = 0
    while i < len(args):
        if args[i] == "--reviewer" and i + 1 < len(args):
            reviewer = args[i + 1]
            i += 2
        elif args[i] == "--implementer" and i + 1 < len(args):
            implementer = args[i + 1]
            i += 2
        else:
            die(USAGE)
    if not reviewer or not implementer:
        die(USAGE)

    reason = check(reviewer, implementer)
    if reason is not None:
        sys.stderr.write(f"model_family: refusing: {reason}\n")
        return 2
    print(f"model_family: {reviewer} ({os.environ.get('REVIEWER_MODEL_FAMILY')})"
          f" and {implementer} ({os.environ.get('IMPLEMENTER_MODEL_FAMILY')})"
          f" are different model families")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
