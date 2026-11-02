#!/usr/bin/env python3
"""Pin what lane-review checks, refuses, dispatches, and records.

The contract this suite holds:

- The state lives outside the worktree, and the decisions are bound to the
  exact commit they were written about. A decision file about a different
  head must not authorise this one.
- Every finding needs a decision; a decision that names no mechanism, no
  reason, or no issue is not a decision, and one malformed entry does not
  cost the others.
- A rejected finding is carried into the next round's answers, so it is
  not raised again.
- --apply dispatches only the accepted findings, with the mechanism named
  and the wrong mechanism forbidden.
- --done refuses while the last round had accepted findings.
- Every round appends one ledger line, and a later round does not damage
  the earlier one.

The lane, the reviewer and `gh` are stubs: the branch is a real git repo in
a scratch directory, the reviewer writes a canned answer, and `gh` answers
from canned JSON. Nothing is pushed and no pull request is opened.

Run directly: `python3 tests/test_lane_review.py`.
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
TOOL = REPO_ROOT / "bin" / "lane-review"
HELPER = REPO_ROOT / "lib" / "lane_review.py"

# The operator's settings must not leak into a test: a lane-pinned identity
# or a driver setting from the parent environment would change what the
# driver does and make the assertions about something else.
_DRIVER_ENV_PREFIXES = ("PIPELINE_", "LANE_REVIEW_", "HERMES_IMPLEMENT_",
                        "HERMES_REVIEW_", "GIT_AUTHOR_", "GIT_COMMITTER_",
                        "LANE_GIT_", "REVIEW_", "DEFAULT_GH_REPO")


def clean_env(**extra):
    """os.environ without the driver's settings, plus `extra`."""
    base = {k: v for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _DRIVER_ENV_PREFIXES)}
    base.update(extra)
    return base


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def make_repo(tmpdir: Path) -> tuple[Path, Path]:
    """A repo with a bare origin, one commit on main, and a lane branch.

    Returns (worktree, bare). The lane branch carries one commit that
    touches one file, which is what the review looks at.
    """
    bare = tmpdir / "origin.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    repo = tmpdir / "wt"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "lane@example.com")
    git(repo, "config", "user.name", "lane")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "base.txt").write_text("base\nchanged\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "the lane's work")
    git(repo, "push", "-q", "origin", "feature")
    return repo, bare


def lane_settled_record(state_dir: Path, worktree: Path, sha: str) -> None:
    """Plant the lane-final record lane-settled reads."""
    import hashlib
    target = state_dir / "lane-final" / hashlib.sha256(
        str(worktree).encode()).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"sha={sha}\nlast_edit_at=2026-01-01T00:00:00\n"
        f"recorded_at=2026-01-01T00:00:00\n")


# A reviewer that answers with a canned JSON array. The driver's hermes
# call goes through hermes_query, which runs `$HERMES -m hermes_cli.main`
# and redirects the agent's stdout to the file the helper named. So the
# stub's own stdout is the reviewer's answer.
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--query-file":
            with open(os.environ["FAKE_REVIEW_PROMPT_LOG"], "w") as f:
                f.write(open(args[i + 1]).read())
    answer = os.environ.get("FAKE_REVIEW_ANSWER", "[]")
    print(answer)
    sys.exit(0)
""")

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    canned = json.load(open(os.environ["FAKE_GH_DATA"]))
    if args[:2] == ["issue", "view"]:
        body = canned.get("issue_body", "")
        if "--jq" in args:
            flt = args[args.index("--jq") + 1]
            sys.stdout.write(subprocess.run(
                ["jq", "-r", flt], input=json.dumps({"body": body}),
                capture_output=True, text=True, check=True).stdout)
        else:
            print(json.dumps({"body": body}))
        sys.exit(0)
    if args[:2] == ["pr", "create"]:
        print(canned.get("pr_url", "https://example.invalid/pr/1"))
        sys.exit(0)
    sys.exit(0)
""")

# The implementer the driver dispatches to. It records the brief it was
# given and commits a fix, so the driver sees a lane that settled with new
# work. It also writes the lane-final record hermes-implement writes at
# its exit, because the driver's settle check reads it.
FAKE_IMPLEMENTER = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -euo pipefail
    worktree=""; brief=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
        --worktree) worktree="$2"; shift 2 ;;
        --extra) brief="$2"; shift 2 ;;
        *) shift ;;
        esac
    done
    printf '%s' "$brief" >"${FAKE_IMPLEMENTER_LOG:-/dev/null}"
    cd "$worktree"
    echo "fixed" >>base.txt
    git -c user.name=lane -c user.email=lane@example.com \\
        commit -qam "apply the accepted findings"
    # The lane-final record hermes-implement writes on exit, so the
    # driver's settle check sees the lane at its new head.
    if [[ -n "${FAKE_LANE_STATE_DIR:-}" ]]; then
        mkdir -p "$FAKE_LANE_STATE_DIR/lane-final"
        key=$(printf '%s' "$(git rev-parse --show-toplevel)" | \\
            python3 -c "import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest())")
        printf 'sha=%s\\nlast_edit_at=2026-01-01T00:00:00\\nrecorded_at=2026-01-01T00:00:00\\n' \\
            "$(git rev-parse HEAD)" >"$FAKE_LANE_STATE_DIR/lane-final/$key"
    fi
    run=$(mktemp -d)
    echo "handover" >"$run/HANDOVER.md"
    echo "$run"
""")


class Env:
    """The scratch environment one case runs in."""

    def __init__(self, tmpdir: Path):
        self.tmpdir = tmpdir
        self.state = tmpdir / "state"
        self.state.mkdir()
        self.bin = tmpdir / "fakebin"
        self.bin.mkdir()
        self.repo, self.bare = make_repo(tmpdir)
        self.gh_log = tmpdir / "gh.log"
        self.gh_log.write_text("")
        self.gh_data = tmpdir / "gh.json"
        self.gh_data.write_text(json.dumps({"issue_body": ""}))
        self.prompt_log = tmpdir / "prompt.txt"
        self.implementer_log = tmpdir / "brief.txt"
        self.write_fake("gh", FAKE_GH)
        self.write_fake("hermes", FAKE_HERMES)
        self.write_fake("hermes-implement", FAKE_IMPLEMENTER)
        # The reviewer's answer. The default is one finding against the
        # changed line, which is a real finding: the file holds it.
        self.answer = json.dumps([{
            "severity": "medium",
            "path": "base.txt",
            "line": 2,
            "snippet": "changed",
            "claim": "the changed line is wrong",
            "settled_by": "reading base.txt",
            "recommendation": "change it back",
        }])

    def write_fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def env(self, **extra):
        base = clean_env(
            PATH=f"{self.bin}:/usr/bin:/bin",
            HOME=str(self.tmpdir / "home"),
            AGENT_STATE_DIR=str(self.state),
            HERMES_PYTHON=f"{self.bin}/hermes",
            FAKE_GH_LOG=str(self.gh_log),
            FAKE_GH_DATA=str(self.gh_data),
            FAKE_REVIEW_ANSWER=self.answer,
            FAKE_REVIEW_PROMPT_LOG=str(self.prompt_log),
            FAKE_IMPLEMENTER_LOG=str(self.implementer_log),
            FAKE_LANE_STATE_DIR=str(self.state),
            # The driver's implementer is the stub, not this checkout's
            # hermes-implement: the test drives the driver, and the real
            # one would wake a model.
            LANE_REVIEW_IMPLEMENT=str(self.bin / "hermes-implement"),
            LANE_REVIEW_BASE="origin/main",
        )
        (self.tmpdir / "home").mkdir(exist_ok=True)
        base.update(extra)
        return base

    def settle(self) -> str:
        """Make the lane look settled at its current head."""
        sha = head(self.repo)
        lane_settled_record(self.state, self.repo.resolve(), sha)
        return sha

    def run(self, *args, **kwargs):
        env = self.env(**kwargs.pop("env", {}))
        return subprocess.run([str(TOOL), *args], capture_output=True,
                              text=True, env=env, cwd=str(self.repo))

    def run_dir(self) -> Path:
        import hashlib
        key = hashlib.sha256(str(self.repo.resolve()).encode()).hexdigest()
        return self.state / "lane-review" / key

    def gh_calls(self) -> list:
        return [json.loads(line) for line in
                self.gh_log.read_text().splitlines() if line.strip()]


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_eq(label, want, got, failures):
    if want != got:
        fail(failures, label, f"want {want!r}, got {got!r}")


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_not_in(label, needle, haystack, failures):
    if needle in haystack:
        fail(failures, label, f"{needle!r} is in {haystack!r}")


# ----- cases ---------------------------------------------------------------

def case_state_is_outside_the_worktree(failures):
    """The findings and the decisions never land in the tree under review."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        r = env.run(str(env.repo), "--base", "origin/main")
        # The review cannot run without a reviewer, so the round may fail
        # for that reason; what is pinned is that the tree is untouched.
        assert_eq("state-outside: rc is not a success",
                  1, r.returncode, failures)
        dirty = git(env.repo, "status", "--porcelain").stdout.strip()
        assert_eq("state-outside: tree untouched", "", dirty, failures)
        run_dir = env.run_dir()
        if not run_dir.is_dir():
            fail(failures, "state-outside: no state directory", str(run_dir))
            return
        inside = [p for p in run_dir.rglob("*")
                  if str(p).startswith(str(env.repo))]
        assert_eq("state-outside: no state inside the worktree",
                  [], inside, failures)
        # The findings file is in the state directory, not in the tree.
        found = list(run_dir.rglob("findings.json"))
        if not found:
            fail(failures, "state-outside: no findings file",
                 str(run_dir))
            return
        for path in found:
            if str(path).startswith(str(env.repo)):
                fail(failures, "state-outside: findings inside the worktree",
                     str(path))


def case_review_writes_findings_and_exits_needs_decisions(failures):
    """A clean review writes round-1/findings.json and exits 1."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        r = env.run(str(env.repo), "--base", "origin/main")
        assert_eq("review: rc", 1, r.returncode, failures)
        assert_in("review: says it needs decisions", "needs decisions",
                  r.stdout + r.stderr, failures)
        findings = env.run_dir() / "round-1" / "findings.json"
        if not findings.is_file():
            fail(failures, "review: no findings.json", str(findings))
            return
        data = json.loads(findings.read_text())
        assert_eq("review: one finding", 1, len(data), failures)
        # The finding was checked against the source, not taken on trust.
        assert_eq("review: finding status", "located", data[0].get("status"),
                  failures)
        assert_eq("review: finding in_diff", True, data[0].get("in_diff"),
                  failures)
        # The ids the decision file refers to.
        ids = env.run_dir() / "round-1" / "ids.txt"
        if not ids.is_file():
            fail(failures, "review: no ids.txt")
            return
        first = ids.read_text().split()
        if len(first) < 2:
            fail(failures, "review: ids.txt has no id", ids.read_text())
            return
        assert_eq("review: the id names the finding", "base.txt:2",
                  first[1], failures)


def case_review_prompt_asks_for_the_finding_schema(failures):
    """The review is asked for finding-check's JSON schema."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        if not env.prompt_log.is_file():
            fail(failures, "prompt: the reviewer was never asked")
            return
        prompt = env.prompt_log.read_text()
        for field in ("severity", "path", "line", "snippet", "claim",
                      "settled_by", "recommendation"):
            assert_in(f"prompt: names {field}", f'"{field}"', prompt, failures)
        assert_in("prompt: asks for an array", "JSON array", prompt, failures)


def case_redaction_artifact_refuses(failures):
    """A commit carrying an agent's redaction rewrite is not reviewed."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        (env.repo / "base.txt").write_text(
            "base\nchanged " + "*" * 3 + "\n")
        git(env.repo, "add", ".")
        git(env.repo, "commit", "-q", "-m", "redaction")
        env.settle()
        r = env.run(str(env.repo), "--base", "origin/main")
        assert_eq("redaction: rc", 3, r.returncode, failures)
        assert_in("redaction: says why", "redaction",
                  r.stdout + r.stderr, failures)
        # No review was spent on it.
        if env.prompt_log.is_file():
            fail(failures, "redaction: the reviewer was called anyway")
        if (env.run_dir() / "round-1" / "findings.json").exists():
            fail(failures, "redaction: findings were written anyway")


def case_unmentioned_file_refuses(failures):
    """A commit that changes a file the issue never mentioned is refused."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        # The issue names base.txt only. The lane also touched other.txt.
        (env.repo / "other.txt").write_text("surprise\n")
        git(env.repo, "add", ".")
        git(env.repo, "commit", "-q", "-m", "sneaky")
        env.settle()
        env.gh_data.write_text(json.dumps(
            {"issue_body": "This changes base.txt and nothing else."}))
        r = env.run(str(env.repo), "--base", "origin/main", "--issue", "7")
        assert_eq("unmentioned: rc", 3, r.returncode, failures)
        assert_in("unmentioned: names the file", "other.txt",
                  r.stdout + r.stderr, failures)
        assert_in("unmentioned: says the reason", "never mentioned",
                  r.stdout + r.stderr, failures)


def case_mentioned_file_is_reviewed(failures):
    """A file the issue does mention passes the same check."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        (env.repo / "other.txt").write_text("named by the issue\n")
        git(env.repo, "add", ".")
        git(env.repo, "commit", "-q", "-m", "named")
        env.settle()
        env.gh_data.write_text(json.dumps(
            {"issue_body": "This changes base.txt and other.txt."}))
        r = env.run(str(env.repo), "--base", "origin/main", "--issue", "7")
        assert_eq("mentioned: rc", 1, r.returncode, failures)
        assert_not_in("mentioned: not refused", "never mentioned",
                      r.stdout + r.stderr, failures)


def case_test_files_are_not_unmentioned(failures):
    """A suite is part of the change; the issue does not name it."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        (env.repo / "tests").mkdir()
        (env.repo / "tests" / "test_x.py").write_text("def test():\n    pass\n")
        git(env.repo, "add", ".")
        git(env.repo, "commit", "-q", "-m", "tests")
        env.settle()
        env.gh_data.write_text(json.dumps(
            {"issue_body": "This changes base.txt only."}))
        r = env.run(str(env.repo), "--base", "origin/main", "--issue", "7")
        assert_eq("test-files: rc", 1, r.returncode, failures)
        assert_not_in("test-files: not refused", "never mentioned",
                      r.stdout + r.stderr, failures)


def case_decision_file_must_cover_every_finding(failures):
    """A finding with no decision stops --apply."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        ids = (env.run_dir() / "round-1" / "ids.txt").read_text().split()
        fid = ids[0]
        decisions = env.run_dir() / "round-1" / "decisions.json"
        # A decision file that decides nothing.
        decisions.write_text(json.dumps(
            {"head": head(env.repo), "decisions": []}))
        r = env.run(str(env.repo), "--apply", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("missing-decision: rc", 1, r.returncode, failures)
        assert_in("missing-decision: names the finding", fid,
                  r.stdout + r.stderr, failures)
        # Nothing was dispatched.
        if env.implementer_log.is_file():
            fail(failures, "missing-decision: a lane was dispatched anyway")


def case_accept_needs_mechanism_and_forbid(failures):
    """An accept with no mechanism, or no forbidden wrong one, is refused."""
    with tempfile.TemporaryDirectory() as t:
        for label, entry in (
            ("no-mechanism", {"id": None, "decision": "accept",
                              "forbid": "do not rewrite it"}),
            ("no-forbid", {"id": None, "decision": "accept",
                           "mechanism": "change the constant"}),
            ("blank-mechanism", {"id": None, "decision": "accept",
                                 "mechanism": "  ", "forbid": "x"}),
        ):
            with tempfile.TemporaryDirectory() as t2:
                env = Env(Path(t2))
                env.settle()
                env.run(str(env.repo), "--base", "origin/main")
                fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
                entry["id"] = fid
                (env.run_dir() / "round-1" / "decisions.json").write_text(
                    json.dumps({"head": head(env.repo),
                                "decisions": [entry]}))
                r = env.run(str(env.repo), "--apply", "--issue", "7",
                            "--base", "origin/main")
                assert_eq(f"accept-{label}: rc", 1, r.returncode, failures)
                assert_in(f"accept-{label}: names the missing field",
                          "mechanism" if label != "no-forbid" else "forbid",
                          r.stdout + r.stderr, failures)
                if env.implementer_log.is_file():
                    fail(failures, f"accept-{label}: a lane was dispatched")


def case_reject_needs_a_reason(failures):
    """A reject with no reason is not a decision."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [{"id": fid, "decision": "reject"}]}))
        r = env.run(str(env.repo), "--apply", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("reject-no-reason: rc", 1, r.returncode, failures)
        assert_in("reject-no-reason: names the field", "reason",
                  r.stdout + r.stderr, failures)


def case_defer_needs_an_issue_number(failures):
    """A defer with no issue, or with something that is not a number."""
    for value in ("", "not-a-number"):
        with tempfile.TemporaryDirectory() as t:
            env = Env(Path(t))
            env.settle()
            env.run(str(env.repo), "--base", "origin/main")
            fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
            (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
                {"head": head(env.repo),
                 "decisions": [{"id": fid, "decision": "defer",
                                "issue": value}]}))
            r = env.run(str(env.repo), "--apply", "--issue", "7",
                        "--base", "origin/main")
            assert_eq("defer: rc", 1, r.returncode, failures)
            assert_in("defer: names the field", "issue",
                      r.stdout + r.stderr, failures)


def case_one_malformed_entry_does_not_cost_the_others(failures):
    """A bad entry is named; the good decision beside it still counts."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [
                 "not an object",
                 {"id": fid, "decision": "accept"},
             ]}))
        r = env.run(str(env.repo), "--apply", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("malformed-entry: rc", 1, r.returncode, failures)
        assert_in("malformed-entry: names the bad entry", "decisions[0]",
                  r.stdout + r.stderr, failures)
        assert_in("malformed-entry: names the missing field", "mechanism",
                  r.stdout + r.stderr, failures)


def case_stale_decisions_do_not_authorise_a_changed_head(failures):
    """A decision file about another commit is refused."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        # The decision names a commit that is not the head.
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": "0" * 40,
             "decisions": [{"id": fid, "decision": "accept",
                            "mechanism": "change it", "forbid": "do not"}]}))
        r = env.run(str(env.repo), "--apply", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("stale-decisions: rc", 1, r.returncode, failures)
        assert_in("stale-decisions: says why", "not",
                  r.stdout + r.stderr, failures)
        if env.implementer_log.is_file():
            fail(failures, "stale-decisions: a lane was dispatched anyway")


def case_apply_dispatches_the_accepted_findings(failures):
    """--apply hands the lane the accepted findings, with the mechanism."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [{"id": fid, "decision": "accept",
                            "mechanism": "replace the constant with 42",
                            "forbid": "do not add a new module"}]}))
        r = env.run(str(env.repo), "--apply", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("apply: rc", 5, r.returncode, failures)
        if not env.implementer_log.is_file():
            fail(failures, "apply: no lane was dispatched")
            return
        brief = env.implementer_log.read_text()
        assert_in("apply: brief names the mechanism",
                  "replace the constant with 42", brief, failures)
        assert_in("apply: brief forbids the wrong mechanism",
                  "do not add a new module", brief, failures)
        assert_in("apply: brief names the finding", "base.txt", brief,
                  failures)
        # The round advanced, so the next review is of the lane's new work.
        state = json.loads((env.run_dir() / "state.json").read_text())
        assert_eq("apply: round advanced", 2, state.get("round"), failures)


def case_rejection_is_carried_into_the_next_review(failures):
    """A rejected finding is not raised again."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [{"id": fid, "decision": "reject",
                            "reason": "the constant is deliberate"}]}))
        # --apply with nothing accepted refuses, but the rejections are
        # still written, so the next round carries them.
        r = env.run(str(env.repo), "--apply", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("carry: rc", 1, r.returncode, failures)
        rejections = env.run_dir() / "round-1" / "rejections.json"
        if not rejections.is_file():
            fail(failures, "carry: no rejections file")
            return
        records = json.loads(rejections.read_text())
        assert_eq("carry: one rejection", 1, len(records), failures)
        assert_eq("carry: the reason is kept",
                  "the constant is deliberate", records[0].get("reason"),
                  failures)
        # The next review's prompt carries it.
        env.answer = json.dumps([])
        r2 = env.run(str(env.repo), "--base", "origin/main")
        assert_eq("carry: next round rc", 1, r2.returncode, failures)
        prompt = env.prompt_log.read_text()
        assert_in("carry: the prompt carries the rejection",
                  "the constant is deliberate", prompt, failures)
        assert_in("carry: the prompt says not to report it again",
                  "Do not report them again", prompt, failures)


def case_done_refuses_while_findings_were_accepted(failures):
    """--done refuses when the last round accepted something."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [{"id": fid, "decision": "accept",
                            "mechanism": "change it", "forbid": "do not"}]}))
        before = head(env.repo)
        r = env.run(str(env.repo), "--done", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("done-accepted: rc", 4, r.returncode, failures)
        assert_in("done-accepted: says why", "accepted",
                  r.stdout + r.stderr, failures)
        # Nothing was pushed and no pull request was opened.
        assert_eq("done-accepted: the branch did not move", before,
                  head(env.repo), failures)
        pushed = subprocess.run(
            ["git", "-C", str(env.bare), "rev-parse", "feature"],
            capture_output=True, text=True)
        if pushed.returncode == 0:
            assert_eq("done-accepted: nothing was pushed", before,
                      pushed.stdout.strip(), failures)
        for call in env.gh_calls():
            assert_not_in("done-accepted: no pull request was opened",
                          "create", " ".join(call), failures)


def case_done_needs_a_decision_file(failures):
    """--done refuses when the round was never decided."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        r = env.run(str(env.repo), "--done", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("done-undecided: rc", 1, r.returncode, failures)
        assert_in("done-undecided: says why", "decision",
                  r.stdout + r.stderr, failures)


def case_done_squashes_pushes_and_opens_a_draft(failures):
    """A decided round with nothing accepted finishes the branch.

    The push and the pull request go to a local bare repository and a fake
    `gh`, so nothing leaves the machine.
    """
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        # Two commits on the branch, so the squash has something to do.
        (env.repo / "base.txt").write_text("base\nchanged\nmore\n")
        git(env.repo, "add", ".")
        git(env.repo, "commit", "-q", "-m", "second")
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [{"id": fid, "decision": "reject",
                            "reason": "the change is deliberate"}]}))
        r = env.run(str(env.repo), "--done", "--issue", "7",
                    "--base", "origin/main")
        assert_eq("done: rc", 0, r.returncode, failures)
        # The branch is one commit on top of the fork point.
        merge_base = git(env.repo, "merge-base", "origin/main",
                         "HEAD").stdout.strip()
        count = git(env.repo, "rev-list", "--count",
                    f"{merge_base}..HEAD").stdout.strip()
        assert_eq("done: the branch is squashed", "1", count, failures)
        # It was pushed.
        pushed = subprocess.run(
            ["git", "-C", str(env.bare), "rev-parse", "feature"],
            capture_output=True, text=True)
        if pushed.returncode != 0:
            fail(failures, "done: the branch was not pushed",
                 pushed.stderr)
        else:
            assert_eq("done: the pushed head is the squashed one",
                      head(env.repo), pushed.stdout.strip(), failures)
        # The pull request was opened as a Draft, saying Closes #7.
        creates = [c for c in env.gh_calls() if c[:2] == ["pr", "create"]]
        if not creates:
            fail(failures, "done: no pull request was opened",
                 str(env.gh_calls()))
            return
        call = creates[0]
        assert_in("done: the pull request is a draft", "--draft", call,
                  failures)
        # The body is a file, so the test reads the file the call names.
        body_path = call[call.index("--body-file") + 1]
        if not Path(body_path).is_file():
            fail(failures, "done: no pull request body", body_path)
            return
        body = Path(body_path).read_text()
        assert_in("done: the pull request names the issue", "Closes #7",
                  body, failures)


def case_ledger_records_every_round(failures):
    """Every round appends one line, and a later round does not damage
    the earlier one."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [{"id": fid, "decision": "accept",
                            "mechanism": "change it", "forbid": "do not"}]}))
        env.run(str(env.repo), "--apply", "--issue", "7",
                "--base", "origin/main")
        ledger = env.run_dir() / "ledger.jsonl"
        if not ledger.is_file():
            fail(failures, "ledger: no ledger file", str(env.run_dir()))
            return
        lines = [l for l in ledger.read_text().splitlines() if l.strip()]
        assert_eq("ledger: one line per round", 1, len(lines), failures)
        entry = json.loads(lines[0])
        assert_eq("ledger: the round", 1, entry.get("round"), failures)
        assert_eq("ledger: the findings", 1, entry.get("findings"), failures)
        assert_eq("ledger: the surviving findings", 1,
                  entry.get("surviving"), failures)
        assert_eq("ledger: the false assertions", 0,
                  entry.get("false_assertions"), failures)
        assert_eq("ledger: the accepted findings", 1, entry.get("accepted"),
                  failures)
        assert_in("ledger: names the model", "model", entry, failures)
        assert_in("ledger: names the brief", "brief", entry, failures)
        # A second round appends, and the first line is unchanged.
        env.answer = json.dumps([])
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        lines2 = [l for l in ledger.read_text().splitlines() if l.strip()]
        assert_eq("ledger: two lines after the second round", 2,
                  len(lines2), failures)
        assert_eq("ledger: the first line is unchanged", lines[0], lines2[0],
                  failures)


def case_misuse_exits_two(failures):
    """Misuse exits 2, not 1: a review that needs a decision is not
    misuse."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        r = env.run("--not-a-flag")
        assert_eq("misuse: unknown flag", 2, r.returncode, failures)
        r = env.run(str(env.tmpdir / "not-a-repo"))
        assert_eq("misuse: not a worktree", 2, r.returncode, failures)
        r = env.run()
        assert_eq("misuse: no arguments", 2, r.returncode, failures)
        assert_in("misuse: says usage", "usage", r.stdout + r.stderr, failures)


def case_an_unsettled_lane_is_not_reviewed(failures):
    """A lane still holding the tree is not reviewed."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        # No lane-final record: lane-settled fails open (exit 0), so the
        # live marker is what makes this tree unsettled.
        import hashlib
        marker = env.state / "lane-active" / hashlib.sha256(
            str(env.repo.resolve()).encode()).hexdigest()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{os.getpid()}\n")
        r = env.run(str(env.repo), "--base", "origin/main",
                    env={"LANE_REVIEW_SETTLE_TIMEOUT": "1"})
        assert_eq("unsettled: rc", 3, r.returncode, failures)
        assert_in("unsettled: says why", "settled",
                  r.stdout + r.stderr, failures)
        if env.prompt_log.is_file():
            fail(failures, "unsettled: the reviewer was called anyway")


def case_priors_are_folded_into_the_prompt(failures):
    """The operator's priors reach the reviewer, and the answers with them."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        priors = env.tmpdir / "priors.txt"
        priors.write_text("the retry budget is 60s\n")
        r = env.run(str(env.repo), "--base", "origin/main",
                    env={"REVIEW_PRIORS": str(priors)})
        assert_eq("priors: rc", 1, r.returncode, failures)
        if not env.prompt_log.is_file():
            fail(failures, "priors: the reviewer was never asked")
            return
        prompt = env.prompt_log.read_text()
        assert_in("priors: the prompt carries the priors",
                  "the retry budget is 60s", prompt, failures)
        assert_in("priors: the prompt says they are settled",
                  "Treat them as settled", prompt, failures)


def case_ids_subcommand_prints_the_finding_ids(failures):
    """`lane-review ids` prints the ids the decision file refers to."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        r = env.run("ids", str(env.repo))
        assert_eq("ids: rc", 0, r.returncode, failures)
        ids_file = env.run_dir() / "round-1" / "ids.txt"
        assert_eq("ids: matches the ids file", ids_file.read_text(),
                  r.stdout, failures)
        # With no round yet, it says so rather than printing nothing.
        with tempfile.TemporaryDirectory() as t2:
            env2 = Env(Path(t2))
            env2.settle()
            r2 = env2.run("ids", str(env2.repo))
            assert_eq("ids: no round yet", 1, r2.returncode, failures)


def case_two_worktrees_keep_separate_state(failures):
    """Two worktrees do not share one round's findings."""
    import hashlib
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        first = env.run_dir()
        # A second worktree, a different directory with the same history.
        other = Path(t) / "other"
        shutil.copytree(env.repo, other)
        with tempfile.TemporaryDirectory() as t2:
            env2 = Env(Path(t2))
            env2.settle()
            r = env2.run(str(other), "--base", "origin/main")
            assert_eq("two-worktrees: rc", 1, r.returncode, failures)
            key = hashlib.sha256(str(other.resolve()).encode()).hexdigest()
            second = env2.state / "lane-review" / key
            if first == second:
                fail(failures, "two-worktrees: the state directories are the "
                              "same", str(first))
                return
            # Each has its own round-1.
            for run_dir in (first, second):
                if not (run_dir / "round-1" / "findings.json").is_file():
                    fail(failures, "two-worktrees: no findings in a run dir",
                         str(run_dir))


def case_done_needs_an_issue_before_it_pushes(failures):
    """--done refuses without --issue, and it refuses before it pushes.

    The squash and the push are irreversible. A branch pushed with no
    pull request is a half-finished state the next run cannot tell from
    a finished one, so the check has to come first.
    """
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        env.run(str(env.repo), "--base", "origin/main")
        fid = (env.run_dir() / "round-1" / "ids.txt").read_text().split()[0]
        (env.run_dir() / "round-1" / "decisions.json").write_text(json.dumps(
            {"head": head(env.repo),
             "decisions": [{"id": fid, "decision": "reject",
                            "reason": "deliberate"}]}))
        before = head(env.repo)
        r = env.run(str(env.repo), "--done", "--base", "origin/main")
        assert_eq("done-no-issue: rc", 2, r.returncode, failures)
        assert_in("done-no-issue: says what it needs", "--issue",
                  r.stdout + r.stderr, failures)
        # Nothing was pushed and the branch did not move.
        assert_eq("done-no-issue: the branch did not move", before,
                  head(env.repo), failures)
        pushed = subprocess.run(
            ["git", "-C", str(env.bare), "rev-parse", "feature"],
            capture_output=True, text=True)
        if pushed.returncode == 0:
            assert_eq("done-no-issue: nothing was pushed", before,
                      pushed.stdout.strip(), failures)


def case_the_ledger_names_the_reviewers_brief(failures):
    """The ledger records which brief the reviewer was given."""
    with tempfile.TemporaryDirectory() as t:
        env = Env(Path(t))
        env.settle()
        priors = env.tmpdir / "priors.txt"
        priors.write_text("the retry budget is 60s\n")
        env.run(str(env.repo), "--base", "origin/main",
                env={"REVIEW_PRIORS": str(priors)})
        ledger = env.run_dir() / "ledger.jsonl"
        if not ledger.is_file():
            fail(failures, "ledger-brief: no ledger file")
            return
        entry = json.loads(ledger.read_text().splitlines()[0])
        brief = entry.get("brief", "")
        if not brief:
            fail(failures, "ledger-brief: the ledger names no brief")
            return
        if not Path(brief).is_file():
            fail(failures, "ledger-brief: the brief is not a file", brief)
            return
        assert_in("ledger-brief: the brief is the one the reviewer read",
                  "the retry budget is 60s", Path(brief).read_text(), failures)


CASES = (
    case_state_is_outside_the_worktree,
    case_review_writes_findings_and_exits_needs_decisions,
    case_review_prompt_asks_for_the_finding_schema,
    case_priors_are_folded_into_the_prompt,
    case_redaction_artifact_refuses,
    case_unmentioned_file_refuses,
    case_mentioned_file_is_reviewed,
    case_test_files_are_not_unmentioned,
    case_decision_file_must_cover_every_finding,
    case_accept_needs_mechanism_and_forbid,
    case_reject_needs_a_reason,
    case_defer_needs_an_issue_number,
    case_one_malformed_entry_does_not_cost_the_others,
    case_stale_decisions_do_not_authorise_a_changed_head,
    case_apply_dispatches_the_accepted_findings,
    case_rejection_is_carried_into_the_next_review,
    case_done_refuses_while_findings_were_accepted,
    case_done_needs_a_decision_file,
    case_done_needs_an_issue_before_it_pushes,
    case_done_squashes_pushes_and_opens_a_draft,
    case_ledger_records_every_round,
    case_the_ledger_names_the_reviewers_brief,
    case_ids_subcommand_prints_the_finding_ids,
    case_two_worktrees_keep_separate_state,
    case_misuse_exits_two,
    case_an_unsettled_lane_is_not_reviewed,
)


def main() -> int:
    if not TOOL.is_file():
        print(f"FAIL tool not built: {TOOL}")
        return 1
    failures: list = []
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
    print(f"PASS lane-review: {len(CASES)} cases -- state outside the tree; "
          "hygiene refusals; the decision contract; --apply; --done; the ledger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
