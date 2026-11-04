#!/usr/bin/env python3
"""Fetch what a reviewer has to judge the next round against.

A review is not a verdict in a vacuum. The next round has to see the
previous findings, the implementer's answers to them -- with the reason
each rejection was given -- and anything a person has said since. A
reviewer that sees none of it re-raises findings that were already
answered, and the loop never converges.

So this fetches three things from a pull request:

- the findings of the reviewer's latest review, read back from the
  inline comments it posted;
- the implementer's answers to them, read back from the replies, which
  carry the answer marker (#129) that says which review they answer;
- the comments a person made since that review.

The findings are read back from the review rather than kept in a file,
because the review on the pull request is the record. A file on one
host is not: the coordinator may sit on another one.

Everything is read with the reviewer's own credentials, because the
reviewer's own review is what is being read.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, NoReturn

USAGE = ("usage: review_context.py --repo OWNER/NAME --pr N "
         "[--reviewer LOGIN]")

# The answer marker (#129). A reply carrying it is the implementer's
# answer to a review, and not a person's comment.
ANSWER_MARKER = "pr-loop:answer"


class ContextError(Exception):
    """The pull request could not be read."""


def die(message: str) -> "NoReturn":
    sys.stderr.write(message + "\n")
    sys.exit(2)


def gh_json(endpoint: str, repo: str, token: str | None = None) -> Any:
    """One `gh api` call, parsed. GitHub's errors are the caller's.

    The token is the reader's own, so the call runs as the role that
    reads the pull request. A read that fell back to the stored
    credentials would read whatever account happens to be logged in, and
    a reviewer's own review is not visible to an account that is not the
    reviewer.
    """
    env = None
    if token:
        env = dict(os.environ)
        env["GH_TOKEN"] = token
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        raise ContextError(f"gh api {endpoint} failed: "
                           f"{result.stderr.strip() or 'no output'}")
    if not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContextError(f"gh api {endpoint} did not return JSON: {exc}")


def gh_json_pages(endpoint: str, repo: str, token: str | None = None
                   ) -> list:
    """Every page of a paginated endpoint, in order."""
    out: list = []
    page = 1
    while True:
        sep = "&" if "?" in endpoint else "?"
        chunk = gh_json(f"{endpoint}{sep}per_page=100&page={page}", repo,
                        token)
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return out


def login_of(entry: dict) -> str:
    user = entry.get("user")
    if isinstance(user, dict):
        return str(user.get("login") or "")
    return ""


def latest_review(reviews: list, reviewer: str) -> dict | None:
    """The reviewer's latest review on the pull request.

    The reviewer's own reviews only: the implementer's replies are
    recorded by GitHub as reviews with an empty body, and one of those
    is not a review of anything.
    """
    candidates = []
    for entry in reviews:
        if not isinstance(entry, dict):
            continue
        if reviewer and login_of(entry) != reviewer:
            continue
        body = entry.get("body")
        if not isinstance(body, str) or not body.strip():
            # A reply, recorded as an empty-body review.
            continue
        if entry.get("state") not in ("APPROVED", "CHANGES_REQUESTED",
                                      "COMMENTED"):
            continue
        submitted = entry.get("submitted_at") or entry.get("created_at") or ""
        candidates.append((submitted, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def findings_from_review(review: dict | None, repo: str, pr: int,
                         token: str | None = None) -> list[dict]:
    """The findings of a review, read back from its inline comments.

    A finding is a comment on a line. The comment's body is the claim,
    and the recommendation is the line under it, because the body the
    review posted carries both.
    """
    if review is None:
        return []
    review_id = review.get("id")
    if review_id is None:
        return []
    comments = gh_json_pages(
        f"repos/{repo}/pulls/{pr}/reviews/{review_id}/comments", repo, token)
    findings = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "")
        claim, recommendation = _split_body(body)
        severity = _severity_of(body)
        findings.append({
            "severity": severity,
            "path": comment.get("path") or "",
            "line": comment.get("line") or comment.get("original_line") or 0,
            "claim": claim,
            "recommendation": recommendation,
            "comment_id": comment.get("id"),
        })
    return findings


def _split_body(body: str) -> tuple[str, str]:
    """The claim and the recommendation, out of a posted comment.

    The posted body is `**severity**`, the claim, and a recommendation
    line. The claim is what is left once the severity and the
    recommendation are taken off.
    """
    claim_lines = []
    recommendation = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("Recommendation:"):
            recommendation = stripped[len("Recommendation:"):].strip()
            continue
        claim_lines.append(stripped)
    claim = " ".join(part for part in claim_lines
                     if part and not part.startswith("**")).strip()
    return (claim, recommendation)


def _severity_of(body: str) -> str:
    """The severity a posted comment carries.

    The body leads with `**high**`. A body with no severity is low: an
    unlabelled finding is reported, not blocking.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("**") and stripped.endswith("**"):
            word = stripped.strip("*").strip().lower()
            if word in ("high", "medium", "low"):
                return word
    return "low"


def answers_from_replies(repo: str, pr: int, findings: list[dict],
                         implementer: str = "",
                         token: str | None = None) -> list[dict]:
    """The implementer's answers to the findings, with their reasons.

    An answer is a reply to one of the findings' comments that carries
    the answer marker. The reason is the part that matters: a reviewer
    that sees only the rejection cannot tell a fix that was argued down
    on the merits from one that was missed.
    """
    if not findings:
        return []
    by_id = {f["comment_id"]: f for f in findings
             if f.get("comment_id") is not None}
    if not by_id:
        return []
    comments = gh_json_pages(f"repos/{repo}/pulls/{pr}/comments", repo,
                             token)
    answers = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "")
        if ANSWER_MARKER not in body:
            continue
        author = login_of(comment)
        if implementer and author and author != implementer:
            continue
        in_reply_to = comment.get("in_reply_to_id")
        finding = by_id.get(in_reply_to)
        if finding is None:
            continue
        verdict, reason = _verdict_and_reason(body)
        answers.append({
            "path": finding["path"],
            "line": finding["line"],
            "verdict": verdict,
            "reason": reason,
            "author": author,
        })
    return answers


def _verdict_and_reason(body: str) -> tuple[str, str]:
    """The verdict of an answer and the reason given for it.

    An answer says what happened to the finding: it was fixed, or it was
    rejected. The reason follows it. Anything that is not a rejection
    reads as a fix, because the implementer pushed a commit.
    """
    verdict = "fixed"
    reason = ""
    for line in body.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("reason:"):
            reason = stripped[len("reason:"):].strip()
            continue
        if "reject" in low or "disagree" in low or "wont fix" in low \
                or "won't fix" in low:
            verdict = "rejected"
    if verdict == "rejected" and not reason:
        # The body is the reason when it names none.
        reason = " ".join(
            line.strip() for line in body.splitlines()
            if line.strip() and not line.strip().startswith("<!--"))
        reason = reason.strip()
    return (verdict, reason)


def comments_since(reviews: list, comments: list, reviewer: str,
                   implementer: str = "") -> list[dict]:
    """What a person has said since the reviewer's latest review.

    The reviewer's own posts are not a person's comments, and neither are
    the implementer's answers: both are the loop talking to itself.
    """
    latest = latest_review(reviews, reviewer)
    if latest is None:
        return []
    since = latest.get("submitted_at") or latest.get("created_at") or ""
    out = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = login_of(comment)
        if reviewer and author == reviewer:
            continue
        if implementer and author == implementer:
            continue
        created = comment.get("created_at") or ""
        if since and created and created < since:
            continue
        body = str(comment.get("body") or "").strip()
        if not body:
            continue
        out.append({"author": author or "someone", "body": body,
                    "created_at": created})
    return out


def collect(repo: str, pr: int, reviewer: str = "",
            implementer: str = "") -> dict:
    """Everything the reviewer has to judge the next round against.

    The pull request is read with the reviewer's own token: the
    reviewer's own review is not visible to an account that is not the
    reviewer, and the context this returns is the reviewer's own past.
    """
    token = (os.environ.get("REVIEWER_GH_TOKEN") or "").strip()
    reviews = gh_json_pages(f"repos/{repo}/pulls/{pr}/reviews", repo, token)
    comments = gh_json_pages(f"repos/{repo}/pulls/{pr}/comments", repo, token)
    issue_comments = gh_json_pages(
        f"repos/{repo}/issues/{pr}/comments", repo, token)

    review = latest_review(reviews, reviewer)
    findings = findings_from_review(review, repo, pr, token)
    answers = answers_from_replies(repo, pr, findings, implementer, token)

    # A conversation comment is a person's comment too.
    conversation = []
    for comment in issue_comments:
        if not isinstance(comment, dict):
            continue
        author = login_of(comment)
        if reviewer and author == reviewer:
            continue
        if implementer and author == implementer:
            continue
        body = str(comment.get("body") or "").strip()
        if not body:
            continue
        conversation.append({"author": author or "someone", "body": body,
                             "created_at": comment.get("created_at") or ""})

    since = comments_since(reviews, comments, reviewer, implementer)
    return {
        "findings": findings,
        "answers": answers,
        "comments": since + conversation,
    }


def main() -> int:
    args = sys.argv[1:]
    repo = ""
    pr = 0
    reviewer = ""
    implementer = ""
    i = 0
    while i < len(args):
        if args[i] == "--repo" and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
        elif args[i] == "--pr" and i + 1 < len(args):
            try:
                pr = int(args[i + 1])
            except ValueError:
                die("--pr must be a number")
            i += 2
        elif args[i] == "--reviewer" and i + 1 < len(args):
            reviewer = args[i + 1]
            i += 2
        elif args[i] == "--implementer" and i + 1 < len(args):
            implementer = args[i + 1]
            i += 2
        else:
            die(USAGE)
    if not repo or not pr:
        die(USAGE)

    try:
        context = collect(repo, pr, reviewer, implementer)
    except ContextError as exc:
        die(f"review_context: {exc}")
    json.dump(context, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
