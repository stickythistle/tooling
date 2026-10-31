#!/usr/bin/env python3
"""Write the canonical pr_gate fixtures under tests/fixtures/pr_gate/.

These files are the suite's in-code scenarios written out, so the JSON the
suite feeds the decision is on disk where a human can read it.

Run: python3 tests/fixtures/pr_gate/build.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

REVIEWER = "stickythistle"
IMPLEMENTER = "harbord-st"

HEAD = "1111111111111111111111111111111111111111"
HEAD2 = "2222222222222222222222222222222222222222"
COMMIT_A = "3333333333333333333333333333333333333333"
COMMIT_B = "4444444444444444444444444444444444444444"
TREE_1 = "aaaaaaaa00000000000000000000000000000000"
TREE_2 = "bbbbbbbb00000000000000000000000000000000"


def review(rid, state, commit, submitted, login=REVIEWER, body="a review"):
    return {
        "id": rid,
        "user": {"login": login, "type": "User"},
        "state": state,
        "commit_id": commit,
        "submitted_at": submitted,
        "body": body,
    }


def comment(cid, created, login, body="a comment"):
    return {
        "id": cid,
        "user": {"login": login, "type": "User"},
        "created_at": created,
        "body": body,
    }


def marker(review_id, head):
    return f"<!-- pr-loop:answer review={review_id} head={head} -->"


def check_run(status="COMPLETED", conclusion="SUCCESS", name="Gates"):
    return {"__typename": "CheckRun", "status": status,
            "conclusion": conclusion, "name": name}


def pr(state="open", merged=False, head=HEAD):
    return {"state": state, "merged": merged, "draft": False,
            "head": {"sha": head}, "base": {"sha": COMMIT_B},
            "mergeable": "MERGEABLE"}


# The four shapes the issue names, plus the ones the rules need.
FIXTURES = {
    # A thread reply arrives as an empty-body COMMENTED review after a
    # CHANGES_REQUESTED. A thread reply is no verdict, so the round stays
    # open.
    "thread_reply_empty_body": {
        "pr": pr(),
        "reviews": [
            review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z"),
            review(802, "COMMENTED", HEAD, "2026-10-20T01:00:00Z", body=""),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "FIX 801",
    },
    # A review and a marked reply land in the same poll. The reply answers
    # the review, so the round needs a ruling.
    "review_and_reply_same_poll": {
        "pr": pr(),
        "reviews": [
            review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                    body="answering\n" + marker(801, HEAD)),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "REVIEW " + HEAD,
    },
    # person_comment_after_approval
    "person_comment_after_approval": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", "a-person"),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "REVIEW " + HEAD,
    },
    # The head moved while a review was in flight. The verdict now says
    # nothing about the head.
    "head_moved": {
        "pr": pr(head=HEAD2),
        "reviews": [
            review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD2: TREE_2, COMMIT_A: TREE_1},
        "expected": "REVIEW " + HEAD2,
    },
    # The squash case: the branch was rewritten, the approval was
    # dismissed, and the work did not change.
    "squash_reapprove": {
        "pr": pr(),
        "reviews": [
            review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1, COMMIT_A: TREE_1},
        "expected": "REAPPROVE " + HEAD,
    },
    # A person's objection, then a squash that leaves the tree
    # unchanged. The approval at B is the verdict, and the objection
    # after it needs a ruling -- so the squash shortcut must not
    # re-approve the head without one.
    "squash_after_objection": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", "a-person",
                    body="this drops the retry, please keep it"),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1, COMMIT_A: TREE_1},
        "expected": "REVIEW " + HEAD,
    },
    # The caller's own unsubmitted review, alongside a verdict. The
    # pending review carries no submitted_at and is dropped.
    "pending_review_alongside_approval": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
            {
                "id": 999,
                "user": {"login": IMPLEMENTER, "type": "User"},
                "state": "PENDING",
                "commit_id": HEAD,
                "submitted_at": None,
                "body": "a draft nobody has submitted",
            },
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "AGREED " + HEAD,
    },
    # A stale approval whose commit's tree really did change.
    "stale_changed_tree": {
        "pr": pr(),
        "reviews": [
            review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_2, COMMIT_A: TREE_1},
        "expected": "REVIEW " + HEAD,
    },
    # A marker naming an older review leaves the round in hand open.
    "marker_names_older": {
        "pr": pr(),
        "reviews": [
            review(800, "CHANGES_REQUESTED", COMMIT_A, "2026-10-19T00:00:00Z"),
            review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                    body=marker(800, HEAD)),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "FIX 801",
    },
    # A DISMISSED review newer than the approval. A dismissed one is no
    # verdict, so V is still the approval.
    "dismissed_newer": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
            review(702, "DISMISSED", HEAD, "2026-10-20T03:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "AGREED " + HEAD,
    },
    # paged_reviews
    "paged_reviews": {
        "pr": pr(),
        "reviews_pages": [
            [review(700, "COMMENTED", HEAD, "2026-10-19T00:00:00Z", body="")],
            [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "FIX 801",
    },
    # Four rounds of changes requested.
    "max_rounds": {
        "pr": pr(),
        "reviews": [
            review(1, "CHANGES_REQUESTED", HEAD, "2026-10-16T00:00:00Z"),
            review(2, "CHANGES_REQUESTED", HEAD, "2026-10-17T00:00:00Z"),
            review(3, "CHANGES_REQUESTED", HEAD, "2026-10-18T00:00:00Z"),
            review(4, "CHANGES_REQUESTED", HEAD, "2026-10-19T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "ESCALATE max-rounds",
    },
    # A check that has not finished.
    "checks_running": {
        "pr": pr(),
        "reviews": [],
        "comments": [],
        "checks": [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                    "conclusion": None, "name": "Gates"}],
        "trees": {},
        "expected": "WAIT checks",
    },
}


def main() -> int:
    HERE.mkdir(parents=True, exist_ok=True)
    for name, data in FIXTURES.items():
        fixture = {
            "pr": data["pr"],
            # A scenario whose reviews are split over two pages records the
            # pages; one page's worth records a flat list. The suite reads
            # whichever the fixture carries.
            "reviews": data.get("reviews", []),
            "comments": data["comments"],
            "checks": data["checks"],
            "trees": data["trees"],
            "expected": data["expected"],
        }
        if "reviews_pages" in data:
            fixture["reviews_pages"] = data["reviews_pages"]
        path = HERE / f"{name}.json"
        path.write_text(json.dumps(fixture, indent=2) + "\n")
        print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
