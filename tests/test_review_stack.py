#!/usr/bin/env python3
"""Pin the stack review against the cases the issues name.

The defect a stacked PR hides is one that only exists when two pull
requests combine: the lower pull request's code drops a value the upper
pull request produces. Neither pull request's own diff shows the loss. A
review of the top of the stack against the default branch does see it --
and then has to say which pull request of the stack it belongs to.

So the suite pins:

1. The stack is found from the pull request's own base, and the review
   runs against the default branch rather than the stack's bottom.
2. A drifted citation is charged by the quoted code, at its located
   line, to the pull request that wrote it.
3. The cited line loses to the quote: a citation that lands on another
   pull request's line still goes to the pull request that wrote the
   quoted code.
4. A line the upper pull request edited belongs to the upper pull
   request, whose commit last wrote the quoted text.
5. A quote that cannot be located is `unlocated` and charged to nobody.
6. A quote written before the stack began belongs to the stack as a
   whole.
7. A malformed entry comes back annotated in its place; the good
   findings beside it are still attributed.
8. A stack that forks is refused, naming the siblings.
9. The stack is discovered once, from the plan the review was set up
   from.

The repository is real: real git, real branches, a real bare origin, the
real finding-check locating the quotes, and real git blaming the located
lines. The reviewer is stubbed -- a model is not what is under test --
and writes both files the driver asks for. `gh` is stubbed at the network
boundary, answering from canned JSON on disk.
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


# The stack: `main` holds 40 base lines. PR 101 writes the quoted line a
# drifted citation is charged by. PR 102 inserts a line above it -- so
# the quote sits at line 11 of the reviewed tree and line 10 of PR 101's
# own -- and edits another line PR 101 had written, so the
# edited-line case has both a lower introducer and an upper last writer.
BASE_LINES = [f"base line {n}" for n in range(1, 41)]
LOWER_QUOTE = "let total = compute_total(items);"
UPPER_INSERT = "let started = Instant::now();"
LOWER_EDITED = "let value = introduced_by_the_lower_pr();"
UPPER_EDITED = "let value = edited_by_the_upper_pr();"


def base_lines() -> list[str]:
    return list(BASE_LINES)


def build_stack(tmp: Path) -> Path:
    """A repository with a bare origin and the branch the stack walks.

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

    def commit_all(message: str) -> None:
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", message)

    write(repo / "app.rs", "\n".join(base_lines()) + "\n")
    commit_all("base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")

    lines = base_lines()
    lines[9] = LOWER_QUOTE
    lines[19] = LOWER_EDITED
    git(repo, "checkout", "-q", "-b", "feat/a")
    write(repo / "app.rs", "\n".join(lines) + "\n")
    commit_all("pr 101")
    git(repo, "push", "-q", "origin", "feat/a")

    lines.insert(2, UPPER_INSERT)
    lines[20] = UPPER_EDITED
    git(repo, "checkout", "-q", "-b", "feat/b")
    write(repo / "app.rs", "\n".join(lines) + "\n")
    commit_all("pr 102")
    git(repo, "push", "-q", "origin", "feat/b")

    git(repo, "checkout", "-q", "main")
    return repo


PR_LIST_JSON = [
    {"number": 101, "headRefName": "feat/a", "baseRefName": "main",
     "title": "pr 101"},
    {"number": 102, "headRefName": "feat/b", "baseRefName": "feat/a",
     "title": "pr 102"},
    {"number": 3, "headRefName": "feat/other", "baseRefName": "main",
     "title": "not part of the stack"},
]

FORKED_PR_LIST_JSON = [
    {"number": 1, "headRefName": "feat/a", "baseRefName": "main",
     "title": "lower"},
    {"number": 2, "headRefName": "feat/side-b", "baseRefName": "feat/a",
     "title": "sibling one"},
    {"number": 3, "headRefName": "feat/side-c", "baseRefName": "feat/a",
     "title": "sibling two"},
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
    (data_dir / "repo_view.json").write_text(json.dumps({
        "defaultBranchRef": {"name": "main"},
    }))


def write_stub_pr_view(data_dir: Path, entry: dict) -> None:
    """The `pr view` answer for one pull request."""
    (data_dir / "pr_view.json").write_text(json.dumps(entry))


def write_stub_reviewer(repo_root: Path) -> Path:
    """A stub reviewer that writes the canned findings, markdown and JSON.

    The real reviewer is a model; what is under test is what happens to
    its findings. The stub takes the same arguments -- including --json,
    whose target it fills with the canned findings JSON -- copies the
    canned markdown to a path of its own, and prints that path on stdout.
    """
    bin_dir = repo_root / "stub-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> "$STUB_DATA/reviewer.log"
        json_out=""
        prev=""
        for arg in "$@"; do
          case "$prev" in
            --json) json_out="$arg" ;;
          esac
          prev="$arg"
        done
        if [ -n "$json_out" ]; then
          cp "$STUB_DATA/findings.json" "$json_out"
        fi
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
    for name in ("stack-attrib", "stack-plan", "finding-check"):
        shutil.copy(REPO_ROOT / "bin" / name, tool_root / "bin" / name)
    # The driver refuses to start when the execution lane is not
    # executable beside it, so the copy has to carry it.
    execute = REPO_ROOT / "bin" / "review-execute"
    if execute.exists():
        shutil.copy(execute, tool_root / "bin" / "review-execute")
        (tool_root / "bin" / "review-execute").chmod(0o755)
    scripts_dir = tool_root / "skills" / "hermes-review" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy(reviewer, scripts_dir / "hermes-scrutinize.sh")
    (scripts_dir / "hermes-scrutinize.sh").chmod(0o755)
    return tool_root


def finding(snippet: str, path: str = "app.rs", line: int = 1,
            severity: str = "low") -> dict:
    return {"severity": severity, "path": path, "line": line,
            "snippet": snippet, "claim": "the claim",
            "settled_by": "the settler", "recommendation": "the fix"}


def run_stack_review(tmp: Path, worktree: Path, tool_root: Path,
                     data_dir: Path, pr: dict,
                     findings: list) -> tuple[int, str, str, Path | None]:
    """Run the stack review and return (exit code, stdout, stderr, attribution).

    The attribution path is read from the tool's own output, so the test
    reads the answer the operator would read.
    """
    write_stub_pr_view(data_dir, pr)
    (data_dir / "findings.json").write_text(json.dumps(findings))
    (data_dir / "findings.md").write_text("markdown findings\n")
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
        [str(tool_root / "bin" / "hermes-review-pr"), "--pr", str(pr["number"]),
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
        (data_dir / "pr_list.json").write_text(json.dumps(PR_LIST_JSON))
        reviewer = write_stub_reviewer(worktree)
        write(Path(tmp / "priors.txt"), "Review the change.\n")
        (tmp / "review-wt").mkdir()
        tool_root = build_tool(tmp, reviewer)

        # ---- The whole attribution, in one stack review. -----------------
        pr_102 = next(p for p in PR_LIST_JSON if p["number"] == 102)
        findings = [
            # The issue's case: the citation has drifted far from the
            # quote, and lands on a line the stack never wrote.
            finding("let total = compute_total(items);", line=30),
            # The cited line is one the upper PR wrote; the quote is the
            # lower PR's. The quote wins.
            finding("let total = compute_total(items);", line=3),
            # The upper PR edited the line the lower one introduced.
            finding("let value = edited_by_the_upper_pr();", line=21),
            # A quote nowhere in the file.
            finding("code that is nowhere at all", line=10),
            # A quote written before the stack began.
            finding("base line 1", line=1),
            # Malformed entries stay in their place; they do not abort
            # the batch around them.
            "not an object at all",
            {"severity": "low"},
        ]
        code, out, err, attrib = run_stack_review(
            tmp, worktree, tool_root, data_dir, pr_102, findings,
        )
        if code != 0:
            failures.append(
                f"the stack review failed: exit {code}\n"
                f"stdout: {out}\nstderr: {err}"
            )
        else:
            gh_log = (data_dir / "gh.log").read_text()
            check("the stack is discovered once, not again for attribution",
                  gh_log.count("pr list") == 1, gh_log)
            reviewer_log = (data_dir / "reviewer.log").read_text()
            check("the review is against the default branch",
                  "--base origin/main" in reviewer_log, reviewer_log)
            check("the reviewer was asked for the findings JSON",
                  "--json" in reviewer_log, reviewer_log)
            check("the attribution file is written", attrib is not None, out)
            if attrib is None:
                pass
            elif not attrib.is_file():
                failures.append(f"the attribution file is missing: {attrib}")
            else:
                doc = json.loads(attrib.read_text())
                check("the stack holds both pull requests",
                      [s["number"] for s in doc["stack"]] == [101, 102],
                      json.dumps(doc["stack"]))
                got = doc["findings"]
                check("every entry survived the batch", len(got) == 7,
                      json.dumps(got))
                if len(got) == 7:
                    drifted, cited, edited, nowhere, before, \
                        bad_object, bad_fields = got

                    def is_finding(entry: dict) -> bool:
                        return entry.get("pr") is not None

                    check(
                        "the drifted citation is charged to the pull "
                        "request that wrote the quoted code, at its "
                        "located line",
                        drifted["pr"] == 101
                        and drifted["reason"] == "attributed"
                        and drifted["line"] == 11,
                        json.dumps(drifted),
                    )
                    check(
                        "the cited line loses to the quote: charged to "
                        "the lower pull request, not the upper one whose "
                        "line was cited",
                        cited["pr"] == 101 and cited["reason"] == "attributed",
                        json.dumps(cited),
                    )
                    check("the edited line goes to the pull request whose "
                          "commit last wrote the quoted text",
                          edited["pr"] == 102
                          and edited["reason"] == "attributed",
                          json.dumps(edited))
                    check("an unlocatable quote is charged to nobody",
                          nowhere["pr"] is None
                          and nowhere["reason"] == "unlocated"
                          and nowhere.get("detail"),
                          json.dumps(nowhere))
                    check("a quote written before the stack began belongs "
                          "to the stack as a whole",
                          before["pr"] is None
                          and before["reason"] == "stack"
                          and before.get("blamed_commit"),
                          json.dumps(before))
                    for index, malformed in ((5, bad_object),
                                             (6, bad_fields)):
                        check(f"the malformed entry at {index} comes back "
                              "as unlocated",
                              malformed.get("reason") == "unlocated"
                              and malformed.get("pr") is None,
                              json.dumps(malformed))
                    tally = doc["tally"]
                    check("the tally counts every finding once",
                          tally.get("attributed") == 3
                          and tally.get("unlocated") == 3
                          and tally.get("stack") == 1,
                          json.dumps(tally))

        # ---- Naming the lower pull request of the stack. -----------------
        #
        # Naming any PR of the stack reviews the top; the attribution
        # still runs across the whole stack.
        pr_101 = next(p for p in PR_LIST_JSON if p["number"] == 101)
        code, out, err, attrib = run_stack_review(
            tmp, worktree, tool_root, data_dir, pr_101,
            [finding("let value = edited_by_the_upper_pr();", line=21)],
        )
        if code != 0:
            failures.append(
                f"the review from the bottom of the stack failed: exit "
                f"{code}\nstdout: {out}\nstderr: {err}"
            )
        elif attrib is None:
            failures.append("the review from the bottom wrote no attribution")
        else:
            doc = json.loads(attrib.read_text())
            check("naming the bottom still finds the whole stack",
                  [s["number"] for s in doc["stack"]] == [101, 102],
                  json.dumps(doc["stack"]))
            check("the finding is still charged to the upper pull request",
                  doc["findings"][0]["pr"] == 102,
                  json.dumps(doc["findings"][0]))

        # ---- A stack that forks is refused. ------------------------------
        #
        # PR 2 and PR 3 are both based on PR 1's branch: there is no top,
        # so the refusal must name the siblings rather than pick one.
        (data_dir / "pr_list.json").write_text(
            json.dumps(FORKED_PR_LIST_JSON))
        pr_forked = next(p for p in FORKED_PR_LIST_JSON
                         if p["number"] == 1)
        code, out, err, _ = run_stack_review(
            tmp, worktree, tool_root, data_dir, pr_forked, [],
        )
        refusal = ("the stack forks" in err and "#2" in err and "#3" in err)
        check("a forked stack is refused, naming the sibling pull requests",
              code != 0 and refusal,
              f"exit {code}\nstdout: {out}\nstderr: {err}")
        (data_dir / "pr_list.json").write_text(json.dumps(PR_LIST_JSON))

    if failures:
        print("FAIL stack review")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS stack review: the stack is found once and reviewed against "
          "the default branch; a drifted citation is charged by its quote; "
          "an edited line goes to its last writer; an unlocatable quote is "
          "charged to nobody; a line older than the stack belongs to the "
          "stack; malformed entries stay in their place; a forked stack is "
          "refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
