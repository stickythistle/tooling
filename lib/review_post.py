#!/usr/bin/env python3
"""Turn a review's findings into the review that gets posted on a pull
request.

A review that a person reads and a review that a loop acts on are not the
same thing. The findings file is a working document: it names a severity,
a path, a line, a snippet and a claim. The posted review is a verdict on
one commit, with each surviving finding attached to the line it is about,
and nothing else. This module is the difference.

The rules:

- **Only a located finding survives.** `finding-check` has already
  decided whether the quoted code is in the file; a finding it could not
  locate, and one whose quote carries the agent's own redaction, are
  dropped here. A finding posted at a line nothing was found at is a
  finding about a line that does not exist.
- **The verdict is the worst surviving severity.** `high` and `medium`
  block; `low` does not. A review with nothing blocking is an approval,
  because a review that requests changes nobody has to act on teaches the
  loop to ignore the verdict.
- **The review is bound to the commit it read.** The commit is named in
  the body and in the API call, so a reader can tell which tree the
  verdict is about, and so a verdict on a tree nobody reviewed cannot be
  filed against a tree that has since moved.
- **The text carries no round numbers and no finding labels.** A label
  like `R2-B1` is copied into commit messages and files by the
  implementer, and a round number is meaningless to anyone reading the
  review later. The findings are identified by their path and line.
"""
from __future__ import annotations

import re
from typing import NoReturn

# The severities that block. `low` is reported, not blocking.
BLOCKING_SEVERITIES = ("high", "medium")

# The severities, worst first. The order is what a reader sees.
SEVERITY_ORDER = ("high", "medium", "low")

# The event posted with the review. GitHub calls an approval "APPROVE"
# and a request for changes "REQUEST_CHANGES".
APPROVE = "APPROVE"
REQUEST_CHANGES = "REQUEST_CHANGES"

USAGE = ("usage: review_post.py <findings.json> --head SHA "
         "[--current-head SHA] [--out FILE]")
USAGE_PAYLOAD = ("usage: review_post.py --payload --review review.json "
                 "--out FILE")

# The patterns the review text must not carry.
#
# A round number is a name for one pass of the loop, and it is copied into
# commit messages and files by the implementer, which is how "round 2"
# ends up in a source file. A finding label is the same thing under
# another name. Both are refused here rather than at push time, because a
# review that carries one has already been read by a person who may copy
# it.
ROUND_PATTERN = re.compile(r"\b(?:round|review)\s*[0-9]+\b", re.IGNORECASE)
# A label like `R2-B1` or `R2B1`: a letter, digits, a dash, more letters
# and digits. It has to be specific enough not to catch an ordinary word,
# and broad enough to catch the shapes implementers actually copy.
LABEL_PATTERN = re.compile(r"\bR[0-9]+(?:[-.]?[A-Z][0-9]*)?\b")


class PostError(Exception):
    """The findings cannot be turned into a review."""


def die(message: str) -> "NoReturn":
    import sys
    sys.stderr.write(message + "\n")
    sys.exit(2)


def severity_rank(severity: str) -> int:
    """How bad a severity is, worst first. An unknown severity sorts
    after every known one, so it cannot quietly become the verdict."""
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


def surviving(findings: list) -> list[dict]:
    """The findings that survive to be posted.

    A finding survives when `finding-check` located it: the quoted code
    was found in the file, and the line is the one it was found at. A
    finding in any other state is dropped, because posting it would put
    a claim about code nobody could find on the pull request.
    """
    kept: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if finding.get("status") != "located":
            continue
        path = finding.get("path")
        line = finding.get("line")
        if not isinstance(path, str) or not path:
            continue
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            continue
        kept.append(finding)
    # Worst first, then by place, so the review reads in the order a
    # reader wants: the worst finding, and the earliest line of it.
    kept.sort(key=lambda f: (severity_rank(f.get("severity", "")),
                              f.get("path", ""), f.get("line", 0)))
    return kept


def verdict(findings: list[dict]) -> str:
    """The event: a request for changes when something blocks, an approval
    when nothing does."""
    for finding in findings:
        if finding.get("severity") in BLOCKING_SEVERITIES:
            return REQUEST_CHANGES
    return APPROVE


def finding_body(finding: dict) -> str:
    """The text of one finding's inline comment.

    It names the severity, the claim and the recommendation. It does not
    name a round and it does not carry a label: the path and the line
    already say which finding this is.
    """
    severity = finding.get("severity", "low")
    claim = str(finding.get("claim", "")).strip()
    recommendation = str(finding.get("recommendation", "")).strip()
    lines = [f"**{severity}**"]
    if claim:
        lines.append("")
        lines.append(claim)
    if recommendation:
        lines.append("")
        lines.append(f"Recommendation: {recommendation}")
    return "\n".join(lines)


def review_body(findings: list[dict], head: str,
                event: str) -> str:
    """The review's body.

    It leads with the verdict and the commit it is about, then the
    findings in the order they were posted. A finding that blocks is
    listed before one that does not, so the body reads as the verdict
    reads.
    """
    counts = {s: 0 for s in SEVERITY_ORDER}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    summary = ", ".join(f"{counts[s]} {s}" for s in SEVERITY_ORDER
                        if counts[s])
    if not summary:
        summary = "no findings"

    head_line = (f"Reviewed commit `{head}`." if head else
                 "Reviewed the commit named below.")
    if event == APPROVE:
        opening = ("Nothing blocking survived. The findings below, if any, "
                   "are reported and not blocking.")
    else:
        opening = ("The findings below block this change. Each one is "
                   "attached to the line it is about.")

    parts = [head_line, "", opening, "", f"Findings: {summary}."]
    if findings:
        parts.append("")
        for finding in findings:
            parts.append(f"- `{finding.get('path')}:{finding.get('line')}`"
                         f" — {finding.get('severity')}: "
                         f"{str(finding.get('claim', '')).strip()}")
    return "\n".join(parts)


def check_text(text: str) -> str | None:
    """A refusal reason when the text carries a round number or a finding
    label, or None when it is clean.

    The check is on the whole text, because the thing implementers copy
    is not only the finding's own comment: it is whatever they read.
    """
    match = ROUND_PATTERN.search(text)
    if match:
        return (f"the review text carries a round number ({match.group(0)!r}); "
                f"round numbers are copied into commit messages and files")
    match = LABEL_PATTERN.search(text)
    if match:
        return (f"the review text carries a finding label ({match.group(0)!r}); "
                f"labels are copied into commit messages and files")
    return None


def build(findings: list, head: str, current_head: str | None = None) -> dict:
    """The review to post, or the refusal to post it.

    When `current_head` is given and differs from `head`, the tree has
    moved since the review read it, and nothing is posted: a verdict
    about one tree filed against another is worse than no verdict.
    """
    if current_head is not None and current_head != head:
        return {"post": False,
                "reason": f"the head moved: the review read {head} but the "
                          f"pull request is at {current_head}"}

    kept = surviving(findings)
    event = verdict(kept)
    body = review_body(kept, head, event)

    reason = check_text(body)
    if reason is None:
        for finding in kept:
            reason = check_text(finding_body(finding))
            if reason is not None:
                break
    if reason is not None:
        return {"post": False, "reason": reason}

    return {
        "post": True,
        "event": event,
        "commit_id": head,
        "body": body,
        "comments": [{"path": f["path"], "line": f["line"],
                      "body": finding_body(f)}
                     for f in kept],
        "count": len(kept),
    }


def payload(review: dict) -> dict:
    """The body the API call carries.

    It is the review without the module's own bookkeeping: the event,
    the commit, the body, and the inline comments. GitHub rejects a
    field it does not know, so the extra keys stay out.
    """
    return {
        "commit_id": review["commit_id"],
        "event": review["event"],
        "body": review["body"],
        "comments": review["comments"],
    }


def main() -> int:
    import json
    import sys
    from pathlib import Path

    if len(sys.argv) >= 2 and sys.argv[1] == "--payload":
        # The payload is written from a review that was already built,
        # so the driver does not assemble the API body itself.
        args = sys.argv[2:]
        source = None
        out_path = None
        i = 0
        while i < len(args):
            if args[i] == "--review" and i + 1 < len(args):
                source = Path(args[i + 1])
                i += 2
            elif args[i] == "--out" and i + 1 < len(args):
                out_path = Path(args[i + 1])
                i += 2
            else:
                die(USAGE_PAYLOAD)
        if source is None or out_path is None:
            die(USAGE_PAYLOAD)
        try:
            review = json.loads(source.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            die(f"review_post: cannot read {source}: {exc}")
        out_path.write_text(json.dumps(payload(review)) + "\n")
        return 0

    if len(sys.argv) < 2:
        die(USAGE)
    findings_path = Path(sys.argv[1])
    args = sys.argv[2:]
    head = ""
    current_head = None
    out_path = None
    i = 0
    while i < len(args):
        if args[i] == "--head" and i + 1 < len(args):
            head = args[i + 1]
            i += 2
        elif args[i] == "--current-head" and i + 1 < len(args):
            current_head = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1])
            i += 2
        else:
            die(USAGE)
    if not head:
        die(f"review_post: --head is required\n{USAGE}")
    if not findings_path.is_file():
        die(f"review_post: {findings_path} is not a file")

    try:
        findings = json.loads(findings_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        die(f"review_post: cannot read {findings_path}: {exc}")
    if not isinstance(findings, list):
        die(f"review_post: {findings_path} is not a JSON array")

    review = build(findings, head, current_head)
    text = json.dumps(review, indent=2) + "\n"
    if out_path is not None:
        out_path.write_text(text)
    else:
        sys.stdout.write(text)
    if not review["post"]:
        sys.stderr.write(f"review_post: not posting: {review['reason']}\n")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
