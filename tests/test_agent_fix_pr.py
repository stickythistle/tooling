#!/usr/bin/env python3
"""Pin what agent-fix-pr pushes, refuses, and records for one review round.

The contract: a round pushes only commits the lane added on top of the PR's
head, fast-forward, after the gate passes; replies are posted only after that
push, or alone when the lane pushed back on everything, one malformed entry
never costing the others; every round, pushed or not,
is one ledger line, and the ledger caps the rounds.

The PR lives in a local bare repository standing in for origin. `gh` is a fake
that answers from canned JSON through the real `jq`, the implementer is a fake
that does whatever the test's FAKE_LANE says, and the gate is a fake exiting
FAKE_GATE_RC.

Run directly: `python3 tests/test_agent_fix_pr.py`.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "agent-fix-pr"
REPO = "owner/proj"
BRANCH = "feature"

# Driver env vars the operator's shell sets. Tests must not see them: a
# prior round had the operator's MODELS command run inside a test, asserting
# the operator's output as the ledger. A lane-pinned identity in the parent
# env would leak into the tests, so the GIT_*/LANE_GIT_ prefixes are
# stripped too. Strip once per env builder.
_DRIVER_ENV_PREFIXES = ("PIPELINE_", "AGENT_FIX_PR_", "HERMES_IMPLEMENT_",
                        "GIT_AUTHOR_", "GIT_COMMITTER_", "LANE_GIT_")


def _clean_env(**extra):
    """Return os.environ with driver settings stripped, plus extra.

    The driver's settings are the operator's, not the test's. A test
    asserting "no MODELS command was run" needs the keys absent, not a
    local copy that happens to unset them.
    """
    base = {k: v for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _DRIVER_ENV_PREFIXES)}
    base.update(extra)
    return base

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    canned = json.load(open(os.environ["FAKE_GH_DATA"]))
    def emit(doc):
        if "--jq" in args:
            flt = args[args.index("--jq") + 1]
            sys.stdout.write(subprocess.run(["jq", "-r", flt], input=json.dumps(doc),
                                            capture_output=True, text=True, check=True).stdout)
        else:
            print(json.dumps(doc))
    if args[:2] == ["pr", "view"]:
        emit(canned["pr"])
    elif args[0] == "api" and "-X" not in args:
        path = args[1]
        if path == "user":
            # The driver reads its own login before any write; a test
            # can make that read fail.
            if canned.get("user_fail"):
                print("gh: Bad credentials", file=sys.stderr)
                sys.exit(1)
            emit({"login": canned.get("user_login", "implementer")})
        elif path.endswith("/comments"):
            emit(canned.get("comments", []))
        elif "/reviews/" in path:
            emit(canned["review"])
        else:
            print(json.dumps({"path": path}))
    sys.exit(0)
""")

FAKE_IMPLEMENTER = textwrap.dedent("""\
    #!/usr/bin/env bash
    # --worktree is the tree the lane is handed.
    while [[ $# -gt 0 ]]; do [[ $1 == --worktree ]] && wt=$2; shift; done
    work=$(mktemp -d)
    echo "fixed it" >"$work/HANDOVER.md"
    cd "$wt"
    case "$FAKE_LANE" in
    commit)
        echo fix >>file.txt; git commit -qam "Fix the thing"
        cp "$FAKE_REPLIES" "$work/replies.json" ;;
    commit_with_author)
        # Shape 2 from the issue: an agent that overrides the author
        # with --author. The committer still picks up the lane's env
        # vars, but the author does not -- the verifier must catch
        # this and re-author.
        echo fix >>file.txt
        git commit -q --author="Wrong Author <wrong@example.invalid>" -am "Fix the thing with wrong author"
        cp "$FAKE_REPLIES" "$work/replies.json" ;;
    amend)
        echo fix >>file.txt; git commit -q --amend -am "Rewritten" ;;
    answer)
        cp "$FAKE_REPLIES" "$work/replies.json" ;;
    none) ;;
    esac
    echo "$work"
""")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_clone(tmp: Path, slug: str) -> tuple:
    """A bare origin at a path the driver's URL check accepts, plus a working
    clone whose remote URL has the same shape. Returns (clone, head) where
    head is the commit the bare origin publishes for BRANCH."""
    # Put the bare origin at tmp/<owner>/<name>.git: the URL ends with
    # /<slug>.git, mirroring the production path so the file:// URL is
    # recognised as for <slug> the same way https://github.com/<slug> would.
    owner, name = slug.split("/", 1)
    origin_dir = tmp / owner
    origin_dir.mkdir(exist_ok=True)
    origin = origin_dir / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    # An initial commit on the test branch so the driver can fetch it. A
    # throwaway local clone builds it once, then pushes to the bare origin.
    seed = tmp / f"seed-{slug.replace('/', '_')}"
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    (seed / "file.txt").write_text("start\n")
    subprocess.run(["git", "-C", str(seed), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "start"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(origin),
                    f"HEAD:refs/heads/{BRANCH}"], check=True)
    head = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    # The actual clone the driver reads. It points at the bare origin via a
    # file:// URL and has the test branch fetched but not checked out, so
    # the driver's worktree add can claim it.
    clone = tmp / f"clone-{slug.replace('/', '_')}"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "add",
                    "origin", f"file://{origin.resolve()}"], check=True)
    subprocess.run(["git", "-C", str(clone), "fetch", "-q", "origin"], check=True)
    return clone, head


def setup(tmp: Path, *, body="Closes #7.", lane="commit", gate_rc=0,
           replies=None, review_body="Please fix.", comments=None,
           repo=REPO, pr="5", user_login="implementer", user_fail=False):
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    wt = tmp / "wt"
    subprocess.run(["git", "clone", "-q", str(origin), str(wt)], check=True)
    git(wt, "config", "user.email", "t@example")
    git(wt, "config", "user.name", "t")
    git(wt, "checkout", "-q", "-b", BRANCH)
    (wt / "file.txt").write_text("start\n")
    git(wt, "add", "file.txt")
    git(wt, "commit", "-qm", "start")
    git(wt, "push", "-q", "-u", "origin", BRANCH)
    head = git(wt, "rev-parse", "HEAD")

    if comments is None:
        comments = [
            {"id": 11, "path": "file.txt", "line": 1, "body": "[P1] wrong", "in_reply_to_id": None},
            {"id": 12, "path": "file.txt", "line": 1, "body": "an earlier reply", "in_reply_to_id": 11},
        ]
    data = tmp / "gh.json"
    data.write_text(json.dumps({
        "pr": {"headRefName": BRANCH, "headRefOid": head, "body": body, "state": "OPEN"},
        "review": {"user": {"login": "reviewer"}, "commit_id": head, "body": review_body},
        "comments": comments,
        "user_login": user_login,
        "user_fail": user_fail,
    }))
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    for name, text in (("gh", FAKE_GH), ("implementer", FAKE_IMPLEMENTER)):
        (fakebin / name).write_text(text)
        (fakebin / name).chmod(0o755)
    gate = fakebin / "gate"
    gate.write_text(f"#!/bin/sh\nexit {gate_rc}\n")
    gate.chmod(0o755)

    state = tmp / "state"
    ledger_dir = state / "fix-pr" / f"{repo.replace('/', '_')}-{pr}"
    replies_src = tmp / "replies.json"
    if replies is None:
        replies = [{"comment_id": 11, "body": "Fixed."}]
    replies_src.write_text(json.dumps(replies))
    env = _clean_env(
        PATH=f"{fakebin}:{os.environ['PATH']}",
        AGENT_STATE_DIR=str(state),
        TOOLS_ENV=str(tmp / "no-env"),
        PIPELINE_IMPLEMENTER=str(fakebin / "implementer"),
        AGENT_FIX_PR_VERIFY=str(gate),
        FAKE_GH_LOG=str(tmp / "gh.log"),
        FAKE_GH_DATA=str(data),
        FAKE_LANE=lane,
        FAKE_REPLIES=str(replies_src))
    return wt, origin, head, env, ledger_dir


def run(env, wt, *extra, review=True, repo=REPO, pr="5", worktree=True):
    args = ["--review", "99"] if review else []
    if worktree:
        args += ["--worktree", str(wt)]
    return subprocess.run([str(BIN), "--repo", repo, "--pr", pr, *args, *extra],
                          env=env, capture_output=True, text=True)


def set_gate(tmp, script):
    gate = tmp / "fakebin" / "gate"
    gate.write_text(f"#!/bin/sh\n{script}\n")


def gh_calls(tmp):
    log = tmp / "gh.log"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def ledger(ledger_dir):
    return [json.loads(l) for l in (ledger_dir / "rounds.jsonl").read_text().splitlines()]


def test_pushes_a_fast_forward_and_replies_after():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp)
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        new = git(origin, "rev-parse", BRANCH)
        assert new != head and git(wt, "merge-base", "--is-ancestor", head, new) == ""
        entry = ledger(ld)[-1]
        assert entry["verdict"] == "pushed" and entry["replied"] == 1, entry
        posts = [c for c in gh_calls(tmp) if "-X" in c]
        assert posts and posts[0][3].endswith("/comments/11/replies"), posts


def test_brief_carries_only_top_level_comments():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        # The implementer sees the brief as its --extra argument.
        spy = tmp / "fakebin" / "implementer"
        spy.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >\"$SPY\"\nmktemp -d\n")
        env["SPY"] = str(tmp / "args")
        run(env, wt)
        args = (tmp / "args").read_text()
        assert "comment 11 at file.txt:1" in args, args
        assert "an earlier reply" not in args


def test_refuses_a_rewritten_history():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="amend")
        r = run(env, wt)
        assert r.returncode == 1
        assert git(origin, "rev-parse", BRANCH) == head
        assert ledger(ld)[-1]["verdict"] == "history rewritten"
        assert not [c for c in gh_calls(tmp) if "-X" in c], "no reply without a push"


def test_failed_gate_is_recorded_and_not_pushed():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, gate_rc=1)
        assert run(env, wt).returncode == 1
        assert git(origin, "rev-parse", BRANCH) == head
        assert ledger(ld)[-1]["gate"] == "fail"


def test_round_cap_counts_every_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, lane="none")
        for _ in range(2):
            assert run(env, wt, "--max-rounds", "2").returncode == 1
        r = run(env, wt, "--max-rounds", "2")
        assert r.returncode == 4, r.stderr
        assert len(ledger(ld)) == 2


def test_refuses_a_tree_not_at_the_pr_head():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        (wt / "file.txt").write_text("local\n")
        git(wt, "commit", "-qam", "someone's work")
        assert run(env, wt).returncode == 3


def test_refuses_a_pr_naming_no_issue():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp, body="No reference here.")
        assert run(env, wt).returncode == 2


def test_one_malformed_reply_does_not_cost_the_rest():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, replies=[
            {"comment_id": "not-a-number", "body": "x"},
            {"comment_id": 11},
            7,
            {"comment_id": 11, "body": "Fixed."},
        ])
        assert run(env, wt).returncode == 0
        assert ledger(ld)[-1]["replied"] == 1



def test_replies_alone_are_a_finished_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="answer", replies=[
            {"comment_id": 11, "body": "Left as is: file.txt:1 already says start."}])
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        assert git(origin, "rev-parse", BRANCH) == head
        entry = ledger(ld)[-1]
        assert entry["verdict"] == "answered" and entry["replied"] == 1, entry
        assert entry["pushed"] is False and entry["gate"] == "skipped", entry
        posts = [c for c in gh_calls(tmp) if "-X" in c]
        assert posts and posts[0][3].endswith("/comments/11/replies"), posts


def test_a_round_with_neither_commit_nor_replies_fails():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, lane="none")
        assert run(env, wt).returncode == 1
        assert ledger(ld)[-1]["verdict"] == "no commit"
        assert not [c for c in gh_calls(tmp) if "-X" in c]


def test_a_reply_naming_a_commit_the_branch_lacks_is_skipped():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, lane="answer", replies=[
            {"comment_id": 11, "body": "Fixed in 1a2b3c4."},
            {"comment_id": 11, "body": "Left as is: issue 1234567 settled it."}])
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        assert ledger(ld)[-1]["replied"] == 1
        assert "naming a commit the branch does not have" in r.stderr


def test_models_come_from_the_runtime_command_or_are_null():
    for script, expected in (("echo '[\"m1\", \"m2\"]'", ["m1", "m2"]),
                             ("echo 'not json'", None),
                             ("echo '{\"a\": 1}'", None),
                             (None, None)):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            wt, _, _, env, ld = setup(tmp)
            if script is not None:
                cmd = tmp / "fakebin" / "models"
                cmd.write_text(f"#!/bin/sh\n{script}\n")
                cmd.chmod(0o755)
                env["AGENT_FIX_PR_MODELS"] = str(cmd)
            assert run(env, wt).returncode == 0
            assert ledger(ld)[-1]["models"] == expected, (script, ledger(ld)[-1])


def test_models_setting_in_the_parent_environment_is_stripped():
    # Round 1 of this PR's review surfaced this: operator-set MODELS made
    # the (None, None) branch above see the operator's models, not null.
    # The plant below exercises setup()'s strip.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        leak = tmp / "leak_models"
        leak.write_text("#!/bin/sh\necho '[\"leak\"]'\n")
        leak.chmod(0o755)
        planted = "AGENT_FIX_PR_MODELS" not in os.environ
        if planted:
            os.environ["AGENT_FIX_PR_MODELS"] = str(leak)
        try:
            wt, _, _, env, ld = setup(tmp)
            # Sanity: the strip happened. If a future edit reverts it,
            # AGENT_FIX_PR_MODELS reappears here and the assertion below
            # names the leak in the operator's terms.
            assert "AGENT_FIX_PR_MODELS" not in env, (
                "setup() leaked AGENT_FIX_PR_MODELS from os.environ",
                sorted(k for k in env if "MODELS" in k))
            r = run(env, wt)
            assert r.returncode == 0, (r.stdout, r.stderr)
            entry = ledger(ld)[-1]
            assert entry["models"] is None, (
                "operator's MODELS_CMD reached the driver via setup()",
                entry)
            assert "leak" not in json.dumps(entry), (
                "ledger contains the leaked command's output", entry)
        finally:
            if planted:
                os.environ.pop("AGENT_FIX_PR_MODELS", None)


def test_implementer_setting_is_word_split():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        spy = tmp / "fakebin" / "implementer"
        spy.write_text("#!/usr/bin/env bash\necho \"$LANE_OPTION\" >\"$SPY\"\nmktemp -d\n")
        env["SPY"] = str(tmp / "seen")
        env["PIPELINE_IMPLEMENTER"] = f"env LANE_OPTION=from-runtime {spy}"
        run(env, wt)
        assert (tmp / "seen").read_text().strip() == "from-runtime"


def test_refuses_to_run_without_an_implementer():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp)
        del env["PIPELINE_IMPLEMENTER"]
        assert run(env, wt).returncode == 2
        assert not (ld / "rounds.jsonl").exists()


def test_resume_pushes_a_round_whose_gate_could_not_run():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, gate_rc=1)
        assert run(env, wt).returncode == 1
        lane_commit = git(wt, "rev-parse", "HEAD")
        set_gate(tmp, "exit 0")
        r = run(env, wt, "--resume", review=False)
        assert r.returncode == 0, r.stderr
        assert git(origin, "rev-parse", BRANCH) == lane_commit
        first, resumed = ledger(ld)
        assert resumed["resumed"] and resumed["round"] == first["round"] == 1
        assert resumed["start"] == head and resumed["replied"] == 1, resumed
        # The resumed line is not a round: the cap still has room for round 2,
        # once the fake PR reports the head the resume pushed.
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["pr"]["headRefOid"] = lane_commit
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        assert run(env, wt, "--max-rounds", "1").returncode == 4
        assert run(env, wt, "--max-rounds", "2").returncode == 0


def test_resume_refuses_with_nothing_unpushed():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        assert run(env, wt, "--resume", review=False).returncode == 2
        assert run(env, wt).returncode == 0
        assert run(env, wt, "--resume", review=False).returncode == 2


def test_resume_refuses_a_tree_that_moved():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp, gate_rc=1)
        run(env, wt)
        (wt / "file.txt").write_text("moved\n")
        git(wt, "commit", "-qam", "someone's next commit")
        set_gate(tmp, "exit 0")
        assert run(env, wt, "--resume", review=False).returncode == 3


def test_resume_accepts_replies_on_an_old_ledger_line_without_comment_ids():
    # The new validator keeps the older permissive rule (any numeric id or
    # "review") for ledger lines without comment_ids, so a --resume against
    # them still posts line-comment replies and `replied` counts them.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # Plant a ledger line as an old round would: a gate that failed
        # left the lane's commit unpushed, the round had at least one
        # line-comment reply, and comment_ids is missing.
        wt, _, head, env, ld = setup(tmp, gate_rc=1)
        run(env, wt)
        lane_commit = git(wt, "rev-parse", "HEAD")
        line_comment_reply = [{"comment_id": 11,
                               "body": "Left as is: file.txt:1 already says start."}]
        work_dir = Path(ledger(ld)[-1]["work"])
        (work_dir / "replies.json").write_text(json.dumps(line_comment_reply))
        # Replace the existing ledger line with one in the older shape, so
        # the resume validator cannot find comment_ids.
        legacy = {
            "round": 1, "review": "99", "pr_head": head,
            "start": head, "end": lane_commit, "verdict": "gate failed",
            "gate": "fail", "pushed": False,
            "models": None, "replied": 0,
            "work": str(work_dir), "resumed": False,
            "at": "2026-01-01T00:00:00Z",
        }
        (ld / "rounds.jsonl").write_text(json.dumps(legacy) + "\n")
        set_gate(tmp, "exit 0")
        r = run(env, wt, "--resume", review=False)
        assert r.returncode == 0, r.stderr
        # The line-comment reply went through as a threaded post — the old
        # rule kicked in via the comment_ids fallback, and `replied` counted it.
        threaded = [c for c in gh_calls(tmp) if "-X" in c
                    and c[3].endswith("/comments/11/replies")]
        assert len(threaded) == 1, threaded
        # The resume line still has the new comment_ids field, computed from
        # the fallback, so a follow-up round reads the same validator.
        resumed_line = ledger(ld)[-1]
        assert resumed_line["replied"] == 1, resumed_line
        assert resumed_line["comment_ids"] == ["review", "*"], resumed_line


def test_gate_finds_this_checkouts_tools_by_name():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp)
        set_gate(tmp, "command -v agent-fix-pr >/dev/null")
        assert run(env, wt).returncode == 0
        assert ledger(ld)[-1]["gate"] == "pass"


def test_detach_returns_at_once_and_the_round_lands_in_the_ledger():
    import time
    for resume in (False, True):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            wt, _, _, env, ld = setup(tmp, gate_rc=1 if resume else 0)
            if resume:
                run(env, wt)
                set_gate(tmp, "exit 0")
            # A lane slow enough that --status sees the round running.
            spy = tmp / "fakebin" / "implementer"
            spy.write_text("sleep 2\n" + spy.read_text().split("\n", 1)[1])
            spy.write_text("#!/usr/bin/env bash\n" + spy.read_text())
            extra = ("--resume", "--detach") if resume else ("--detach",)
            r = run(env, wt, *extra, review=not resume)
            assert r.returncode == 0, (resume, r.stdout, r.stderr)
            status = [str(BIN), "--status", "--repo", REPO, "--pr", "5"]
            deadline = time.time() + 30
            while subprocess.run(status, env=env, capture_output=True).returncode == 5:
                assert time.time() < deadline, "the detached round never finished"
                time.sleep(0.2)
            assert ledger(ld)[-1]["verdict"] == "pushed", (resume, ledger(ld))
            assert ledger(ld)[-1]["resumed"] is resume


def test_next_round_builds_on_an_unpushed_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, gate_rc=1)
        assert run(env, wt).returncode == 1
        first_end = ledger(ld)[-1]["end"]
        set_gate(tmp, "exit 0")
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        second = ledger(ld)[-1]
        assert second["pr_head"] == head and second["start"] == first_end, second
        assert git(origin, "rev-parse", BRANCH) == second["end"]
        assert git(wt, "merge-base", "--is-ancestor", first_end, second["end"]) == ""


def test_next_round_refuses_work_on_top_of_an_unpushed_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp, gate_rc=1)
        run(env, wt)
        (wt / "file.txt").write_text("someone else\n")
        git(wt, "commit", "-qam", "someone's work")
        assert run(env, wt).returncode == 3


def _issue_comment_posts(calls):
    # POSTs to issues/$PR/comments, distinct from threaded /comments/$id/replies.
    return [c for c in calls
            if "-X" in c and len(c) > 3
            and "/issues/" in c[3] and c[3].endswith("/comments")]


def test_brief_carries_the_review_body_under_a_review_heading():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        body = "Two things:\n1. Use `review` not `head`.\n2. Drop the `id` field."
        wt, _, _, env, _ = setup(tmp, review_body=body)
        spy = tmp / "fakebin" / "implementer"
        spy.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >\"$SPY\"\nmktemp -d\n")
        env["SPY"] = str(tmp / "args")
        run(env, wt)
        args = (tmp / "args").read_text()
        assert "### review" in args, args
        assert body in args, args


def test_body_only_review_posts_each_reply_as_an_issue_comment():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        body = "1. Item one.\n2. Item two."
        # Body items are commit-less, so the replies quote the items
        # without naming a sha — the sha-skipper has nothing to fire on.
        replies = [
            {"comment_id": "review", "body": "> 1. Item one.\n\nAlready correct in the brief."},
            {"comment_id": "review", "body": "> 2. Item two.\n\nLeft as is: this is the wrong file."},
        ]
        wt, _, _, env, ld = setup(tmp, review_body=body, comments=[],
                                  replies=replies, lane="answer")
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        entry = ledger(ld)[-1]
        assert entry["verdict"] == "answered" and entry["replied"] == 2, entry
        posts = _issue_comment_posts(gh_calls(tmp))
        # All body replies go out as one PR comment, joined with a blank line,
        # in the order the lane wrote them — counted as two replied items.
        assert len(posts) == 1, posts
        post = posts[0]
        assert post[1] == "-X" and post[2] == "POST", post
        assert post[3].endswith("issues/5/comments"), post
        body_arg = post[post.index("-f") + 1]
        # Joined with a blank line; both replies present, the lane's quoting
        # survives the join.
        assert "> 1. Item one." in body_arg and "> 2. Item two." in body_arg
        assert "\n\n" in body_arg, body_arg


def test_mixed_review_uses_each_kind_only_for_its_kind():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        body = "Body item."
        comments = [
            {"id": 11, "path": "file.txt", "line": 1, "body": "line item", "in_reply_to_id": None},
        ]
        replies = [
            {"comment_id": "review", "body": "> Body item.\n\nFixed."},
            {"comment_id": 11, "body": "Fixed."},
        ]
        wt, _, _, env, ld = setup(tmp, review_body=body, comments=comments,
                                  replies=replies)
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        assert ledger(ld)[-1]["replied"] == 2, ledger(ld)[-1]
        threaded = [c for c in gh_calls(tmp) if "-X" in c
                    and c[-1].startswith("body=Fixed.")]
        issue_posts = _issue_comment_posts(gh_calls(tmp))
        # Body reply goes to issues/<PR>/comments, not as a threaded reply.
        assert any(c[3].endswith("issues/5/comments") and "> Body item." in c[c.index("-f") + 1]
                   for c in issue_posts), issue_posts
        # The line-comment reply keeps posting to /comments/<id>/replies.
        assert any(c[3].endswith("/comments/11/replies") for c in threaded), threaded


def test_unknown_comment_id_is_refused_with_a_reason_and_does_not_drop_the_rest():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        replies = [
            {"comment_id": 9999, "body": "Made-up id."},
            {"comment_id": 11, "body": "Fixed."},
        ]
        wt, _, _, env, ld = setup(tmp, replies=replies)
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        assert ledger(ld)[-1]["replied"] == 1, ledger(ld)[-1]
        assert "refused reply to unknown comment id 9999" in r.stderr, r.stderr
        # The real reply still posted, on its own endpoint.
        threaded = [c for c in gh_calls(tmp) if "-X" in c
                    and c[3].endswith("/comments/11/replies")]
        assert len(threaded) == 1, threaded


def _ledger_path(ledger_dir):
    return ledger_dir / "rounds.jsonl"


MARKER_PREFIX = "<!-- pr-loop:answer review=99 head="
STATUS_PREFIX = "<!-- pr-loop:status review=99 head="
MARKER_SUFFIX = " -->"


def _marker_line(body):
    return body.split("\n")[-1]


def _body_without_marker(value):
    """The `-f body=...` argument with the marker line removed."""
    lines = value.split("\n")
    assert lines[-1].startswith(MARKER_PREFIX) and lines[-1].endswith(MARKER_SUFFIX), lines
    return "\n".join(lines[:-1])


def test_refuses_when_no_clone_can_fetch_the_pr_branch():
    # The script picks a clone itself when --worktree is missing. With
    # nothing set, it refuses before a round is recorded.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp)
        env.pop("PIPELINE_REPO", None)
        env.pop("PIPELINE_REPO_OWNER_PROJ", None)
        # The default worktree path is ~/wt/<branch>; redirect HOME so that
        # path is empty and the script must fall back on a clone.
        empty_home = tmp / "home-empty"
        empty_home.mkdir()
        env["HOME"] = str(empty_home)
        r = run(env, wt, worktree=False, repo="owner/proj", pr="5")
        assert r.returncode == 2, r.stderr
        assert not _ledger_path(ld).exists(), "no round should be recorded"
        assert "no checkout" in r.stderr, r.stderr


def test_clone_is_chosen_per_repo_via_PIPELINE_REPO_owner_name():
    # Two clones. PIPELINE_REPO points at the wrong repo's checkout; the
    # per-repo override PIPELINE_REPO_OWNER_PROJ points at the right one.
    # Without --worktree, the script must pick the override.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone_other, _ = make_clone(tmp, "other/thing")
        clone_proj, head = make_clone(tmp, "owner/proj")
        data = tmp / "gh.json"
        data.write_text(json.dumps({
            "pr": {"headRefName": BRANCH, "headRefOid": head, "body": "Closes #7.", "state": "OPEN"},
            "review": {"user": {"login": "reviewer"}, "commit_id": head, "body": "Please fix."},
            "comments": [],
        }))
        replies_src = tmp / "replies.json"
        replies_src.write_text(json.dumps([{"comment_id": "review", "body": "Pushed back."}]))
        state = tmp / "state"
        ledger_dir = state / "fix-pr" / "owner_proj-5"
        env = _clean_env(
            PATH=f"{tmp/'fakebin'}:{os.environ['PATH']}",
            AGENT_STATE_DIR=str(state),
            TOOLS_ENV=str(tmp / "no-env"),
            # The wrong clone as the default, the right clone as the
            # per-repo override: the per-repo must win.
            PIPELINE_REPO=str(clone_other),
            PIPELINE_REPO_OWNER_PROJ=str(clone_proj),
            PIPELINE_IMPLEMENTER=str(tmp / "fakebin" / "implementer"),
            AGENT_FIX_PR_VERIFY=str(tmp / "fakebin" / "gate"),
            FAKE_GH_LOG=str(tmp / "gh.log"),
            FAKE_GH_DATA=str(data),
            FAKE_LANE="answer",
            FAKE_REPLIES=str(replies_src),
            PIPELINE_WT_ROOT=str(tmp / "wt-root"))
        # Implementer and gate stand-ins in the env.
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        for name, text in (("gh", FAKE_GH), ("implementer", FAKE_IMPLEMENTER)):
            (fakebin / name).write_text(text)
            (fakebin / name).chmod(0o755)
        gate = fakebin / "gate"
        gate.write_text("#!/bin/sh\nexit 0\n")
        gate.chmod(0o755)
        r = subprocess.run([str(BIN), "--repo", "owner/proj", "--pr", "5",
                            "--review", "99"], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        # The per-repo clone was used: a worktree was added under it.
        assert (clone_proj / ".git" ).exists()
        # The round was recorded with the override path's head.
        line = _ledger_path(ledger_dir).read_text().strip().splitlines()[-1]
        rec = json.loads(line)
        assert rec["verdict"] == "answered", rec


def test_refuses_to_use_a_clone_pointing_at_a_different_repo():
    # PIPELINE_REPO points at a clone whose remote is for a different repo.
    # The driver must refuse before any round is recorded.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone_other, _ = make_clone(tmp, "other/thing")
        wt, _, _, env, ld = setup(tmp)
        env["PIPELINE_REPO"] = str(clone_other)
        env.pop("PIPELINE_REPO_OWNER_PROJ", None)
        env["PIPELINE_WT_ROOT"] = str(tmp / "wt-root")
        r = run(env, wt, worktree=False, repo="owner/proj", pr="5")
        assert r.returncode == 2, r.stderr
        assert not _ledger_path(ld).exists(), "no round should be recorded"
        assert "not for owner/proj" in r.stderr, r.stderr


def test_author_override_is_reauthored_before_push():
    """A lane commit made with `--author 'X <x@x>'` must be re-authored
    before push: the gate sees the re-authored tree, the rewrite is
    logged, and the pushed commit carries the expected identity."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="commit_with_author")
        # The expected identity comes from LANE_GIT_*; without those,
        # the verifier falls back to the worktree's user.name=t.
        # Set the override so the test names a specific expected
        # identity that is neither t nor Wrong Author.
        env["LANE_GIT_NAME"] = "Expected"
        env["LANE_GIT_EMAIL"] = "expected@example.invalid"
        r = run(env, wt)
        assert r.returncode == 0, (
            f"agent-fix-pr failed: rc={r.returncode} stderr={r.stderr!r}")
        # The pushed branch has the re-authored commit: the verifier
        # rewrote the lane's commit on top of $head before push.
        pushed = git(origin, "rev-parse", BRANCH)
        assert git(wt, "merge-base", "--is-ancestor", head, pushed) == "", (
            f"push was not fast-forward: head={head} pushed={pushed}")
        # Bare repos do not have HEAD checked out, so name the branch.
        ident = git(origin, "log", "-1", "--format=%an <%ae> | %cn <%ce>",
                    BRANCH)
        assert "Expected <expected@example.invalid>" in ident, (
            f"pushed commit does not carry the expected identity: {ident!r}")
        assert "Wrong Author" not in ident, (
            f"pushed commit still carries the wrong author: {ident!r}")
        assert "re-authored:" in r.stderr, (
            f"verifier did not log the rewrite: stderr={r.stderr!r}")


def test_refuse_setting_refuses_with_commit_name_and_nothing_pushed():
    """LANE_GIT_IDENTITY=refuse: a lane commit with the wrong author
    must be refused before push, with a message naming the offending
    commit. Nothing is pushed."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="commit_with_author")
        env["LANE_GIT_NAME"] = "Expected"
        env["LANE_GIT_EMAIL"] = "expected@example.invalid"
        env["LANE_GIT_IDENTITY"] = "refuse"
        r = run(env, wt)
        # The verifier refused; agent-fix-pr records the refusal as the verdict.
        assert r.returncode != 0, (
            f"agent-fix-pr accepted a refused lane commit: rc={r.returncode}")
        assert git(origin, "rev-parse", BRANCH) == head, (
            f"something was pushed despite a refused lane commit")
        # The stderr names the offending commit and the wrong identity.
        assert "refusing:" in r.stderr, (
            f"refusal message missing: stderr={r.stderr!r}")
        assert "Wrong Author" in r.stderr, (
            f"refusal does not name the offending identity: "
            f"stderr={r.stderr!r}")
        entry = ledger(ld)[-1]
        # The ledger line must record the refusal too: a round that
        # refused because of identity should land in the ledger with
        # a verdict that names the cause.
        assert entry["pushed"] is False, entry


def _api_post_bodies(calls):
    """Threaded replies and the body-reply comment (`gh api -X POST -f`)."""
    return [c[c.index("-f") + 1].removeprefix("body=")
            for c in calls if "-X" in c and "-f" in c]


def _summary_body(calls):
    """The round summary's body (`gh pr comment --body`)."""
    bodies = [c[c.index("--body") + 1] for c in calls
              if c[:2] == ["pr", "comment"] and "--body" in c]
    assert len(bodies) == 1, bodies
    return bodies[0]


def test_a_pushed_round_answers_with_the_pushed_head_and_summaries_with_a_status_marker():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, replies=[
            {"comment_id": 11, "body": "Fixed."},
            {"comment_id": "review", "body": "Body answer."},
        ])
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        new_head = git(origin, "rev-parse", BRANCH)
        # Replies carry the answer marker naming the pushed head.
        posts = _api_post_bodies(gh_calls(tmp))
        assert len(posts) == 2, posts
        for body in posts:
            assert _marker_line(body) == (
                f"<!-- pr-loop:answer review=99 head={new_head} -->"
            ), _marker_line(body)
        # The summary carries the status marker, never an answer.
        summary = _summary_body(gh_calls(tmp))
        assert _marker_line(summary) == (
            f"<!-- pr-loop:status review=99 head={new_head} -->"
        ), _marker_line(summary)
        assert MARKER_PREFIX not in summary
        joined = "\n".join(posts + [summary])
        assert "Fixed." in joined and "Body answer." in joined
        assert "Review round 1 (review 99): **pushed**" in joined


def test_a_replies_only_round_marks_the_pr_head():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="answer", replies=[
            {"comment_id": 11, "body": "Left as is."},
            {"comment_id": "review", "body": "Body answer."},
        ])
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        assert git(origin, "rev-parse", BRANCH) == head
        posts = _api_post_bodies(gh_calls(tmp))
        assert len(posts) == 2, posts
        for body in posts:
            assert _marker_line(body) == (
                f"<!-- pr-loop:answer review=99 head={head} -->"
            ), _marker_line(body)
        summary = _summary_body(gh_calls(tmp))
        assert _marker_line(summary) == (
            f"<!-- pr-loop:status review=99 head={head} -->"
        ), _marker_line(summary)
        # Without the marker suffix the body is exactly the lane's text.
        threaded = [p for p in posts if p.startswith("Left as is.\n")]
        assert threaded and _body_without_marker(threaded[0]) == "Left as is."


def test_a_failed_round_summaries_with_a_status_marker_and_no_answer():
    # The summary of a round that committed nothing carries the status
    # marker only: the loop spoke, and the fix stays in hand.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="none")
        assert run(env, wt).returncode == 1
        calls = gh_calls(tmp)
        assert not _api_post_bodies(calls), "a failed round posts no replies"
        summary = _summary_body(calls)
        assert _marker_line(summary) == (
            f"<!-- pr-loop:status review=99 head={head} -->"
        ), _marker_line(summary)
        assert MARKER_PREFIX not in summary
        assert "**no commit**" in summary


def test_running_as_the_owner_exits_2_and_makes_no_post():
    # Logins are case-insensitive: `Jerhaad` owning `jerhaad/proj` is the
    # same account.
    for login, repo in (("owner", "owner/proj"),
                        ("Jerhaad", "jerhaad/proj"),
                        ("OWNER", "owner/proj")):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            wt, _, _, env, ld = setup(tmp, user_login=login, repo=repo)
            r = run(env, wt, repo=repo)
            assert r.returncode == 2, (login, repo, r.stderr)
            assert f"{login} owns {repo}" in r.stderr, r.stderr
            assert "the owner reviews" in r.stderr, r.stderr
            assert not [c for c in gh_calls(tmp) if "-X" in c], "no write"
            assert not [c for c in gh_calls(tmp) if c[:2] == ["pr", "comment"]]
            assert not _ledger_path(ld).exists(), "no round recorded"


def test_an_unreadable_gh_login_exits_2_and_makes_no_post():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, user_fail=True)
        r = run(env, wt)
        assert r.returncode == 2, r.stderr
        assert "could not read the gh login" in r.stderr, r.stderr
        assert "Bad credentials" in r.stderr, r.stderr
        assert not [c for c in gh_calls(tmp) if "-X" in c], "no write"
        assert not _ledger_path(ld).exists(), "no round recorded"


def test_refuses_a_rewritten_history_in_agent_fix_pr():
    """A lane that amended HEAD (no descendant-of-base) must be
    refused by the verifier before the ancestry check below would
    notice. Without the ancestry check, the verifier used to rebase
    the rewritten commit onto the original base, which produced a
    clean fast-forward and hid the rewrite from agent-fix-pr."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="amend")
        # Pin the lane identity in the parent env: the shape the
        # gate runs under on every lane.
        env["GIT_AUTHOR_NAME"] = "Pin"
        env["GIT_AUTHOR_EMAIL"] = "p@x"
        env["GIT_COMMITTER_NAME"] = "Pin"
        env["GIT_COMMITTER_EMAIL"] = "p@x"
        r = run(env, wt)
        assert r.returncode == 1
        assert git(origin, "rev-parse", BRANCH) == head
        # The verdict names the cause: history rewritten, not a
        # generic identity refusal.
        assert ledger(ld)[-1]["verdict"] == "history rewritten"
        assert "history rewritten" in r.stderr, r.stderr


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # a crash is a failure too, not a silent exit
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
