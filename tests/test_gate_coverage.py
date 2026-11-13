#!/usr/bin/env python3
"""Pin that every test suite in tests/ is reachable from gates.toml, and every
bin/ tool by a gate whose `when` cannot also match a tool that does not exist.

gate-verify reports the gates it selected, never the ones that do not exist, so
an unwired suite reads as a short run rather than a hole. An unwired bin/ tool
reads the same way when a broad `when` (a bare `bin/`, a `^bin/.*`, anything
that would also match a name under bin/ that nothing owns) wins the assertion
by accident: the check would report full coverage while running only the gate
that happened to match the broad pattern. The probe for that is mechanical: if
a gate's `when` also matches `bin/<a name that does not exist>`, it is matching
the directory, not this tool, and is not coverage for any specific one. The
same probe distinguishes a real suite-coverage gate from one whose `when` is
just a catch-all for tests/.

`self_tests` runs the probe against bare and blanket patterns for both
directories.

Run directly: `python3 tests/test_gate_coverage.py`.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATES = REPO_ROOT / "gates.toml"
TESTS = REPO_ROOT / "tests"
BIN = REPO_ROOT / "bin"

sys.path.insert(0, str(REPO_ROOT / "lib"))
# The gates come from the reader every tool uses, so this suite checks the
# way the gates are selected.
import manifest  # noqa: E402

# Pins the manifest, not a tool, so no `when` should select it.
EXEMPT = {"test_gate_coverage.py"}

# A path under bin/ no gate's `when` could legitimately mean to name. Used
# as the probe that distinguishes a gate that covers a specific tool from
# one that merely matches the directory: a `when` that matches this probe
# also matches any tool, which is not coverage for any of them.
PHONY = "bin/__definitely_not_a_real_tool__"

# The same probe for tests/: a suite name nothing owns.
SUITE_PHONY = "tests/test___definitely_not_a_real_suite___.py"

# bin/ and tests/ have per-file checks above, which the directory rule would
# only paper over. skills/ is content the installer symlinks, not code this
# repository has committed to gating. Hidden directories are VCS state.
DIR_EXEMPT = {"bin", "skills", "tests"} | {p.name for p in REPO_ROOT.iterdir()
                                          if p.name.startswith(".")}
SOURCE_EXTS = (".sh", ".py")


def check_suites(gates: list, tests_dir: Path) -> tuple[list[str], list[str]]:
    """A suite no `when` selects is one gate-verify refuses to gate at all.
    A gate that also matches SUITE_PHONY covers the directory, not this suite.
    A gate naming a suite that is gone passes green having run nothing."""
    failures = []
    suites = sorted(p.name for p in tests_dir.glob("test_*.py")
                    if p.name not in EXEMPT)
    for suite in suites:
        rel = f"tests/{suite}"
        matching = [g for g in gates
                    if g.get("when") and re.search(g["when"], rel)]
        if not matching:
            failures.append(
                f"{rel} is named by no gate, so editing it runs nothing")
            continue
        real = [g for g in matching if not re.search(g["when"], SUITE_PHONY)]
        if not real:
            names = ", ".join(g.get("name", "?") for g in matching)
            failures.append(
                f"{rel}: only catch-all gates name it ({names} "
                f"also match {SUITE_PHONY!r})")
    for g in gates:
        for named in re.findall(r"tests/test_\w+\.py", g.get("run", "")):
            if not (REPO_ROOT / named).is_file():
                failures.append(
                    f"gate {g.get('name', '?')!r} runs {named}, which does not exist")
    return failures, suites


def check_tools(gates: list, bin_dir: Path) -> list[str]:
    """A bin/ tool no gate names is one gate-verify refuses to gate at all.
    A gate whose `when` also matches a path under bin/ that nothing owns is
    matching the directory, not this tool -- it is not coverage for any
    specific one, and the tool still has no real gate even if a broad one
    happened to land in the matching list."""
    failures = []
    for tool in sorted(p.name for p in bin_dir.iterdir() if p.is_file()):
        rel = f"bin/{tool}"
        matching = [g for g in gates
                    if g.get("when") and re.search(g["when"], rel)]
        if not matching:
            failures.append(f"{rel} matches no gate's `when`")
            continue
        # Probe: drop any gate whose `when` would also match a name no tool
        # owns. A non-empty residue is real coverage; an empty one means the
        # only gates pointing here are catch-alls and the tool is unwired.
        real = [g for g in matching if not re.search(g["when"], PHONY)]
        if not real:
            names = ", ".join(g.get("name", "?") for g in matching)
            failures.append(
                f"{rel}: only catch-all gates name it ({names} "
                f"also match {PHONY!r})")
    return failures


def suite_pins(suite: str) -> list[str]:
    """Return the tool a suite runs, read from the suite's own source,
    so the rule needs no hand maintenance and a suite that stops driving
    a tool stops demanding a gate for it.

    Only a tool that exists counts, found two ways:

      * the path a suite builds to run it --
        `REPO_ROOT / "bin" / "pr-loop-gate"`; and
      * a module the suite imports from `lib/`, which is the code the
        tool is built from.
    """
    path = TESTS / suite
    try:
        text = path.read_text()
    except OSError:
        return []
    found = set()
    # The path a suite builds to run the tool: REPO_ROOT / "bin" / "name".
    for match in re.finditer(
            r"""REPO_ROOT\s*/\s*"bin"\s*/\s*"([\w./-]+)\"""", text):
        found.add(f"bin/{match.group(1)}")
    # The module a suite imports: `import pr_gate` pins lib/pr_gate.py,
    # which is the code the tool is built from.
    for match in re.finditer(r"^\s*import\s+([\w]+)\b", text, re.M):
        module = match.group(1)
        candidate = REPO_ROOT / "lib" / f"{module}.py"
        if candidate.is_file():
            found.add(f"lib/{module}.py")
    return sorted(tool for tool in found if (REPO_ROOT / tool).is_file())


def main() -> int:
    gates = manifest.gates(GATES)
    if not gates:
        print("FAIL the manifest declares no gates")
        return 1

    failures, suites = check_suites(gates, TESTS)
    failures.extend(check_tools(gates, BIN))

    # A top-level directory shipping source must be matched by some `when`, or
    # gate-verify refuses a diff that touches only it. The directory is the unit:
    # one matching `when` lifts the refusal, and per-file coverage here would be
    # a stricter rule this suite has not committed to.
    for entry in sorted(p for p in REPO_ROOT.iterdir()
                        if p.is_dir() and p.name not in DIR_EXEMPT):
        files = [str(f.relative_to(REPO_ROOT))
                 for f in entry.rglob("*")
                 if f.is_file() and f.suffix in SOURCE_EXTS]
        if not files:
            continue
        if not any(g.get("when") and any(re.search(g["when"], f) for f in files)
                   for g in gates):
            failures.append(
                f"{entry.name}/ ships source but no gate's `when` matches a file inside it")

    # A syntax gate that cannot fail is worse than none. Two forms do that, and
    # both passed review here before this check existed:
    #   `bash -n a.sh b.sh`   parses a.sh, takes the rest as $1, $2...
    #   `find -exec cmd +/\;` does not propagate cmd's exit status
    #
    # Named as well as probed: whether `-exec ... +` fails depends on which file
    # find lists first, so the behavioural run below cannot catch it reliably.
    for g in gates:
        run = g.get("run", "")
        if "bash -n" not in run:
            continue
        if re.search(r"bash -n [^|]*\*", run):
            failures.append(
                f"gate {g.get('name', '?')!r} passes a glob to `bash -n`, which "
                f"parses only the first file")
        # `find ... -exec`, not a bare `-exec` substring: a tool whose
        # own name ends in `-exec` is no `find -exec`, and a gate that
        # parses it must not be rejected for that.
        if re.search(r"\bfind\b[^|]*\s-exec\b", run):
            failures.append(
                f"gate {g.get('name', '?')!r} uses `find -exec`, which does not "
                f"propagate the exit status of what it runs")
        target = next((d for d in ("install", "pipeline")
                       if f" {d} " in run or f"'{d}'" in run or run.startswith(d)
                       or f" {d}/" in run), None)
        if target is None or not (REPO_ROOT / target).is_dir():
            continue
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td)
            shutil.copytree(REPO_ROOT / target, sandbox / target)
            broken = sandbox / target / "_coverage_probe.sh"
            broken.write_text("#!/usr/bin/env bash\nif [ 1 -eq 1 ]; then\n")
            r = subprocess.run(["bash", "-c", run], cwd=sandbox,
                               capture_output=True, text=True)
            if r.returncode == 0:
                failures.append(
                    f"gate {g.get('name', '?')!r} passed with an unparseable "
                    f"script in {target}/; its command checks fewer files than "
                    f"its `when` selects")

    # The manifest itself. Every other gate is read from it and from the files
    # beside it, so a `when` broken here breaks gate selection everywhere -- and
    # a change to it selected nothing, including this check. It has to gate
    # itself.
    for rel in ("gates.toml", "gates.d/", "tests/test_gate_coverage.py"):
        if not any(g.get("when") and re.search(g["when"], rel) for g in gates):
            failures.append(
                f"{rel} is named by no gate, so breaking gate selection runs nothing")

    # A suite and the tool it pins share a `when`, so editing either runs
    # the test. A `when` naming only the suite runs nothing when the tool
    # changes -- and a change to the tool is the change the suite exists
    # to cover. The rule is per suite: the gate that runs a suite is the
    # one that must also name the tool.
    for suite in suites:
        rel = f"tests/{suite}"
        running = [g for g in gates
                   if g.get("when") and re.search(g["when"], rel)]
        for tool in suite_pins(suite):
            if not any(g.get("when") and re.search(g["when"], tool)
                       for g in running):
                failures.append(
                    f"{rel} pins {tool}, but the gate that runs it names "
                    "neither, so a change to the tool runs nothing")

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print(f"PASS the gates cover {len(suites)} suite(s) and every bin/ tool")
    return 0


# ----- self_tests ----------------------------------------------------------
#
# `check_tools` and `check_suites` against a fake bin/, tests/ and gates list,
# so the probe is exercised without editing the real manifest.

def self_tests() -> int:
    failures = []

    # Each case: (label, gates list, bin/ filenames under test, expected
    # substring in failure message, or None when the case is expected to
    # pass cleanly). Each case gets its own fresh bin/ so the directory
    # contains only the tool(s) under test -- the test for "every entry in
    # bin/ is covered" is the main() check over the real bin/.
    coverage_cases = [
        ("bare bin/ alternative is not coverage",
         [{"name": "catch-all", "when": "^(bin/)",
           "run": "echo ok"}],
         ["hermes-doctor"],
         "only catch-all"),
        ("blanket ^bin/.* is not coverage",
         [{"name": "catch-all", "when": "^bin/.*",
           "run": "echo ok"}],
         ["hermes-doctor"],
         "only catch-all"),
        ("specific name is real coverage",
         [{"name": "specific", "when": r"^(bin/hermes-doctor)$",
           "run": "echo ok"}],
         ["hermes-doctor"],
         None),
        ("one specific gate among catch-alls counts",
         [
             {"name": "catch-all", "when": "^(bin/)",
              "run": "echo ok"},
             {"name": "specific", "when": r"^(bin/hermes-doctor)$",
              "run": "echo ok"},
         ],
         ["hermes-doctor"],
         None),
        ("tool with no gate at all is rejected",
         [{"name": "specific", "when": r"^(bin/other-tool)$",
           "run": "echo ok"}],
         ["hermes-doctor"],
         "matches no gate"),
        # The audit's own catch-all `when` must not cover a new tool.
        ("audit gate alone does not cover a new tool",
         [{"name": "audit", "when": "^(bin/)",
           "run": "echo ok"}],
         ["brand-new-tool"],
         "only catch-all"),
        ("audit gate plus a specific gate covers a new tool",
         [
             {"name": "audit", "when": "^(bin/)",
              "run": "echo ok"},
             {"name": "specific", "when": r"^(bin/brand-new-tool)$",
              "run": "echo ok"},
         ],
         ["brand-new-tool"],
         None),
    ]

    # (label, gates, suite files, expected failure substring or None)
    suite_cases = [
        ("bare tests/ catch-all is not coverage",
         [{"name": "catch-all", "when": r"^(tests/)",
           "run": "echo ok"}],
         ["test_brand_new.py"],
         "only catch-all"),
        ("blanket tests/ pattern is not coverage",
         [{"name": "catch-all", "when": r"^tests/test_\w+\.py$",
           "run": "echo ok"}],
         ["test_brand_new.py"],
         "only catch-all"),
        ("specific tests/test_*.py is real coverage",
         [{"name": "specific",
           "when": r"^(tests/test_brand_new\.py)$",
           "run": "echo ok"}],
         ["test_brand_new.py"],
         None),
        ("one specific gate among tests/ catch-alls counts",
         [
             {"name": "catch-all", "when": r"^(tests/)",
              "run": "echo ok"},
             {"name": "specific",
              "when": r"^(tests/test_brand_new\.py)$",
              "run": "echo ok"},
         ],
         ["test_brand_new.py"],
         None),
        # The audit's own catch-all `when` must not cover a new suite.
        ("audit gate alone does not cover a new suite",
         [{"name": "audit",
           "when": r"^(tests/test_\w+\.py|bin/)",
           "run": "echo ok"}],
         ["test_brand_new.py"],
         "only catch-all"),
        ("audit gate plus a specific gate covers a new suite",
         [
             {"name": "audit",
              "when": r"^(tests/test_\w+\.py|bin/)",
              "run": "echo ok"},
             {"name": "specific",
              "when": r"^(tests/test_brand_new\.py)$",
              "run": "echo ok"},
         ],
         ["test_brand_new.py"],
         None),
    ]

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)

        # Each coverage case gets its own bin/ so it contains only the
        # tool(s) under test -- the cross-cutting "iterate every entry"
        # behaviour is what main() exercises over the real bin/.
        for case_no, (label, gates, tools, expect) in enumerate(coverage_cases):
            fake_bin = tmp / f"coverage-{case_no}" / "bin"
            fake_bin.mkdir(parents=True)
            for tool in tools:
                (fake_bin / tool).write_text("#!/usr/bin/sh\n")
            fails = check_tools(gates, fake_bin)
            if expect is None:
                if fails:
                    failures.append(
                        f"{label}: expected pass, got {fails}")
            else:
                if not any(expect in f for f in fails):
                    failures.append(
                        f"{label}: expected a failure containing {expect!r}, "
                        f"got {fails}")

        for case_no, (label, gates, suites_, expect) in enumerate(suite_cases):
            fake_tests = tmp / f"suite-{case_no}" / "tests"
            fake_tests.mkdir(parents=True)
            for suite in suites_:
                (fake_tests / suite).write_text("#!/usr/bin/env python3\n")
            fails, _ = check_suites(gates, fake_tests)
            if expect is None:
                if fails:
                    failures.append(
                        f"{label}: expected pass, got {fails}")
            else:
                if not any(expect in f for f in fails):
                    failures.append(
                        f"{label}: expected a failure containing {expect!r}, "
                        f"got {fails}")

        # Syntax-floor cases: build one broken and one clean file in a fresh
        # tmpdir each, then assert the gate's run command fails on the broken
        # one and passes on the clean one.
        syntax_cases = [
            ("bash -n", "echo hi", "echo hi("),
            ("py_compile", "print(1)", "print(1"),
        ]
        for kind, good, bad in syntax_cases:
            work = tmp / kind.replace(" ", "_")
            work.mkdir()
            clean = work / "ok.sh" if kind == "bash -n" else work / "ok.py"
            broken_p = work / "bad.sh" if kind == "bash -n" else work / "bad.py"
            clean.write_text(good + "\n")
            broken_p.write_text(bad + "\n")
            cmd = (["bash", "-n", str(clean)] if kind == "bash -n"
                   else ["python3", "-m", "py_compile", str(clean)])
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                failures.append(
                    f"{kind} on clean {clean.name} returned "
                    f"{r.returncode}: {r.stderr.strip()}")
            cmd = (["bash", "-n", str(broken_p)] if kind == "bash -n"
                   else ["python3", "-m", "py_compile", str(broken_p)])
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                failures.append(
                    f"{kind} on broken {broken_p.name} returned 0: "
                    f"{r.stdout.strip()}{r.stderr.strip()}")
            # Footer left behind by py_compile -- not a failure.
            if (work / "__pycache__").is_dir():
                for cached in (work / "__pycache__").iterdir():
                    cached.unlink()
                (work / "__pycache__").rmdir()

    # The `find -exec` arm of the syntax-floor audit: a gate that uses
    # `find ... -exec` cannot fail, because find fails to propagate the
    # exit status of what it runs.

    # The arm is a string check, so it is pinned with a case that must be
    # rejected -- and with the near miss that must not be: a tool whose
    # own name ends in `-exec` is no `find -exec`.
    find_exec_cases = [
        ("find -exec is rejected",
         "find install -name '*.sh' -exec bash -n {} \\;",
         True),
        ("a tool named -exec is not find -exec",
         "bash -n bin/review-execute",
         False),
    ]
    for label, run, must_fail in find_exec_cases:
        reported = bool(re.search(r"\bfind\b[^|]*\s-exec\b", run))
        if reported != must_fail:
            failures.append(
                f"{label}: the find -exec check reported {reported}, "
                f"expected {must_fail} for run={run!r}")

    # The real manifest's audit fires on bin/ and tests/test_*.py paths. The
    # probes name no file the `when` lists literally, so each exercises its arm.
    real_gates = manifest.gates(GATES)
    coverage_gates = [g for g in real_gates
                      if g.get("name") == "gate coverage"]
    if not coverage_gates:
        failures.append("real manifest has no `gate coverage` gate")
    else:
        audit_when = coverage_gates[0].get("when", "")
        for probe in ("bin/pr-ready", "tests/test_pr_ready.py"):
            if not re.search(audit_when, probe):
                failures.append(
                    f"`gate coverage`'s `when` ({audit_when!r}) does not "
                    f"select {probe}; the audit must fire on bin/ and "
                    f"tests/test_*.py diffs")

    if failures:
        print("FAIL self_tests:")
        for f in failures:
            print(" -", f)
        return 1
    print(f"PASS self_tests ({len(coverage_cases)} coverage, "
          f"{len(suite_cases)} suite, {len(syntax_cases)} syntax-floor cases)")
    return 0


if __name__ == "__main__":
    rc = self_tests()
    if rc != 0:
        sys.exit(rc)
    sys.exit(main())
