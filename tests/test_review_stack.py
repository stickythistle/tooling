#!/usr/bin/env python3
"""Pin the stack review against the cases the issue names.

The defect a stacked PR hides is one that only exists when two pull
requests combine: the lower pull request's code drops a value the upper
pull request produces. Neither pull request's own diff shows the loss, so
neither review sees it. A review of the top of the stack against the
default branch does see it -- and then has to say which pull request of
the stack it belongs to, because the fix goes in one of them and not in
the other.

So the suite pins:

1. The stack is found from the pull request's own base, and the review
   runs against the default branch rather than the stack's bottom.
2. A defect the two pull requests create together yields one finding.
3. That finding is attributed to the lower pull request -- the one whose
   diff introduced the line the finding cites.
4. A finding on a line no pull request of the stack changed is reported
   against the stack as a whole, not against one of them.
5. A line the top of the stack edited but the bottom introduced belongs
   to the bottom.
6. A pending deletion pairs only with an addition in the same run of
   changed lines.
7. A stack that forks is refused, naming the siblings.

The repository is real: real git, real branches, a real bare origin. The
reviewer is stubbed, because a model is not what is under test -- the
attribution is. `gh` is stubbed at the network boundary, answering from
canned JSON on disk.
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
TOOL = REPO_ROOT / "bin" / "hermes-review-pr"
LIB = REPO_ROOT / "lib"
sys.path.insert(0, str(LIB))

import stack_attrib  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"{result.stderr.strip() or 'no stderr'}"
        )
    return result.stdout


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# The two-pull-request stack the issue names.
#
# `main` returns the value. The lower pull request (#1) replaces it with
# `None` -- the drop. The upper pull request (#2) adds a line above it and
# passes a cause in, so the value the upper pull request produces is the
# one the lower pull request discards. Neither diff shows the loss; only
# the combined tree shows that the argument is thrown away.
#
# The upper pull request's new line sits above the drop, so the drop is on
# line 3 of the lower pull request's tree and line 4 of the reviewed tree:
# an attribution that never carried positions forward gets it wrong. The
# first line is the constant neither pull request touches -- a finding on
# it belongs to the stack as a whole.
BASE_ROUTE = "MAX = 10\ndef route(x):\n    return x\n"
LOWER_ROUTE = "MAX = 10\ndef route(x):\n    return None\n"
UPPER_ROUTE = "MAX = 10\ndef route(x, cause):\n    cause = check(cause)\n    return None\n"

# The third pull request of the taller stack edits the line the bottom
# pull request introduced, so reviewed line 4 is claimed by two pull
# requests; the introducer owns it.
TOP_EDIT = "MAX = 10\ndef route(x, cause):\n    cause = check(cause)\n    return None  # the drop\n"


def build_stack(tmp: Path) -> Path:
    """A repository with a bare origin and two stacks.

    - `feat/lower` -> `feat/upper`: the stack the issue describes.
    - `alt/base` -> `alt/mid` -> `alt/top`: the taller stack, whose top
      edits the line its bottom introduced.

    Returns the worktree.
    """
    bare = tmp / "origin.git"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    repo = tmp / "worktree"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    write(repo / "route.py", BASE_ROUTE)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")

    # The lower pull request: it drops the value.
    git(repo, "checkout", "-q", "-b", "feat/lower")
    write(repo / "route.py", LOWER_ROUTE)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "lower: drop the value")
    git(repo, "push", "-q", "origin", "feat/lower")

    # The upper pull request: it produces the value the lower one drops.
    git(repo, "checkout", "-q", "-b", "feat/upper")
    write(repo / "route.py", UPPER_ROUTE)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "upper: produce the cause")
    git(repo, "push", "-q", "origin", "feat/upper")

    # The taller stack. Its bottom drops the value and its top edits the
    # line the bottom dropped.
    git(repo, "checkout", "-q", "-b", "alt/base")
    write(repo / "route.py", LOWER_ROUTE)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "alt base: drop the value")
    git(repo, "push", "-q", "origin", "alt/base")

    git(repo, "checkout", "-q", "-b", "alt/mid")
    write(repo / "route.py", UPPER_ROUTE)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "alt mid: produce the cause")
    git(repo, "push", "-q", "origin", "alt/mid")

    git(repo, "checkout", "-q", "-b", "alt/top")
    write(repo / "route.py", TOP_EDIT)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "alt top: edit the drop")
    git(repo, "push", "-q", "origin", "alt/top")

    git(repo, "checkout", "-q", "main")
    return repo


PR_LIST_JSON = [
    {"number": 1, "headRefName": "feat/lower", "baseRefName": "main",
     "title": "lower: drop the value"},
    {"number": 2, "headRefName": "feat/upper", "baseRefName": "feat/lower",
     "title": "upper: produce the cause"},
    {"number": 3, "headRefName": "feat/other", "baseRefName": "main",
     "title": "not part of the stack"},
    {"number": 10, "headRefName": "alt/base", "baseRefName": "main",
     "title": "alt base: drop the value"},
    {"number": 11, "headRefName": "alt/mid", "baseRefName": "alt/base",
     "title": "alt mid: produce the cause"},
    {"number": 12, "headRefName": "alt/top", "baseRefName": "alt/mid",
     "title": "alt top: edit the drop"},
]


def write_stub_gh(bin_dir: Path, data_dir: Path) -> None:
    """A stub `gh` that answers the subcommands the stack review invokes.

    `pr list` answers with the canned pull requests on disk, `pr view`
    with the canned one, and `repo view` with the default branch. Every
    call is logged, so a test can read back what the tool asked. `pr
    view` is called with --jq, so the stub pipes the canned JSON through
    the same expression.
    """
    body = textwrap.dedent("""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> "$STUB_DATA/gh.log"
        jq_expr=""
        prev=""
        for arg in "$@"; do
          case "$prev" in
            --jq) jq_expr="$arg" ;;
          esac
          prev="$arg"
        done
        case "$1 $2" in
          "pr list")
            payload="$STUB_DATA/pr_list.json"
            ;;
          "pr view")
            payload="$STUB_DATA/pr_view.json"
            ;;
          "repo view")
            payload="$STUB_DATA/repo_view.json"
            ;;
          *)
            echo "stub gh: unhandled: $*" >&2
            exit 1
            ;;
        esac
        if [ -n "$jq_expr" ]; then
          cat "$payload" | jq -r "$jq_expr"
        else
          cat "$payload"
        fi
        """)
    gh = bin_dir / "gh"
    gh.write_text(body)
    gh.chmod(0o755)
    (data_dir / "pr_list.json").write_text(json.dumps(PR_LIST_JSON))
    (data_dir / "repo_view.json").write_text(json.dumps({
        "defaultBranchRef": {"name": "main"},
    }))


def write_stub_pr_view(data_dir: Path, number: int) -> None:
    """The `pr view` answer for one pull request."""
    pr = next(p for p in PR_LIST_JSON if p["number"] == number)
    (data_dir / "pr_view.json").write_text(json.dumps({
        "number": pr["number"],
        "headRefName": pr["headRefName"],
        "baseRefName": pr["baseRefName"],
        "title": pr["title"],
    }))


def write_stub_reviewer(repo_root: Path) -> Path:
    """A stub reviewer that writes the canned findings.

    The real reviewer is a model, and what is under test is the
    attribution of what it wrote. The stub takes the same arguments,
    copies the canned findings to where the caller will look, and prints
    that path on stdout.
    """
    bin_dir = repo_root / "stub-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail
        # The review script's contract: write the findings, print the path
        # on stdout. The findings go where the caller will look for them.
        # The arguments are recorded, so a test can read back what the
        # review was actually asked to look at.
        printf '%s\\n' "$*" >> "$STUB_DATA/reviewer.log"
        cp "$STUB_DATA/findings.md" "$STUB_DATA/findings.written.md"
        echo "$STUB_DATA/findings.written.md"
        """)
    reviewer = bin_dir / "hermes-scrutinize.sh"
    reviewer.write_text(body)
    reviewer.chmod(0o755)
    return reviewer


def build_tool(tmp: Path, reviewer: Path) -> Path:
    """A copy of the tools laid out the way the repository is.

    The script resolves its own sibling tools and its `lib/` from its own
    directory, so the copy that runs lives in a directory with `bin/`
    beside `lib/`.
    """
    tool_root = tmp / "tool"
    (tool_root / "bin").mkdir(parents=True)
    shutil.copytree(LIB, tool_root / "lib")
    shutil.copy(TOOL, tool_root / "bin" / "hermes-review-pr")
    (tool_root / "bin" / "hermes-review-pr").chmod(0o755)
    for name in ("stack-attrib", "stack-plan"):
        shutil.copy(REPO_ROOT / "bin" / name, tool_root / "bin" / name)
    scripts_dir = tool_root / "skills" / "hermes-review" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy(reviewer, scripts_dir / "hermes-scrutinize.sh")
    (scripts_dir / "hermes-scrutinize.sh").chmod(0o755)
    return tool_root


def run_stack_review(tmp: Path, worktree: Path, tool_root: Path,
                     data_dir: Path, pr: int,
                     findings: str) -> tuple[int, str, str, Path | None]:
    """Run the stack review and return (exit code, stdout, stderr, attribution).

    The attribution path is read from the tool's own output, so the test
    reads the answer the operator would read.
    """
    write_stub_pr_view(data_dir, pr)
    (data_dir / "findings.md").write_text(findings)
    env = {
        **os.environ,
        "PATH": f"{data_dir.parent / 'bin'}:{os.environ['PATH']}",
        "STUB_DATA": str(data_dir),
        "REVIEW_WT_ROOT": str(tmp / "review-wt"),
        "REVIEW_PRIORS": str(tmp / "priors.txt"),
        "REVIEW_PROFILE": "tester",
        "HERMES_PYTHON": "/bin/true",
        # The operator's own settings must not leak into the run.
        "LANE_REVIEW_PROFILE": "",
        "PIPELINE_PROFILE": "",
        "HERMES_IMPLEMENT_PROFILE": "",
        "REVIEW_SLOTS_TESTER": "1",
    }
    result = subprocess.run(
        [str(tool_root / "bin" / "hermes-review-pr"), "--pr", str(pr),
         "--stack"],
        capture_output=True, text=True, check=False, env=env,
        cwd=str(worktree),
    )
    attrib = None
    for line in result.stdout.splitlines():
        if line.startswith("attribution: "):
            attrib = Path(line.split("attribution: ", 1)[1].strip())
    return result.returncode, result.stdout, result.stderr, attrib


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        worktree = build_stack(tmp)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        data_dir = tmp / "data"
        data_dir.mkdir()
        write_stub_gh(bin_dir, data_dir)
        reviewer = write_stub_reviewer(worktree)
        write(Path(tmp / "priors.txt"), "Review the change.\n")
        (tmp / "review-wt").mkdir()
        tool_root = build_tool(tmp, reviewer)

        # ---- The two-pull-request stack the issue names. ----------------
        findings = (
            "Correctness\n"
            "----------\n"
            "\n"
            "1. The route discards the cause it is given.\n"
            "\n"
            "Paths:\n"
            "  route.py:4 (the return that drops it)\n"
            "\n"
            "2. A line nothing in the stack changed.\n"
            "\n"
            "Paths:\n"
            "  route.py:1 (the constant)\n"
        )
        code, out, err, attrib = run_stack_review(
            tmp, worktree, tool_root, data_dir, 2, findings,
        )
        if code != 0:
            failures.append(
                f"the stack review failed: exit {code}\n"
                f"stdout: {out}\nstderr: {err}"
            )
        else:
            gh_log = (data_dir / "gh.log").read_text()
            check("the stack is read from GitHub", "pr list" in gh_log,
                  gh_log)
            # The attribution reads the plan, not a fresh listing: a second
            # discovery would attribute over whatever the stack changed into
            # while the review ran.
            check("the stack is discovered once, not again for attribution",
                  gh_log.count("pr list") == 1, gh_log)
            # The review is of the whole stack: the top of it, against the
            # default branch. A review against the stack's bottom would see
            # only the pull request above it, which is the review this
            # mode exists to replace.
            reviewer_log = (data_dir / "reviewer.log").read_text()
            check("the review is against the default branch",
                  "--base origin/main" in reviewer_log, reviewer_log)
            check("the attribution file is written", attrib is not None, out)
            if attrib is None:
                pass
            elif not attrib.is_file():
                failures.append(
                    f"the attribution file is missing: {attrib}"
                )
            else:
                attrib_doc = json.loads(attrib.read_text())
                check("the stack holds both pull requests",
                      [s["number"] for s in attrib_doc["stack"]] == [1, 2],
                      json.dumps(attrib_doc["stack"]))
                # The finding on the dropped value belongs to the lower
                # pull request. This is the acceptance criterion.
                dropped = [f for f in attrib_doc["findings"]
                           if f["path"] == "route.py" and f["line"] == 4]
                check("the dropped-value finding is found",
                      len(dropped) == 1, json.dumps(attrib_doc["findings"]))
                if dropped:
                    check("the dropped-value finding is attributed to the "
                          "lower pull request",
                          dropped[0]["pr"] == 1 and
                          dropped[0]["reason"] == "introduced",
                          json.dumps(dropped[0]))
                # The finding on the constant: no pull request of the stack
                # changed it, so it belongs to the stack as a whole.
                untouched = [f for f in attrib_doc["findings"]
                             if f["path"] == "route.py" and f["line"] == 1]
                check("the untouched-line finding is found",
                      len(untouched) == 1, json.dumps(attrib_doc["findings"]))
                if untouched:
                    check("the untouched-line finding is reported against "
                          "the stack",
                          untouched[0]["pr"] is None and
                          untouched[0]["reason"] == "stack",
                          json.dumps(untouched[0]))

        # ---- The taller stack: the top edits the bottom's line. ---------
        taller = (
            "Correctness\n"
            "----------\n"
            "\n"
            "1. The route discards the cause it is given.\n"
            "\n"
            "Paths:\n"
            "  route.py:4 (the return that drops it)\n"
        )
        code, out, err, attrib = run_stack_review(
            tmp, worktree, tool_root, data_dir, 12, taller,
        )
        if code != 0:
            failures.append(
                f"the taller stack review failed: exit {code}\n"
                f"stdout: {out}\nstderr: {err}"
            )
        elif attrib is None:
            failures.append("the taller stack review wrote no attribution")
        else:
            attrib_doc = json.loads(attrib.read_text())
            check("the taller stack holds all three pull requests",
                  [s["number"] for s in attrib_doc["stack"]] == [10, 11, 12],
                  json.dumps(attrib_doc["stack"]))
            dropped = [f for f in attrib_doc["findings"]
                       if f["path"] == "route.py" and f["line"] == 4]
            check("the taller stack's dropped-value finding is found",
                  len(dropped) == 1, json.dumps(attrib_doc["findings"]))
            if dropped:
                # The top pull request edited the line, but the bottom one
                # introduced it, and the fix goes in the bottom one.
                check("the edited line stays with the pull request that "
                      "introduced it",
                      dropped[0]["pr"] == 10 and
                      dropped[0]["reason"] == "introduced",
                      json.dumps(dropped[0]))

        # ---- Naming a pull request in the middle of the stack. ---------
        #
        # The review is still of the top, and the attribution is still
        # across the whole stack.
        code, out, err, attrib = run_stack_review(
            tmp, worktree, tool_root, data_dir, 11, taller,
        )
        if code != 0:
            failures.append(
                f"the review from the middle of the stack failed: exit "
                f"{code}\nstdout: {out}\nstderr: {err}"
            )
        elif attrib is None:
            failures.append("the review from the middle wrote no attribution")
        else:
            attrib_doc = json.loads(attrib.read_text())
            check("naming the middle of the stack still finds the whole "
                  "stack",
                  [s["number"] for s in attrib_doc["stack"]] == [10, 11, 12],
                  json.dumps(attrib_doc["stack"]))
            dropped = [f for f in attrib_doc["findings"]
                       if f["path"] == "route.py" and f["line"] == 4]
            if dropped:
                check("the finding from the middle is still attributed to "
                      "the pull request that introduced the line",
                      dropped[0]["pr"] == 10, json.dumps(dropped[0]))

        # ---- A pending deletion stops at a context line. ----------------
        #
        # main: l1 l2 l3 l4 l5 l6
        # a:    l1 A2 A3 l4 l5 l6      # PR 1 edits lines 2-3
        # b:    l1 B2 l4 NEW l5 l6     # PR 2 rewrites line 2, deletes A3,
        #                               # inserts NEW after the l4 context
        #
        # Line 4 of `b` is NEW, which only PR 2 wrote, and belongs to PR 2.
        # Line 2 is B2 in the slot PR 1's A2 occupied, and stays with PR 1.
        with tempfile.TemporaryDirectory() as td2:
            drift = Path(td2) / "drift"
            drift.mkdir()
            git(drift, "init", "-q", "-b", "main")
            git(drift, "config", "user.email", "test@example.com")
            git(drift, "config", "user.name", "Test")
            write(drift / "f", "l1\nl2\nl3\nl4\nl5\nl6\n")
            git(drift, "add", ".")
            git(drift, "commit", "-q", "-m", "base")
            git(drift, "checkout", "-q", "-b", "a")
            write(drift / "f", "l1\nA2\nA3\nl4\nl5\nl6\n")
            git(drift, "add", ".")
            git(drift, "commit", "-q", "-m", "a")
            git(drift, "checkout", "-q", "-b", "b")
            write(drift / "f", "l1\nB2\nl4\nNEW\nl5\nl6\n")
            git(drift, "add", ".")
            git(drift, "commit", "-q", "-m", "b")

            lower = stack_attrib.StackMember(number=1, head_ref="a",
                                             base_ref="main")
            upper = stack_attrib.StackMember(number=2, head_ref="b",
                                             base_ref="a")
            owners = stack_attrib.introduced_by(str(drift), [lower, upper],
                                                "b")
            check("the rewritten slot stays with the lower pull request",
                  owners.get("f", {}).get(2) == 1, json.dumps(owners))
            check("an insertion after a context line belongs to the pull "
                  "request that added it",
                  owners.get("f", {}).get(4) == 2, json.dumps(owners))

        # ---- A stack that forks is refused. ------------------------------
        #
        # PR 2 and PR 3 are both based on PR 1's branch: there is no top, so
        # the refusal must name the siblings rather than pick one.
        forked = [
            {"number": 1, "headRefName": "feat/lower",
             "baseRefName": "main", "title": "lower"},
            {"number": 2, "headRefName": "feat/side-b",
             "baseRefName": "feat/lower", "title": "sibling one"},
            {"number": 3, "headRefName": "feat/side-c",
             "baseRefName": "feat/lower", "title": "sibling two"},
        ]
        (data_dir / "pr_list.json").write_text(json.dumps(forked))
        code, out, err, _ = run_stack_review(
            tmp, worktree, tool_root, data_dir, 1, "a finding\n",
        )
        refusal = ("the stack forks" in err and "#2" in err and "#3" in err)
        check("a forked stack is refused, naming the sibling pull requests",
              code != 0 and refusal,
              f"exit {code}\nstdout: {out}\nstderr: {err}")
        # Back to the un-forked shape for anything after this section.
        (data_dir / "pr_list.json").write_text(json.dumps(PR_LIST_JSON))

    if failures:
        print("FAIL stack review")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS stack review: the stack is found; a defect two pull requests "
          "create is attributed to the lower one; an edited line stays with "
          "the pull request that introduced it; a line no pull request "
          "changed belongs to the stack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
