#!/usr/bin/env python3
"""Pin what pr-loop-gate decides, from GitHub data alone.

Every scenario is asserted through `lib/pr_gate.decide` and once end to
end through `bin/pr-loop-gate` with a stub `gh` on PATH. No network.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "bin" / "pr-loop-gate"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pr_gate"

sys.path.insert(0, str(REPO_ROOT / "lib"))
import pr_gate  # noqa: E402

REVIEWER = "stickythistle"
IMPLEMENTER = "harbord-st"

HEAD = "1111111111111111111111111111111111111111"
HEAD2 = "2222222222222222222222222222222222222222"
COMMIT_A = "3333333333333333333333333333333333333333"
COMMIT_B = "4444444444444444444444444444444444444444"
TREE_1 = "aaaaaaaa00000000000000000000000000000000"
TREE_2 = "bbbbbbbb00000000000000000000000000000000000"
BASE = "5555555555555555555555555555555555555555"

# The patch a compare reports for a commit.
PATCH_BODY = ("--- a/lib/x.py\n"
              "+++ b/lib/x.py\n"
              "@@ -10,3 +10,3 @@ def f():\n"
              "-old line\n"
              "+new line\n"
              " keep\n")


def compare_file(filename="lib/x.py", status="modified",
                 patch: str | None = PATCH_BODY):
    """Return one file of a compare response."""
    entry = {"filename": filename, "status": status}
    if patch is not None:
        entry["patch"] = patch
    return entry


def compare_files(patch: str | None = PATCH_BODY, filename="lib/x.py"):
    return [compare_file(filename=filename, patch=patch)]


# ----- fixture builders ---------------------------------------------------

def review(rid, state, commit, submitted, login=REVIEWER, body="a review"):
    return {
        "id": rid,
        "user": {"login": login, "type": "User"},
        "state": state,
        "commit_id": commit,
        "submitted_at": submitted,
        "body": body,
    }


def comment(cid, created, login, body="a comment", in_reply_to=None,
            user_type="User"):
    entry = {
        "id": cid,
        "user": {"login": login, "type": user_type},
        "created_at": created,
        "body": body,
    }
    if in_reply_to is not None:
        entry["in_reply_to_id"] = in_reply_to
    return entry


def marker(review_id, head):
    return f"<!-- pr-loop:answer review={review_id} head={head} -->"


def status_marker(review_id, head):
    return f"<!-- pr-loop:status review={review_id} head={head} -->"


def check_run(status: str = "COMPLETED", conclusion=None,
             name: str = "Gates"):
    return {"__typename": "CheckRun", "status": status,
            "conclusion": conclusion, "name": name}


def status_context(state: str = "SUCCESS", name: str = "ci"):
    return {"__typename": "StatusContext", "state": state, "context": name}


def pr(state="open", merged=False, head=HEAD, draft=False,
       mergeable: bool | None = True, base=BASE):
    return {"state": state, "merged": merged, "draft": draft,
            "head": {"sha": head}, "base": {"sha": base},
            "mergeable": mergeable}


# ----- scenario table -----------------------------------------------------
#
# Each scenario is (name, pr, reviews, comments, checks, trees, expected,
# extra). `extra` carries:
#
#   patch_equal  what decide() is told about the patch comparison
#   compares     the compare responses the stub gh serves, keyed by
#                "<base>...<commit>"


def scenarios():
    out = []

    def add(name, pr_obj, reviews, comments, checks, trees, expected,
            extra=None):
        out.append((name, pr_obj, reviews, comments, checks, trees,
                    expected, extra or {}))

    # Row 1: merged.
    add("merged", pr(state="closed", merged=True),
                [], [], [], {}, "DONE merged")

    # Row 2: closed without a merge.
    add("closed", pr(state="closed"),
                [], [], [], {}, "DONE closed")

    # Row 3a: a CheckRun that has not completed.
    add("checks_running", pr(),
                [], [], [check_run(status="IN_PROGRESS", conclusion=None)],
                {}, "WAIT checks")

    # Row 3b: a StatusContext that is still pending.
    add("status_pending", pr(),
                [], [], [status_context(state="PENDING")],
                {}, "WAIT checks")

    # Row 3c: an EXPECTED status context is also still running.
    add("status_expected", pr(),
                [], [], [status_context(state="EXPECTED")],
                {}, "WAIT checks")

    # Row 3d: an empty rollup is WAIT never.
    add("no_checks", pr(),
                [], [], [], {}, "REVIEW " + HEAD)

    # Row 4: the reviewer has never ruled.
    add("no_verdict", pr(),
                [], [], [check_run()], {}, "REVIEW " + HEAD)

    # Row 4b: only COMMENTED reviews by the reviewer. A thread reply is
    # no verdict, so this is still "no verdict".
    add("only_commented", pr(),
                [review(900, "COMMENTED", HEAD, "2026-10-20T00:00:00Z", body="")],
                [], [check_run()], {}, "REVIEW " + HEAD)

    # Row 5: the verdict is stale but the tree is unchanged -- the squash
    # case.
    add("reapprove", pr(),
                [review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()],
                {HEAD: TREE_1, COMMIT_A: TREE_1}, "REAPPROVE " + HEAD)

    # Row 5, with an objection. The reviewer approves at B, a person
    # objects, and the lane then squashes to H with the same tree, so the
    # shortcut must not re-approve H over an unruled objection.
    add("squash_after_objection", pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "a-person",
                         body="this drops the retry, please keep it")],
                [check_run()],
                {HEAD: TREE_1, COMMIT_A: TREE_1},
                "REVIEW " + HEAD + " " + COMMIT_A)

    # The objection is the only thing that changes, so the two together
    # pin that the guard is on the objection, not on the shape.
    add("squash_without_objection", pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()],
                {HEAD: TREE_1, COMMIT_A: TREE_1}, "REAPPROVE " + HEAD)

    # The objection before the verdict is part of the round the verdict
    # already answered, so it leaves the shortcut open.
    add("squash_objection_before_verdict", pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T02:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "a-person")],
                [check_run()],
                {HEAD: TREE_1, COMMIT_A: TREE_1}, "REAPPROVE " + HEAD)

    # The caller's own unsubmitted review. `GET pulls/N/reviews` returns
    # it with no `submitted_at`, and it must not fail the run.
    add("pending_review_alongside_approval", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
                 {"id": 999, "user": {"login": IMPLEMENTER, "type": "User"},
                  "state": "PENDING", "commit_id": HEAD, "submitted_at": None,
                  "body": "a draft nobody has submitted"}],
                [], [check_run()], {HEAD: TREE_1}, "AGREED " + HEAD)

    # Row 6: the verdict is stale and the work really did change.
    #
    # An unknown tree is a state the gate cannot reach: a failed lookup
    # is a hard error.
    add("stale_changed_tree", pr(),
                [review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()],
                {HEAD: TREE_2, COMMIT_A: TREE_1},
                "REVIEW " + HEAD + " " + COMMIT_A,
                {"patch_equal": False,
                 "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                              f"{BASE}...{HEAD}": compare_files(
                                  filename="lib/y.py")}})

    # Row 7: a person comments after the approval.
    add("third_party_after_approval", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "someone-else")],
                [check_run()], {},
                "REVIEW " + HEAD + " " + HEAD)

    # Row 7b: the third party's post is before the verdict, so it is part
    # of the round the verdict already answered.
    add("third_party_before", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T02:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "someone-else")],
                [check_run()], {}, "AGREED " + HEAD)

    # Row 7c: the reviewer's own post after the verdict is no third
    # party.
    add("reviewer_post_after", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", REVIEWER)],
                [check_run()], {}, "AGREED " + HEAD)

    # Row 7d: a marked post from a listed implementer is the loop's own
    # reply, never a third party's comment.
    add("marked_post_is_ours", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body=marker(701, HEAD))],
                [check_run()], {}, "AGREED " + HEAD)

    # A marker's format proves nothing: from a login outside the
    # implementer set it reads as a person speaking, and rule 7 asks for
    # a ruling.
    add("marked_post_untrusted", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "someone-else",
                         body=marker(701, HEAD))],
                [check_run()], {},
                "REVIEW " + HEAD + " " + HEAD)

    # The reviewer wearing the loop's marker is a third party too.
    add("marked_post_by_reviewer", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", REVIEWER,
                         body=marker(701, HEAD))],
                [check_run()], {},
                "REVIEW " + HEAD + " " + HEAD)

    # No implementers configured: no marker is trusted.
    add("marked_post_no_implementers", pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body=marker(701, COMMIT_A))],
                [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1},
                "REVIEW " + HEAD + " " + COMMIT_A)

    # The trusted twin of the three above: the same stale approval, the
    # same marked post, now from a listed implementer. No third-party
    # trip, so the squash shortcut stands.
    add("trusted_marker_no_trip", pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body=marker(701, COMMIT_A))],
                [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1},
                "REAPPROVE " + HEAD)

    # Logins are case-insensitive on GitHub: a configured implementer in
    # a different case still matches the API login.
    add("implementer_case_differs", pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body=marker(701, COMMIT_A))],
                [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1},
                "REAPPROVE " + HEAD)

    # Row 8: approved on the head.
    add("agreed", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [], [check_run()], {}, "AGREED " + HEAD)

    # Row 9: changes requested, and the implementer answered with a
    # marker naming that review, without a new commit. The round's
    # summary (status marker) rides along; only the answer counts.
    add("answered_without_commit", pr(),
                [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body="here is the fix\n" + marker(801, HEAD)),
                 comment(2, "2026-10-20T01:00:01Z", IMPLEMENTER,
                         body="Review round 1: answered\n"
                              + status_marker(801, HEAD))],
                [check_run()], {},
                "REVIEW " + HEAD + " " + HEAD)

    # A failed round: its summary is the loop's only post. The status
    # marker keeps it from reading as a person speaking, and claims no
    # answer, so the fix stays in hand.
    add("failed_round_summary_only", pr(),
                [review(77, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body="Review round 1: no commit\n"
                              + status_marker(77, HEAD))],
                [check_run()], {}, "FIX 77")

    # A status marker from outside the implementer set is a forgery like
    # any other: rule 7 asks for a ruling.
    add("status_marker_untrusted", pr(),
                [review(77, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "someone-else",
                         body=status_marker(77, HEAD))],
                [check_run()], {},
                "REVIEW " + HEAD + " " + HEAD)

    # Row 9b: marker_names_older
    add("marker_names_older", pr(),
                [review(800, "CHANGES_REQUESTED", COMMIT_A,
                        "2026-10-19T00:00:00Z"),
                 review(801, "CHANGES_REQUESTED", HEAD,
                        "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body=marker(800, HEAD))],
                [check_run()], {}, "FIX 801")

    # Row 10: max_rounds
    add("max_rounds", pr(),
                [review(1, "CHANGES_REQUESTED", HEAD, "2026-10-16T00:00:00Z"),
                 review(2, "CHANGES_REQUESTED", HEAD, "2026-10-17T00:00:00Z"),
                 review(3, "CHANGES_REQUESTED", HEAD, "2026-10-18T00:00:00Z"),
                 review(4, "CHANGES_REQUESTED", HEAD, "2026-10-19T00:00:00Z")],
                [], [check_run()], {}, "ESCALATE max-rounds")

    # Row 10b: one round short of the limit. Still a fix.
    add("under_max_rounds", pr(),
                [review(1, "CHANGES_REQUESTED", HEAD, "2026-10-16T00:00:00Z"),
                 review(2, "CHANGES_REQUESTED", HEAD, "2026-10-17T00:00:00Z"),
                 review(3, "CHANGES_REQUESTED", HEAD, "2026-10-18T00:00:00Z")],
                [], [check_run()], {}, "FIX 3")

    # Row 11: changes requested, unanswered.
    add("fix", pr(),
                [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
                [], [check_run()], {}, "FIX 801")

    # Row 11b: dismissed_newer
    add("dismissed_newer", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
                 review(702, "DISMISSED", HEAD, "2026-10-20T03:00:00Z")],
                [], [check_run()], {}, "AGREED " + HEAD)

    # Row 11c: a COMMENTED review newer than the changes-requested one.
    # A thread reply is no verdict.
    add("commented_newer", pr(),
                [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z"),
                 review(802, "COMMENTED", HEAD, "2026-10-20T02:00:00Z", body="")],
                [], [check_run()], {}, "FIX 801")

    # The issue's four fixtures, each recorded on disk under
    # tests/fixtures/pr_gate/ and described there.

    # (a) thread_reply_empty_body
    add("thread_reply_empty_body", pr(),
                [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z"),
                 review(802, "COMMENTED", HEAD, "2026-10-20T01:00:00Z", body="")],
                [], [check_run()], {}, "FIX 801")

    # (b) review_and_reply_same_poll
    add("review_and_reply_same_poll", pr(),
                [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", IMPLEMENTER,
                         body="answering\n" + marker(801, HEAD))],
                [check_run()], {},
                "REVIEW " + HEAD + " " + HEAD)

    # (c) person_comment_after_approval
    add("person_comment_after_approval", pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "a-person")],
                [check_run()], {},
                "REVIEW " + HEAD + " " + HEAD)

    # (d) head_moved
    add("head_moved", pr(head=HEAD2),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()],
                {HEAD2: TREE_2, COMMIT_A: TREE_1},
                "REVIEW " + HEAD2 + " " + COMMIT_A,
                {"patch_equal": False,
                 "compares": {f"{BASE}...{COMMIT_A}": compare_files(),
                              f"{BASE}...{HEAD2}": compare_files(
                                  filename="lib/y.py")}})

    # The rules this change adds are recorded as fixtures under
    # tests/fixtures/pr_gate/, which the suite reads below.

    return out


# Which implementers each scenario configures. Scenarios not named here
# run with the one implementer listed; `marked_post_no_implementers`
# runs with none. The names pick the outcome; the rest is shared setup.
def implementers_for(name):
    if name == "marked_post_no_implementers":
        return []
    if name == "implementer_case_differs":
        # The configured login differs from the API login only in case.
        return ["Harbord-St"]
    return [IMPLEMENTER]


# ----- the stub gh --------------------------------------------------------

STUB = r"""#!/usr/bin/env bash
# A stub `gh` that answers from files under $STUB_DATA, so the test sets
# the canned JSON on disk rather than in a here-string the script only
# sees once.
printf '%s\n' "$*" >> "$STUB_DATA/gh.log"
case "$1" in
  api)
    shift
    endpoint=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --paginate|--slurp) shift ;;
        -*) shift ;;
        *) endpoint="$1"; shift ;;
      esac
    done
    # A scenario can ask an endpoint to fail by creating
    # $STUB_DATA/gh_fail_<name>.
    case "$endpoint" in
      */pulls/*/reviews)   name=reviews ;;
      */issues/*/comments) name=issue_comments ;;
      */pulls/*/comments)  name=pull_comments ;;
      */git/commits/*)     name=tree ;;
      */compare/*)         name=compare ;;
      */pulls/*)           name=pr ;;
      *) name=other ;;
    esac
    if [ -e "$STUB_DATA/gh_fail_$name" ]; then
      echo "gh: simulated failure for $endpoint" >&2
      exit 1
    fi
    case "$endpoint" in
      */git/commits/*)
        sha="${endpoint##*/}"
        cat "$STUB_DATA/trees.json" 2>/dev/null \
          | python3 -c 'import json,sys
trees = json.load(sys.stdin)
sha = sys.argv[1]
if sha not in trees:
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"tree": {"sha": trees[sha]}}))' "$sha"
        exit $?
        ;;
      */compare/*)
        # The compare is keyed by "<base>...<commit>".
        pair="${endpoint##*/compare/}"
        cat "$STUB_DATA/compares.json" 2>/dev/null \
          | python3 -c 'import json,sys
compares = json.load(sys.stdin)
pair = sys.argv[1]
if pair not in compares:
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"files": compares[pair]}))' "$pair"
        exit $?
        ;;
      */pulls/*/reviews)   file=reviews.json ;;
      */issues/*/comments) file=issue_comments.json ;;
      */pulls/*/comments)  file=pull_comments.json ;;
      */pulls/*)           file=pr.json ;;
      *) echo "gh: unexpected endpoint $endpoint" >&2; exit 1 ;;
    esac
    cat "$STUB_DATA/$file"
    ;;
  pr)
    case "$2" in
      view)
        if [ -e "$STUB_DATA/gh_fail_checks" ]; then
          echo "gh: simulated failure for statusCheckRollup" >&2
          exit 1
        fi
        cat "$STUB_DATA/checks.json"
        ;;
      *) echo "gh: unexpected pr subcommand $2" >&2; exit 1 ;;
    esac
    ;;
  *) echo "gh: unexpected command $1" >&2; exit 1 ;;
esac
"""


def write_stub_gh(bin_dir: Path) -> None:
    gh = bin_dir / "gh"
    gh.write_text(STUB)
    gh.chmod(0o755)


def install(data_dir: Path, pr_obj, reviews, comments, checks, trees,
            compares=None) -> None:
    """Write the scenario's canned JSON into the data dir.

    The reviews are written as the array of pages `--slurp` returns, so the
    fetcher's flattening is exercised. The compares are keyed by
    "<base>...<commit>".
    """
    (data_dir / "pr.json").write_text(json.dumps(pr_obj))
    (data_dir / "reviews.json").write_text(json.dumps([reviews]))
    (data_dir / "issue_comments.json").write_text(json.dumps([comments]))
    (data_dir / "pull_comments.json").write_text(json.dumps([[]]))
    (data_dir / "checks.json").write_text(
        json.dumps({"statusCheckRollup": checks}))
    (data_dir / "trees.json").write_text(json.dumps(trees))
    (data_dir / "compares.json").write_text(json.dumps(compares or {}))


def run_gate(data_dir: Path, bin_dir: Path, *extra: str,
             reviewer: str | None = REVIEWER,
             implementers: list | None = None,
             env_extra: dict | None = None):
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_DATA"] = str(data_dir)
    env.pop("REVIEWER_LOGIN", None)
    env.pop("IMPLEMENTER_LOGINS", None)
    if reviewer is not None:
        env["REVIEWER_LOGIN"] = reviewer
    if implementers is not None:
        extra = (*extra, *(a for login in implementers
                           for a in ("--implementer", login)))
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(GATE), "owner/repo", "42", *extra],
        capture_output=True, text=True, env=env,
    )


# The substring each failing endpoint must be named by in the error. The
# gate names the endpoint it was asking for, so a failure says what could
# not be read.
ENDPOINT_NAMES = {
    "reviews": "pulls/42/reviews",
    "issue_comments": "issues/42/comments",
    "pull_comments": "pulls/42/comments",
    "pr": "pulls/42",
    "checks": "statusCheckRollup",
    "tree": "git/commits/",
}


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        write_stub_gh(bin_dir)

        for name, pr_obj, reviews, comments, checks, trees, expected, extra \
                in scenarios():
            implementers = implementers_for(name)
            patch_equal = extra.get("patch_equal")
            compares = extra.get("compares")
            # 1. Through the pure function.
            try:
                got = pr_gate.decide(pr_obj, list(reviews), list(comments),
                                     list(checks), dict(trees), REVIEWER, 4,
                                     frozenset(implementers), patch_equal)
            except Exception as exc:  # noqa: BLE001 - the point is to report it
                got = f"<raised {exc!r}>"
            if got != expected:
                failures.append(
                    f"{name}: decide() returned {got!r}, expected {expected!r}")

            # 2. End to end through bin/pr-loop-gate with the stub gh.
            data = tmp / f"data_{name}"
            data.mkdir()
            install(data, pr_obj, list(reviews), list(comments),
                    list(checks), dict(trees), compares)
            r = run_gate(data, bin_dir, implementers=implementers)
            if r.returncode != 0:
                failures.append(
                    f"{name}: gate exited {r.returncode}: "
                    f"stderr={r.stderr.strip()!r} stdout={r.stdout.strip()!r}")
            else:
                line = r.stdout.strip()
                if line != expected:
                    failures.append(
                        f"{name}: gate printed {line!r}, expected {expected!r}")
                if r.stdout.count("\n") != 1:
                    failures.append(
                        f"{name}: gate printed {r.stdout.count(chr(10))} "
                        f"newlines, expected exactly one line: {r.stdout!r}")

        # ---- the recorded fixtures under tests/fixtures/pr_gate/ -------
        #
        # The same shapes written to disk. Each is asserted through
        # decide() and end to end, against the fixture's own `expected`
        # line.

        for path in sorted((REPO_ROOT / "tests" / "fixtures" / "pr_gate").glob("*.json")):
            fixture = json.loads(path.read_text())
            reviews = fixture.get("reviews", [])
            if "reviews_pages" in fixture:
                reviews = [r for page in fixture["reviews_pages"] for r in page]
            expected = fixture["expected"]
            fixture_implementers = fixture.get("implementers", [IMPLEMENTER])
            compares = fixture.get("compares")
            patch_equal = fixture.get("patch_equal")

            try:
                got = pr_gate.decide(fixture["pr"], list(reviews),
                                     list(fixture["comments"]),
                                     list(fixture["checks"]),
                                     dict(fixture["trees"]), REVIEWER, 4,
                                     frozenset(fixture_implementers),
                                     patch_equal)
            except pr_gate.Malformed as exc:
                got = f"<malformed: {exc}>"
            if got != expected:
                failures.append(
                    f"{path.name}: decide() returned {got!r}, expected {expected!r}")

            data = tmp / f"data_fixture_{path.stem}"
            data.mkdir()
            install(data, fixture["pr"], list(reviews),
                    list(fixture["comments"]), list(fixture["checks"]),
                    dict(fixture["trees"]), compares)
            if "reviews_pages" in fixture:
                (data / "reviews.json").write_text(
                    json.dumps(fixture["reviews_pages"]))
            r = run_gate(data, bin_dir, implementers=fixture_implementers)
            if r.returncode != 0:
                failures.append(
                    f"{path.name}: gate exited {r.returncode}: "
                    f"stderr={r.stderr.strip()!r}")
            elif r.stdout.strip() != expected:
                failures.append(
                    f"{path.name}: gate printed {r.stdout.strip()!r}, "
                    f"expected {expected!r}")

        # ---- the scenarios that need their own setup ------------------

        # paged_reviews
        data = tmp / "data_paged_reviews"
        data.mkdir()
        install(data, pr(),
                [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
                [], [check_run()], {})
        (data / "reviews.json").write_text(json.dumps([
            [review(700, "COMMENTED", HEAD, "2026-10-19T00:00:00Z", body="")],
            [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
        ]))
        r = run_gate(data, bin_dir)
        if r.returncode != 0:
            failures.append(f"paged_reviews: gate exited {r.returncode}: "
                            f"stderr={r.stderr.strip()!r}")
        elif r.stdout.strip() != "FIX 801":
            failures.append(f"paged_reviews: gate printed {r.stdout.strip()!r}, "
                            "expected 'FIX 801'")

        # The same thing through decide(), so the flattening is pinned on
        # both sides.
        pages = json.loads((data / "reviews.json").read_text())
        flat = [r for page in pages for r in page]
        got = pr_gate.decide(pr(), flat, [], [check_run()], {}, REVIEWER, 4)
        if got != "FIX 801":
            failures.append(f"paged_reviews: decide() returned {got!r}, "
                            "expected 'FIX 801'")

        # ---- failure paths --------------------------------------------

        # A gh failure: nothing on stdout, ERR on stderr, exit 1.
        for endpoint in ("reviews", "issue_comments", "pull_comments",
                         "pr", "checks", "tree"):
            data = tmp / f"data_fail_{endpoint}"
            data.mkdir()
            # A stale APPROVED verdict, so that a tree lookup is actually
            # made and the `tree` failure has something to fail on.
            install(data, pr(),
                    [review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                    [], [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1})
            (data / f"gh_fail_{endpoint}").write_text("")
            r = run_gate(data, bin_dir)
            if r.returncode != 1:
                failures.append(
                    f"gh_fail_{endpoint}: expected exit 1, got {r.returncode}")
            if r.stdout != "":
                failures.append(
                    f"gh_fail_{endpoint}: stdout was {r.stdout!r}, expected empty")
            if not r.stderr.startswith("ERR "):
                failures.append(
                    f"gh_fail_{endpoint}: stderr was {r.stderr!r}, expected "
                    "to start with 'ERR '")
            if ENDPOINT_NAMES[endpoint] not in r.stderr:
                failures.append(
                    f"gh_fail_{endpoint}: stderr did not name the endpoint: "
                    f"{r.stderr!r}")

        # A compare that fails is an error, never a quiet `REVIEW`: the
        # gate must not answer from a comparison it did not make.
        data = tmp / "data_fail_compare"
        data.mkdir()
        install(data, pr(),
                [review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()], {HEAD: TREE_2, COMMIT_A: TREE_1},
                {f"{BASE}...{COMMIT_A}": compare_files()})
        (data / "gh_fail_compare").write_text("")
        r = run_gate(data, bin_dir)
        if r.returncode != 1:
            failures.append(
                f"gh_fail_compare: expected exit 1, got {r.returncode}")
        if r.stdout != "":
            failures.append(
                f"gh_fail_compare: stdout was {r.stdout!r}, expected empty")
        if not r.stderr.startswith("ERR "):
            failures.append(
                f"gh_fail_compare: stderr was {r.stderr!r}, expected to start "
                "with 'ERR '")
        if f"compare/{BASE}...{COMMIT_A}" not in r.stderr:
            failures.append(
                f"gh_fail_compare: stderr did not name the endpoint: "
                f"{r.stderr!r}")

        # A malformed entry: a review with no `user`. Skipping it could
        # hide a verdict, so it is a failure.
        data = tmp / "data_malformed_review"
        data.mkdir()
        bad = review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")
        del bad["user"]
        install(data, pr(), [bad], [], [check_run()], {HEAD: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 1:
            failures.append(f"malformed_review: expected exit 1, got {r.returncode}")
        if r.stdout != "":
            failures.append(
                f"malformed_review: stdout was {r.stdout!r}, expected empty")
        if "ERR malformed" not in r.stderr:
            failures.append(
                f"malformed_review: stderr was {r.stderr!r}, expected "
                "'ERR malformed'")

        # The same, through decide().
        try:
            pr_gate.decide(pr(), [bad], [], [check_run()], {HEAD: TREE_1},
                           REVIEWER, 4)
            failures.append("malformed_review: decide() did not raise")
        except pr_gate.Malformed as exc:
            if "malformed reviews" not in str(exc):
                failures.append(
                    f"malformed_review: decide() raised {exc!r}, expected "
                    "'malformed reviews'")

        # A malformed entry fails the run even when the rule that would
        # have answered would not have needed it: the entry might be the
        # verdict the next run needs.
        for pr_state, merged, label in ((("closed"), True, "merged"),
                                        (("open"), False, "open")):
            data = tmp / f"data_malformed_{label}"
            data.mkdir()
            bad = review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")
            del bad["user"]
            install(data, pr(state=pr_state, merged=merged), [bad], [],
                    [check_run()], {HEAD: TREE_1})
            r = run_gate(data, bin_dir)
            if r.returncode != 1:
                failures.append(
                    f"malformed_{label}: expected exit 1, got {r.returncode}")
            if r.stdout != "":
                failures.append(
                    f"malformed_{label}: stdout was {r.stdout!r}, expected empty")
            if "ERR malformed" not in r.stderr:
                failures.append(
                    f"malformed_{label}: stderr was {r.stderr!r}, expected "
                    "'ERR malformed'")

        # The same through decide().
        try:
            pr_gate.decide(pr(state="closed", merged=True), [bad], [],
                           [check_run()], {HEAD: TREE_1}, REVIEWER, 4)
            failures.append(
                "malformed_merged: decide() returned a line over a list it "
                "could not read")
        except pr_gate.Malformed:
            pass

        # A malformed comment fails the run too.
        data = tmp / "data_malformed_comment"
        data.mkdir()
        bad_comment = comment(1, "2026-10-20T01:00:00Z", "someone-else")
        del bad_comment["created_at"]
        install(data, pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [bad_comment], [check_run()], {HEAD: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 1 or "ERR malformed" not in r.stderr:
            failures.append(
                f"malformed_comment: expected exit 1 with 'ERR malformed', got "
                f"rc={r.returncode} stderr={r.stderr!r}")
        if r.stdout != "":
            failures.append(
                f"malformed_comment: stdout was {r.stdout!r}, expected empty")

        # A submitted verdict with no `submitted_at` is still malformed.
        # Dropping PENDING reviews is the narrow fix for the caller's own
        # unsubmitted draft, and leaves the timestamp check in place for
        # every other state.
        for state in ("APPROVED", "CHANGES_REQUESTED"):
            data = tmp / f"data_malformed_no_time_{state}"
            data.mkdir()
            bad = review(801, state, HEAD, "2026-10-20T00:00:00Z")
            del bad["submitted_at"]
            install(data, pr(), [bad], [], [check_run()], {HEAD: TREE_1})
            r = run_gate(data, bin_dir)
            if r.returncode != 1 or "ERR malformed" not in r.stderr:
                failures.append(
                    f"malformed_no_time_{state}: expected exit 1 with "
                    f"'ERR malformed', got rc={r.returncode} "
                    f"stderr={r.stderr!r}")
            if r.stdout != "":
                failures.append(
                    f"malformed_no_time_{state}: stdout was {r.stdout!r}, "
                    "expected empty")

        # A PENDING review with a `submitted_at` is still dropped: the
        # state decides, whatever the shape.
        data = tmp / "data_pending_with_time"
        data.mkdir()
        pending = {"id": 999, "user": {"login": IMPLEMENTER, "type": "User"},
                   "state": "PENDING", "commit_id": HEAD,
                   "submitted_at": "2026-10-20T00:00:00Z", "body": "draft"}
        install(data, pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
                 pending],
                [], [check_run()], {HEAD: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 0 or r.stdout.strip() != "AGREED " + HEAD:
            failures.append(
                f"pending_with_time: expected 'AGREED {HEAD}', got "
                f"rc={r.returncode} stdout={r.stdout.strip()!r}")

        # A PENDING review is no verdict, so one alone leaves the head
        # needing a review.
        data = tmp / "data_pending_only"
        data.mkdir()
        install(data, pr(), [pending], [], [check_run()], {HEAD: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 0 or r.stdout.strip() != "REVIEW " + HEAD:
            failures.append(
                f"pending_only: expected 'REVIEW {HEAD}', got rc={r.returncode} "
                f"stdout={r.stdout.strip()!r}")

        # A review with a `state` of the wrong type.
        data = tmp / "data_malformed_state"
        data.mkdir()
        bad = review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")
        bad["state"] = 42
        install(data, pr(), [bad], [], [check_run()], {HEAD: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 1 or "ERR malformed" not in r.stderr:
            failures.append(
                f"malformed_state: expected exit 1 with 'ERR malformed', got "
                f"rc={r.returncode} stderr={r.stderr!r}")

        # A review with no `submitted_at`.
        data = tmp / "data_malformed_submitted"
        data.mkdir()
        bad = review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")
        del bad["submitted_at"]
        install(data, pr(), [bad], [], [check_run()], {HEAD: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 1 or "ERR malformed" not in r.stderr:
            failures.append(
                f"malformed_submitted: expected exit 1 with 'ERR malformed', got "
                f"rc={r.returncode} stderr={r.stderr!r}")

        # A review with no `commit_id`.
        data = tmp / "data_malformed_commit"
        data.mkdir()
        bad = review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")
        del bad["commit_id"]
        install(data, pr(), [bad], [], [check_run()], {HEAD: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 1 or "ERR malformed" not in r.stderr:
            failures.append(
                f"malformed_commit: expected exit 1 with 'ERR malformed', got "
                f"rc={r.returncode} stderr={r.stderr!r}")

        # With no --reviewer and no $REVIEWER_LOGIN, the reviewer is the
        # owner segment of the repo argument: the owner reviews their own
        # repository. The stub's repo is owner/repo, so `owner` is V.
        data = tmp / "data_no_reviewer"
        data.mkdir()
        install(data, pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z",
                        login="owner")],
                [], [check_run()], {})
        r = run_gate(data, bin_dir, reviewer=None)
        if r.returncode != 0 or r.stdout.strip() != "AGREED " + HEAD:
            failures.append(
                f"no_reviewer: expected 'AGREED {HEAD}' from the owner "
                f"fallback, got rc={r.returncode} stdout={r.stdout.strip()!r} "
                f"stderr={r.stderr.strip()!r}")

        # The reviewer can come from the environment, with no flag.
        data = tmp / "data_env_reviewer"
        data.mkdir()
        install(data, pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [], [check_run()], {})
        r = run_gate(data, bin_dir, reviewer=None,
                     env_extra={"REVIEWER_LOGIN": REVIEWER})
        if r.returncode != 0 or r.stdout.strip() != "AGREED " + HEAD:
            failures.append(
                f"env_reviewer: expected 'AGREED {HEAD}', got rc={r.returncode} "
                f"stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")

        # The reviewer is a login, not a constant: a different login's
        # verdict is no V.
        data = tmp / "data_other_reviewer"
        data.mkdir()
        install(data, pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z",
                        login="someone-else")],
                [], [check_run()], {})
        r = run_gate(data, bin_dir, reviewer=REVIEWER)
        if r.returncode != 0 or r.stdout.strip() != "REVIEW " + HEAD:
            failures.append(
                f"other_reviewer: expected 'REVIEW {HEAD}', got rc={r.returncode} "
                f"stdout={r.stdout.strip()!r}")

        data = tmp / "data_max_rounds_arg"
        data.mkdir()
        install(data, pr(),
                [review(1, "CHANGES_REQUESTED", HEAD, "2026-10-16T00:00:00Z"),
                 review(2, "CHANGES_REQUESTED", HEAD, "2026-10-17T00:00:00Z")],
                [], [check_run()], {})
        r = run_gate(data, bin_dir, "--max-rounds", "2")
        if r.returncode != 0 or r.stdout.strip() != "ESCALATE max-rounds":
            failures.append(
                f"max_rounds_arg: expected 'ESCALATE max-rounds', got "
                f"rc={r.returncode} stdout={r.stdout.strip()!r}")

        # The gate never writes to GitHub: the stub logs every call, and
        # none of them may be a write.
        data = tmp / "data_no_writes"
        data.mkdir()
        install(data, pr(),
                [review(801, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")],
                [], [check_run()], {HEAD: TREE_1})
        run_gate(data, bin_dir)
        log = data / "gh.log"
        if not log.exists():
            failures.append("no_writes: the stub gh was never called")
        else:
            for line in log.read_text().splitlines():
                for write in ("--method POST", "--method PUT", "--method PATCH",
                              "--method DELETE", " issue ", " comment "):
                    if write in line:
                        failures.append(
                            f"no_writes: the gate issued a write: {line!r}")
            # And no file other than the log was written.
            for path in data.iterdir():
                if path.name not in ("gh.log", "pr.json", "reviews.json",
                                     "issue_comments.json", "pull_comments.json",
                                     "checks.json", "trees.json", "compares.json"):
                    failures.append(
                        f"no_writes: the gate wrote {path.name}")

        # The gate asks for the reviewed commit's tree only when the
        # verdict is stale. On a verdict that matches the head, one tree
        # lookup is enough.
        data = tmp / "data_tree_count"
        data.mkdir()
        # A verdict that matches the head decides without a tree, so no
        # tree is fetched at all.
        install(data, pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [], [check_run()], {HEAD: TREE_1})
        run_gate(data, bin_dir)
        log = (data / "gh.log").read_text()
        lookups = [ln for ln in log.splitlines() if "/git/commits/" in ln]
        if lookups:
            failures.append(
                f"tree_count: a verdict on the head needs no tree lookup, "
                f"but {len(lookups)} were made: {lookups!r}")

        # The gate and the decision must agree on which review is V. If
        # they drift, the gate fetches one review's tree and the decision
        # compares another's, and rule 5 answers REVIEW, not REAPPROVE.
        data = tmp / "data_v_agreement"
        data.mkdir()
        install(data, pr(),
                [review(700, "COMMENTED", HEAD, "2026-10-19T00:00:00Z",
                        body=""),
                 review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 0 or r.stdout.strip() != "REAPPROVE " + HEAD:
            failures.append(
                f"v_agreement: expected 'REAPPROVE {HEAD}', got rc={r.returncode} "
                f"stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")

        log = (data / "gh.log").read_text()
        lookups = {ln.rsplit("/", 1)[-1] for ln in log.splitlines()
                   if "/git/commits/" in ln}
        # The head, and the commit of the verdict -- which is the latest
        # verdict review, not the latest review of any state.
        if lookups != {HEAD, COMMIT_A}:
            failures.append(
                f"v_agreement: the gate looked up {sorted(lookups)}, expected "
                f"the head and the verdict's commit "
                f"{sorted([HEAD, COMMIT_A])}")

        # And the decision reads the same verdict: the latest APPROVED
        # review, not the COMMENTED one that is newer in no sense.
        verdicts = pr_gate.verdict_reviews(
            [review(700, "COMMENTED", HEAD, "2026-10-19T00:00:00Z", body=""),
             review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
            REVIEWER)
        if len(verdicts) != 1 or verdicts[0]["commit_id"] != COMMIT_A:
            failures.append(
                f"v_agreement: verdict_reviews() picked "
                f"{[v['commit_id'] for v in verdicts]}, expected [{COMMIT_A}]")

        # The rule lives in one place. A behavioural check cannot catch a
        # second copy of "which review is V" that agrees today: the drift
        # is what the copy allows later. So the check is structural.
        gate_src = (REPO_ROOT / "bin" / "pr-loop-gate").read_text()
        lib_src = (REPO_ROOT / "lib" / "pr_gate.py").read_text()

        # The caller's tree choice goes through the same helper.
        if "verdict_reviews(" not in gate_src:
            failures.append(
                "v_agreement: bin/pr-loop-gate does not read the verdicts "
                "through verdict_reviews()")
        # The decision carries no filter or sort of its own.
        decide_body = lib_src.split("def decide(", 1)[-1]
        for stray in ('r["state"] in VERDICT_STATES',
                      'r["submitted_at"],'):
            if stray in decide_body:
                failures.append(
                    f"v_agreement: decide() carries its own copy of the "
                    f"verdict rule ({stray!r}); the rule must live in one "
                    "place")

        # A third-party post since the verdict blocks the squash
        # shortcut, so the decision skips the tree comparison and the
        # fetch skips the lookups. The fetch must also read the reviewer
        # correctly: the API's `user` is a nested object.
        data = tmp / "data_no_trees_when_interrupted"
        data.mkdir()
        install(data, pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "a-person")],
                [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1})
        r = run_gate(data, bin_dir)
        expected = "REVIEW " + HEAD + " " + COMMIT_A
        if r.returncode != 0 or r.stdout.strip() != expected:
            failures.append(
                f"no_trees_when_interrupted: expected {expected!r}, got "
                f"rc={r.returncode} stdout={r.stdout.strip()!r} "
                f"stderr={r.stderr.strip()!r}")
        log = (data / "gh.log").read_text()
        lookups = [ln for ln in log.splitlines() if "/git/commits/" in ln]
        if lookups:
            failures.append(
                f"no_trees_when_interrupted: the gate looked up "
                f"{len(lookups)} tree(s) that the decision cannot use: "
                f"{lookups!r}")

        # The reviewer's own post after the verdict is no third party, so
        # the trees are looked up and the shortcut applies.
        data = tmp / "data_trees_when_not_interrupted"
        data.mkdir()
        install(data, pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", REVIEWER)],
                [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1})
        r = run_gate(data, bin_dir)
        if r.returncode != 0 or r.stdout.strip() != "REAPPROVE " + HEAD:
            failures.append(
                f"trees_when_not_interrupted: expected 'REAPPROVE {HEAD}', got "
                f"rc={r.returncode} stdout={r.stdout.strip()!r} "
                f"stderr={r.stderr.strip()!r}")
        log = (data / "gh.log").read_text()
        lookups = {ln.rsplit("/", 1)[-1] for ln in log.splitlines()
                   if "/git/commits/" in ln}
        if lookups != {HEAD, COMMIT_A}:
            failures.append(
                f"trees_when_not_interrupted: the gate looked up "
                f"{sorted(lookups)}, expected {sorted([HEAD, COMMIT_A])}")

        # A stale APPROVED verdict compares two trees, and each lookup
        # asks for a full 40-character sha.
        data = tmp / "data_tree_count_stale"
        data.mkdir()
        install(data, pr(),
                [review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1})
        run_gate(data, bin_dir)
        log = (data / "gh.log").read_text()
        lookups = [ln for ln in log.splitlines() if "/git/commits/" in ln]
        if len(lookups) != 2:
            failures.append(
                f"tree_count_stale: expected 2 tree lookups, got "
                f"{len(lookups)}: {lookups!r}")
        for line in lookups:
            sha = line.rsplit("/", 1)[-1]
            if len(sha) != 40:
                failures.append(
                    f"tree_count_stale: tree lookup used a sha that is not "
                    f"40 characters: {line!r}")

        # The compares are fetched lazily, as the trees are.
        data = tmp / "data_compare_is_lazy"
        data.mkdir()
        install(data, pr(),
                [review(501, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [], [check_run()], {HEAD: TREE_1, COMMIT_A: TREE_1})
        run_gate(data, bin_dir)
        log = (data / "gh.log").read_text()
        compares = [ln for ln in log.splitlines() if "/compare/" in ln]
        if compares:
            failures.append(
                f"compare_is_lazy: the gate compared {len(compares)} patch(es) "
                f"for an equal tree: {compares!r}")

        # A verdict on the head needs no tree and no compare.
        data = tmp / "data_compare_not_needed"
        data.mkdir()
        install(data, pr(),
                [review(701, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
                [], [check_run()], {HEAD: TREE_1})
        run_gate(data, bin_dir)
        log = (data / "gh.log").read_text()
        compares = [ln for ln in log.splitlines() if "/compare/" in ln]
        if compares:
            failures.append(
                f"compare_not_needed: the gate compared {len(compares)} "
                f"patch(es) for a verdict on the head: {compares!r}")

        # A third party since the verdict stops the comparison.
        data = tmp / "data_compare_not_when_interrupted"
        data.mkdir()
        install(data, pr(),
                [review(701, "APPROVED", COMMIT_A, "2026-10-20T00:00:00Z")],
                [comment(1, "2026-10-20T01:00:00Z", "a-person")],
                [check_run()], {HEAD: TREE_2, COMMIT_A: TREE_1})
        run_gate(data, bin_dir)
        log = (data / "gh.log").read_text()
        compares = [ln for ln in log.splitlines() if "/compare/" in ln]
        if compares:
            failures.append(
                f"compare_not_when_interrupted: the gate compared "
                f"{len(compares)} patch(es) that the decision cannot use: "
                f"{compares!r}")

        # ---- the patch comparison, through the pure helper ------------

        # A patch that differs only in its hunk headers is the same
        # patch: a rebase shifts every line number.
        shifted = PATCH_BODY.replace("@@ -10,3 +10,3 @@", "@@ -99,3 +99,3 @@")
        if not pr_gate.same_patch(compare_files(), compare_files(patch=shifted)):
            failures.append(
                "same_patch: a patch that differs only in its hunk headers "
                "is not equal")

        changed = PATCH_BODY.replace("-old line", "-other line")
        if pr_gate.same_patch(compare_files(), compare_files(patch=changed)):
            failures.append(
                "same_patch: a patch that changes the work is equal")

        # A file with no `patch` cannot be compared.
        if pr_gate.same_patch(compare_files(), compare_files(patch=None)):
            failures.append(
                "same_patch: a file with no patch compares equal")

        # 300 files is a truncated list, so it cannot be compared.
        many = [compare_file(filename=f"lib/f{i}.py") for i in range(300)]
        if pr_gate.same_patch(many, many):
            failures.append(
                "same_patch: a 300-file response compares equal")

        many = [compare_file(filename=f"lib/f{i}.py") for i in range(299)]
        if not pr_gate.same_patch(many, many):
            failures.append(
                "same_patch: a 299-file response does not compare equal")

        # The header's trailing context is dropped with the line
        # numbers: it names the code around the hunk, which a rebase
        # moves.
        moved = PATCH_BODY.replace("@@ -10,3 +10,3 @@ def f():",
                                   "@@ -10,3 +10,3 @@ def g():")
        if not pr_gate.same_patch(compare_files(), compare_files(patch=moved)):
            failures.append(
                "same_patch: the hunk header's context is compared")

        # The two sides are compared as multisets.
        two = compare_files() + [compare_file(filename="lib/y.py")]
        if not pr_gate.same_patch(two, list(reversed(two))):
            failures.append(
                "same_patch: the order the files are listed in matters")

        # A different file is a different patch.
        if pr_gate.same_patch(compare_files(),
                              compare_files(filename="lib/y.py")):
            failures.append(
                "same_patch: a patch touching another file is equal")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
