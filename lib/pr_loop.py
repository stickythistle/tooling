#!/usr/bin/env python3
"""The pure half of `pr-loop`: configuration, scope, polling arithmetic.

Everything here decides without touching GitHub or starting a run, so the
loop's behaviour is testable against recorded data the way `pr_gate.decide`
is. `bin/pr-loop` is the thin half: it calls `gh`, spawns runs, and keeps
the clocks.

The loop's contract with the outside world:

- GitHub is the only channel. State is re-read through `pr-loop-gate` on
  every poll, so stopping a loop and restarting it picks up where the PR
  stands, and two loops never need to talk to each other.
- A conditional GET on the open-PR list is the only request made when
  nothing moved; a 304 costs no rate limit.
- One run per PR at a time, `slots` PRs at a time per loop, and no new
  MiniMax run at all while `minimax_max` MiniMax processes are live.
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

# The label that admits a PR to the loop. The implementer decides what
# enters; the reviewer follows it.
PR_LABEL = "pr-loop"
# Set by the loop when both sides agree and the merge is GitHub's to make.
AGREED_LABEL = "pr-loop:agreed"
# Set, with a comment naming the reason, when the loop gives up on a PR.
ESCALATED_LABEL = "pr-loop:escalated"

# Polling arithmetic, from the issue: start at 30s, double while nothing
# changes, cap at 300s. Any change, or the loop's own post, resets it.
# The floor is an environment knob so a test can poll in milliseconds;
# the doubling and the cap are the same either way.
MIN_INTERVAL_SEC = int(os.environ.get("PR_LOOP_MIN_INTERVAL_SEC", "30"))
MAX_INTERVAL_SEC = int(os.environ.get("PR_LOOP_MAX_INTERVAL_SEC", "300"))

# Three consecutive gate ERR lines end the loop; a good gate read clears
# the counter.
MAX_CONSECUTIVE_ERRORS = 3

# The status marker, same shape pr_gate reads. The escalation comment
# carries it so the gate keeps counting the post as the loop's own.
STATUS_MARKER = "<!-- pr-loop:status review={review} head={head} -->"


class ConfigError(Exception):
    """A configuration value is present but unusable. The loop refuses
    rather than guessing at a profile or a slot count."""


class RepoConfig:
    """One repository's settings, with the issue's defaults.

    `slots` caps in-flight runs, `max_rounds` passes to the gate,
    `issue_label` names the issues a lane may take, `minimax_max` is the
    session cap, and `auto_merge` is the #82 mode: `shadow` records the
    would-merge decision, `on` asks GitHub to merge when the ruleset
    agrees.
    """

    def __init__(self, repo: str, table: dict) -> None:
        self.repo = repo
        self.slots = _posint(table, "slots", 1)
        self.reviewer_profile = _str(table, "reviewer_profile", "")
        self.implementer_profile = _str(table, "implementer_profile", "")
        self.verify = _str(table, "verify", "")
        self.max_rounds = _posint(table, "max_rounds", 4)
        self.issue_label = _str(table, "issue_label", "")
        self.minimax_max = _posint(table, "minimax_max", 4)
        measure = table.get("measure", True)
        if not isinstance(measure, bool):
            raise ConfigError("measure must be a boolean")
        self.measure = measure
        mode = _str(table, "auto_merge", "shadow")
        if mode not in ("shadow", "on"):
            raise ConfigError(f"{repo}: auto_merge must be 'shadow' or 'on'")
        self.auto_merge = mode
        # The commands the loop runs. They name the tools, not paths: a
        # checkout's bin/ on PATH resolves them, and an operator can
        # point the loop at a wrapper without editing it.
        self.implement_cmd = _str(table, "implement_cmd", "hermes-implement")
        self.fix_cmd = _str(table, "fix_cmd", "agent-fix-pr")
        self.review_cmd = _str(table, "review_cmd", "hermes-review-post")
        # The clone the loop's worktrees come from. Empty means the
        # current directory's repository.
        self.clone = _str(table, "clone", "")
        _reject_unknown(table, repo, (
            "slots", "reviewer_profile", "implementer_profile", "verify",
            "max_rounds", "issue_label", "minimax_max", "auto_merge",
            "implement_cmd", "fix_cmd", "review_cmd", "clone", "measure"))


def _str(table: dict, key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value


def _posint(table: dict, key: str, default: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{key} must be an integer >= 1")
    return value


_KNOWN_TOP = ("repos",)


def load_config(path: Path, repo: str) -> RepoConfig:
    """Read `path` and return the section for `repo`.

    A missing file is not an error: the defaults are the safe ones and a
    loop with no config still runs, narrowly. A section whose shape is
    wrong is an error, because the wrong shape is where a typo'd profile
    hides.
    """
    if not path or not Path(path).exists():
        return RepoConfig(repo, {})
    with open(path, "rb") as handle:
        try:
            doc = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    for key in doc:
        if key not in _KNOWN_TOP:
            raise ConfigError(f"{path}: unknown top-level key {key!r}")
    repos = doc.get("repos", {})
    if not isinstance(repos, dict):
        raise ConfigError(f"{path}: repos must be a table")
    for name, table in repos.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: repos.{name} must be a table")
    table = repos.get(repo, {})
    return RepoConfig(repo, table)


def _reject_unknown(table: dict, repo: str, known: tuple) -> None:
    for key in table:
        if key not in known:
            raise ConfigError(
                f"repos.{repo}: unknown key {key!r} "
                f"(known: {', '.join(known)})")


# ----- the conditional GET --------------------------------------------------

def parse_conditional_response(raw: str, returncode: int):
    """Interpret `gh api <endpoint> -i` output for a conditional GET.

    Returns (changed, etag, body):

    - 304: (False, the echoed etag, None). gh exits non-zero on 304, so
      the exit code is not consulted for it — only the status line is.
    - 2xx: (True, the new etag or None, the body after the blank line).
    - anything else: (None, None, the failure reason), which the caller
      counts toward the three-error exit.
    """
    head, _, body = raw.partition("\r\n\r\n")
    if head == raw:  # no CRLF separator; tolerate bare-LF responses
        head, _, body = raw.partition("\n\n")
    lines = head.splitlines()
    if not lines:
        return (None, None, "no status line")
    parts = lines[0].split()
    if len(parts) < 2 or not parts[0].upper().startswith("HTTP"):
        return (None, None, f"unparseable status line: {lines[0]!r}")
    try:
        status = int(parts[1])
    except ValueError:
        return (None, None, f"unparseable status: {parts[1]!r}")
    etag = None
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "etag":
            etag = value.strip()
    if status == 304:
        return (False, etag, None)
    if 200 <= status < 300:
        return (True, etag, body)
    return (None, None, f"HTTP {status}")


def next_interval(current: int, changed: bool) -> int:
    """The interval for the next poll: double on no change, reset on one,
    capped both ends."""
    if changed:
        return MIN_INTERVAL_SEC
    return min(current * 2, MAX_INTERVAL_SEC)


# ----- scope ---------------------------------------------------------------

def pr_labels(pr: dict) -> list:
    """The label names of one entry from the pulls list.

    The list embeds labels as objects; a missing or odd `labels` reads as
    no labels rather than an error, because a pull request nobody labelled
    is the normal case and the loop has nothing to do with it.
    """
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return []
    out = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            out.append(label["name"])
    return out


def scope_prs(entries: list, from_n: int) -> list:
    """The open PRs in scope: labelled pr-loop, numbered at or above
    `from_n`, lowest number first.

    Agreed PRs stay in scope — their gate line is `DONE merged` or a
    wait, and the loop learns the merge happened. Escalated PRs drop out:
    a person decides what happens next.
    """
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if not isinstance(number, int) or number < from_n:
            continue
        labels = pr_labels(entry)
        if PR_LABEL not in labels or ESCALATED_LABEL in labels:
            continue
        out.append(entry)
    out.sort(key=lambda e: e["number"])
    return out


def next_issue(issues: list, issue_label: str, from_n: int,
               claimed: set) -> int | None:
    """The lowest-numbered eligible issue, or None.

    Eligible: open, carrying `issue_label`, numbered at or above
    `from_n`, and not `claimed` — the numbers a loop already opened a PR
    for. A loop that opened a PR for an issue and lost its state finds
    the claim through the open PRs rather than opening a second one.
    """
    if not issue_label:
        return None
    numbers = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        number = issue.get("number")
        if not isinstance(number, int) or number < from_n:
            continue
        if number in claimed:
            continue
        if issue.get("pull_request") is not None:
            continue
        labels = [n for n in pr_labels(issue)]
        if issue_label not in labels:
            continue
        numbers.append(number)
    return min(numbers) if numbers else None


def issue_claimed_by(issue_number: int, prs: list) -> bool:
    """Whether any in-list PR is the loop's PR for this issue.

    The claim is the branch name the loop uses — loop/issue-<n> — or a
    `Closes #n` line in the body, matched exactly so `Closes #13` does
    not read as a claim on 3.
    """
    branch = f"loop/issue-{issue_number}"
    closes = re.compile(rf"Closes #{issue_number}(?![0-9])")
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        head = pr.get("head")
        if isinstance(head, dict) and head.get("ref") == branch:
            return True
        body = pr.get("body")
        if isinstance(body, str) and closes.search(body):
            return True
    return False


def base_blocked(pr_entry: dict, default_branch: str, prs: list) -> bool:
    """Whether this PR's base is an open PR that is not agreed yet.

    A PR based on another branch waits for that branch's PR: merging a
    stack member before its parent agreed would merge unreviewed code.
    A base that is nobody's open PR (a plain long-lived branch) is not
    blocked — there is nothing to wait for.
    """
    base = pr_entry.get("base")
    if not isinstance(base, dict):
        return False
    base_ref = base.get("ref")
    if not isinstance(base_ref, str) or base_ref == default_branch:
        return False
    for other in prs:
        if not isinstance(other, dict):
            continue
        head = other.get("head")
        if not isinstance(head, dict) or head.get("ref") != base_ref:
            continue
        labels = pr_labels(other)
        if PR_LABEL not in labels:
            return True  # the parent is in the loop's way but not of it
        if AGREED_LABEL not in labels:
            return True
    return False


# ----- the run bookkeeping ---------------------------------------------------

def can_start(role: str, profile: str, running: list, config: RepoConfig,
              live_minimax: int) -> bool:
    """Whether the loop may start one more run.

    Three limits, all from the issue: `slots` per loop, one run per PR
    (the caller checks its own map first), and the MiniMax session cap —
    a fifth MiniMax session falls back to the local model and evicts
    whatever is loaded there, so the cap counts every live MiniMax
    process, not just this loop's.
    """
    if len(running) >= config.slots:
        return False
    if profile and profile.startswith("minimax") and live_minimax >= config.minimax_max:
        return False
    return True


def should_escalate(failures: int) -> bool:
    """A run that fails twice ends the loop's involvement with the PR."""
    return failures >= 2


def gate_error_is_terminal(count: int) -> bool:
    return count >= MAX_CONSECUTIVE_ERRORS


def parse_gate_line(line: str):
    """Split a gate line into (verb, argument) or None when unparseable.

    The gate's own format: VERB, or VERB with one space-separated
    argument.
    """
    line = (line or "").strip()
    if not line:
        return None
    verb, _, rest = line.partition(" ")
    if verb not in ("FIX", "AGREED", "REVIEW", "REAPPROVE", "WAIT",
                    "ESCALATE", "DONE", "ERR"):
        return None
    return (verb, rest.strip())
