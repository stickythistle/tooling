#!/usr/bin/env python3
"""Pin finding-verify's safety and decision contract.

The model is a stub throughout: the suite must decide from the tool's own
rules, not from a live model's judgement. The cases cover the allow-list,
model failures, malformed answers, invalid quotes, and the requirement that
no finding disappears.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "bin" / "finding-verify"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text(
        "def target(value):\n"
        "    if value:\n"
        "        return 'real'\n"
        "    return 'fallback'\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "base")
    # The tool diffs the branch against origin/main, so the fixture has to
    # have one for the diff to be taken against anything.
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    return repo


def branch_repo(tmp: Path) -> Path:
    """A repo whose branch edits a.py, so the diff has something to show."""
    repo = make_repo(tmp)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.py").write_text(
        "def target(value):\n"
        "    if value:\n"
        "        return 'real'\n"
        "    return 'fallback'\n"
        "\n"
        "def extra():\n"
        "    return 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "change the guard")
    return repo


def located(claim: str = "target always returns real", **extra) -> dict:
    finding = {
        "severity": "low", "path": "a.py", "line": 2,
        "snippet": "if value:", "claim": claim,
        "settled_by": "file:a.py", "recommendation": "fix it",
        "status": "located", "in_diff": False,
    }
    finding.update(extra)
    return finding


def controls_payload(include_false: bool = True) -> dict:
    controls = [{
        "claim": "the guard returns real when value is true",
        "evidence": "return 'real'",
        "verdict": "holds",
        "quote": "return 'real'",
    }]
    if include_false:
        controls.append({
            "claim": "the guard always returns real",
            "evidence": "return 'fallback'",
            "verdict": "does-not-hold",
            "quote": "return 'fallback'",
        })
    return {"controls": controls}


def write_stub(tmp: Path, script: str) -> Path:
    """Write an executable model stub.

    The shebang is added here rather than left to the caller: a stub whose
    first line is blank cannot be exec'd.
    """
    stub = tmp / "model-stub"
    body = script.lstrip("\n")
    if not body.startswith("#!"):
        body = "#!/usr/bin/env python3\n" + body
    stub.write_text(body)
    stub.chmod(0o755)
    return stub


def run_tool(repo: Path, findings: list, *, model: Path | None,
             controls: dict | None = None,
             control_file: Path | None = None,
             extra_env: dict | None = None) -> subprocess.CompletedProcess:
    findings_path = repo / "findings.json"
    findings_path.write_text(json.dumps(findings))
    if control_file is None:
        control_file = repo / "controls.json"
        control_file.write_text(json.dumps(
            controls if controls is not None else controls_payload()))
    env = dict(os.environ)
    env.pop("VERIFY_PROFILE", None)
    env.pop("VERIFY_CONTROLS", None)
    env.pop("VERIFY_MODEL", None)
    env.update({
        "VERIFY_PROFILE": "stub-profile",
        "VERIFY_CONTROLS": str(control_file),
        "VERIFY_MODEL": str(model) if model else str(tmp_missing()),
    })
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(TOOL), str(repo), str(findings_path)],
        capture_output=True, text=True, env=env, timeout=30)


def tmp_missing() -> Path:
    return Path("/nonexistent/finding-verify-model-stub")


def case_located_finding_is_verified(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print(json.dumps({"verdict": "holds", "quote": "        return 'real'"}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    result = run_tool(repo, [located()], model=stub)
    if result.returncode != 0:
        failures.append(f"located: exit {result.returncode}: {result.stderr}")
        return
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        failures.append(f"located: row count {len(rows)}")
        return
    if rows[0].get("verdict") != "holds":
        failures.append(f"located: verdict {rows[0].get('verdict')}")
    if rows[0].get("evidence") != ["        return 'real'"]:
        failures.append(f"located: evidence {rows[0].get('evidence')}")


def case_non_located_preserved(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    original = located()
    original["status"] = "not-found"
    malformed = "not an object"
    result = run_tool(repo, [original, malformed], model=stub)
    if result.returncode != 0:
        failures.append(f"non-located: exit {result.returncode}: {result.stderr}")
        return
    rows = json.loads(result.stdout)
    if len(rows) != 2:
        failures.append(f"non-located: row count {len(rows)}")
        return
    if rows[0].get("status") != "not-found":
        failures.append(f"non-located: status changed {rows[0].get('status')}")
    if rows[0].get("verdict") != "cannot-settle":
        failures.append(f"non-located: verdict {rows[0].get('verdict')}")
    if rows[1].get("value") != "not an object":
        failures.append(f"non-located: malformed value lost {rows[1]}")


def case_disallowed_and_metacharacters(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    marker = tmp / "should-not-exist"
    stub = write_stub(tmp, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "the guard always returns real" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
        else:
            print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
    """))
    bad = [
        "rm -rf /",
        f"; touch {marker}",
        f"$(touch {marker})",
        f"`touch {marker}`",
        "file:a.py; touch marker",
        "curl https://example.invalid",
    ]
    findings = [located(settled_by=value) for value in bad]
    result = run_tool(repo, findings, model=stub)
    if result.returncode != 0:
        failures.append(f"disallowed: exit {result.returncode}: {result.stderr}")
        return
    if marker.exists():
        failures.append("disallowed: shell metacharacter ran")
    rows = json.loads(result.stdout)
    if len(rows) != len(findings):
        failures.append(f"disallowed: row count {len(rows)}")
    for row in rows:
        notes = row.get("verification_notes", [])
        if not any("not run" in note for note in notes):
            failures.append(f"disallowed: no note that command was refused: {row}")
        if row.get("verdict") not in ("holds", "does-not-hold", "cannot-settle"):
            failures.append(f"disallowed: verdict {row.get('verdict')}")


def case_invalid_quote_becomes_cannot_settle(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print(json.dumps({"verdict": "holds", "quote": "line not in evidence"}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    result = run_tool(repo, [located()], model=stub)
    rows = json.loads(result.stdout)
    if rows[0].get("verdict") != "cannot-settle":
        failures.append(f"invalid-quote: verdict {rows[0].get('verdict')}")
    if "not in the evidence" not in rows[0].get("verification_reason", ""):
        failures.append(f"invalid-quote: reason {rows[0].get('verification_reason')}")


def case_wrapped_model_response_is_accepted(tmp: Path, failures: list) -> None:
    """A model that wraps its JSON in prose or a fence is still read.

    The verdict vocabulary is closed; the wrapper around it is not.
    """
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print("Here is my answer:\\n```json\\n"
                  + json.dumps({"verdict": "holds", "quote": "        return 'real'"})
                  + "\\n```")
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    result = run_tool(repo, [located()], model=stub)
    if result.returncode != 0:
        failures.append(f"wrapped-response: exit {result.returncode}: {result.stderr}")
        return
    rows = json.loads(result.stdout)
    if rows[0].get("verdict") != "holds":
        failures.append(f"wrapped-response: verdict {rows[0].get('verdict')}")


def case_malformed_model_response(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print("I am not JSON and have no verdict")
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    result = run_tool(repo, [located()], model=stub)
    rows = json.loads(result.stdout)
    if rows[0].get("verdict") != "cannot-settle":
        failures.append(f"malformed-response: verdict {rows[0].get('verdict')}")
    if "verdict" not in rows[0].get("verification_reason", ""):
        failures.append(f"malformed-response: reason {rows[0].get('verification_reason')}")


def case_model_failure(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    calls = tmp / "calls"
    stub = write_stub(tmp, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, pathlib, sys
        p = pathlib.Path({str(calls)!r})
        p.write_text(p.read_text() if p.exists() else "")
        with p.open("a") as fh:
            fh.write("call\\n")
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            sys.exit(9)
        elif "the guard always returns real" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
        else:
            print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
    """))
    result = run_tool(repo, [located()], model=stub)
    if result.returncode != 0:
        failures.append(f"model-failure: exit {result.returncode}: {result.stderr}")
        return
    rows = json.loads(result.stdout)
    if rows[0].get("verdict") != "cannot-settle":
        failures.append(f"model-failure: verdict {rows[0].get('verdict')}")
    reason = rows[0].get("verification_reason", "")
    if "model call failed" not in reason and "model exited 9" not in reason:
        failures.append(f"model-failure: reason {reason}")


def case_failed_control_refuses_model(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    calls = tmp / "calls"
    stub = write_stub(tmp, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, pathlib, sys
        p = pathlib.Path({str(calls)!r})
        p.write_text(p.read_text() if p.exists() else "")
        with p.open("a") as fh:
            fh.write("call\\n")
        prompt = sys.stdin.read()
        if "the guard always returns real" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
        elif "the guard returns real when value is true" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'real'"}}))
        else:
            print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
    """))
    result = run_tool(repo, [located()], model=stub)
    if result.returncode == 0:
        failures.append("failed-control: tool accepted a profile that failed controls")
    if "known-answer control" not in result.stderr:
        failures.append(f"failed-control: stderr {result.stderr!r}")
    calls_text = calls.read_text() if calls.exists() else ""
    if calls_text.count("call") != 1:
        failures.append("failed-control: model was called for the finding too")


def case_missing_false_control_refuses(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    stub = write_stub(tmp, "#!/usr/bin/env python3\nprint('{}')\n")
    result = run_tool(repo, [located()], model=stub,
                      controls=controls_payload(include_false=False))
    if result.returncode == 0:
        failures.append("missing-false-control: tool accepted controls without a false claim")
    if "does-not-hold" not in result.stderr:
        failures.append(f"missing-false-control: stderr {result.stderr!r}")


def case_missing_profile_refuses(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    stub = write_stub(tmp, "#!/usr/bin/env python3\nprint('{}')\n")
    result = run_tool(repo, [located()], model=stub)
    env = dict(os.environ)
    env.pop("VERIFY_PROFILE", None)
    env["VERIFY_CONTROLS"] = str(repo / "controls.json")
    env["VERIFY_MODEL"] = str(stub)
    result = subprocess.run(
        [sys.executable, str(TOOL), str(repo), str(repo / "findings.json")],
        capture_output=True, text=True, env=env)
    if result.returncode != 2:
        failures.append(f"missing-profile: exit {result.returncode}")
    if "VERIFY_PROFILE" not in result.stderr:
        failures.append(f"missing-profile: stderr {result.stderr!r}")


def case_allow_listed_commands_run(tmp: Path, failures: list) -> None:
    """The four allow-listed read-only forms run, and their output is evidence."""
    repo = make_repo(tmp)
    log = tmp / "log"
    stub = write_stub(tmp, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, pathlib, sys
        prompt = sys.stdin.read()
        pathlib.Path({str(log)!r}).write_text(prompt)
        if "target always returns real" in prompt:
            print(json.dumps({{"verdict": "holds", "quote": "        return 'real'"}}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
        else:
            print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
    """))
    forms = [
        ("file:a.py", "def target(value):"),
        ("grep:a.py:fallback", "    return 'fallback'"),
        ("git-log:HEAD:a.py", "base"),
        ("git-show:HEAD:a.py", "def target(value):"),
    ]
    for settled_by, expect in forms:
        result = run_tool(repo, [located(settled_by=settled_by)], model=stub)
        if result.returncode != 0:
            failures.append(f"allow-list[{settled_by}]: exit "
                            f"{result.returncode}: {result.stderr}")
            continue
        rows = json.loads(result.stdout)
        if rows[0].get("verdict") != "holds":
            failures.append(f"allow-list[{settled_by}]: verdict "
                            f"{rows[0].get('verdict')}")
        if rows[0].get("verification_notes"):
            failures.append(f"allow-list[{settled_by}]: refused: "
                            f"{rows[0].get('verification_notes')}")
        if expect not in log.read_text():
            failures.append(f"allow-list[{settled_by}]: output not in evidence")


def case_path_escape_is_refused(tmp: Path, failures: list) -> None:
    """A settled_by path outside the worktree is refused, not read."""
    repo = make_repo(tmp)
    outside = tmp / "outside.txt"
    outside.write_text("secret outside the worktree\n")
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print(json.dumps({"verdict": "holds", "quote": "        return 'real'"}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    for settled_by in (f"file:{outside}", "file:../outside.txt"):
        result = run_tool(repo, [located(settled_by=settled_by)], model=stub)
        if result.returncode != 0:
            failures.append(f"escape[{settled_by}]: exit {result.returncode}")
            continue
        rows = json.loads(result.stdout)
        notes = rows[0].get("verification_notes", [])
        if not any("not run" in note for note in notes):
            failures.append(f"escape[{settled_by}]: not refused: {rows[0]}")
        if "secret outside" in json.dumps(rows[0]):
            failures.append(f"escape[{settled_by}]: outside content leaked")


def case_invalid_json_and_misuse(tmp: Path, failures: list) -> None:
    repo = make_repo(tmp)
    bad = repo / "bad.json"
    bad.write_text("not json")
    result = subprocess.run(
        [sys.executable, str(TOOL), str(repo), str(bad)],
        capture_output=True, text=True)
    if result.returncode != 2:
        failures.append(f"invalid-json: exit {result.returncode}")
    result = subprocess.run([sys.executable, str(TOOL)],
                            capture_output=True, text=True)
    if result.returncode != 2:
        failures.append(f"misuse: exit {result.returncode}")


def case_diff_hunk_is_the_branch_diff(tmp: Path, failures: list) -> None:
    """The diff hunk is the branch's diff, not the working tree's.

    A checked-out branch has a clean working tree, so a diff against HEAD
    is empty and the model is asked to decide without ever seeing what
    changed.
    """
    repo = branch_repo(tmp)
    log = tmp / "log"
    stub = write_stub(tmp, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, pathlib, sys
        prompt = sys.stdin.read()
        pathlib.Path({str(log)!r}).write_text(prompt)
        if "target always returns real" in prompt:
            print(json.dumps({{"verdict": "holds", "quote": "        return 'real'"}}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
        else:
            print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
    """))
    result = run_tool(repo, [located()], model=stub)
    if result.returncode != 0:
        failures.append(f"diff-hunk: exit {result.returncode}: {result.stderr}")
        return
    prompt = log.read_text()
    if "(no diff hunk)" in prompt:
        failures.append("diff-hunk: the branch's diff did not reach the model")
    if "def extra" not in prompt:
        failures.append("diff-hunk: the changed line did not reach the model")


def case_a_dot_directory_finding_gets_its_hunk(tmp: Path, failures: list) -> None:
    """A finding in a dot-directory gets its diff hunk.

    Stripping a `./` prefix by characters takes the leading dot off a
    dot-directory too, so `.github/workflows/gates.yaml` became
    `github/workflows/gates.yaml` and matched no hunk at all.
    """
    repo = branch_repo(tmp)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "gates.yaml").write_text(
        "name: Gates\non: pull_request\n")
    git(repo, "add", ".github/workflows/gates.yaml")
    git(repo, "commit", "-q", "-m", "add the workflow")
    log = tmp / "log"
    stub = write_stub(tmp, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, pathlib, sys
        prompt = sys.stdin.read()
        pathlib.Path({str(log)!r}).write_text(prompt)
        if "the guard always returns real" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
        else:
            print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
    """))
    finding = located(path=".github/workflows/gates.yaml", line=1,
                      settled_by="file:.github/workflows/gates.yaml")
    result = run_tool(repo, [finding], model=stub)
    if result.returncode != 0:
        failures.append(f"dot-directory: exit {result.returncode}: {result.stderr}")
        return
    prompt = log.read_text()
    if "(no diff hunk)" in prompt:
        failures.append("dot-directory: the dot-directory finding got no diff hunk")
    if "name: Gates" not in prompt:
        failures.append("dot-directory: the changed line did not reach the model")


def case_a_marker_cannot_be_quoted(tmp: Path, failures: list) -> None:
    """A section marker is not evidence, so it cannot decide a finding.

    The markers and the placeholders were numbered like any other entry,
    so a model could answer "does-not-hold" quoting `(no diff hunk)` and
    pass the quote check while saying nothing at all.
    """
    repo = make_repo(tmp)
    for marker in ("=== diff hunk ===", "(no diff hunk)",
                   "=== enclosing block ===", "=== settled_by ==="):
        stub = write_stub(tmp, textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, sys
            prompt = sys.stdin.read()
            if "the guard always returns real" in prompt:
                print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
            elif "the guard returns real when value is true" in prompt:
                print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
            else:
                print(json.dumps({{"verdict": "holds", "quote": {marker!r}}}))
        """))
        result = run_tool(repo, [located()], model=stub)
        if result.returncode != 0:
            failures.append(f"marker: exit {result.returncode}: {result.stderr}")
            continue
        rows = json.loads(result.stdout)
        if rows[0].get("verdict") != "cannot-settle":
            failures.append(
                f"marker: a quote of {marker!r} decided the finding: "
                f"{rows[0].get('verdict')}")


def case_an_evidence_line_that_looks_like_a_marker_is_quotable(
        tmp: Path, failures: list) -> None:
    """A real evidence line that starts with `=== ` is quotable.

    The quote check judged by content, so a line in a source file that
    began with `=== ` could not be quoted at all: the finding came back
    `cannot-settle` even though the model had cited the right line.
    """
    repo = make_repo(tmp)
    (repo / "b.py").write_text(
        "def other(value):\n"
        "=== a section header inside the source\n"
        "    return value\n")
    git(repo, "add", "b.py")
    git(repo, "commit", "-q", "-m", "add b.py")
    quoted = "=== a section header inside the source"
    stub = write_stub(tmp, textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "the guard always returns real" in prompt:
            print(json.dumps({{"verdict": "does-not-hold", "quote": "return 'fallback'"}}))
        elif "the guard returns real when value is true" in prompt:
            print(json.dumps({{"verdict": "holds", "quote": "return 'real'"}}))
        else:
            print(json.dumps({{"verdict": "holds", "quote": {quoted!r}}}))
    """))
    finding = located(path="b.py", line=2, snippet="=== a section header",
                      settled_by="file:b.py")
    result = run_tool(repo, [finding], model=stub)
    if result.returncode != 0:
        failures.append(f"marker-like: exit {result.returncode}: {result.stderr}")
        return
    rows = json.loads(result.stdout)
    if rows[0].get("verdict") != "holds":
        failures.append(
            f"marker-like: a quote of a source line starting with '=== ' did "
            f"not decide the finding: {rows[0].get('verdict')}")
    if rows[0].get("evidence") != [quoted]:
        failures.append(f"marker-like: evidence {rows[0].get('evidence')}")


def case_git_log_revision_cannot_write_a_file(tmp: Path, failures: list) -> None:
    """A git-log revision that is a git option is refused, not run.

    The revision is model-supplied data that reaches argv, so a form
    documented as read-only must not be able to write a file.
    """
    repo = make_repo(tmp)
    canary = tmp / "canary.txt"
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print(json.dumps({"verdict": "holds", "quote": "        return 'real'"}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    for settled_by in (f"git-log:--output={canary}:a.py",
                       f"git-show:--output={canary}:a.py",
                       "git-log:--stat:a.py"):
        result = run_tool(repo, [located(settled_by=settled_by)], model=stub)
        if result.returncode != 0:
            failures.append(f"revision[{settled_by}]: exit "
                            f"{result.returncode}: {result.stderr}")
            continue
        if canary.exists():
            failures.append(f"revision[{settled_by}]: a file was written")
            canary.unlink()
        rows = json.loads(result.stdout)
        notes = rows[0].get("verification_notes", [])
        if not any("not run" in note for note in notes):
            failures.append(f"revision[{settled_by}]: not refused: {rows[0]}")


def case_prose_with_braces_keeps_the_verdict(tmp: Path, failures: list) -> None:
    """A brace in the model's prose does not cost it its answer.

    The model is asked to explain itself; a model reasoning about code
    writes braces constantly, and the answer must survive that.
    """
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print("The function f() { return 1; } does not do what you say.\\n"
                  + json.dumps({"verdict": "holds", "quote": "        return 'real'"}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    result = run_tool(repo, [located()], model=stub)
    if result.returncode != 0:
        failures.append(f"prose-braces: exit {result.returncode}: {result.stderr}")
        return
    rows = json.loads(result.stdout)
    if rows[0].get("verdict") != "holds":
        failures.append(f"prose-braces: verdict {rows[0].get('verdict')}")
    if rows[0].get("evidence") != ["        return 'real'"]:
        failures.append(f"prose-braces: evidence {rows[0].get('evidence')}")


def case_a_nested_answer_keeps_the_verdict(tmp: Path, failures: list) -> None:
    """An answer nested under a key is still the answer.

    A model may hand back one JSON object whose value carries the
    answer -- `{"reasoning": {"verdict": ..., "quote": ...}}`. The
    object that answers is the one inside, so the parser has to look
    for it.
    """
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "QUOTED SOURCE:" in prompt:
            # The finding's answer is nested under a key.
            print(json.dumps({"reasoning": {
                "verdict": "holds", "quote": "        return 'real'"}}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    result = run_tool(repo, [located()], model=stub)
    if result.returncode != 0:
        failures.append(f"nested-answer: exit {result.returncode}: {result.stderr}")
        return
    rows = json.loads(result.stdout)
    if rows[0].get("verdict") != "holds":
        failures.append(f"nested-answer: verdict {rows[0].get('verdict')}")
    if rows[0].get("evidence") != ["        return 'real'"]:
        failures.append(f"nested-answer: evidence {rows[0].get('evidence')}")


def case_non_numeric_timeout_is_a_config_error(tmp: Path, failures: list) -> None:
    """A typo in VERIFY_TIMEOUT is the tool's clean exit 2.

    Every other configuration mistake exits 2 with a one-line message; a
    traceback makes the caller treat it as a different class of failure.
    """
    repo = make_repo(tmp)
    stub = write_stub(tmp, textwrap.dedent("""\
        #!/usr/bin/env python3
        import json, sys
        prompt = sys.stdin.read()
        if "target always returns real" in prompt:
            print(json.dumps({"verdict": "holds", "quote": "        return 'real'"}))
        elif "the guard always returns real" in prompt:
            print(json.dumps({"verdict": "does-not-hold", "quote": "return 'fallback'"}))
        else:
            print(json.dumps({"verdict": "holds", "quote": "return 'real'"}))
    """))
    for raw in ("not-a-number", "120s", ""):
        result = run_tool(repo, [located()], model=stub,
                          extra_env={"VERIFY_TIMEOUT": raw})
        if result.returncode != 2:
            failures.append(f"timeout[{raw!r}]: exit {result.returncode}")
            continue
        if "VERIFY_TIMEOUT" not in result.stderr:
            failures.append(f"timeout[{raw!r}]: stderr {result.stderr!r}")
        if "Traceback" in result.stderr:
            failures.append(f"timeout[{raw!r}]: traceback in output")


def main() -> int:
    failures: list = []
    cases = [
        case_located_finding_is_verified,
        case_non_located_preserved,
        case_disallowed_and_metacharacters,
        case_invalid_quote_becomes_cannot_settle,
        case_wrapped_model_response_is_accepted,
        case_malformed_model_response,
        case_model_failure,
        case_failed_control_refuses_model,
        case_missing_false_control_refuses,
        case_missing_profile_refuses,
        case_allow_listed_commands_run,
        case_path_escape_is_refused,
        case_invalid_json_and_misuse,
        case_diff_hunk_is_the_branch_diff,
        case_a_dot_directory_finding_gets_its_hunk,
        case_a_marker_cannot_be_quoted,
        case_an_evidence_line_that_looks_like_a_marker_is_quotable,
        case_git_log_revision_cannot_write_a_file,
        case_prose_with_braces_keeps_the_verdict,
        case_a_nested_answer_keeps_the_verdict,
        case_non_numeric_timeout_is_a_config_error,
    ]
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp_str:
            try:
                case(Path(tmp_str), failures)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{case.__name__} raised {type(exc).__name__}: {exc}")
    if failures:
        print("FAIL:")
        for failure in failures:
            print(" -", failure)
        return 1
    print("PASS finding-verify: controls; allow-list; verdicts; preservation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
