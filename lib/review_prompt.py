#!/usr/bin/env python3
"""Build the prompt a posted review is made from.

Everything the reviewer needs goes in the prompt. The MiniMax lane
refuses a procedure it has to read from a file and treats the file as
prompt injection, so the diff, the previous findings, the implementer's
answers and a person's comments are all inlined.

The prompt asks for the findings as JSON in `finding-check`'s schema,
because the findings are checked against the source before they are
posted: a quote that is not in the file is a finding about code nobody
could find, and posting it puts a claim on the pull request that a
person has to disprove rather than one they can read.

The prompt is a pure function of what it is given, so the suite can
assert over it without a model and without a network.
"""
from __future__ import annotations

import json
from typing import Any

# The fields a finding carries. The reviewer has to produce every one of
# them, because `finding-check` reads them and a finding missing one is
# malformed and dropped.
FINDING_FIELDS = ("severity", "path", "line", "snippet", "claim",
                   "settled_by", "recommendation")

# The severities a finding may carry.
SEVERITIES = ("high", "medium", "low")

# The instruction that asks for the findings in the schema. It names the
# fields and says what each is for, because a reviewer that guesses
# produces a finding the checker drops.
SCHEMA_INSTRUCTION = """\
Write your findings to {path} as a JSON array. Each entry is an object
with exactly these fields:

- "severity": one of "high", "medium", "low".
- "path": the path of the file, relative to the repository root.
- "line": the line the finding is about, as a number.
- "snippet": the code the finding is about, quoted exactly as it is in
  the file. Quote the lines that show the defect, not a summary of them.
- "claim": what is wrong, in one sentence.
- "settled_by": the evidence that settles the finding -- the test, the
  run, or the reading of the source that shows it.
- "recommendation": the concrete change that fixes it.

A finding whose snippet is not in the file is dropped before anyone
reads it, so quote the file exactly. If the change is correct, write an
empty array rather than inventing a finding.

When the file is written, reply with one line: the count of findings per
severity."""


def render_findings(findings: list) -> str:
    """The previous findings, as text the reviewer can read."""
    if not findings:
        return "There is no previous review of this pull request."
    parts = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        path = finding.get("path", "")
        line = finding.get("line", "")
        severity = finding.get("severity", "")
        claim = finding.get("claim", "")
        recommendation = finding.get("recommendation", "")
        parts.append(f"- {path}:{line} ({severity}): {claim}")
        if recommendation:
            parts.append(f"  Recommendation: {recommendation}")
    if not parts:
        return "There is no previous review of this pull request."
    return "\n".join(parts)


def render_answers(answers: list) -> str:
    """The implementer's answers, with the reason for each rejection.

    The reason is the part that matters: a reviewer that sees only the
    rejection cannot tell a fix that was argued down on the merits from
    one that was missed.
    """
    if not answers:
        return "The implementer has not answered a review yet."
    parts = []
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        path = answer.get("path", "")
        line = answer.get("line", "")
        verdict = answer.get("verdict", "")
        reason = answer.get("reason", "")
        parts.append(f"- {path}:{line} — {verdict}")
        if reason:
            parts.append(f"  Reason: {reason}")
    if not parts:
        return "The implementer has not answered a review yet."
    return "\n".join(parts)


def render_comments(comments: list) -> str:
    """What a person has said since the last review."""
    if not comments:
        return "Nobody has commented since the last review."
    parts = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author", "someone")
        body = str(comment.get("body", "")).strip()
        if body:
            parts.append(f"- {author}: {body}")
    if not parts:
        return "Nobody has commented since the last review."
    return "\n".join(parts)


def build(diff: str, findings_path: str, previous: list,
          answers: list, comments: list, priors: str = "",
          head: str = "", base: str = "", worktree: str = "") -> str:
    """The whole prompt.

    The diff comes first: it is the evidence. Then what the reviewer
    has to judge it against -- the previous findings, the implementer's
    answers, and what a person has said since.
    """
    parts: list[str] = []
    parts.append(
        "Review a proposed code change in the repository at {wt}.\n"
        "\n"
        "The commit under review is {head}. The change is {head} against "
        "{base}.\n"
        "\n"
        "The complete diff is below. Read it first, then read the "
        "surrounding source files in the repository for the context "
        "needed to judge each change.".format(
            wt=worktree or "the repository under review",
            head=head or "the commit named below",
            base=base or "its base"))
    parts.append("## The diff\n\n```diff\n" + diff.rstrip() + "\n```")
    parts.append("## The previous review\n\n" + render_findings(previous))
    parts.append("## The implementer's answers\n\n" + render_answers(answers))
    parts.append("## What a person has said since\n\n"
                 + render_comments(comments))
    if priors:
        parts.append("## What the reviewer already knows\n\n" + priors.strip())

    parts.append(
        "Judge the change, not the repository: a defect that predates it "
        "and that it does not make worse is out of scope. What the change "
        "leaves out is not -- a member of a set it half-covers, a consumer "
        "it never updated, and a rule its own doc comment states and its "
        "code contradicts are all defects of the change.")
    parts.append(
        "A finding you raise has to be about this commit. A finding that "
        "was already raised and is still open is not raised again; a "
        "finding the implementer answered with a reason is not raised "
        "again unless that reason is wrong, and then the reply says why.")
    parts.append(SCHEMA_INSTRUCTION.format(path=findings_path))
    return "\n\n".join(parts) + "\n"


def findings_schema_example() -> Any:
    """One finding in the schema, for the prompt's own sake."""
    return {
        "severity": "high",
        "path": "src/example.rs",
        "line": 42,
        "snippet": "let total = compute(items);",
        "claim": "the total is never checked, so a bad item passes",
        "settled_by": "the caller asserts on it",
        "recommendation": "assert on the total before returning it",
    }


USAGE = ("usage: review_prompt.py --diff FILE --out FILE --findings-out FILE "
         "[--previous FILE] [--priors TEXT] [--head SHA] [--base SHA] "
         "[--worktree PATH]")


def main() -> int:
    """Build the prompt from the files the driver names.

    The diff is read from a file rather than an argument, so a diff with
    quotes, backticks or a NUL-free but very large body survives intact
    instead of bumping into the shell's argument limit.
    """
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--diff", required=True,
                        help="the diff, as a file")
    parser.add_argument("--out", required=True,
                        help="where to write the prompt")
    parser.add_argument("--findings-out", required=True,
                        help="where the reviewer is told to write the "
                             "findings")
    parser.add_argument("--previous", default="",
                        help="the review context, as a JSON file")
    parser.add_argument("--priors", default="",
                        help="text folded into the prompt")
    parser.add_argument("--head", default="")
    parser.add_argument("--base", default="")
    parser.add_argument("--worktree", default="")
    args = parser.parse_args()

    diff = Path(args.diff).read_text(errors="replace")
    previous: dict = {}
    if args.previous:
        try:
            loaded = json.loads(Path(args.previous).read_text())
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"review_prompt: cannot read the context: "
                             f"{exc}\n")
            return 2

    prompt = build(
        diff,
        args.findings_out,
        previous.get("findings", []),
        previous.get("answers", []),
        previous.get("comments", []),
        priors=args.priors,
        head=args.head,
        base=args.base,
        worktree=args.worktree,
    )
    Path(args.out).write_text(prompt)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
