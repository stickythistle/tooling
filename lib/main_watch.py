#!/usr/bin/env python3
"""Decide whether a merge broke the branch it landed on.

The question: **was this commit red where the commit it landed on was
green?**

Five states, not two: `broke`, `already_red`, `flaky`, `unattributed`
and `pending`, plus `green`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# A check run that concluded against the branch. `cancelled` and
# `skipped` are absent: a cancelled run is the one a later push
# superseded, and a skipped run is a decision, not a failure.
FAILING_CHECK_CONCLUSIONS = frozenset({
    "failure", "timed_out", "action_required", "startup_failure",
    "stale",
})

# A commit status that failed.
FAILING_STATUS_STATES = frozenset({"failure", "error"})

# The three states a commit's checks can be in.
GREEN = "green"
RED = "red"
PENDING = "pending"

# A commit whose checks never completed and will never be found by
# walking. It is no red -- nothing says it failed -- and no green
# either, since nothing says it passed.

# It is its own verdict: the redness of the commit on top is attributed
# to nothing.
UNATTRIBUTED = "unattributed"

# The commit is red, but every red check also passed at some point. The
# failure was real and is kept as evidence, but the check ended green,
# so the commit did not leave the branch red.

# Reported apart from `broke`, so the caller opens no revert over a
# flake.
FLAKY = "flaky"


class Malformed(Exception):
    """A fetched entry carries a shape outside the API's."""

    def __init__(self, endpoint: str, index: int) -> None:
        super().__init__(f"malformed {endpoint} {index}")
        self.endpoint = endpoint
        self.index = index


@dataclass
class Check:
    """One check on a commit, by name.

    `state` is `green`, `red` or `pending`. `pending` covers a run that
    has not completed and a status that is still expected.

    `flaky` says the check failed and then passed.
    """

    name: str
    state: str
    # The URL of the run, for the report. A commit status has one too.
    url: str = ""
    # The check failed earlier and its most recent report passed.
    flaky: bool = False


@dataclass
class Verdict:
    """What a commit's checks say, against the commit it landed on."""

    # `broke` when this commit is red and the base was green.
    # `already_red` when this commit is red and the base was red.
    # `flaky` when every red check also passed, so the commit did not
    # leave the branch red.

    # `unattributed` when the base ran no checks that completed.
    # `pending` when this commit's checks, or the base's, have yet to
    # finish. `green` when this commit's checks are green.
    kind: str
    # The checks that are red on this commit.
    failing: list[Check] = field(default_factory=list)
    # The checks this commit has that the base never ran, and so cannot
    # be compared against. A commit with one of these cannot be said to
    # have broken the branch on that check alone.
    uncompared: list[str] = field(default_factory=list)
    # The base's own state, for the report.
    parent_state: str = ""

    @property
    def broke(self) -> bool:
        """True when this commit is the one that broke the branch."""
        return self.kind == "broke"


def _require(entry: dict, key: str, endpoint: str, index: int):
    """Return entry[key], or raise Malformed if it is absent or the wrong type."""
    if key not in entry:
        raise Malformed(endpoint, index)
    value = entry[key]
    if value is None:
        raise Malformed(endpoint, index)
    return value


def check_run_state(entry: dict, endpoint: str, index: int) -> str:
    """The state of one check run.

    A run has a `status` (did it end) and a `conclusion` (did it pass).
    A finished-and-failed run reports `completed` with a failing
    conclusion, so "has it finished" reads a red check as a pass.
    """
    status = _require(entry, "status", endpoint, index)
    if not isinstance(status, str):
        raise Malformed(endpoint, index)
    if status.lower() != "completed":
        return PENDING
    conclusion = entry.get("conclusion")
    if conclusion is None:
        # A finished run with no conclusion is unknown, and unknown is
        # no pass. It is no failure either: it is no evidence against
        # the branch.
        return PENDING
    if not isinstance(conclusion, str):
        raise Malformed(endpoint, index)
    if conclusion.lower() in FAILING_CHECK_CONCLUSIONS:
        return RED
    return GREEN


def status_context_state(entry: dict, endpoint: str, index: int) -> str:
    """The state of one commit status.

    A commit status is told apart from a check run by its fields: a
    status has a `state` and a `context`, a run has a `status` and a
    `conclusion`.
    """
    state = _require(entry, "state", endpoint, index)
    if not isinstance(state, str):
        raise Malformed(endpoint, index)
    lowered = state.lower()
    if lowered in FAILING_STATUS_STATES:
        return RED
    if lowered in ("pending", "expected"):
        return PENDING
    if lowered == "success":
        return GREEN
    # A state outside the API's vocabulary. It is no evidence against
    # the branch, so it is no red; it is no pass either.
    return PENDING


def collect_checks(check_runs: list, statuses: list) -> dict[str, Check]:
    """Every check on a commit, by name.

    Within one source the worst state wins: a later successful re-run
    adds to the evidence. Across sources a check run beats a commit
    status of the same name.
    """
    out: dict[str, Check] = {}
    # Which source a stored check came from: a run or a status.
    from_run: dict[str, bool] = {}
    # The state of the *most recent* report of each check. The check
    # runs come back newest first, so the first report of a name is the
    # latest one.
    latest: dict[str, str] = {}

    # How much worse a state is than another. Higher wins.
    severity = {GREEN: 0, PENDING: 1, RED: 2}

    def put(check: Check, is_run: bool) -> None:
        key = check.name.casefold()
        existing = out.get(key)
        if existing is None:
            out[key] = check
            from_run[key] = is_run
            latest[key] = check.state
            return
        if not is_run and from_run.get(key):
            # A status under a run of the same name. The run is the
            # stronger evidence; the status leaves it alone.
            return
        # A check whose latest report is green but which failed earlier
        # is flaky: it was re-run, and it passed. The failure is kept,
        # but the check ended green, so it is no evidence against the
        # branch.
        flaky = existing.flaky
        if latest.get(key) == GREEN and check.state == RED:
            flaky = True
        if severity[check.state] > severity[existing.state]:
            kept = check
            from_run[key] = is_run
        elif is_run and not from_run.get(key):
            # A run under a status of the same name, with the same
            # severity: the run wins, so its name and url are kept.
            kept = check
            from_run[key] = is_run
        else:
            kept = existing
        out[key] = replace(kept, flaky=flaky)

    for index, entry in enumerate(check_runs):
        if not isinstance(entry, dict):
            raise Malformed("check-runs", index)
        name = _require(entry, "name", "check-runs", index)
        if not isinstance(name, str) or not name:
            raise Malformed("check-runs", index)
        state = check_run_state(entry, "check-runs", index)
        url = entry.get("html_url")
        put(Check(name=name, state=state,
                  url=url if isinstance(url, str) else ""), True)

    for index, entry in enumerate(statuses):
        if not isinstance(entry, dict):
            raise Malformed("status", index)
        context = _require(entry, "context", "status", index)
        if not isinstance(context, str) or not context:
            raise Malformed("status", index)
        state = status_context_state(entry, "status", index)
        url = entry.get("target_url")
        put(Check(name=context, state=state,
                  url=url if isinstance(url, str) else ""), False)

    return out


def overall_state(checks: dict[str, Check]) -> str:
    """The state of a commit's checks as a whole.

    Red wins over pending, because a failed check is evidence against
    the branch even while another runs. Pending wins over green.
    """
    if any(c.state == RED for c in checks.values()):
        return RED
    if not checks:
        # A commit with no checks at all. It is no green: nothing
        # reported, so nothing was proved. It is no red either.
        return PENDING
    if any(c.state == PENDING for c in checks.values()):
        return PENDING
    return GREEN


def compare(commit_checks: dict[str, Check],
            parent_checks: dict[str, Check],
            parent_state: str | None = None) -> Verdict:
    """Compare a commit's checks against the base's.

    A check the base never ran is a gap, so a red there is reported but
    not attributed to this commit. `parent_state` overrides the base's
    own checks for a caller that knows more.
    """
    commit_state = overall_state(commit_checks)
    if parent_state is None:
        parent_state = overall_state(parent_checks)

    failing = [c for c in commit_checks.values() if c.state == RED]
    # The checks this commit has that the parent never ran. The name is
    # the one the API printed, not the case-folded key it is stored
    # under: the report says which check it was.
    parent_names = {name.casefold() for name in parent_checks}
    uncompared = sorted(
        check.name for name, check in commit_checks.items()
        if check.state == RED and name.casefold() not in parent_names
    )

    if commit_state == PENDING:
        return Verdict(kind=PENDING, failing=failing,
                       uncompared=uncompared, parent_state=parent_state)
    if commit_state == GREEN:
        return Verdict(kind=GREEN, failing=failing,
                       uncompared=uncompared, parent_state=parent_state)

    # The commit is red.
    if parent_state == RED:
        # The branch was already broken. This commit did not break it.
        return Verdict(kind="already_red", failing=failing,
                       uncompared=uncompared, parent_state=parent_state)
    if parent_state == PENDING:
        # The parent's checks have not finished. Its state is unknown,
        # and a verdict on a commit whose CI has not completed gets
        # reversed when it does. No verdict yet.
        return Verdict(kind=PENDING, failing=failing,
                       uncompared=uncompared, parent_state=parent_state)
    if parent_state == UNATTRIBUTED:
        # The parent ran no checks that completed. Nothing says it
        # was green and nothing says it was red, so the redness is
        # reported and attributed to nothing.
        return Verdict(kind=UNATTRIBUTED, failing=failing,
                       uncompared=uncompared, parent_state=parent_state)
    # The parent was green, so this commit broke the branch -- unless the
    # only red checks are ones the parent never ran.
    if uncompared and len(uncompared) == len(failing):
        return Verdict(kind="already_red", failing=failing,
                       uncompared=uncompared, parent_state=parent_state)
    # Every red check also passed at some point. The failure was real,
    # but the check ended green, so this commit did not leave the branch
    # red. It is reported apart from `broke`, so no revert is opened
    # over a flake.
    if failing and all(c.flaky for c in failing):
        return Verdict(kind=FLAKY, failing=failing,
                       uncompared=uncompared, parent_state=parent_state)
    return Verdict(kind="broke", failing=failing, uncompared=uncompared,
                   parent_state=parent_state)
