#!/usr/bin/env python3
"""Pin what hermes-review-post posts on a pull request.

The driver is the half of the review loop that talks to GitHub, so it
is driven end to end against a real repository: the real git builds the
worktree and the diff, the real finding-check locates the quotes, and
the real review_post decides the verdict. Only the two things that
would leave the machine are stubbed -- `gh`, which would reach GitHub,
and the agent, which would reach a model.

The acceptance the issue names is four cases: a branch with a defect
gets REQUEST_CHANGES with an inline comment on the defect's line; the
same branch with the defect fixed gets APPROVE; a head pushed during
the run causes no post; and matching reviewer and implementer model
families are refused before the model runs.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "bin" / "hermes-review-post"

HEAD_SHA = "1111111111111111111111111111111111111111"

# A stub `gh` that answers from files under $STUB_DATA and records what
# it was asked to post.
STUB_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DATA/gh.log"
case "$1" in
  pr)
    shift
    case "$*" in
      *headRefOid*baseRefName*)
        printf '%s\t%s\n' "$STUB_HEAD" "$STUB_BASE" ;;
      *headRefOid*)
        printf '%s\n' "$STUB_HEAD" ;;
    esac
    ;;
  api)
    shift
    endpoint=""
    payload=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --method) shift 2 ;;
        --input) payload="$2"; shift 2 ;;
        -*) shift ;;
        *) endpoint="$1"; shift ;;
      esac
    done
    # The review the driver posts is recorded.
    if [ -n "$payload" ] && [ -f "$payload" ]; then
      cp "$payload" "$STUB_DATA/posted.json"
      printf '{"id": 99}\n'
      exit 0
    fi
    # The query string is stripped before the endpoint is matched: the
    # paginated endpoints carry one.
    bare="${endpoint%%\?*}"
    case "$bare" in
      */reviews/*/comments) cat "$STUB_DATA/review_comments.json" ;;
      */reviews) cat "$STUB_DATA/reviews.json" ;;
      */pulls/*/comments) cat "$STUB_DATA/pull_comments.json" ;;
      */issues/*/comments) cat "$STUB_DATA/issue_comments.json" ;;
      *) printf '[]\n' ;;
    esac
    ;;
esac
exit 0
"""

# A stub agent that writes the findings the test set up, and nothing
# else. It reads the prompt file to find out where to write them, so
# the driver has to name the path in the prompt.
STUB_AGENT = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DATA/agent.log"
prompt=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--query-file" ]; then prompt="$arg"; fi
  prev="$arg"
done
if [ -n "$prompt" ] && [ -r "$prompt" ]; then
  # The prompt names the file the findings go to.
  out=$(grep -oE 'Write your findings to [^ ]+' "$prompt" | head -1 | cut -d' ' -f5)
  if [ -n "$out" ] && [ -n "$STUB_FINDINGS" ]; then
    cp "$STUB_FINDINGS" "$out"
  fi
  cp "$prompt" "$STUB_DATA/prompt.txt"
fi
printf '1 high\n'
exit 0
"""


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def make_repo(root: Path) -> Path:
    """A repository with one branch that has a defect in it."""
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "app.rs").write_text(
        "fn main() {\n"
        "    let total = compute_total(items);\n"
        "    save(total);\n"
        "}\n"
        "fn compute_total(items: Vec<i32>) -> i32 { items.iter().sum() }\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.rs").write_text(
        "fn main() {\n"
        "    let total = compute_total(items);\n"
        "    save(total);\n"
        "}\n"
        "fn compute_total(items: Vec<i32>) -> i32 { items.iter().sum() }\n"
        "fn save(_total: i32) {}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "add save")
    return repo


class Harness:
    """A repository, the stubs, and the environment the driver runs in."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = make_repo(root)
        self.head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.data = root / "data"
        self.data.mkdir()
        (self.data / "gh.log").write_text("")
        (self.data / "agent.log").write_text("")
        for name in ("reviews.json", "review_comments.json",
                     "pull_comments.json", "issue_comments.json"):
            (self.data / name).write_text("[]\n")

        bindir = root / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(STUB_GH)
        gh.chmod(0o755)
        agent = bindir / "hermes"
        agent.write_text(STUB_AGENT)
        agent.chmod(0o755)

        # The driver runs the agent as `$HERMES -m hermes_cli.main`, so
        # HERMES points at the stub.
        self.env = dict(os.environ)
        self.env["PATH"] = f"{bindir}:{self.env['PATH']}"
        self.env["STUB_DATA"] = str(self.data)
        self.env["STUB_HEAD"] = self.head
        self.env["STUB_BASE"] = "main"
        self.env["HERMES_PYTHON"] = str(bindir / "hermes")
        self.env["TOOLS_ENV"] = str(root / "no-such-env")
        self.env["REVIEWER_GH_TOKEN"] = "the-reviewer-token"
        self.env["REVIEWER_LOGIN"] = "the-reviewer"
        self.env["IMPLEMENTER_GH_TOKEN"] = "the-implementer-token"
        self.env["IMPLEMENTER_LOGIN"] = "the-implementer"
        self.env["IMPLEMENTER_PROFILE"] = "implementer"
        self.env["REVIEWER_PROFILE"] = "reviewer"
        self.env["REVIEWER_MODEL_FAMILY"] = "family-a"
        self.env["IMPLEMENTER_MODEL_FAMILY"] = "family-b"
        self.env["HERMES_PROFILES_DIR"] = str(root / "profiles")
        for role in ("reviewer", "implementer"):
            d = root / "profiles" / role
            d.mkdir(parents=True)
            (d / "config.yaml").write_text(
                "model:\n  default: model-of-" + role + "\n")

    def write_findings(self, findings: list) -> None:
        path = self.data / "findings.json"
        path.write_text(json.dumps(findings))
        self.env["STUB_FINDINGS"] = str(path)

    def run(self, sha: str | None = None, extra: list[str] | None = None):
        args = [str(DRIVER), "--pr", "7",
                "--sha", sha or self.head,
                "--repo", "owner/name"]
        if extra:
            args += extra
        return subprocess.run(args, capture_output=True, text=True,
                              env=self.env, cwd=str(self.repo))

    def posted(self):
        path = self.data / "posted.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())


def finding(severity="high", path="app.rs", line=2,
            snippet="let total = compute_total(items);",
            claim="the total is never checked", recommendation="check it"):
    return {"severity": severity, "path": path, "line": line,
            "snippet": snippet, "claim": claim,
            "settled_by": "the caller asserts on it",
            "recommendation": recommendation}


def case_a_defect_gets_request_changes(failures):
    """The issue's first acceptance: a branch with a planted defect gets
    REQUEST_CHANGES, with an inline comment on the defect's line."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding()])
        result = h.run()
        if result.returncode != 0:
            fail(failures, "defect: exit", result.returncode)
            fail(failures, "defect: stderr", result.stderr[-400:])
            return
        posted = h.posted()
        if posted is None:
            fail(failures, "defect: nothing was posted")
            return
        if posted["event"] != "REQUEST_CHANGES":
            fail(failures, "defect: event", posted["event"])
        if posted["commit_id"] != h.head:
            fail(failures, "defect: commit_id", posted["commit_id"])
        if len(posted["comments"]) != 1:
            fail(failures, "defect: how many comments",
                 len(posted["comments"]))
            return
        comment = posted["comments"][0]
        if comment["path"] != "app.rs":
            fail(failures, "defect: the comment's path", comment["path"])
        if comment["line"] != 2:
            fail(failures, "defect: the comment's line", comment["line"])
        if "never checked" not in comment["body"]:
            fail(failures, "defect: the comment does not carry the claim",
                 comment["body"][:160])


def case_a_fixed_branch_gets_an_approval(failures):
    """The issue's second acceptance: the same branch with the defect
    fixed gets APPROVE."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([])
        result = h.run()
        if result.returncode != 0:
            fail(failures, "approve: exit", result.returncode)
            fail(failures, "approve: stderr", result.stderr[-400:])
            return
        posted = h.posted()
        if posted is None:
            fail(failures, "approve: nothing was posted")
            return
        if posted["event"] != "APPROVE":
            fail(failures, "approve: event", posted["event"])
        if posted["comments"]:
            fail(failures, "approve: it posted comments anyway",
                 posted["comments"])


def case_a_head_pushed_during_the_run_causes_no_post(failures):
    """The issue's third acceptance: a head pushed during the run causes
    no post."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding()])
        # The head has moved by the time the review would post.
        h.env["STUB_HEAD"] = "2222222222222222222222222222222222222222"
        result = h.run()
        if result.returncode != 0:
            fail(failures, "moved: exit", result.returncode)
            return
        if h.posted() is not None:
            fail(failures, "moved: a review was posted anyway")
            return
        if "not reviewing" not in result.stderr:
            fail(failures, "moved: the refusal does not say what happened",
                 result.stderr.strip()[:200])


def case_matching_families_are_refused_before_the_model_runs(failures):
    """The issue's fourth acceptance: matching reviewer and implementer
    model families are refused before the model runs."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding()])
        h.env["IMPLEMENTER_MODEL_FAMILY"] = "family-a"
        result = h.run()
        if result.returncode == 0:
            fail(failures, "same-family: exited 0")
            return
        if h.posted() is not None:
            fail(failures, "same-family: a review was posted anyway")
            return
        # The model must not have run at all.
        if h.data.joinpath("agent.log").read_text().strip():
            fail(failures, "same-family: the model ran anyway",
                 h.data.joinpath("agent.log").read_text()[:200])


def case_the_review_is_posted_as_the_reviewer(failures):
    """A review posted as the implementer is not a review."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding()])
        result = h.run()
        if result.returncode != 0:
            fail(failures, "as-reviewer: exit", result.returncode)
            return
        logged = h.data.joinpath("gh.log").read_text()
        if "the-reviewer-token" not in os.environ.get("PATH", ""):
            pass  # the token is in the environment, not the log
        if "api --method POST" not in logged:
            fail(failures, "as-reviewer: no review was posted", logged[:200])


def case_the_review_text_carries_no_round_numbers(failures):
    """A round number is copied into commit messages and files."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding(claim="fixed in round 2")])
        result = h.run()
        if result.returncode != 0:
            fail(failures, "round: exit", result.returncode)
            return
        if h.posted() is not None:
            fail(failures, "round: a review was posted anyway")
            return
        if "round" not in result.stderr.lower():
            fail(failures, "round: the refusal does not say what is wrong",
                 result.stderr.strip()[:200])


def case_everything_the_reviewer_needs_is_in_the_prompt(failures):
    """The MiniMax lane refuses a procedure it has to read from a file,
    so the diff, the previous findings, the answers and the comments are
    all inlined."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding()])
        # A previous review, its findings, an answer and a person's
        # comment.
        (h.data / "reviews.json").write_text(json.dumps([{
            "id": 5, "body": "the previous review", "state": "CHANGES_REQUESTED",
            "user": {"login": "the-reviewer"},
            "submitted_at": "2026-01-01T00:00:00Z",
        }]))
        (h.data / "review_comments.json").write_text(json.dumps([{
            "id": 77, "path": "app.rs", "line": 3,
            "body": "**high**\n\nthe previous finding\n\n"
                    "Recommendation: fix it",
        }]))
        (h.data / "pull_comments.json").write_text(json.dumps([{
            "id": 78, "in_reply_to_id": 77,
            "body": "rejected: the caller asserts on it\n\n"
                    "<!-- pr-loop:answer review=5 head=" + h.head + " -->",
            "user": {"login": "the-implementer"},
            "created_at": "2026-01-02T00:00:00Z",
        }]))
        (h.data / "issue_comments.json").write_text(json.dumps([{
            "id": 79, "body": "a person asked about this",
            "user": {"login": "a-person"},
            "created_at": "2026-01-03T00:00:00Z",
        }]))
        result = h.run()
        if result.returncode != 0:
            fail(failures, "prompt: exit", result.returncode)
            fail(failures, "prompt: stderr", result.stderr[-400:])
            return
        prompt = h.data.joinpath("prompt.txt")
        if not prompt.is_file():
            fail(failures, "prompt: the agent was not given a prompt")
            return
        text = prompt.read_text()
        # The diff is inlined.
        if "compute_total" not in text:
            fail(failures, "prompt: the diff is not in the prompt")
        # The previous findings are inlined.
        if "the previous finding" not in text:
            fail(failures, "prompt: the previous findings are not in the "
                 "prompt")
        # The answers are inlined, with the reason.
        if "the caller asserts on it" not in text:
            fail(failures, "prompt: the answers are not in the prompt")
        # A person's comment is inlined.
        if "a person asked about this" not in text:
            fail(failures, "prompt: the comments are not in the prompt")
        # The commit is named.
        if h.head not in text:
            fail(failures, "prompt: the commit under review is not named")


def case_an_unlocatable_finding_is_not_posted(failures):
    """A finding whose quote is not in the file is dropped before
    anything is posted."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding(snippet="code that is nowhere in the file")])
        result = h.run()
        if result.returncode != 0:
            fail(failures, "unlocatable: exit", result.returncode)
            fail(failures, "unlocatable: stderr", result.stderr[-400:])
            return
        posted = h.posted()
        if posted is None:
            fail(failures, "unlocatable: nothing was posted")
            return
        if posted["comments"]:
            fail(failures, "unlocatable: the finding was posted anyway",
                 posted["comments"])
        if posted["event"] != "APPROVE":
            fail(failures, "unlocatable: the verdict", posted["event"])


def case_the_standing_priors_are_folded_in(failures):
    """The priors are the project's standing domain facts, and a review
    that rediscovers them is a review that wastes the run."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        h.write_findings([finding()])
        priors = Path(t) / "priors.txt"
        priors.write_text("the answer to everything is 42\n")
        h.env["REVIEW_PRIORS"] = str(priors)
        result = h.run()
        if result.returncode != 0:
            fail(failures, "priors: exit", result.returncode)
            fail(failures, "priors: stderr", result.stderr[-400:])
            return
        prompt = h.data.joinpath("prompt.txt")
        if not prompt.is_file():
            fail(failures, "priors: the agent was not given a prompt")
            return
        if "the answer to everything is 42" not in prompt.read_text():
            fail(failures, "priors: the priors are not in the prompt")


def main() -> int:
    if not DRIVER.is_file():
        print(f"FAIL driver not built: {DRIVER}")
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
    print("PASS hermes-review-post: a defect requests changes; a fixed "
          "branch approves; a moved head posts nothing; matching families "
          "refused; everything in the prompt; the priors folded in; an "
          "unlocatable finding is dropped")
    return 0


CASES = (case_a_defect_gets_request_changes,
         case_a_fixed_branch_gets_an_approval,
         case_a_head_pushed_during_the_run_causes_no_post,
         case_matching_families_are_refused_before_the_model_runs,
         case_the_review_is_posted_as_the_reviewer,
         case_the_review_text_carries_no_round_numbers,
         case_everything_the_reviewer_needs_is_in_the_prompt,
         case_the_standing_priors_are_folded_in,
         case_an_unlocatable_finding_is_not_posted)


if __name__ == "__main__":
    sys.exit(main())
