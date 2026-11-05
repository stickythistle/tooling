#!/usr/bin/env python3
"""Pin gate-verify's decision between three cases.

  docs-only      -> exit 0, names the files
  gated code     -> exit 0, runs the gates the diff selected
  ungated code   -> exit 1, names the code path, not the whole diff

`--list` selects without running, so the gates' own suites never stand up. The
real gates.toml is copied into each tmpdir rather than a synthetic one.

Run directly: `python3 tests/test_gate_verify.py`.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_VERIFY = REPO_ROOT / "bin" / "gate-verify"
REDACTION_CHECK = REPO_ROOT / "bin" / "redaction-check"
GATES_TOML = REPO_ROOT / "gates.toml"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True, check=True)


def make_repo(tmpdir: Path, with_gates: bool = True) -> Path:
    """Stand up a throwaway repo with one commit on a branch off main.

    The project's gates.toml is copied in (when `with_gates`) so the
    test exercises the same manifest shape gate-verify sees in
    production. Initial files give `git diff --name-only` something to
    show; the diff under test is what each case appends on top.
    """
    repo = tmpdir / "wt"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n")
    git(repo, "add", "README.md")
    if with_gates:
        shutil.copy(GATES_TOML, repo / "gates.toml")
    else:
        (repo / "gates.toml").write_text("")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "init")
    # Set up a remote so `origin/main` resolves; gate-verify's default
    # VERIFY_BASE is origin/main and the test passes the repo as a path,
    # not a branch name.
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def run_gate_verify(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke bin/gate-verify against the repo with the given flags."""
    return subprocess.run(
        [str(GATE_VERIFY), str(repo), *args],
        capture_output=True, text=True,
        # common.sh reads $HOME for the optional env file; setting it
        # here keeps `set -u` from aborting before the test can read
        # the verdict. The other vars match what gate-verify expects
        # in production (a default VERIFY_BASE so the test does not
        # need to point at a real origin/main).
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"})


def selected_names(out: str) -> list[str]:
    """Parse `gate-verify --list` output. The format is two leading
    spaces, the gate name left-padded to 18 characters (and truncated
    if longer), then the run command -- so the name is the bytes from
    column 2 up to column 20, not a regex on whitespace."""
    names = []
    for ln in out.splitlines():
        if not ln.startswith("  "):
            continue
        if len(ln) < 20:
            continue
        names.append(ln[2:20].rstrip())
    return names


def selected_names_match(out: str, expected: list[str]) -> set[str]:
    """--list pads names to 18 chars and truncates anything longer; this
    matches each output name to the manifest's names by prefix so the
    test does not break on a name wider than the column."""
    seen = selected_names(out)
    matched = set()
    for got in seen:
        for want in expected:
            if got == want or want.startswith(got):
                matched.add(want)
    return matched


def case_docs_only(tmp: Path) -> list[str]:
    """A diff that is only docs, a licence, and a config file: nothing
    to gate, exit 0, message names the paths."""
    repo = make_repo(tmp / "docs", with_gates=False)
    # No gate, but the repo must still be a valid git repo for the
    # diff to be readable. The decision under test does not depend on
    # what gates are present.
    (repo / "LICENSE").write_text("MIT\n")
    (repo / "README.md").write_text("now with docs\n")
    (repo / ".editorconfig").write_text("root = true\n")
    git(repo, "add", "LICENSE", "README.md", ".editorconfig")
    git(repo, "commit", "-q", "-m", "docs-only")
    r = run_gate_verify(repo)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"docs-only diff refused: rc={r.returncode} stderr={r.stderr.strip()}")
        return failures
    if "nothing to gate" not in r.stderr:
        failures.append(
            f"docs-only diff did not report 'nothing to gate': stderr={r.stderr.strip()}")
    for path in ("LICENSE", "README.md", ".editorconfig"):
        if path not in r.stderr:
            failures.append(
                f"docs-only diff did not name {path}: stderr={r.stderr.strip()}")
    return failures


def case_ungated_code(tmp: Path) -> list[str]:
    """A diff that adds an unwired .py file under lib/ -- code reaching
    main unverified. Exits 1 and names the file, not any docs alongside."""
    repo = make_repo(tmp / "ungated")
    # lib/, not bin/: every bin/ path matches a catch-all gate.
    (repo / "lib").mkdir()
    (repo / "lib" / "ungated_thing.py").write_text("# nothing\n")
    (repo / "LICENSE").write_text("MIT\n")
    git(repo, "add", "lib/ungated_thing.py", "LICENSE")
    git(repo, "commit", "-q", "-m", "ungated")
    r = run_gate_verify(repo)
    failures = []
    if r.returncode != 1:
        failures.append(
            f"ungated code did not refuse: rc={r.returncode} stderr={r.stderr.strip()}")
    if "lib/ungated_thing.py" not in r.stderr:
        failures.append(
            f"ungated refusal did not name the file: stderr={r.stderr.strip()}")
    if "LICENSE" in r.stderr:
        failures.append(
            f"ungated refusal named the licence: stderr={r.stderr.strip()}")
    return failures


def case_gated_code_selects(tmp: Path) -> list[str]:
    """A diff that touches only a tool with a per-file gate must select
    exactly the gates that gate prescribes, nothing more. bin/pr-ready
    is the per-file case that pins the selection contract."""
    repo = make_repo(tmp / "gated")
    # Touch pr-ready on a fresh branch; the gate `when`
    # `^(bin/pr-ready|tests/test_pr_ready\.py|tests/fixtures/gh_.*\.json)$`
    # selects only the pr-ready tests gate.
    git(repo, "checkout", "-q", "-b", "pr-ready-change")
    # Copy the actual bin/pr-ready from the repo so the file exists
    # for `git diff` to see.
    (repo / "bin").mkdir()
    shutil.copy(REPO_ROOT / "bin" / "pr-ready", repo / "bin" / "pr-ready")
    git(repo, "add", "bin/pr-ready")
    git(repo, "commit", "-q", "-m", "edit pr-ready")
    # VERIFY_BASE=origin/main selects against the same base gate-verify
    # uses in production; --list skips running the gates so the test
    # exercises selection without needing the test suites.
    r = subprocess.run(
        [str(GATE_VERIFY), str(repo), "--list"],
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"},
        capture_output=True, text=True)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"gated code did not select cleanly: rc={r.returncode} "
            f"stderr={r.stderr.strip()} stdout={r.stdout.strip()}")
        return failures
    names = selected_names(r.stdout)
    # Selection is unchanged. Expected names come from the manifest, so the
    # suite list can grow without breaking this.
    if "pr-ready tests" not in names:
        failures.append(
            f"pr-ready diff did not select 'pr-ready tests': got {names!r}")
    # No gate whose `when` does not match bin/pr-ready should fire.
    import tomllib
    manifest_gates = tomllib.loads((repo / "gates.toml").read_text())["gates"]
    import re as _re
    expected = [g["name"] for g in manifest_gates
                if g.get("when") and _re.search(g["when"], "bin/pr-ready")]
    matched = selected_names_match(r.stdout, expected)
    if matched != set(expected):
        failures.append(
            f"pr-ready diff selected {names!r}, expected {expected!r}")
    return failures


def case_mixed_gateable_ungateable(tmp: Path) -> list[str]:
    """A diff with both gated code and a licence runs the gates and
    exits 0. The licence is not the reason the branch is gated, and it
    must not block selection."""
    repo = make_repo(tmp / "mixed")
    (repo / "bin").mkdir()
    shutil.copy(REPO_ROOT / "bin" / "pr-ready", repo / "bin" / "pr-ready")
    (repo / "LICENSE").write_text("MIT\n")
    git(repo, "add", "bin/pr-ready", "LICENSE")
    git(repo, "commit", "-q", "-m", "mixed")
    r = subprocess.run(
        [str(GATE_VERIFY), str(repo), "--list"],
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"},
        capture_output=True, text=True)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"mixed diff refused: rc={r.returncode} stderr={r.stderr.strip()}")
        return failures
    names = selected_names(r.stdout)
    if "pr-ready tests" not in names:
        failures.append(
            f"mixed diff lost pr-ready selection: got {names!r}")
    return failures


def case_bin_doc_is_not_code(tmp: Path) -> list[str]:
    """A file under bin/ with a suffix that is not a source one is a
    document, not a script. The carve-out that makes no-suffix bin/ scripts
    gateable must not swallow bin/README.md, or a diff adding one is refused
    for having no gate to run on prose."""
    repo = make_repo(tmp / "bindoc", with_gates=False)
    (repo / "bin").mkdir(exist_ok=True)
    (repo / "bin" / "README.md").write_text("what lives here\n")
    git(repo, "add", "bin/README.md")
    git(repo, "commit", "-q", "-m", "a readme under bin")
    r = run_gate_verify(repo)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"bin/README.md refused as ungated code: rc={r.returncode} "
            f"stderr={r.stderr.strip()}")
        return failures
    if "nothing to gate" not in r.stderr:
        failures.append(
            f"bin/README.md was classified as code: stderr={r.stderr.strip()}")
    return failures



def case_declared_gates_are_authoritative(tmp: Path) -> list[str]:
    failures = []
    for label, path, when in [
        ("rust", "src/lib.rs", r"\.rs$"),
        ("fixture", "tests/fixtures/gh_head.json", r"\.json$"),
        ("documentation", "README.md", r"\.md$"),
        ("unconditional", "LICENSE", ""),
    ]:
        repo = make_repo(tmp / label, with_gates=False)
        (repo / "gates.toml").write_text(
            f"[[gates]]\nname = 'sentinel'\nwhen = '{when}'\n"
            "run = 'sh -c \"echo GATE-RAN; exit 7\"'\n")
        git(repo, "add", "gates.toml")
        git(repo, "commit", "-q", "-m", "manifest")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n")
        git(repo, "add", path)
        git(repo, "commit", "-q", "-m", "change")
        listed = run_gate_verify(repo, "--list")
        ran = run_gate_verify(repo)
        if listed.returncode or "sentinel" not in listed.stdout:
            failures.append(f"{label}: explicit gate was not selected: {listed}")
        if ran.returncode != 1 or "GATE-RAN" not in ran.stdout:
            failures.append(f"{label}: explicit gate failure was skipped: {ran}")
    return failures


def case_unprovable_gate_not_treated_as_failure(tmp: Path) -> list[str]:
    """A gate that exits 3 says it could not answer, which is neither
    a pass nor a failure: a gate that gathered no evidence must not
    appear in a list headed "FAILED". This is the property the
    issue pins: a test-only branch makes gate-prove exit 3, and
    gate-verify must list it as UNPROVABLE rather than refuse the
    branch."""
    failures = []
    for label, path, when in [
        ("unprovable-unconditional", "LICENSE", ""),
        ("unprovable-conditional", "src/lib.rs", r"\.rs$"),
    ]:
        repo = make_repo(tmp / label, with_gates=False)
        (repo / "gates.toml").write_text(
            "[[gates]]\nname = 'unprovable'\nwhen = '" + when + "'\n"
            "run = 'sh -c \"echo UNPROVABLE-MARKER; exit 3\"'\n")
        git(repo, "add", "gates.toml")
        git(repo, "commit", "-q", "-m", "manifest")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n")
        git(repo, "add", path)
        git(repo, "commit", "-q", "-m", "change")
        r = run_gate_verify(repo)
        if r.returncode != 0:
            failures.append(
                f"{label}: gate-verify refused an unprovable gate "
                f"(rc={r.returncode} stderr={r.stderr.strip()!r})")
        if "FAILED" in (r.stdout + r.stderr):
            failures.append(
                f"{label}: unprovable gate was reported as FAILED: "
                f"stdout={r.stdout!r} stderr={r.stderr!r}")
        if "UNPROVABLE" not in r.stderr:
            failures.append(
                f"{label}: gate-verify did not list the gate as "
                f"UNPROVABLE on its UNPROVABLE line: "
                f"stderr={r.stderr!r}")
        if "unprovable" not in r.stderr:
            failures.append(
                f"{label}: UNPROVABLE line did not name the gate: "
                f"stderr={r.stderr!r}")
        if "UNPROVABLE-MARKER" not in r.stdout:
            failures.append(
                f"{label}: the gate itself was not run: "
                f"stdout={r.stdout!r}")
    return failures


def case_unknown_source_and_invalid_manifest_refuse(tmp: Path) -> list[str]:
    failures = []
    for label, manifest, path in [
        ("unknown-source", "", "src/lib.rs"),
        ("invalid-manifest", "invalid = [", "README.md"),
        ("missing-manifest", None, "README.md"),
    ]:
        repo = make_repo(tmp / label, with_gates=False)
        if manifest is None:
            (repo / "gates.toml").unlink()
        else:
            (repo / "gates.toml").write_text(manifest)
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "change")
        r = run_gate_verify(repo)
        if r.returncode == 0 or "nothing to gate" in r.stderr:
            failures.append(f"{label}: incorrectly accepted: {r}")
        if label in ("invalid-manifest", "missing-manifest"):
            # The refusal below exits non-zero even when the manifest cannot
            # be read, so the exit code alone cannot tell a reader failure
            # from a gate-selection verdict. The advice for an unwired
            # script must not appear over a manifest that never reached the
            # selection step.
            for phrase in ("no gate matches", "Add one to gates.toml"):
                if phrase in r.stderr:
                    failures.append(
                        f"{label}: manifest failure read as gate refusal: "
                        f"stderr={r.stderr.strip()}")
    return failures


def case_gated_code_with_ungated(tmp: Path) -> list[str]:
    """A diff with a gated bin/ tool and an ungated code path still runs
    the gate. The ungated path is not the reason to refuse; refusal is
    reserved for the case where no gate covers any code path."""
    repo = make_repo(tmp / "gated_with_ungated")
    (repo / "bin").mkdir()
    shutil.copy(REPO_ROOT / "bin" / "pr-ready", repo / "bin" / "pr-ready")
    (repo / "lib").mkdir()
    (repo / "lib" / "ungated_thing.py").write_text("# nothing\n")
    git(repo, "add", "bin/pr-ready", "lib/ungated_thing.py")
    git(repo, "commit", "-q", "-m", "gated plus ungated")
    r = subprocess.run(
        [str(GATE_VERIFY), str(repo), "--list"],
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"},
        capture_output=True, text=True)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"gated+ungated refused: rc={r.returncode} stderr={r.stderr.strip()}")
        return failures
    names = selected_names(r.stdout)
    if "pr-ready tests" not in names:
        failures.append(
            f"gated+ungated lost pr-ready selection: got {names!r}")
    return failures


def case_all_runs_gate_when_does_not_match_diff(tmp: Path) -> list[str]:
    """--all bypasses the `when` filter, so a gate whose regex does not
    match the diff still runs. Without --all the same diff would gate
    nothing."""
    repo = make_repo(tmp / "all_unmatched", with_gates=False)
    # A single gate whose `when` matches an empty extension. The diff
    # below edits a `.py` file, so the regex misses -- a non-`--all`
    # run would skip this gate.
    (repo / "gates.toml").write_text(
        "[[gates]]\nname = 'sentinel'\nwhen = '\\.rs$'\n"
        "run = 'sh -c \"echo GATE-RAN\"'\n")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "manifest")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "lib").mkdir()
    (repo / "lib" / "thing.py").write_text("# touch\n")
    git(repo, "add", "lib/thing.py")
    git(repo, "commit", "-q", "-m", "py diff")
    # Without --all: gate is filtered out, no gate runs, refusal.
    no_all = run_gate_verify(repo)
    failures = []
    if no_all.returncode != 1:
        failures.append(
            f"non-`--all` should refuse (no gate matches): rc={no_all.returncode} "
            f"stderr={no_all.stderr.strip()}")
    # With --all: every gate runs, sentinel fires.
    listed = run_gate_verify(repo, "--all", "--list")
    ran = run_gate_verify(repo, "--all")
    if listed.returncode or "sentinel" not in listed.stdout:
        failures.append(f"--all did not select unmatched gate: {listed.stdout!r}")
    if ran.returncode != 0 or "GATE-RAN" not in ran.stdout:
        failures.append(f"--all did not run unmatched gate: rc={ran.returncode} "
                     f"stdout={ran.stdout.strip()}")
    return failures


def case_all_works_with_empty_diff(tmp: Path) -> list[str]:
    """On main, `git diff origin/main...HEAD` is empty, which under a
    normal run is a refusal. --all turns that refusal into a verdict:
    every gate runs against the tree as it stands on main."""
    repo = make_repo(tmp / "all_empty", with_gates=False)
    # No second commit -- the branch tip equals origin/main, so the diff
    # is empty.
    (repo / "gates.toml").write_text(
        "[[gates]]\nname = 'sentinel'\nwhen = ''\n"
        "run = 'sh -c \"echo GATE-RAN\"'\n")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "manifest")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # Without --all: refusal with "no changes against $BASE".
    no_all = run_gate_verify(repo)
    failures = []
    if no_all.returncode == 0 or "no changes against" not in no_all.stderr:
        failures.append(
            f"empty diff should refuse without --all: rc={no_all.returncode} "
            f"stderr={no_all.stderr.strip()}")
    # With --all: empty diff is acceptable, sentinel runs.
    ran = run_gate_verify(repo, "--all")
    if ran.returncode != 0 or "GATE-RAN" not in ran.stdout:
        failures.append(
            f"--all did not run on empty diff: rc={ran.returncode} "
            f"stdout={ran.stdout.strip()} stderr={ran.stderr.strip()}")
    return failures


def case_all_skip_still_excludes(tmp: Path) -> list[str]:
    """--all selects every gate; --skip still removes the named one.
    A run that bypassed `when` but bypassed `--skip` too would be hard
    to use: a broken gate could not be excluded for one run."""
    repo = make_repo(tmp / "all_skip", with_gates=False)
    # Two unconditional gates; both run under --all by default.
    (repo / "gates.toml").write_text(
        "[[gates]]\nname = 'first'\nwhen = ''\n"
        "run = 'sh -c \"echo FIRST-RAN\"'\n"
        "[[gates]]\nname = 'second'\nwhen = ''\n"
        "run = 'sh -c \"echo SECOND-RAN\"'\n")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "manifest")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "lib").mkdir()
    (repo / "lib" / "thing.py").write_text("# touch\n")
    git(repo, "add", "lib/thing.py")
    git(repo, "commit", "-q", "-m", "trigger")
    # Without --skip: both gates fire.
    listed = run_gate_verify(repo, "--all", "--list")
    failures = []
    if listed.returncode != 0:
        failures.append(f"--all --list refused: {listed.stderr.strip()}")
        return failures
    names = selected_names(listed.stdout)
    if "first" not in names or "second" not in names:
        failures.append(f"--all did not select both: {names!r}")
    # With --skip second: only first appears.
    skipped = run_gate_verify(repo, "--all", "--list", "--skip", "second")
    if skipped.returncode != 0:
        failures.append(f"--all --skip refused: {skipped.stderr.strip()}")
        return failures
    names = selected_names(skipped.stdout)
    if "second" in names:
        failures.append(f"--skip did not exclude 'second' under --all: {names!r}")
    if "first" not in names:
        failures.append(f"--skip excluded too much under --all: {names!r}")
    return failures


def case_redaction_gate_runs_on_every_branch(tmp: Path) -> list[str]:
    """A branch that carries an agent's redaction into the source fails
    the gate, a branch that only touches a file already holding one
    passes, and a branch that changes only a document still runs it.

    The halves are read off real git commits, not a hand-written diff.
    """
    failures = []

    # Assembled rather than written out: a literal of it would be caught
    # by the very tool under test, and the case would plant wrong bytes.
    stars = "*" * 3
    scheme = "Author" + "ization"
    secret = scheme + ": Bearer " + stars + " this line"

    def repo_with_tool(tmpdir: Path, label: str) -> Path:
        """A repo holding the redaction tool in its base, so the diff
        under test is only the file the branch changes."""
        repo = make_repo(tmpdir / label)
        (repo / "bin").mkdir(exist_ok=True)
        shutil.copy(REDACTION_CHECK, repo / "bin" / "redaction-check")
        git(repo, "add", "bin/redaction-check")
        git(repo, "commit", "-q", "-m", "add the redaction tool")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        return repo

    def repo_with_redaction(tmpdir: Path, label: str) -> Path:
        """A repo whose base already holds a redaction on a line the
        branch under test does not touch."""
        repo = repo_with_tool(tmpdir, label)
        (repo / "notes.md").write_text(secret + "\n")
        git(repo, "add", "notes.md")
        git(repo, "commit", "-q", "-m", "a note that already holds one")
        # The base is the commit that holds the redaction, so the branch
        # below reads it as an unchanged line.
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        return repo

    # A branch that adds the redaction: the gate must fail it.
    adds = repo_with_tool(tmp / "adds", "adds")
    (adds / "notes.md").write_text(secret)
    git(adds, "add", "notes.md")
    git(adds, "commit", "-q", "-m", "add the redaction")
    r = run_gate_verify(adds)
    if r.returncode == 0:
        failures.append(
            f"a branch adding a redaction passed: rc={r.returncode} "
            f"stdout={r.stdout.strip()!r}")
    if "redaction check" not in r.stderr:
        failures.append(
            f"the redaction gate did not fail the branch: "
            f"stderr={r.stderr.strip()!r}")

    # A branch that only touches a file already holding one passes.
    touches = repo_with_redaction(tmp, "touches")
    git(touches, "checkout", "-q", "-b", "touch-only")
    (touches / "notes.md").write_text(secret + "\nanother line\n")
    git(touches, "add", "notes.md")
    git(touches, "commit", "-q", "-m", "touch the file, keep the redaction")
    r = run_gate_verify(touches)
    if r.returncode != 0:
        failures.append(
            f"a branch that left an existing redaction alone failed: "
            f"rc={r.returncode} stdout={r.stdout.strip()!r} "
            f"stderr={r.stderr.strip()!r}")
    if "FAILED" in (r.stdout + r.stderr):
        failures.append(
            f"the redaction gate failed a branch that only touched an "
            f"existing redaction: stdout={r.stdout!r} stderr={r.stderr!r}")

    # A branch that changes only a document still runs the gate.
    docs = repo_with_tool(tmp / "docs", "docs")
    (docs / "README.md").write_text("init\n" + secret)
    git(docs, "add", "README.md")
    git(docs, "commit", "-q", "-m", "add the redaction to a document")
    r = run_gate_verify(docs)
    if r.returncode == 0:
        failures.append(
            f"a document-only branch carrying a redaction passed: "
            f"rc={r.returncode} stdout={r.stdout.strip()!r}")
    return failures


# ---------------------------------------------------------------------------
# One gate run per tree, and no orphaned children.
#
# Real processes throughout: a gate that sleeps, a gate-verify that is
# killed with a signal, and a run that is made to crash. Each case observes
# what the process did.
# ---------------------------------------------------------------------------


def sleeping_gate_repo(parent: Path, name: str, gate_body: str) -> Path:
    """A repo whose single gate runs `gate_body` and sleeps.

    The gate writes its own pid to a file, so a test can look up the
    process it started.
    """
    repo = make_repo(parent / name, with_gates=False)
    (repo / "gates.toml").write_text(
        "[[gates]]\nname = 'sleeper'\nwhen = '^src/thing\\.py$'\n"
        f"run = {gate_body!r}\n")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "manifest")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "thing.py").write_text("# touch\n")
    git(repo, "add", "src/thing.py")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def two_gate_repo(parent: Path, name: str, first: str, second: str) -> Path:
    """A repo whose two gates both run, in manifest order.

    `first` and `second` are the shell each gate runs. Both name the
    diff's file, so the selection has a gate for it.
    """
    repo = make_repo(parent / name, with_gates=False)
    (repo / "gates.toml").write_text(
        "[[gates]]\nname = 'first'\nwhen = '^src/thing\\.py$'\n"
        f"run = {first!r}\n"
        "[[gates]]\nname = 'second'\nwhen = '^src/thing\\.py$'\n"
        f"run = {second!r}\n")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "manifest")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "thing.py").write_text("# touch\n")
    git(repo, "add", "src/thing.py")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def gate_verify_env(runtime_dir: Path) -> dict:
    """The environment a gate-verify run needs, with its own lock directory.

    XDG_RUNTIME_DIR points at a directory the test owns, so this run's lock
    cannot collide with a real run's.
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return {"HOME": "/tmp", "PATH": "/usr/bin:/bin",
            "VERIFY_BASE": "origin/main",
            "XDG_RUNTIME_DIR": str(runtime_dir)}


def start_gate_verify(repo: Path, runtime_dir: Path) -> subprocess.Popen:
    """Start gate-verify in the background and return the process."""
    return subprocess.Popen(
        [str(GATE_VERIFY), str(repo)],
        env=gate_verify_env(runtime_dir),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.05):
    """Poll until the predicate is true, or give up and return None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def pids_alive(pids: list[int]) -> list[int]:
    """The pids that are still running."""
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            alive.append(pid)
            continue
        alive.append(pid)
    return alive


def running_group_members(pgid: int) -> list[int]:
    """The pids still running in the process group.

    A zombie has exited and holds nothing, so it does not count.
    """
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        try:
            # The comm field holds spaces and parentheses, so the fields
            # after it are the ones that count: state, ppid, pgrp.
            fields = stat[stat.rindex(")") + 2:].split()
            if len(fields) < 3 or fields[0] == "Z":
                continue
            if int(fields[2]) == pgid:
                members.append(int(entry.name))
        except (ValueError, IndexError):
            continue
    return members


def start_gate_verify_logged(repo: Path, runtime_dir: Path,
                             out_path: Path, err_path: Path):
    """Start gate-verify with its output in files.

    A pipe stays open while any of the gate's group survives, so a file
    is what lets the case read what the run said without waiting on a
    survivor.
    """
    out = out_path.open("w")
    err = err_path.open("w")
    proc = subprocess.Popen(
        [str(GATE_VERIFY), str(repo)],
        env=gate_verify_env(runtime_dir), stdout=out, stderr=err)
    return proc, out, err


def _second_run_stderr(repo: Path, runtime_dir: Path) -> str:
    """What a second gate-verify run on this tree says, on stderr."""
    try:
        r = subprocess.run(
            [str(GATE_VERIFY), str(repo)],
            env=gate_verify_env(runtime_dir),
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return ""
    return r.stderr


def case_second_run_on_same_tree_refuses(tmp: Path) -> list[str]:
    """A second gate-verify on the same tree refuses, naming the pid that
    holds the tree's lock.

    The issue's shape: a lane finds a gate run already going and, having
    nothing to identify it by, kills a pid that may belong to another
    worktree's run.
    """
    runtime = tmp / "runtime-same-tree"
    repo = sleeping_gate_repo(
        tmp, "same-tree",
        "sh -c 'echo $$ > " + str(tmp / "same-tree.pid") + "; sleep 30'")
    first = start_gate_verify(repo, runtime)
    failures = []
    try:
        got_pid = wait_for(lambda: (tmp / "same-tree.pid").exists()
                            and (tmp / "same-tree.pid").read_text().strip())
        if not got_pid:
            failures.append(
                "the first run never reached its gate: "
                f"stdout={first.stdout.read() if first.stdout else ''!r} "
                f"stderr={first.stderr.read() if first.stderr else ''!r}")
            return failures
        second = subprocess.run(
            [str(GATE_VERIFY), str(repo)],
            env=gate_verify_env(runtime),
            capture_output=True, text=True, timeout=60)
        if second.returncode == 0:
            failures.append(
                "a second run on the same tree was accepted: "
                f"rc={second.returncode} stdout={second.stdout.strip()!r}")
        if "already running" not in second.stderr:
            failures.append(
                "a second run on the same tree did not say it was already "
                f"running: stderr={second.stderr.strip()!r}")
        # The refusal names the pid of the run that holds the lock -- the
        # gate-verify process itself, not the gate it started. That pid is
        # the one a lane must not kill.
        if "as pid" not in second.stderr:
            failures.append(
                "the refusal did not name a pid: stderr={second.stderr.strip()!r}")
        else:
            reported = second.stderr.split("as pid", 1)[1].strip().split()[0]
            if not reported.isdigit():
                failures.append(
                    f"the refusal named a non-pid {reported!r}: "
                    f"stderr={second.stderr.strip()!r}")
            elif int(reported) != first.pid:
                failures.append(
                    f"the refusal named pid {reported!r}, not the holder's "
                    f"pid {first.pid!r}: stderr={second.stderr.strip()!r}")
        # A refusal starts no gate.
        if "sleeper" in second.stdout:
            failures.append(
                "the refused run started a gate: stdout={second.stdout.strip()!r}")
    finally:
        first.terminate()
        try:
            first.wait(timeout=30)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=30)
    return failures


def case_run_on_another_tree_proceeds(tmp: Path) -> list[str]:
    """A run on a different tree is unaffected by another tree's lock.

    The lock is per tree, not per machine: a shared host must still gate
    two branches at once.
    """
    runtime = tmp / "runtime-two-trees"
    repo_a = sleeping_gate_repo(
        tmp, "tree-a",
        "sh -c 'echo $$ > " + str(tmp / "tree-a.pid") + "; sleep 30'")
    repo_b = sleeping_gate_repo(
        tmp, "tree-b",
        "sh -c 'echo A > " + str(tmp / "tree-b.marker") + "; sleep 2'")
    first = start_gate_verify(repo_a, runtime)
    failures = []
    try:
        if not wait_for(lambda: (tmp / "tree-a.pid").exists()):
            failures.append("the first tree's run never reached its gate")
            return failures
        # The other tree proceeds while the first tree is still gating.
        second = subprocess.run(
            [str(GATE_VERIFY), str(repo_b)],
            env=gate_verify_env(runtime),
            capture_output=True, text=True, timeout=60)
        if second.returncode != 0:
            failures.append(
                "a run on another tree was refused by another tree's lock: "
                f"rc={second.returncode} stderr={second.stderr.strip()!r}")
        if "already running" in second.stderr:
            failures.append(
                "a run on another tree was refused as already running: "
                f"stderr={second.stderr.strip()!r}")
        if not (tmp / "tree-b.marker").exists():
            failures.append(
                "the run on the other tree did not run its gate: "
                f"stdout={second.stdout.strip()!r}")
    finally:
        first.terminate()
        try:
            first.wait(timeout=30)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=30)
    return failures


def case_kill_leaves_no_orphan(tmp: Path) -> list[str]:
    """Killing gate-verify with TERM leaves no child of the fake gate
    running.

    The gate starts a child of its own -- the shape of a gate that starts
    a remote-task. An orphan keeps running against a tree nobody is
    watching, and keeps holding the builder lock.
    """
    runtime = tmp / "runtime-orphan"
    marker = tmp / "orphan.marker"
    # The gate writes its own pid, then starts a child that outlives a
    # plain kill of the gate alone.
    repo = sleeping_gate_repo(
        tmp, "orphan",
        "sh -c 'sleep 300 & echo $! > " + str(marker) +
        "; echo $$ > " + str(tmp / "orphan.pid") + "; wait'")
    proc = start_gate_verify(repo, runtime)
    failures = []
    try:
        if not wait_for(lambda: marker.exists() and marker.read_text().strip()):
            failures.append("the gate never started its child")
            return failures
        child = int(marker.read_text().strip())
        # The gate's own pid, for the record.
        if not wait_for(lambda: (tmp / "orphan.pid").exists()):
            failures.append("the gate never reported its pid")
            return failures
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            failures.append("gate-verify ignored the TERM")
            proc.kill()
            proc.wait(timeout=30)
            return failures
        # The child must be gone. Give it a moment: the trap runs as the
        # script exits, and the kill is a signal, not a wait.
        still = wait_for(lambda: not pids_alive([child]), timeout=15)
        if still is None:
            failures.append(
                f"the gate's child {child} survived gate-verify's death: "
                f"still alive: {pids_alive([child])!r}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
    return failures


def case_crashed_run_lock_is_free(tmp: Path) -> list[str]:
    """A crashed run's lock is free for the next run.

    A pidfile without a lock goes stale after a crash, and someone deletes
    it by hand. A flock is released by the kernel when the descriptor
    closes.
    """
    runtime = tmp / "runtime-crash"
    repo = make_repo(tmp / "crash", with_gates=False)
    # A helper the gate runs. It walks up its own parent chain until it
    # finds the gate-verify script and kills it with SIGKILL, so no trap
    # runs and nothing is cleaned up.
    #
    # Walking the chain rather than counting levels off it: the number of
    # levels between the gate and the script depends on how the script
    # starts its gates, and a fixture that hard-codes the count kills the
    # wrong process the moment that changes.
    #
    # The helper announces itself and then waits to be told to crash, so
    # the test can look at the lock while the run is provably alive.
    ready = tmp / "crash.ready"
    go = tmp / "crash.go"
    helper = tmp / "crasher.sh"
    helper.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'ready\\n' > {str(ready)!r}\n"
        f"while [ ! -e {str(go)!r} ]; do sleep 0.05; done\n"
        "pid=$PPID\n"
        "for _ in 1 2 3 4 5 6 7 8; do\n"
        "  [ -n \"$pid\" ] || break\n"
        "  if tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null"
        " | grep -q gate-verify; then\n"
        "    kill -9 \"$pid\" 2>/dev/null || true\n"
        "    break\n"
        "  fi\n"
        "  pid=$(ps -o ppid= -p \"$pid\" 2>/dev/null | tr -d ' ')\n"
        "done\n"
        "sleep 30\n")
    helper.chmod(0o755)
    (repo / "gates.toml").write_text(
        "[[gates]]\nname = 'crasher'\nwhen = '^src/thing\\.py$'\n"
        f"run = {str(helper)!r}\n")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "manifest")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "thing.py").write_text("# touch\n")
    git(repo, "add", "src/thing.py")
    git(repo, "commit", "-q", "-m", "change")
    failures = []
    # The first run dies hard: the gate SIGKILLs the script, so no trap
    # runs and nothing is cleaned up by the script.
    #
    # It is started in the background rather than run to completion, so
    # the case can look at the lock while the run is still alive.
    first = start_gate_verify(repo, runtime)
    try:
        # Wait for the gate to start, so the lock is held before the
        # script is killed.
        if wait_for(lambda: ready.exists(), timeout=30) is None:
            failures.append("the crashing gate never started")
            first.kill()
            first.wait(timeout=30)
            return failures
        # The lock is held while the run lives: a second run is refused.
        refused = wait_for(
            lambda: "already running" in _second_run_stderr(repo, runtime),
            timeout=30)
        if refused is None:
            failures.append(
                "the running gate did not hold the tree's lock: a second "
                "run was not refused")
        # Now crash it. The gate kills the script with SIGKILL.
        go.write_text("go\n")
        first.wait(timeout=60)
    except subprocess.TimeoutExpired:
        failures.append("the crashing run hung instead of dying")
        first.kill()
        first.wait(timeout=30)
        return failures
    if first.returncode == 0:
        failures.append(
            "the crashing run reported success: "
            f"rc={first.returncode}")
    # The lock must be free now: a second run proceeds. Its gate also
    # crashes, so the point of the case is the lock, not the verdict.
    try:
        second = subprocess.run(
            [str(GATE_VERIFY), str(repo)],
            env=gate_verify_env(runtime),
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        failures.append("the second run hung instead of dying")
        return failures
    if "already running" in second.stderr:
        failures.append(
            "a crashed run left its lock held: "
            f"stderr={second.stderr.strip()!r}")
    # A third run, with a gate that does not crash, must reach its gate.
    (repo / "gates.toml").write_text(
        "[[gates]]\nname = 'quiet'\nwhen = '^src/thing\\.py$'\n"
        "run = 'sh -c \"echo QUIET-RAN\"'\n")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "quiet manifest")
    try:
        third = subprocess.run(
            [str(GATE_VERIFY), str(repo)],
            env=gate_verify_env(runtime),
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        failures.append("the third run hung")
        return failures
    if third.returncode != 0 or "QUIET-RAN" not in third.stdout:
        failures.append(
            "the lock was still held after the crashing run: "
            f"rc={third.returncode} stdout={third.stdout.strip()!r} "
            f"stderr={third.stderr.strip()!r}")
    return failures


def case_signal_stops_the_run(tmp: Path) -> list[str]:
    """A TERM, INT or HUP ends the run: no later gate starts, nothing of the
    running gate's group survives, and gate-verify dies by the signal.
    """
    failures = []
    for label, signum in [("TERM", signal.SIGTERM),
                          ("INT", signal.SIGINT),
                          ("HUP", signal.SIGHUP)]:
        runtime = tmp / f"runtime-{label}"
        pid_file = tmp / f"{label}.pid"
        marker = tmp / f"{label}.second"
        out_path = tmp / f"{label}.out"
        err_path = tmp / f"{label}.err"
        repo = two_gate_repo(
            tmp, f"signal-{label}",
            "sh -c 'echo $$ > " + str(pid_file) + "; sleep 30'",
            "sh -c 'touch " + str(marker) + "'")
        proc, out_log, err_log = start_gate_verify_logged(
            repo, runtime, out_path, err_path)
        try:
            got = wait_for(
                lambda: pid_file.exists() and pid_file.read_text().strip())
            if not got:
                failures.append(f"{label}: the run never reached its first gate")
                continue
            try:
                pgid = os.getpgid(int(got.strip()))
            except (ProcessLookupError, ValueError):
                failures.append(f"{label}: the gate was already gone")
                continue
            proc.send_signal(signum)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                failures.append(f"{label}: gate-verify ignored the {label}")
                proc.kill()
                proc.wait(timeout=30)
                continue
            if proc.returncode != -signum:
                failures.append(
                    f"{label}: gate-verify did not die by the signal: "
                    f"rc={proc.returncode}")
            if marker.exists():
                failures.append(f"{label}: the run started the next gate")
            if wait_for(lambda: not running_group_members(pgid),
                        timeout=15) is None:
                failures.append(
                    f"{label}: the gate's group survived: "
                    f"{running_group_members(pgid)!r}")
            said = out_path.read_text() + err_path.read_text()
            if "FAILED" in said:
                failures.append(
                    f"{label}: the killed run read as a gate failure: {said!r}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
            out_log.close()
            err_log.close()
    return failures


def case_gate_failure_still_runs_the_next_gate(tmp: Path) -> list[str]:
    """A gate that exits 1 is recorded and the next gate still runs.

    A failed gate is a verdict, not an interruption: the run must not
    stop at the first failure, and it must say which gate failed.
    """
    repo = two_gate_repo(
        tmp, "gate-failure",
        "sh -c 'echo FIRST-RAN; exit 1'",
        "sh -c 'echo SECOND-RAN'")
    r = run_gate_verify(repo)
    failures = []
    if r.returncode != 1:
        failures.append(
            f"a failed gate did not fail the run: rc={r.returncode} "
            f"stderr={r.stderr.strip()!r}")
    if "==> FAILED: first" not in r.stderr:
        failures.append(
            f"the failed gate was not named on the FAILED line: "
            f"stderr={r.stderr.strip()!r}")
    if "SECOND-RAN" not in r.stdout:
        failures.append(
            f"the next gate did not run: stdout={r.stdout.strip()!r}")
    return failures


def case_list_does_not_take_the_lock(tmp: Path) -> list[str]:
    """--list selects gates without taking the tree's lock.

    A selection runs no gate, so it is not a run, and it must not be
    refused by the run that is gating the same tree.
    """
    runtime = tmp / "runtime-list"
    repo = sleeping_gate_repo(
        tmp, "list",
        "sh -c 'echo $$ > " + str(tmp / "list.pid") + "; sleep 30'")
    first = start_gate_verify(repo, runtime)
    failures = []
    try:
        if not wait_for(lambda: (tmp / "list.pid").exists()):
            failures.append("the run never reached its gate")
            return failures
        listed = subprocess.run(
            [str(GATE_VERIFY), str(repo), "--list"],
            env=gate_verify_env(runtime),
            capture_output=True, text=True, timeout=60)
        if listed.returncode != 0:
            failures.append(
                f"--list was refused while a run held the tree: "
                f"rc={listed.returncode} stderr={listed.stderr.strip()!r}")
        if "already running" in listed.stderr:
            failures.append(
                f"--list was refused as already running: "
                f"stderr={listed.stderr.strip()!r}")
        if "sleeper" not in selected_names(listed.stdout):
            failures.append(
                f"--list did not print the selected gates: "
                f"stdout={listed.stdout.strip()!r}")
    finally:
        first.terminate()
        try:
            first.wait(timeout=30)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=30)
    return failures


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for label, fn in [
            ("declared gates remain authoritative", case_declared_gates_are_authoritative),
            ("unprovable gate is not treated as failure",
             case_unprovable_gate_not_treated_as_failure),
            ("unknown source and invalid manifests refuse", case_unknown_source_and_invalid_manifest_refuse),
            ("docs-only diff", case_docs_only),
            ("ungated code refuses with the script named",
             case_ungated_code),
            ("gated code selects the matching gates",
             case_gated_code_selects),
            ("mixed gated code + licence still gates",
             case_mixed_gateable_ungateable),
            ("a doc under bin/ is not code", case_bin_doc_is_not_code),
            ("gated code plus ungated non-doc path still passes",
             case_gated_code_with_ungated),
            ("--all runs a gate whose `when` does not match the diff",
             case_all_runs_gate_when_does_not_match_diff),
            ("--all works with an empty diff",
             case_all_works_with_empty_diff),
            ("--skip still excludes a gate under --all",
             case_all_skip_still_excludes),
            ("the redaction gate runs on every branch",
             case_redaction_gate_runs_on_every_branch),
            ("a second run on the same tree refuses and names the pid",
             case_second_run_on_same_tree_refuses),
            ("a run on another tree proceeds",
             case_run_on_another_tree_proceeds),
            ("killing gate-verify leaves no orphan",
             case_kill_leaves_no_orphan),
            ("a crashed run's lock is free for the next run",
             case_crashed_run_lock_is_free),
            ("a signal stops the run and dies by the signal",
             case_signal_stops_the_run),
            ("a failed gate still runs the next gate",
             case_gate_failure_still_runs_the_next_gate),
            ("--list does not take the tree's lock",
             case_list_does_not_take_the_lock),
        ]:
            try:
                failures.extend(fn(tmp))
            except subprocess.CalledProcessError as e:
                failures.append(f"{label}: git exited {e.returncode}: {e.stderr}")
    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS gate-verify distinguishes docs-only, ungated code, "
          "and gated code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
