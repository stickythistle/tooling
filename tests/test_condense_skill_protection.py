#!/usr/bin/env python3
"""Pin that a condense pass protects what a skill file says.

A lane running `condense-prose` as the finishing step cut meaning, not just
words, on two PRs in a row: on #148 it turned "say what the reviewer did
**not** check" into "name what was checked" -- the opposite -- and on #147 it
cut an instruction and the example that carried a rule.

A skill file is not prose that describes behaviour. It *is* the behaviour.
So the finder marks a skill file's blocks, a block outside one is not
marked, and the skill says what cannot be cut near the top.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIND_PROSE = REPO_ROOT / "skills" / "condense-prose" / "find_prose.py"
CONDENSE_SKILL = REPO_ROOT / "skills" / "condense-prose" / "SKILL.md"
COORDINATOR_SKILL = REPO_ROOT / "skills" / "pr-review-coordinator" / "SKILL.md"

SKILL_TEXT = """---
name: example-skill
description: Does a thing. Triggers - "do the thing", "run the thing".
---

# Example skill

Run the gate before the commit. A failure is feedback, not a stopping point.

A route's response is tested through the route, not through the repo
function under it.

Say in the approval what the reviewer did not check, such as a merge order
two PRs share.

This paragraph is a restatement of the rules above it, and it runs on past
the forty words the finder allows a block, so the finder has a reason to
report it as oversize even though the rules it repeats are the ones above.
"""


def fail(failures: list[str], msg: str, detail: str = "") -> None:
    failures.append(f"{msg}: {detail}" if detail else msg)


def run_find_prose(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FIND_PROSE), "--json", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )


def blocks_for(proc: subprocess.CompletedProcess) -> list[dict]:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"non-JSON output from find_prose.py: {proc.stdout[:200]!r} "
            f"(stderr={proc.stderr!r})") from exc


def make_skill_tree(tmp: Path) -> Path:
    """A tree with one skill file and one ordinary markdown file."""
    root = tmp / "repo"
    skill = root / "skills" / "example-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_TEXT)
    docs = root / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "# Docs\n\n"
        "Run the gate before the commit, and say what the reviewer did not\n"
        "check, such as a merge order two PRs share.\n"
    )
    return root


# ---------------------------------------------------------------------------
# The finder marks a skill file's blocks.

def test_a_skill_files_blocks_are_marked():
    with tempfile.TemporaryDirectory() as t:
        root = make_skill_tree(Path(t))
        proc = run_find_prose("skills/example-skill/SKILL.md", cwd=root)
        assert proc.returncode == 0, proc.stderr
        blocks = blocks_for(proc)
        assert blocks, "the finder flagged nothing in the skill file"
        for block in blocks:
            if "skill-file" not in block.get("context", []):
                fail(failures_holder, "skill block not marked",
                     f"{block['path']}:{block['line']} {block['findings']}")


# A module-level holder so the helper above can report without threading a
# list through every call.
failures_holder: list[str] = []


def test_a_block_outside_a_skill_file_is_not_marked():
    with tempfile.TemporaryDirectory() as t:
        root = make_skill_tree(Path(t))
        proc = run_find_prose("docs/README.md", cwd=root)
        assert proc.returncode == 0, proc.stderr
        for block in blocks_for(proc):
            if "skill-file" in block.get("context", []):
                fail(failures_holder, "an ordinary file was marked as a "
                     "skill file", block["path"])


def test_a_skill_file_is_still_scanned():
    # The mark is context, not an exemption. A restatement in a skill file
    # is a finding like any other, or the protection would have cost the
    # lane the findings in the files it most needs to read.
    with tempfile.TemporaryDirectory() as t:
        root = make_skill_tree(Path(t))
        proc = run_find_prose("skills/example-skill/SKILL.md", cwd=root)
        assert proc.returncode == 0, proc.stderr
        blocks = blocks_for(proc)
        assert blocks, "the finder found nothing in the skill file"
        for block in blocks:
            if not block["findings"]:
                fail(failures_holder, "a skill block has no finding",
                     f"{block['path']}:{block['line']}")


def test_skill_file_is_never_a_finding_by_itself():
    # `pr-ready` refuses when the finder returns any block, so a mark that
    # stands alone would refuse every PR that touches a skill file.
    with tempfile.TemporaryDirectory() as t:
        root = make_skill_tree(Path(t))
        proc = run_find_prose("skills/example-skill/SKILL.md", cwd=root)
        assert proc.returncode == 0, proc.stderr
        for block in blocks_for(proc):
            if "skill-file" in block["findings"]:
                fail(failures_holder, "skill-file is a finding",
                     f"{block['path']}:{block['line']} {block['findings']}")


def test_a_clean_one_line_skill_edit_gives_no_findings():
    # The acceptance criterion: a repo whose only change is one line in a
    # skill file gives `pr-ready` nothing to refuse on.
    with tempfile.TemporaryDirectory() as t:
        root = Path(t) / "repo"
        skill = root / "skills" / "x"
        skill.mkdir(parents=True)
        before = "---\nname: x\ndescription: Does a thing.\n---\n\n# X\n\nRun the gate.\n"
        after = "---\nname: x\ndescription: Does a thing.\n---\n\n# X\n\nRun the gate first.\n"
        (skill / "SKILL.md").write_text(before)
        git = ["git", "-C", str(root)]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
        subprocess.run(git + ["config", "user.name", "t"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "base"], check=True)
        (skill / "SKILL.md").write_text(after)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "edit"], check=True)

        proc = run_find_prose("--diff", "HEAD~1", cwd=root)
        assert proc.returncode == 0, proc.stderr
        blocks = blocks_for(proc)
        # `pr-ready` counts the blocks the finder returns.
        if blocks:
            fail(failures_holder, "a clean one-line skill edit was flagged",
                 repr(blocks))


def test_the_skill_file_notice_names_what_cannot_be_cut():
    # The text report is what a lane reads. It has to say what a skill
    # file is and what may not be cut, or the finding is a label with no
    # meaning.
    with tempfile.TemporaryDirectory() as t:
        root = make_skill_tree(Path(t))
        proc = subprocess.run(
            [sys.executable, str(FIND_PROSE), "skills/example-skill/SKILL.md"],
            cwd=str(root), capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        assert "skill-file" in out, out[:600]
        assert "cannot be cut" in out, out[:600]
        # And it names the four things, or a lane cannot act on it.
        for term in ("instruction", "example", "trigger phrase", "negation"):
            if term not in out:
                fail(failures_holder, f"the notice does not name {term}",
                     out[:600])


def test_the_skill_says_what_cannot_be_cut_near_the_top():
    # "Near the top" is the acceptance criterion. A rule at the bottom of
    # a file a lane reads once is a rule the lane has already gone past.
    text = CONDENSE_SKILL.read_text()
    lines = text.splitlines()
    flat_text = " ".join(text.split()).lower()
    # The heading that carries the rule.
    heading = None
    for i, line in enumerate(lines):
        if line.strip() == "## What cannot be cut":
            heading = i
            break
    assert heading is not None, (
        "the condense-prose skill has no 'What cannot be cut' section")
    # Within the first quarter of the file: a lane reads the top first.
    assert heading < len(lines) // 4, (
        f"the rule is at line {heading + 1} of {len(lines)}; it has to be "
        "near the top, where the lane reads first")

    body = " ".join("\n".join(lines[heading:heading + 40]).split()).lower()
    for term in ("instruction", "example", "trigger phrase", "negation"):
        if term not in body:
            fail(failures_holder, f"the rule does not name {term}",
                 body[:600])
    # And it says what MAY be cut, so the rule is not "cut nothing".
    if "restatement" not in body and "filler" not in body:
        fail(failures_holder,
             "the rule does not say what may be cut", body[:600])


def test_a_flagged_block_with_nothing_to_remove_stays_and_is_reported_as_kept():
    # The acceptance criterion: if a flagged block has none of those to
    # remove, it stays and is reported as kept.
    flat = " ".join(CONDENSE_SKILL.read_text().split()).lower()
    # The acceptance criterion's own words: a flagged block with none of
    # those to remove "stays and is reported as kept".
    if "it stays, and you report it as kept" not in flat:
        fail(failures_holder,
             "the skill does not say a protected block stays and is reported "
             "as kept", "")


# ---------------------------------------------------------------------------
# The condense pass reports the sentence before and after.

def test_the_skill_asks_for_the_sentence_before_and_after():
    flat = " ".join(CONDENSE_SKILL.read_text().split()).lower()
    if "before" not in flat or "after" not in flat:
        fail(failures_holder,
             "the skill does not ask for the sentence before and after", "")
    # The phrase the coordinator reads.
    if "for every block you changed" not in flat:
        fail(failures_holder,
             "the skill does not ask for a pair per changed block", "")
    # And the kept blocks, so a kept block and an unfinished one do not
    # look the same.
    if "report the blocks you kept" not in flat:
        fail(failures_holder, "the skill does not ask for the kept blocks",
             "")


# ---------------------------------------------------------------------------
# The coordinator checks that pass before approving.

def test_the_coordinator_finishing_order_checks_the_condense_pass():
    # The acceptance criterion names the finishing order: the numbered list
    # at the top of the coordinator skill. A rule anywhere else in the file
    # is not the finishing order, and a lane reading the loop would not see
    # it.
    text = COORDINATOR_SKILL.read_text()
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == "## A pull request's life"), None)
    assert start is not None, "the coordinator skill has no 'life' section"
    # The order runs from that heading to the next one.
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    order = "\n".join(lines[start:end]).lower()
    # Prose wraps, so a phrase can straddle a line break. The check is on
    # the words, not on the line they happen to sit on.
    flat = " ".join(order.split())

    if "condense pass" not in flat:
        fail(failures_holder,
             "the finishing order never mentions the condense pass", order)
    if "lost instruction" not in flat:
        fail(failures_holder,
             "the finishing order does not check for lost instructions", order)
    if "reversed negation" not in flat:
        fail(failures_holder,
             "the finishing order does not check for reversed negations",
             order)
    # And the check has to be in the order at all, not only in the body.
    order_lines = lines[start:end]
    check_idx = next((i for i, ln in enumerate(order_lines)
                      if "lost instruction" in ln.lower()), None)
    approve_idx = next((i for i, ln in enumerate(order_lines)
                         if "approves at that sha" in ln.lower()), None)
    assert check_idx is not None, (
        "the finishing order does not check for lost instructions")
    assert approve_idx is not None, (
        "the finishing order has no approval step")
    # The check's step comes before the approval's step, or it is not a
    # gate.
    if check_idx > approve_idx:
        fail(failures_holder,
             "the condense check comes after the approval in the order",
             f"check: {order_lines[check_idx]!r} "
             f"approval: {order_lines[approve_idx]!r}")


def test_the_coordinator_checks_before_it_approves():
    # The check has to come before the approval, or it is not a gate.
    text = COORDINATOR_SKILL.read_text()
    check = text.lower().find("lost instruction")
    approve = text.lower().find("approves at that sha")
    assert check != -1, "the coordinator does not check for lost instructions"
    assert approve != -1, "the coordinator's approval step is missing"
    # The first approval mention after the check must come after it. The
    # loop's numbered order has the check in step 5 and the approval there.
    if check > approve:
        fail(failures_holder,
             "the condense check comes after the approval",
             f"check at {check}, approval at {approve}")


def main() -> int:
    failures: list[str] = []
    global failures_holder
    failures_holder = failures
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
        for f in failures:
            print(f" - {f}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
