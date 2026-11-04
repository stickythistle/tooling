#!/usr/bin/env python3
"""Attribute a finding on a stacked pull request to the PR whose diff introduced it.

A stacked PR is reviewed against its own base, so a defect that appears only
when two PRs combine is invisible to both. Reviewing the top of the stack
against the default branch sees the combined tree; this module then says
which PR of the stack owns each `path:line` in the reviewed tree.

The rule: a line belongs to the PR that introduced it, not the PR that last
edited it, because the fix goes in the introducer. A finding on a line no PR
of the stack changed is reported against the stack as a whole.

A line's position moves as the stack grows, so each PR's changed lines are
carried forward into the reviewed tree's coordinates, one stack level at a
time.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

# A hunk header: `@@ -old[,count] +new[,count] @@`.
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# A path reference in a reviewer's findings: `path:line` or `path:line-line`
# (a range resolves to its first line). The path runs to the last colon
# followed by a number, so `crates/a:b.rs` still resolves.
REF_RE = re.compile(
    r"(?P<path>[^\s`():,;]+?):(?P<start>\d+)(?:-(?P<end>\d+))?"
)

# The number of context lines that makes `git diff` show every line of a
# file. `git diff -U<n>` uses a huge n to mean "no limit".
FULL_CONTEXT = "1000000"


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


@dataclass
class PullRequest:
    """The shape of a pull request the stack is read from."""

    number: int
    head_ref: str
    base_ref: str
    title: str = ""
    state: str = "OPEN"


def discover_stack(prs: list[PullRequest], number: int,
                   default_branch: str) -> list[PullRequest]:
    """The stack a pull request belongs to, bottom first.

    A PR is stacked on another when its base branch is another open PR's head
    branch. The chain is followed down to the PR based on the default branch,
    then up to the top, which carries every change below it and is the one
    reviewed. `prs` is every open pull request; a PR stacked on nothing is a
    stack of one.

    A PR based on any branch that is neither the default branch nor an open
    PR's head is refused: the stack does not have that base. A stack that
    forks -- more than one open PR based on the same member's head -- has no
    single top to review and is refused naming the siblings.
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


@dataclass
class StackMember:
    """One pull request of the stack.

    `base_ref` and `head_ref` are refs in the local repository, resolved by
    the caller: `base_ref` is the branch the PR is based on, `head_ref` the
    branch it pushes. The stack is ordered bottom first.
    """

    number: int
    head_ref: str
    base_ref: str
    title: str = ""
    # The commit the PR's head is at, as the caller resolved it.
    head_sha: str = ""
    # New-side line ranges per path, in this PR's own head-tree coordinates.
    # Filled in by `load_own_ranges`; empty means "not computed yet".
    own_ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


@dataclass
class Attribution:
    """Where one finding lands in the stack."""

    path: str
    line: int
    # The PR number that introduced the line, or None when no PR of the
    # stack changed it.
    pr: int | None
    # Why: `introduced`, `stack` (no PR changed the line), or
    # `unattributable` (the path is not in the reviewed tree at all).
    reason: str

    @property
    def attributed(self) -> bool:
        """True when a single PR of the stack owns the finding."""
        return self.pr is not None


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


def parse_hunks(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """New-side line ranges per path, from a `git diff --unified=0`.

    A pure deletion in a surviving file still gets a one-line range at the
    deletion point, so a finding about the removed code counts as in the
    change.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    path = None
    for line in diff_text.splitlines():
        # The `+++` line names the new side, which is what a finding cites.
        # A deleted file's is `/dev/null`, and its hunks belong to no path.
        if line.startswith("+++ "):
            m = re.match(r"^\+\+\+ b/(.+)$", line)
            path = m.group(1) if m else None
            continue
        m = HUNK_RE.match(line)
        if m and path is not None:
            start = int(m.group(3))
            count = int(m.group(4)) if m.group(4) is not None else 1
            ranges.setdefault(path, []).append((start, start + max(count, 1) - 1))
    return ranges


def build_line_map(repo: str, from_tree: str,
                   to_tree: str) -> dict[str, dict[int, int]]:
    """Map line numbers of `from_tree` onto `to_tree`, per path.

    The diff uses full context so every unchanged line is present and
    mappable. A deleted line and the addition that replaced it are the same
    slot, so the deleted line maps onto the added line's position.

    A deletion pairs only with an addition in the same run of changed
    lines: pending deletions clear at every hunk header and context line,
    because carrying one across a context line pairs it with any later
    addition in the file.
    """
    if from_tree == to_tree:
        # The same tree: every line maps to itself. This is the top PR of a
        # stack, whose head *is* the reviewed tree.
        return {}
    diff = _git(repo, "diff", f"--unified={FULL_CONTEXT}", from_tree, to_tree)
    mapping: dict[str, dict[int, int]] = {}
    path = None
    old = new = 0
    deleted: list[int] = []
    for line in diff.splitlines():
        if line.startswith("--- ") or line.startswith("diff --git "):
            continue
        if line.startswith("+++ "):
            m = re.match(r"^\+\+\+ b/(.+)$", line)
            path = m.group(1) if m else None
            if path is not None:
                mapping.setdefault(path, {})
            old = new = 0
            deleted = []
            continue
        m = HUNK_RE.match(line)
        if m and path is not None:
            old = int(m.group(1))
            new = int(m.group(3))
            # A hunk header means the next lines are a fresh run of changes;
            # anything deleted in the previous run stayed deleted.
            deleted = []
            continue
        if path is None:
            continue
        if line.startswith("-"):
            deleted.append(old)
            old += 1
        elif line.startswith("+"):
            new += 1
            # The oldest pending deletion is the one this addition replaced.
            if deleted:
                mapping[path][deleted.pop(0)] = new - 1
        elif line.startswith(" "):
            mapping[path][old] = new
            old += 1
            new += 1
            # A context line ends the run: a deletion before it and an
            # addition after it are different edits at different positions,
            # not a replacement of one another.
            deleted = []
    return mapping


def compose_line_maps(first: dict[str, dict[int, int]],
                      second: dict[str, dict[int, int]]
                      ) -> dict[str, dict[int, int]]:
    """Carry a mapping through another mapping.

    `first` maps a PR's own tree onto an intermediate tree, and `second` maps
    that intermediate tree onto the reviewed tree. The composition maps the
    PR's own tree onto the reviewed tree.
    """
    composed: dict[str, dict[int, int]] = {}
    for path, lines in first.items():
        onward = second.get(path, {})
        for old, mid in lines.items():
            composed.setdefault(path, {})[old] = onward.get(mid, mid)
    return composed


def load_own_ranges(repo: str, member: StackMember) -> StackMember:
    """Fill in the lines this PR's own diff changed, in its head's coordinates.

    The diff is `base...head`, so a commit the base gained since the fork
    does not count as the PR's change.
    """
    diff = _git(repo, "diff", "--unified=0",
                f"{member.base_ref}...{member.head_ref}")
    member.own_ranges = parse_hunks(diff)
    return member


def tree_paths(repo: str, tree: str) -> set[str]:
    """The files the reviewed tree holds.

    A finding citing a path outside it names something the review never saw.
    """
    listing = _git(repo, "ls-tree", "-r", "--name-only", tree)
    return {line for line in listing.splitlines() if line}


def introduced_by(repo: str, members: list[StackMember],
                  final_tree: str) -> dict[str, dict[int, int]]:
    """Which PR introduced which line of the reviewed tree, per path.

    The result maps `path` to `line -> pr number`; a line no PR changed is
    absent. `members` is the stack, bottom first.
    """
    owner: dict[str, dict[int, int]] = {}
    for index, member in enumerate(members):
        if not member.own_ranges:
            load_own_ranges(repo, member)
        own = member.own_ranges
        # The member's own head tree, carried forward to the reviewed tree.
        pr_map = carry_to_final(repo, members, index, final_tree)
        for path, ranges in own.items():
            path_map = pr_map.get(path, {})
            for start, end in ranges:
                for line in range(start, end + 1):
                    # A line with no counterpart in the reviewed tree was
                    # deleted above; it has no position there, so it cannot
                    # be attributed.
                    if path_map and line not in path_map:
                        continue
                    final_line = path_map.get(line, line)
                    # The lowest PR that introduced a line owns it: a PR
                    # above it may have edited the line, but did not
                    # introduce it.
                    owner.setdefault(path, {}).setdefault(final_line,
                                                         member.number)
    return owner


def carry_to_final(repo: str, members: list[StackMember], index: int,
                   final_tree: str) -> dict[str, dict[int, int]]:
    """Map member `index`'s own head tree onto the reviewed tree.

    The carry goes one stack level at a time: a diff taken across two
    levels at once cannot tell which of two adjacent changed lines
    replaced which.
    """
    member = members[index]
    if member.head_ref == final_tree or member.head_sha == final_tree:
        return {}
    result: dict[str, dict[int, int]] = {}
    # The tree this member's head is at, and the tree above it.
    current = member.head_ref
    for level in range(index + 1, len(members)):
        above = members[level]
        step = build_line_map(repo, current, above.head_ref)
        result = compose_line_maps(result, step) if result else step
        current = above.head_ref
    return result


def attribute_finding(repo: str, members: list[StackMember],
                      final_tree: str, path: str,
                      line: int) -> Attribution:
    """Attribute one `path:line` to the PR that introduced it."""
    owner = introduced_by(repo, members, final_tree)
    if path not in tree_paths(repo, final_tree):
        # The review never saw this path -- a different answer from "a line
        # no PR changed".
        return Attribution(path=path, line=line, pr=None,
                           reason="unattributable")
    pr = owner.get(path, {}).get(line)
    if pr is None:
        return Attribution(path=path, line=line, pr=None, reason="stack")
    return Attribution(path=path, line=line, pr=pr, reason="introduced")


def parse_references(text: str) -> list[tuple[str, int]]:
    """The `path:line` references in a reviewer's findings.

    Every reference the text carries, in the order it carries them. A
    reference with a range (`path:12-20`) resolves to its first line, which
    is the line the finding is about.
    """
    found: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for m in REF_RE.finditer(text):
        path = m.group("path")
        if not path:
            continue
        try:
            line = int(m.group("start"))
        except ValueError:
            continue
        if line < 1:
            continue
        key = (path, line)
        if key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found
