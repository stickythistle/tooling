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
BASE = "5555555555555555555555555555555555555555"
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


def comment(cid, created, login, body="a comment", user_type="User"):
    return {
        "id": cid,
        "user": {"login": login, "type": user_type},
        "created_at": created,
        "body": body,
    }


def marker(review_id, head):
    return f"<!-- pr-loop:answer review={review_id} head={head} -->"


def status_marker(review_id, head):
    return f"<!-- pr-loop:status review={review_id} head={head} -->"


def check_run(status="COMPLETED", conclusion="SUCCESS", name="Gates"):
    return {"__typename": "CheckRun", "status": status,
            "conclusion": conclusion, "name": name}


# The patch a compare reports. A rebase shifts the hunk header's line
# numbers and changes nothing else.
PATCH_BODY = ("--- a/lib/x.py\n"
              "+++ b/lib/x.py\n"
              "@@ -10,3 +10,3 @@ def f():\n"
              "-old line\n"
              "+new line\n"
              " keep\n")


def compare_files(patch: str | None = PATCH_BODY, filename="lib/x.py"):
    entry = {"filename": filename, "status": "modified"}
    if patch is not None:
        entry["patch"] = patch
    return [entry]


def pr(state="open", merged=False, head=HEAD, draft=False,
       mergeable: bool | None = True, base=BASE):
    return {"state": state, "merged": merged, "draft": draft,
            "head": {"sha": head}, "base": {"sha": base},
            "mergeable": mergeable}


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
        "expected": "REVIEW " + HEAD + " " + HEAD,
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
        "expected": "REVIEW " + HEAD + " " + HEAD,
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
        "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                     f"{BASE}...{HEAD2}": compare_files(filename="lib/y.py")},
        "patch_equal": False,
        "expected": "REVIEW " + HEAD2 + " " + COMMIT_A,
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
        "expected": "REVIEW " + HEAD + " " + COMMIT_A,
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
        "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                     f"{BASE}...{HEAD}": compare_files(filename="lib/y.py")},
        "patch_equal": False,
        "expected": "REVIEW " + HEAD + " " + COMMIT_A,
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
    # An unlisted login's marker reads as a person speaking.
    "marked_post_untrusted": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", "a-person",
                    body=marker(701, HEAD)),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "implementers": [IMPLEMENTER],
        "expected": "REVIEW " + HEAD + " " + HEAD,
    },
    # The reviewer's own marker.
    "marked_post_by_reviewer": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", REVIEWER,
                    body=marker(701, HEAD)),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "implementers": [IMPLEMENTER],
        "expected": "REVIEW " + HEAD + " " + HEAD,
    },
    # An empty implementer set trusts nothing.
    "marked_post_no_implementers": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                    body=marker(701, COMMIT_A)),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1, COMMIT_A: TREE_1},
        "implementers": [],
        "expected": "REVIEW " + HEAD + " " + COMMIT_A,
    },
    # The same marked post from a listed implementer: no third-party
    # trip, so the squash shortcut stands.
    "trusted_marker_no_trip": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                    body=marker(701, COMMIT_A)),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1, COMMIT_A: TREE_1},
        "implementers": [IMPLEMENTER],
        "expected": "REAPPROVE " + HEAD,
    },
    # A failed round's summary proves the loop spoke without claiming an
    # answer, so the fix stays in hand.
    "failed_round_summary_only": {
        "pr": pr(),
        "reviews": [
            review(77, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                    body="Review round 1: no commit\n" + status_marker(77, HEAD)),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "implementers": [IMPLEMENTER],
        "expected": "FIX 77",
    },
    "draft_waits": {
        "pr": pr(draft=True),
        "reviews": [],
        "comments": [],
        "checks": [check_run()],
        "trees": {},
        "expected": "WAIT draft",
    },
    # A failed CheckRun alongside a successful one is a failure.
    "failed_check_run": {
        "pr": pr(),
        "reviews": [],
        "comments": [],
        "checks": [check_run(conclusion="SUCCESS"),
                   check_run(conclusion="FAILURE")],
        "trees": {},
        "expected": "FIX checks",
    },
    # A failed StatusContext is a failure.
    "failed_status_context": {
        "pr": pr(),
        "reviews": [],
        "comments": [],
        "checks": [{"__typename": "StatusContext", "state": "ERROR",
                    "context": "ci"}],
        "trees": {},
        "expected": "FIX checks",
    },
    # A cancelled run is no evidence against the branch: every push
    # cancels the run it supersedes.
    "cancelled_check_is_not_a_failure": {
        "pr": pr(),
        "reviews": [],
        "comments": [],
        "checks": [check_run(conclusion="CANCELLED")],
        "trees": {},
        "expected": "REVIEW " + HEAD,
    },
    # The base conflicts with the head.
    "mergeable_false_is_a_conflict": {
        "pr": pr(mergeable=False),
        "reviews": [],
        "comments": [],
        "checks": [check_run()],
        "trees": {},
        "expected": "FIX conflict",
    },
    # A null `mergeable` is unknown, not a conflict: GitHub returns it
    # for seconds after every push.
    "mergeable_null_is_not_a_conflict": {
        "pr": pr(mergeable=None),
        "reviews": [],
        "comments": [],
        "checks": [check_run()],
        "trees": {},
        "expected": "REVIEW " + HEAD,
    },
    # A rebase that leaves the patch equal carries the approval: the tree
    # moved, the work did not.
    "rebased_equal_patch_reapproves": {
        "pr": pr(),
        "reviews": [
            review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_2, COMMIT_A: TREE_1},
        "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                     f"{BASE}...{HEAD}": compare_files()},
        "patch_equal": True,
        "expected": "REAPPROVE " + HEAD,
    },
    # The same rebase, but the verdict asked for changes. The request
    # still stands, so the fix stays in hand.
    "rebased_equal_patch_keeps_the_fix": {
        "pr": pr(),
        "reviews": [
            review(801, "CHANGES_REQUESTED", COMMIT_A,
                   "2026-10-20T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_2, COMMIT_A: TREE_1},
        "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                     f"{BASE}...{HEAD}": compare_files()},
        "patch_equal": True,
        "expected": "FIX 801",
    },
    # A person objected since the verdict.
    "rebased_equal_patch_with_objection": {
        "pr": pr(),
        "reviews": [
            review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", "a-person"),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_2, COMMIT_A: TREE_1},
        "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                     f"{BASE}...{HEAD}": compare_files()},
        "patch_equal": True,
        "expected": "REVIEW " + HEAD + " " + COMMIT_A,
    },
    # A rebase that shifts every hunk header leaves the patch equal: the
    # line numbers say nothing about the work.
    "hunk_line_numbers_do_not_matter": {
        "pr": pr(),
        "reviews": [
            review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_2, COMMIT_A: TREE_1},
        "compares": {
            f"{BASE}...{COMMIT_A}": compare_files(),
            f"{BASE}...{HEAD}": compare_files(
                patch=PATCH_BODY.replace("@@ -10,3 +10,3 @@",
                                         "@@ -42,3 +42,3 @@")),
        },
        "patch_equal": True,
        "expected": "REAPPROVE " + HEAD,
    },
    # A file the API cannot show a patch for: not equal.
    "file_without_a_patch_is_not_equal": {
        "pr": pr(),
        "reviews": [
            review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [],
        "checks": [check_run()],
        "trees": {HEAD: TREE_2, COMMIT_A: TREE_1},
        "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                     f"{BASE}...{HEAD}": compare_files(patch=None)},
        "patch_equal": False,
        "expected": "REVIEW " + HEAD + " " + COMMIT_A,
    },
    # A bot's post is never a third party's: the approval carries.
    "bot_comment_does_not_interrupt": {
        "pr": pr(),
        "reviews": [
            review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            comment(1, "2026-10-20T01:00:00Z", "ci-bot",
                    body="the build finished", user_type="Bot"),
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_2, COMMIT_A: TREE_1},
        "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                     f"{BASE}...{HEAD}": compare_files()},
        "patch_equal": True,
        "expected": "REAPPROVE " + HEAD,
    },
    # A draft that has already been merged is still DONE merged: the
    # draft rule sits below the DONE rules.
    "draft_merged": {
        "pr": pr(state="closed", merged=True, draft=True),
        "reviews": [],
        "comments": [],
        "checks": [],
        "trees": {},
        "expected": "DONE merged",
    },
    # A draft with a failing check still waits: the draft rule is above
    # the check rules.
    "draft_beats_failed_checks": {
        "pr": pr(draft=True),
        "reviews": [],
        "comments": [],
        "checks": [check_run(conclusion="FAILURE")],
        "trees": {},
        "expected": "WAIT draft",
    },
    # SKIPPED, NEUTRAL and STARTUP_FAILURE are no evidence against the
    # branch either.
    "skipped_neutral_startup_are_not_failures": {
        "pr": pr(),
        "reviews": [],
        "comments": [],
        "checks": [check_run(conclusion="SKIPPED"),
                   check_run(conclusion="NEUTRAL"),
                   check_run(conclusion="STARTUP_FAILURE")],
        "trees": {},
        "expected": "REVIEW " + HEAD,
    },
    # No `user.type` at all.
    "comment_without_a_type_is_a_person": {
        "pr": pr(),
        "reviews": [
            review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
        ],
        "comments": [
            {"id": 1, "user": {"login": "someone-else"},
             "created_at": "2026-10-20T01:00:00Z", "body": "a comment"},
        ],
        "checks": [check_run()],
        "trees": {HEAD: TREE_1},
        "expected": "REVIEW " + HEAD + " " + HEAD,
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
            # The compare responses the gate reads, keyed by
            # "<base>...<commit>".
            "compares": data.get("compares", {}),
            # What the decision is told the patch comparison found.
            "patch_equal": data.get("patch_equal"),
            # The logins whose answer markers this scenario trusts. The
            # suite passes them as --implementer, exactly as the host
            # config would.
            "implementers": data.get("implementers", [IMPLEMENTER]),
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
