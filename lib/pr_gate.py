#!/usr/bin/env python3
"""Decide what a pull request needs, from GitHub data alone.

`decide` is pure, so the decision is tested against recorded API
responses without a network, and a failed fetch is reported as a
failure.
"""
from __future__ import annotations

import re

# The answer marker (#129's format). A post carrying it is the loop's own
# reply, never a third party's comment. It names the review it answers and
# the head it was written against.
MARKER_RE = re.compile(
    r"<!--\s*pr-loop:answer\s+review=(?P<review>\d+)\s+head=(?P<head>[0-9a-fA-F]{7,40})\s*-->"
)

# The round-status marker. The summary of every round carries it, so the
# loop's record of a failed round is the loop's own post too. It proves
# authorship only: it answers no review.
STATUS_MARKER_RE = re.compile(
    r"<!--\s*pr-loop:status\s+review=(?P<review>\d+)\s+head=(?P<head>[0-9a-fA-F]{7,40})\s*-->"
)

# A review by the reviewer that is a verdict. `COMMENTED` and `DISMISSED`
# are absent: a thread reply is a `COMMENTED` review, and counting one as a
# verdict is the mistake this module exists to avoid.
VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED")

# A review the authenticated user has started and left unsubmitted.
# GitHub returns it from `GET pulls/N/reviews` with no `submitted_at`, and
# it is visible to nobody else, so it is dropped before the field checks.
PENDING_REVIEW_STATE = "PENDING"

# A `StatusContext` that has not finished. A `CheckRun` is told apart by
# its `status`, where `COMPLETED` is the only finished value.
PENDING_STATUS_CONTEXTS = ("PENDING", "EXPECTED")

# The conclusions that are evidence *for* a branch. A conclusion outside
# the API's vocabulary is no pass: treating "concluded" as "green" is what
# let a failed check through the merge gate.
PASSING_CHECK_CONCLUSIONS = ("SUCCESS", "NEUTRAL", "SKIPPED")

# The states that are evidence for a commit status. The statuses API
# answers the same vocabulary as the check runs, in upper case.
PASSING_STATUS_STATES = ("SUCCESS", "NEUTRAL", "SKIPPED")

# A `CheckRun` conclusion that is evidence against the branch. A
# superseded run is cancelled, a check with nothing to say is skipped,
# and a runner that never came up is the runner's account.
FAILING_CHECK_CONCLUSIONS = ("FAILURE", "TIMED_OUT")

# A `StatusContext` state that is evidence against the branch.
FAILING_STATUS_STATES = ("FAILURE", "ERROR")

# The `user.type` of a comment that counts as a person. A comment from
# a `Bot` is never a third-party post, and a comment with no `type` is
# read as a person.
USER_TYPE = "User"
BOT_TYPE = "Bot"


class Malformed(Exception):
    """A fetched entry carries a shape outside the API's.

    Raised, and the run stops: a review with no `user`, `state`,
    `commit_id` or `submitted_at` is a verdict we cannot read.
    """

    def __init__(self, endpoint: str, index: int) -> None:
        super().__init__(f"malformed {endpoint} {index}")
        self.endpoint = endpoint
        self.index = index


def _require(entry: dict, key: str, endpoint: str, index: int):
    """Return entry[key], or raise Malformed if it is absent or the wrong type."""
    if key not in entry:
        raise Malformed(endpoint, index)
    value = entry[key]
    # `commit_id` may legitimately be null, but the field has to be
    # present; a missing field means we are reading something else.
    if value is None and key != "commit_id":
        raise Malformed(endpoint, index)
    return value


def _login(entry: dict, endpoint: str, index: int) -> str:
    """Return the author login of a review or comment."""
    user = _require(entry, "user", endpoint, index)
    if not isinstance(user, dict):
        raise Malformed(endpoint, index)
    login = user.get("login")
    if not isinstance(login, str) or not login:
        raise Malformed(endpoint, index)
    return login


def _text(entry: dict, endpoint: str, index: int) -> str:
    """Return a post's body, which may legitimately be the empty string."""
    body = entry.get("body")
    if body is None:
        return ""
    if not isinstance(body, str):
        raise Malformed(endpoint, index)
    return body


def validate_reviews(reviews: list) -> list:
    """Return the reviews as checked records.

    Every review is checked, the reviewer's included: a malformed entry
    anywhere in the list fails the run, because that entry may carry the
    ruling.

    A `PENDING` review is dropped first: its `submitted_at` is null.
    """
    out = []
    for index, review in enumerate(reviews):
        if not isinstance(review, dict):
            raise Malformed("reviews", index)
        state = review.get("state")
        if state == PENDING_REVIEW_STATE:
            continue
        login = _login(review, "reviews", index)
        state = _require(review, "state", "reviews", index)
        if not isinstance(state, str):
            raise Malformed("reviews", index)
        commit_id = _require(review, "commit_id", "reviews", index)
        if not isinstance(commit_id, str):
            raise Malformed("reviews", index)
        submitted_at = _require(review, "submitted_at", "reviews", index)
        if not isinstance(submitted_at, str):
            raise Malformed("reviews", index)
        out.append({
            "id": review.get("id"),
            "user": login,
            "state": state,
            "commit_id": commit_id,
            "submitted_at": submitted_at,
        })
    return out


def _verdicts(checked_reviews: list, reviewer: str) -> list:
    """Return the reviewer's verdict reviews, oldest first.

    A *verdict review* is the reviewer's `APPROVED` or `CHANGES_REQUESTED`.
    GitHub records a thread reply as an empty-body `COMMENTED` review, so
    the latest review and the latest verdict differ.
    """
    out = [r for r in checked_reviews
           if r["user"].casefold() == reviewer.casefold()
           and r["state"] in VERDICT_STATES]
    # `submitted_at` is an ISO-8601 UTC timestamp, so it sorts as a string.
    # The id breaks a tie: reviews submitted in the same second are ordered
    # by id, which GitHub assigns in increasing order.
    out.sort(key=lambda r: (r["submitted_at"],
                             r["id"] if isinstance(r["id"], int) else 0))
    return out


def verdict_reviews(reviews: list, reviewer: str) -> list:
    """Return the reviewer's verdict reviews, oldest first."""
    return _verdicts(validate_reviews(reviews), reviewer)


def changes_requested_count(checked_reviews: list, reviewer: str) -> int:
    """Count the reviewer's `CHANGES_REQUESTED` verdicts.

    `checked_reviews` is a list of checked review records, as
    `validate_reviews` returns. Counts every such verdict, not only the
    latest, so a run of rejections is what pushes a PR to `ESCALATE`.
    """
    return sum(1 for r in checked_reviews
               if r["user"].casefold() == reviewer.casefold()
               and r["state"] == "CHANGES_REQUESTED")


def _check_key(entry: dict) -> tuple | None:
    """The check an entry is one run of; None for an entry of neither shape.

    A `CheckRun` is keyed by (`workflowName`, `name`), since two workflows
    can each carry a job called `test`; a `StatusContext` by its `context`.
    """
    typename = entry.get("__typename")
    if typename == "CheckRun":
        workflow = entry.get("workflowName")
        name = entry.get("name")
        return ("CheckRun",
                workflow if isinstance(workflow, str) else "",
                name if isinstance(name, str) else "")
    if typename == "StatusContext":
        context = entry.get("context")
        return ("StatusContext", context if isinstance(context, str) else "")
    return None


def _check_started(entry: dict) -> str | None:
    """When the run started, or None while it has not started.

    `startedAt` is ISO-8601 UTC, so it sorts as text. A run nobody has
    started carries no value, and it was queued after every run that has.
    """
    started = entry.get("startedAt")
    return started if isinstance(started, str) and started else None


def _supersedes(newer: dict, older: dict) -> bool:
    """True when `newer` is the later run of the same check.

    Two runs with no start time say nothing of their order, so neither
    supersedes the other: dropping one is a guess.
    """
    newer_started = _check_started(newer)
    older_started = _check_started(older)
    if newer_started is None:
        return older_started is not None
    if older_started is None:
        return False
    return newer_started > older_started


def latest_check_runs(checks: list) -> list:
    """Return the rollup holding only each check's latest run, in input order.

    A `concurrency: cancel-in-progress` push leaves a superseded run
    beside the new one, and it is no evidence about the branch. An entry
    of neither shape passes through.
    """
    runs: dict[tuple, list] = {}
    for entry in checks:
        if isinstance(entry, dict):
            key = _check_key(entry)
            if key is not None:
                runs.setdefault(key, []).append(entry)

    out = []
    for entry in checks:
        key = _check_key(entry) if isinstance(entry, dict) else None
        if key is None or not any(_supersedes(o, entry) for o in runs[key]):
            out.append(entry)
    return out


def pending_checks(checks: list) -> bool:
    """True when a check's latest run on the head has not finished.

    `statusCheckRollup` mixes two shapes, told apart by `__typename`: a
    `CheckRun` carries a `status`, a `StatusContext` a `state`
    (`PENDING` or `EXPECTED` while it runs).

    An empty rollup counts as finished.
    """
    for entry in latest_check_runs(checks):
        if not isinstance(entry, dict):
            continue
        typename = entry.get("__typename")
        if typename == "CheckRun":
            status = entry.get("status")
            if isinstance(status, str) and status != "COMPLETED":
                return True
        elif typename == "StatusContext":
            state = entry.get("state")
            if isinstance(state, str) and state in PENDING_STATUS_CONTEXTS:
                return True
    return False


def failed_check_names(checks: list) -> list[str]:
    """The names of the checks that concluded against the branch.

    A check is a `CheckRun` (a `conclusion`) or a `StatusContext` (a
    `state`), told apart by `__typename`. A running check is never named,
    and only each check's latest run is read at all.

    Only `success`, `neutral` and `skipped` count as evidence.

    The merge gate asks for evidence for the branch, so a cancelled or
    startup-failed run is named.
    """
    failed: list[str] = []
    for entry in latest_check_runs(checks):
        if not isinstance(entry, dict):
            continue
        typename = entry.get("__typename")
        if typename == "CheckRun":
            status = entry.get("status")
            if isinstance(status, str) and status != "COMPLETED":
                # Still running. Not a failure.
                continue
            conclusion = entry.get("conclusion")
            if isinstance(conclusion, str) and \
                    conclusion.upper() in PASSING_CHECK_CONCLUSIONS:
                continue
            name = entry.get("name")
            failed.append(str(name) if isinstance(name, str) and name else "?")
        elif typename == "StatusContext":
            state = entry.get("state")
            if isinstance(state, str) and state.upper() in PENDING_STATUS_CONTEXTS:
                # Still running. Not a failure.
                continue
            if isinstance(state, str) and state.upper() in PASSING_STATUS_STATES:
                continue
            context = entry.get("context")
            failed.append(str(context) if isinstance(context, str) and context else "?")
    return failed


def failed_checks(checks: list) -> bool:
    """True when a check on the head concluded against the branch.

    A `CheckRun` is failed by its `conclusion`, a `StatusContext` by
    its `state`, and only a check's latest run speaks for it.

    The loop asks for evidence against the branch, and a cancelled run
    gives none: every push cancels the run it supersedes.
    """
    for entry in latest_check_runs(checks):
        if not isinstance(entry, dict):
            continue
        typename = entry.get("__typename")
        if typename == "CheckRun":
            conclusion = entry.get("conclusion")
            if (isinstance(conclusion, str)
                    and conclusion in FAILING_CHECK_CONCLUSIONS):
                return True
        elif typename == "StatusContext":
            state = entry.get("state")
            if isinstance(state, str) and state in FAILING_STATUS_STATES:
                return True
    return False


def _user_type(entry: dict, endpoint: str, index: int) -> str:
    """Return the author's `user.type`, reading a missing one as a person.

    The field is absent from some responses; a post with no `type` is a
    person's.
    """
    user = _require(entry, "user", endpoint, index)
    if not isinstance(user, dict):
        raise Malformed(endpoint, index)
    kind = user.get("type")
    if kind is None:
        return USER_TYPE
    if not isinstance(kind, str):
        raise Malformed(endpoint, index)
    return kind


def validate_comments(comments: list) -> list:
    """Return the comments as a list of checked records.

    A comment that fails to be a dict, or whose `user` or `created_at` is
    missing or the wrong type, is malformed. The body may be absent or
    empty.
    """
    out = []
    for index, entry in enumerate(comments):
        if not isinstance(entry, dict):
            raise Malformed("comments", index)
        login = _login(entry, "comments", index)
        kind = _user_type(entry, "comments", index)
        created = entry.get("created_at")
        if not isinstance(created, str) or not created:
            raise Malformed("comments", index)
        out.append({
            "id": entry.get("id"),
            "user": login,
            "type": kind,
            "created_at": created,
            "body": _text(entry, "comments", index),
        })
    return out


def _is_our_post(entry: dict, reviewer: str, implementers) -> bool:
    """True when the post is the loop's own: a marker whose author is a
    case-folded member of `implementers` and not the reviewer.

    Either marker proves authorship; only an answer marker answers.
    """
    if not (MARKER_RE.search(entry["body"])
            or STATUS_MARKER_RE.search(entry["body"])):
        return False
    author = entry["user"].casefold()
    reviewer_name = reviewer.casefold()
    if author == reviewer_name:
        return False
    return any(author == name.casefold() for name in implementers)


def third_party_post(comments: list, reviewer: str, after: str | None,
                     implementers=frozenset()) -> bool:
    """True when someone other than the reviewer spoke after the verdict.

    A *third-party post* is a comment created after the verdict that
    speaks for itself. A marker counts only from a listed implementer,
    and a bot's post never does.
    """
    for entry in comments:
        if after is not None and entry["created_at"] <= after:
            continue
        if entry.get("type") == BOT_TYPE:
            continue
        marked = bool(MARKER_RE.search(entry["body"])
                      or STATUS_MARKER_RE.search(entry["body"]))
        # The reviewer speaking plainly is no third party; the reviewer
        # wearing the loop's marker is.
        if entry["user"].casefold() == reviewer.casefold() and not marked:
            continue
        if _is_our_post(entry, reviewer, implementers):
            continue
        return True
    return False


def answered_review(comments: list, review_id, reviewer: str,
                    implementers=frozenset()) -> bool:
    """True when a listed implementer's answer marker names this review id.

    `comments` is a list of checked comment records, as `validate_comments`
    returns. The marker's `review=` must name the *latest* verdict. A
    status marker proves authorship and answers nothing.
    """
    for entry in comments:
        if not _is_our_post(entry, reviewer, implementers):
            continue
        for match in MARKER_RE.finditer(entry["body"]):
            if int(match.group("review")) == review_id:
                return True
    return False


# The API lists at most this many files in a compare. A response that
# long is truncated, so it cannot be compared.
MAX_COMPARE_FILES = 300


def _files_by_name(files) -> dict | None:
    """The compare response's files keyed by filename.

    None when the response cannot be compared: not a list, truncated at
    MAX_COMPARE_FILES, an entry that is not an object, or a file listed
    twice, with no name, or with no `sha`.
    """
    if not isinstance(files, list) or len(files) >= MAX_COMPARE_FILES:
        return None
    out: dict[str, dict] = {}
    for entry in files:
        if not isinstance(entry, dict):
            return None
        filename = entry.get("filename")
        sha = entry.get("sha")
        if not isinstance(filename, str) or not filename:
            return None
        if not isinstance(sha, str) or not sha:
            return None
        if filename in out:
            return None
        out[filename] = entry
    return out


def same_patch(left_files, right_files) -> bool:
    """True when two compare responses carry the same work.

    Equal when both list the same files and every file's blob sha is
    equal. The blob sha names the file's content, so a file the API
    will not show a patch for -- binary, or too large to diff -- still
    compares.

    Anything else is not equal. A false equality merges unreviewed
    work; a false difference costs one review.
    """
    left = _files_by_name(left_files)
    right = _files_by_name(right_files)
    if left is None or right is None:
        return False
    if set(left) != set(right):
        return False

    for name, entry in left.items():
        if entry["sha"] != right[name]["sha"]:
            return False
    return True


def _same_work(trees: dict, head: str, review_commit: str,
               patch_equal) -> bool:
    """True when the head carries the same work as the reviewed commit.

    Equal trees are the same work. Failing that, an equal patch is: a
    rebase over a base that left the file alone replays the change
    byte for byte.
    """
    head_tree = trees.get(head)
    review_tree = trees.get(review_commit)
    if head_tree is not None and head_tree == review_tree:
        return True
    return patch_equal is True


def early_decision(pr: dict, reviews: list, comments: list, checks: list,
                   reviewer: str, implementers=frozenset()):
    """Return what the pull request needs, if the first rules settle it.

    The rules are in the order `decide` applies them. The first seven
    need nothing beyond the pull request, its reviews, comments and
    checks, so a caller can answer from them alone and skip the requests
    the later rules need.

    Returns `(line, verdict)`: `line` is the answer, or None when the
    later rules are needed. `verdict` is the reviewer's latest verdict
    review, or None; returned so the caller can look up the trees the
    later rules compare without re-deriving which review is the verdict.
    """
    head = _pr_head(pr)
    state = pr.get("state")
    merged = pr.get("merged")

    checked_reviews = validate_reviews(reviews)
    checked_comments = validate_comments(comments)

    # Rule 1: merged -> DONE merged. Before `closed` because a merged
    # pull request is closed too, and "merged" is the more useful answer.
    if merged is True:
        return "DONE merged", None
    # Rule 2: closed without a merge -> DONE closed.
    if state == "closed":
        return "DONE closed", None
    # Rule 3: a draft waits. The reviewer reads only ready pull requests.
    if pr.get("draft") is True:
        return "WAIT draft", None
    # Rule 4: checks still running -> WAIT checks. A verdict on a head
    # whose CI has not finished gets reversed when the CI finishes. An
    # empty rollup counts as finished: a repository with no CI would
    # otherwise wait forever.
    if pending_checks(checks):
        return "WAIT checks", None
    # Rule 5: a check concluded against the branch -> FIX checks. The
    # implementer reads the failing job's log.
    if failed_checks(checks):
        return "FIX checks", None
    # Rule 6: the base conflicts with the head -> FIX conflict. A null
    # `mergeable` is unknown, not a conflict: GitHub returns it for
    # seconds after every push.
    if pr.get("mergeable") is False:
        return "FIX conflict", None

    verdicts = _verdicts(checked_reviews, reviewer)
    # Rule 7: no verdict review exists -> REVIEW <head>.
    if not verdicts:
        return f"REVIEW {head}", None

    return None, verdicts[-1]


def decide(pr: dict, reviews: list, comments: list, checks: list, trees: dict,
           reviewer: str, max_rounds: int = 4,
           implementers=frozenset(), patch_equal=None) -> str:
    """Return the one line that says what the pull request needs next.

    `pr` is the parsed `repos/R/pulls/N` object; `reviews`, `comments`
    and `checks` are the fetched lists; `implementers` are the logins
    whose markers are trusted. `patch_equal` says whether the head and
    the reviewed commit carry the same patch; None means no comparison
    was made.

    The rules that need nothing but the pull request and its reviews
    come from `early_decision`.
    """
    head = _pr_head(pr)

    line, verdict = early_decision(pr, reviews, comments, checks,
                                   reviewer, implementers)
    if line is not None:
        return line
    # `early_decision` returns a verdict whenever it returns no line: the
    # only way past rule 7 is to have a verdict to rule on.
    assert verdict is not None

    checked_reviews = validate_reviews(reviews)
    checked_comments = validate_comments(comments)

    review_commit = verdict["commit_id"]

    # Someone other than the reviewer spoke after the verdict. Their words
    # need a ruling before the verdict can be acted on. Asked once, here,
    # so the one question has one answer.
    interrupted = third_party_post(checked_comments, reviewer,
                                    verdict["submitted_at"], implementers)

    # Rule 8: the verdict is stale, nobody has objected since, and the
    # work is the same -> the verdict carries, with no model run. A
    # squash leaves the tree equal; a rebase leaves the patch equal.
    stale = review_commit != head
    if stale and not interrupted and _same_work(trees, head, review_commit,
                                                patch_equal):
        if verdict["state"] == "APPROVED":
            return f"REAPPROVE {head}"
        # A request for changes still stands. Carry on with the rules
        # below as though the verdict were on the head.
    elif stale:
        # Rule 9: the verdict is on an older commit -> REVIEW <head>
        # <V>. It says nothing about the head.
        return f"REVIEW {head} {review_commit}"

    # Rule 10: a third party spoke after the verdict -> REVIEW <head> <V>.
    if interrupted:
        return f"REVIEW {head} {review_commit}"

    # Rule 11: the verdict approved -> AGREED <head>.
    if verdict["state"] == "APPROVED":
        return f"AGREED {head}"

    # Rule 12: the verdict was answered without a commit -> REVIEW <head>
    # <V>. The rejections need a ruling.
    if answered_review(checked_comments, verdict["id"], reviewer, implementers):
        return f"REVIEW {head} {review_commit}"

    # Rule 13: too many rounds -> ESCALATE max-rounds.
    if changes_requested_count(checked_reviews, reviewer) >= max_rounds:
        return "ESCALATE max-rounds"

    # Rule 14: the verdict asked for changes -> FIX <review-id>.
    return f"FIX {verdict['id']}"


def _pr_head(pr: dict) -> str:
    """Return the head sha of the pull request."""
    head = pr.get("head")
    if not isinstance(head, dict):
        raise Malformed("pulls", 0)
    sha = head.get("sha")
    if not isinstance(sha, str) or not sha:
        raise Malformed("pulls", 0)
    return sha
