#!/usr/bin/env python3
"""Pin pr-ready's verdict on the three cases the issue separates.

A check that ran and failed has steps with conclusions, and a log. A
check that never started has zero steps and no log. The two are
different answers about the branch: the first refuses the promotion
because CI produced evidence against it; the second promotes because
CI produced no evidence at all, and an annotation tells the reviewer
what they are getting. Lumping them together is the defect this test
guards against.

The third case -- genuine failure mixed with never-started checks --
is the one most likely to be wrong, because the obvious code path
that "promotes under no evidence" will also promote under a real
failure that simply happens to share the run. The fixture isolates
each shape, and the test asserts the verdict on each in isolation,
so a regression on one case cannot be hidden by another.

A repository that runs no workflows reports no checks for every head,
which is the shape a head that never reached GitHub reports too. The
pushed head tells the two apart: a no-CI repository promotes a clean,
pushed head and refuses every other one.

No network. A stub `gh` answers every subcommand pr-ready invokes,
reading from fixtures on disk so the canned JSON is the kind a real
`gh` actually prints and not something this test only ever feeds
itself. A stub `find_prose.py` emits `$STUB_DATA/find_prose.json`, or `[]`
when a scenario sets none.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_READY = REPO_ROOT / "bin" / "pr-ready"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def make_repo(tmpdir: Path) -> tuple[Path, str, str]:
    """Initialise a git repo with one commit on `issue-28`. Returns
    (worktree, branch, oid) so the test can build a pr_view fixture
    whose HEAD OID matches what `git rev-parse` will return."""
    tmpdir.mkdir(parents=True, exist_ok=True)
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "issue-28", str(repo)],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name",
                    "Test"], check=True)
    (repo / "README").write_text("test\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True)
    oid = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    return repo, "issue-28", oid


def write_stub_gh(bin_dir: Path, data_dir: Path) -> None:
    """Write a stub `gh` that answers the subcommands pr-ready invokes,
    reading each response from $data_dir so the test sets the canned
    JSON on disk rather than in a here-string the script only sees once.

    Every invocation is logged to data_dir/gh.log so a test can read
    back what pr-ready actually asked for. pr-ready treats `gh pr
    checks` exit non-zero as "no checks reported"; the stub exits 0.

    `pr view` is called with --jq to flatten the JSON into a TSV the
    caller reads into separate variables; the stub pipes the canned
    JSON through the same jq expression so the test sees what
    pr-ready actually sees.
    """
    body = (
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$STUB_DATA/gh.log\"\n"
        "case \"$1\" in\n"
        "  pr)\n"
        "    case \"$2\" in\n"
        "      view)\n"
        "        # args: pr view <PR> --json FIELDS --jq EXPR\n"
        "        json=() jq_expr=\"\"\n"
        "        prev=\"\"\n"
        "        for arg in \"$@\"; do\n"
        "          case \"$prev\" in\n"
        "            --json) json=(\"$arg\") ;;\n"
        "            --jq)   jq_expr=\"$arg\" ;;\n"
        "          esac\n"
        "          prev=\"$arg\"\n"
        "        done\n"
        "        # A scenario can ask the body fetch to fail by creating\n"
        "        # $STUB_DATA/gh_body_fetch_fail: pr-ready is then expected\n"
        "        # to refuse with a named failure rather than read an empty\n"
        "        # body as a pass.\n"
        "        for f in \"${json[@]}\"; do\n"
        "          if [ \"$f\" = \"body\" ] && [ -e \"$STUB_DATA/gh_body_fetch_fail\" ]; then\n"
        "            echo \"gh: could not fetch PR body (simulated)\" >&2\n"
        "            exit 1\n"
        "          fi\n"
        "        done\n"
        "        if [ -n \"$jq_expr\" ]; then\n"
        "          cat \"$STUB_DATA/pr_view.json\" | jq -r \"$jq_expr\"\n"
        "        else\n"
        "          cat \"$STUB_DATA/pr_view.json\"\n"
        "        fi\n"
        "        ;;\n"
        "      checks)\n"
        "        cat \"$STUB_DATA/pr_checks.json\"\n"
        "        ;;\n"
        "      comment)\n"
        "        # args: pr comment <PR> --body-file <PATH>\n"
        "        prev=\"\" body=\"\"\n"
        "        for arg in \"$@\"; do\n"
        "          [ \"$prev\" = \"--body-file\" ] && body=\"$arg\"\n"
        "          prev=\"$arg\"\n"
        "        done\n"
        "        if [ -n \"$body\" ]; then\n"
        "          cp \"$body\" \"$STUB_DATA/comment_body.txt\"\n"
        "        fi\n"
        "        ;;\n"
        "      ready)\n"
        "        echo \"$3\" > \"$STUB_DATA/ready_called\"\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "  run)\n"
        "    # args: run view <RUN_ID> --json jobs\n"
        "    cat \"$STUB_DATA/run_$3.json\" 2>/dev/null || echo '{\"jobs\":[]}'\n"
        "    ;;\n"
        "  api)\n"
        "    # args: api repos/{owner}/{repo}/actions/workflows --jq .total_count\n"
        "    # The count comes from a file, so a scenario can say how\n"
        "    # many workflows the repository runs. A scenario can also\n"
        "    # ask the query to fail by creating\n"
        "    # $STUB_DATA/gh_workflow_count_fail: pr-ready is then expected\n"
        "    # to refuse, not to read the failure as a repository with no\n"
        "    # workflows.\n"
        "    if [ -e \"$STUB_DATA/gh_workflow_count_fail\" ]; then\n"
        "      echo \"gh: could not count workflows (simulated)\" >&2\n"
        "      exit 1\n"
        "    fi\n"
        "    cat \"$STUB_DATA/workflow_count\" 2>/dev/null || echo 0\n"
        "    ;;\n"
        "esac\n"
    )
    gh = bin_dir / "gh"
    gh.write_text(body)
    gh.chmod(0o755)


def write_stub_gate_verify(bin_dir: Path) -> None:
    """Stub gate-verify so a test that runs it does not have to build a
    real one. Two faces:
      --list: prints one gate per line.
      default: exits 0 (pass).
    """
    body = (
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$STUB_DATA/gate_verify.log\"\n"
        "case \"$2\" in\n"
        "  --list)\n"
        "    echo '  bench       remote-task $REPO'\n"
        "    echo '  artifacts   redaction-check $REPO'\n"
        "    ;;\n"
        "  *)\n"
        "    echo '==> all gates passed: bench,artifacts'\n"
        "    ;;\n"
        "esac\n"
    )
    p = bin_dir / "gate-verify"
    p.write_text(body)
    p.chmod(0o755)


def write_stub_find_prose(bin_dir: Path) -> None:
    """Emits $STUB_DATA/find_prose.json whatever its arguments, or [] when absent."""
    body = (
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$STUB_DATA/find_prose.log\"\n"
        "# A scenario can ask the finder to exit non-zero by creating\n"
        "# $STUB_DATA/find_prose_fail: pr-ready is then expected to refuse\n"
        "# with a named failure rather than read an empty finding list as\n"
        "# a pass.\n"
        "if [ -e \"$STUB_DATA/find_prose_fail\" ]; then\n"
        "  echo \"find_prose: simulated crash\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "# The diff pass alone can be given its own crash or output, as a\n"
        "# merge-base failure breaks only that pass.\n"
        "case \" $* \" in *\" --diff \"*)\n"
        "  if [ -e \"$STUB_DATA/find_prose_diff_fail\" ]; then\n"
        "    echo \"find_prose: simulated crash\" >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  if [ -e \"$STUB_DATA/find_prose_diff.json\" ]; then\n"
        "    cat \"$STUB_DATA/find_prose_diff.json\"\n"
        "    exit 0\n"
        "  fi ;;\n"
        "esac\n"
        "if [ -e \"$STUB_DATA/find_prose.json\" ]; then\n"
        "  cat \"$STUB_DATA/find_prose.json\"\n"
        "else\n"
        "  echo '[]'\n"
        "fi\n"
    )
    p = bin_dir / "find_prose.py"
    p.write_text(body)
    p.chmod(0o755)


def write_real_finder_wrapper(bin_dir: Path, real_finder: Path) -> Path:
    """The real finder behind a wrapper that logs each path it is given and keeps
    a copy of the file, so a test can check what pr-ready handed it.
    """
    body = (
        "#!/usr/bin/env bash\n"
        f"REAL_FINDER=\"{real_finder}\"\n"
        "# Log every invocation: a single line of \"$@\" so the test can\n"
        "# assert the path ends in `.md` -- the regression the wrapper\n"
        "# exists to catch.\n"
        "printf '%s\\n' \"$*\" >> \"$STUB_DATA/real_finder.log\"\n"
        "# Copy the body file (the last arg) so the test can read back\n"
        "# what the finder actually scanned. A regression that writes\n"
        "# the fetched JSON to the `.md` file instead of the decoded\n"
        "# body would still pass an extension check, but the content\n"
        "# would not match what the test sent through the stub `gh`.\n"
        "BODY_PATH=\"${@: -1}\"\n"
        "if [ -e \"$BODY_PATH\" ]; then\n"
        "  cp \"$BODY_PATH\" \"$STUB_DATA/real_finder_body\"\n"
        "fi\n"
        "exec \"$REAL_FINDER\" \"$@\"\n"
    )
    p = bin_dir / "real_finder_wrapper.sh"
    p.write_text(body)
    p.chmod(0o755)
    return p


def install_fixtures(data_dir: Path, scenario: str, oid: str,
                     branch: str, body: str | None = None) -> None:
    """Copy a scenario's canned JSON into data_dir with the test repo's HEAD
    and branch substituted, so the pushed-head comparison passes.

    ``body`` is what ``gh pr view --json body`` returns; None leaves the
    field out.
    """
    pr_view = {
        "state": "OPEN",
        "isDraft": True,
        "headRefName": branch,
        "headRefOid": oid,
        "mergeable": "MERGEABLE",
    }
    if body is not None:
        pr_view["body"] = body
    (data_dir / "pr_view.json").write_text(json.dumps(pr_view))

    if scenario == "all_pass":
        shutil.copy(FIXTURES / "gh_pr_checks_pass.json",
                    data_dir / "pr_checks.json")
    elif scenario == "real_failure":
        shutil.copy(FIXTURES / "gh_pr_checks_failed.json",
                    data_dir / "pr_checks.json")
        shutil.copy(FIXTURES / "gh_run_view_failed.json",
                    data_dir / "run_2001.json")
    elif scenario == "never_started":
        shutil.copy(FIXTURES / "gh_pr_checks_never_started.json",
                    data_dir / "pr_checks.json")
        shutil.copy(FIXTURES / "gh_run_view_never_started.json",
                    data_dir / "run_3001.json")
    elif scenario == "no_checks":
        # A head with no checks at all: what a repository that runs no
        # CI reports for every head, and what a head that never reached
        # GitHub reports too.
        (data_dir / "pr_checks.json").write_text("[]")
    elif scenario == "mixed":
        shutil.copy(FIXTURES / "gh_pr_checks_mixed.json",
                    data_dir / "pr_checks.json")
        shutil.copy(FIXTURES / "gh_run_view_mixed_5001.json",
                    data_dir / "run_5001.json")
        shutil.copy(FIXTURES / "gh_run_view_mixed_5002.json",
                    data_dir / "run_5002.json")
    else:
        raise AssertionError(f"unknown scenario: {scenario}")


def run_pr_ready(worktree: Path, stub_path: Path, data_dir: Path,
                 *extra: str,
                 finder: str | None = None,
                 condense: bool = False) -> subprocess.CompletedProcess:
    """Run pr-ready with the stubs first on PATH and STUB_DATA at the
    scenario. ``finder`` overrides CONDENSE_PROSE, a missing path standing
    for a finder that is not installed. ``condense`` runs the diff pass,
    which every other scenario skips with --no-condense.
    """
    env = os.environ.copy()
    env["PATH"] = f"{stub_path}:{env['PATH']}"
    env["STUB_DATA"] = str(data_dir)
    if finder is None:
        env["CONDENSE_PROSE"] = str(stub_path / "find_prose.py")
    else:
        env["CONDENSE_PROSE"] = finder
    env.pop("PR_READY_BASE", None)
    env["HOME"] = str(worktree.parent)
    return subprocess.run(
        ["bash", str(PR_READY), "42", str(worktree),
         *([] if condense else ["--no-condense"]), *extra],
        capture_output=True, text=True, env=env,
    )


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        worktree, branch, oid = make_repo(tmp / "src")

        # One stub dir shared across scenarios; the per-scenario data dir
        # is what the stub reads from, so swapping fixtures swaps the
        # canned `gh` behaviour without rewriting the script.
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        write_stub_gh(bin_dir, bin_dir)
        write_stub_gate_verify(bin_dir)
        write_stub_find_prose(bin_dir)

        try:
            # Scenario 1: every CI check has passed. Promote, no
            # annotation needed (no never-started checks to call out).
            data = tmp / "data_all_pass"
            data.mkdir()
            install_fixtures(data, "all_pass", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data, "--check")
            if r.returncode != 0:
                failures.append(
                    f"all_pass --check should exit 0: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r} stdout={r.stdout.strip()!r}"
                )
            else:
                # --check must not have posted an annotation, and must
                # not have called `gh pr ready`. Both are writes.
                if (data / "comment_body.txt").exists():
                    failures.append(
                        "all_pass --check posted a comment: "
                        f"{(data / 'comment_body.txt').read_text()!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "all_pass --check called `gh pr ready`"
                    )

            # Same scenario without --check: must call `gh pr ready`
            # (the PR is a draft) and post no annotation.
            data = tmp / "data_all_pass_real"
            data.mkdir()
            install_fixtures(data, "all_pass", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"all_pass (promote) should exit 0: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "all_pass (promote) did not call `gh pr ready`"
                    )
                if (data / "comment_body.txt").exists():
                    failures.append(
                        "all_pass (promote) posted a comment without "
                        "any never-started check to annotate"
                    )

            # Scenario 2: one check failed for real (steps present).
            # Refuse. The reason must name the failing check, and
            # crucially must not claim the account is at fault.
            data = tmp / "data_real_failure"
            data.mkdir()
            install_fixtures(data, "real_failure", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "real_failure should refuse (exit 1): "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "bench" not in msg:
                    failures.append(
                        f"real_failure reason did not name 'bench': "
                        f"{msg!r}"
                    )
                # The fix the issue names: nothing pr-ready prints may
                # say the cause is on the account rather than the
                # branch. Refusing to promote here is about the
                # branch -- the check ran and produced a failure --
                # so the reason must read as such.
                for word in ("quota", "account", "runner not",
                             "never started", "no evidence"):
                    if word in msg.lower():
                        failures.append(
                            f"real_failure reason frames a real "
                            f"failure as '{word}': {msg!r}"
                        )
                if (data / "ready_called").exists():
                    failures.append(
                        "real_failure promoted after refusing"
                    )

            # Scenario 3: one check reports bucket=fail but the
            # matching job has zero steps. Promote, annotate, do not
            # refuse.
            data = tmp / "data_never_started"
            data.mkdir()
            install_fixtures(data, "never_started", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"never_started should promote (exit 0): "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "never_started did not call `gh pr ready`"
                    )
                body_file = data / "comment_body.txt"
                if not body_file.exists():
                    failures.append(
                        "never_started did not post an annotation"
                    )
                else:
                    body = body_file.read_text()
                    if "bench" not in body:
                        failures.append(
                            f"annotation did not name the never-started "
                            f"check: {body!r}"
                        )
                    if oid[:7] not in body:
                        failures.append(
                            f"annotation did not name the local sha "
                            f"({oid[:7]}): {body!r}"
                        )

            # Same scenario with --no-verify: the annotation must say
            # no local gates ran, because --no-verify skipped them.
            # A check name happens to overlap with a gate name in the
            # canonical fixture, so we read the gate-list line out of
            # the annotation rather than scanning for the names --
            # what the test really pins is that the annotation's
            # gate section says none ran, not that no gate name string
            # appears anywhere in the file.
            data = tmp / "data_never_started_no_verify"
            data.mkdir()
            install_fixtures(data, "never_started", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data, "--no-verify")
            if r.returncode != 0:
                failures.append(
                    f"never_started --no-verify should promote: "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            elif (data / "comment_body.txt").exists():
                body = (data / "comment_body.txt").read_text()
                gates_line = [
                    ln for ln in body.splitlines()
                    if ln.startswith("Local gates run")
                ]
                if not gates_line:
                    failures.append(
                        f"--no-verify annotation missing 'Local gates "
                        f"run' line: {body!r}"
                    )
                elif "none" not in gates_line[0].lower():
                    failures.append(
                        f"--no-verify annotation lists local gates that "
                        f"did not run: {gates_line[0]!r}"
                    )

            # Mixed: one check failed for real (steps present), one
            # never started (steps empty). The issue calls this the case
            # to get right: the obvious "promote under no evidence"
            # branch would also promote a real failure that happens to
            # sit alongside a never-started one. The verdict must
            # refuse, and the reason must name the real failure -- not
            # the never-started one, and not both.
            data = tmp / "data_mixed"
            data.mkdir()
            install_fixtures(data, "mixed", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "mixed (real failure + never-started) should refuse "
                    f"(exit 1): stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "real-failure" not in msg:
                    failures.append(
                        f"mixed reason did not name the real failure "
                        f"('real-failure'): {msg!r}"
                    )
                if "quota-blocked" in msg:
                    failures.append(
                        "mixed reason named the never-started check "
                        "as a reason to refuse: "
                        f"{msg!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "mixed promoted after refusing"
                    )

            # Scenario 5: the run fetches, but carries no job with the
            # id the check's link names. That is a run we could not
            # inspect, not a run with no steps, and reading it as
            # never-started promotes a check that ran and failed.
            data = tmp / "data_job_absent"
            data.mkdir()
            install_fixtures(data, "real_failure", oid, branch)
            run = json.loads((data / "run_2001.json").read_text())
            for job in run["jobs"]:
                job["databaseId"] = 999999
            (data / "run_2001.json").write_text(json.dumps(run))
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "job absent from run: promoted a failing check "
                    f"because its job id matched nothing: "
                    f"stderr={r.stderr.strip()!r}"
                )
            if (data / "ready_called").exists():
                failures.append(
                    "job absent from run: called `gh pr ready`"
                )

            # Scenario 6: the PR is already ready. The script documents
            # re-running as a no-op, and posting another annotation on
            # every run is not one.
            data = tmp / "data_already_ready"
            data.mkdir()
            install_fixtures(data, "never_started", oid, branch)
            pv = json.loads((data / "pr_view.json").read_text())
            pv["isDraft"] = False
            (data / "pr_view.json").write_text(json.dumps(pv))
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"already-ready should exit 0: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r}"
                )
            if (data / "comment_body.txt").exists():
                failures.append(
                    "already-ready re-run posted another annotation: "
                    f"{(data / 'comment_body.txt').read_text()!r}"
                )

            # Scenario 7: a `provenance` finding in the body refuses, naming the
            # body line, on a branch whose CI would otherwise promote.
            data = tmp / "data_body_provenance"
            data.mkdir()
            install_fixtures(
                data, "never_started", oid, branch,
                body=(
                    "# What changed\n"
                    "\n"
                    "Refuses on a provenance link in the body.\n"
                    "\n"
                    "Session: https://hermes.example/sessions/abc\n"
                ),
            )
            (data / "find_prose.json").write_text(json.dumps([
                {
                    "path": "PR_BODY",
                    "line": 5,
                    "kind": "list-item",
                    "words": 6,
                    "findings": ["provenance"],
                    "text": "Session: https://hermes.example/sessions/abc",
                },
            ]))
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "body_provenance should refuse (exit 1): "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "provenance" not in msg:
                    failures.append(
                        f"body_provenance reason did not name 'provenance': "
                        f"{msg!r}"
                    )
                if "line 5" not in msg:
                    failures.append(
                        f"body_provenance reason did not name the offending "
                        f"line number (5): {msg!r}"
                    )
                # The mktemp file handed to the finder is gone by now.
                if "Session: https://hermes.example/sessions/abc" not in msg:
                    failures.append(
                        f"body_provenance reason did not name the body "
                        f"line text: {msg!r}"
                    )
                if "pr-ready-body-" in msg:
                    failures.append(
                        f"body_provenance reason named the mktemp path "
                        f"(deleted before the refusal prints): {msg!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "body_provenance promoted after refusing"
                    )

            # Scenario 8: the body carries no `provenance` finding. The
            # same stub setup emits a non-provenance finding (oversize)
            # to prove the filter distinguishes kinds: a non-provenance
            # finding in the body is advisory and must not refuse.
            data = tmp / "data_body_no_provenance"
            data.mkdir()
            install_fixtures(
                data, "never_started", oid, branch,
                body="# A heading\n\nA short body that does not link anywhere.\n",
            )
            (data / "find_prose.json").write_text(json.dumps([
                {
                    "path": "PR_BODY",
                    "line": 1,
                    "kind": "heading",
                    "words": 3,
                    "findings": ["oversize"],
                    "text": "A heading",
                },
            ]))
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"body_no_provenance should promote (exit 0): "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "body_no_provenance did not call `gh pr ready`"
                    )

            # Scenario 9: --no-condense does not waive a provenance finding.
            data = tmp / "data_body_provenance_no_condense"
            data.mkdir()
            install_fixtures(
                data, "never_started", oid, branch,
                body=(
                    "# What changed\n"
                    "\n"
                    "Refuses on a provenance link in the body.\n"
                    "\n"
                    "Session: https://hermes.example/sessions/abc\n"
                ),
            )
            (data / "find_prose.json").write_text(json.dumps([
                {
                    "path": "PR_BODY",
                    "line": 5,
                    "kind": "list-item",
                    "words": 6,
                    "findings": ["provenance"],
                    "text": "Session: https://hermes.example/sessions/abc",
                },
            ]))
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "body_provenance with --no-condense should still "
                    f"refuse: stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "provenance" not in msg:
                    failures.append(
                        f"--no-condense reason did not name 'provenance': "
                        f"{msg!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "--no-condense bypassed the body check"
                    )

            # Scenario 10: a body that could not be fetched refuses, naming the
            # failure.
            data = tmp / "data_body_fetch_fail"
            data.mkdir()
            install_fixtures(
                data, "never_started", oid, branch,
                body="A body that should never be scanned, because "
                     "the fetch should fail.\n",
            )
            (data / "gh_body_fetch_fail").write_text("")
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "body_fetch_fail should refuse (exit 1): "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "could not be fetched" not in msg:
                    failures.append(
                        f"body_fetch_fail reason did not name the "
                        f"fetch failure: {msg!r}"
                    )
                if "gh: could not fetch PR body (simulated)" not in msg:
                    failures.append(
                        f"body_fetch_fail reason did not surface the "
                        f"underlying gh error: {msg!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "body_fetch_fail promoted after refusing"
                    )
                # The fetch failure should refuse before the finder
                # is even invoked; if the body check did fall through,
                # ready_called would not exist, but neither would any
                # provenance refuse line.
                if "provenance" in msg:
                    failures.append(
                        f"body_fetch_fail refused for the wrong "
                        f"reason (provenance): {msg!r}"
                    )

            # Scenario 11: a finder that exits non-zero refuses, naming the failure.
            data = tmp / "data_finder_fail"
            data.mkdir()
            install_fixtures(
                data, "never_started", oid, branch,
                body=(
                    "# What changed\n"
                    "\n"
                    "Refuses on a finder crash, not on a finding.\n"
                ),
            )
            (data / "find_prose_fail").write_text("")
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "finder_fail should refuse (exit 1): "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "finder failed" not in msg:
                    failures.append(
                        f"finder_fail reason did not name the finder "
                        f"failure: {msg!r}"
                    )
                if "find_prose: simulated crash" not in msg:
                    failures.append(
                        f"finder_fail reason did not surface the "
                        f"underlying finder error: {msg!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "finder_fail promoted after refusing"
                    )
                if "provenance" in msg:
                    failures.append(
                        f"finder_fail refused for the wrong reason "
                        f"(provenance): {msg!r}"
                    )

            # Scenario 12: with no finder installed the PR promotes, and stderr says
            # the body was not scanned.
            data = tmp / "data_finder_absent"
            data.mkdir()
            install_fixtures(
                data, "never_started", oid, branch,
                body="# A heading\n\nA body with no provenance.\n",
            )
            r = run_pr_ready(
                worktree, bin_dir, data,
                finder=str(tmp / "no-such-finder.py"),
            )
            if r.returncode != 0:
                failures.append(
                    f"finder_absent should promote (exit 0): "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "finder_absent did not call `gh pr ready`"
                    )
                # Any wording passes as long as it says the body was skipped.
                notes = [
                    ln for ln in r.stderr.splitlines()
                    if ln.startswith("note:") and "body" in ln
                ]
                if not notes:
                    failures.append(
                        f"finder_absent did not print a body-check "
                        f"note: stderr={r.stderr!r}"
                    )

            # Scenarios 13-14 run the real finder, which, unlike the stub,
            # reads a file only by its extension. An oversize paragraph
            # proves the finder read the body pr-ready handed it.
            real_finder = REPO_ROOT / "skills" / "condense-prose" / "find_prose.py"
            wrapper = write_real_finder_wrapper(bin_dir, real_finder)

            # Scenario 13: the body file pr-ready passes ends in `.md`, and
            # the real finder reports the paragraph; oversize is advisory.
            data = tmp / "data_real_finder_oversize"
            data.mkdir()
            oversize_body = (
                "# What changed\n"
                "\n"
                "This body paragraph runs well past the forty words the finder "
                "allows a single block, so the real finder has a finding to "
                "report about it, and that finding can only appear if the "
                "finder opened the file pr-ready wrote and read it as prose "
                "rather than skipping it for its name.\n"
            )
            install_fixtures(
                data, "never_started", oid, branch, body=oversize_body,
            )
            r = run_pr_ready(worktree, bin_dir, data, finder=str(wrapper))
            if r.returncode != 0:
                failures.append(
                    f"real_finder_oversize should promote (exit 0): "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "real_finder_oversize did not call `gh pr ready`"
                    )
                log_path = data / "real_finder.log"
                if not log_path.exists():
                    failures.append(
                        "real_finder_oversize: wrapper was never invoked -- "
                        "the body file the finder reads is what this test "
                        "is meant to confirm, and a missing log means it "
                        "was never passed anything"
                    )
                else:
                    log_text = log_path.read_text()
                    # The last token of the wrapper's log line is the path pr-ready passed.
                    body_paths = [
                        ln.split()[-1] for ln in log_text.splitlines() if ln
                    ]
                    md_paths = [p for p in body_paths if p.endswith(".md")]
                    if not md_paths:
                        failures.append(
                            "real_finder_oversize: no body path passed to "
                            "the finder ended in `.md`, so the finder "
                            "skipped it. Wrapper log:\n"
                            f"{log_text!r}"
                        )
                    else:
                        # Verify the wrapper captured the right content.
                        # A regression that wrote the fetched JSON to the
                        # body file (or any other string) would still pass
                        # the extension check, but the content here would
                        # not match what the stub `gh` returned.
                        captured_body_path = data / "real_finder_body"
                        if not captured_body_path.exists():
                            failures.append(
                                "real_finder_oversize: wrapper did not "
                                "copy the body file the finder scanned"
                            )
                        else:
                            captured = captured_body_path.read_text()
                            if captured.strip() != oversize_body.strip():
                                failures.append(
                                    "real_finder_oversize: body the "
                                    "finder scanned does not match the "
                                    "body the stub `gh` returned. "
                                    f"Expected {oversize_body!r}, "
                                    f"got {captured!r}"
                                )
                        # The real finder, run on the same body as a `.md` file, reports it.
                        sample_body = data / "sample_body.md"
                        sample_body.write_text(oversize_body)
                        finder_proc = subprocess.run(
                            [str(real_finder), "--json", str(sample_body)],
                            capture_output=True, text=True,
                        )
                        try:
                            findings = json.loads(finder_proc.stdout)
                        except json.JSONDecodeError:
                            findings = []
                        kinds = [
                            f.get("findings", [])
                            for f in findings
                            if isinstance(f, dict)
                        ]
                        if "oversize" not in [k for sub in kinds for k in sub]:
                            failures.append(
                                "real_finder_oversize: the real finder "
                                "did not emit an `oversize` finding for "
                                "the oversize body -- the file format "
                                "the test pins is not the one the real "
                                "finder reads. Findings: "
                                f"{findings!r}"
                            )
                        sample_body.unlink()

            # Scenario 14: a short body the real finder has nothing to
            # say about. pr-ready promotes, the wrapper still received a
            # `.md` path (which is the regression assertion), and the
            # real finder run directly against the same body emits no
            # findings.
            data = tmp / "data_real_finder_ordinary"
            data.mkdir()
            ordinary_body = "# A heading\n\nA short body that links nowhere.\n"
            install_fixtures(
                data, "never_started", oid, branch, body=ordinary_body,
            )
            r = run_pr_ready(worktree, bin_dir, data, finder=str(wrapper))
            if r.returncode != 0:
                failures.append(
                    f"real_finder_ordinary should promote (exit 0): "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "real_finder_ordinary did not call `gh pr ready`"
                    )
                log_path = data / "real_finder.log"
                if not log_path.exists():
                    failures.append(
                        "real_finder_ordinary: wrapper was never invoked"
                    )
                else:
                    log_text = log_path.read_text()
                    body_paths = [
                        ln.split()[-1] for ln in log_text.splitlines() if ln
                    ]
                    md_paths = [p for p in body_paths if p.endswith(".md")]
                    if not md_paths:
                        failures.append(
                            "real_finder_ordinary: no body path passed to "
                            "the finder ended in `.md`. Wrapper log:\n"
                            f"{log_text!r}"
                        )
                    else:
                        # A body the real finder has nothing to say about.
                        sample_body = data / "sample_body.md"
                        sample_body.write_text(ordinary_body)
                        finder_proc = subprocess.run(
                            [str(real_finder), "--json", str(sample_body)],
                            capture_output=True, text=True,
                        )
                        try:
                            findings = json.loads(finder_proc.stdout)
                        except json.JSONDecodeError:
                            findings = []
                        if findings:
                            failures.append(
                                "real_finder_ordinary: the real finder "
                                "emitted findings against the ordinary "
                                "body, so this scenario no longer pins "
                                "the case it claims to. Findings: "
                                f"{findings!r}"
                            )
                        sample_body.unlink()

            # Scenarios 15-18 run the diff pass. A finder that crashed or
            # printed something unreadable must refuse with a reason and exit
            # 1, not end the script on the finder's own status.
            def diff_pass(name, finder_out=None, crash=False):
                data = tmp / f"data_diff_{name}"
                data.mkdir()
                install_fixtures(data, "never_started", oid, branch,
                                 body="# What changed\n\nA short body.\n")
                if finder_out is not None:
                    (data / "find_prose_diff.json").write_text(finder_out)
                if crash:
                    (data / "find_prose_diff_fail").write_text("")
                return data, run_pr_ready(worktree, bin_dir, data, condense=True)

            # Scenario 15: the finder exits non-zero on the diff.
            data, r = diff_pass("crash", crash=True)
            if r.returncode != 1 or (data / "ready_called").exists():
                failures.append(f"diff_crash should refuse: stderr={r.stderr.strip()!r}")
            elif ("finder failed on the diff" not in r.stderr
                  or "find_prose: simulated crash" not in r.stderr):
                failures.append(f"diff_crash did not name the failure: {r.stderr!r}")

            # Scenario 16: the finder prints something other than a JSON array.
            data, r = diff_pass("garbage", finder_out="Traceback: not json\n")
            if r.returncode != 1 or (data / "ready_called").exists():
                failures.append(f"diff_garbage should refuse: stderr={r.stderr.strip()!r}")
            elif "other than a JSON array" not in r.stderr:
                failures.append(f"diff_garbage did not name the failure: {r.stderr!r}")

            # Scenario 17: an empty finding list promotes.
            data, r = diff_pass("clean", finder_out="[]\n")
            if r.returncode != 0 or not (data / "ready_called").exists():
                failures.append(f"diff_clean should promote: rc={r.returncode} "
                                f"stderr={r.stderr.strip()!r}")

            # Scenario 18: a finding refuses, as before.
            data, r = diff_pass("flagged", finder_out=json.dumps(
                [{"path": "a.md", "line": 1, "text": "long", "findings": ["oversize"]}]))
            if r.returncode == 0 or (data / "ready_called").exists():
                failures.append(f"diff_flagged should refuse: stderr={r.stderr.strip()!r}")
            elif "flagged 1 block(s)" not in r.stderr:
                failures.append(f"diff_flagged did not count the block: {r.stderr!r}")

            # Scenarios 19-24: a repository that runs no workflows, where
            # every head reports no checks. The count is asked for only
            # when there are no checks at all.
            def no_ci(name, workflows=0, count_fails=False, prepare=None):
                """A head with no checks, in a repository whose workflow
                count the scenario sets."""
                data = tmp / f"data_no_ci_{name}"
                data.mkdir()
                install_fixtures(data, "no_checks", oid, branch)
                if count_fails:
                    (data / "gh_workflow_count_fail").write_text("")
                else:
                    (data / "workflow_count").write_text(str(workflows))
                if prepare is not None:
                    prepare(data)
                return data, run_pr_ready(worktree, bin_dir, data)

            def stale_head(data):
                """The PR's pushed head differs from the local head."""
                pv = json.loads((data / "pr_view.json").read_text())
                pv["headRefOid"] = "0" * 40
                (data / "pr_view.json").write_text(json.dumps(pv))

            def dirty_tree(data):
                """The worktree has an uncommitted change."""
                (worktree / "README").write_text("test\nuncommitted\n")

            # Scenario 19: no workflows, no checks, clean tree, local head
            # is the pushed head. The PR is promoted, and stderr says that
            # no CI applies.
            data, r = no_ci("no_workflows")
            if r.returncode != 0:
                failures.append(
                    f"no_ci (no workflows) should promote: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "no_ci (no workflows) did not call `gh pr ready`"
                    )
                if "note: this repository has no workflows; no CI applies" not in r.stderr:
                    failures.append(
                        "no_ci (no workflows) did not say that no CI "
                        f"applies: {r.stderr!r}"
                    )

            # Scenario 20: no workflows, and the local head differs from
            # the pushed head. The head never reached GitHub, so it is
            # refused.
            data, r = no_ci("stale_head", prepare=stale_head)
            if r.returncode == 0:
                failures.append(
                    "no_ci (stale head) should refuse: "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if "not the pushed head" not in r.stderr:
                    failures.append(
                        "no_ci (stale head) did not name the pushed head: "
                        f"{r.stderr!r}"
                    )
                if "no CI checks reported" in r.stderr:
                    failures.append(
                        "no_ci (stale head) refused for the checks: "
                        f"{r.stderr!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "no_ci (stale head) promoted after refusing"
                    )

            # Scenario 21: no workflows, and the worktree is dirty. The
            # refusal names the dirty tree.
            data, r = no_ci("dirty_tree", prepare=dirty_tree)
            subprocess.run(["git", "-C", str(worktree), "checkout", "--",
                            "README"], check=True)
            if r.returncode == 0:
                failures.append(
                    "no_ci (dirty tree) should refuse: "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if "uncommitted changes" not in r.stderr:
                    failures.append(
                        "no_ci (dirty tree) did not name the uncommitted "
                        f"changes: {r.stderr!r}"
                    )
                if "no CI checks reported" in r.stderr:
                    failures.append(
                        "no_ci (dirty tree) refused for the checks: "
                        f"{r.stderr!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "no_ci (dirty tree) promoted after refusing"
                    )

            # Scenario 22: the repository runs two workflows and reports
            # no checks for this head. The head's checks have not
            # registered yet, so it is refused.
            data, r = no_ci("with_workflows", workflows=2)
            if r.returncode == 0:
                failures.append(
                    "no_ci (repository has workflows) should refuse: "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if "no CI checks reported" not in r.stderr:
                    failures.append(
                        "no_ci (repository has workflows) did not name the "
                        f"missing checks: {r.stderr!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "no_ci (repository has workflows) promoted after "
                        "refusing"
                    )

            # Scenario 23: the workflow count could not be read. A failed
            # count refuses.
            data, r = no_ci("count_fails", count_fails=True)
            if r.returncode == 0:
                failures.append(
                    "no_ci (count fails) should refuse: "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if "could not count the repository's workflows" not in r.stderr:
                    failures.append(
                        "no_ci (count fails) did not name the workflow "
                        f"count: {r.stderr!r}"
                    )
                if "could not count workflows (simulated)" not in r.stderr:
                    failures.append(
                        "no_ci (count fails) did not surface the gh "
                        f"error: {r.stderr!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "no_ci (count fails) promoted after refusing"
                    )

            # Scenario 24: a PR whose checks have passed. The workflow
            # count is skipped: the repository's CI has already answered
            # it.
            data = tmp / "data_no_api_call"
            data.mkdir()
            install_fixtures(data, "all_pass", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"no_api_call should promote: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "no_api_call did not call `gh pr ready`"
                    )
                log = data / "gh.log"
                api_calls = []
                if log.exists():
                    api_calls = [
                        ln for ln in log.read_text().splitlines()
                        if ln.startswith("api")
                    ]
                if api_calls:
                    failures.append(
                        "no_api_call asked for the workflow count on a PR "
                        f"with checks: {api_calls!r}"
                    )

        except AssertionError as e:
            failures.append(f"unexpected assertion error: {e}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
