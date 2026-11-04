#!/usr/bin/env python3
"""Pin the parent-green comparison against the cases the issue separates.

The fixtures are recorded API responses, in the shape `gh api` prints.
The decision is a pure function over them, so nothing reaches the
network.

Run directly: `python3 tests/test_main_watch.py`.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE = REPO_ROOT / "lib" / "main_watch.py"


def _load_module():
    """Load `lib/main_watch.py` as an importable module."""
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    tmp.close()
    shutil.copy(str(MODULE), tmp.name)
    spec = importlib.util.spec_from_file_location("_main_watch_under_test",
                                                  tmp.name)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE} as a module")
    mod = importlib.util.module_from_spec(spec)
    # A dataclass reads its own module out of sys.modules, so the module
    # has to be registered before it is executed.
    sys.modules["_main_watch_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


mw = _load_module()


# ----- recorded API responses ----------------------------------------------

def check_run(name, status="completed", conclusion="success", url=""):
    """One check run, as the API prints it.

    `conclusion` may be None: a finished run with no conclusion is a
    shape the API produces, and it is no pass.
    """
    return {"name": name, "status": status, "conclusion": conclusion,
            "html_url": url}


def status_context(context, state="success", url=""):
    return {"context": context, "state": state, "target_url": url}


# The shapes the API prints, recorded verbatim.
FIXTURES = {
    # A merge that broke the branch: `Gates` was green on the parent and
    # red on the merge.
    "broke_the_branch": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "failure",
                                     "https://ci/1")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success",
                                     "https://ci/0")],
            "statuses": [],
        },
        "expected": "broke",
    },
    # The branch was already red before the merge. The merge is red too,
    # but it did not break the branch.
    "already_red": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "failure")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "failure")],
            "statuses": [],
        },
        "expected": "already_red",
    },
    # The commit's checks have not finished. No verdict yet.
    "still_running": {
        "commit": {
            "check_runs": [check_run("Gates", "in_progress", None)],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "pending",
    },
    # Green, on a green parent.
    "green": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "green",
    },
    # A check the parent never ran: the parent cannot be shown to have
    # been green on it, so the redness is reported but not attributed.
    "check_the_parent_never_ran": {
        "commit": {
            "check_runs": [check_run("New-Checks", "completed", "failure")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "already_red",
    },
    # The same check, reported by both sources: a run and a status. The
    # run's conclusion wins.
    "run_and_status_same_name": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "failure")],
            "statuses": [status_context("gates", "success")],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [status_context("gates", "success")],
        },
        "expected": "broke",
    },
    # A cancelled run is no failure: a later push cancels the run it
    # supersedes.
    "cancelled_is_not_a_failure": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "cancelled")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "green",
    },
    # A skipped run is a decision, not a failure.
    "skipped_is_not_a_failure": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "skipped")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "green",
    },
    # A finished run with no conclusion is unknown, and unknown is no
    # pass.
    "no_conclusion_is_unknown": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", None)],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "pending",
    },
    # A commit with no checks at all: nothing was proved, so nothing is
    # green.
    "no_checks_at_all": {
        "commit": {
            "check_runs": [],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "pending",
    },
    # A commit status that failed, with no check run at all.
    "status_failure_only": {
        "commit": {
            "check_runs": [],
            "statuses": [status_context("ci/legacy", "failure")],
        },
        "parent": {
            "check_runs": [],
            "statuses": [status_context("ci/legacy", "success")],
        },
        "expected": "broke",
    },
    # A status still expected: not evidence either way.
    "status_pending": {
        "commit": {
            "check_runs": [],
            "statuses": [status_context("ci/legacy", "pending")],
        },
        "parent": {
            "check_runs": [],
            "statuses": [status_context("ci/legacy", "success")],
        },
        "expected": "pending",
    },
    # A timeout is a failure.
    "timed_out_is_a_failure": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "timed_out")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "broke",
    },
    # One red check and one still running: the red wins, because a check
    # that has failed is evidence against the branch even while another
    # is running.
    "red_beats_pending": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "failure"),
                           check_run("Other", "in_progress", None)],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success"),
                           check_run("Other", "completed", "success")],
            "statuses": [],
        },
        "expected": "broke",
    },
    # The parent's checks have yet to finish, so the verdict is pending.

    # It is never "already red": the parent is unknown.
    "parent_unknown": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "failure")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "in_progress", None)],
            "statuses": [],
        },
        "expected": "pending",
    },
    # A check that was re-run. The API prints the runs newest first, so
    # the first entry is the latest one.

    # The re-run failed and the run before it passed: the check ended
    # red, so the commit really did leave the branch red.
    "rerun_keeps_the_failure": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "failure"),
                           check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "broke",
    },
    # The same, the other way round: the re-run passed and the run
    # before it failed. The failure is kept as evidence -- it really
    # happened -- but the check ended green, so the commit did not leave
    # the branch red.

    # It is reported as `flaky`, apart from `broke`, so the caller opens
    # no revert over it.
    "rerun_failure_last": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "success"),
                           check_run("Gates", "completed", "failure")],
            "statuses": [],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [],
        },
        "expected": "flaky",
    },
    # A commit status that failed, under a check run of the same name
    # that passed. The status is weaker evidence than the run, so the
    # run wins and the commit is green.
    "a_status_does_not_downgrade_a_run": {
        "commit": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [status_context("gates", "failure")],
        },
        "parent": {
            "check_runs": [check_run("Gates", "completed", "success")],
            "statuses": [status_context("gates", "success")],
        },
        "expected": "green",
    },
}


def verdict_for(fixture: dict):
    """The verdict the module reaches for one fixture."""
    commit = mw.collect_checks(fixture["commit"]["check_runs"],
                               fixture["commit"]["statuses"])
    parent = mw.collect_checks(fixture["parent"]["check_runs"],
                               fixture["parent"]["statuses"])
    return mw.compare(commit, parent)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")


def test_the_recorded_fixtures() -> None:
    """Every recorded fixture reaches its recorded verdict."""
    for name, fixture in sorted(FIXTURES.items()):
        verdict = verdict_for(fixture)
        check(name, verdict.kind == fixture["expected"],
              f"expected {fixture['expected']!r}, got {verdict.kind!r}")


def test_a_broken_branch_names_the_failing_check() -> None:
    """The verdict names the check that failed, for the report.

    A verdict that leaves it unnamed makes the report useless.
    """
    verdict = verdict_for(FIXTURES["broke_the_branch"])
    check("broke", verdict.broke, verdict.kind)
    check("one failing check", len(verdict.failing) == 1,
          str(verdict.failing))
    check("the check is named", verdict.failing[0].name == "Gates",
          verdict.failing[0].name)
    check("the url is kept", verdict.failing[0].url == "https://ci/1",
          verdict.failing[0].url)


def test_a_check_the_parent_never_ran_is_named() -> None:
    """A check the parent never ran is named, so the report can say so.

    The operator still needs to know which check it was.
    """
    verdict = verdict_for(FIXTURES["check_the_parent_never_ran"])
    check("not attributed", not verdict.broke, verdict.kind)
    check("the uncompared check is named",
          verdict.uncompared == ["New-Checks"], str(verdict.uncompared))


def test_the_parent_state_is_reported() -> None:
    """The verdict carries the parent's own state.

    "Already red" and "broke it" are different reports, and the report
    says which one it is.
    """
    broke = verdict_for(FIXTURES["broke_the_branch"])
    check("the parent was green", broke.parent_state == mw.GREEN,
          broke.parent_state)
    already = verdict_for(FIXTURES["already_red"])
    check("the parent was red", already.parent_state == mw.RED,
          already.parent_state)


def test_a_pending_verdict_is_not_a_break() -> None:
    """A commit whose checks have not finished has not broken anything.

    A verdict on a commit whose CI has not completed gets reversed when
    it does, so a pending commit must never read as a break.
    """
    for name in ("still_running", "no_checks_at_all"):
        verdict = verdict_for(FIXTURES[name])
        check(name, not verdict.broke, verdict.kind)
        check(name, verdict.kind == "pending", verdict.kind)


def test_the_case_of_the_check_names_does_not_matter() -> None:
    """A run and a status with the same name in different cases are one check.

    GitHub's two sources disagree about the case of a context name, and
    a comparison that missed one would call a real check uncompared.
    """
    commit = mw.collect_checks([check_run("Gates", "completed", "failure")],
                               [status_context("gates", "success")])
    check("one check", len(commit) == 1, str(commit))
    key = next(iter(commit))
    check("the run won", commit[key].state == mw.RED, commit[key].state)


def test_a_malformed_entry_is_refused() -> None:
    """A fetched entry outside the API's shape is refused.

    A check run with no `status` cannot be read: the reader cannot tell
    a green run from a red one, and a wrong answer here is a wrong
    verdict.
    """
    for bad in ([{"name": "Gates"}], ["not a dict"], [None]):
        try:
            mw.collect_checks(bad, [])
        except mw.Malformed:
            continue
        raise AssertionError(f"a malformed check run was accepted: {bad!r}")


def _stub_gh_chain(tmp: Path, tip: str, chain: list, tip_checks: list) -> Path:
    """A stub `gh` for a commit with a history behind it.

    `tip` is the commit the driver is asked about. `chain` is a list of
    `(sha, parent_sha, checks)` for the commits behind it.
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    data_dir = tmp / "data"
    data_dir.mkdir(exist_ok=True)

    commits = {}
    checks = {}
    for sha, parent_sha, runs in chain:
        parents = [{"sha": parent_sha}] if parent_sha else []
        commits[sha] = {"sha": sha, "parents": parents}
        checks[sha] = runs
    tip_parent = chain[0][0] if chain else ""
    commits[tip] = {"sha": tip,
                    "parents": [{"sha": tip_parent}] if tip_parent else []}
    checks[tip] = tip_checks

    (data_dir / "commits.json").write_text(json.dumps(commits))
    (data_dir / "checks.json").write_text(json.dumps(checks))

    # The stub is written flat, without indentation. A `textwrap.dedent`
    # cannot strip a block that holds a multi-line string at column 0,
    # so the shebang kept its indentation and the file stopped being
    # executable.
    body = (
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$STUB_DATA/gh.log\"\n"
        "case \"$2\" in\n"
        "  */commits/*/check-runs)\n"
        "    sha=$(echo \"$2\" | sed 's|.*/commits/||; s|/check-runs||')\n"
        "    python3 \"$STUB_DATA/checks.py\" \"$sha\"\n"
        "    ;;\n"
        "  */commits/*/status)\n"
        "    printf '{\"state\": \"success\", \"statuses\": []}'\n"
        "    ;;\n"
        "  */commits/*)\n"
        "    sha=$(echo \"$2\" | sed 's|.*/commits/||')\n"
        "    python3 \"$STUB_DATA/commit.py\" \"$sha\"\n"
        "    ;;\n"
        "  *)\n"
        "    echo \"stub gh: unhandled: $*\" >&2\n"
        "    exit 1\n"
        "    ;;\n"
        "esac\n")
    (data_dir / "checks.py").write_text(
        "import json, os, sys\n"
        "checks = json.load(open(os.environ['STUB_DATA'] + '/checks.json'))\n"
        "runs = checks.get(sys.argv[1], [])\n"
        "print(json.dumps({'check_runs': runs, 'total_count': len(runs)}))\n")
    (data_dir / "commit.py").write_text(
        "import json, os, sys\n"
        "commits = json.load(open(os.environ['STUB_DATA'] + '/commits.json'))\n"
        "print(json.dumps(commits.get(sys.argv[1],\n"
        "                       {'sha': sys.argv[1], 'parents': []})))\n")
    gh = bin_dir / "gh"
    gh.write_text(body)
    gh.chmod(0o755)
    return bin_dir


def test_a_rebase_merge_is_compared_against_the_base_it_landed_on() -> None:
    """A merge of three commits is compared against the base, not the parent.

    Merges here are rebase-only, so the tip's first parent carries no
    checks, and comparing against it read the merge as "main was
    already broken".
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        green = [{"name": "Gates", "status": "completed",
                  "conclusion": "success"}]
        red = [{"name": "Gates", "status": "completed",
                "conclusion": "failure"}]
        bin_dir = _stub_gh_chain(
            tmp,
            "tip-sha",
            [
                # The tip's parent: an intermediate commit of the
                # merge, with no checks at all.
                ("mid-sha", "base-sha", []),
                # The base: the `main` the merge landed on. It was
                # checked, and it was green.
                ("base-sha", "", green),
            ],
            tip_checks=red)
        data_dir = tmp / "data"
        result = _run_driver(bin_dir, data_dir, "owner/repo", "tip-sha")

        check("the driver succeeded", result.returncode == 0,
              f"exit {result.returncode}: {result.stderr!r}")
        check("the verdict is the break",
              result.stdout.startswith("BROKE"), result.stdout)
        # And it read the base, not the intermediate parent.
        log = (data_dir / "gh.log").read_text()
        check("the base's checks were read", "base-sha/check-runs" in log, log)


def test_a_base_that_is_still_running_is_pending() -> None:
    """A base whose checks are still running yields no verdict yet.

    Reading it as a break would revert a commit that may turn out to be
    fine.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        running = [{"name": "Gates", "status": "in_progress", "conclusion": None}]
        red = [{"name": "Gates", "status": "completed",
                "conclusion": "failure"}]
        bin_dir = _stub_gh_chain(
            tmp,
            "tip-sha",
            [("base-sha", "", running)],
            tip_checks=red)
        data_dir = tmp / "data"
        result = _run_driver(bin_dir, data_dir, "owner/repo", "tip-sha")

        check("the driver succeeded", result.returncode == 0,
              f"exit {result.returncode}: {result.stderr!r}")
        check("the verdict is pending",
              result.stdout.startswith("PENDING"), result.stdout)
        check("it is not a break", "BROKE" not in result.stdout, result.stdout)


def test_a_base_with_no_completed_checks_is_unattributed() -> None:
    """A base with no completed checks anywhere behind it is unattributed.

    Nothing was proved about it, so the redness is attributed to
    nothing. It is never "already red": nobody checked it.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        red = [{"name": "Gates", "status": "completed",
                "conclusion": "failure"}]
        bin_dir = _stub_gh_chain(
            tmp,
            "tip-sha",
            [
                ("mid-sha", "base-sha", []),
                ("base-sha", "", []),
            ],
            tip_checks=red)
        data_dir = tmp / "data"
        result = _run_driver(bin_dir, data_dir, "owner/repo", "tip-sha")

        check("the driver succeeded", result.returncode == 0,
              f"exit {result.returncode}: {result.stderr!r}")
        check("the verdict is unattributed",
              result.stdout.startswith("UNATTRIBUTED"), result.stdout)
        check("it is not a break", "BROKE" not in result.stdout, result.stdout)


def test_a_flaky_check_is_not_a_break() -> None:
    """A check that failed and then passed is no break.

    The check ended green, so the commit did not leave the branch red.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        green = [{"name": "Gates", "status": "completed",
                  "conclusion": "success"}]
        # Newest first, as the API prints it: the re-run passed, the
        # run before it failed.
        flaky = [{"name": "Gates", "status": "completed",
                  "conclusion": "success"},
                 {"name": "Gates", "status": "completed",
                  "conclusion": "failure"}]
        bin_dir = _stub_gh_chain(
            tmp,
            "tip-sha",
            [("base-sha", "", green)],
            tip_checks=flaky)
        data_dir = tmp / "data"
        result = _run_driver(bin_dir, data_dir, "owner/repo", "tip-sha")

        check("the driver succeeded", result.returncode == 0,
              f"exit {result.returncode}: {result.stderr!r}")
        check("the verdict is flaky",
              result.stdout.startswith("FLAKY"), result.stdout)
        check("it is not a break", "BROKE" not in result.stdout, result.stdout)


def _stub_gh(tmp: Path, commit_checks: list, parent_checks: list,
             parent_sha: str = "parent-sha") -> Path:
    """A stub `gh` that answers the three endpoints the driver reads.

    It answers from files under $STUB_DATA, so the test sets the canned
    JSON on disk. The endpoints are distinguished by their path.
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    data_dir = tmp / "data"
    data_dir.mkdir(exist_ok=True)

    commit_obj = {"sha": "commit-sha", "parents": [{"sha": parent_sha}]}
    (data_dir / "commit.json").write_text(json.dumps(commit_obj))
    (data_dir / "commit_checks.json").write_text(json.dumps(
        {"check_runs": commit_checks, "total_count": len(commit_checks)}))
    (data_dir / "commit_status.json").write_text(json.dumps(
        {"state": "success", "statuses": []}))
    (data_dir / "parent_checks.json").write_text(json.dumps(
        {"check_runs": parent_checks, "total_count": len(parent_checks)}))
    (data_dir / "parent_status.json").write_text(json.dumps(
        {"state": "success", "statuses": []}))

    body = textwrap.dedent("""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> "$STUB_DATA/gh.log"
        case "$2" in
          */commits/*/check-runs)
            if [[ "$2" == *"$STUB_COMMIT"* ]]; then
              cat "$STUB_DATA/commit_checks.json"
            else
              cat "$STUB_DATA/parent_checks.json"
            fi
            ;;
          */commits/*/status)
            if [[ "$2" == *"$STUB_COMMIT"* ]]; then
              cat "$STUB_DATA/commit_status.json"
            else
              cat "$STUB_DATA/parent_status.json"
            fi
            ;;
          */commits/*)
            cat "$STUB_DATA/commit.json"
            ;;
          *)
            echo "stub gh: unhandled: $*" >&2
            exit 1
            ;;
        esac
        """)
    gh = bin_dir / "gh"
    gh.write_text(body)
    gh.chmod(0o755)
    return bin_dir


def _run_driver(bin_dir: Path, data_dir: Path, *args: str):
    env = {**os.environ,
           "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "STUB_DATA": str(data_dir),
           "STUB_COMMIT": "commit-sha"}
    return subprocess.run(
        [str(REPO_ROOT / "bin" / "main-watch"), *args],
        capture_output=True, text=True, check=False, env=env,
    )


def test_the_driver_reads_the_commit_and_its_parents_checks() -> None:
    """The driver must read both the commit's checks and its parent's.

    A comparison with nothing always says the commit was green, so a
    broken branch would read as green.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = _stub_gh(
            tmp,
            commit_checks=[{"name": "Gates", "status": "completed",
                            "conclusion": "failure"}],
            parent_checks=[{"name": "Gates", "status": "completed",
                            "conclusion": "success"}],
        )
        data_dir = tmp / "data"
        result = _run_driver(bin_dir, data_dir, "owner/repo", "commit-sha")

        check("the driver succeeded", result.returncode == 0,
              f"exit {result.returncode}: {result.stderr!r}")
        check("the verdict is the break",
              result.stdout.startswith("BROKE"), result.stdout)
        check("the failing check is named", "Gates" in result.stdout,
              result.stdout)

        # Both commits' checks were read.
        log = (data_dir / "gh.log").read_text()
        check("the commit's checks were read",
              "commit-sha/check-runs" in log, log)
        check("the parent's checks were read",
              "parent-sha/check-runs" in log, log)


def test_a_failed_fetch_is_a_failure() -> None:
    """A fetch that fails is a failure, never a green branch.

    A branch whose checks could not be read has an unknown state, and
    unknown is no green.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        data_dir = tmp / "data"
        data_dir.mkdir()
        body = textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "gh: could not reach the API" >&2
            exit 1
            """)
        gh = bin_dir / "gh"
        gh.write_text(body)
        gh.chmod(0o755)

        result = _run_driver(bin_dir, data_dir, "owner/repo", "commit-sha")
        check("the driver failed", result.returncode != 0,
              f"exit {result.returncode}")
        check("the failure is named", result.stderr.startswith("ERR "),
              result.stderr)
        check("nothing was printed as a verdict",
              "GREEN" not in result.stdout, result.stdout)


def main() -> int:
    failures: list[str] = []
    for name, fn in [
        ("every recorded fixture reaches its recorded verdict",
         test_the_recorded_fixtures),
        ("a broken branch names the failing check",
         test_a_broken_branch_names_the_failing_check),
        ("a check the parent never ran is named",
         test_a_check_the_parent_never_ran_is_named),
        ("the parent's state is reported",
         test_the_parent_state_is_reported),
        ("a pending commit has not broken anything",
         test_a_pending_verdict_is_not_a_break),
        ("the case of a check name does not matter",
         test_the_case_of_the_check_names_does_not_matter),
        ("a malformed entry is refused",
         test_a_malformed_entry_is_refused),
        ("a rebase merge is compared against the base it landed on",
         test_a_rebase_merge_is_compared_against_the_base_it_landed_on),
        ("a base that is still running is pending",
         test_a_base_that_is_still_running_is_pending),
        ("a base with no completed checks is unattributed",
         test_a_base_with_no_completed_checks_is_unattributed),
        ("a flaky check is not a break",
         test_a_flaky_check_is_not_a_break),
        ("the driver reads the commit and its parent's checks",
         test_the_driver_reads_the_commit_and_its_parents_checks),
        ("a failed fetch is a failure, never a green branch",
         test_a_failed_fetch_is_a_failure),
    ]:
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{name}: {e}")
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures.append(f"{name}: {e!r}")
            print(f"FAIL {name}: {e!r}")
        else:
            print(f"ok   {name}")
    if failures:
        return 1
    print(f"PASS main_watch: {len(FIXTURES)} recorded fixtures, and the "
          f"cases the issue separates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
