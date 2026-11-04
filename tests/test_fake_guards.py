#!/usr/bin/env python3
"""A test fake run by hand must not act on the repository it is run from.

A fake that runs git on a path from its arguments is dangerous when that
path is empty: `cd ""` succeeds and stays where it is, and `git -C ""`
means the current directory, so the fake acts on the lane's own branch.

So every fake in `tests/` that runs git on a path from its arguments
refuses when that path is empty. This suite finds them, runs each one
with no arguments from inside a throwaway repository, and asserts that
the repository's HEAD is unchanged.

The fakes are found by reading the test files rather than by a list
here: a fake added later is covered without this file being edited, and
a fake that loses its guard is caught by the scan. The scan matches a
program by its text, not by the name it is bound to.

A fake that takes its path from the environment has the same hazard and
is covered by the rule it can meet: it leaves the repository's HEAD
alone.

Run directly: `python3 tests/test_fake_guards.py`.
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# The git invocations that act on a path from the fake's arguments.
# `cd "$wt"` and `git -C "$wt"` are the two shapes; the guard has to
# cover both, because they differ in what an empty path means.
PATH_GIT_RE = re.compile(
    r'(?:cd\s+"-?\$|git\s+-C\s+"-?\$|git\s+--git-dir[= ]\s*"?\$|'
    r'--work-tree[= ]\s*"?\$)')

# The variable the fake takes its path from. The git match ends at the
# `$`, so the name is read from what follows it: `$wt`, `${wt}` and
# `${wt:?message}` all name `wt`.
PATH_VAR_RE = re.compile(r'^\{?([A-Za-z_][A-Za-z0-9_]*)')

# The environment variable a fake reads its path from. Such a fake is
# guarded differently: it no-ops on an empty path rather than refusing,
# because it may have other work to do.
ENV_PATH_RE = re.compile(r'FAKE_HERMES_WT')


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def make_repo(root: Path) -> Path:
    """A throwaway repository with one commit whose HEAD must survive."""
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("the original content\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "the commit that must survive")
    return repo


def embedded_programs(text: str):
    """Every embedded program in a test file, with where it came from and
    the line it starts on.

    A fake is a program written to a file and executed, so both ways of
    spelling one are found: a literal bound to a name, and a literal
    handed to `write_text`. The second is how a fake written inside a
    function is spelled, and a scan that only looked for the first
    would miss it.

    The name may be any identifier, not just an upper-case one.
    """
    out = []
    # A literal bound to a name: `NAME = """..."""`.
    for m in re.finditer(
            r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:textwrap\.dedent\(\)?)?\s*'
            r'(?P<prefix>f?r?)"""', text):
        start = m.end()
        end = text.find('"""', start)
        if end == -1:
            continue
        out.append((m.group("name"), text[start:end],
                    text[:m.start()].count("\n") + 1, False))
    # A literal written to a file: `something.write_text("""...""")`.
    for m in re.finditer(
            r'\.write_text\(\s*(?:textwrap\.dedent\()?\s*(?P<prefix>f?r?)"""',
            text):
        start = m.end()
        end = text.find('"""', start)
        if end == -1:
            continue
        out.append((None, text[start:end],
                    text[:m.start()].count("\n") + 1, True))
    return out


def find_risky_fakes():
    """The fakes that run git on a path from their arguments.

    A fake is an embedded program that mentions git and reaches for a
    variable path. The variable it reaches for is what the guard has to
    check, so it is read out of the body rather than assumed.

    Where the path comes from is recorded too, because the two sources
    are guarded differently: an argument-path fake has to refuse an
    empty one, an environment-path fake has to leave the repository
    alone.
    """
    risky = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        text = path.read_text()
        for name, body, line, written in embedded_programs(text):
            if "#!" not in body:
                continue
            if not re.search(r'\bgit\b', body):
                continue
            match = PATH_GIT_RE.search(body)
            if not match:
                continue
            # The variable immediately after the `$` the match ended on.
            var_match = PATH_VAR_RE.match(body[match.end():])
            if not var_match:
                continue
            risky.append({
                "file": path,
                "name": name or f"<written at line {line}>",
                "line": line,
                "body": body,
                "var": var_match.group(1),
                "from_env": bool(ENV_PATH_RE.search(body)),
            })
    return risky


def extract_body(source: str, body: str) -> str:
    """The fake's own text, as the shell would see it.

    The braces are doubled in a fake written inside an f-string, so the
    text that reaches the shell has single braces. The body is also
    indented, because it is a literal inside a Python function: the
    first line carries no indentation, so `textwrap.dedent` on its own
    would remove nothing.
    """
    resolved = body.replace("{{", "{").replace("}}", "}")
    # The leading `\` and its newline: the literal continues on the
    # next line.
    resolved = resolved.lstrip("\\").lstrip("\n")
    # The common indentation, computed the way `textwrap.dedent`
    # computes it but over the body rather than over the literal's
    # first line, which is the `\` that has none.
    lines = resolved.splitlines()
    indents = [len(line) - len(line.lstrip()) for line in lines
               if line.strip()]
    common = min(indents) if indents else 0
    return "\n".join(line[common:] if line.strip() else ""
                      for line in lines) + "\n"


def case_the_scan_finds_the_fakes(failures):
    """The scan has to find the fakes, or it proves nothing. Each one it
    finds is named, so a fake that stops being risky shows up as a
    change in the list."""
    risky = find_risky_fakes()
    if not risky:
        fail(failures, "scan: no risky fakes were found at all")
        return
    found = {(f["file"].name, f["line"]) for f in risky}
    expected = {
        ("test_agent_fix_pr.py", 85),
        ("test_lane_rustc.py", 265),
        ("test_lane_settled.py", 162),
    }
    missing = expected - found
    if missing:
        fail(failures, "scan: the fakes the issue names were not found",
             sorted(missing))


def case_each_fake_refuses_an_empty_path(failures):
    """The case the issue names: a fake run with no arguments from
    inside a throwaway repository leaves that repository's HEAD alone.

    The two sources of a path are held to the rule each of them can
    meet. An argument-path fake has nothing else to do without a path,
    so it refuses and says which path was missing. An environment-path
    fake may have other work to do, so it does that work and leaves the
    repository alone.
    """
    risky = find_risky_fakes()
    if not risky:
        fail(failures, "refuse: no risky fakes were found at all")
        return
    for fake in risky:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            repo = make_repo(tmp)
            before = git(repo, "rev-parse", "HEAD").stdout.strip()
            script = tmp / "fake"
            script.write_text(extract_body(fake["file"].read_text(),
                                           fake["body"]))
            script.chmod(0o755)
            # The environment the fakes read: the destructive cases.
            env = dict(os.environ)
            env.update({
                "FAKE_LANE": "amend",
                "FAKE_HERMES_LOG": str(tmp / "log"),
                "FAKE_HERMES_WT": "",
                "FAKE_REPLIES": str(tmp / "replies.json"),
            })
            (tmp / "replies.json").write_text("[]")
            result = subprocess.run([str(script)], capture_output=True,
                                    text=True, env=env, cwd=str(repo))
            after = git(repo, "rev-parse", "HEAD").stdout.strip()
            label = f"{fake['file'].name}:{fake['name']}"
            if after != before:
                fail(failures, f"refuse[{label}]: the repository's HEAD "
                     f"changed", f"{before[:12]} -> {after[:12]}")
                continue
            if fake["from_env"]:
                # It took the path from the environment and the
                # environment had none, so it did nothing.
                continue
            if result.returncode == 0:
                fail(failures, f"refuse[{label}]: the fake exited 0 on an "
                     f"empty path")
                continue
            # The refusal has to say what was wrong, so a person
            # running it by hand knows what to pass.
            if fake["var"] not in result.stderr:
                fail(failures, f"refuse[{label}]: the refusal does not name "
                     f"the missing path",
                     result.stderr.strip()[:200])


def case_the_fake_still_works_with_a_path(failures):
    """The guard must not refuse a fake that was given a worktree. A
    guard that always refuses would make the fakes useless.

    Every guarded fake is checked, not just one: a guard that refused
    everything would pass a case that only drove one fake.
    """
    risky = find_risky_fakes()
    if not risky:
        fail(failures, "still-works: no risky fakes were found at all")
        return
    for fake in risky:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            # The layout the fakes expect: a repository beside the
            # worktree, one level above it.
            repo = make_repo(tmp)
            wt = tmp / "wt" / "issue-1"
            wt.mkdir(parents=True)
            git(wt.parent, "init", "-q", "-b", "main", str(wt.parent))
            git(wt.parent, "config", "user.email", "t@t")
            git(wt.parent, "config", "user.name", "t")
            (wt / "file.txt").write_text("original\n")
            git(wt, "add", ".")
            git(wt, "commit", "-q", "-m", "base")

            script = tmp / "fake"
            script.write_text(extract_body(fake["file"].read_text(),
                                           fake["body"]))
            script.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "FAKE_LANE": "commit",
                "FAKE_HERMES_LOG": str(tmp / "log"),
                "FAKE_HERMES_WT": str(wt),
                "FAKE_REPLIES": str(tmp / "replies.json"),
            })
            (tmp / "replies.json").write_text("[]")
            before = git(wt, "rev-parse", "HEAD").stdout.strip()
            result = subprocess.run([str(script), "--worktree", str(wt)],
                                    capture_output=True, text=True, env=env,
                                    cwd=str(tmp))
            label = f"{fake['file'].name}:{fake['name']}"
            after = git(wt, "rev-parse", "HEAD").stdout.strip()
            # The fake has to have done its work. A guard that refuses
            # everything -- or one whose guard fires on a path it was
            # given -- leaves the worktree as it was.
            if after == before:
                fail(failures, f"still-works[{label}]: the fake did nothing "
                     f"with the worktree it was given",
                     result.stderr.strip()[:300])


def case_a_fake_that_reads_a_path_from_the_environment(failures):
    """A fake that takes its path from the environment rather than from
    an argument has the same hazard: an empty path is the current
    directory. The guard has to cover it.

    The fake is read out of the file rather than imported: it is built
    inside a function there, so there is no module-level name to
    import.
    """
    source = (TESTS_DIR / "test_lane_settled.py").read_text()
    if "FAKE_HERMES_WT" not in source:
        fail(failures, "env-path: the fake no longer reads a path from "
             "the environment")
        return
    # The fake in question already guards its path, which is the shape
    # the other two were brought to.
    guarded = re.search(r'\[\[\s*-n\s+"\$wt"\s*\]\].*\[\[\s*-d\s+"\$wt"',
                         source, re.S)
    if not guarded:
        fail(failures, "env-path: the fake does not check that its path is "
             "a directory before running git in it")


def case_the_lane_skills_say_it(failures):
    """The rules the issue names have to be in the skills the lane loads.

    The guard in the fake is the first half of the fix; this is the
    second. A lane that runs a fake by hand in its own worktree is not
    stopped by a fake refusing to run -- the fake may be a helper with
    no guard, or a command the lane composes itself.
    """
    wanted = (
        "a scratch copy",
        "never in the worktree you are changing",
        "Do not export `GIT_*`",
    )
    for name in ("swe-updater", "swe-review-fix"):
        path = REPO_ROOT / "skills" / name / "SKILL.md"
        if not path.is_file():
            fail(failures, f"skills[{name}]: the skill is not there", path)
            continue
        # The prose is wrapped, so a phrase can straddle a line break.
        # The text is compared with its whitespace collapsed.
        text = " ".join(path.read_text().split())
        for phrase in wanted:
            if " ".join(phrase.split()) not in text:
                fail(failures, f"skills[{name}]: the skill does not say "
                     f"{phrase!r}")


def main() -> int:
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
    print("PASS fakes: the scan finds them; each refuses an empty path "
          "and leaves the repository's HEAD alone; a fake given a "
          "worktree still works; a fake that reads a path from the "
          "environment guards it; the lane skills say it")
    return 0


CASES = (case_the_scan_finds_the_fakes,
         case_each_fake_refuses_an_empty_path,
         case_the_fake_still_works_with_a_path,
         case_a_fake_that_reads_a_path_from_the_environment,
         case_the_lane_skills_say_it)


if __name__ == "__main__":
    sys.exit(main())
