#!/usr/bin/env python3
"""Pin what merge-check refuses, and what it prints when it agrees.

The decision itself is pure, so it is tested against facts. The driver is
tested against a real git repository with a bare origin and a `gh` stub.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "merge-check"
sys.path.insert(0, str(REPO_ROOT / "lib"))

import merge_check as mc  # noqa: E402
import pr_gate  # noqa: E402


def fail(failures: list[str], msg: str, detail: str = "") -> None:
    failures.append(f"{msg}: {detail}" if detail else msg)


# --------------------------------------------------------------------------
# The pure decision.
# --------------------------------------------------------------------------

BASE = "a" * 40
HEAD = "b" * 40
PARENT = "c" * 40
REVIEWED = "d" * 40

IDENTITY = {"name": "Lane Bot", "email": "lane@example.invalid"}

# The reviewer whose review counts as the review, and the implementer
# whose comments are the loop's own.
REVIEWER = "the-reviewer"
IMPLEMENTER = "the-implementer"


def commit(sha: str = HEAD, name: str = "Lane Bot", email: str = "lane@example.invalid",
           message: str = "Fix the thing\n\nSigned-off-by: Lane Bot <lane@example.invalid>\n",
           committer_name: str = "Lane Bot",
           committer_email: str = "lane@example.invalid"):
    return {"sha": sha, "author_name": name, "author_email": email,
            "committer_name": committer_name, "committer_email": committer_email,
            "message": message}


def good_facts(**over):
    """A pull request that passes every check."""
    facts = {
        "base_tip": BASE,
        "head": HEAD,
        "merge_base": BASE,
        "head_parent": BASE,
        "commit_count": 1,
        "merge": None,
        "head_files": ["a.txt"],
        "reviewed_files": ["a.txt"],
        "reviewed_sha": REVIEWED,
        "commits": [commit()],
        "expected_name": IDENTITY["name"],
        "expected_email": IDENTITY["email"],
        "require_signoff": True,
        "checks_pending": False,
        "pending_names": [],
        "failing_names": [],
        "third_party": [],
        "threads": [],
        "labels": [],
    }
    facts.update(over)
    return facts


def case_a_ready_pull_request_passes(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts())
    if not verdict.ok:
        fail(failures, "ready: a pull request that passes every check was refused",
             str(verdict.failures))
        return
    if "--match-head-commit" not in verdict.merge_command:
        fail(failures, "ready: the merge command does not name the head",
             verdict.merge_command)
    if HEAD not in verdict.merge_command:
        fail(failures, "ready: the merge command does not carry the head sha",
             verdict.merge_command)


def case_a_branch_on_the_base_passes(failures: list[str]) -> None:
    # The merge base is the base tip: the branch carries everything the
    # base has, and no merge is needed.
    verdict = mc.decide(**good_facts())
    if not verdict.ok:
        fail(failures, "base: a branch on the base was refused",
             str(verdict.failures))


def case_a_branch_behind_the_base_that_merges_cleanly_passes(failures: list[str]) -> None:
    # The merge base is an ancestor of the base tip: the branch forked
    # before the base moved on. The merge succeeds, so the branch
    # passes. The single commit's parent is the merge base.
    verdict = mc.decide(**good_facts(
        merge_base=PARENT,
        head_parent=PARENT,
        merge=mc.MergeOutcome("clean")))
    if not verdict.ok:
        fail(failures, "base: a branch behind the base that merges cleanly "
                      "was refused", str(verdict.failures))


def case_a_branch_behind_the_base_that_conflicts_is_refused(failures: list[str]) -> None:
    # The merge is tried and it fails: the branch and the base changed
    # the same lines. The refusal names the file that conflicts.
    verdict = mc.decide(**good_facts(
        merge_base=PARENT,
        head_parent=PARENT,
        merge=mc.MergeOutcome("conflict", files=["a.txt"])))
    if verdict.ok:
        fail(failures, "base: a branch that conflicts with the base was "
                      "not refused")
        return
    if not any(f.check == "base" for f in verdict.failures):
        fail(failures, "base: the refusal does not name the base check",
             str(verdict.failures))
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "a.txt" not in detail:
        fail(failures, "base: the refusal does not name the conflicting file",
             detail)


def case_a_branch_behind_the_base_whose_merge_failed_is_refused(failures: list[str]) -> None:
    # The merge could not be run, or answered something unexpected. An
    # unknown result never passes: the refusal names the error.
    verdict = mc.decide(**good_facts(
        merge_base=PARENT,
        head_parent=PARENT,
        merge=mc.MergeOutcome("error", detail="not something we can merge")))
    if verdict.ok:
        fail(failures, "base: a branch whose merge could not be run was "
                      "not refused")
        return
    if not any(f.check == "base" for f in verdict.failures):
        fail(failures, "base: the refusal does not name the base check",
             str(verdict.failures))
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "not something we can merge" not in detail:
        fail(failures, "base: the refusal does not name the error", detail)


def case_a_one_commit_pull_request_must_sit_on_the_base(failures: list[str]) -> None:
    # One commit, but its parent is an old fork, not the base.
    verdict = mc.decide(**good_facts(head_parent=PARENT))
    if verdict.ok:
        fail(failures, "base: a one-commit pull request off a stale fork was not refused")
    # Two commits: the parent check leaves them alone. The branch is
    # otherwise current.
    verdict = mc.decide(**good_facts(commit_count=2, head_parent=PARENT,
                                     commits=[commit(), commit(HEAD)]))
    if not verdict.ok:
        fail(failures, "base: a two-commit pull request was refused by the "
                       "one-commit parent rule", str(verdict.failures))


def case_a_changed_file_is_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(head_files=["a.txt", "b.txt"]))
    if verdict.ok:
        fail(failures, "files: a file added since the review was not refused")
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "b.txt" not in detail:
        fail(failures, "files: the refusal does not name the file", detail)


def case_a_removed_file_is_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(reviewed_files=["a.txt", "b.txt"]))
    if verdict.ok:
        fail(failures, "files: a file removed since the review was not refused")
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "b.txt" not in detail:
        fail(failures, "files: the refusal does not name the removed file", detail)


def case_a_changed_file_set_is_the_same_change(failures: list[str]) -> None:
    # The finishing run rewrote prose, so the patch changed and the files
    # did not. That is the same change.
    verdict = mc.decide(**good_facts())
    if not verdict.ok:
        fail(failures, "files: an unchanged file set was refused",
             str(verdict.failures))


def case_a_wrong_author_is_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(commits=[commit(name="Somebody Else",
                                                    email="other@example.invalid")]))
    if verdict.ok:
        fail(failures, "identity: a commit by another author was not refused")
        return
    if not any(f.check == "identity" for f in verdict.failures):
        fail(failures, "identity: the refusal does not name the identity check",
             str(verdict.failures))


def case_a_missing_signoff_is_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(
        commits=[commit(message="Fix the thing\n")]))
    if verdict.ok:
        fail(failures, "signoff: an unsigned commit was not refused")
        return
    if not any(f.check == "signoff" for f in verdict.failures):
        fail(failures, "signoff: the refusal does not name the signoff check",
             str(verdict.failures))


def case_a_signoff_by_another_person_is_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(commits=[commit(
        message="Fix the thing\n\nSigned-off-by: Somebody Else <other@example.invalid>\n")]))
    if verdict.ok:
        fail(failures, "signoff: a commit signed by another person was not refused")


def case_two_signoffs_are_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(commits=[commit(
        message="Fix the thing\n\nSigned-off-by: Lane Bot <lane@example.invalid>\n"
                "Signed-off-by: Second Person <second@example.invalid>\n")]))
    if verdict.ok:
        fail(failures, "signoff: a commit with two sign-offs was not refused")


def case_a_signoff_mentioned_in_prose_is_not_a_signoff(failures: list[str]) -> None:
    # The phrase in the body of a commit is no trailer.
    verdict = mc.decide(**good_facts(commits=[commit(
        message="Fix the thing\n\nThis fixes the Signed-off-by: trailer bug.\n")]))
    if verdict.ok:
        fail(failures, "signoff: a commit that mentions the phrase in prose "
                       "was not refused")


def case_a_repository_that_asks_for_no_signoff_is_not_checked(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(
        require_signoff=False,
        commits=[commit(message="Fix the thing\n")]))
    if not verdict.ok:
        fail(failures, "signoff: a repository that asks for no sign-off was "
                       "refused one", str(verdict.failures))


def case_pending_checks_are_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(checks_pending=True, pending_names=["ci/build"]))
    if verdict.ok:
        fail(failures, "checks: a pull request with a running check was not refused")
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "ci/build" not in detail:
        fail(failures, "checks: the refusal does not name the check", detail)


def case_a_failed_check_is_refused(failures: list[str]) -> None:
    # A check that concluded `failure` is evidence against the branch.
    # Treating "concluded" as "green" is what let a red head through.
    verdict = mc.decide(**good_facts(failing_names=["ci/build"]))
    if verdict.ok:
        fail(failures, "checks: a failed check was not refused")
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "ci/build" not in detail:
        fail(failures, "checks: the refusal does not name the check", detail)
    if not any(f.check == "checks" for f in verdict.failures):
        fail(failures, "checks: the refusal does not name the checks check",
             str(verdict.failures))


def case_only_success_neutral_and_skipped_pass(failures: list[str]) -> None:
    # The classifier is a list of what passes, not of what fails: a
    # conclusion nobody anticipated is refused, not let through.
    passing = pr_gate.PASSING_CHECK_CONCLUSIONS
    if tuple(passing) != ("SUCCESS", "NEUTRAL", "SKIPPED"):
        fail(failures, "checks: the passing conclusions are not the three "
                      f"expected ones", str(passing))
        return
    for conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
                       "STARTUP_FAILURE", "STALE"):
        names = pr_gate.failed_check_names([
            {"__typename": "CheckRun", "name": "ci/build",
             "status": "COMPLETED", "conclusion": conclusion}])
        if names != ["ci/build"]:
            fail(failures, f"checks: {conclusion} was not a failure", str(names))
    for conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
        names = pr_gate.failed_check_names([
            {"__typename": "CheckRun", "name": "ci/build",
             "status": "COMPLETED", "conclusion": conclusion}])
        if names:
            fail(failures, f"checks: {conclusion} was a failure", str(names))


def case_a_running_check_is_not_a_failure(failures: list[str]) -> None:
    # A check that has not finished is no failure: it is still running,
    # which the other check refuses.
    names = pr_gate.failed_check_names([
        {"__typename": "CheckRun", "name": "ci/build",
         "status": "IN_PROGRESS", "conclusion": None}])
    if names:
        fail(failures, "checks: a running check was named as a failure",
             str(names))


def rollup_run(name: str = "ci/build", workflow: str = "CI", status: str = "COMPLETED",
               conclusion: str | None = None, started: str | None = None):
    """A `CheckRun` as the rollup reports it, fields the reads use only.

    `started` left out is a run that has not started, which is how a
    queued run reaches the rollup.
    """
    entry = {"__typename": "CheckRun", "name": name,
             "workflowName": workflow, "status": status,
             "conclusion": conclusion}
    if started is not None:
        entry["startedAt"] = started
    return entry


def rollup_status(context: str = "ci/build", state: str = "SUCCESS",
                  started: str | None = None):
    """A `StatusContext` as the rollup reports it: `context`, `state`,
    and `startedAt` for the moment the status was posted."""
    entry = {"__typename": "StatusContext", "context": context, "state": state}
    if started is not None:
        entry["startedAt"] = started
    return entry


# Each row is the runs of one check on one head, oldest first, and what
# the two reads answer. A workflow with `concurrency: cancel-in-progress`
# leaves two runs of a job on a head: the push supersedes the first, which
# ends `cancelled`. GitHub judges a check by its latest run, so a
# superseded run says nothing about the branch.
SUPERSEDED_ROWS = [
    ("cancelled then success",
     [rollup_run(conclusion="CANCELLED", started="2026-11-13T05:50:12Z"),
      rollup_run(conclusion="SUCCESS", started="2026-11-13T06:11:27Z")],
     [], False),
    ("success then failure",
     [rollup_run(conclusion="SUCCESS", started="2026-11-13T05:00:00Z"),
      rollup_run(conclusion="FAILURE", started="2026-11-13T06:00:00Z")],
     ["ci/build"], False),
    ("cancelled only",
     [rollup_run(conclusion="CANCELLED", started="2026-11-13T05:50:12Z")],
     ["ci/build"], False),
    ("success then a queued run",
     [rollup_run(conclusion="SUCCESS", started="2026-11-13T05:00:00Z"),
      rollup_run(status="QUEUED", conclusion=None)],
     [], True),
    # Two workflows, one job name. The name is what a read returns, so
    # the row says which run survives: the success is the later one, and
    # keying by name alone would drop the failure with it.
    ("the same job name in two workflows",
     [rollup_run(name="test", workflow="A", conclusion="FAILURE",
                 started="2026-11-13T05:00:00Z"),
      rollup_run(name="test", workflow="B", conclusion="SUCCESS",
                 started="2026-11-13T06:00:00Z")],
     ["test"], False),
    ("cancelled then skipped",
     [rollup_run(conclusion="CANCELLED", started="2026-11-13T05:50:12Z"),
      rollup_run(conclusion="SKIPPED", started="2026-11-13T06:11:27Z")],
     [], False),
    # A commit status supersedes the one before it for the same context.
    ("a status failure then a status success",
     [rollup_status(state="FAILURE", started="2026-11-13T05:00:00Z"),
      rollup_status(state="SUCCESS", started="2026-11-13T06:00:00Z")],
     [], False),
    ("a status success then a pending status",
     [rollup_status(state="SUCCESS", started="2026-11-13T05:00:00Z"),
      rollup_status(state="PENDING", started="2026-11-13T06:00:00Z")],
     [], True),
]


def case_a_superseded_check_run_is_not_evidence(failures: list[str]) -> None:
    for label, runs, expected_failed, expected_pending in SUPERSEDED_ROWS:
        names = pr_gate.failed_check_names(runs)
        if names != expected_failed:
            fail(failures, f"checks: {label}: unexpected failed names",
                 f"got {names}, expected {expected_failed}")
        pending = bool(pr_gate.pending_checks(runs))
        if pending is not expected_pending:
            fail(failures, f"checks: {label}: unexpected pending answer",
                 f"got {pending}, expected {expected_pending}")


def case_a_superseded_run_of_another_check_is_not_dropped(failures: list[str]) -> None:
    # Dropping every cancelled entry would let a check whose only run was
    # cancelled pass for lack of evidence. A check with nothing but a
    # superseded run still refuses.
    runs = [rollup_run(name="other", conclusion="SUCCESS"),
            rollup_run(name="cancelled_only", conclusion="CANCELLED",
                       started="2026-11-13T05:50:12Z")]
    names = pr_gate.failed_check_names(runs)
    if names != ["cancelled_only"]:
        fail(failures, "checks: a check whose only run was cancelled was "
                       "not named", str(names))
    if pr_gate.pending_checks(runs):
        fail(failures, "checks: completed runs were reported as pending")


def case_a_wrong_committer_is_refused(failures: list[str]) -> None:
    # The committer is who put the commit in the repository. A commit
    # the lane wrote and something else committed is no lane commit.
    verdict = mc.decide(**good_facts(commits=[commit(
        committer_name="Somebody Else",
        committer_email="other@example.invalid")]))
    if verdict.ok:
        fail(failures, "identity: a commit by another committer was not refused")
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "Somebody Else" not in detail:
        fail(failures, "identity: the refusal does not name the committer",
             detail)


def case_a_third_party_comment_is_refused(failures: list[str]) -> None:
    # A person's comment is input to the loop, and the merge waits for
    # it.
    verdict = mc.decide(**good_facts(third_party=[
        {"login": "somebody", "created_at": "2026-01-04T00:00:00Z"}]))
    if verdict.ok:
        fail(failures, "comments: a third party's comment was not refused")
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "somebody" not in detail:
        fail(failures, "comments: the refusal does not name the commenter",
             detail)
    if "2026-01-04" not in detail:
        fail(failures, "comments: the refusal does not say when the comment "
                       "was made", detail)


def case_an_unanswered_thread_by_the_implementers_login_is_refused(failures: list[str]) -> None:
    # A thread's author never decides whether it needs an answer: the
    # reviewer posting as the implementer's login merged its findings
    # unanswered.
    verdict = mc.decide(**good_facts(threads=[thread_node(author=IMPLEMENTER)]))
    if verdict.ok:
        fail(failures, "threads: an unanswered thread by the implementer's "
                       "login was not refused")
        return
    if not any(f.check == "threads" for f in verdict.failures):
        fail(failures, "threads: the refusal does not name the threads check",
             str(verdict.failures))
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "a.txt:3" not in detail:
        fail(failures, "threads: the refusal does not say where the thread "
                       "is", detail)
    if IMPLEMENTER not in detail:
        fail(failures, "threads: the refusal does not name who started the "
                       "thread", detail)


def case_an_unanswered_thread_by_the_reviewer_is_refused(failures: list[str]) -> None:
    # The rule is author-blind: the reviewer's own finding waits for an
    # answer as much as anybody else's.
    verdict = mc.decide(**good_facts(threads=[thread_node(author=REVIEWER)]))
    if verdict.ok:
        fail(failures, "threads: an unanswered thread by the reviewer was "
                       "not refused")


def case_a_replied_thread_passes(failures: list[str]) -> None:
    # Two comments: the finding and a reply to it. The thread is still
    # open, and an open thread somebody answered asks nothing of the merge.
    verdict = mc.decide(**good_facts(threads=[thread_node(comments=2)]))
    if not verdict.ok:
        fail(failures, "threads: an answered thread stopped the merge",
             str(verdict.failures))


def case_a_resolved_thread_passes(failures: list[str]) -> None:
    # Resolved with no reply: whoever read it decided it needed none.
    verdict = mc.decide(**good_facts(threads=[thread_node(resolved=True)]))
    if not verdict.ok:
        fail(failures, "threads: a resolved thread stopped the merge",
             str(verdict.failures))


def case_an_outdated_unanswered_thread_names_where_it_was(failures: list[str]) -> None:
    # The code moved out from under the comment, so `line` is null.
    # Moving is no answer; the refusal names where it was written.
    verdict = mc.decide(**good_facts(
        threads=[thread_node("b.txt", 9, outdated=True, original_line=12)]))
    if verdict.ok:
        fail(failures, "threads: an unanswered outdated thread was not refused")
        return
    detail = " ".join(f.detail for f in verdict.failures)
    if "b.txt:12" not in detail:
        fail(failures, "threads: the refusal does not name the line the "
                       "thread was written against", detail)


def case_no_review_threads_pass(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(threads=[]))
    if not verdict.ok:
        fail(failures, "threads: a pull request with no threads was refused",
             str(verdict.failures))


def case_a_needs_decision_label_is_refused(failures: list[str]) -> None:
    verdict = mc.decide(**good_facts(labels=["needs-decision"]))
    if verdict.ok:
        fail(failures, "label: a needs-decision pull request was not refused")
        return
    if not any(f.check == "label" for f in verdict.failures):
        fail(failures, "label: the refusal does not name the label check",
             str(verdict.failures))


def case_every_check_runs_after_one_fails(failures: list[str]) -> None:
    # A reader sees everything that is wrong, not only the first thing.
    verdict = mc.decide(**good_facts(merge_base=PARENT,
                                     merge=mc.MergeOutcome(
                                         "conflict", files=["a.txt"]),
                                     labels=["needs-decision"],
                                     checks_pending=True, pending_names=["ci"]))
    checks = {f.check for f in verdict.failures}
    for expected in ("base", "label", "checks"):
        if expected not in checks:
            fail(failures, "all: the refusal did not report every failing check",
                 f"{expected} missing from {sorted(checks)}")


def case_the_trailer_needs_its_own_line(failures: list[str]) -> None:
    trailers = mc.signoff_trailers(
        "Subject\n\nSigned-off-by: Lane Bot <lane@example.invalid>\n")
    if len(trailers) != 1:
        fail(failures, "trailer: a trailer on its own line was not found",
             str(trailers))
        return
    if trailers[0] != ("Lane Bot", "lane@example.invalid"):
        fail(failures, "trailer: the trailer was not read as name and email",
             str(trailers))
    trailers = mc.signoff_trailers(
        "Subject\n\nSigned-off-by: A <a@x>\nSigned-off-by: B <b@x>\n")
    if len(trailers) != 2:
        fail(failures, "trailer: two trailers were not both found", str(trailers))


# --------------------------------------------------------------------------
# The driver, against a real repository.
# --------------------------------------------------------------------------

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    # `gh api` sends a request with a field (`-f` or `-F`) as a POST
    # unless the method is named, and a read sent as a POST is refused
    # by the endpoints that answer a read. A call that carries a field
    # without `-X GET` is refused here too, so the defect is caught in
    # the suite and not only on a real pull request.
    if args and args[0] == "api" and args[1:2] != ["graphql"]:
        has_field = any(a == "-f" or a == "-F" for a in args)
        names_get = any(a == "GET" for a in args)
        if has_field and not names_get:
            print("gh: a request with a field is a POST; use -X GET to "
                  "read", file=sys.stderr)
            sys.exit(1)
    canned = json.load(open(os.environ["FAKE_GH_DATA"]))
    if args[:2] == ["api", "graphql"]:
        # GraphQL reads travel as POSTs by design, so the guard above
        # does not reach them. Pages come back in the order a driver
        # asks for them, and any page after the first has to be asked
        # for with the cursor the page before handed out; a driver that
        # never follows the cursor gets an error rather than the page it
        # already had.
        pages = canned.get("graphql_threads") or []
        plog = os.environ["FAKE_GH_LOG"] + ".graphql"
        seen = (len(open(plog).read().splitlines())
                if os.path.exists(plog) else 0)
        after = next((a.split("=", 1)[1] for a in args
                      if a.startswith("after=")), None)
        if seen:
            want = (pages[min(seen - 1, len(pages) - 1)]["pageInfo"].get("endCursor")
                    if pages else None)
            if after != want:
                print("graphql: page %d asked for with cursor %r, not %r"
                      % (seen + 1, after, want), file=sys.stderr)
                sys.exit(1)
        with open(plog, "a") as f:
            f.write(json.dumps(args) + "\\n")
        if canned.get("graphql_fail"):
            print("graphql: Could not resolve to a PullRequest",
                  file=sys.stderr)
            sys.exit(1)
        page = (pages[seen] if seen < len(pages) else pages[-1] if pages
                else {"nodes": [],
                      "pageInfo": {"hasNextPage": False, "endCursor": None}})
        print(json.dumps({"data": {"repository": {"pullRequest": {
            "reviewThreads": page}}}}))
        sys.exit(0)
    if args and args[0] == "pr":
        # `gh pr view N --json statusCheckRollup`
        print(json.dumps({"statusCheckRollup":
                          canned.get("statusCheckRollup", [])}))
        sys.exit(0)
    if args[0] != "api":
        print(json.dumps(canned.get("pr", {})))
        sys.exit(0)
    path = args[1]
    if path.endswith("/reviews"):
        print(json.dumps(canned.get("reviews", [])))
    elif path.endswith("/issues/5/comments"):
        print(json.dumps(canned.get("issue_comments", [])))
    elif path.endswith("/pulls/5/comments"):
        print(json.dumps(canned.get("inline_comments", [])))
    else:
        print(json.dumps(canned.get("pr", {})))
""")

IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Lane Bot",
    "GIT_AUTHOR_EMAIL": "lane@example.invalid",
    "GIT_COMMITTER_NAME": "Lane Bot",
    "GIT_COMMITTER_EMAIL": "lane@example.invalid",
}


def git(cwd: Path, *args: str, author: dict | None = None) -> str:
    env = {**os.environ, **IDENTITY_ENV}
    if author is not None:
        env.update({
            "GIT_AUTHOR_NAME": author["name"],
            "GIT_AUTHOR_EMAIL": author["email"],
        })
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


FEATURE_MESSAGE = (
    "Fix the thing\n\nSigned-off-by: Lane Bot <lane@example.invalid>\n")
FEATURE_AUTHOR = {"name": "Lane Bot", "email": "lane@example.invalid"}


def make_repo(tmp: Path, *, feature_message: str = FEATURE_MESSAGE,
              feature_author: dict = FEATURE_AUTHOR) -> tuple[Path, str, str]:
    """A bare origin, a clone with a main and a feature branch, and the
    shas of the two tips.

    The feature commit's message and author are the knobs: the driver
    reads the commits from the object database.
    """
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = tmp / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "config", "user.email", "lane@example.invalid")
    git(seed, "config", "user.name", "Lane Bot")
    (seed / "a.txt").write_text("one\n")
    git(seed, "add", "a.txt")
    git(seed, "commit", "-qm", "base")
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/main")
    git(seed, "checkout", "-q", "-b", "feature")
    (seed / "a.txt").write_text("one\ntwo\n")
    (seed / "b.txt").write_text("new\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", feature_message, author=feature_author)
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/feature")
    # The base repository mirrors the pull request's head, the way
    # GitHub does: the driver fetches by that ref, not by sha, because a
    # raw sha from a fork cannot be fetched.

    # The pull request the driver checks is number 5.
    git(seed, "push", "-q", str(origin), "HEAD:refs/pull/5/head")
    head = git(seed, "rev-parse", "HEAD")
    main = git(seed, "rev-parse", "main")

    clone = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    git(clone, "config", "user.email", "lane@example.invalid")
    git(clone, "config", "user.name", "Lane Bot")
    return clone, head, main


def make_multi_commit_repo(tmp: Path, oldest_author: dict) -> tuple[Path, str, str]:
    """A clone whose branch carries three commits over the base.

    The oldest of the three carries `oldest_author`, so a reader that
    reads only the newest commit misses it. The other two carry the
    lane's identity, so the branch is otherwise the lane's own work.

    The base repository mirrors the pull request's head under
    `refs/pull/5/head`, the way GitHub does.
    """
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = tmp / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "config", "user.email", "lane@example.invalid")
    git(seed, "config", "user.name", "Lane Bot")
    (seed / "a.txt").write_text("one\n")
    git(seed, "add", "a.txt")
    git(seed, "commit", "-qm", "base")
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/main")
    git(seed, "checkout", "-q", "-b", "feature")
    # The oldest commit on the branch: the one a reader that reads only
    # the newest never sees.
    (seed / "b.txt").write_text("new\n")
    git(seed, "add", "b.txt")
    git(seed, "commit", "-qm",
        "First change\n\nSigned-off-by: %s <%s>\n"
        % (oldest_author["name"], oldest_author["email"]),
        author=oldest_author)
    for n in ("two", "three"):
        (seed / f"{n}.txt").write_text(f"{n}\n")
        git(seed, "add", f"{n}.txt")
        git(seed, "commit", "-qm",
            f"Change {n}\n\nSigned-off-by: Lane Bot <lane@example.invalid>\n")
    git(seed, "push", "-q", str(origin), "HEAD:refs/heads/feature")
    git(seed, "push", "-q", str(origin), "HEAD:refs/pull/5/head")
    head = git(seed, "rev-parse", "HEAD")
    main = git(seed, "rev-parse", "main")

    clone = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    git(clone, "config", "user.email", "lane@example.invalid")
    git(clone, "config", "user.name", "Lane Bot")
    return clone, head, main


def thread_node(path: str = "a.txt", line: int = 3, *, resolved: bool = False,
                outdated: bool = False, original_line: int | None = None,
                comments: int = 1, author: str = IMPLEMENTER) -> dict:
    """A `reviewThreads` node, as GraphQL reports it.

    `comments` is `comments.totalCount`: one is the finding alone, more is
    a finding with a reply. An outdated thread's `line` is null; its
    `originalLine` is where it was written.
    """
    nodes = [{"author": {"login": author}, "createdAt": "2026-01-02T00:00:00Z"}]
    if comments > 1:
        nodes.append({"author": {"login": REVIEWER},
                      "createdAt": "2026-01-03T00:00:00Z"})
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


def review(state: str, commit_id: str, *, id: int = 1,
           submitted_at: str = "2026-01-02T00:00:00Z",
           login: str = REVIEWER) -> dict:
    """One review, as the API prints it.

    The reviewer's login is carried: only the reviewer's own reviews
    count as the review, so a review without one is no review.
    """
    return {"state": state, "commit_id": commit_id, "id": id,
            "submitted_at": submitted_at, "user": {"login": login}}


def setup(tmp: Path, *, labels=None, pending=False, reviews=None,
          contributing=None, feature_message: str = FEATURE_MESSAGE,
          feature_author: dict = FEATURE_AUTHOR,
          check_runs=None, issue_comments=None, inline_comments=None,
          status=None, rollup=None, threads=None, thread_pages=None):
    """A clone, a fake gh, and the environment that runs the driver."""
    clone, head, main = make_repo(tmp, feature_message=feature_message,
                                  feature_author=feature_author)

    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    gh = fakebin / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)

    # The reviewer's login is named in the environment: the repository's
    # owner is a temporary directory's name here, which is no login
    # anybody could type.
    if reviews is None:
        reviews = [{"state": "APPROVED", "commit_id": head, "id": 1,
                    "submitted_at": "2026-01-02T00:00:00Z",
                    "user": {"login": REVIEWER}}]

    # The checks are the pull request's own rollup, the way
    # `gh pr view --json statusCheckRollup` reports them. A check run
    # carries a `status` and a `conclusion`; a commit status carries a
    # `state`, and the two are told apart by their `__typename`.
    if rollup is None:
        rollup = []
        if check_runs is None:
            check_runs = [{
                "__typename": "CheckRun",
                "name": "ci/build",
                "status": "IN_PROGRESS" if pending else "COMPLETED",
                "conclusion": None if pending else "SUCCESS"}]
        for run in check_runs:
            entry = dict(run)
            entry.setdefault("__typename", "CheckRun")
            rollup.append(entry)
        if status is not None:
            for ctx in status.get("statuses", []):
                entry = dict(ctx)
                entry["__typename"] = "StatusContext"
                rollup.append(entry)

    data = tmp / "gh.json"
    data.write_text(json.dumps({
        "pr": {
            "head": {"sha": head},
            "base": {"ref": "main"},
            "labels": [{"name": n} for n in (labels or [])],
        },
        "statusCheckRollup": rollup,
        "reviews": reviews,
        "issue_comments": issue_comments if issue_comments is not None else [],
        "inline_comments": inline_comments if inline_comments is not None else [],
        # The review threads GraphQL answers. No threads is the ordinary
        # case: a pull request nobody annotated inline.
        "graphql_threads": (thread_pages if thread_pages is not None
                            else [thread_page(threads or [])]),
    }))

    if contributing is not None:
        (clone / "CONTRIBUTING.md").write_text(contributing)

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LANE_GIT_", "MERGE_CHECK_", "GH_",
                                "REVIEWER_LOGIN", "IMPLEMENTER_LOGINS"))}
    env.update({
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "FAKE_GH_DATA": str(data),
        "FAKE_GH_LOG": str(tmp / "gh.log"),
        "LANE_GIT_NAME": "Lane Bot",
        "LANE_GIT_EMAIL": "lane@example.invalid",
        "REVIEWER_LOGIN": REVIEWER,
    })
    return clone, head, main, env


def setup_multi_commit(tmp: Path, oldest_author: dict, **kwargs):
    """A clone whose branch carries three commits, and the environment
    that runs the driver against it.

    The oldest of the three carries `oldest_author`.
    """
    clone, head, main = make_multi_commit_repo(tmp, oldest_author)

    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    gh = fakebin / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)

    reviews = kwargs.pop("reviews", None)
    if reviews is None:
        reviews = [{"state": "APPROVED", "commit_id": head, "id": 1,
                    "submitted_at": "2026-01-02T00:00:00Z",
                    "user": {"login": REVIEWER}}]

    rollup = kwargs.pop("rollup", None)
    if rollup is None:
        rollup = [{"__typename": "CheckRun", "name": "ci/build",
                   "status": "COMPLETED", "conclusion": "SUCCESS"}]

    data = tmp / "gh.json"
    data.write_text(json.dumps({
        "pr": {
            "head": {"sha": head},
            "base": {"ref": "main"},
            "labels": [{"name": n} for n in (kwargs.pop("labels", None) or [])],
        },
        "statusCheckRollup": rollup,
        "reviews": reviews,
        "issue_comments": [],
        "inline_comments": [],
    }))

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LANE_GIT_", "MERGE_CHECK_", "GH_",
                                "REVIEWER_LOGIN", "IMPLEMENTER_LOGINS"))}
    env.update({
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "FAKE_GH_DATA": str(data),
        "FAKE_GH_LOG": str(tmp / "gh.log"),
        "LANE_GIT_NAME": "Lane Bot",
        "LANE_GIT_EMAIL": "lane@example.invalid",
        "REVIEWER_LOGIN": REVIEWER,
    })
    return clone, head, main, env


def run(env, clone: Path, *extra: str):
    return subprocess.run([str(BIN), "5", *extra],
                          cwd=str(clone), env=env, capture_output=True, text=True)


def case_the_driver_prints_the_merge_command(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp)
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "driver: a ready pull request was refused",
                 f"rc={r.returncode} stderr={r.stderr}")
            return
        if "READY" not in r.stdout:
            fail(failures, "driver: the output does not say READY", r.stdout)
        if "--match-head-commit" not in r.stdout:
            fail(failures, "driver: the output has no merge command", r.stdout)
        if head not in r.stdout:
            fail(failures, "driver: the merge command does not name the head",
                 r.stdout)


def case_the_driver_refuses_a_needs_decision_label(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, labels=["needs-decision"])
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "driver: a needs-decision pull request was accepted",
                 r.stdout)
            return
        if "needs-decision" not in r.stderr:
            fail(failures, "driver: the refusal does not name the label",
                 r.stderr)


def case_the_driver_refuses_a_running_check(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, pending=True)
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "driver: a pull request with a running check was accepted",
                 r.stdout)
            return
        if "ci/build" not in r.stderr:
            fail(failures, "driver: the refusal does not name the check", r.stderr)


def case_the_driver_refuses_a_pending_commit_status(failures: list[str]) -> None:
    # A status context, not a check run, is still running. The rollup
    # carries a commit status as a `StatusContext` with a `state`, and
    # the shared pending helper reads that state.

    # A status left uncounted would go unnoticed and the pull request
    # would be called ready while it runs.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp)
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["statusCheckRollup"] = [
            {"__typename": "StatusContext",
             "context": "continuous-integration/jenkins",
             "state": "PENDING"}]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "driver: a pull request with a pending commit "
                           "status was accepted", r.stdout)
            return
        if "checks" not in r.stderr:
            fail(failures, "driver: the refusal does not name the checks "
                           "check", r.stderr)
        if "jenkins" not in r.stderr:
            fail(failures, "driver: the refusal does not name the status",
                 r.stderr)


def case_the_driver_accepts_a_branch_behind_the_base_that_merges_cleanly(failures: list[str]) -> None:
    # The base moves on after the clone was made: the branch forked
    # before it. The base commit edits another file, so the branch and
    # the base merge cleanly, and the driver must accept it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, main, env = setup(tmp)
        # Fetching here would make the clone's `origin/main` current and
        # hide a driver that forgot to fetch the base itself.
        seed = tmp / "seed"
        # The base moves on from main itself, not from the feature branch:
        # a commit made on top of feature would make the origin's main
        # point at the feature tip, and the branch would not be behind.
        git(seed, "checkout", "-q", "main")
        # Another file: the branch did not touch it, so nothing conflicts.
        (seed / "c.txt").write_text("base only\n")
        git(seed, "add", "c.txt")
        git(seed, "commit", "-qm", "main moves on")
        git(seed, "push", "-q", str(tmp / "origin.git"), "main:main")
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "driver: a branch behind the base that merges "
                           "cleanly was refused",
                 f"rc={r.returncode} stderr={r.stderr}")


def case_the_driver_refuses_a_branch_that_conflicts_with_the_base(failures: list[str]) -> None:
    # The base moves on and edits the same lines the branch edited. The
    # merge conflicts, so the driver refuses and names the file.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, main, env = setup(tmp)
        seed = tmp / "seed"
        git(seed, "checkout", "-q", "main")
        # The same lines the branch changed: the branch appended "two"
        # to a.txt, and so does the base.
        (seed / "a.txt").write_text("one\ntwo\nthree\n")
        git(seed, "add", "a.txt")
        git(seed, "commit", "-qm", "main moves on")
        git(seed, "push", "-q", str(tmp / "origin.git"), "main:main")
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "driver: a branch that conflicts with the base "
                           "was accepted", r.stdout)
            return
        if "base" not in r.stderr:
            fail(failures, "driver: the refusal does not name the base check",
                 r.stderr)
            return
        if "a.txt" not in r.stderr:
            fail(failures, "driver: the refusal does not name the conflicting "
                           "file", r.stderr)


def case_the_driver_refuses_an_add_add_conflict(failures: list[str]) -> None:
    # Both branches add the same new file, with different contents. The
    # base never held it, so the conflict list carries no stage-1 entry
    # for it. The refusal must still be a conflict naming the file.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, main, env = setup(tmp)
        seed = tmp / "seed"
        git(seed, "checkout", "-q", "main")
        # The branch added b.txt with "new" in it. The base adds the
        # same path with different contents: both added it, so the
        # merge cannot resolve it.
        (seed / "b.txt").write_text("different\n")
        git(seed, "add", "b.txt")
        git(seed, "commit", "-qm", "main adds the same file")
        git(seed, "push", "-q", str(tmp / "origin.git"), "main:main")
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "driver: a branch that adds a file the base also "
                           "adds was accepted", r.stdout)
            return
        if "base" not in r.stderr:
            fail(failures, "driver: the refusal does not name the base check",
                 r.stderr)
            return
        if "b.txt" not in r.stderr:
            fail(failures, "driver: the refusal does not name the conflicting "
                           "file", r.stderr)
            return
        # A conflict asks for a rebase. A merge that merely broke is
        # reported as an error, and names the wrong fix.
        if "rebase" not in r.stderr:
            fail(failures, "driver: an add/add conflict was not reported as "
                           "a conflict", r.stderr)


def case_the_driver_refuses_an_unknown_base_tip(failures: list[str]) -> None:
    # The merge cannot be run: git cannot merge the base tip. An
    # unknown result never passes.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, main, env = setup(tmp)
        seed = tmp / "seed"
        git(seed, "checkout", "-q", "main")
        (seed / "c.txt").write_text("base only\n")
        git(seed, "add", "c.txt")
        git(seed, "commit", "-qm", "main moves on")
        git(seed, "push", "-q", str(tmp / "origin.git"), "main:main")
        # The base tip the driver resolves is no commit: the merge
        # cannot run, and the refusal names the error.
        r = run(env, clone, "--base", "refs/heads/no-such-base")
        if r.returncode == 0:
            fail(failures, "driver: an unknown base tip was accepted",
                 r.stdout)
            return
        if "base" not in r.stderr:
            fail(failures, "driver: the refusal does not name the base check",
                 r.stderr)


def case_the_driver_refuses_a_wrong_author(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # The commit is authored by another person: the identity check
        # reads the commit from the object database, so the author is a
        # property of the commit, not of a report about it.
        clone, _head, _main, env = setup(
            tmp,
            feature_author={"name": "Somebody Else",
                            "email": "other@example.invalid"})
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "driver: a commit by another author was accepted",
                 r.stdout)
            return
        if "identity" not in r.stderr:
            fail(failures, "driver: the refusal does not name the identity check",
                 r.stderr)


def case_the_driver_requires_a_signoff_only_where_the_repository_asks(failures: list[str]) -> None:
    # A repository that declares the DCO rule: the sign-off is required.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(
            tmp, feature_message="Fix the thing\n",
            contributing="Every commit must carry a DCO sign-off.\n")
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "signoff: an unsigned commit was accepted in a "
                           "repository that declares the DCO rule", r.stdout)
            return
        if "signoff" not in r.stderr:
            fail(failures, "signoff: the refusal does not name the signoff check",
                 r.stderr)

    # A repository that says nothing about sign-offs is asked for none.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, feature_message="Fix the thing\n")
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "signoff: a repository that asks for no sign-off was "
                           "refused one", f"rc={r.returncode} stderr={r.stderr}")


def case_the_driver_refuses_a_file_added_since_the_review(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp)
        # The reviewer read an earlier commit, before b.txt appeared.
        older = git(clone, "rev-parse", "origin/main")
        reviews = [review("APPROVED", older)]
        env["FAKE_GH_DATA"] = str(tmp / "gh.json")
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["reviews"] = reviews
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "files: a file added since the review was accepted",
                 r.stdout)
            return
        if "files" not in r.stderr:
            fail(failures, "files: the refusal does not name the files check",
                 r.stderr)


def case_the_driver_reads_the_latest_approval_by_time(failures: list[str]) -> None:
    # Two approvals: the reviewer first approved the base (before b.txt
    # existed), then the head. The file set is compared against the
    # latest approval, by its timestamp. The approval that read the
    # file is the one that counts.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, main, env = setup(tmp)
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["reviews"] = [
            review("APPROVED", main, id=1, submitted_at="2026-01-01T00:00:00Z"),
            review("APPROVED", head, id=2, submitted_at="2026-01-02T00:00:00Z"),
        ]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "approvals: a pull request approved at its head "
                           "was refused", f"rc={r.returncode} "
                                          f"stderr={r.stderr}")


def case_the_driver_reads_the_latest_approval_when_api_order_lies(failures: list[str]) -> None:
    # The same two approvals, returned newest first: the decision must
    # not depend on the order the list came back in.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, main, env = setup(tmp)
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["reviews"] = [
            review("APPROVED", head, id=2, submitted_at="2026-01-02T00:00:00Z"),
            review("APPROVED", main, id=1, submitted_at="2026-01-01T00:00:00Z"),
        ]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "approvals: the decision changed with the "
                           "API's list order", f"rc={r.returncode} "
                                               f"stderr={r.stderr}")


def case_a_later_push_after_the_last_approval_is_refused(failures: list[str]) -> None:
    # The reviewer approved the head, and the implementer pushed one
    # more commit after that. The latest approval is now the older sha,
    # and the head has changed since the review: the file check must
    # refuse it, whatever the approval order.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp)
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        # The approval names the head, but the pull request the API
        # reports has one more commit on top: its head is past the
        # approved sha.

        # The fixture's origin mirrors that newer tip, so the git facts
        # and the API facts agree on a head that is no the one the
        # reviewer read.
        seed = tmp / "seed"
        git(seed, "checkout", "-q", "feature")
        (seed / "c.txt").write_text("later\n")
        git(seed, "add", "c.txt")
        git(seed, "commit", "-qm",
            "one more commit\n\nSigned-off-by: Lane Bot <lane@example.invalid>\n")
        git(seed, "push", "-q", str(tmp / "origin.git"), "HEAD:refs/pull/5/head")
        git(seed, "push", "-q", str(tmp / "origin.git"), "HEAD:refs/heads/feature")
        new_head = git(seed, "rev-parse", "HEAD")
        data["pr"]["head"]["sha"] = new_head
        data["reviews"] = [
            review("APPROVED", head, id=2, submitted_at="2026-01-02T00:00:00Z"),
        ]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "approval: a push after the last approval was "
                           "accepted", r.stdout)
            return
        if "files" not in r.stderr:
            fail(failures, "approval: the refusal does not name the files "
                           "check", r.stderr)


def case_the_driver_refuses_a_failed_check(failures: list[str]) -> None:
    # A check that concluded `failure` refuses the merge, and the
    # refusal names it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, check_runs=[
            {"name": "ci/build", "status": "COMPLETED", "conclusion": "FAILURE"}])
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "checks: a failed check was accepted", r.stdout)
            return
        if "ci/build" not in r.stderr:
            fail(failures, "checks: the refusal does not name the check",
                 r.stderr)


def case_the_driver_refuses_a_failed_status(failures: list[str]) -> None:
    # The same for a commit status: the rollup carries the commit
    # statuses beside the check runs, and they are a second source of
    # the same verdicts.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, status={
            "statuses": [{"context": "ci/legacy", "state": "FAILURE"}]})
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "checks: a failed status was accepted", r.stdout)
            return
        if "ci/legacy" not in r.stderr:
            fail(failures, "checks: the refusal does not name the status",
                 r.stderr)


# The rollup a real pull request reports, recorded from
# `gh pr view 191 --json statusCheckRollup` and trimmed to the fields the
# decision reads. A hand-made fixture invents a shape the API never
# answers, and the suite then proves nothing about the real one.
REAL_ROLLUP = [{
    "__typename": "CheckRun",
    "name": "Gates",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "workflowName": "Gates",
}]


def case_the_driver_accepts_a_real_check_rollup(failures: list[str]) -> None:
    # A rollup as GitHub reports it: one completed, successful check run
    # and nothing else. The pull request is ready.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp, rollup=REAL_ROLLUP)
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "checks: a pull request whose real check "
                           "rollup is green was refused",
                 f"rc={r.returncode} stderr={r.stderr}")
            return
        if "READY" not in r.stdout:
            fail(failures, "checks: the output does not say READY", r.stdout)
        if head not in r.stdout:
            fail(failures, "checks: the merge command does not name the head",
                 r.stdout)


def case_the_driver_refuses_a_real_rollup_that_is_still_running(failures: list[str]) -> None:
    # The same rollup with the run still going: the pull request is not
    # ready, and the refusal names the check.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, rollup=[{
            "__typename": "CheckRun",
            "name": "Gates",
            "status": "IN_PROGRESS",
            "conclusion": None,
            "workflowName": "Gates",
        }])
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "checks: a pull request whose real check "
                           "rollup is running was accepted", r.stdout)
            return
        if "Gates" not in r.stderr:
            fail(failures, "checks: the refusal does not name the check",
                 r.stderr)


def case_the_driver_accepts_a_superseded_cancelled_run(failures: list[str]) -> None:
    # The shape `concurrency: cancel-in-progress` leaves on a head: one
    # cancelled run of a job and one success of it. The cancelled run was
    # superseded, so it says nothing about the branch and the pull request
    # is ready.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp, rollup=[
            rollup_run(name="check", conclusion="CANCELLED",
                       started="2026-11-13T05:50:12Z"),
            rollup_run(name="check", conclusion="SUCCESS",
                       started="2026-11-13T06:11:27Z")])
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "checks: a check whose cancelled run was "
                           "superseded by a success was refused",
                 f"rc={r.returncode} stderr={r.stderr}")
            return
        if "READY" not in r.stdout:
            fail(failures, "checks: the output does not say READY", r.stdout)
        if head not in r.stdout:
            fail(failures, "checks: the merge command does not name the head",
                 r.stdout)


def case_the_driver_reads_every_commit(failures: list[str]) -> None:
    # Three commits on the branch, and the oldest one carries a wrong
    # identity. Every commit is read, so the oldest is refused and
    # named.

    # Reading only the newest commit leaves the oldest one unchecked,
    # and a pull request with a commit by somebody else merges.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup_multi_commit(
            tmp, {"name": "Somebody Else", "email": "other@example.invalid"})
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "commits: a pull request whose oldest commit "
                           "carries a wrong identity was accepted", r.stdout)
            return
        if "identity" not in r.stderr:
            fail(failures, "commits: the refusal does not name the identity "
                           "check", r.stderr)
            return
        detail = " ".join(r.stderr.splitlines())
        if "Somebody Else" not in detail:
            fail(failures, "commits: the refusal does not name the author "
                           "of the oldest commit", detail)


def case_the_driver_counts_every_commit(failures: list[str]) -> None:
    # Three commits on the branch, all by the lane's identity. The
    # one-commit rule does not fire: three commits are not one, so the
    # pull request is not held to the single commit's parent.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup_multi_commit(
            tmp, {"name": "Lane Bot", "email": "lane@example.invalid"})
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "commits: a pull request with three commits by "
                           "the right identity was refused",
                 f"rc={r.returncode} stderr={r.stderr}")
            return
        if "READY" not in r.stdout:
            fail(failures, "commits: the output does not say READY", r.stdout)


def case_the_stub_refuses_a_read_sent_as_a_post(failures: list[str]) -> None:
    # `gh api` sends a request with a field (`-f` or `-F`) as a POST
    # unless the method is named, and the check-runs endpoint answers a
    # POST with 404. The stub refuses such a call, so a read sent as a
    # POST fails in the suite too.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp)
        gh = tmp / "fakebin" / "gh"
        for args, expect_refused in (
                (["api", "repos/O/R/commits/abc/check-runs",
                  "-f", "per_page=100"], True),
                (["api", "repos/O/R/commits/abc/check-runs",
                  "-F", "per_page=100"], True),
                (["api", "repos/O/R/commits/abc/check-runs",
                  "-f", "per_page=100", "-X", "GET"], False),
                (["api", "repos/O/R/commits/abc/check-runs"], False),
        ):
            r = subprocess.run([str(gh), *args], env=env,
                               capture_output=True, text=True)
            refused = r.returncode != 0
            if refused != expect_refused:
                fail(failures, "stub: a call carrying a field without "
                               "-X GET was "
                               f"{'not ' if not expect_refused else ''}"
                               "refused",
                     f"args={args} rc={r.returncode} stderr={r.stderr}")
        # The driver itself never sends such a call: it reads the pull
        # request's rollup. The driver runs first, so its calls are in
        # the log; the probes above add theirs after, and are told
        # apart by their endpoint.
        run(env, clone)
        log = Path(env["FAKE_GH_LOG"]).read_text().splitlines()
        driver_calls = [json.loads(line) for line in log
                        if json.loads(line)[:2] !=
                        ["api", "repos/O/R/commits/abc/check-runs"]]
        for args in driver_calls:
            # GraphQL reads are POSTs by design, so only the REST reads
            # are held to the guard.
            if args and args[0] == "api" and args[1:2] != ["graphql"]:
                has_field = any(a in ("-f", "-F") for a in args)
                names_get = any(a == "GET" for a in args)
                if has_field and not names_get:
                    fail(failures, "stub: the driver sent a read as a POST",
                         str(args))
        if not any(args[:2] == ["pr", "view"] for args in driver_calls):
            fail(failures, "stub: the driver did not read the pull "
                           "request's check rollup", "\n".join(log))


def case_the_driver_refuses_when_the_reviewer_has_not_reviewed(failures: list[str]) -> None:
    # There is nothing to compare the head against, and comparing the
    # head with itself cannot fail.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, reviews=[])
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "review: a pull request the reviewer never "
                          "reviewed was accepted", r.stdout)
            return
        if "not reviewed" not in r.stderr and "reviewed" not in r.stderr:
            fail(failures, "review: the refusal does not say the reviewer "
                           "has not reviewed", r.stderr)


def case_the_driver_refuses_a_file_added_after_a_changes_requested(failures: list[str]) -> None:
    # The reviewer's latest review is the one that asked for changes,
    # and the file set is compared against the commit it read.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp)
        older = git(clone, "rev-parse", "origin/main")
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["reviews"] = [review("CHANGES_REQUESTED", older, id=1,
                                  submitted_at="2026-01-01T00:00:00Z")]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "review: a file added since the reviewer's "
                           "changes-requested was accepted", r.stdout)
            return
        if "files" not in r.stderr:
            fail(failures, "review: the refusal does not name the files "
                           "check", r.stderr)


def case_the_driver_ignores_an_approval_by_another_login(failures: list[str]) -> None:
    # An approval from somebody else says nothing about what the
    # reviewer read, so it is no the reviewed commit. The sha is the
    # head, so the only thing wrong with the review is who wrote it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp)
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["reviews"] = [review("APPROVED", head, id=1, login="somebody-else")]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "review: another login's approval was taken as "
                           "the review", r.stdout)
            return
        if "not reviewed" not in r.stderr:
            fail(failures, "review: the refusal does not say the reviewer "
                           "has not reviewed", r.stderr)


def case_the_driver_refuses_without_a_lane_identity(failures: list[str]) -> None:
    # The expected identity is the repository's rule. Without
    # LANE_GIT_NAME and LANE_GIT_EMAIL no rule applies. The checkout's
    # config is no one: it says what this machine would commit as.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp)
        env.pop("LANE_GIT_NAME", None)
        env.pop("LANE_GIT_EMAIL", None)
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "identity: a pull request was accepted with no "
                           "expected identity", r.stdout)
            return
        if "LANE_GIT_NAME" not in r.stderr:
            fail(failures, "identity: the refusal does not name the "
                           "setting", r.stderr)


def case_the_driver_refuses_a_third_party_comment(failures: list[str]) -> None:
    # A person's comment is input to the loop. All three channels are
    # read.
    for channel in ("issue_comments", "inline_comments"):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            clone, _head, _main, env = setup(tmp, **{channel: [
                {"user": {"login": "somebody-else"}, "body": "a comment"}]})
            r = run(env, clone)
            if r.returncode == 0:
                fail(failures, "comments: a third party's comment in the "
                               f"{channel} was accepted", r.stdout)
                return
            if "somebody-else" not in r.stderr:
                fail(failures, "comments: the refusal does not name the "
                               "commenter", r.stderr)


def case_the_driver_allows_the_loop_to_comment(failures: list[str]) -> None:
    # The reviewer's and the implementer's own comments are the loop's.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, issue_comments=[
            {"user": {"login": REVIEWER}, "body": "a comment"},
            {"user": {"login": IMPLEMENTER}, "body": "a comment"}])
        env["IMPLEMENTER_LOGINS"] = IMPLEMENTER
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "comments: the loop's own comments stopped the "
                           "merge", f"rc={r.returncode} stderr={r.stderr}")


def case_an_answered_third_party_comment_passes(failures: list[str]) -> None:
    # A comment blocks only while it is unanswered. The reviewer's review
    # came after it, so the comment was answered and the merge waits for
    # nothing.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp, issue_comments=[
            {"user": {"login": "the-owner"}, "body": "OK to merge",
             "created_at": "2026-01-01T00:00:00Z"}])
        env["FAKE_GH_DATA"] = str(tmp / "gh.json")
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["reviews"] = [{"state": "APPROVED", "commit_id": head, "id": 2,
                            "submitted_at": "2026-01-03T00:00:00Z",
                            "user": {"login": REVIEWER}}]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "comments: an answered comment still stopped the "
                           "merge", f"rc={r.returncode} stderr={r.stderr}")


def case_an_unanswered_third_party_comment_is_refused(failures: list[str]) -> None:
    # The owner's comment came after the reviewer's last post, so nothing
    # answered it. The refusal names the login and when it was made, so
    # the reader can find it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp, issue_comments=[
            {"user": {"login": "the-owner"}, "body": "one more thing",
             "created_at": "2026-01-04T00:00:00Z"}])
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["reviews"] = [{"state": "APPROVED", "commit_id": head, "id": 2,
                            "submitted_at": "2026-01-03T00:00:00Z",
                            "user": {"login": REVIEWER}}]
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "comments: an unanswered comment was accepted",
                 r.stdout)
            return
        if "the-owner" not in r.stderr:
            fail(failures, "comments: the refusal does not name the "
                           "commenter", r.stderr)
        if "2026-01-04" not in r.stderr:
            fail(failures, "comments: the refusal does not say when the "
                           "comment was made", r.stderr)


def case_a_bot_comment_passes(failures: list[str]) -> None:
    # A bot is no person. Its comment is never a third party's.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, issue_comments=[
            {"user": {"login": "ci-bot", "type": "Bot"},
             "body": "the build finished",
             "created_at": "2026-01-04T00:00:00Z"}])
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "comments: a bot's comment stopped the merge",
                 f"rc={r.returncode} stderr={r.stderr}")


def case_hardcoded_does_not_ask_for_a_signoff(failures: list[str]) -> None:
    # "DCO" is matched as a word. A substring match found it inside
    # "hardcoded", and the contribution file then asked for a sign-off
    # nobody had asked for. The commit carries none, so the spurious
    # requirement refuses it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(
            tmp,
            contributing="# Contributing\n\nThe value is hardcoded on purpose.\n",
            feature_message="Fix the thing\n")
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "dco: 'hardcoded' turned on the sign-off "
                           "requirement", f"rc={r.returncode} stderr={r.stderr}")


def case_the_driver_refuses_a_head_that_moved_during_the_check(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp)
        # The pull request's head advances after the API read: the
        # reported sha and the ref the base repository mirrors
        # disagree.

        # A verdict on the stale facts would be a verdict on a pull
        # request that has ceased to exist.
        seed = tmp / "seed"
        git(seed, "checkout", "-q", "feature")
        (seed / "c.txt").write_text("more\n")
        git(seed, "add", "c.txt")
        git(seed, "commit", "-qm",
            "one more commit\n\nSigned-off-by: Lane Bot <lane@example.invalid>\n")
        git(seed, "push", "-q", str(tmp / "origin.git"), "HEAD:refs/pull/5/head")
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "head move: a pull request whose head moved "
                           "during the check was accepted", r.stdout)
            return
        if "head moved" not in r.stderr:
            fail(failures, "head move: the failure does not name the move",
                 r.stderr)


def case_the_driver_refuses_an_unanswered_thread(failures: list[str]) -> None:
    # The thread is read through GraphQL and refused end to end, and the
    # refusal lands beside the label's: every check runs whatever else
    # refused.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(
            tmp, labels=["needs-decision"],
            threads=[thread_node("src/a.py", 42, author=IMPLEMENTER)])
        # The thread's author is declared the loop's own: it still
        # blocks, because an author is no say about whether a finding is
        # answered.
        env["IMPLEMENTER_LOGINS"] = IMPLEMENTER
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "threads: an unanswered thread was accepted",
                 r.stdout)
            return
        if "src/a.py:42" not in r.stderr:
            fail(failures, "threads: the refusal does not say where the "
                           "thread is", r.stderr)
        if IMPLEMENTER not in r.stderr:
            fail(failures, "threads: the refusal does not name who started "
                           "the thread", r.stderr)
        if "needs-decision" not in r.stderr:
            fail(failures, "threads: the thread refusal hid the label "
                           "refusal", r.stderr)


def case_the_driver_follows_the_thread_cursor_to_the_last_page(failures: list[str]) -> None:
    # A long review round passes 100 threads and GraphQL answers a
    # hundred a page. Reading only the first page calls the pull request
    # ready over a finding on the second.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        answered = [thread_node(f"f{n}.txt", n + 1, comments=2)
                    for n in range(100)]
        clone, _head, _main, env = setup(tmp, thread_pages=[
            thread_page(answered, next_cursor="cur-2"),
            thread_page([thread_node("late.txt", 7, author=IMPLEMENTER)])])
        r = run(env, clone)
        if r.returncode == 0:
            fail(failures, "threads: an unanswered thread on the second page "
                           "was accepted", r.stdout)
            return
        if "late.txt:7" not in r.stderr:
            fail(failures, "threads: the refusal does not name the thread on "
                           "the second page", r.stderr)


def case_the_driver_fails_when_review_threads_cannot_be_read(failures: list[str]) -> None:
    # "Could not check" is never "nothing to answer": the run ends the
    # way any failed fetch does, naming what could not be read.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp)
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["graphql_fail"] = True
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode != 1:
            fail(failures, "threads: a failed thread fetch did not exit 1",
                 f"rc={r.returncode} stdout={r.stdout}")
            return
        if not r.stderr.startswith("ERR "):
            fail(failures, "threads: the failure is not reported as a fetch "
                           "error", r.stderr)
        if "review threads" not in r.stderr:
            fail(failures, "threads: the failure does not name what could "
                           "not be read", r.stderr)


def case_the_driver_passes_a_pull_request_with_no_threads(failures: list[str]) -> None:
    # An empty thread list is nothing to answer, not a fetch failure.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp, threads=[])
        r = run(env, clone)
        if r.returncode != 0:
            fail(failures, "threads: a pull request with no review threads "
                           "was refused",
                 f"rc={r.returncode} stderr={r.stderr}")


def case_the_driver_runs_in_a_named_worktree(failures: list[str]) -> None:
    # The worktree is the lane's checkout, not the caller's directory.
    # Run from a different directory, naming the checkout as the
    # worktree: the git facts must come from the named checkout.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _main, env = setup(tmp)
        r = subprocess.run([str(BIN), "5", str(clone)], cwd=str(tmp),
                           env=env, capture_output=True, text=True)
        if r.returncode != 0:
            fail(failures, "driver: a ready pull request run in a named "
                           "worktree was refused",
                 f"rc={r.returncode} stderr={r.stderr}")
            return
        if head not in r.stdout:
            fail(failures, "driver: the merge command does not name the head "
                           "when the worktree is named", r.stdout)


def case_a_usage_error_exits_2(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp)
        for args in (["not-a-number"], ["5", "--no-such-flag"],
                     ["5", "--name", "Lane Bot"]):
            r = subprocess.run([str(BIN), *args], cwd=str(clone), env=env,
                               capture_output=True, text=True)
            if r.returncode != 2:
                fail(failures, "usage: a usage error did not exit 2",
                     f"args={args} rc={r.returncode}")


def case_an_unreadable_pull_request_exits_1(failures: list[str]) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, _head, _main, env = setup(tmp)
        # The gh stub answers with something that is no pull request.
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["pr"] = {"head": {}}
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        r = run(env, clone)
        if r.returncode != 1:
            fail(failures, "fetch: an unreadable pull request did not exit 1",
                 f"rc={r.returncode} stderr={r.stderr}")
            return
        if not r.stderr.startswith("ERR "):
            fail(failures, "fetch: the failure does not start with ERR", r.stderr)


def main() -> int:
    failures: list[str] = []
    case_a_ready_pull_request_passes(failures)
    case_a_branch_on_the_base_passes(failures)
    case_a_branch_behind_the_base_that_merges_cleanly_passes(failures)
    case_a_branch_behind_the_base_that_conflicts_is_refused(failures)
    case_a_branch_behind_the_base_whose_merge_failed_is_refused(failures)
    case_a_one_commit_pull_request_must_sit_on_the_base(failures)
    case_a_changed_file_is_refused(failures)
    case_a_removed_file_is_refused(failures)
    case_a_changed_file_set_is_the_same_change(failures)
    case_a_wrong_author_is_refused(failures)
    case_a_missing_signoff_is_refused(failures)
    case_a_signoff_by_another_person_is_refused(failures)
    case_two_signoffs_are_refused(failures)
    case_a_signoff_mentioned_in_prose_is_not_a_signoff(failures)
    case_a_repository_that_asks_for_no_signoff_is_not_checked(failures)
    case_pending_checks_are_refused(failures)
    case_a_failed_check_is_refused(failures)
    case_only_success_neutral_and_skipped_pass(failures)
    case_a_running_check_is_not_a_failure(failures)
    case_a_superseded_check_run_is_not_evidence(failures)
    case_a_superseded_run_of_another_check_is_not_dropped(failures)
    case_a_wrong_committer_is_refused(failures)
    case_a_third_party_comment_is_refused(failures)
    case_an_unanswered_thread_by_the_implementers_login_is_refused(failures)
    case_an_unanswered_thread_by_the_reviewer_is_refused(failures)
    case_a_replied_thread_passes(failures)
    case_a_resolved_thread_passes(failures)
    case_an_outdated_unanswered_thread_names_where_it_was(failures)
    case_no_review_threads_pass(failures)
    case_a_needs_decision_label_is_refused(failures)
    case_every_check_runs_after_one_fails(failures)
    case_the_trailer_needs_its_own_line(failures)
    case_the_driver_prints_the_merge_command(failures)
    case_the_driver_refuses_a_needs_decision_label(failures)
    case_the_driver_refuses_a_running_check(failures)
    case_the_driver_refuses_a_pending_commit_status(failures)
    case_the_driver_refuses_a_failed_check(failures)
    case_the_driver_refuses_a_failed_status(failures)
    case_the_driver_accepts_a_real_check_rollup(failures)
    case_the_driver_refuses_a_real_rollup_that_is_still_running(failures)
    case_the_driver_accepts_a_superseded_cancelled_run(failures)
    case_the_driver_reads_every_commit(failures)
    case_the_driver_counts_every_commit(failures)
    case_the_stub_refuses_a_read_sent_as_a_post(failures)
    case_the_driver_refuses_when_the_reviewer_has_not_reviewed(failures)
    case_the_driver_refuses_a_file_added_after_a_changes_requested(failures)
    case_the_driver_ignores_an_approval_by_another_login(failures)
    case_the_driver_refuses_without_a_lane_identity(failures)
    case_the_driver_refuses_a_third_party_comment(failures)
    case_an_answered_third_party_comment_passes(failures)
    case_an_unanswered_third_party_comment_is_refused(failures)
    case_a_bot_comment_passes(failures)
    case_the_driver_allows_the_loop_to_comment(failures)
    case_hardcoded_does_not_ask_for_a_signoff(failures)
    case_the_driver_accepts_a_branch_behind_the_base_that_merges_cleanly(failures)
    case_the_driver_refuses_a_branch_that_conflicts_with_the_base(failures)
    case_the_driver_refuses_an_add_add_conflict(failures)
    case_the_driver_refuses_an_unknown_base_tip(failures)
    case_the_driver_refuses_a_wrong_author(failures)
    case_the_driver_requires_a_signoff_only_where_the_repository_asks(failures)
    case_the_driver_refuses_a_file_added_since_the_review(failures)
    case_the_driver_reads_the_latest_approval_by_time(failures)
    case_the_driver_reads_the_latest_approval_when_api_order_lies(failures)
    case_a_later_push_after_the_last_approval_is_refused(failures)
    case_the_driver_refuses_a_head_that_moved_during_the_check(failures)
    case_the_driver_refuses_an_unanswered_thread(failures)
    case_the_driver_follows_the_thread_cursor_to_the_last_page(failures)
    case_the_driver_fails_when_review_threads_cannot_be_read(failures)
    case_the_driver_passes_a_pull_request_with_no_threads(failures)
    case_the_driver_runs_in_a_named_worktree(failures)
    case_a_usage_error_exits_2(failures)
    case_an_unreadable_pull_request_exits_1(failures)

    if failures:
        print("FAIL", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
