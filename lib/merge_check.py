#!/usr/bin/env python3
"""Decide whether a pull request is ready to merge, from facts about it.

`decide` is pure: the decision is tested against recorded facts, and a
failed fetch is reported as a failure, not as a refusal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import review_threads

# A DCO sign-off trailer: `Signed-off-by: Name <email>`.
SIGNOFF_RE = re.compile(
    r"^Signed-off-by:\s*(?P<name>.+?)\s*<(?P<email>[^>]+)>\s*$",
    re.MULTILINE,
)

# The label that says a decision is outstanding. The loop puts it on a PR
# awaiting a person's ruling, which makes it unready to merge however
# green it is.
NEEDS_DECISION_LABEL = "needs-decision"


@dataclass
class Failure:
    """One check that did not pass, and why.

    `check` names the check, so a reader knows which one refused, and
    `detail` says what was observed.
    """

    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


@dataclass
class Verdict:
    """The answer, and the facts it was decided from."""

    failures: list[Failure] = field(default_factory=list)
    merge_command: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class MergeOutcome:
    """What merging the base tip into the head would do.

    `kind` is `clean`, `conflict` (`files` names the paths), or `error`
    (`detail` says why).

    Only a conflict asks for a rebase: GitHub merges by rebase and
    replays a clean branch itself.
    """

    kind: str
    files: list[str] = field(default_factory=list)
    detail: str = ""


def signoff_trailers(message: str) -> list[tuple[str, str]]:
    """The `Signed-off-by:` trailers in a commit message, as (name, email).

    A trailer is a line of its own. A commit that mentions the phrase in
    prose is no sign-off, and counting it would make an unsigned commit
    look signed.
    """
    return [(m.group("name"), m.group("email")) for m in SIGNOFF_RE.finditer(message)]


def check_base_current(base_tip: str, merge_base: str,
                       head_parent: str | None, commit_count: int,
                       merge: MergeOutcome | None) -> list[Failure]:
    """The head can be replayed onto the base.

    `merge` is the outcome of the merge trial, or None when the branch
    sits on the base already.

    For a one-commit PR the parent is the merge base.
    """
    failures = []
    if merge_base != base_tip:
        if merge is None:
            failures.append(Failure(
                "base",
                f"the head's merge base is {merge_base[:7]}, but the base is at "
                f"{base_tip[:7]}; the branch is behind the base and could not "
                f"be checked against it"))
            return failures
        if merge.kind == "conflict":
            files = ", ".join(merge.files) if merge.files else "unknown files"
            failures.append(Failure(
                "base",
                f"the branch conflicts with the base in {files}; rebase"))
            return failures
        if merge.kind != "clean":
            detail = merge.detail or "the merge could not be run"
            failures.append(Failure(
                "base",
                f"the branch is behind the base and could not be checked "
                f"against it: {detail}"))
            return failures
    if commit_count == 1:
        if head_parent is None:
            failures.append(Failure("base", "the pull request has one commit with no parent"))
        elif head_parent != merge_base:
            failures.append(Failure(
                "base",
                f"the pull request's single commit sits on {head_parent[:7]}, "
                f"not on the base at {merge_base[:7]}"))
    return failures


def check_same_files(head_files: list[str],
                     reviewed_files: list[str], reviewed_sha: str) -> list[Failure]:
    """The change is the change that was reviewed.

    The file set is compared, not the content: a finishing run may rewrite
    prose. A file that appeared or vanished is a change the reviewer did
    not read.
    """
    failures = []
    head_set = set(head_files)
    reviewed_set = set(reviewed_files)
    added = sorted(head_set - reviewed_set)
    removed = sorted(reviewed_set - head_set)
    if added or removed:
        detail = []
        if added:
            detail.append(f"added since {reviewed_sha[:7]}: {', '.join(added)}")
        if removed:
            detail.append(f"removed since {reviewed_sha[:7]}: {', '.join(removed)}")
        failures.append(Failure("files", "; ".join(detail)))
    return failures


def check_signoff(commits: list[dict], expected_name: str,
                  expected_email: str, require_signoff: bool) -> list[Failure]:
    """Every commit is the repository's identity, and carries one sign-off.

    The author and the committer are both checked. A commit the lane
    wrote but something else committed is no lane commit.

    Exactly one sign-off: two say two people signed one.
    """
    failures = []
    want_name = expected_name.strip().lower()
    want_email = expected_email.strip().lower()
    for commit in commits:
        sha = commit.get("sha", "")
        short = sha[:7]
        author_name = (commit.get("author_name") or "").strip()
        author_email = (commit.get("author_email") or "").strip()
        if author_name.lower() != want_name or author_email.lower() != want_email:
            failures.append(Failure(
                "identity",
                f"{short} is authored by {author_name} <{author_email}>, not by "
                f"{expected_name} <{expected_email}>"))
        # The committer is checked when the record carries one: a
        # caller that leaves it out is no asking about it.
        committer_name = commit.get("committer_name")
        committer_email = commit.get("committer_email")
        if isinstance(committer_name, str) and isinstance(committer_email, str):
            if committer_name.strip().lower() != want_name or \
                    committer_email.strip().lower() != want_email:
                failures.append(Failure(
                    "identity",
                    f"{short} is committed by {committer_name} "
                    f"<{committer_email}>, not by {expected_name} "
                    f"<{expected_email}>"))
        if not require_signoff:
            continue
        trailers = signoff_trailers(commit.get("message") or "")
        if not trailers:
            failures.append(Failure(
                "signoff", f"{short} carries no Signed-off-by: trailer"))
            continue
        if len(trailers) > 1:
            failures.append(Failure(
                "signoff",
                f"{short} carries {len(trailers)} Signed-off-by: trailers; "
                f"one commit is signed by one person"))
            continue
        name, email = trailers[0]
        if name.strip().lower() != author_name.lower() or \
                email.strip().lower() != author_email.lower():
            failures.append(Failure(
                "signoff",
                f"{short} is signed by {name} <{email}>, but authored by "
                f"{author_name} <{author_email}>"))
    return failures


def check_checks_finished(pending: bool, pending_names: list[str]) -> list[Failure]:
    """CI has concluded.

    A verdict on a head whose CI has not finished gets reversed when the
    check fails, so the merge waits for the checks.
    """
    if not pending:
        return []
    names = ", ".join(pending_names) if pending_names else "some"
    return [Failure("checks", f"still running: {names}")]


def check_checks_passed(failing_names: list[str]) -> list[Failure]:
    """No check concluded against the branch.

    A check that concluded `failure` is evidence against the branch,
    whatever else is green. The names are the ones the API printed, so
    the report says which check refused.
    """
    if not failing_names:
        return []
    names = ", ".join(failing_names)
    return [Failure("checks", f"failed: {names}")]


def check_labels(labels: list[str]) -> list[Failure]:
    """No label says a decision is outstanding."""
    failures = []
    for label in labels:
        if label.lower() == NEEDS_DECISION_LABEL:
            failures.append(Failure(
                "label", f"the pull request carries the {NEEDS_DECISION_LABEL} label"))
    return failures


def check_no_third_party(posts: list[dict]) -> list[Failure]:
    """Nobody outside the loop has an unanswered comment.

    A pull request that merged over someone's comment answered nobody.

    Each post is a record with `login` and `created_at`. The time is
    reported so a reader can find the post.
    """
    if not posts:
        return []
    names = ", ".join(
        f"{p.get('login', '?')} ({p.get('created_at') or 'unknown time'})"
        for p in posts)
    return [Failure("comments", f"commented by {names}")]


def check_threads_answered(threads: list) -> list[Failure]:
    """Every review thread is resolved or has a reply after its first comment.

    No login is exempt: a thread's author never decides whether its
    finding was addressed. An outdated thread waits, named at its
    `originalLine` once `line` goes null.
    """
    failures = []
    for thread in threads:
        if not review_threads.is_unanswered(thread):
            continue
        failures.append(Failure(
            "threads",
            f"{review_threads.location(thread)} is unresolved and nobody has "
            f"answered it; the thread was started by "
            f"{review_threads.author(thread)}"))
    return failures


def decide(*, base_tip: str, head: str, merge_base: str, head_parent: str | None,
           commit_count: int, merge: MergeOutcome | None,
           head_files: list[str], reviewed_files: list[str],
           reviewed_sha: str, commits: list[dict], expected_name: str,
           expected_email: str, require_signoff: bool, checks_pending: bool,
           pending_names: list[str], failing_names: list[str],
           third_party: list[dict], threads: list, labels: list[str]) -> Verdict:
    """Run every check, and report the ones that did not pass.

    Every check runs even after one fails, so a reader sees everything
    wrong, not only the first thing.
    """
    failures = []
    failures += check_base_current(base_tip, merge_base, head_parent,
                                   commit_count, merge)
    failures += check_same_files(head_files, reviewed_files, reviewed_sha)
    failures += check_signoff(commits, expected_name, expected_email,
                              require_signoff)
    failures += check_checks_finished(checks_pending, pending_names)
    failures += check_checks_passed(failing_names)
    failures += check_no_third_party(third_party)
    failures += check_threads_answered(threads)
    failures += check_labels(labels)
    if failures:
        return Verdict(failures=failures)
    return Verdict(merge_command=(
        f"gh pr merge --rebase --match-head-commit {head}"))
