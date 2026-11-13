#!/usr/bin/env python3
"""Pin the gates.d loader: what it reads, in what order, and what it refuses.

Every gate lives in its own file under gates.d/, so two branches that each
add a gate touch two files and merge without a conflict. The reader has to
hold to that: the manifest's own gates and every gates.d file beside it, in
name order, and a refusal that names the file it refused.

Run directly: `python3 tests/test_gates_d.py`.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PY = REPO_ROOT / "lib" / "manifest.py"

sys.path.insert(0, str(REPO_ROOT / "lib"))
import manifest  # noqa: E402


def gate(name: str, when: str = "") -> str:
    """One [[gates]] entry, as a manifest holds it."""
    text = f"[[gates]]\nname = \"{name}\"\n"
    if when:
        text += f"when = '{when}'\n"
    return text + "run = \"true\"\n"


def project(root: Path, gates_toml: str = "",
            gates_d: dict[str, str] | None = None) -> Path:
    """A project root with a gates.toml and, when asked, a gates.d/."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "gates.toml").write_text(gates_toml)
    if gates_d:
        (root / "gates.d").mkdir(exist_ok=True)
        for name, text in gates_d.items():
            (root / "gates.d" / name).write_text(text)
    return root / "gates.toml"


def refusal(path: Path) -> str:
    """What the loader refuses with, or "" when it accepted the manifest."""
    try:
        manifest.gates(path)
    except SystemExit as e:
        return str(e)
    return ""


def read_gates(path: Path) -> subprocess.CompletedProcess:
    """What the reader says when the gates are asked for."""
    return subprocess.run(
        [sys.executable, str(MANIFEST_PY), str(path.parent), "gates"],
        capture_output=True, text=True)


def case_manifest_only(tmp: Path) -> list[str]:
    """A project with no gates.d/ reads the gates it always read."""
    path = project(tmp / "manifest-only",
                   gates_toml=gate("first", "^src/") + gate("second"))
    found = manifest.gates(path)
    names = [g.get("name") for g in found]
    whens = [g.get("when", "") for g in found]
    failures = []
    if names != ["first", "second"]:
        failures.append(f"a manifest with no gates.d/ read {names!r}")
    if whens != ["^src/", ""]:
        failures.append(f"a manifest with no gates.d/ read {whens!r}")
    return failures


def case_gates_d_only(tmp: Path) -> list[str]:
    """The gates live in gates.d/, one file each, read in name order."""
    path = project(tmp / "gates-d-only", gates_d={
        "b-second.toml": gate("second"),
        "a-first.toml": gate("first"),
    })
    names = [g.get("name") for g in manifest.gates(path)]
    if names != ["first", "second"]:
        return [f"gates.d/ was read as {names!r}, not in name order"]
    return []


def case_manifest_and_gates_d(tmp: Path) -> list[str]:
    """A manifest and a gates.d/ together hold every gate, the manifest's
    first: a project that keeps some gates in the manifest keeps them."""
    path = project(tmp / "both",
                   gates_toml=gate("from-manifest"),
                   gates_d={"a-file.toml": gate("from-gates-d")})
    names = [g.get("name") for g in manifest.gates(path)]
    if names != ["from-manifest", "from-gates-d"]:
        return [f"a manifest and a gates.d/ read {names!r}"]
    return []


def case_duplicate_name(tmp: Path) -> list[str]:
    """Two files that declare one gate name are refused, naming both.

    A name is how a gate is picked, so two of them is a manifest that
    cannot say which one a caller meant.
    """
    path = project(tmp / "duplicate", gates_d={
        "a-one.toml": gate("same"),
        "b-two.toml": gate("same"),
    })
    failures = []
    said = refusal(path)
    if not said:
        failures.append("two files declaring one gate name were accepted")
    else:
        for name in ("a-one.toml", "b-two.toml"):
            if name not in said:
                failures.append(f"the refusal did not name {name}: {said!r}")
    r = read_gates(path)
    if r.returncode == 0:
        failures.append(f"the reader accepted one gate name twice: {r.stdout!r}")
    return failures


def case_stray_table(tmp: Path) -> list[str]:
    """A gates.d file holds gates and nothing else.

    A file that also holds a task or a table is read by two rules at once,
    and the reader cannot say which one a caller meant.
    """
    failures = []
    for label, stray in [
        ("task", '[[task]]\nname = "bench"\nrole = "builder"\n'
                 'command = "just ci"\n'),
        ("table", "[prove]\ncommand = \"just test\"\n"),
    ]:
        path = project(tmp / f"stray-{label}", gates_d={
            "a-one.toml": gate("one") + stray,
        })
        said = refusal(path)
        if not said:
            failures.append(f"a gates.d file holding a {label} was accepted")
            continue
        if "a-one.toml" not in said:
            failures.append(
                f"the refusal of a {label} did not name the file: {said!r}")
        r = read_gates(path)
        if r.returncode == 0:
            failures.append(f"the reader accepted a gates.d file with a {label}")
    return failures


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, env=env, check=True)


def case_two_branches_merge(tmp: Path) -> list[str]:
    """Two branches that each add a gates.d file merge without a conflict.

    The same two branches adding a gate to one manifest do conflict: that
    is the cost this layout removes, so the case holds both halves.
    """
    failures = []
    for layout in ("gates.d", "one manifest"):
        root = tmp / f"merge-{layout.replace(' ', '-')}"
        root.mkdir(parents=True)
        git(root, "init", "--quiet", "-b", "main")
        if layout == "gates.d":
            project(root, gates_d={"base.toml": gate("base")})
        else:
            project(root, gates_toml=gate("base"))
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "base")
        for branch in ("one", "two"):
            git(root, "checkout", "-q", "-b", branch, "main")
            if layout == "gates.d":
                (root / "gates.d" / f"{branch}.toml").write_text(gate(branch))
            else:
                (root / "gates.toml").write_text(gate("base") + gate(branch))
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", branch)
        merged = subprocess.run(
            ["git", "-C", str(root), "merge-tree", "--write-tree", "one", "two"],
            capture_output=True, text=True)
        conflicted = merged.returncode != 0
        if layout == "gates.d" and conflicted:
            failures.append(
                f"two branches that each added a gates.d file conflicted: "
                f"{merged.stdout.strip()} {merged.stderr.strip()}")
        if layout == "one manifest" and not conflicted:
            failures.append(
                "two branches that each added a gate to one manifest did not "
                "conflict, so the case proves nothing")
    return failures


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for label, fn in [
            ("a manifest with no gates.d/", case_manifest_only),
            ("gates.d/ alone, read in name order", case_gates_d_only),
            ("a manifest and a gates.d/ together", case_manifest_and_gates_d),
            ("one gate name in two files", case_duplicate_name),
            ("a gates.d file holding something else", case_stray_table),
            ("two branches each adding a gates.d file", case_two_branches_merge),
        ]:
            try:
                failures.extend(fn(tmp))
            except Exception as e:  # noqa: BLE001 - the case must not abort
                failures.append(f"{label}: {type(e).__name__}: {e}")
    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS the gates.d loader reads every gate, in name order, and "
          "refuses a manifest it cannot read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
