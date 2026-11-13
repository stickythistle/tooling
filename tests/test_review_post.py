#!/usr/bin/env python3
"""Pin the owner's review poster: one review at one head, or nothing.

The suite drives the real finding-check and real git against a scratch
repository with a bare origin. Only GitHub is stubbed: it serves the PR
head and patch, and records the review payload the tool posts.

Run directly: `python3 tests/test_review_post.py`.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "bin" / "review-post"

GH_STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    state_path = os.environ["GH_STUB_STATE"]
    with open(state_path) as handle:
        state = json.load(handle)
    args = sys.argv[1:]

    if args[:2] == ["pr", "view"]:
        print(json.dumps({"headRefOid": state["head"]}))
        raise SystemExit(0)

    endpoint = ""
    for arg in args:
        if arg.startswith("repos/") and "/pulls/" in arg:
            endpoint = arg
            break
    if endpoint.endswith("/files"):
        print(json.dumps(state["files"]))
        raise SystemExit(0)
    if endpoint.endswith("/reviews"):
        if "POST" not in args:
            print("stub gh: expected POST", file=sys.stderr)
            raise SystemExit(2)
        payload_path = args[args.index("--input") + 1]
        with open(payload_path) as handle:
            payload = json.load(handle)
        state["posts"].append(payload)
        with open(state_path, "w") as handle:
            json.dump(state, handle)
        print(json.dumps({"id": state["review_id"],
                          "state": state.get("review_state", "COMMENTED")}))
        raise SystemExit(0)

    print(f"stub gh: unsupported command: {args!r}", file=sys.stderr)
    raise SystemExit(2)
""")


def git(cwd: Path, *args: str, check: bool = True
        ) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], check=check,
                          capture_output=True, text=True)


def make_repository(tmp: Path) -> tuple[Path, str, str, Path]:
    """Build a bare origin, a feature branch, and a clone of the origin."""
    origin = tmp / "origin.git"
    seed = tmp / "seed"
    clone = tmp / "clone"
    subprocess.run(["git", "init", "--quiet", "--bare",
                    "--initial-branch=main", str(origin)], check=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main",
                    str(seed)], check=True)
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    (seed / "example.txt").write_text(textwrap.dedent("""\
        one
        two
        three
        four
        five
        six
        seven
        eight
        nine
        ten
    """))
    git(seed, "add", "example.txt")
    git(seed, "commit", "--quiet", "-m", "base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "--quiet", "origin", "main")
    base_sha = git(seed, "rev-parse", "HEAD").stdout.strip()

    git(seed, "checkout", "--quiet", "-b", "feature")
    (seed / "example.txt").write_text(textwrap.dedent("""\
        one
        two changed
        three changed
        four
        five
        six
        seven
        eight
        nine
        ten
    """))
    git(seed, "add", "example.txt")
    git(seed, "commit", "--quiet", "-m", "change")
    git(seed, "push", "--quiet", "origin", "feature")
    head_sha = git(seed, "rev-parse", "HEAD").stdout.strip()

    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)],
                   check=True)
    return clone, head_sha, base_sha, origin


def write_state(tmp: Path, head: str) -> Path:
    state_path = tmp / "gh-state.json"
    diff = git(tmp / "clone", "diff",
               "origin/main...origin/feature").stdout
    patch = diff[diff.index("@@"):]
    state_path.write_text(json.dumps({
        "head": head,
        "files": [{
            "filename": "example.txt",
            "patch": patch,
        }],
        "posts": [],
        "review_id": 4242,
    }))
    return state_path


def write_gh_stub(tmp: Path) -> Path:
    stub = tmp / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)
    return stub


def finding(line: int, snippet: str, body: str = "This line is wrong."
            ) -> dict:
    return {
        "path": "example.txt",
        "line": line,
        "snippet": snippet,
        "body": body,
    }


def run_tool(clone: Path, state_path: Path, findings: list, head: str,
             *, event: str = "REQUEST_CHANGES", body: str | None = None,
             dry_run: bool = False) -> subprocess.CompletedProcess:
    findings_path = state_path.parent / "findings.json"
    findings_path.write_text(json.dumps(findings))
    args = [str(TOOL), "owner/repo", "1", str(findings_path),
            "--sha", head, "--event", event, "--clone", str(clone)]
    if body is not None:
        body_path = state_path.parent / "body.md"
        body_path.write_text(body)
        args.extend(["--body", str(body_path)])
    if dry_run:
        args.append("--dry-run")
    env = {
        **os.environ,
        "PATH": f"{state_path.parent}:{os.environ.get('PATH', '')}",
        "GH_STUB_STATE": str(state_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(state_path.parent),
    }
    return subprocess.run(args, capture_output=True, text=True, env=env,
                          timeout=60)


def state(state_path: Path) -> dict:
    return json.loads(state_path.read_text())


def assert_no_temp_worktree(clone: Path, before: set[Path]) -> None:
    """The detached worktree and its temporary directory are both gone."""
    listed = git(clone, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1, listed
    now = set(next(iter(before)).parent.iterdir())
    leftovers = [path for path in now - before if path.is_dir()]
    assert not leftovers, f"the temporary worktree was left behind: {leftovers}"


def test_two_findings_post_one_review_at_the_named_head():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(2, "two changed", "First problem."),
            finding(3, "three changed", "Second problem."),
        ], head)
        assert result.returncode == 0, result.stderr
        posts = state(state_path)["posts"]
        assert len(posts) == 1, posts
        payload = posts[0]
        assert payload["commit_id"] == head, payload
        assert payload["event"] == "REQUEST_CHANGES", payload
        assert payload["comments"] == [
            {"path": "example.txt", "line": 2, "side": "RIGHT",
             "body": "First problem."},
            {"path": "example.txt", "line": 3, "side": "RIGHT",
             "body": "Second problem."},
        ], payload
        assert result.stdout == "4242\n", result.stdout
        assert "review state: COMMENTED" in result.stderr, result.stderr


def test_a_context_line_inside_a_hunk_is_posted():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(4, "four", "The context line matters too."),
        ], head)
        assert result.returncode == 0, result.stderr
        payload = state(state_path)["posts"][0]
        assert payload["comments"] == [
            {"path": "example.txt", "line": 4, "side": "RIGHT",
             "body": "The context line matters too."},
        ], payload


def test_a_line_outside_every_hunk_is_refused_without_posting():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(8, "eight", "Outside the hunk."),
        ], head)
        assert result.returncode == 1, result.stdout
        assert "finding[0]" in result.stderr, result.stderr
        assert "example.txt:8" in result.stderr, result.stderr
        assert state(state_path)["posts"] == [], "a refused review was posted"


def test_a_snippet_missing_at_the_sha_is_refused_without_posting():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(2, "not in the file", "Wrong quote."),
        ], head)
        assert result.returncode == 1, result.stdout
        assert "finding[0]" in result.stderr, result.stderr
        assert "example.txt:2" in result.stderr, result.stderr
        assert state(state_path)["posts"] == [], "a refused review was posted"


def test_a_snippet_that_drifted_two_lines_is_corrected():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(1, "three changed", "The quote drifted two lines."),
        ], head)
        assert result.returncode == 0, result.stderr
        payload = state(state_path)["posts"][0]
        assert payload["comments"] == [
            {"path": "example.txt", "line": 3, "side": "RIGHT",
             "body": "The quote drifted two lines."},
        ], payload


def test_a_quote_containing_a_redaction_marker_is_refused():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(2, "two " + "*" * 3 + " changed",
                    "The quote was redacted."),
        ], head)
        assert result.returncode == 1, result.stdout
        assert "finding[0]" in result.stderr, result.stderr
        assert "example.txt:2" in result.stderr, result.stderr
        assert state(state_path)["posts"] == [], "a refused review was posted"


def test_a_moved_pr_head_is_refused_with_both_shas():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, base, _ = make_repository(tmp)
        state_path = write_state(tmp, base)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(2, "two changed"),
        ], head)
        assert result.returncode == 1, result.stdout
        assert head in result.stderr, result.stderr
        assert base in result.stderr, result.stderr
        assert state(state_path)["posts"] == [], "a refused review was posted"


def test_approval_with_a_finding_is_refused():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(2, "two changed"),
        ], head, event="APPROVE")
        assert result.returncode == 1, result.stdout
        assert "APPROVE" in result.stderr, result.stderr
        assert state(state_path)["posts"] == [], "a refused review was posted"


def test_request_changes_without_findings_or_a_body_is_refused():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [], head)
        assert result.returncode == 1, result.stdout
        assert "REQUEST_CHANGES" in result.stderr, result.stderr
        assert state(state_path)["posts"] == [], "a refused review was posted"


def test_dry_run_prints_the_payload_without_posting():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(2, "two changed", "First problem."),
        ], head, dry_run=True)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["commit_id"] == head, payload
        assert payload["event"] == "REQUEST_CHANGES", payload
        assert payload["comments"][0]["line"] == 2, payload
        assert state(state_path)["posts"] == [], "a dry run posted a review"


def test_the_clone_checkout_does_not_decide_which_file_is_read():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, base, _ = make_repository(tmp)
        git(clone, "checkout", "--quiet", "--detach", base)
        assert git(clone, "rev-parse", "HEAD").stdout.strip() == base
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        before = set(tmp.iterdir())
        result = run_tool(clone, state_path, [
            finding(2, "two changed"),
        ], head)
        assert result.returncode == 0, result.stderr
        payload = state(state_path)["posts"][0]
        assert payload["commit_id"] == head, payload
        assert payload["comments"][0]["line"] == 2, payload
        assert_no_temp_worktree(clone, before)


def test_a_finding_check_failure_is_refused_and_cleans_up():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        # finding-check asks git for a diff. Making that diff fail makes the
        # real tool fail without replacing it with a stub.
        git(clone, "config", "diff.external", "/bin/false")
        write_gh_stub(tmp)
        before = set(tmp.iterdir())
        result = run_tool(clone, state_path, [
            finding(2, "two changed"),
        ], head)
        assert result.returncode == 1, result.stdout
        assert "finding-check" in result.stderr, result.stderr
        assert state(state_path)["posts"] == [], "a refused review was posted"
        assert_no_temp_worktree(clone, before)


def test_a_body_only_request_for_changes_is_posted():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [], head,
                          body="Please address the open questions.")
        assert result.returncode == 0, result.stderr
        payload = state(state_path)["posts"][0]
        assert payload["event"] == "REQUEST_CHANGES", payload
        assert payload["body"] == "Please address the open questions.", payload
        assert payload["comments"] == [], payload


def test_the_owner_can_name_the_comment_event():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clone, head, _, _ = make_repository(tmp)
        state_path = write_state(tmp, head)
        write_gh_stub(tmp)
        result = run_tool(clone, state_path, [
            finding(2, "two changed"),
        ], head, event="COMMENT")
        assert result.returncode == 0, result.stderr
        payload = state(state_path)["posts"][0]
        assert payload["event"] == "COMMENT", payload


def main() -> int:
    failures: list[str] = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
