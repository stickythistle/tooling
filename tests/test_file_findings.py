#!/usr/bin/env python3
"""Pin the finding filer's duplicate detection against the tracker.

The tracker is stubbed so the suite never calls GitHub or a model. The judge
is a real subprocess, though: the filer passes its prompt as one argument and
needs the reply in the issue-number form its contract defines.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "pipeline" / "file_findings.py"


def load_module() -> ModuleType:
    """Load the filer with its generic marker, as a fresh installation has it."""
    previous = os.environ.get("PIPELINE_FINDING_MARKER")
    os.environ["PIPELINE_FINDING_MARKER"] = "agent-finding:"
    try:
        spec = importlib.util.spec_from_file_location("file_findings_under_test",
                                                      TOOL)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load {TOOL}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop("PIPELINE_FINDING_MARKER", None)
        else:
            os.environ["PIPELINE_FINDING_MARKER"] = previous


FILE_FINDINGS = load_module()


# Each row pairs an original title with a later rewording. The final flag
# says whether the pair needs the judge or the mechanical overlap check.
TITLE_PAIRS = [
    ("The `only` scope is checked against the manifest's script **names** at",
     "The `only` scope's submission guard compares names while the runtime predicate compares ids",
     True),
    ("The `only` scope is checked against the manifest's script **names** at",
     "the `only` scope is checked against names at submission and against ids at selection",
     True),
    ("`only` is enforced against the **live** catalog, so a later",
     "`only` is enforced against the live catalog, so a later `POST /catalogs` changes an earlier Trigger's candidate set",
     True),
    ("`only` is enforced against the **live** catalog, so a later",
     "`only` is enforced against the live catalog, so a later catalog post widens it",
     False),
    ("Migration 0067 backfills with `CROSS JOIN medium`, which silently",
     "Migration 0067's backfill silently matches zero rows when the `Medium` rung is absent",
     True),
    ("Migration 0067 backfills with `CROSS JOIN medium`, which silently",
     "migration 0067's `CROSS JOIN medium` matches zero rows when the `Medium` rung is absent",
     True),
    ("The runner's ownership check on the artifact upload and link routes is a",
     "The artifact routes authorize on an actor string a user account can collide with",
     True),
    ("The raw route's `git_ref` query parameter changes meaning with no",
     "The raw route's `git_ref` parameter changed meaning with no versioning",
     False),
    ("The agent's stand-down sends SIGTERM to the **direct child only**, so a",
     "The agent's stand-down signals the direct child only",
     False),
    ('The digest\'s "already digested this week" check counts **any**',
     'The digest\'s "already digested this week" check counts any `placeholder.digest` row, delivered or not',
     False),
]


def issue(number: int, title: str, body: str) -> dict:
    return {"number": number, "title": title, "body": body}


@contextlib.contextmanager
def tracker(issues: list[dict]):
    """Serve `issues` from the issue-list call and record its search."""
    searches: list[str] = []

    def fake_run(cmd, **kwargs):
        search = cmd[cmd.index("--search") + 1]
        searches.append(search)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(issues), "")

    previous = FILE_FINDINGS.run
    FILE_FINDINGS.run = fake_run
    try:
        yield searches
    finally:
        FILE_FINDINGS.run = previous


def judge_stub(path: Path, log: Path) -> None:
    """Write a judge that accepts one prompt and answers with an issue number."""
    path.write_text("""\
#!/usr/bin/env python3
import os
import sys

if len(sys.argv) != 2:
    raise SystemExit("expected one prompt argument")
with open(os.environ["FAKE_JUDGE_LOG"], "a") as handle:
    handle.write(sys.argv[1] + "\\n")
print(os.environ["FAKE_JUDGE_NUMBER"])
""")
    path.chmod(0o755)
    log.write_text("")


def triage_file(path: Path, titles: list[str]) -> None:
    blocks = []
    for number, title in enumerate(titles, 1):
        blocks.append(
            f"# Finding {number}: {title}\n\n"
            "**Verdict:** still real\n"
            "**Severity:** Correctness (High)\n\n"
            "## What the code does today\n\nIt does the wrong thing.\n\n"
            "## Acceptance criteria\n\nIt stops doing the wrong thing.\n\n"
            "## Candidate mechanisms\n\nFix it.\n"
        )
    path.write_text("\n".join(blocks))


def run_filer(tmp: Path, titles: list[str], issues: list[dict]) -> str:
    """Run the filer's dry-run path and return what it printed."""
    source = tmp / "triage.md"
    triage_file(source, titles)
    old_argv = sys.argv
    sys.argv = [
        str(TOOL), str(source), "--repo", "owner/repo",
        "--queue", str(tmp / "queue"), "--dry-run",
    ]
    try:
        with tracker(issues), contextlib.redirect_stdout(io.StringIO()) as output:
            FILE_FINDINGS.main()
            return output.getvalue()
    finally:
        sys.argv = old_argv


def test_the_previous_marker_spelling_is_pipeline_owned() -> None:
    """The marker comment's shape identifies pipeline issues, not its prefix."""
    original, rewritten, _ = TITLE_PAIRS[3]
    body = (f"<!-- hypatia-finding: "
            f"{FILE_FINDINGS.fingerprint(original)} -->")
    with tracker([issue(101, original, body)]) as searches:
        found = FILE_FINDINGS.already_filed(
            "owner/repo", rewritten, FILE_FINDINGS.fingerprint(rewritten))
    assert found == 101, found
    assert any(" OR " in query for query in searches), searches


def test_every_rewording_is_a_duplicate() -> None:
    """Each issue-title pair is caught mechanically or by the judge."""
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        judge = tmp / "judge"
        log = tmp / "judge.log"
        judge_stub(judge, log)

        old_judge = os.environ.get("PIPELINE_DEDUP_JUDGE")
        old_number = os.environ.get("FAKE_JUDGE_NUMBER")
        old_log = os.environ.get("FAKE_JUDGE_LOG")
        old_timeout = os.environ.get("PIPELINE_DEDUP_TIMEOUT")
        os.environ["PIPELINE_DEDUP_JUDGE"] = str(judge)
        os.environ["FAKE_JUDGE_NUMBER"] = "101"
        os.environ["FAKE_JUDGE_LOG"] = str(log)
        os.environ["PIPELINE_DEDUP_TIMEOUT"] = "5"
        try:
            for original, rewritten, expects_judge in TITLE_PAIRS:
                log.write_text("")
                body = (f"<!-- hypatia-finding: "
                        f"{FILE_FINDINGS.fingerprint(original)} -->")
                with tracker([issue(101, original, body)]):
                    found = FILE_FINDINGS.already_filed(
                        "owner/repo", rewritten,
                        FILE_FINDINGS.fingerprint(rewritten))
                assert found == 101, (original, rewritten, found)
                prompt = log.read_text()
                called = bool(prompt)
                assert called == expects_judge, (original, rewritten, called)
                if expects_judge:
                    assert "earliest" in prompt, prompt
        finally:
            for key, value in (
                    ("PIPELINE_DEDUP_JUDGE", old_judge),
                    ("FAKE_JUDGE_NUMBER", old_number),
                    ("FAKE_JUDGE_LOG", old_log),
                    ("PIPELINE_DEDUP_TIMEOUT", old_timeout)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_the_original_is_reported_instead_of_a_later_refiling() -> None:
    """When several pipeline issues are one finding, report the oldest."""
    original, rewritten, _ = TITLE_PAIRS[0]
    later = TITLE_PAIRS[1][1]
    issues = [
        issue(101, original,
              f"<!-- hypatia-finding: {FILE_FINDINGS.fingerprint(original)} -->"),
        issue(102, later,
              f"<!-- hypatia-finding: {FILE_FINDINGS.fingerprint(later)} -->"),
    ]

    calls = []

    def judge(title, candidates):
        calls.append((title, candidates))
        if any(num == 102 for num, _ in candidates):
            return 102
        return 101

    previous = FILE_FINDINGS.ask_judge
    FILE_FINDINGS.ask_judge = judge
    try:
        with tracker(issues):
            found = FILE_FINDINGS.already_filed(
                "owner/repo", rewritten, FILE_FINDINGS.fingerprint(rewritten))
    finally:
        FILE_FINDINGS.ask_judge = previous
    assert found == 101, found
    assert len(calls) == 2, calls


def test_an_exact_match_participates_in_choosing_the_oldest_duplicate() -> None:
    """An exact issue wins over a newer, strongly overlapping rewording."""
    exact = "Database publish exposes password in connection string"
    newer = f"{exact} today"
    issues = [
        issue(101, exact,
              f"<!-- hypatia-finding: {FILE_FINDINGS.fingerprint(exact)} -->"),
        issue(102, newer,
              f"<!-- hypatia-finding: {FILE_FINDINGS.fingerprint(newer)} -->"),
    ]
    with tracker(issues):
        found = FILE_FINDINGS.already_filed(
            "owner/repo", exact, FILE_FINDINGS.fingerprint(exact))
    assert found == 101, found


def test_an_older_candidate_is_judged_before_replacing_the_selected_issue() -> None:
    """Overlap nominates an older issue; the judge decides whether it matches."""
    incoming = "Artifact download lets users read another account files"
    older = "Artifact upload overwrites files belonging to another account"
    selected = "Artifact download lets users fetch another account files"
    issues = [
        issue(101, older,
              f"<!-- hypatia-finding: {FILE_FINDINGS.fingerprint(older)} -->"),
        issue(102, selected,
              f"<!-- hypatia-finding: {FILE_FINDINGS.fingerprint(selected)} -->"),
    ]

    calls = []

    def judge(title, candidates):
        calls.append((title, candidates))
        return 102 if len(calls) == 1 else None

    previous = FILE_FINDINGS.ask_judge
    FILE_FINDINGS.ask_judge = judge
    try:
        with tracker(issues):
            found = FILE_FINDINGS.already_filed(
                "owner/repo", incoming, FILE_FINDINGS.fingerprint(incoming))
    finally:
        FILE_FINDINGS.ask_judge = previous
    assert found == 102, found
    assert len(calls) == 2, calls
    assert calls[1][0] == selected, calls
    assert [num for num, _ in calls[1][1]] == [101], calls


def test_two_unrelated_findings_are_still_filed() -> None:
    """Dedup leaves unrelated findings alone."""
    with tempfile.TemporaryDirectory() as tmp_name:
        output = run_filer(tmp := Path(tmp_name),
                           ["The first unrelated defect", "The second unrelated defect"],
                           [])
    assert output.count("would file:") == 2, output
    assert "filed 2, already tracked 0, skipped 0" in output, output


def test_a_human_issue_is_not_matched_by_overlap() -> None:
    """A similar human issue remains a human issue, not a pipeline duplicate."""
    human = "Database publish exposes password in connection string today"
    incoming = "Database publish exposes password in connection string"
    want = FILE_FINDINGS.tokens(incoming)
    have = FILE_FINDINGS.tokens(human)
    overlap = len(want & have) / len(want | have)
    assert overlap >= 0.6, overlap

    with tempfile.TemporaryDirectory() as tmp_name:
        output = run_filer(Path(tmp_name), [incoming],
                           [issue(201, human, "A human wrote this issue.")])
    assert "would file:" in output, output
    assert "already tracks:" not in output, output


def main() -> int:
    failures: list[str] = []
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            try:
                case()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {exc}")
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
