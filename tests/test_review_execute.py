#!/usr/bin/env python3
"""Pin the execution review lane: a finding is only a finding if it reproduces.

Every finding the lane reports carries the command that reproduces it, and
the command is re-run against the reviewed head before the finding reaches
the findings file. A finding whose reproduction passes is dropped, and the
drop is counted.

The suite drives the real tools against real temporary repositories. The
agent is stubbed at the boundary where it would otherwise reach a model;
the gate, the reproduction runner and the head binding are the real code.

Run directly: `python3 tests/test_review_execute.py`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTE = REPO_ROOT / "bin" / "review-execute"
VERIFY = REPO_ROOT / "lib" / "review_execute.py"
DRIVER = REPO_ROOT / "bin" / "hermes-review-pr"

# The agent stub writes the findings the prompt asks for, and nothing else.
# It is the boundary where a live model would be: everything after it --
# the re-run of every reproduction, the drop decision, the tally, the
# head binding -- is the real code.
STUB_AGENT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, re, sys
    args = sys.argv[1:]
    prompt = ""
    if "--query-file" in args:
        qf = args[args.index("--query-file") + 1]
        if os.path.exists(qf):
            prompt = open(qf).read()
    # The prompt names the findings file to write.
    m = re.search(r"Write your findings to (\\S+)", prompt)
    if not m:
        print("stub agent: no findings path in the prompt", file=sys.stderr)
        sys.exit(1)
    # The stub's findings come from a file the test wrote, so the case
    # decides what the lane is given.
    src = os.environ.get("STUB_FINDINGS_SOURCE")
    findings = []
    if src and os.path.exists(src):
        findings = json.load(open(src))
    with open(m.group(1), "w") as f:
        json.dump(findings, f)
    # -Q: the helper strips this line, so the caller never sees it.
    print("session_id: stub-session-123")
    print(f"{len(findings)} findings")
""")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(tmp: Path, bug: bool) -> Path:
    """A repository whose head carries a bug only running the code reveals.

    The `bug` branch's `counter.py` drops the second increment: reading
    the diff shows the changed line, but nothing short of running it shows
    that two calls to `bump()` leave the counter at one.

    The diff is never empty: a review of an empty diff refuses.
    """
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    run("init", "--initial-branch=main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "counter.py").write_text(textwrap.dedent("""\
        def bump(count):
            count = count + 1
            return count
    """))
    (repo / "README.md").write_text("a project\n")
    run("add", ".")
    run("commit", "-m", "base")
    # The base commit is the far side of the diff.
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    # A change of its own, so the diff is never empty.
    (repo / "README.md").write_text("a project\nwith a change\n")
    run("add", "README.md")
    run("commit", "-m", "a change of its own")
    if bug:
        (repo / "counter.py").write_text(textwrap.dedent("""\
            def bump(count):
                count = count + 1
                return count


            def bump_twice(count):
                bump(count)
                return bump(count)
        """))
        run("add", "counter.py")
        run("commit", "-m", "plant the bug")
    return repo


def write_stub_agent(tmp: Path) -> Path:
    agent = tmp / "agent"
    agent.write_text(STUB_AGENT)
    agent.chmod(0o755)
    return agent


def run_execute(repo: Path, agent: Path, findings: list, *,
                head: str | None = None, extra_env: dict | None = None
                ) -> subprocess.CompletedProcess:
    """Run bin/review-execute against `repo` with the stub agent."""
    tmp = repo.parent
    src = tmp / "stub-findings.json"
    src.write_text(json.dumps(findings))
    head = head or git(repo, "rev-parse", "HEAD")
    env = {
        **os.environ,
        "HERMES_PYTHON": str(agent),
        "STUB_FINDINGS_SOURCE": str(src),
        "TMPDIR": str(tmp),
        "REVIEW_TIMEOUT_REV": "60",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(EXECUTE), str(repo), "--head", head, "--base", "origin/main",
         "--profile", "rev"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=120)


def findings_of(result: subprocess.CompletedProcess) -> dict:
    """The checked findings document the driver printed."""
    path = Path(result.stdout.strip().splitlines()[-1])
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# A finding whose reproduction passes on the reviewed head is dropped, and
# the drop is counted.

def test_reproduction_that_passes_is_dropped_and_counted():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=False)
        agent = write_stub_agent(tmp)
        # The finding claims the counter is wrong, but its reproduction
        # passes: the counter is right on this head.
        result = run_execute(repo, agent, [{
            "severity": "high",
            "path": "counter.py",
            "line": 2,
            "claim": "bump does not increment",
            "reproduce": "python3 -c \"import counter; assert counter.bump(0) == 1\"",
            "observed": "AssertionError",
            "recommendation": "fix bump",
        }])
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        doc = findings_of(result)
        assert doc["tally"]["not-reproduced"] == 1, doc["tally"]
        assert doc["tally"]["reproduced"] == 0, doc["tally"]
        # Nothing is dropped silently: the finding is still in the document,
        # marked with why it went.
        assert len(doc["findings"]) == 1, doc["findings"]
        assert doc["findings"][0]["status"] == "not-reproduced", doc["findings"][0]
        # The re-run's own output is the evidence.
        assert "reproduction_output" in doc["findings"][0], doc["findings"][0]
        assert doc["findings"][0]["reproduction_status"] == 0, doc["findings"][0]


def test_finding_without_a_reproduction_is_dropped_and_counted():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=False)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high",
            "path": "counter.py",
            "line": 2,
            "claim": "bump does not increment",
            "observed": "I looked at it",
            "recommendation": "fix bump",
        }])
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        doc = findings_of(result)
        assert doc["tally"]["no-reproduction"] == 1, doc["tally"]
        assert doc["findings"][0]["status"] == "no-reproduction", doc["findings"][0]


def test_tally_counts_every_finding_in_exactly_one_status():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [
            # Reproduces: the bug is real on this head.
            {
                "severity": "high", "path": "counter.py", "line": 9,
                "claim": "bump_twice only increments once",
                "reproduce": "python3 -c \"import counter; assert counter.bump_twice(0) == 2\"",
                "observed": "AssertionError", "recommendation": "return the count",
            },
            # Fails to reproduce: the claim is false on this head.
            {
                "severity": "low", "path": "counter.py", "line": 2,
                "claim": "bump is wrong",
                "reproduce": "python3 -c \"import counter; assert counter.bump(0) == 1\"",
                "observed": "AssertionError", "recommendation": "change it",
            },
            # No reproduction at all.
            {
                "severity": "low", "path": "counter.py", "line": 3,
                "claim": "looks wrong", "observed": "trust me",
                "recommendation": "change it",
            },
        ])
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        doc = findings_of(result)
        assert doc["tally"]["reproduced"] == 1, doc["tally"]
        assert doc["tally"]["not-reproduced"] == 1, doc["tally"]
        assert doc["tally"]["no-reproduction"] == 1, doc["tally"]
        total = sum(doc["tally"].values())
        assert total == 3, f"a finding went uncounted: {doc['tally']}"


# ---------------------------------------------------------------------------
# A planted bug that only execution reveals is reported, with a reproduction
# that fails on that head.

def test_planted_bug_is_reported_with_a_failing_reproduction():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "python3 -c \"import counter; assert counter.bump_twice(0) == 2\"",
            "observed": "AssertionError: assert 1 == 2",
            "recommendation": "return the counter from bump_twice",
        }])
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        doc = findings_of(result)
        assert doc["tally"]["reproduced"] == 1, doc["tally"]
        kept = doc["findings"][0]
        assert kept["status"] == "reproduced", kept
        # The reproduction failed, and its output is the evidence.
        assert kept["reproduction_status"] != 0, kept
        assert "AssertionError" in kept["reproduction_output"], kept
        # The head it ran against is recorded.
        assert doc["head"] == git(repo, "rev-parse", "HEAD"), doc["head"]


def test_the_same_reproduction_fails_on_the_buggy_head_and_passes_on_the_fixed_one():
    # The finding is about the code, not the diff. The same command is the
    # whole difference between a finding and a drop, and the head decides
    # which.
    command = "python3 -c \"import counter; assert counter.bump_twice(0) == 2\""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        buggy = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        finding = [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": command, "observed": "AssertionError",
            "recommendation": "return the counter",
        }]
        on_buggy = run_execute(buggy, agent, finding)
        assert on_buggy.returncode == 0, on_buggy.stderr
        assert findings_of(on_buggy)["tally"]["reproduced"] == 1, \
            findings_of(on_buggy)["tally"]

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # The same tree with the bug fixed: `bump_twice` now returns the
        # count. The finding's claim is now false, and its reproduction says
        # so.
        fixed = make_repo(tmp, bug=True)
        (fixed / "counter.py").write_text(textwrap.dedent("""\
            def bump(count):
                count = count + 1
                return count


            def bump_twice(count):
                return bump(bump(count))
        """))
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "-C", str(fixed), "commit", "-am", "fix"],
                       check=True, capture_output=True, env=env)
        agent = write_stub_agent(tmp)
        on_fixed = run_execute(fixed, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": command, "observed": "AssertionError",
            "recommendation": "return the counter",
        }])
        assert on_fixed.returncode == 0, on_fixed.stderr
        assert findings_of(on_fixed)["tally"]["not-reproduced"] == 1, \
            findings_of(on_fixed)["tally"]


# ---------------------------------------------------------------------------
# The phase refuses to run when the worktree's head differs from the head it
# was asked to review.

def test_the_verification_pass_refuses_a_worktree_at_the_wrong_head():
    # The library is the second line of defence: the driver refuses a
    # mismatched head before it reaches the library, so the library's own
    # check is only exercised by calling it directly.

    # Without this case a loosened check in the library would never be
    # caught, because the driver's refusal would mask it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        findings = tmp / "findings.json"
        findings.write_text(json.dumps([{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "python3 -c \"import counter; assert counter.bump_twice(0) == 2\"",
            "observed": "AssertionError", "recommendation": "fix",
        }]))
        other_head = git(repo, "rev-parse", "HEAD~1")
        result = subprocess.run(
            [sys.executable, str(VERIFY), str(repo), str(findings),
             "--head", other_head],
            capture_output=True, text=True, timeout=60)
        assert result.returncode != 0, (
            "the verification pass ran against a head it was not asked to "
            f"review. stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "refusing to run" in result.stderr, result.stderr
        # And it produced no findings at all.
        assert result.stdout.strip() == "", result.stdout


def test_refuses_when_the_worktree_head_differs_from_the_asked_head():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        # Ask for the parent of the head the worktree is at.
        other_head = git(repo, "rev-parse", "HEAD~1")
        result = run_execute(repo, agent, [], head=other_head)
        assert result.returncode != 0, (
            "the lane ran against a head it was not asked to review; a "
            f"reproduction would prove nothing. stdout={result.stdout!r} "
            f"stderr={result.stderr!r}")
        assert "refusing to review" in result.stderr, result.stderr
        # And it produced no findings at all.
        assert result.stdout.strip() == "", result.stdout


def test_refuses_when_the_head_is_an_ancestor_of_the_worktree_head():
    # An ancestor check would pass this one: the asked-for head IS an
    # ancestor of the worktree's. Only an exact comparison refuses it.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        ancestor = git(repo, "rev-parse", "HEAD~1")
        result = run_execute(repo, agent, [], head=ancestor)
        assert result.returncode != 0, (
            "the lane accepted an ancestor of the worktree head; a head "
            "force-pushed back to an ancestor must be refused too. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}")
        assert "refusing to review" in result.stderr, result.stderr


def test_refuses_when_the_head_does_not_exist_at_all():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=False)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [], head="0" * 40)
        assert result.returncode != 0, result.stdout
        assert "refusing to review" in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# A reproduction that did not run says nothing about the defect. The exit is
# classified before it is trusted, so a hallucinated reproduction cannot
# survive the re-run.

# The exits that mean the command never ran, and the status each must get.
# 127 is the shell's "not found", 126 is "found but not executable", 124
# is this module's own timeout, and a signal arrives as a negative code.
EXIT_CASES = (
    ("the command does not exist", "definitely-not-a-real-command --show-bug",
     127, "not found"),
    ("the command cannot be executed", "bash -c \"exit 126\"",
     126, "could not be executed"),
    ("the command times out", "sleep 5", 124, "timed out"),
    ("the command is killed by a signal",
     "python3 -c \"import os, signal; os.kill(os.getpid(), signal.SIGKILL)\"",
     None, "signal"),
)


def run_verify(repo: Path, findings: list, timeout: int
               ) -> subprocess.CompletedProcess:
    """Run lib/review_execute.py against `repo` with a short timeout.

    The reproduction timeout is the one the verification pass gives each
    command, and it is what turns a hanging command into exit 124.
    """
    src = repo.parent / "verify-findings.json"
    src.write_text(json.dumps(findings))
    out = repo.parent / "verify-findings.out.json"
    return subprocess.run(
        [sys.executable, str(VERIFY), str(repo), str(src),
         "--head", git(repo, "rev-parse", "HEAD"),
         "--timeout", str(timeout), "--out", str(out)],
        cwd=repo, capture_output=True, text=True, timeout=120)


def test_a_reproduction_that_did_not_run_is_not_reproduced():
    for label, command, status, reason in EXIT_CASES:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo = make_repo(tmp, bug=True)
            result = run_verify(repo, [{
                "severity": "high", "path": "counter.py", "line": 9,
                "claim": "bump_twice increments only once",
                "reproduce": command,
                "observed": "AssertionError", "recommendation": "fix",
            }], timeout=2)
            assert result.returncode == 0, f"{label}: {result.stderr}"
            doc = json.loads((repo.parent / "verify-findings.out.json").read_text())
            kept = doc["findings"][0]
            assert kept["status"] == "reproduction-error", f"{label}: {kept}"
            assert doc["tally"]["reproduced"] == 0, f"{label}: {doc['tally']}"
            assert doc["tally"]["reproduction-error"] == 1, f"{label}: {doc['tally']}"
            assert reason in kept["reason"], f"{label}: {kept['reason']!r}"
            if status is not None:
                assert kept["reproduction_status"] == status, f"{label}: {kept}"


def test_a_command_that_ran_and_failed_is_reproduced():
    # The other side of the same rule: an ordinary non-zero exit from a
    # command that ran is the evidence the finding rests on.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": 'bash -c "exit 1"',
            "observed": "AssertionError", "recommendation": "fix",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        assert doc["tally"]["reproduced"] == 1, doc["tally"]
        assert doc["findings"][0]["reproduction_status"] == 1, doc["findings"][0]


# ---------------------------------------------------------------------------
# The tree under review is the evidence. A reproduction that changed it
# is caught afterwards, because the program name is no what does the write.

def test_a_reproduction_that_changes_the_tree_is_a_reproduction_error():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 1,
            "claim": "writes a file",
            "reproduce": "python3 -c \"open('DIRTY','w').write('x')\"",
            "observed": "", "recommendation": "do not",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        kept = doc["findings"][0]
        assert kept["status"] == "reproduction-error", kept
        assert "changed the tree" in kept["reason"], kept
        assert doc["tally"]["reproduced"] == 0, doc["tally"]


def test_an_ignored_file_a_reproduction_wrote_is_put_back_too():
    # The tree state is read with `--ignored`, so an ignored file counts
    # as a change to the tree. It has to be removed as well:
    # `git clean -ffd` leaves it, and the next reproduction would run
    # against a tree that had left the head.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        (repo / ".gitignore").write_text("ignored.txt\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "ignore a file")
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 1,
            "claim": "writes an ignored file",
            "reproduce": "python3 -c \"open('ignored.txt','w').write('x')\"",
            "observed": "", "recommendation": "do not",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        kept = doc["findings"][0]
        assert kept["status"] == "reproduction-error", kept
        assert "changed the tree" in kept["reason"], kept
        # The ignored file is gone, so the tree is the reviewed head again.
        assert not (repo / "ignored.txt").exists(), "the ignored file was left"
        state = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--ignored"],
            capture_output=True, text=True, check=True).stdout
        assert state == "", f"the tree was left dirty: {state!r}"


def test_a_reproduction_that_writes_and_then_times_out_leaves_no_tree_behind():
    # The exit check and the tree comparison are both on the path a
    # command takes. A command that writes a file and then hangs timed
    # out, so the exit reason came back first -- and the tree it had
    # dirtied stayed, because the restore keyed on the reason.

    # The flag says the tree changed, so the restore happens either way.
    # The verification pass is driven directly, with the short timeout that
    # turns a hanging command into exit 124.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        result = run_verify(repo, [
            {
                "severity": "high", "path": "counter.py", "line": 1,
                "claim": "writes a file and then hangs",
                "reproduce": "python3 -c \"import time; "
                             "open('DIRTY','w').write('x'); time.sleep(30)\"",
                "observed": "", "recommendation": "do not",
            },
            {
                "severity": "high", "path": "counter.py", "line": 9,
                "claim": "bump_twice increments only once",
                "reproduce": "python3 -c \"import counter; "
                             "assert counter.bump_twice(0) == 2\"",
                "observed": "AssertionError", "recommendation": "fix",
            },
        ], timeout=2)
        assert result.returncode == 0, result.stderr
        doc = json.loads((repo.parent / "verify-findings.out.json").read_text())
        first = doc["findings"][0]
        assert first["status"] == "reproduction-error", first
        assert first["tree_changed"] is True, first
        assert "timed out" in first["reason"], first
        # The file the timed-out command wrote is gone, so the second
        # reproduction ran against the reviewed head and found the defect.
        assert not (repo / "DIRTY").exists(), "the file was left behind"
        assert doc["tally"]["reproduced"] == 1, doc["tally"]


def test_a_worktree_that_is_not_clean_is_refused():
    # A restore puts the tree back with `reset --hard` and `clean -ffdx`.
    # On a developer's working tree that would take their uncommitted edits
    # with it, so the lane refuses to start on a dirty tree and the edit
    # survives.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        (repo / "README.md").write_text("an uncommitted edit\n")
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "python3 -c \"import counter; "
                         "assert counter.bump_twice(0) == 2\"",
            "observed": "AssertionError", "recommendation": "fix",
        }])
        assert result.returncode != 0, "a dirty worktree was accepted"
        assert "not clean" in result.stderr, result.stderr
        # The edit is still there.
        assert (repo / "README.md").read_text() == "an uncommitted edit\n", \
            "the uncommitted edit was destroyed"


def test_the_tree_is_put_back_before_the_next_reproduction():
    # A reproduction that dirtied the tree must not leave the next one
    # running against a tree that has left the reviewed head.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [
            {
                "severity": "high", "path": "counter.py", "line": 1,
                "claim": "writes a file",
                "reproduce": "python3 -c \"open('DIRTY','w').write('x')\"",
                "observed": "", "recommendation": "do not",
            },
            {
                "severity": "high", "path": "counter.py", "line": 9,
                "claim": "bump_twice increments only once",
                "reproduce": "python3 -c \"import counter; "
                             "assert counter.bump_twice(0) == 2\"",
                "observed": "AssertionError", "recommendation": "fix",
            },
        ])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        assert doc["tally"]["reproduction-error"] == 1, doc["tally"]
        # The second reproduction ran against the reviewed head and found
        # the real defect, which it could not have done in a dirtied tree.
        assert doc["tally"]["reproduced"] == 1, doc["tally"]
        # And the tree is clean afterwards.
        state = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--ignored"],
            capture_output=True, text=True, check=True).stdout
        assert state == "", f"the tree was left dirty: {state!r}"


def test_a_reading_git_command_with_an_option_is_allowed():
    # The git arm reads the subcommand past git's own options, so
    # `git -C /path show` is the read it is and not a write.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": f"git -C {repo} show HEAD:counter.py",
            "observed": "the code", "recommendation": "return the counter",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        assert doc["tally"]["reproduction-error"] == 0, doc["tally"]
        assert doc["tally"]["not-reproduced"] == 1, doc["tally"]


# ---------------------------------------------------------------------------
# A kept finding's `observed` is the re-run's output, not the model's text.

def test_a_kept_findings_observed_is_the_reruns_output():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "python3 -c \"import counter; "
                         "assert counter.bump_twice(0) == 2\"",
            "observed": "a transcript the model made up",
            "recommendation": "return the counter",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        kept = doc["findings"][0]
        assert kept["status"] == "reproduced", kept
        # The re-run's own output is what the reader sees first.
        assert kept["observed"] == kept["reproduction_output"], kept
        assert "AssertionError" in kept["observed"], kept
        assert "made up" not in kept["observed"], kept
        # And the model's text is kept under its own key.
        assert kept["claimed_output"] == "a transcript the model made up", kept


# ---------------------------------------------------------------------------
# The verification pass refuses to run a reproduction that would change the
# tree or reach off the machine.

def test_a_reproduction_that_writes_is_refused():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=False)
        agent = write_stub_agent(tmp)
        marker = tmp / "marker"
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 1,
            "claim": "writes a file",
            "reproduce": f"touch {marker}",
            "observed": "", "recommendation": "do not",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        assert doc["tally"]["reproduction-error"] == 1, doc["tally"]
        assert doc["findings"][0]["status"] == "reproduction-error", doc["findings"][0]
        assert not marker.exists(), "the refused reproduction ran anyway"


def test_a_reproduction_that_pipelines_is_refused():
    # A pipeline's parts cannot be seen from the text, so what actually ran
    # cannot be checked.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=False)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 1,
            "claim": "pipes",
            "reproduce": "python3 -c 'import counter' | cat",
            "observed": "", "recommendation": "do not",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        assert doc["tally"]["reproduction-error"] == 1, doc["tally"]


def test_a_writing_git_command_is_refused():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=False)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 1,
            "claim": "pushes",
            "reproduce": "git push origin main",
            "observed": "", "recommendation": "do not",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        assert doc["tally"]["reproduction-error"] == 1, doc["tally"]


def test_a_reading_git_command_is_allowed():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "git show HEAD:counter.py",
            "observed": "the code", "recommendation": "return the counter",
        }])
        assert result.returncode == 0, result.stderr
        doc = findings_of(result)
        # `git show` succeeds, so the finding is dropped as
        # not-reproduced, not refused as an error: the command ran.
        assert doc["tally"]["not-reproduced"] == 1, doc["tally"]
        assert doc["tally"]["reproduction-error"] == 0, doc["tally"]


# ---------------------------------------------------------------------------
# The tree under review is unchanged by the lane.

def test_the_reviewed_tree_is_unchanged_after_the_run():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = make_repo(tmp, bug=True)
        agent = write_stub_agent(tmp)
        before = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        head_before = git(repo, "rev-parse", "HEAD")
        result = run_execute(repo, agent, [{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "python3 -c \"import counter; assert counter.bump_twice(0) == 2\"",
            "observed": "AssertionError", "recommendation": "fix",
        }])
        assert result.returncode == 0, result.stderr
        after = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout
        assert after == before, (
            f"the lane changed the tree under review: before={before!r} "
            f"after={after!r}")
        assert git(repo, "rev-parse", "HEAD") == head_before, "the head moved"


# ---------------------------------------------------------------------------
# The driver: the execute phase refuses a worktree at the wrong head, and
# parks the findings on the artifact branch.

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo -e "feature\\tmain"
""")


def make_pr_repo(tmp: Path) -> tuple[Path, Path]:
    """A local repo with a bare origin and a `feature` branch off `main`."""
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD",
                    "refs/heads/main"], check=True, capture_output=True)
    repo = tmp / "repo"
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)],
                   check=True, capture_output=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("remote", "add", "origin", str(remote))
    (repo / "counter.py").write_text(textwrap.dedent("""\
        def bump(count):
            count = count + 1
            return count
    """))
    run("add", ".")
    run("commit", "-m", "base")
    run("push", "--quiet", "origin", "main:main")
    run("checkout", "--quiet", "-b", "feature")
    (repo / "counter.py").write_text(textwrap.dedent("""\
        def bump(count):
            count = count + 1
            return count


        def bump_twice(count):
            bump(count)
            return bump(count)
    """))
    run("add", "counter.py")
    run("commit", "-m", "plant the bug")
    run("push", "--quiet", "origin", "feature:feature")
    return (repo, tmp)


def run_driver(repo: Path, tmp: Path, *, phase: str = "execute",
               findings: list | None = None,
               extra_env: dict | None = None) -> subprocess.CompletedProcess:
    gh = tmp / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    agent = write_stub_agent(tmp)
    src = tmp / "stub-findings.json"
    src.write_text(json.dumps(findings if findings is not None else []))
    wt_root = tmp / "wt"
    wt_root.mkdir(exist_ok=True)
    priors = tmp / "priors.txt"
    priors.write_text("priors\n")
    env = {
        **os.environ,
        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
        "HERMES_PYTHON": str(agent),
        "STUB_FINDINGS_SOURCE": str(src),
        "REVIEW_WT_ROOT": str(wt_root),
        "REVIEW_PRIORS": str(priors),
        "REVIEW_PROFILE": "rev",
        "TMPDIR": str(tmp),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(DRIVER), "--pr", "1", "--phase", phase],
        cwd=repo, env=env, capture_output=True, text=True, timeout=180)


def test_driver_execute_phase_parks_findings_naming_the_reviewed_head():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _ = make_pr_repo(tmp)
        pr_head = git(repo, "rev-parse", "origin/feature")
        result = run_driver(repo, tmp, findings=[{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "python3 -c \"import counter; assert counter.bump_twice(0) == 2\"",
            "observed": "AssertionError", "recommendation": "fix",
        }])
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        wt = tmp / "wt" / "review-pr-1"
        notes = wt / "EXECUTION_NOTES.json"
        assert notes.exists(), result.stderr
        doc = json.loads(notes.read_text())
        # The head it ran against is recorded in the artifact itself.
        assert doc["head"] == pr_head, (doc["head"], pr_head)
        assert doc["tally"]["reproduced"] == 1, doc["tally"]
        # And the commit that parks the artifact names the same head.
        message = subprocess.run(["git", "-C", str(wt), "log", "-1", "--format=%B"],
                                 check=True, capture_output=True, text=True).stdout
        assert pr_head[:7] in message, message


def test_driver_execute_phase_refuses_a_worktree_at_the_wrong_head():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _ = make_pr_repo(tmp)
        # A first run establishes the worktree and its recorded head.
        first = run_driver(repo, tmp)
        assert first.returncode == 0, f"exit {first.returncode}: {first.stderr}"
        # The branch moves on, so the worktree holds a head other than
        # the one the phase was asked to review.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "-C", str(repo), "checkout", "--quiet", "feature"],
                       check=True, capture_output=True, env=env)
        (repo / "counter.py").write_text(textwrap.dedent("""\
            def bump(count):
                count = count + 1
                return count


            def bump_twice(count):
                return bump(bump(count))
        """))
        subprocess.run(["git", "-C", str(repo), "commit", "-am", "fix"],
                       check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(repo), "push", "--quiet",
                        "origin", "feature:feature"],
                       check=True, capture_output=True, env=env)
        second = run_driver(repo, tmp)
        assert second.returncode != 0, (
            "the execute phase reviewed a head the worktree no longer held; "
            f"stdout={second.stdout!r} stderr={second.stderr!r}")
        assert "stale source" in second.stderr, second.stderr


def test_driver_execute_phase_uses_a_worktree_at_the_reviewed_head():
    # The artifact worktree carries the review's own commits, so it cannot
    # be the tree the reproductions run in. The phase makes a separate
    # detached worktree at the reviewed head, and that is where they ran.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo, _ = make_pr_repo(tmp)
        pr_head = git(repo, "rev-parse", "origin/feature")
        result = run_driver(repo, tmp, findings=[{
            "severity": "high", "path": "counter.py", "line": 9,
            "claim": "bump_twice increments only once",
            "reproduce": "python3 -c \"import counter; assert counter.bump_twice(0) == 2\"",
            "observed": "AssertionError", "recommendation": "fix",
        }])
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        exec_wt = tmp / "wt" / "review-pr-1-exec"
        assert exec_wt.exists(), result.stdout
        assert git(exec_wt, "rev-parse", "HEAD") == pr_head, \
            "the execution worktree is not at the reviewed head"
        # And the artifact worktree is on its own branch, carrying the
        # findings commit.
        artifact_wt = tmp / "wt" / "review-pr-1"
        assert git(artifact_wt, "rev-parse", "--abbrev-ref", "HEAD") == "review/pr-1"


def main() -> int:
    failures: list[str] = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures.append(f"{name}: {exc}")
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
