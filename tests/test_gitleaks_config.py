#!/usr/bin/env python3
"""Pin the repository's .gitleaks.toml: the three fixture values pass the
secret scan, and a real secret in the same files is still found.

Four scans: this repository's history with the config finds nothing; a scratch
repository holding the three fixture files finds nothing with the config and
every fixture without it; a scratch repository holding the fixture files and a
real-shaped token planted in each finds every token.

The token is assembled at run time from parts; written out in full it would
put a secret-shaped string in this repository's own history, which the same
scan reads.

Run directly: `python3 tests/test_gitleaks_config.py`.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / ".gitleaks.toml"

# The scanner the guard runs, the same way an operator names it.
GITLEAKS = os.environ.get("PUSH_GUARD_GITLEAKS") or shutil.which("gitleaks")

# The three fixture files the config allowlists: a made-up key the proxy test
# checks never leaks, a made-up key the picker test forwards, a tokenizer hash.
FIXTURES = (
    "tests/test_bakeoff_proxy.py",
    "tests/test_pick_lane.py",
    "skills/llama-swap-setup/SKILL.md",
)

# The token the secret case plants, assembled at run time.
PAT_PREFIX = "gh" + "p_"
PAT_BODY = "kQ7zM2xB9wR4" * 3

IDENTITY = {"GIT_AUTHOR_NAME": "gitleaks-config-test",
            "GIT_AUTHOR_EMAIL": "gitleaks-config-test@example.com",
            "GIT_COMMITTER_NAME": "gitleaks-config-test",
            "GIT_COMMITTER_EMAIL": "gitleaks-config-test@example.com"}


def pat():
    """The planted token, built at run time."""
    return PAT_PREFIX + PAT_BODY


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def clean_env():
    """An environment with the git identity and nothing else that could
    change what the scan reads."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
            **IDENTITY}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, env=clean_env())


def has_ref(ref):
    """Whether the repository has the ref."""
    return git(REPO_ROOT, "rev-parse", "--verify", "--quiet",
               ref).returncode == 0


def scan(repo, config=None, log_opts=None):
    """Scan a repository's history. Returns (exit code, findings, stderr).

    A finding is (rule, file, line). The value that matched is never
    returned: the scan runs with --redact, and a lane's output is logged.
    """
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "report.json"
        cmd = [GITLEAKS, "git", "--redact", "--no-banner", "--exit-code", "3",
               "--report-format", "json", "--report-path", str(report)]
        if log_opts:
            cmd += ["--log-opts", log_opts]
        if config:
            cmd += ["--config", str(config)]
        cmd.append(str(repo))
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=clean_env(), cwd=str(repo))
        findings = []
        if report.is_file():
            for f in json.loads(report.read_text()):
                findings.append((f.get("RuleID", "?"), f.get("File", "?"),
                                 f.get("StartLine", 0)))
        return r.returncode, findings, r.stderr


def scratch_repo(tmp):
    """A git repository holding the three fixture files, in one commit.

    It holds no .gitleaks.toml: gitleaks reads one from the directory it
    scans, so the case that scans without the config needs none there.
    """
    repo = tmp / "scratch"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel in FIXTURES:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, dest)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "the fixtures")
    return repo


def plant(repo, rel):
    """Append the token to the file and commit it. Returns the line it
    landed on.

    The scan reads commits, not the working tree, so an uncommitted line
    would not be seen.
    """
    path = repo / rel
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines) + f"\nGITHUB_TOKEN = \"{pat()}\"\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "plant a token")
    return len(lines) + 1


def case_config_shape(failures):
    """The config extends the default rules, and every allowlist entry
    names one fixture's path and one regex for its value: an entry that
    allowlists a path alone passes any secret pasted into that file."""
    doc = tomllib.loads(CONFIG.read_text())
    if doc.get("extend", {}).get("useDefault") is not True:
        fail(failures, "the config does not extend the default rules")

    entries = doc.get("allowlists", [])
    if not entries:
        fail(failures, "the config allowlists nothing")
        return
    for entry in entries:
        label = str(entry.get("description", "?"))
        if entry.get("condition") != "AND":
            fail(failures, f"the allowlist entry {label!r} does not require "
                           "both its path and its regex")
        if not entry.get("paths") or not entry.get("regexes"):
            fail(failures, f"the allowlist entry {label!r} names no path or "
                           "no regex")
        named = [rel for rel in FIXTURES
                 if any(re.search(p, rel) for p in entry.get("paths", []))]
        if len(named) != 1:
            fail(failures, f"the allowlist entry {label!r} names "
                           f"{len(named)} fixture files, not one")
    for rel in FIXTURES:
        if not any(any(re.search(p, rel) for p in entry.get("paths", []))
                   for entry in entries):
            fail(failures, f"no allowlist entry names {rel}")


def case_history_is_clean(failures):
    """The scan over main's history, with the config, finds nothing: the
    three fixtures are allowlisted by value."""
    if not has_ref("origin/main"):
        fail(failures, "no origin/main ref to scan; fetch the remote")
        return
    for label, log_opts in (("origin/main", "origin/main"), ("HEAD", "HEAD")):
        rc, findings, err = scan(REPO_ROOT, config=CONFIG, log_opts=log_opts)
        if rc not in (0, 3):
            fail(failures, f"the scan over {label} did not run", err.strip())
            continue
        if findings:
            fail(failures, f"the scan over {label} found something",
                 ", ".join(f"{rule} {f}:{line}" for rule, f, line in findings))


def case_fixtures_pass_with_the_config(failures):
    """The three fixture files in a scratch repository pass the scan when
    the config is the one this repository ships."""
    with tempfile.TemporaryDirectory() as t:
        repo = scratch_repo(Path(t))
        rc, findings, err = scan(repo, config=CONFIG)
        if rc not in (0, 3):
            fail(failures, "the scan over the fixtures did not run",
                 err.strip())
            return
        if findings:
            fail(failures, "the config did not pass the three fixtures",
                 ", ".join(f"{rule} {f}:{line}" for rule, f, line in findings))


def case_fixtures_are_found_without_the_config(failures):
    """The same scratch repository without the config reports all three
    fixtures, so it is the allowlist that passes them."""
    with tempfile.TemporaryDirectory() as t:
        repo = scratch_repo(Path(t))
        rc, findings, err = scan(repo)
        if rc not in (0, 3):
            fail(failures, "the scan without the config did not run",
                 err.strip())
            return
        found = {f for _, f, _ in findings}
        for rel in FIXTURES:
            if rel not in found:
                fail(failures, f"the scan without the config missed {rel}")


def case_a_real_secret_in_a_fixture_is_found(failures):
    """A real-shaped token planted in a fixture file is still found: the
    allowlist names the fixture's own value, not its path."""
    with tempfile.TemporaryDirectory() as t:
        repo = scratch_repo(Path(t))
        planted = {rel: plant(repo, rel) for rel in FIXTURES}

        rc, findings, err = scan(repo, config=CONFIG)
        if rc not in (0, 3):
            fail(failures, "the scan over the planted token did not run",
                 err.strip())
            return
        if not findings:
            fail(failures, "the config passed a real token planted in a "
                           "fixture file")
            return
        for rel, line in planted.items():
            if not any(f == rel and start == line
                       for _, f, start in findings):
                fail(failures, f"the scan did not find the token in {rel}")
        allowed = set(planted.items())
        for _, f, line in findings:
            if (f, line) not in allowed:
                fail(failures, "the scan found something the config does "
                               f"not allowlist: {f}:{line}")


def main():
    failures: list = []
    if not GITLEAKS:
        print("FAIL gitleaks is not installed; run install/gitleaks/install.sh")
        return 1
    if not CONFIG.is_file():
        print(f"FAIL missing config: {CONFIG}")
        return 1

    case_config_shape(failures)
    case_history_is_clean(failures)
    case_fixtures_pass_with_the_config(failures)
    case_fixtures_are_found_without_the_config(failures)
    case_a_real_secret_in_a_fixture_is_found(failures)

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS gitleaks config: the three fixtures pass the scan, and a "
          "real token in the same files is still found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
