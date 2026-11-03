#!/usr/bin/env python3
"""Pin what gets posted on a pull request, and what does not.

The findings file is a working document; the posted review is a verdict
on one commit. The suite pins the difference: which findings survive to
be posted, what the verdict is, what the text carries, and when nothing
is posted at all.

The module is driven directly -- it is the pure part, and the driver
around it is a separate suite.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "lib" / "review_post.py"


def _load_module():
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    tmp.close()
    shutil.copy(str(MODULE_PATH), tmp.name)
    spec = importlib.util.spec_from_file_location("_rp_under_test",
                                                  tmp.name)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH} as a module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load_module()


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def located(severity="high", path="src/a.rs", line=10, claim="a claim",
            recommendation="a fix", status="located"):
    return {"severity": severity, "path": path, "line": line,
            "snippet": "the snippet", "claim": claim,
            "settled_by": "the settler", "recommendation": recommendation,
            "status": status, "in_diff": True}


def case_only_a_located_finding_survives(failures):
    """A finding finding-check could not locate is not posted, and one
    whose quote carries the agent's own redaction is not posted either."""
    findings = [
        located(status="located"),
        located(path="src/b.rs", status="not-found"),
        located(path="src/c.rs", status="redaction-artifact"),
        located(path="src/d.rs", status="path-missing"),
        located(path="src/e.rs", status="malformed"),
    ]
    review = rp.build(findings, "abc123")
    if review["count"] != 1:
        fail(failures, "survives: how many were posted", review["count"])
    if not review["comments"]:
        fail(failures, "survives: no comments")
        return
    if review["comments"][0]["path"] != "src/a.rs":
        fail(failures, "survives: the wrong finding survived",
             review["comments"][0]["path"])


def case_a_blocking_finding_requests_changes(failures):
    """A high or medium finding blocks; a low one does not."""
    review = rp.build([located(severity="high")], "abc123")
    if review["event"] != rp.REQUEST_CHANGES:
        fail(failures, "blocking-high: event", review["event"])
    review = rp.build([located(severity="medium")], "abc123")
    if review["event"] != rp.REQUEST_CHANGES:
        fail(failures, "blocking-medium: event", review["event"])
    review = rp.build([located(severity="low")], "abc123")
    if review["event"] != rp.APPROVE:
        fail(failures, "blocking-low: event", review["event"])


def case_nothing_blocking_is_an_approval(failures):
    """A review that requests changes nobody has to act on teaches the
    loop to ignore the verdict."""
    review = rp.build([located(severity="low", path="src/a.rs")], "abc123")
    if review["event"] != rp.APPROVE:
        fail(failures, "approve: event", review["event"])
    if "Nothing blocking" not in review["body"]:
        fail(failures, "approve: the body does not say so",
             review["body"][:120])
    review = rp.build([], "abc123")
    if review["event"] != rp.APPROVE:
        fail(failures, "approve-empty: event", review["event"])
    if review["count"] != 0:
        fail(failures, "approve-empty: count", review["count"])


def case_the_verdict_is_the_worst_surviving_severity(failures):
    """A low finding beside a high one does not soften the verdict."""
    review = rp.build([located(severity="low", path="src/a.rs"),
                       located(severity="high", path="src/b.rs")], "abc123")
    if review["event"] != rp.REQUEST_CHANGES:
        fail(failures, "worst: event", review["event"])
    # And the blocking one is the first thing the reader sees.
    if review["comments"][0]["path"] != "src/b.rs":
        fail(failures, "worst: the order the findings are posted in",
             review["comments"][0]["path"])


def case_the_review_is_bound_to_the_commit_it_read(failures):
    """The commit is named in the body and in the call, so a reader can
    tell which tree the verdict is about."""
    review = rp.build([located()], "abc123")
    if review["commit_id"] != "abc123":
        fail(failures, "bound: commit_id", review["commit_id"])
    if "abc123" not in review["body"]:
        fail(failures, "bound: the body does not name the commit",
             review["body"][:160])


def case_a_moved_head_posts_nothing(failures):
    """A verdict about one tree filed against another is worse than no
    verdict."""
    review = rp.build([located()], "abc123", current_head="def456")
    if review["post"] is not False:
        fail(failures, "moved: it would post", review)
        return
    if "moved" not in review["reason"]:
        fail(failures, "moved: the reason does not say the head moved",
             review["reason"][:160])
    if review.get("event"):
        fail(failures, "moved: it carries a verdict anyway",
             review.get("event"))
    if review.get("comments"):
        fail(failures, "moved: it carries comments anyway")


def case_the_same_head_posts(failures):
    """The head check compares exactly, so a head that did not move is
    not refused."""
    review = rp.build([located()], "abc123", current_head="abc123")
    if review["post"] is not True:
        fail(failures, "same-head: it would not post", review["reason"])
    # And no current head at all means the check did not apply.
    review = rp.build([located()], "abc123")
    if review["post"] is not True:
        fail(failures, "no-current-head: it would not post",
             review["reason"])


def case_the_text_carries_no_round_numbers(failures):
    """A round number is copied into commit messages and files by the
    implementer, which is how "round 2" ends up in a source file."""
    for text in ("fixed in round 2", "as Review 3 said",
                 "seen again in Round 12"):
        finding = located(claim=text)
        review = rp.build([finding], "abc123")
        if review["post"] is not False:
            fail(failures, "round: it would post", text)
            continue
        if "round" not in review["reason"].lower():
            fail(failures, "round: the reason does not name it",
                 review["reason"][:160])


def case_the_text_carries_no_finding_labels(failures):
    """A label like R2-B1 is the same thing under another name."""
    for text in ("see R2-B1", "as R2B1 says", "R2"):
        finding = located(claim=text)
        review = rp.build([finding], "abc123")
        if review["post"] is not False:
            fail(failures, "label: it would post", text)
            continue
        if "label" not in review["reason"].lower():
            fail(failures, "label: the reason does not name it",
                 review["reason"][:160])


def case_ordinary_words_are_not_refused(failures):
    """The check has to be specific enough not to refuse a review that
    says "the router rewrites the path"."""
    for text in ("the roundtrip handler leaks",
                 "the reviewer's notes are stale",
                 "the README says otherwise",
                 "a RUN constant is missing",
                 "the router rewrites the path"):
        finding = located(claim=text)
        review = rp.build([finding], "abc123")
        if review["post"] is not True:
            fail(failures, "ordinary: it refused", f"{text!r}: "
                 f"{review['reason'][:120]}")


def case_a_non_object_finding_does_not_abort_the_batch(failures):
    """One malformed entry comes back dropped, and the good findings
    beside it are still posted."""
    findings = ["not an object", located(), 42, None]
    review = rp.build(findings, "abc123")
    if review["count"] != 1:
        fail(failures, "malformed: how many were posted", review["count"])
    if not review["comments"] or review["comments"][0]["path"] != "src/a.rs":
        fail(failures, "malformed: the good finding was lost",
             review.get("comments"))


def case_a_finding_without_a_line_is_dropped(failures):
    """A finding with no line has nowhere to be attached."""
    finding = located()
    finding["line"] = None
    finding["path"] = ""
    review = rp.build([finding, located(path="src/b.rs")], "abc123")
    if review["count"] != 1:
        fail(failures, "no-line: how many were posted", review["count"])
    if review["comments"][0]["path"] != "src/b.rs":
        fail(failures, "no-line: the wrong finding survived",
             review["comments"][0]["path"])


def case_the_command_writes_the_review(failures):
    """The module runs as a command too, so the driver does not have to
    import it."""
    with tempfile.TemporaryDirectory() as t:
        findings_path = Path(t) / "findings.json"
        findings_path.write_text(json.dumps([located()]))
        out_path = Path(t) / "review.json"
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), str(findings_path),
             "--head", "abc123", "--out", str(out_path)],
            capture_output=True, text=True)
        if result.returncode != 0:
            fail(failures, "command exit", result.returncode)
            return
        review = json.loads(out_path.read_text())
        if review["event"] != rp.REQUEST_CHANGES:
            fail(failures, "command: event", review["event"])
        if review["count"] != 1:
            fail(failures, "command: count", review["count"])


def case_the_command_refuses_a_missing_head(failures):
    """--head is required: a review with no commit is not a review."""
    with tempfile.TemporaryDirectory() as t:
        findings_path = Path(t) / "findings.json"
        findings_path.write_text(json.dumps([]))
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), str(findings_path)],
            capture_output=True, text=True)
        if result.returncode != 2:
            fail(failures, "no-head: exit code", result.returncode)


def main() -> int:
    if not MODULE_PATH.is_file():
        print(f"FAIL module not built: {MODULE_PATH}")
        return 1
    failures: list = []
    for case in CASES:
        try:
            case(failures)
        except Exception as exc:  # noqa: BLE001
            fail(failures, f"{case.__name__} raised",
                 f"{type(exc).__name__}: {exc}")
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS review_post: only located findings; the verdict; the commit "
          "it is bound to; a moved head; no round numbers; no labels; "
          "malformed entries; the command")
    return 0


CASES = (case_only_a_located_finding_survives,
         case_a_blocking_finding_requests_changes,
         case_nothing_blocking_is_an_approval,
         case_the_verdict_is_the_worst_surviving_severity,
         case_the_review_is_bound_to_the_commit_it_read,
         case_a_moved_head_posts_nothing,
         case_the_same_head_posts,
         case_the_text_carries_no_round_numbers,
         case_the_text_carries_no_finding_labels,
         case_ordinary_words_are_not_refused,
         case_a_non_object_finding_does_not_abort_the_batch,
         case_a_finding_without_a_line_is_dropped,
         case_the_command_writes_the_review,
         case_the_command_refuses_a_missing_head)


if __name__ == "__main__":
    sys.exit(main())
