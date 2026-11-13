#!/usr/bin/env python3
"""The review threads of a pull request, and the rule for an unanswered one.

`merge-check` and `pr-ready --teammate` ask which threads carry a finding
nobody has answered, and the fetch and the rule live here.
"""
from __future__ import annotations

import json
import subprocess

# The query, and the fields it asks for: whether each thread is resolved,
# where it sits, and its first two comments. Two comments tell a finding
# from a finding with a reply.

# The threads are read through GraphQL: the REST comments carry no
# resolution state.
REVIEW_THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String){\n"
    "  repository(owner:$owner,name:$name){pullRequest(number:$number){\n"
    "    reviewThreads(first:100, after:$after){\n"
    "      pageInfo{hasNextPage endCursor}\n"
    "      nodes{isResolved isOutdated path line originalLine\n"
    "        comments(first:2){totalCount nodes{author{login} createdAt body}}}}}}}")

# A hundred pages is ten thousand threads; a server that hands out a
# next page forever is a fault, not a pull request worth waiting on.
MAX_PAGES = 100


class ThreadFetchError(Exception):
    """The threads could not be read.

    `detail` says what the run lost. One type, so every caller reads the
    same answer the same way.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def gh_json(args: list[str]) -> object:
    """One `gh api` call, parsed.

    A failure is an error, never an empty answer: threads that could not
    be read are threads nobody has answered.
    """
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"gh exited {proc.returncode}"
        raise ThreadFetchError(
            f"gh {args[1] if len(args) > 1 else 'api'} failed: {reason}")
    if not proc.stdout.strip():
        return []
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ThreadFetchError(f"gh did not return JSON: {exc}")


def dig(payload, *keys):
    """Walk a chain of JSON objects, answering None wherever the chain
    stops being one."""
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def fetch(repo: str, number) -> list[dict]:
    """Every review thread on the pull request, cursor followed to the end.

    A failed page is a failed fetch: "could not check the threads" is
    never "there was nothing to answer".
    """
    owner, _, name = repo.partition("/")
    threads: list[dict] = []
    cursor = None
    for _page in range(MAX_PAGES):
        args = ["api", "graphql", "-f", f"query={REVIEW_THREADS_QUERY}",
                "-F", f"owner={owner}", "-F", f"name={name}",
                "-F", f"number={number}"]
        if cursor is not None:
            args += ["-F", f"after={cursor}"]
        try:
            payload = gh_json(args)
        except ThreadFetchError as exc:
            # The endpoint is named `graphql`, which says nothing about
            # what the run lost; a reader needs to see the threads.
            raise ThreadFetchError(f"review threads: {exc}") from exc
        # GraphQL answers a failed query with HTTP 200 and an `errors`
        # list, so a broken query would otherwise read as no threads.
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list) and errors:
            message = (errors[0].get("message")
                       if isinstance(errors[0], dict) else "")
            raise ThreadFetchError(
                f"review threads: {message or 'the query failed'}")
        page = dig(payload, "data", "repository", "pullRequest",
                   "reviewThreads")
        if not isinstance(page, dict):
            raise ThreadFetchError(
                "review threads: no reviewThreads in the response")
        nodes = page.get("nodes")
        if not isinstance(nodes, list):
            raise ThreadFetchError("review threads: the page carried no nodes")
        threads += [n for n in nodes if isinstance(n, dict)]
        info = page.get("pageInfo")
        if not isinstance(info, dict):
            raise ThreadFetchError("review threads: the page carried no pageInfo")
        if not info.get("hasNextPage"):
            return threads
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise ThreadFetchError(
                "review threads: the next page came with no cursor")
    raise ThreadFetchError("review threads: pagination never ended")


def first_comment(thread: dict) -> dict:
    """The first comment in the thread, or an empty one."""
    comments = thread.get("comments")
    comments = comments if isinstance(comments, dict) else {}
    nodes = comments.get("nodes")
    nodes = nodes if isinstance(nodes, list) else []
    first = nodes[0] if nodes else None
    return first if isinstance(first, dict) else {}


def is_unanswered(thread: dict) -> bool:
    """A thread nobody has answered.

    A thread is answered when it is resolved or when somebody replied to
    it: the rule both callers use, so they agree about what is
    outstanding.
    """
    if not isinstance(thread, dict) or thread.get("isResolved"):
        return False
    comments = thread.get("comments")
    comments = comments if isinstance(comments, dict) else {}
    total = comments.get("totalCount")
    if not isinstance(total, int):
        total = len(comments.get("nodes") or [])
    return total <= 1


def location(thread: dict) -> str:
    """Where a thread sits, as path:line.

    An outdated thread's `line` is null, and it is named where it was
    written.
    """
    path = thread.get("path") or "?"
    line = thread.get("line")
    if not isinstance(line, int):
        line = thread.get("originalLine")
    if not isinstance(line, int):
        line = "?"
    return f"{path}:{line}"


def author(thread: dict) -> str:
    """Who started the thread."""
    user = first_comment(thread).get("author")
    login = user.get("login") if isinstance(user, dict) else None
    if isinstance(login, str) and login:
        return login
    return "?"
