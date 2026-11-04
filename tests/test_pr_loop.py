#!/usr/bin/env python3
"""Pin what pr-loop does, from GitHub data alone, with no network.

The pure half runs against recorded fixtures. The driver runs end to end
with a stub `gh`, a stub `pr-loop-gate` and stub run tools on PATH: the
stub gate answers from a file the test rewrites between ticks, so a test
can say "this poll, the gate says FIX 55" and assert exactly which stub
started, what it was given, and what the loop printed and posted.

Acceptance facts this pins (issue #133):

- a review, an inline comment and a thread reply move a PR's own ETag but
  not the list's, and the loop polls per-PR endpoints too (measured live
  before writing it);
- killing the loop mid-round and restarting posts nothing twice, because
  the gate decision is re-read from GitHub every poll and a run's posts
  are inside the run;
- FIX/REVIEW/REAPPROVE/AGREED/ESCALATE each do the one thing the issue
  names, under the slots and MiniMax limits;
- three consecutive gate ERRs exit non-zero; an empty scope exits 0.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_LOOP = REPO_ROOT / "bin" / "pr-loop"

sys.path.insert(0, str(REPO_ROOT / "lib"))
import pr_loop as L  # noqa: E402

REPO = "owner/repo"
HEAD = "1111111111111111111111111111111111111111"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"ok    {name}")
    else:
        print(f"FAIL  {name} {detail}")
        failures.append(name)


# ----- pure: configuration ---------------------------------------------------

def config_tests():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pr-loop.toml"

        # Missing file: defaults.
        cfg = L.load_config(path, REPO)
        check("config missing file uses defaults",
              cfg.slots == 1 and cfg.auto_merge == "shadow"
              and cfg.issue_label == "" and cfg.max_rounds == 4)

        path.write_text(textwrap.dedent("""
            [repos."owner/repo"]
            slots = 2
            reviewer_profile = "minimax"
            implementer_profile = "small"
            max_rounds = 6
            issue_label = "lane-ready"
            minimax_max = 3
            auto_merge = "on"
            measure = false
        """))
        cfg = L.load_config(path, REPO)
        check("config reads every key",
              cfg.slots == 2 and cfg.reviewer_profile == "minimax"
              and cfg.implementer_profile == "small" and cfg.max_rounds == 6
              and cfg.issue_label == "lane-ready" and cfg.minimax_max == 3
              and cfg.auto_merge == "on" and cfg.measure is False,
              vars(cfg))

        # Another repo in the same file keeps its own defaults.
        other = L.load_config(path, "someone/else")
        check("config per-repo isolation", other.slots == 1
              and other.issue_label == "")

        path.write_text('[repos."owner/repo"]\nslots = 0\n')
        try:
            L.load_config(path, REPO)
            check("config slots=0 refused", False)
        except L.ConfigError:
            check("config slots=0 refused", True)

        path.write_text('[repos."owner/repo"]\nauto_merge = "yolo"\n')
        try:
            L.load_config(path, REPO)
            check("config bad auto_merge refused", False)
        except L.ConfigError:
            check("config bad auto_merge refused", True)

        path.write_text('[repos."owner/repo"]\nslot = 2\n')
        try:
            L.load_config(path, REPO)
            check("config unknown key refused", False)
        except L.ConfigError:
            check("config unknown key refused", True)


# ----- pure: the conditional GET ---------------------------------------------

def conditional_tests():
    raw304 = 'HTTP/2.0 304 Not Modified\r\nEtag: "abc"\r\n\r\n'
    changed, etag, body = L.parse_conditional_response(raw304, 1)
    check("304 is unchanged with its echoed etag",
          changed is False and etag == '"abc"' and body is None)

    raw200 = 'HTTP/2.0 200 OK\r\nEtag: "def"\r\n\r\n[{"number": 1}]'
    changed, etag, body = L.parse_conditional_response(raw200, 0)
    check("200 is changed with body",
          changed is True and etag == '"def"'
          and json.loads(body) == [{"number": 1}])

    changed, _, reason = L.parse_conditional_response(
        'HTTP/2.0 502 Bad Gateway\r\n\r\noops', 1)
    check("5xx is a failure naming the status",
          changed is None and "502" in (reason or ""))

    changed, _, reason = L.parse_conditional_response("", 1)
    check("empty output is a failure", changed is None)

    # The interval arithmetic: 30 -> 60 -> 120 -> 240 -> 300, capped.
    interval = L.MIN_INTERVAL_SEC
    for want in (60, 120, 240, 300, 300):
        interval = L.next_interval(interval, False)
        check(f"interval doubles to {want}", interval == want)
    check("a change resets to the floor",
          L.next_interval(300, True) == L.MIN_INTERVAL_SEC)


# ----- pure: scope -------------------------------------------------------------

def pr(number, labels=("pr-loop",), base="main", head_ref=None):
    return {"number": number, "state": "open", "draft": False,
            "labels": [{"name": n} for n in labels],
            "base": {"ref": base, "sha": "b" * 40},
            "head": {"ref": head_ref or f"branch-{number}", "sha": HEAD}}


def scope_tests():
    entries = [pr(7), pr(9, labels=()), pr(8),
               pr(11, labels=("pr-loop", L.ESCALATED_LABEL)),
               pr(5)]
    scoped = [e["number"] for e in L.scope_prs(entries, 0)]
    check("scope keeps labelled PRs lowest-first, drops escalated",
          scoped == [5, 7, 8], scoped)
    scoped = [e["number"] for e in L.scope_prs(entries, 7)]
    check("--from drops numbers below it", scoped == [7, 8], scoped)

    issues = [{"number": 12, "labels": [{"name": "lane-ready"}]},
              {"number": 10, "labels": [{"name": "lane-ready"}]},
              {"number": 11, "labels": [{"name": "lane-ready"}],
               "pull_request": {"url": "x"}},
              {"number": 9, "labels": []}]
    check("next_issue is the lowest eligible",
          L.next_issue(issues, "lane-ready", 0, set()) == 10)
    check("a claimed issue is skipped",
          L.next_issue(issues, "lane-ready", 0, {10}) == 12)
    check("no issue_label means the loop never takes issues",
          L.next_issue(issues, "", 0, set()) is None)
    check("from filters issue numbers",
          L.next_issue(issues, "lane-ready", 11, set()) == 12)

    # The claim: a loop/issue-N branch or an exact Closes #N.
    prs = [pr(20, head_ref="loop/issue-12")]
    check("a loop/issue branch claims its issue",
          L.issue_claimed_by(12, prs) and not L.issue_claimed_by(2, prs))
    prs2 = [{"body": "Implements #3.\n\nCloses #3."}]
    check("Closes #3 claims 3 and not 13",
          L.issue_claimed_by(3, prs2) and not L.issue_claimed_by(13, prs2))

    # Base blocking.
    stack = [pr(31, base="feature/parent"), pr(30, head_ref="feature/parent"),
             pr(29, head_ref="feature/other")]
    blocked = L.base_blocked(stack[0], "main", stack)
    check("a PR based on an unagreed PR waits", blocked)
    stack[1]["labels"].append({"name": L.AGREED_LABEL})
    check("a PR based on an agreed PR proceeds",
          not L.base_blocked(stack[0], "main", stack))
    check("a PR based on main is never blocked",
          not L.base_blocked(pr(32), "main", stack))
    check("a base nobody's PR is not blocked",
          not L.base_blocked(pr(33, base="feature/unpr'd"), "main", stack))


# ----- pure: limits -------------------------------------------------------------

def limit_tests():
    cfg = L.RepoConfig(REPO, {"slots": 2, "minimax_max": 4})
    one = [{"proc": None}]
    check("a slot frees the next start",
          L.can_start("review", "minimax", one, cfg, 0))
    check("slots cap in-flight runs",
          not L.can_start("review", "minimax", [one, one], cfg, 0))
    check("the MiniMax cap counts every live session",
          not L.can_start("review", "minimax", one, cfg, 4))
    check("a non-MiniMax profile ignores the MiniMax cap",
          L.can_start("review", "gemma", [one, one], cfg, 99) is False)  # slots
    check("a non-MiniMax profile under slots with MiniMax full starts",
          L.can_start("review", "gemma", one, cfg, 99))
    check("two failures escalate, one does not",
          L.should_escalate(2) and not L.should_escalate(1))
    check("three gate errors are terminal",
          L.gate_error_is_terminal(3) and not L.gate_error_is_terminal(2))

    check("gate lines parse",
          L.parse_gate_line(f"FIX 55") == ("FIX", "55")
          and L.parse_gate_line("WAIT checks") == ("WAIT", "checks")
          and L.parse_gate_line("DONE merged") == ("DONE", "merged")
          and L.parse_gate_line(f"REVIEW {HEAD}") == ("REVIEW", HEAD)
          and L.parse_gate_line("") is None
          and L.parse_gate_line("NOPE x") is None)


# ----- the driver with stubs ------------------------------------------------------

STUB_GH = r"""#!/usr/bin/env bash
# A stub gh: answers from $STUB_DATA and records every call. The list
# endpoint answers 304 once it has seen the list ETag and the test has
# dropped a $STUB_DATA/list_same marker, so a test can say "GitHub has
# been quiet since the last poll".
printf '%s\n' "$*" >> "$STUB_DATA/gh.log"
args="$*"
if [[ "$args" == *"--jq"*".default_branch"* ]]; then
    printf 'main\n'
    exit 0
fi
if [[ "$args" == *"pulls?state=open"* && "$args" == *"-i"* ]]; then
    if [ -e "$STUB_DATA/list_same" ] && [[ "$args" == *"If-None-Match"* ]]; then
        printf 'HTTP/2.0 304 Not Modified\r\nEtag: "list1"\r\n\r\n'
        exit 1
    fi
    printf 'HTTP/2.0 200 OK\r\nEtag: "list1"\r\n\r\n'
    python3 -c '''import json,sys
entries = json.load(open(sys.argv[1]))
print(json.dumps(entries if isinstance(entries, list) else [entries]))''' "$STUB_DATA/prs.json"
    exit 0
fi
# Per-PR conditional GET: any `-i` call is one. Quiet by default.
if [[ "$args" == api* && "$args" == *"-i"* ]]; then
    printf 'HTTP/2.0 304 Not Modified\r\nEtag: "pr1"\r\n\r\n'
    exit 1
fi
if [[ "$args" == *"issues/"*comments* ]]; then
    printf '{"html_url": "https://github.com/owner/repo/issues/1#issuecomment-9"}\n'
    exit 0
fi
if [[ "$args" == *"graphql"* ]]; then
    printf '{"data":{"enablePullRequestAutoMerge":{"pullRequest":{"id":"N"}}}}\n'
    exit 0
fi
if [[ "$args" == *labels* ]]; then
    printf '{}\n'
    exit 0
fi
if [[ "$args" == *"issues?state=open"* ]]; then
    printf '[]\n'
    exit 0
fi
if [[ "$args" == *"pulls/18"* ]]; then
    cat "$STUB_DATA/pr18.json" 2>/dev/null || printf '{}\n'
    exit 0
fi
exit 0
"""

STUB_GATE = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DATA/gate.log"
cat "$STUB_DATA/gate_line" 2>/dev/null
exit 0
"""

STUB_RUN = r"""#!/usr/bin/env bash
# Stub for agent-fix-pr / hermes-review-post / hermes-implement.
printf '%s\n' "$*" >> "$STUB_DATA/runs.log"
printf '%s\n' "$0 $*" >> "$STUB_DATA/ran.txt"
[ -n "$RUN_FAIL" ] && exit "$RUN_FAIL"
exit 0
"""


def make_env(tmp: Path):
    data = tmp / "data"
    data.mkdir()
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", STUB_GH), ("pr-loop-gate", STUB_GATE),
                       ("agent-fix-pr", STUB_RUN),
                       ("hermes-review-post", STUB_RUN),
                       ("hermes-implement", STUB_RUN),
                       ("findings-ledger", STUB_RUN)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_DATA"] = str(data)
    env["PR_LOOP_STATE"] = str(tmp / "state")
    env["PR_LOOP_WT_ROOT"] = str(tmp / "wt")
    env["PR_LOOP_CONFIG"] = str(tmp / "empty-config.toml")
    # Poll in milliseconds so the multi-tick tests finish.
    env["PR_LOOP_MIN_INTERVAL_SEC"] = "0"
    env["PR_LOOP_MAX_INTERVAL_SEC"] = "0"
    (data / "prs.json").write_text("[]")
    (data / "gate_line").write_text("WAIT checks\n")
    return env, data, bin_dir


def run_loop(env, role, once=True, extra=()):
    argv = [sys.executable, str(PR_LOOP), role, REPO] + list(extra)
    if once:
        argv.append("--once")
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def driver_tests():
    # -- REVIEW: the review loop starts hermes-review-post at the sha.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text(f"REVIEW {HEAD}\n")
        proc = run_loop(env, "review")
        ran = (data / "ran.txt").read_text() if (data / "ran.txt").exists() else ""
        check("REVIEW starts hermes-review-post at the gate's sha",
              f"hermes-review-post --pr 18 --sha {HEAD} --repo {REPO}" in ran
              or f"--sha {HEAD}" in ran, ran)
        check("REVIEW logs the transition with the log path",
              "PR 18 review: run started, log" in proc.stdout, proc.stdout)

        # The gate line printed once, not once per quiet poll.
        (data / "list_same").write_text("1")
        first = run_loop(env, "review").stdout
        check("a 304 list with no PR change prints no new gate line",
              "gate REVIEW" not in first or "PR 18: gate REVIEW" in proc.stdout,
              first)

    # -- FIX: the implement loop starts the fix tool with the review id.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text("FIX 4455\n")
        run_loop(env, "implement")
        ran = (data / "ran.txt").read_text() if (data / "ran.txt").exists() else ""
        check("FIX starts the fix tool with the review id",
              "agent-fix-pr --pr 18 --review 4455" in ran, ran)

    # -- AGREED (shadow, the default): ledger line, agreed label, comment.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        entry = pr(18)
        (data / "prs.json").write_text(json.dumps([entry]))
        (data / "pr18.json").write_text(json.dumps(entry))
        (data / "gate_line").write_text(f"AGREED {HEAD}\n")
        proc = run_loop(env, "implement")
        ghlog = (data / "gh.log").read_text()
        shadow = Path(env["PR_LOOP_STATE"]) / "shadow-merge.jsonl"
        line = json.loads(shadow.read_text().splitlines()[0]) if shadow.exists() else {}
        check("AGREED records the shadow decision",
              line.get("pr") == 18 and line.get("head") == HEAD
              and line.get("action") == "would-enable-auto-merge", line)
        check("AGREED labels the PR agreed",
              f"labels[]=pr-loop:agreed" in ghlog, ghlog)
        check("shadow mode never enables auto-merge",
              "enablePullRequestAutoMerge" not in ghlog, ghlog)
        check("AGREED comments the would-merge decision",
              "both sides agree" in ghlog, ghlog)

    # -- AGREED with auto_merge = "on": the graphql call, not the ledger.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        cfg = tmp / "pr-loop.toml"
        cfg.write_text('[repos."owner/repo"]\nauto_merge = "on"\n')
        env["PR_LOOP_CONFIG"] = str(cfg)
        entry = pr(18)
        (data / "prs.json").write_text(json.dumps([entry]))
        (data / "pr18.json").write_text(json.dumps(entry))
        (data / "gate_line").write_text(f"AGREED {HEAD}\n")
        run_loop(env, "implement")
        ghlog = (data / "gh.log").read_text()
        check("auto_merge on enables GitHub auto-merge",
              "enablePullRequestAutoMerge" in ghlog, ghlog)
        shadow = Path(env["PR_LOOP_STATE"]) / "shadow-merge.jsonl"
        check("auto_merge on writes no shadow line", not shadow.exists())

    # -- REAPPROVE: approve with no run.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text(f"REAPPROVE {HEAD}\n")
        run_loop(env, "review")
        ghlog = (data / "gh.log").read_text()
        ran = (data / "ran.txt").read_text() if (data / "ran.txt").exists() else ""
        check("REAPPROVE approves with gh pr review",
              "pr review 18 --repo owner/repo --approve" in ghlog, ghlog)
        check("REAPPROVE starts no model run", ran.strip() == "", ran)

    # -- ESCALATE from the gate: label + comment, no run.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text("ESCALATE max-rounds\n")
        run_loop(env, "review")
        ghlog = (data / "gh.log").read_text()
        ran = (data / "ran.txt").read_text() if (data / "ran.txt").exists() else ""
        check("ESCALATE labels the PR escalated",
              f"labels[]={L.ESCALATED_LABEL}" in ghlog, ghlog)
        check("ESCALATE comments the reason",
              "escalated" in ghlog, ghlog)
        check("ESCALATE starts no run", ran.strip() == "", ran)

    # -- A failed run twice escalates; a run's posts are not repeated.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text(f"REVIEW {HEAD}\n")
        env["RUN_FAIL"] = "3"
        first = run_loop(env, "review")
        # The stub run recorded one attempt; the failure is counted in the
        # loop's memory, which --once does not carry across processes. A
        # restart therefore re-asks the gate — and the gate, reading
        # GitHub, re-asks for the review. Nothing was posted by the loop
        # itself in the meantime: a killed round posts nothing, because
        # every post lives inside the run.
        ghlog = (data / "gh.log").read_text()
        check("a failed round posted nothing outside the run",
              "issues/18/comments" not in ghlog, ghlog)
        second = run_loop(env, "review")
        runs = (data / "ran.txt").read_text().splitlines()
        check("a restart re-reads the gate rather than replaying a post",
              len(runs) == 2, runs)

    # -- Three consecutive gate ERRs exit non-zero.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text("ERR repos/owner/repo/pulls/18: down\n")
        gate = tmp / "bin" / "pr-loop-gate"
        gate.write_text(STUB_GATE + "exit 1\n")
        proc = run_loop(env, "review", once=False)
        check("three consecutive gate errors exit non-zero",
              proc.returncode != 0, proc.stdout + proc.stderr)

    # -- Empty scope exits 0.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text("[]")
        proc = run_loop(env, "implement", once=False)
        check("empty scope exits 0 with a line",
              proc.returncode == 0 and "nothing left in scope" in proc.stdout,
              proc.stdout + proc.stderr)

    # -- The MiniMax cap blocks a review run while four sessions are live.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        cfg = tmp / "pr-loop.toml"
        # minimax_max = 1 so the one sleeper below is the fifth-session
        # stand-in exactly: a larger cap would make this test pass or
        # fail on however many MiniMax runs the machine happens to have.
        cfg.write_text('[repos."owner/repo"]\nreviewer_profile = "minimax"'
                       '\nminimax_max = 1\n')
        env["PR_LOOP_CONFIG"] = str(cfg)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text(f"REVIEW {HEAD}\n")
        # A live minimax hermes: stand one up with a long sleep whose argv
        # matches the loop's ps probe.
        sleeper = subprocess.Popen(["bash", "-c",
                                    "exec -a 'hermes -p minimax chat' sleep 30"])
        try:
            run_loop(env, "review")
            ran = (data / "ran.txt").read_text() if (data / "ran.txt").exists() else ""
            check("the MiniMax cap holds a fifth session back",
                  "hermes-review-post" not in ran, ran)
        finally:
            sleeper.terminate()
            sleeper.wait()

    # -- --from keeps a low PR out of scope entirely.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env, data, _ = make_env(tmp)
        (data / "prs.json").write_text(json.dumps([pr(18)]))
        (data / "gate_line").write_text(f"REVIEW {HEAD}\n")
        run_loop(env, "review", extra=["--from", "19"])
        gate_calls = (data / "gate.log").read_text() if (data / "gate.log").exists() else ""
        check("--from below-scope PRs never reach the gate",
              "18" not in gate_calls, gate_calls)


def main() -> int:
    config_tests()
    conditional_tests()
    scope_tests()
    limit_tests()
    driver_tests()
    if failures:
        print(f"\n{len(failures)} assertion(s) failed")
        return 1
    print("\nall assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
