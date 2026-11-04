#!/usr/bin/env python3
"""Install the repository's skills into an isolated HERMES_HOME.

A driver refuses a dispatch whose skill differs from the repository's copy.
A suite that drives one gives it a home whose trees carry those skills. The
helper writes them with the operator's own install.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC = REPO_ROOT / "bin" / "hermes-skills-sync"


def install(home: Path, profiles=()) -> None:
    """Install the repository's skills into `home`'s root tree and into
    each named profile's tree."""
    (home / "skills").mkdir(parents=True, exist_ok=True)
    for profile in profiles:
        (home / "profiles" / profile / "skills").mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [str(SYNC)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(home),
            "HERMES_HOME": str(home),
            "SKILLS_REPO": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"hermes-skills-sync failed: {r.returncode}\n{r.stdout}\n{r.stderr}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: skill_install_fixture.py <hermes-home> [<profile> ...]", file=sys.stderr)
        return 2
    home = Path(argv[1])
    install(home, argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
