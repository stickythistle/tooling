#!/usr/bin/env python3
"""Pin that the review phase's prompt stays under the one-argument cap.

Linux refuses an execve whose single argument passes MAX_ARG_STRLEN (128 KiB),
and the previous review the prompt carries forward only grows. The previous
review and the changed-file list therefore travel as files the prompt names.

Each case runs the real pipeline/review.sh against a local bare origin, with
$PIPELINE_AGENT a stub that records the prompt it was given, follows the paths
it names, and writes REVIEW_NOTES.md the way the real agent would.

Run directly: `python3 tests/test_review_phase.py`.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_install_fixture import install as install_skills
SCRIPT = REPO_ROOT / "pipeline" / "review.sh"
MAX_ARG_STRLEN = 131072

STUB_AGENT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, re, sys
    args = sys.argv[1:]
    # pipeline/review.sh now hands the prompt to the agent via
    # `--query-file PATH` rather than `-z "$prompt"`. The stub reads the
    # file at `--query-file` to record its size; if the runtime ever
    # falls back to `-z`, this branch is skipped (the prompt went on
    # argv instead) and `rec["prompt_via_query_file"]` is False. `args`
    # is the full argv the helper passed after the program name; the
    # multi-word PIPELINE_AGENT case asserts on it to confirm the
    # helper word-split the agent command rather than treating it as
    # one program name.
    seen = {"prompt_via_query_file": False,
            "prompt_bytes": 0,
            "prompt_sha": "",
            "args": list(args)}
    if "--query-file" in args:
        qf = args[args.index("--query-file") + 1]
        if os.path.exists(qf):
            with open(qf, "rb") as fh:
                data = fh.read()
            seen["prompt_via_query_file"] = True
            seen["prompt_bytes"] = len(data)
            import hashlib
            seen["prompt_sha"] = hashlib.sha256(data).hexdigest()
        # The prompt body names the context files (`previous-review.md`,
        # `changed-files.txt`); the stub still reads those, just as it
        # does in production.
        for name in ("previous-review.md", "changed-files.txt"):
            m = re.search(r"(\\S+/" + re.escape(name) + r")", data.decode("utf-8", "replace"))
            if m and os.path.exists(m.group(1)):
                seen[name] = open(m.group(1)).read()
                seen[name + ":path"] = m.group(1)
    elif "-z" in args:
        # The previous `-z` path: still record, but note that the
        # prompt bytes would have hit MAX_ARG_STRLEN -- the test's
        # assertion still flags this. Reading `args[index("-z")+1]`
        # here lets older invocations be diagnosed, even though the
        # helper now refuses to use `-z`.
        seen["prompt_via_argv"] = True
        seen["prompt_bytes"] = len(args[args.index("-z") + 1].encode())
    json.dump(seen, open(os.environ["STUB_RECORD"], "w"))
    notes = "# Review\\n\\n" + seen.get("previous-review.md", "") + "\\n### new finding\\n"
    open(os.path.join(os.environ["PIPELINE_REVIEW_WORK"], "REVIEW_NOTES.md"), "w").write(notes)
    # -Q: prepend the session_id marker; the helper strips it on the
    # way back, so the script's caller (cron, --no-agent delivery)
    # never sees it on stdout.
    print("session_id: stub-session-123")
""")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def setup(tmp: Path, prev_notes: str | None, *,
          multiword_agent: bool = False):
    origin = tmp / "origin.git"
    git(tmp, "init", "-q", "--bare", "--initial-branch=main", str(origin))
    seed = tmp / "seed"
    git(tmp, "clone", "-q", str(origin), str(seed))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(seed, "config", k, v)
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "a.txt").write_text("one\n")
    git(seed, "add", "a.txt")
    git(seed, "commit", "-qm", "first")
    git(seed, "push", "-q", "origin", "main")
    if prev_notes is not None:
        git(seed, "checkout", "-q", "-b", "reviewer-2000-01-01")
        (seed / "REVIEW_NOTES.md").write_text(prev_notes)
        git(seed, "add", "REVIEW_NOTES.md")
        git(seed, "commit", "-qm", "review")
        git(seed, "push", "-q", "origin", "reviewer-2000-01-01")
        git(seed, "checkout", "-q", "main")

    work = tmp / "work"
    git(tmp, "clone", "-q", str(origin), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(work, "config", k, v)
    git(work, "remote", "set-head", "origin", "main")

    stub = tmp / "agent"
    if multiword_agent:
        # Wrap the stub in a shell command with extra words, the
        # way $PIPELINE_AGENT is on the live host (`<hermes_python>
        # -m hermes_cli.main`). The wrapper is just a shell alias:
        # it runs the stub with whatever args follow. The helper
        # has to word-split the string into argv; without that
        # word-split, the agent would never start and the review
        # would silently no-op.
        wrapper = tmp / "agent-wrapper"
        wrapper.write_text("#!/usr/bin/env bash\nexec \"" + str(stub) + "\" \"$@\"\n")
        wrapper.chmod(0o755)
        agent_cmd = f"bash {wrapper} -m stub_module"
    else:
        agent_cmd = str(stub)
    stub.write_text(STUB_AGENT)
    stub.chmod(0o755)
    home = tmp / "home"
    home.mkdir()
    # The phase refuses to start when swe-reviewer differs from the
    # repository's copy. The profile the picker falls back to is `default`.
    install_skills(home, ["default"])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPELINE_", "GPU_GROUP"))}
    env.update(HOME=str(home), TMPDIR=str(tmp),
               HERMES_HOME=str(home),
               AGENT_PIPELINE_ENV=str(tmp / "no-env"),
               PIPELINE_REPO=str(work), PIPELINE_GH_REPO="owner/proj",
               PIPELINE_VERIFY="true", PIPELINE_AGENT=agent_cmd,
               PIPELINE_REVIEW_WORK=str(work),
               PIPELINE_STATE_DIR=str(tmp / "state"),
               PIPELINE_QUEUE=str(tmp / "queue"),
               STUB_RECORD=str(tmp / "record.json"))
    return origin, work, env


def run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def case_large_previous_review_reaches_the_agent(failures):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        prev = "### old finding\n" + ("x" * 99 + "\n") * 2000  # 200 KB
        origin, work, env = setup(tmp, prev)
        r = run(env)
        if r.returncode != 0:
            failures.append(f"large-previous: exit {r.returncode}: {r.stdout}{r.stderr}")
            return
        rec = json.loads((tmp / "record.json").read_text())
        # The agent must have been called with `--query-file PATH`,
        # not `-z "$prompt"` -- a 200 KB prompt via `-z` would have hit
        # MAX_ARG_STRLEN and the agent would never have started. The
        # previous-review file the prompt names is what makes the
        # review reach the agent in production.
        if not rec.get("prompt_via_query_file"):
            failures.append(
                "large-previous: stub was called without --query-file; "
                "the prompt went over argv via -z instead")
        if rec["prompt_bytes"] >= MAX_ARG_STRLEN // 8:
            failures.append(f"large-previous: prompt is {rec['prompt_bytes']} bytes")
        if rec.get("previous-review.md", "").strip() != prev.strip():
            failures.append("large-previous: the agent did not get the previous review")
        if Path(rec["previous-review.md:path"]).is_relative_to(work):
            failures.append("large-previous: context file sits inside the checkout")
        if Path(rec["previous-review.md:path"]).exists():
            failures.append("large-previous: context file left behind after the run")
        branch = [b for b in git(origin, "branch", "--format=%(refname:short)").split()
                  if b.startswith("reviewer-") and b != "reviewer-2000-01-01"]
        if not branch or "### old finding" not in git(origin, "show", f"{branch[0]}:REVIEW_NOTES.md"):
            failures.append(f"large-previous: no review pushed carrying the old finding: {branch}")


def case_whole_repository_scope_lists_files_through_a_file(failures):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        origin, work, env = setup(tmp, None)
        r = run(env)
        if r.returncode != 0:
            failures.append(f"whole-repo: exit {r.returncode}: {r.stdout}{r.stderr}")
            return
        rec = json.loads((tmp / "record.json").read_text())
        # The whole-repository prompt must also reach the agent via
        # `--query-file`, not `-z`.
        if not rec.get("prompt_via_query_file"):
            failures.append(
                "whole-repo: stub was called without --query-file; "
                "the prompt went over argv via -z instead")
        if rec.get("changed-files.txt", "").split() != ["a.txt"]:
            failures.append(f"whole-repo: changed-file list was {rec.get('changed-files.txt')!r}")
        if "previous-review.md" in rec:
            failures.append("whole-repo: a previous review appeared with none on origin")


def case_multiword_pipeline_agent_starts_and_writes_review(failures):
    """On the live host PIPELINE_AGENT is `…/venv/bin/python -m
    hermes_cli.main`: one command line, multiple words. The
    helper has to word-split it. Without that word-split, the
    agent is run as one program name (`bash <wrapper> -m
    stub_module`) and exits with "No such file or directory",
    and the review no-ops while reporting nothing on stderr.
    This case pins the multi-word shape end-to-end through
    pipeline/review.sh."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        origin, work, env = setup(tmp, None, multiword_agent=True)
        r = run(env)
        if r.returncode != 0:
            failures.append(
                f"multiword-agent: exit {r.returncode}: "
                f"{r.stdout}{r.stderr}")
            return
        rec = json.loads((tmp / "record.json").read_text())
        if not rec.get("prompt_via_query_file"):
            failures.append(
                "multiword-agent: stub was called without --query-file; "
                "the helper's word-split dropped the prompt on the floor")
        # The agent has to have actually run end-to-end, with the
        # helper's `chat --query-file …` line in its argv. A
        # helper that didn't word-split the agent would invoke
        # `bash <wrapper> -m stub_module` as a single program
        # name, the shell would exit 127 with "No such file or
        # directory", and the stub would never run. The stub
        # always writes `args`, so an `args` missing the helper's
        # `chat` is the regression signature.
        args = rec.get("args") or []
        if "chat" not in args:
            failures.append(
                f"multiword-agent: agent's argv did not include "
                f"the helper's `chat`; the wrapper's `exec` "
                f"never ran, which means the helper did not "
                f"word-split the agent command. "
                f"args={args!r}")


def main() -> int:
    failures: list[str] = []
    case_large_previous_review_reaches_the_agent(failures)
    case_whole_repository_scope_lists_files_through_a_file(failures)
    case_multiword_pipeline_agent_starts_and_writes_review(failures)
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
