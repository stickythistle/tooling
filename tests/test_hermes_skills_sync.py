#!/usr/bin/env python3
"""Pin that the repository's skills reach the trees a lane loads them from.

A lane dispatches a skill by name and the agent resolves that name from the
profile's own tree. The comparison is the content of the skill directory.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC = REPO_ROOT / "bin" / "hermes-skills-sync"
COMMON = REPO_ROOT / "lib" / "common.sh"

SKILL = """---
name: swe-updater
version: 3.3.0
---

The rules a lane runs.
"""


def run(cmd, cwd=None, env=None):
    """Run a command and return the CompletedProcess. No shell: the
    arguments are the ones under test, not a string to be re-parsed."""
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(
        cmd, cwd=str(cwd or REPO_ROOT), env=e, capture_output=True, text=True
    )


def repo_version() -> str:
    """The version the repository's own copy of the skill carries.

    Read from the file, so a version bump in the repository cannot stale
    the suite.
    """
    for line in (REPO_ROOT / "skills" / "swe-updater" / "SKILL.md").read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("the repository's swe-updater names no version")


def write_skill(directory: Path, name: str, body: str = SKILL) -> Path:
    """A skill directory in the layout a tree keeps: <category>/<name>/."""
    d = directory / "software-development" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body)
    return d


def write_archived_skill(directory: Path, name: str, body: str = SKILL) -> Path:
    """A skill directory where an operator filed it: .archive/<name>/."""
    d = directory / ".archive" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body)
    return d


def make_home(tmp: Path, *, profiles=("minimax",)) -> Path:
    """A root home with a skills tree and the named profiles' trees."""
    home = tmp / "home"
    (home / "skills").mkdir(parents=True)
    for p in profiles:
        (home / "profiles" / p / "skills").mkdir(parents=True)
    return home


def sync(home: Path, *args, repo: Path = REPO_ROOT):
    """Run the sync against an isolated home. SKILLS_REPO names the
    repository whose skills are installed, so the test never installs from
    the operator's own checkout."""
    return run(
        [str(SYNC), *args],
        env={"HERMES_HOME": str(home), "SKILLS_REPO": str(repo)},
    )


def check_skill_versions(home: Path, profile: str, *names, repo: Path = REPO_ROOT):
    """Run the pre-dispatch check the way the drivers do, and return the
    CompletedProcess."""
    script = (
        f'. "{COMMON}"\n'
        f'check_skill_versions "{repo}" "{profile}" ' + " ".join(f'"{n}"' for n in names) + "\n"
    )
    return run(
        ["bash", "-c", script],
        env={"HERMES_HOME": str(home)},
    )


# --- the sync installs the repository's copy -------------------------------

def test_sync_installs_the_repository_skill_into_a_profile_tree():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        r = sync(home)
        assert r.returncode == 0, r.stderr
        installed = home / "profiles" / "minimax" / "skills" / "software-development"
        assert (installed / "swe-updater" / "SKILL.md").is_file(), r.stdout
        assert "installed" in r.stdout, r.stdout


def test_sync_installs_into_the_root_tree_too():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        r = sync(home)
        assert r.returncode == 0, r.stderr
        assert (home / "skills" / "software-development" / "swe-updater" / "SKILL.md").is_file()


def test_sync_replaces_a_copy_whose_text_differs_at_the_same_version():
    """A version-only comparison calls this tree current: the version is
    the same, so nothing is installed and the lane keeps running the old
    rules."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        # The stale copy carries the repository's own version, so only a
        # content comparison can tell the two apart.
        stale = write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            (REPO_ROOT / "skills" / "swe-updater" / "SKILL.md").read_text()
            + "\nThe OLD rules, appended.\n",
        )
        r = sync(home)
        assert r.returncode == 0, r.stderr
        text = (stale / "SKILL.md").read_text()
        assert "The OLD rules, appended." not in text, f"the stale copy survived:\n{text}"


def test_sync_replaces_a_copy_whose_version_differs():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        stale = write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        r = sync(home)
        assert r.returncode == 0, r.stderr
        assert f"version: {repo_version()}" in (stale / "SKILL.md").read_text()


def test_sync_leaves_a_skill_the_repository_does_not_ship_alone():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        local = write_skill(
            home / "profiles" / "minimax" / "skills",
            "my-own-skill",
            "---\nname: my-own-skill\nversion: 0.1.0\n---\n\nMine.\n",
        )
        r = sync(home)
        assert r.returncode == 0, r.stderr
        assert (local / "SKILL.md").is_file()
        assert "Mine." in (local / "SKILL.md").read_text()


def test_sync_updates_a_skill_in_place_where_the_tree_keeps_it():
    """A skill an operator filed under .archive/ is updated there, not
    re-created under the dispatch category."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        archived = home / "profiles" / "minimax" / "skills" / ".archive" / "swe-updater"
        archived.mkdir(parents=True)
        (archived / "SKILL.md").write_text("---\nname: swe-updater\nversion: 3.2.0\n---\n")
        r = sync(home)
        assert r.returncode == 0, r.stderr
        assert f"version: {repo_version()}" in (archived / "SKILL.md").read_text()
        assert not (home / "profiles" / "minimax" / "skills" / "software-development" / "swe-updater").exists()


# --- the check the drivers run before a dispatch ---------------------------

def test_check_refuses_a_copy_whose_text_differs_at_the_same_version():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            (REPO_ROOT / "skills" / "swe-updater" / "SKILL.md").read_text()
            + "\nThe OLD rules, appended.\n",
        )
        r = check_skill_versions(home, "minimax", "swe-updater")
        assert r.returncode != 0, f"the stale copy passed the check:\n{r.stdout}"


def test_check_refuses_a_copy_the_tree_never_carried():
    """A tree without the skill is a lane that cannot load it."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        r = check_skill_versions(home, "minimax", "swe-updater")
        assert r.returncode != 0, f"the missing copy passed the check:\n{r.stdout}"


def test_check_passes_a_copy_that_matches_the_repository():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        sync(home)
        r = check_skill_versions(home, "minimax", "swe-updater")
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


def test_check_ignores_a_skill_the_repository_does_not_ship():
    """There is nothing to compare against, so there is nothing to refuse."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        r = check_skill_versions(home, "minimax", "swe-triage")
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


def test_check_names_the_skill_it_refused():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        r = check_skill_versions(home, "minimax", "swe-updater")
        assert "swe-updater" in r.stderr, r.stderr


def test_check_ignores_whitespace_around_a_skill_name():
    """The agent splits a comma-separated list and trims each name, so the
    check has to see the same names the agent will."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        r = check_skill_versions(home, "minimax", " swe-updater ")
        assert r.returncode != 0, f"a padded name escaped the check:\n{r.stdout}"


def test_check_reads_the_profile_tree_named_by_the_profile():
    """The root tree and a profile's tree are separate copies. The check
    must read the one the lane loads: the profile's tree here holds the
    repository's copy and the root tree holds an older one."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        write_skill(
            home / "skills",
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            (REPO_ROOT / "skills" / "swe-updater" / "SKILL.md").read_text(),
        )
        r = check_skill_versions(home, "minimax", "swe-updater")
        assert r.returncode == 0, f"the root tree's copy decided the profile's:\n{r.stdout}"


def test_check_refuses_the_profile_tree_when_the_root_tree_is_current():
    """The root tree holds the repository's copy and the profile's tree does
    not. The profile is the one the lane loads."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        sync(home)
        stale = write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        r = check_skill_versions(home, "minimax", "swe-updater")
        assert r.returncode != 0, f"the profile's stale copy passed:\n{r.stdout}"
        assert "minimax" in r.stderr, r.stderr


def test_check_reads_the_root_tree_when_no_profile_is_named():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        write_skill(
            home / "skills",
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        r = check_skill_versions(home, "", "swe-updater")
        assert r.returncode != 0, f"the root tree's stale copy passed:\n{r.stdout}"


def test_check_finds_the_root_home_when_it_has_no_profiles_directory():
    """A root home without a profiles/ directory is still a root home. A
    layout that resolves it to the parent reads and writes /tmp."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = tmp / "home"
        (home / "skills").mkdir(parents=True)
        script = f'. "{COMMON}"\nhermes_root_home\n'
        r = run(["bash", "-c", script], env={"HERMES_HOME": str(home)})
        assert r.stdout.strip() == str(home), r.stdout


def test_check_leaves_the_callers_failed_variable_alone():
    """`failed` is the check's own flag. As a global it leaked into every
    script that sources lib/common.sh, so a second call in the same shell
    reported a mismatch that was not there."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        repo = tmp / "repo"
        (repo / "skills" / "swe-updater").mkdir(parents=True)
        (repo / "skills" / "swe-updater" / "SKILL.md").write_text(SKILL)
        script = (
            f'. "{COMMON}"\n'
            'failed=0\n'
            f'check_skill_versions "{repo}" "minimax" "swe-updater" || true\n'
            'echo "failed=${failed:-unset}"\n'
        )
        r = run(["bash", "-c", script], env={"HERMES_HOME": str(home)})
        assert "failed=0" in r.stdout, r.stdout


def test_check_prefers_the_live_copy_over_the_archived_one():
    """A tree can carry both. The archived copy sorts first, so taking the
    first match would aim the check at the archived copy and pass while the
    live copy stayed stale."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        tree = home / "profiles" / "minimax" / "skills"
        write_archived_skill(
            tree,
            "swe-updater",
            (REPO_ROOT / "skills" / "swe-updater" / "SKILL.md").read_text(),
        )
        write_skill(
            tree,
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        r = check_skill_versions(home, "minimax", "swe-updater")
        assert r.returncode != 0, f"the stale live copy passed the check:\n{r.stdout}"


def test_sync_updates_the_live_copy_when_the_tree_carries_an_archive_too():
    """The sync writes to the copy the check reads. With both present the
    live copy is the one that gets the repository's rules."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        tree = home / "profiles" / "minimax" / "skills"
        archived = write_archived_skill(
            tree, "swe-updater", "---\nname: swe-updater\nversion: 3.2.0\n---\n"
        )
        live = write_skill(
            tree, "swe-updater", "---\nname: swe-updater\nversion: 3.2.0\n---\n"
        )
        r = sync(home)
        assert r.returncode == 0, r.stderr
        assert f"version: {repo_version()}" in (live / "SKILL.md").read_text(), r.stdout
        assert "version: 3.2.0" in (archived / "SKILL.md").read_text(), (
            "the archived copy was updated instead of the live one"
        )


# --- --check reports without changing anything -----------------------------

def test_check_mode_reports_the_stale_copy_and_exits_3():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        stale = write_skill(
            home / "profiles" / "minimax" / "skills",
            "swe-updater",
            "---\nname: swe-updater\nversion: 3.2.0\n---\n",
        )
        r = sync(home, "--check")
        assert r.returncode == 3, f"rc={r.returncode}\n{r.stdout}"
        assert "swe-updater" in r.stdout, r.stdout
        assert "version: 3.2.0" in (stale / "SKILL.md").read_text(), "--check wrote"


def test_check_mode_exits_0_when_every_tree_is_current():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = make_home(tmp)
        sync(home)
        r = sync(home, "--check")
        assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}"


def main() -> int:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - the runner reports the error
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    if failures:
        print(f"{failures} failure(s)")
        return 1
    print(f"PASS {len(tests)} tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
