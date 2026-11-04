#!/usr/bin/env python3
"""Attribute a finding on a stacked pull request to the PR that wrote its quote.

Reviewing the top of a stack against the default branch sees the combined
tree of every PR below it; this module says which PR owns each finding. A
finding belongs to the PR whose commit last wrote the code the reviewer
quoted: finding-check locates the quote in the file, the located line is
blamed at the reviewed head, and the blamed commit is charged to the PR
whose range (`base..head`) holds it.

The cited line numbers are not used. Reviewers' line numbers drift, often
by tens of lines, and a drifted line lands on code another PR wrote or on
code no PR wrote at all.

Three answers, and every finding lands in exactly one:

- `attributed` -- the PR named owns the blamed commit.
- `unlocated` -- the quote was not located in the file, so the finding is
  charged to nobody: a citation that cannot be located is evidence of
  nothing.
- `stack` -- the located line was written before the stack began, so the
  finding is charged to the stack as a whole.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


class StackError(Exception):
    """A stack could not be established from what the caller supplied."""


def gh_json(args: list[str]) -> object:
    """`gh <args>` and its parsed JSON."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        raise StackError(
            f"gh {' '.join(args)} failed: "
            f"{result.stderr.strip() or 'no stderr'}"
        )
    out = result.stdout
    return json.loads(out) if out.strip() else None


@dataclass
class PullRequest:
    """The shape of a pull request the stack is read from."""

    number: int
    head_ref: str
    base_ref: str
    title: str = ""
    state: str = "OPEN"


def open_pull_requests(repo: str) -> list["PullRequest"]:
    """Every open pull request of the repository."""
    raw = gh_json([
        "pr", "list", "--repo", repo, "--state", "open", "--limit", "200",
        "--json", "number,headRefName,baseRefName,title",
    ])
    prs = []
    for entry in raw or []:
        prs.append(PullRequest(
            number=entry["number"],
            head_ref=entry["headRefName"],
            base_ref=entry["baseRefName"],
            title=entry.get("title", ""),
        ))
    return prs


def default_branch(repo: str) -> str:
    """The repository's default branch."""
    raw = gh_json(["repo", "view", "--repo", repo, "--json",
                   "defaultBranchRef"])
    if not raw or not raw.get("defaultBranchRef"):
        raise StackError(
            f"could not resolve the default branch of {repo}"
        )
    return raw["defaultBranchRef"]["name"]


def discover_stack(prs: list[PullRequest], number: int,
                   default_branch: str) -> list[PullRequest]:
    """The stack a pull request belongs to, bottom first.

    A PR is stacked on another when its base branch is another open PR's head
    branch. The chain runs down to the PR based on the default branch and up
    to the top, which carries every change below it and is the one reviewed.
    `prs` is every open pull request; a PR stacked on nothing is a stack of
    one.

    A PR based on any branch that is neither the default branch nor an open
    PR's head is refused. A stack that forks -- more than one open PR based
    on the same member's head -- has no single top and is refused naming the
    siblings.
    """
    by_number = {pr.number: pr for pr in prs}
    by_head: dict[str, PullRequest] = {}
    for pr in prs:
        # The first PR wins a head branch: two open PRs from one branch are
        # not a stack, and the lower one is the one a stack would build on.
        by_head.setdefault(pr.head_ref, pr)

    if number not in by_number:
        raise StackError(f"PR {number} is not an open pull request")

    # Downwards: from `number` to the floor.
    chain: list[PullRequest] = []
    seen: set[int] = set()
    current = by_number[number]
    while True:
        if current.number in seen:
            raise StackError(
                f"PR {current.number}'s stack contains a cycle"
            )
        seen.add(current.number)
        chain.append(current)
        if current.base_ref == default_branch:
            break
        parent = by_head.get(current.base_ref)
        if parent is None:
            raise StackError(
                f"PR {current.number} is based on {current.base_ref!r}, which "
                f"is neither the default branch {default_branch!r} nor the "
                f"head of an open pull request; it is not part of a stack"
            )
        current = parent
    chain.reverse()

    # Upwards: a PR based on any member's head is stacked on it. Two PRs on
    # one head fork the stack; the named PR counts as a sibling, so the map
    # covers every open PR.
    based_on: dict[str, list[PullRequest]] = {}
    for pr in prs:
        based_on.setdefault(pr.base_ref, []).append(pr)

    grown = True
    while grown:
        grown = False
        for member in chain:
            siblings = based_on.get(member.head_ref, [])
            if len(siblings) > 1:
                names = ", ".join(f"#{sibling.number}"
                                  for sibling in sorted(siblings,
                                                        key=lambda p: p.number))
                raise StackError(
                    f"the stack forks at {member.head_ref!r}: {names} are "
                    f"all open against it, so the stack has no single top; "
                    f"close or retarget the sibling, and name the branch to "
                    f"review with --pr"
                )
        for pr in prs:
            if pr.number in seen:
                continue
            if pr.base_ref in {m.head_ref for m in chain}:
                chain.append(pr)
                seen.add(pr.number)
                grown = True
    return chain


def stack_floor(stack: list[PullRequest], default_branch: str) -> str:
    """The branch the whole stack is reviewed against: the bottom PR's base.

    Refuses a stack whose floor is not the default branch.
    """
    if not stack:
        raise StackError("the stack is empty")
    floor = stack[0].base_ref
    if floor != default_branch:
        raise StackError(
            f"the stack is based on {floor!r}, not the default branch "
            f"{default_branch!r}"
        )
    return floor


def _git(repo: str, *args: str) -> str:
    """Run git in `repo` and return stdout. Raises on failure."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise StackError(
            f"git {' '.join(args)} failed: "
            f"{result.stderr.strip() or 'no stderr'}"
        )
    return result.stdout


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """True when `ancestor` is in the history of `descendant`.

    A range that cannot be decided raises instead of reading as empty or as
    containing everything.
    """
    result = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor",
         ancestor, descendant],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise StackError(
        f"merge-base --is-ancestor {ancestor} {descendant} failed: "
        f"{result.stderr.strip() or 'no output'}"
    )


def owner_of(repo: str, ranges: list[tuple[int, str, str]],
             commit: str) -> int | None:
    """The PR whose `base..head` range holds `commit`, or None.

    `ranges` is the stack bottom first as (number, base_ref, head_ref).
    Each member's base is the one below it's head, so the ranges are
    disjoint and a commit belongs to at most one.
    """
    for number, base_ref, head_ref in ranges:
        if is_ancestor(repo, commit, head_ref) and \
                not is_ancestor(repo, commit, base_ref):
            return number
    return None


def parse_blame(porcelain: str) -> dict[int, str]:
    """The commit each final line came from, from `git blame --porcelain`.

    The header line is `<sha> <orig-line> <final-line> [<num-lines>]`. When
    a commit contributed several consecutive lines, the first header carries
    the count and the rest are bare `<sha> <orig-line> <final-line>`
    headers, so the count is what says how many lines to expect.
    """
    shas: dict[int, str] = {}
    lines = porcelain.splitlines()
    i = 0
    while i < len(lines):
        header = lines[i]
        if not header or header.startswith("\t"):
            i += 1
            continue
        parts = header.split(" ")
        if len(parts) < 3:
            i += 1
            continue
        sha, _, final = parts[0], parts[1], parts[2]
        try:
            final_line = int(final)
        except ValueError:
            i += 1
            continue
        count = 1
        if len(parts) >= 4:
            try:
                count = int(parts[3])
            except ValueError:
                count = 1
        for offset in range(count):
            shas[final_line + offset] = sha
        # Skip the metadata block up to the content line.
        while i < len(lines) and not lines[i].startswith("\t"):
            i += 1
        i += 1
    return shas


class Blamer:
    """The blamed commit per line, cached by path.

    Blame walks a file's history once, and a review puts most of its
    findings in a handful of files. A path git cannot blame is a path the
    reviewed tree does not hold: the finding it belongs to is charged to
    nobody rather than aborting the batch.
    """

    def __init__(self, repo: str, rev: str) -> None:
        self.repo = repo
        self.rev = rev
        self._cache: dict[str, dict[int, str] | None] = {}

    def blame(self, path: str) -> dict[int, str]:
        if path not in self._cache:
            result = subprocess.run(
                ["git", "-C", self.repo, "blame", "--porcelain", self.rev,
                 "--", path],
                capture_output=True, text=True, check=False,
            )
            self._cache[path] = (
                parse_blame(result.stdout) if result.returncode == 0
                else None
            )
        cached = self._cache[path]
        return cached if cached is not None else {}


def git_path(repo: str, path: str) -> str:
    """The path as git spells it, relative to the repository root.

    A reviewer may write `./src/a.rs`; git names the file `src/a.rs`. The
    reviewer's spelling is kept in the finding, so the two are both there.
    """
    from pathlib import Path as _Path
    root = _Path(repo).resolve(strict=False)
    candidate = (root / path).resolve(strict=False)
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return path


# The finding-check status that means the quote was found in the file.
# `redaction-artifact` means the quote was found but holds a `***` the
# agent's own redaction wrote: that text is the agent's rendering, not the
# source's, so it is not a located quote.
LOCATED_STATUS = "located"

ATTRIBUTED = "attributed"
UNLOCATED = "unlocated"
STACK = "stack"

STATUSES = (ATTRIBUTED, UNLOCATED, STACK)


def attribute_finding(finding: object, repo: str,
                      ranges: list[tuple[int, str, str]],
                      blamer: Blamer) -> dict:
    """Charge one located finding. Never raises for a bad finding."""
    def annotated(**fields: object) -> dict:
        base = finding if isinstance(finding, dict) else {"value": finding}
        return {**base, **fields}

    def unlocated(reason: str) -> dict:
        return annotated(pr=None, reason=UNLOCATED, detail=reason)

    status = finding.get("status") if isinstance(finding, dict) else None
    if status != LOCATED_STATUS:
        # A quote that was not located has no located line, and the cited
        # line is not a substitute.
        return unlocated(f"the quoted code was not located "
                         f"(finding-check status {status!r})")

    line = finding.get("line")  # type: ignore[union-attr]
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return unlocated(f"the located line {line!r} is not a line number")

    path = finding.get("path")  # type: ignore[union-attr]
    if not isinstance(path, str) or not path:
        return unlocated("the finding names no path")

    commit = blamer.blame(git_path(repo, path)).get(line)
    if commit is None:
        return unlocated(f"line {line} of {path} is not in the file as "
                         f"blamed at the reviewed head")

    pr = owner_of(repo, ranges, commit)
    if pr is None:
        # Written before the stack began: the stack as a whole owns it,
        # which is a different answer from owning it by one PR.
        return annotated(pr=None, reason=STACK, blamed_commit=commit)
    return annotated(pr=pr, reason=ATTRIBUTED, blamed_commit=commit)
