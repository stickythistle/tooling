#!/usr/bin/env python3
"""Run each finding's reproduction against the reviewed head and keep only
the ones that fail.

A reviewer that reads a diff cannot see a bug that only running the code
shows. This module is the backstop for the findings such a reviewer missed:
it takes the findings an agentic lane produced, re-runs the command each
finding carries, and compares the outcome against the claim the finding
makes.

A finding counts only when its command ran and failed. A command that
passed, never ran, or changed the tree under review is dropped, and the
tree goes back to the reviewed head before the next one runs.

A reproduction still runs with this process's environment and can reach the
network. This lane prevents neither: a write is caught after the fact, and
a network call by neither. Preventing it means a sandbox, which is the
owner's call because it changes what runs on the host.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# The status a finding can end in. Every finding ends in exactly one.
KEPT = "reproduced"
DROPPED_NO_REPRODUCTION = "no-reproduction"
DROPPED_NOT_REPRODUCED = "not-reproduced"
DROPPED_ERROR = "reproduction-error"

STATUSES = (KEPT, DROPPED_NO_REPRODUCTION, DROPPED_NOT_REPRODUCED,
            DROPPED_ERROR)

USAGE = ("usage: review_execute.py <worktree> <findings.json> "
         "--head SHA [--out FILE] [--timeout SECONDS]")

# A first pass over the command's program name. The guarantee rests on the
# tree comparison in `classify`, which catches a write no list of names
# anticipated.
FORBIDDEN = (
    "curl", "wget", "ssh", "scp", "rsync", "nc", "telnet",
    "rm", "mv", "cp", "dd", "mkfs", "shutdown", "reboot", "halt",
    "sudo", "su", "doas",
    "kill", "killall", "pkill",
    "systemctl", "service",
    "chmod", "chown",
    "touch", "mkdir", "rmdir", "truncate",
    "tee", "install",
)

# The git subcommands that only read the tree. Anything else writes. The
# subcommand is found past git's own options, so `git -C /path show` is
# recognised as the read it is.
READ_ONLY_GIT = frozenset({
    "show", "log", "diff", "grep", "rev-parse", "status", "ls-files",
    "ls-tree", "cat-file", "blame", "describe", "rev-list", "shortlog",
    "symbolic-ref", "merge-base", "for-each-ref", "name-rev",
})


class ExecutionError(Exception):
    """Something went wrong that is no the finding's fault."""


def die(message: str) -> "NoReturn":
    sys.stderr.write(message + "\n")
    sys.exit(2)


def verify_head(worktree: Path, head: str) -> str:
    """Refuse unless the worktree's HEAD is exactly the reviewed commit.

    An ancestor check is no enough: a head force-pushed back to an
    ancestor is still an ancestor of itself.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"review_execute: cannot read HEAD in {worktree}: "
            f"{result.stderr.strip() or 'git failed'}")
    actual = result.stdout.strip()
    if actual != head:
        die(f"review_execute: refusing to run: worktree {worktree} is at "
            f"{actual[:12]} but the review was asked to run against "
            f"{head[:12]}")
    return actual


def tree_state(worktree: Path) -> str:
    """The worktree's state, so a reproduction that changed it is seen.

    `--ignored` is in the command because an ignored file is a change to the
    tree too, and the next reproduction would run against it.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--ignored"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return f"<git status failed: {result.stderr.strip() or 'git failed'}>"
    return result.stdout


def shell_tokens(command: str) -> list[str] | None:
    """Tokenise a command the way the shell would, or None if it fails to parse.

    `shlex` with punctuation enabled separates a metacharacter the shell
    acts on from one inside quotes.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def git_subcommand(tokens: list[str]) -> str | None:
    """The git subcommand a command runs, or None when there is none.

    `git show` and `git -C /path show` both run `show`, but the options
    that take a value come first. Reading `tokens[1]` calls `git -C /path
    show` a write.
    """
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            # An option that takes a value takes the next token with it.
            skip_next = token in ("-C", "-c", "--git-dir", "--work-tree",
                                  "--namespace", "--exec-path")
            continue
        return token
    return None


def check_command(command: str) -> str | None:
    """Return a refusal reason, or None when the command may run.

    The check is on the shell tokens: `git show` is allowed, `git push`
    refused.
    """
    stripped = command.strip()
    if not stripped:
        return "the reproduction command is empty"
    tokens = shell_tokens(stripped)
    if tokens is None:
        return "the reproduction command does not parse"
    if not tokens:
        return "the reproduction command is empty"
    # The shell operators that make a command more than one command. A
    # token that is exactly one of these is an operator; one that merely
    # contains one (a filename, an argument) never is.
    operators = ("|", "||", "&&", ";", "&", ">", ">>", "<", "<<",
                 "|&", ">&", "<&", "(", ")")
    for token in tokens:
        if token in operators:
            return (f"the reproduction command contains the shell operator "
                    f"{token!r}; a reproduction must be one command, so what "
                    "actually ran can be checked")
        if token in ("`", "$(", "${"):
            return (f"the reproduction command contains {token!r}; a "
                    "reproduction must be one command, so what actually ran "
                    "can be checked")
    program = Path(tokens[0]).name
    if program in FORBIDDEN:
        return (f"the reproduction command runs {program!r}, which would "
                "change the tree or reach off this machine")
    if program == "git":
        sub = git_subcommand(tokens)
        if sub is None:
            return "the reproduction command is a bare `git`"
        if sub not in READ_ONLY_GIT:
            return (f"the reproduction command runs `git {sub}`, which writes; "
                    "a reproduction must only read the tree")
    return None


def run_reproduction(worktree: Path, command: str,
                     timeout: int) -> tuple[int, str]:
    """Run the reproduction in the worktree. Returns (status, output).

    The output is stdout and stderr together, because a reproduction that
    fails loudly on stderr is the common case.

    The environment refuses to leave a bytecode cache behind.
    """
    env = dict(os.environ)
    # A reproduction must not stop to ask for anything: a credential
    # prompt would hang the lane until its timeout.
    env["GIT_TERMINAL_PROMPT"] = "0"
    # No bytecode cache, and no pytest cache: both are written into the
    # tree the reproduction runs in.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = env.get("PYTEST_ADDOPTS", "") + " -p no:cacheprovider"
    try:
        # Bash, not `shell=True`. The shell decides how a death by signal
        # is reported: bash hands back the negative signal number, while
        # dash folds it into 128 plus that number. The verdict needs the
        # two apart.
        proc = subprocess.run(
            ["bash", "-c", command], cwd=str(worktree), env=env,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return (124, f"the reproduction timed out after {timeout}s")
    except OSError as exc:
        raise ExecutionError(str(exc)) from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    return (proc.returncode, output)


def exit_failure(status: int) -> str | None:
    """Why this exit says nothing about the defect, or None when it does.

    Evidence needs a command that ran and failed. The shell's codes for
    one that never ran are 126 and 127, and a signal arrives negative.
    """
    if status == 124:
        return "the reproduction timed out"
    if status == 126:
        return "the reproduction could not be executed (126)"
    if status == 127:
        return "the reproduction command was not found (127)"
    if status < 0:
        return (f"the reproduction died by signal {-status}")
    return None


def classify(finding: object, worktree: Path, timeout: int) -> dict:
    """Put one finding in its status. Never raises for a bad finding."""
    if not isinstance(finding, dict):
        return {"status": DROPPED_ERROR, "reason": "finding is not an object",
                "value": finding}
    command = finding.get("reproduce")
    if not isinstance(command, str) or not command.strip():
        return {**finding, "status": DROPPED_NO_REPRODUCTION,
                "reason": "the finding carries no reproduce command"}
    reason = check_command(command)
    if reason is not None:
        return {**finding, "status": DROPPED_ERROR, "reason": reason}
    before = tree_state(worktree)
    try:
        status, output = run_reproduction(worktree, command, timeout)
    except ExecutionError as exc:
        return {**finding, "status": DROPPED_ERROR,
                "reason": f"the reproduction could not be started: {exc}"}
    # The lane's own transcript is kept: it is what the agent saw, and a
    # reader comparing it against the re-run's output can see whether the
    # two runs agree. The re-run's own output is what the verdict rests on.
    record = {**finding, "reproduction_status": status,
              "reproduction_output": output}
    # The tree is compared on every path that ran a command, including the
    # ones whose exit says nothing about the defect. A command that writes
    # a file and then hangs left the tree dirty.
    changed = tree_state(worktree) != before
    failure = exit_failure(status)
    if failure is not None:
        # Both can hold: the exit says the command never ran, and the tree
        # says it wrote anyway. The exit reason is kept because it is the
        # first thing wrong, and the flag says the tree has to be put back.
        return {**record, "status": DROPPED_ERROR, "reason": failure,
                "tree_changed": changed}
    if changed:
        return {**record, "status": DROPPED_ERROR,
                "reason": "the reproduction changed the tree under review",
                "tree_changed": True}
    if status == 0:
        # The command passed. Whatever the finding claims, the reviewed
        # head shows no such defect.
        return {**record, "status": DROPPED_NOT_REPRODUCED,
                "reason": "the reproduction passed on the reviewed head",
                "tree_changed": False}
    return {**record, "status": KEPT,
            "observed": output,
            "claimed_output": finding.get("observed")
            if isinstance(finding.get("observed"), str) else None,
            "tree_changed": False}


def restore_worktree(worktree: Path, head: str) -> bool:
    """Put the worktree's tree back at the reviewed head. True when it worked.

    A reproduction that changed the tree has left evidence behind, and the
    next one would run against a tree that has left the reviewed head.

    `-x` is in the clean because the tree state is read with `--ignored`:
    an ignored file is a change to the tree, so it has to go too.

    This is destructive to anything uncommitted, so `process` refuses a
    dirty tree.
    """
    reset = subprocess.run(
        ["git", "-C", str(worktree), "reset", "--hard", head],
        capture_output=True, text=True, check=False,
    )
    if reset.returncode != 0:
        return False
    clean = subprocess.run(
        ["git", "-C", str(worktree), "clean", "-ffdx"],
        capture_output=True, text=True, check=False,
    )
    return clean.returncode == 0


def process(worktree: Path, findings: list, head: str,
            timeout: int) -> tuple[list[dict], dict[str, int]]:
    """Classify every finding. Returns (annotated findings, tally)."""
    verify_head(worktree, head)
    # The worktree must be clean before the first finding. A restore puts
    # the tree back with `reset --hard` and `clean -ffdx`, which on a
    # developer's working tree would take their uncommitted edits.
    #
    # So the lane refuses a dirty tree, and a restore can only ever
    # remove what a reproduction wrote.
    dirty = tree_state(worktree)
    if dirty.strip():
        die(f"review_execute: the worktree is not clean:\n{dirty.rstrip()}\n"
            "a reproduction that changed it would put it back with "
            "`reset --hard` and `clean -ffdx`, which would take your "
            "uncommitted work with it")
    annotated = []
    for finding in findings:
        result = classify(finding, worktree, timeout)
        annotated.append(result)
        # A reproduction that changed the tree leaves the next one running
        # against a tree that has left the reviewed head, so the worktree
        # goes back before the next finding is judged. The flag says so.
        if result.get("tree_changed"):
            restore_worktree(worktree, head)
    tally = {s: 0 for s in STATUSES}
    for f in annotated:
        status = f.get("status", DROPPED_ERROR)
        tally[status] = tally.get(status, 0) + 1
    return (annotated, tally)


def main() -> int:
    if len(sys.argv) < 3:
        die(USAGE)
    worktree = Path(sys.argv[1])
    findings_path = Path(sys.argv[2])
    args = sys.argv[3:]
    head = ""
    out_path = None
    timeout = 120
    i = 0
    while i < len(args):
        if args[i] == "--head" and i + 1 < len(args):
            head = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--timeout" and i + 1 < len(args):
            try:
                timeout = int(args[i + 1])
            except ValueError:
                die(f"review_execute: --timeout must be a number, "
                    f"got {args[i + 1]!r}")
            i += 2
        else:
            die(USAGE)
    if not head:
        die(f"review_execute: --head is required\n{USAGE}")
    if not worktree.is_dir():
        die(f"review_execute: {worktree} is not a directory")
    if not findings_path.is_file():
        die(f"review_execute: {findings_path} is not a file")

    try:
        findings = json.loads(findings_path.read_text())
    except json.JSONDecodeError as exc:
        die(f"review_execute: {findings_path}: {exc.msg} "
            f"(line {exc.lineno} col {exc.colno})")
    if not isinstance(findings, list):
        die("review_execute: findings must be a JSON array")

    annotated, tally = process(worktree, findings, head, timeout)

    payload = {"head": head, "tally": tally, "findings": annotated}
    text = json.dumps(payload, indent=2)
    if out_path:
        out_path.write_text(text + "\n")
    else:
        sys.stdout.write(text + "\n")
    # The tally goes to stderr so a caller reading stdout as JSON stays
    # unbroken.
    parts = [f"{k}={tally[k]}" for k in STATUSES if tally[k]]
    sys.stderr.write("review_execute: " + ", ".join(parts) + "\n")
    return 0


if __name__ == "__main__":
    main()
