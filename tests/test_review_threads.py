#!/usr/bin/env python3
"""Pin the shared review-thread fetch.

Two tools ask the same question of a pull request: which review threads
carry a finding nobody has answered. `merge-check` refuses to merge while
one waits, and `pr-ready --teammate` refuses to mark a pull request ready
while the teammate's own finding waits. The fetch and the rule live in
`lib/review_threads.py`, so a fix to the paging or to the rule lands in
one place.

What is pinned here:

- the fetch follows the cursor to the last page;
- a failed page is a failed fetch: a GraphQL error, a response with no
  threads, a page with no nodes and a next page with no cursor are all
  refusals;
- a thread is answered when it is resolved or when somebody replied to
  it, and the rule reads the same in both callers;
- the query lives in one file, so a fix to it lands in one place.

No network: a stub gh answers every call.

Run directly: `python3 tests/test_review_threads.py`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import review_threads  # noqa: E402

REPO = "example-org/tooling"

STUB_GH = r'''#!/usr/bin/env python3
"""A stub gh: the GraphQL pages come from a file, and the cursor picks
the page."""
import json
import os
import sys

DATA = os.environ["STUB_DATA"]
ARGS = sys.argv[1:]

with open(os.path.join(DATA, "gh.log"), "a") as fh:
    fh.write(" ".join(ARGS) + "\n")

if ARGS[:2] == ["api", "graphql"]:
    canned = json.load(open(os.path.join(DATA, "gh.json")))
    pages = canned.get("pages") or []
    after = next((a.split("=", 1)[1] for a in ARGS
                  if a.startswith("after=")), None)
    index = 0 if after is None else int(after)
    if index >= len(pages):
        print("graphql: no page at cursor %r" % after, file=sys.stderr)
        sys.exit(1)
    print(json.dumps(pages[index]))
    sys.exit(0)

print("[]")
'''


def fail(failures: list[str], msg: str, detail: str = "") -> None:
    failures.append(f"{msg}: {detail}" if detail else msg)


def thread_node(path: str = "a.txt", line: int = 3, *, resolved: bool = False,
                outdated: bool = False, original_line: int | None = None,
                comments: int = 1, author: str = "lane",
                body: str = "") -> dict:
    """A `reviewThreads` node, as GraphQL reports it.

    `comments` is `comments.totalCount`: one is the finding alone, more
    is a finding with a reply. An outdated thread's `line` is null; its
    `originalLine` is where it was written.
    """
    nodes = [{"author": {"login": author}, "createdAt": "2026-01-02T00:00:00Z",
             "body": body}]
    if comments > 1:
        nodes.append({"author": {"login": "the-reviewer"},
                      "createdAt": "2026-01-03T00:00:00Z", "body": ""})
    if outdated and original_line is None:
        original_line = line
    return {"isResolved": resolved, "isOutdated": outdated, "path": path,
            "line": None if outdated else line,
            "originalLine": original_line,
            "comments": {"totalCount": comments, "nodes": nodes}}


def thread_page(nodes: list, *, next_cursor: str | None = None) -> dict:
    """One `reviewThreads` page: its nodes and the cursor to the next one."""
    return {"nodes": nodes,
            "pageInfo": {"hasNextPage": next_cursor is not None,
                         "endCursor": next_cursor}}


def graphql_page(nodes: list, *, next_cursor: str | None = None) -> dict:
    """One page as the API answers the query."""
    return {"data": {"repository": {"pullRequest": {
        "reviewThreads": thread_page(nodes, next_cursor=next_cursor)}}}}


def write_stub(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(STUB_GH)
    gh.chmod(0o755)


def run_fetch(data_dir: Path, bin_dir: Path, repo: str = REPO,
              number: str = "5") -> tuple[list[dict], str]:
    """Fetch the threads with the stub gh on PATH.

    Returns the threads, or the refusal's message. The stub is found
    through the environment, because that is how the fetch reaches `gh`.
    """
    saved = {key: os.environ.get(key) for key in ("PATH", "STUB_DATA")}
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    os.environ["STUB_DATA"] = str(data_dir)
    try:
        return review_threads.fetch(repo, number), ""
    except review_threads.ThreadFetchError as exc:
        return [], exc.detail
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def case_the_fetch_follows_the_cursor(failures: list[str]) -> None:
    # Two pages, and the second is asked for with the cursor the first
    # handed out.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        write_stub(bin_dir)
        data = tmp / "data"
        data.mkdir()
        (data / "gh.json").write_text(json.dumps({"pages": [
            graphql_page([thread_node("a.txt", 3)], next_cursor="1"),
            graphql_page([thread_node("b.txt", 9)]),
        ]}))
        threads, refusal = run_fetch(data, bin_dir)
        if refusal:
            fail(failures, "paging: the fetch refused", refusal)
            return
        paths = [t.get("path") for t in threads]
        if paths != ["a.txt", "b.txt"]:
            fail(failures, "paging: the threads", str(paths))
        log = (data / "gh.log").read_text()
        if "after=1" not in log:
            fail(failures, "paging: the second page was not asked for with "
                 "the cursor", log)


def case_a_graphql_error_is_a_refusal(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        write_stub(bin_dir)
        data = tmp / "data"
        data.mkdir()
        (data / "gh.json").write_text(json.dumps({"pages": [
            {"errors": [{"message": "Field 'reviewThreads' not found"}]},
        ]}))
        threads, refusal = run_fetch(data, bin_dir)
        if threads:
            fail(failures, "graphql error: the fetch answered", str(threads))
        if "not found" not in refusal:
            fail(failures, "graphql error: the refusal does not carry the "
                 "message", refusal)


def case_a_page_without_threads_is_a_refusal(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        write_stub(bin_dir)
        data = tmp / "data"
        data.mkdir()
        (data / "gh.json").write_text(json.dumps({"pages": [{"data": {}}]}))
        threads, refusal = run_fetch(data, bin_dir)
        if threads:
            fail(failures, "no threads: the fetch answered", str(threads))
        if not refusal:
            fail(failures, "no threads: the fetch answered with nothing")


def case_a_page_without_nodes_is_a_refusal(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        write_stub(bin_dir)
        data = tmp / "data"
        data.mkdir()
        (data / "gh.json").write_text(json.dumps({"pages": [
            {"data": {"repository": {"pullRequest": {"reviewThreads": {}}}}},
        ]}))
        threads, refusal = run_fetch(data, bin_dir)
        if threads:
            fail(failures, "no nodes: the fetch answered", str(threads))
        if not refusal:
            fail(failures, "no nodes: the fetch answered with nothing")


def case_a_next_page_without_a_cursor_is_a_refusal(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        write_stub(bin_dir)
        data = tmp / "data"
        data.mkdir()
        (data / "gh.json").write_text(json.dumps({"pages": [
            {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [], "pageInfo": {"hasNextPage": True,
                                          "endCursor": None}}}}}},
        ]}))
        threads, refusal = run_fetch(data, bin_dir)
        if threads:
            fail(failures, "no cursor: the fetch answered", str(threads))
        if not refusal:
            fail(failures, "no cursor: the fetch answered with nothing")


def case_an_unresolved_thread_is_unanswered(failures: list[str]) -> None:
    if not review_threads.is_unanswered(thread_node()):
        fail(failures, "unanswered: a thread with one comment was answered")


def case_a_replied_thread_is_answered(failures: list[str]) -> None:
    # The thread is still open, and an open thread somebody answered asks
    # nothing of the merge.
    if review_threads.is_unanswered(thread_node(comments=2)):
        fail(failures, "answered: a thread with a reply was unanswered")


def case_a_resolved_thread_is_answered(failures: list[str]) -> None:
    if review_threads.is_unanswered(thread_node(resolved=True)):
        fail(failures, "resolved: a resolved thread was unanswered")


def case_the_location_names_the_line(failures: list[str]) -> None:
    if review_threads.location(thread_node("a.txt", 3)) != "a.txt:3":
        fail(failures, "location: the place",
             review_threads.location(thread_node("a.txt", 3)))
    # The code moved out from under the comment, so `line` is null, and
    # the refusal names where it was written.
    moved = thread_node("b.txt", 9, outdated=True, original_line=12)
    if review_threads.location(moved) != "b.txt:12":
        fail(failures, "location: an outdated thread",
             review_threads.location(moved))


def case_the_author_names_who_started_the_thread(failures: list[str]) -> None:
    if review_threads.author(thread_node(author="lane")) != "lane":
        fail(failures, "author: the login",
             review_threads.author(thread_node(author="lane")))
    if review_threads.author({"path": "a.txt"}) != "?":
        fail(failures, "author: a thread with no comment",
             review_threads.author({"path": "a.txt"}))


def case_the_query_lives_in_one_file(failures: list[str]) -> None:
    # Two copies of one query drift: the next fix to the paging lands in
    # one of them. The rule is read the same way: the callers ask the
    # shared module whether a thread is answered.
    shared = (REPO_ROOT / "lib" / "review_threads.py").read_text()
    if "reviewThreads" not in shared:
        fail(failures, "the query: the shared module holds no copy")
    if "totalCount" not in shared:
        fail(failures, "the rule: the shared module holds no copy")
    for name in ("bin/merge-check", "lib/teammate.py", "lib/merge_check.py"):
        text = (REPO_ROOT / name).read_text()
        for marker in ("reviewThreads", "totalCount"):
            if marker in text:
                fail(failures, f"the query: {name} holds a copy of {marker}")


CASES = (
    case_the_fetch_follows_the_cursor,
    case_a_graphql_error_is_a_refusal,
    case_a_page_without_threads_is_a_refusal,
    case_a_page_without_nodes_is_a_refusal,
    case_a_next_page_without_a_cursor_is_a_refusal,
    case_an_unresolved_thread_is_unanswered,
    case_a_replied_thread_is_answered,
    case_a_resolved_thread_is_answered,
    case_the_location_names_the_line,
    case_the_author_names_who_started_the_thread,
    case_the_query_lives_in_one_file,
)


def main() -> int:
    failures: list[str] = []
    for case in CASES:
        try:
            case(failures)
        except Exception as exc:  # noqa: BLE001
            fail(failures, f"{case.__name__} raised",
                 f"{type(exc).__name__}: {exc}")
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS the review threads: one fetch, one rule, one query; a "
          "failed page is a failed fetch; a thread is answered when it is "
          "resolved or somebody replied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
