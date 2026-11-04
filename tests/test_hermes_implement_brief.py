#!/usr/bin/env python3
"""Pin hermes-implement's --brief: a written brief reaches the lane whole.

The brief is a file, not text in the prompt, and it is copied into the run
directory.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_install_fixture import install as install_skills
BIN = REPO_ROOT / "bin" / "hermes-implement"

# A stub `gh` that answers the one call hermes-implement makes.
FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # `gh api user` answers who the call would act as, which `gh_as`
    # checks against IMPLEMENTER_LOGINS.
    if [[ "$1" == "api" && "$2" == "user" ]]; then
        echo "test-implementer"
        exit 0
    fi
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        printf 'title: brief regression\\nstate: OPEN\\n'
        exit 0
    fi
    exit 0
    """)

# A stub agent that records the prompt it was handed and the skills it
# was asked to load, and exits 0.
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env bash
    args=("$@")
    query=""
    skills=""
    for i in "${!args[@]}"; do
        case "${args[$i]}" in
            --query-file) query="${args[$((i + 1))]}" ;;
            --skills) skills="${args[$((i + 1))]}" ;;
        esac
    done
    {
        printf 'skills=%s\\n' "$skills"
        printf 'query_bytes=%s\\n' "$(wc -c <"$query" 2>/dev/null || echo 0)"
        cat "$query" 2>/dev/null
    } >> "$STUB_LOG"
    exit 0
    """)


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.txt").write_text("two\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")
    return repo


class Harness:
    """A repository, the stubs, and the environment the driver runs in."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = make_repo(root)
        bindir = root / "bin"
        bindir.mkdir()
        for name, body in (("gh", FAKE_GH), ("hermes", FAKE_HERMES)):
            path = bindir / name
            path.write_text(body)
            path.chmod(0o755)
        self.log = root / "agent.log"
        self.log.write_text("")
        self.env = dict(os.environ)
        self.env["PATH"] = f"{bindir}:{self.env['PATH']}"
        self.env["STUB_LOG"] = str(self.log)
        self.env["HERMES_PYTHON"] = str(bindir / "hermes")
        self.env["TOOLS_ENV"] = str(root / "no-such-env")
        self.env["AGENT_STATE_DIR"] = str(root / "state")
        self.env["HERMES_HOME"] = str(root / "hermes_home")
        # The login the implementer role may act as. `gh_as` resolves
        # the login the call would act as and refuses unless it is
        # one of these.
        self.env["IMPLEMENTER_LOGINS"] = "test-implementer"
        # The lane refuses to start when a dispatched skill differs from the
        # repository's copy. The install is the operator's own, so the suite
        # makes one for its temporary home rather than turning the check off.
        install_skills(root / "hermes_home")
        self.env["HERMES_IMPLEMENT_SETTLE"] = "0"
        self.env["HERMES_IMPLEMENT_TIMEOUT"] = "60"
        # The operator's settings must not leak in: a lane-pinned
        # identity in the parent env changes what the tool does.
        for name in list(self.env):
            if name.startswith(("HERMES_IMPLEMENT_", "PIPELINE_",
                                "LANE_REVIEW_", "REVIEW_")):
                if name not in ("HERMES_IMPLEMENT_SETTLE",
                                "HERMES_IMPLEMENT_TIMEOUT"):
                    self.env.pop(name)

    def write_brief(self, text: str) -> Path:
        path = self.root / "brief.md"
        path.write_text(text)
        return path

    def run(self, brief: Path | None = None, extra: list[str] | None = None):
        args = [str(BIN), "--issue", "1", "--worktree", str(self.repo),
                "--verify", "true"]
        if brief is not None:
            args += ["--brief", str(brief)]
        if extra:
            args += extra
        self.last_run = subprocess.run(
            args, capture_output=True, text=True, env=self.env,
            cwd=str(self.repo))
        return self.last_run

    def agent_log(self) -> str:
        return self.log.read_text()

    def run_dir(self) -> Path:
        """The run's private directory, from the driver's stdout."""
        return Path(self.last_run.stdout.strip().splitlines()[-1])

    def stale_brief_skill(self) -> Path:
        """Make the installed `brief` skill differ from the repository's
        copy, the way a profile that missed a merge carries it.

        The check reads the whole directory, so a word appended to the
        installed SKILL.md is enough to make it stale.
        """
        installed = (self.root / "hermes_home" / "skills" /
                     "software-development" / "brief" / "SKILL.md")
        if not installed.is_file():
            installed = next(self.root.glob(
                "hermes_home/skills/*/brief/SKILL.md"))
        installed.write_text(installed.read_text() +
                             "\nThe OLD rules, appended.\n")
        return installed


BRIEF = """\
# Goal

Make the parser accept a trailing comma.

# Context

- src/parser.rs -- the parser, and the only place the rule lives.
- tests/parser.rs -- the tests that cover it.

# Mechanism

Strip the trailing comma in the tokenizer, before the grammar sees it.

The tempting wrong one is to strip it in the grammar rule: that reads
better and it accepts a trailing comma in the middle of a list, which
the grammar cannot then reject.

# Constraints

The public API does not change.

# Tests

tests/parser.rs -- `parses_a_trailing_comma`. It fails before the change.

# Acceptance

- A list with a trailing comma parses.
- A list with a comma in the middle does not.

# Outputs

None.
"""


def case_the_brief_reaches_the_lane(failures):
    """The prompt names the brief's path, and the brief is in the run
    directory."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        brief = h.write_brief(BRIEF)
        result = h.run(brief)
        if result.returncode != 0:
            fail(failures, "brief: exit", result.returncode)
            fail(failures, "brief: stderr", result.stderr[-600:])
            return
        log = h.agent_log()
        if "brief.md" not in log:
            fail(failures, "brief: the prompt does not name the brief",
                 log[:300])
            return
        run_dir = h.run_dir()
        kept = run_dir / "brief.md"
        if not kept.is_file():
            fail(failures, "brief: the brief was not kept in the run "
                 f"directory", sorted(p.name for p in run_dir.iterdir()))
            return
        if kept.read_text() != BRIEF:
            fail(failures, "brief: the kept brief is not the one given")


def case_a_brief_over_the_argument_cap_reaches_the_lane(failures):
    """The acceptance case: a brief over 128 KiB reaches the lane whole.

    A brief pasted into the prompt, or passed as an argument, could not.
    """
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        # Well over MAX_ARG_STRLEN (128 KiB on Linux).
        big = BRIEF + ("\n" + "x" * 200 + "\n") * 800
        assert len(big) > 128 * 1024, len(big)
        brief = h.write_brief(big)
        result = h.run(brief)
        if result.returncode != 0:
            fail(failures, "big-brief: exit", result.returncode)
            fail(failures, "big-brief: stderr", result.stderr[-600:])
            return
        run_dir = h.run_dir()
        kept = run_dir / "brief.md"
        if not kept.is_file():
            fail(failures, "big-brief: the brief was not kept")
            return
        if kept.stat().st_size != len(big):
            fail(failures, "big-brief: the kept brief is not the whole "
                 "brief", f"{kept.stat().st_size} of {len(big)}")
            return
        # The prompt carries the brief's path, not the brief.
        log = h.agent_log()
        if "brief.md" not in log:
            fail(failures, "big-brief: the prompt does not name the brief")
        prompt_bytes = 0
        for line in log.splitlines():
            if line.startswith("query_bytes="):
                prompt_bytes = int(line.split("=", 1)[1])
        if prompt_bytes > 128 * 1024:
            fail(failures, "big-brief: the brief was pasted into the "
                 f"prompt", f"prompt is {prompt_bytes} bytes")


def case_the_brief_skill_is_composed_with_the_others(failures):
    """The brief skill is added to the skills the lane runs with, not
    swapped for them."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        brief = h.write_brief(BRIEF)
        result = h.run(brief)
        if result.returncode != 0:
            fail(failures, "skills: exit", result.returncode)
            return
        log = h.agent_log()
        skills_line = next((line for line in log.splitlines()
                            if line.startswith("skills=")), "")
        skills = skills_line.split("=", 1)[1] if skills_line else ""
        if "brief" not in skills:
            fail(failures, "skills: the brief skill is not loaded", skills)
        if "swe-updater" not in skills:
            fail(failures, "skills: the brief skill replaced the updater",
                 skills)


def case_without_a_brief_the_skills_are_unchanged(failures):
    """A run with no brief loads the skills it always did."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        result = h.run()
        if result.returncode != 0:
            fail(failures, "no-brief: exit", result.returncode)
            return
        log = h.agent_log()
        skills_line = next((line for line in log.splitlines()
                            if line.startswith("skills=")), "")
        skills = skills_line.split("=", 1)[1] if skills_line else ""
        if "brief" in skills:
            fail(failures, "no-brief: the brief skill was loaded anyway",
                 skills)
        if "swe-updater" not in skills:
            fail(failures, "no-brief: the updater is missing", skills)


def case_the_brief_outranks_the_issue_text(failures):
    """The prompt says the brief wins where the two disagree, and that
    a contradiction is reported."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        brief = h.write_brief(BRIEF)
        result = h.run(brief)
        if result.returncode != 0:
            fail(failures, "outranks: exit", result.returncode)
            return
        log = h.agent_log()
        if "BLOCKED.md" not in log:
            fail(failures, "outranks: the prompt does not tell the lane to "
                 "stop when the brief contradicts the code")
        low = log.lower()
        if "the brief wins" not in low and "brief wins" not in low:
            fail(failures, "outranks: the prompt does not say the brief "
                 "outranks the issue text")


def case_an_unreadable_brief_is_refused_by_name(failures):
    """A brief that cannot be read is refused, and the refusal names
    the file."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        missing = Path(t) / "no-such-brief.md"
        result = h.run(missing)
        if result.returncode == 0:
            fail(failures, "unreadable: exited 0")
            return
        if "no-such-brief.md" not in result.stderr:
            fail(failures, "unreadable: the refusal does not name the "
                 "brief", result.stderr.strip()[:300])
        if h.agent_log().strip():
            fail(failures, "unreadable: the lane ran anyway")


def case_an_empty_brief_is_refused(failures):
    """A brief that copies as nothing is a brief the lane cannot act on.
    An unchecked copy leaves an empty file and the lane runs believing it
    had a brief."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        brief = h.write_brief("")
        result = h.run(brief)
        if result.returncode == 0:
            fail(failures, "empty-brief: exited 0")
            return
        if h.agent_log().strip():
            fail(failures, "empty-brief: the lane ran anyway")
            return
        if "empty" not in result.stderr.lower():
            fail(failures, "empty-brief: the refusal does not say the brief "
                 "is empty", result.stderr.strip()[:300])


def case_the_retry_names_the_brief(failures):
    """The retry prompt names the brief's path, so a resumed attempt
    works from the brief."""
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        brief = h.write_brief(BRIEF)
        # The stub agent exits 0 without committing, so the driver
        # retries, and the retry prompt is the one under test.
        h.env["HERMES_IMPLEMENT_MAX_ATTEMPTS"] = "2"
        result = h.run(brief)
        if result.returncode != 0:
            fail(failures, "retry: exit", result.returncode)
            fail(failures, "retry: stderr", result.stderr[-400:])
            return
        run_dir = h.run_dir()
        prompts = sorted(run_dir.glob("attempt*.prompt"))
        if len(prompts) < 2:
            fail(failures, "retry: there was no retry",
                 [p.name for p in run_dir.glob("attempt*")])
            return
        resume = prompts[-1].read_text()
        if "brief.md" not in resume:
            fail(failures, "retry: the resume prompt does not name the "
                 "brief", resume[:300])


def case_a_stale_brief_skill_is_refused(failures):
    """The brief skill is dispatched, so it is version-checked too.

    The check refuses a dispatched skill whose installed copy differs from
    the repository's. A `brief` skill that reached the lane without the
    check would run the old rules.
    """
    with tempfile.TemporaryDirectory() as t:
        h = Harness(Path(t))
        brief = h.write_brief(BRIEF)
        h.stale_brief_skill()
        result = h.run(brief)
        if result.returncode == 0:
            fail(failures, "stale-brief: exited 0")
            return
        if "brief" not in result.stderr:
            fail(failures, "stale-brief: the refusal does not name the "
                 "brief skill", result.stderr.strip()[:400])
        if h.agent_log().strip():
            fail(failures, "stale-brief: the lane ran anyway")


def main() -> int:
    if not BIN.is_file():
        print(f"FAIL driver not built: {BIN}")
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
    print("PASS hermes-implement --brief: the brief reaches the lane; a "
          "brief over the argument cap reaches it whole; the skill is "
          "composed; no brief leaves the skills alone; the brief "
          "outranks the issue; an unreadable brief is refused; an empty "
          "brief is refused; the retry names it; a stale brief skill is "
          "refused")
    return 0


CASES = (case_the_brief_reaches_the_lane,
         case_a_brief_over_the_argument_cap_reaches_the_lane,
         case_the_brief_skill_is_composed_with_the_others,
         case_without_a_brief_the_skills_are_unchanged,
         case_the_brief_outranks_the_issue_text,
         case_an_unreadable_brief_is_refused_by_name,
         case_an_empty_brief_is_refused,
         case_the_retry_names_the_brief,
         case_a_stale_brief_skill_is_refused)


if __name__ == "__main__":
    sys.exit(main())
