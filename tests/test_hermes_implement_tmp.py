#!/usr/bin/env python3
"""Pin that hermes-implement gives each lane a private directory.

A lane's `mktemp -d` and Python's `tempfile.mkdtemp` must both land inside the
directory the script exports as TMPDIR, and that directory, with its
HANDOVER.md, must survive every run and be printed on stdout.

Run directly: `python3 tests/test_hermes_implement_tmp.py`. A stub agent and a
stub gh stand in for hermes and the network.
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
HERMES_IMPLEMENT = REPO_ROOT / "bin" / "hermes-implement"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """A git invocation that fails the test on error and returns the
    CompletedProcess for inspection. `cwd` is the git working tree."""
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True, check=True)


def make_worktree(tmpdir: Path) -> Path:
    """Stand up a throwaway git repo with one commit. hermes-implement
    cd's into this tree and `git rev-parse --show-toplevel`s it, so the
    path the stub agent sees as cwd is what the test pins against."""
    repo = tmpdir / "wt"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet")
    for k, v in [("user.email", "test@test"), ("user.name", "test"),
                 ("init.defaultBranch", "main")]:
        git(repo, "config", k, v)
    (repo / "README").write_text("seed\n")
    git(repo, "add", "README")
    git(repo, "commit", "--quiet", "-m", "seed")
    return repo


STUB_HERMES_BODY = """\
import os, subprocess, sys, tempfile

root = os.getcwd()

# The script exports TMPDIR/TMP/TEMP at process start; we just read them
# back so the test can pin what the lane actually saw.
print('TMPDIR=' + os.environ.get('TMPDIR', ''))
print('TMP=' + os.environ.get('TMP', ''))
print('TEMP=' + os.environ.get('TEMP', ''))
# lane-settled, run by the lane itself, must see this lane as still running.
if os.environ.get('STUB_LANE_SETTLED'):
    rc = subprocess.run([os.environ['STUB_LANE_SETTLED'], root],
                        capture_output=True).returncode
    print('SETTLED_RC=' + str(rc))
# A lane that works from a subdirectory must still find its TMPDIR.
if os.environ.get('STUB_CHDIR'):
    os.makedirs(os.environ['STUB_CHDIR'], exist_ok=True)
    os.chdir(os.environ['STUB_CHDIR'])
# `mktemp -d` with no template chooses $TMPDIR (now the private dir) as
# its parent. Anything else would mean the lane leaked out.
mktemp = subprocess.check_output(['mktemp', '-d'], text=True).strip()
print('MKTEMP=' + mktemp)
# Python's tempfile.mkdtemp defaults to TMPDIR (also our private dir).
py = subprocess.check_output(
    [sys.executable, '-c', 'import tempfile; print(tempfile.mkdtemp())'],
    text=True).strip()
print('PY=' + py)
print('CWD=' + os.getcwd())

if os.environ.get('STUB_FAIL'):
    # Leave the run broken: no commit, no handover. The script will
    # exhaust its attempts and declare FAILED.
    sys.exit(0)

# Success means a commit on the lane's branch and a handover in the run
# directory the script expects.
tmpdir = os.environ['TMPDIR']
with open(os.path.join(tmpdir, 'HANDOVER.md'), 'w') as fh:
    fh.write('stub handover\\n')
subprocess.check_call(
    ['git', '-C', root, 'commit', '--allow-empty', '-m', 'stub'],
    env={**os.environ,
         'GIT_AUTHOR_NAME': 'stub',
         'GIT_AUTHOR_EMAIL': 'stub@stub',
         'GIT_COMMITTER_NAME': 'stub',
         'GIT_COMMITTER_EMAIL': 'stub@stub'})
sys.exit(0)
"""


def write_stub_hermes(tmpdir: Path) -> Path:
    """Drop a Python stub in place of the hermes agent. The body
    prints a six-line report (TMPDIR, TMP, TEMP, MKTEMP, PY, CWD) so
    the test can pin where each subprocess's tmpdir resolved, and
    either commits + writes a handover (success) or does nothing
    (failure) based on $STUB_FAIL.

    The script carries a python shebang so the test can hand
    HERMES_PYTHON a single path -- the script's `${HERMES_PYTHON:-...}`
    is one token, and a multi-word value would arrive at execve as
    a single argv[0] containing a space."""
    script = tmpdir / "hermes_stub.py"
    script.write_text("#!/usr/bin/env python3\n" + STUB_HERMES_BODY)
    script.chmod(0o755)
    return script


def write_stub_gh(tmpdir: Path) -> Path:
    """Drop a `gh` shim that prints a stub issue body. hermes-implement
    runs `gh issue view N` before invoking the agent; without this the
    network call would block or fail."""
    shim = tmpdir / "gh"
    shim.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # `gh api user` answers who the call would act as, which `gh_as`
        # checks against IMPLEMENTER_LOGINS.
        if [[ "$1" == "api" && "$2" == "user" ]]; then
            echo "test-implementer"
            exit 0
        fi
        echo 'stub issue'
        """))
    shim.chmod(0o755)
    return shim


def run_lane(tmpdir: Path, worktree: Path, state_dir: Path | str,
             stub: Path, fail: bool, cwd: Path | None = None,
             chdir: str | None = None) -> subprocess.CompletedProcess:
    """Invoke bin/hermes-implement against a fresh worktree, with stubs
    for the agent and for `gh`. `fail` flips the stub between the
    success path (commit + handover) and the failure path (no commit,
    no handover)."""
    gh_dir = tmpdir / "ghbin"
    gh_dir.mkdir(exist_ok=True)
    gh_shim = write_stub_gh(gh_dir)
    env = {
        **os.environ,
        # HERMES_PYTHON is a single token the script passes to execve;
        # a multi-word value would arrive as one argv[0] and exit 127.
        "HERMES_PYTHON": str(stub),
        "AGENT_STATE_DIR": str(state_dir),
        # Prepend the gh shim dir so the script's `gh issue view` resolves
        # to our stub before the system's gh.
        "PATH": f"{gh_dir}:{os.environ.get('PATH', '')}",
        "IMPLEMENTER_LOGINS": "test-implementer",
        # Cap attempts at 2 so the failure case finishes promptly: each
        # attempt is otherwise bounded by the agent's timeout (90 minutes
        # by default). The stub exits 0 immediately either way.
        "HERMES_IMPLEMENT_ATTEMPTS": "2",
        "HERMES_IMPLEMENT_TIMEOUT": "30",
    }
    if chdir:
        env["STUB_CHDIR"] = chdir
        env["STUB_LANE_SETTLED"] = str(HERMES_IMPLEMENT.parent / "lane-settled")
    if fail:
        env["STUB_FAIL"] = "1"
    else:
        env.pop("STUB_FAIL", None)
    home = tmpdir / "home"
    install_skills(home)
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        [str(HERMES_IMPLEMENT), "--issue", "1", "--worktree", str(worktree)],
        capture_output=True, text=True, env=env, timeout=120, cwd=cwd)


def parse_stub_report(stdout: str) -> dict:
    """Read the six-line report the stub prints to attempt1.stdout.
    Returned as a dict the assertions can index by key."""
    report = {}
    for line in stdout.splitlines():
        for key in ("TMPDIR", "TMP", "TEMP", "MKTEMP", "PY", "CWD", "SETTLED_RC"):
            prefix = f"{key}="
            if line.startswith(prefix):
                report[key] = line[len(prefix):]
                break
    return report


def main() -> int:
    failures: list = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state = tmp / "agent-tools"
        wt = make_worktree(tmp / "real")
        # Place the stub inside `tmp` so the test owns the only path that
        # could ever be removed; hermes-implement will write $WORK under
        # `state`, which is also under `tmp`.
        stub_dir = tmp / "stubs"
        stub_dir.mkdir()
        stub = write_stub_hermes(stub_dir)

        # SUCCESS PATH: the stub commits and writes a handover. The script
        # sees HEAD != BASE on the first iteration, exits cleanly, and the
        # private directory and its HANDOVER.md must survive -- the caller
        # reads the handover after every run, successful or not.
        success_state = state / "success"
        success_state.mkdir(parents=True)
        ok = run_lane(tmp, wt, success_state, stub, fail=False)
        if ok.returncode != 0:
            failures.append(
                f"success run exited non-zero: rc={ok.returncode} "
                f"stderr={ok.stderr!r}")
        # The script prints the private directory's path on stdout on
        # every exit, including success. A caller that reads it from the
        # command's stdout should not have to grep stderr.
        success_paths = [ln for ln in ok.stdout.splitlines() if ln]
        if not success_paths:
            failures.append(
                f"success run printed no path on stdout: "
                f"stdout={ok.stdout!r}")
        else:
            success_kept = Path(success_paths[-1])
            if not success_kept.exists():
                failures.append(
                    f"success run's printed path does not exist: "
                    f"{success_kept} (stdout={ok.stdout!r})")
            elif not success_kept.is_dir():
                failures.append(
                    f"success run's printed path is not a directory: "
                    f"{success_kept}")
            else:
                # The private directory survives a successful run: the
                # HANDOVER.md the lane wrote must still be there for the
                # caller to read after the run is over.
                handover = success_kept / "HANDOVER.md"
                if not handover.exists():
                    failures.append(
                        f"success run removed the kept directory "
                        f"(HANDOVER.md gone): {success_kept}")
                # And the directory lives under AGENT_STATE_DIR, not a
                # leak into /tmp.
                work_dir_success = success_state / "hermes-implement-runs"
                try:
                    rel = success_kept.resolve().relative_to(work_dir_success.resolve())
                except ValueError:
                    failures.append(
                        f"success run's kept path is not under "
                        f"hermes-implement-runs: {success_kept}")
                else:
                    if not rel.name.startswith("hermes-implement-"):
                        failures.append(
                            f"success run's kept directory name is "
                            f"unexpected: {rel.name}")

        # FAILURE PATH: the stub does nothing. After two attempts the
        # script declares FAILED and must leave the directory in place,
        # with its path printed on stdout just like a successful run.
        # Use a separate state dir so the two runs do not collide on a
        # shared WORKTREE_HASH (state/lane-active/<hash>).
        fail_wt = make_worktree(tmp / "real_fail")
        fail_state = state / "failure"
        fail_state.mkdir(parents=True)
        bad = run_lane(tmp, fail_wt, fail_state, stub, fail=True)
        # The script always exits 0 (the failure is reported on stderr);
        # what matters is the FAILED line on stderr and the directory's
        # path on stdout.
        if "FAILED:" not in bad.stderr:
            failures.append(
                f"failure run did not report FAILED: stderr={bad.stderr!r}")
        # The script prints the kept directory's path on stdout on every
        # exit, success or failure. The path is the bare value, with no
        # FAILED prefix -- the same shape a successful run prints -- so a
        # caller reading the script's stdout sees one line, in both cases.
        fail_paths = [ln for ln in bad.stdout.splitlines() if ln]
        if not fail_paths:
            failures.append(
                f"failure run printed no path on stdout: "
                f"stdout={bad.stdout!r}")
        elif fail_paths:
            kept_path = Path(fail_paths[-1])
            if not kept_path.exists():
                failures.append(
                    f"failure run printed path that does not exist: "
                    f"{kept_path} (stdout={bad.stdout!r})")
            elif not kept_path.is_dir():
                failures.append(
                    f"failure run printed a path that is not a directory: "
                    f"{kept_path}")
            # The kept path lives under $AGENT_STATE_DIR/hermes-implement-runs/,
            # i.e. the private directory, not a leak into /tmp.
            try:
                rel = kept_path.resolve().relative_to(
                    (fail_state / "hermes-implement-runs").resolve())
            except ValueError:
                failures.append(
                    f"kept path is not under hermes-implement-runs: {kept_path}")
            else:
                # And it has the suffix the script's mktemp template used.
                if not rel.name.startswith("hermes-implement-"):
                    failures.append(
                        f"kept directory's name is unexpected: {rel.name}")
            # Now read the stub's report from attempt1.stdout inside the
            # kept directory and pin the four assertions: TMPDIR is set,
            # mktemp -d and tempfile.mkdtemp resolve under TMPDIR.
            attempt_log = kept_path / "attempt1.stdout"
            if not attempt_log.exists():
                failures.append(
                    f"kept directory has no attempt log: {kept_path}")
            else:
                report = parse_stub_report(attempt_log.read_text())
                tmpdir_seen = report.get("TMPDIR", "")
                if tmpdir_seen != str(kept_path):
                    failures.append(
                        f"stub's TMPDIR ({tmpdir_seen!r}) does not match the "
                        f"directory the script kept ({str(kept_path)!r})")
                # TMP and TEMP are what the issue also asks the script to
                # export; pin them too so a future edit that drops one of
                # the three is caught here rather than in production.
                for key in ("TMP", "TEMP"):
                    if report.get(key, "") != str(kept_path):
                        failures.append(
                            f"stub's {key} ({report.get(key, '')!r}) does not "
                            f"match the private directory ({str(kept_path)!r})")
                mktemp_seen = report.get("MKTEMP", "")
                if not (mktemp_seen and Path(mktemp_seen).is_relative_to(kept_path)):
                    failures.append(
                        f"stub's mktemp -d ({mktemp_seen!r}) did not resolve "
                        f"inside the private directory ({str(kept_path)!r})")
                py_seen = report.get("PY", "")
                if not (py_seen and Path(py_seen).is_relative_to(kept_path)):
                    failures.append(
                        f"stub's tempfile.mkdtemp ({py_seen!r}) did not "
                        f"resolve inside the private directory "
                        f"({str(kept_path)!r})")
                cwd_seen = report.get("CWD", "")
                if cwd_seen != str(fail_wt):
                    failures.append(
                        f"stub's cwd ({cwd_seen!r}) was not the worktree "
                        f"({str(fail_wt)!r})")

        # Cleanup is the test's responsibility: the script leaves $WORK
        # behind on both the success and the failure path, and we created
        # everything under `tmp` so removing the TemporaryDirectory takes
        # it with it. No further action needed.

    # A lane that changes directory still finds its TMPDIR, and lane-settled
    # run by the lane from its worktree sees the lane as still running.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wt = make_worktree(tmp / "nest")
        stub_dir = tmp / "stubs"
        stub_dir.mkdir()
        stub = write_stub_hermes(stub_dir)
        runs = tmp / "state" / "hermes-implement-runs"
        run = run_lane(tmp, wt, tmp / "state", stub, fail=False, cwd=tmp,
                       chdir="nested")
        printed = [ln for ln in run.stdout.splitlines() if ln]
        log = Path(printed[-1]) / "attempt1.stdout" if printed else Path("/nonexistent")
        report = parse_stub_report(log.read_text() if log.exists() else "")
        for key in ("TMPDIR", "MKTEMP", "PY"):
            if not report.get(key, "").startswith(str(runs) + "/"):
                failures.append(
                    f"after chdir: {key}={report.get(key)!r} is not under {runs}")
        if report.get("SETTLED_RC") != "1":
            failures.append(
                f"lane-settled run by the lane returned "
                f"{report.get('SETTLED_RC')!r}, not 1 (still running)")

    # A relative AGENT_STATE_DIR is refused, from the environment or from the
    # tools' env file, so no two tools can resolve it differently.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wt = make_worktree(tmp / "rel")
        envfile = tmp / "tools.env"
        envfile.write_text("AGENT_STATE_DIR=.state\n")
        stub_dir = tmp / "stubs"
        stub_dir.mkdir()
        stub = write_stub_hermes(stub_dir)
        rel = run_lane(tmp, wt, ".state", stub, fail=False, cwd=tmp)
        if rel.returncode != 2 or "absolute" not in rel.stderr:
            failures.append(
                f"relative AGENT_STATE_DIR not refused by hermes-implement: "
                f"rc={rel.returncode} stderr={rel.stderr[-200:]!r}")
        settled = subprocess.run(
            [str(HERMES_IMPLEMENT.parent / "lane-settled"), str(wt)],
            capture_output=True, text=True, cwd=tmp,
            env={**os.environ, "TOOLS_ENV": str(envfile)})
        if settled.returncode != 2 or "absolute" not in settled.stderr:
            failures.append(
                f"relative AGENT_STATE_DIR in the env file not refused by "
                f"lane-settled: rc={settled.returncode} "
                f"stderr={settled.stderr[-200:]!r}")

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        return 1
    print("PASS hermes-implement private directory is honored end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())