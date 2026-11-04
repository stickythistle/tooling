#!/usr/bin/env python3
"""The lane wrapper refuses a compile under a login shell, and hermes-implement
exports it only when the manifest declares a builder.

Run directly: `python3 tests/test_lane_rustc.py`. The cargo case needs cargo.

The nightly lane test at the bottom runs pipeline/implement.sh against a
fake repo, origin, and `gh` so the verifier's re-author and refusal
paths are exercised at the script level."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "lib" / "lane-rustc"
PIPELINE_IMPLEMENT = REPO_ROOT / "pipeline" / "implement.sh"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hermes_implement_tmp import (  # noqa: E402
    HERMES_IMPLEMENT, git, make_worktree, write_stub_gh)

STUB_HERMES = """#!/usr/bin/env python3
import os, subprocess
print('RUSTC_WRAPPER=' + os.environ.get('RUSTC_WRAPPER', ''))
open(os.path.join(os.environ['TMPDIR'], 'HANDOVER.md'), 'w').write('stub\\n')
subprocess.check_call(['git', 'commit', '--quiet', '--allow-empty', '-m', 'stub'],
    env={**os.environ, 'GIT_AUTHOR_NAME': 's', 'GIT_AUTHOR_EMAIL': 's@s',
         'GIT_COMMITTER_NAME': 's', 'GIT_COMMITTER_EMAIL': 's@s'})
"""


def wrap(tmp: Path, *args: str) -> tuple[subprocess.CompletedProcess, bool]:
    """Run the wrapper over a stub rustc; report whether the stub ran."""
    marker = tmp / "ran"
    marker.unlink(missing_ok=True)
    rustc = tmp / "rustc"
    rustc.write_text(f"#!/bin/sh\ntouch {marker}\n")
    rustc.chmod(0o755)
    proc = subprocess.run([str(WRAPPER), str(rustc), *args],
                          capture_output=True, text=True)
    return proc, marker.exists()


def lane_wrapper(root: Path, role: str, stub: Path, gh_dir: Path) -> str:
    """Run hermes-implement on a project whose one task has `role`; return the
    RUSTC_WRAPPER its agent saw."""
    wt = make_worktree(root)
    (wt / "gates.toml").write_text(
        f'[[task]]\nname = "bench"\nrole = "{role}"\ncommand = "true"\n')
    git(wt, "add", "gates.toml")
    git(wt, "commit", "--quiet", "-m", "manifest")
    state = root / "state"
    state.mkdir()
    proc = subprocess.run(
        [str(HERMES_IMPLEMENT), "--issue", "1", "--worktree", str(wt)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "HERMES_PYTHON": str(stub),
             "AGENT_STATE_DIR": str(state),
             "PATH": f"{gh_dir}:{os.environ.get('PATH', '')}",
             "HERMES_IMPLEMENT_ATTEMPTS": "1",
             "HERMES_IMPLEMENT_TIMEOUT": "30"})
    out = proc.stdout.strip().splitlines()
    report = Path(out[-1]) / "attempt1.stdout" if out else None
    if not report or not report.exists():
        raise RuntimeError(f"no lane report: rc={proc.returncode} stderr={proc.stderr[-400:]!r}")
    for line in report.read_text().splitlines():
        if line.startswith("RUSTC_WRAPPER="):
            return line.split("=", 1)[1]
    return ""


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        proc, ran = wrap(tmp, "--crate-name", "t", "src/main.rs")
        if proc.returncode == 0 or ran:
            failures.append(f"a compile went through: rc={proc.returncode} ran={ran}")
        if "remote-task" not in proc.stderr:
            failures.append(f"the refusal does not name remote-task: {proc.stderr!r}")

        for args in (["-", "--crate-name", "___", "--print=cfg"], ["-vV"]):
            proc, ran = wrap(tmp, *args)
            if proc.returncode != 0 or not ran:
                failures.append(f"a probe was refused: {args} rc={proc.returncode}")

        if shutil.which("cargo"):
            crate = tmp / "crate"
            (crate / "src").mkdir(parents=True)
            (crate / "Cargo.toml").write_text(
                '[package]\nname = "t"\nversion = "0.1.0"\nedition = "2021"\n')
            (crate / "src" / "main.rs").write_text("fn main() {}\n")
            env = {**os.environ, "RUSTC_WRAPPER": str(WRAPPER)}
            for cmd, want_ok in (("cargo build", False),
                                 ("cargo metadata --format-version 1", True),
                                 ("cargo fmt --check", True)):
                proc = subprocess.run(["bash", "-l", "-c", cmd], cwd=crate, env=env,
                                      capture_output=True, text=True, timeout=300)
                if (proc.returncode == 0) != want_ok:
                    failures.append(f"`{cmd}` under bash -l: rc={proc.returncode} "
                                    f"stderr={proc.stderr[-400:]!r}")
            if list((crate / "target").glob("debug/t")):
                failures.append("cargo build produced a binary")
        else:
            print("SKIP cargo under a login shell: cargo not installed")

        stub = tmp / "hermes_stub.py"
        stub.write_text(STUB_HERMES)
        stub.chmod(0o755)
        gh_dir = tmp / "ghbin"
        gh_dir.mkdir()
        write_stub_gh(gh_dir)
        for name, role, want in (("builder", "builder", WRAPPER.resolve()),
                                 ("local", "cluster", None)):
            seen = lane_wrapper(tmp / name, role, stub, gh_dir)
            got = Path(seen).resolve() if seen else None
            if got != want:
                failures.append(f"a project whose task role is {role} gave the lane "
                                f"RUSTC_WRAPPER={seen!r}")

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        return 1
    print("PASS a project with a builder keeps lanes from compiling Rust locally")

    nightly_failures = test_nightly_pipeline_verifier_paths()
    for f in nightly_failures:
        print(f"FAIL {f}")
    if nightly_failures:
        return 1
    print("PASS pipeline/implement.sh honours its re-author and refusal paths")
    return 0


# ----------------------------------------------------------------------
# Nightly: pipeline/implement.sh honours the lane-identity verifier.
#
#   1. Default mode: a lane commits with `--author`, the verifier
#      re-authors it, the push carries the expected identity.
#   2. Refuse mode (LANE_GIT_IDENTITY=refuse): the verifier names the
#      offending commit, the script skips the push and the PR, and
#      the line on stdout names the refusal.
#   3. Real amend of main's tip on the fresh path: the script records
#      the lane base as `rev-parse HEAD` right after `worktree add`,
#      so a later amend that takes issue-1 through a different sha
#      than origin/main breaks the recorded-base ancestry check. The
#      verifier refuses (exit 2), the script skips the push and the
#      PR, and stderr names "history rewritten".
#
# Calling `verify_lane_identity` directly would already cover the
# verifier; the script's behaviour on each return is what the test
# checks. The fake `gh` answers the script's three network calls,
# the fake origin is a local bare repo the lane can push to without
# a network, and the worktree is a real `git worktree add` so `git
# rev-parse --git-dir` returns an absolute path the script can stat
# from any CWD.
# ----------------------------------------------------------------------


NIGHTLY_FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # `gh issue view N --json state --jq .state` -> OPEN so the
    # resume loop keeps the tree in play and the selector sees
    # the issue as open; `gh issue view --json labels --jq
    # '.labels[].name'` -> a label matching $VERIFIABLE so the
    # selector does not skip the issue; `gh pr create` -> a stub
    # URL the script prints as the draft PR.
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        case " $* " in
            *" --json state "*) printf '%s\n' '"OPEN"'; exit 0 ;;
            # `--jq '.labels[].name'` emits one label per line, so
            # the selector's grep can match a label against
            # $VERIFIABLE. The selector does not parse the JSON
            # wrapper itself, so the fake has to skip it.
            *" --json labels "*) printf '%s\n' area:rust; exit 0 ;;
        esac
        exit 0
    fi
    if [[ "$1" == "pr" && "$2" == "create" ]]; then
        printf '%s\\n' "https://example.invalid/pr/1"
        exit 0
    fi
    exit 0
""")


def _setup_nightly_repo_no_worktree(tmp: Path) -> tuple[Path, Path]:
    """Same as _setup_nightly_repo but without the worktree or
    nightly-owned marker: the script's fresh path creates the
    worktree itself and the resume scan sees no stranded work.
    Returns (repo, origin). The repo's user config is set to
    Wrong so the lane commit the fake implementer makes carries
    the bypass identity the verifier is supposed to catch."""
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)],
                   check=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main",
                    str(repo)], check=True)
    # Repo-level user config drives both repo and (later) the
    # worktree's commits: the fake implementer makes its lane commit
    # under `Wrong`, which is what the recorded-base check sees
    # through the ancestry refusal.
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                  "wrong@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name",
                  "Wrong"], check=True)
    # An empty root commit so a later amend of the seed produces a
    # commit that shares this root as parent (see _setup_nightly_repo).
    subprocess.run(["git", "-C", str(repo), "commit", "-q",
                    "--allow-empty", "-m", "root"], check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u",
                    "origin", "main"], check=True)
    return repo, origin


def _setup_nightly_queue(tmp: Path) -> None:
    """Write a single bounded triage file the script's selector
    picks up. The format matches what pipeline/implement.sh reads:
    `**Verdict:** still real` in the body, and `bounded` in the
    `## Verdict on scope` section. The label must match $VERIFIABLE
    (default area:rust|area:db|area:ci)."""
    queue = tmp / "home" / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "issue-1.md").write_text(
        "**Verdict:** still real\n"
        "\n"
        "## Verdict on scope\n"
        "\n"
        "bounded -- the script's lane-identity verifier can be\n"
        "exercised end-to-end without a real change. area:rust.\n"
        "\n"
        "## Candidate mechanisms\n"
        "\n"
        "Amend main's tip and rebase. The test setup uses this\n"
        "exactly.\n"
    )


def _write_fresh_lane_implementer(tmp: Path) -> Path:
    """Fake PIPELINE_IMPLEMENTER for case 3. The script invokes it
    with `--worktree <path>` after creating the worktree, so the
    recorded lane_base is already set to M0 (origin/main's tip).
    The implementer adds a lane commit (with the bypass identity
    the verifier is meant to catch), then amends main's tip in the
    repo and rebases issue-1 onto the amended tip. HEAD's lineage
    no longer carries the recorded base M0, so a fresh
    `merge-base origin/main HEAD` would return P (M0's parent)
    rather than M0. The recorded base is what catches this."""
    impl = tmp / "fakebin" / "fresh-implementer"
    impl.parent.mkdir(exist_ok=True)
    impl.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -e
        wt=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --worktree) wt="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        # The worktree is required, and an empty one is refused rather
        # than defaulted. `git -C ""` means the current directory, so a
        # fake run by hand without --worktree would commit into
        # whatever repository the shell happened to be in -- which, in a
        # lane's own worktree, is the lane's branch. The parameter
        # expansion below refuses with the variable's name rather than a
        # message someone has to keep in step with the code.
        #
        # The braces are doubled: this is an f-string, and a single
        # brace would be read as a Python substitution.
        : "${{wt:?the fake implementer needs --worktree; refusing to act on the current directory}}"
        # wt is like <tmp>/wt/issue-1; the script's parent <tmp>
        # holds repo/ next to wt/ and origin.git. The repo is the
        # primary checkout bound to `main`, so amending it rewrites
        # the same `main` ref the worktree sees.
        wt_root=$(dirname "$(dirname "$wt")")
        repo="$wt_root/repo"
        # Add a lane commit the verifier is supposed to notice. The
        # worktree inherits the repo's user config, which is set to
        # Wrong upstream so the commit carries the bypass identity.
        echo change > "$wt/agent_change.txt"
        git -C "$wt" add agent_change.txt
        git -C "$wt" commit -qm "lane work"
        # Amend main's tip in the repo, sharing refs with the
        # worktree. -m "seed (amended)" gives the amended commit a
        # new sha (M0') with the same parent P. --allow-empty keeps
        # the tree byte-identical to M0's: the verifier only cares
        # about ancestry here, and a tree-changing amend would
        # produce a different failure mode the test would have to
        # reason around.
        git -C "$repo" commit --quiet --amend \\
            -m "seed (amended)" --allow-empty
        # Rebase issue-1 onto the amended main so HEAD's lineage
        # goes through M0' rather than M0.
        git -C "$wt" rebase -q main
        """))
    impl.chmod(0o755)
    return impl


def _setup_nightly_repo(tmp: Path) -> tuple[Path, Path, Path]:
    """Stand up REPO (a clone of a bare origin), a real worktree on
    branch issue-1, and the nightly-owned marker. Returns (repo,
    origin, worktree). The worktree's lane commit carries the
    worktree's user.name/user.email (usually 'Wrong' /
    'wrong@wrong'), so the verifier has something to refuse or
    rewrite depending on the mode.

    The trunk is two commits deep: a root P and the seed M0. Case 3
    amends the seed into M0' that shares P as parent; without P,
    M0 and M0' would be disjoint roots and `merge-base origin/main
    HEAD` would return empty rather than P, taking the script down
    a different path before the verifier runs."""
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    wt = tmp / "wt" / "issue-1"

    subprocess.run(["git", "init", "-q", "--bare", str(origin)],
                   check=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main",
                    str(repo)], check=True)
    # The REPO clone is what the script reads from. Its user.email
    # matches what the upstream trunk was committed with; the
    # worktree's own user.email is what the lane commits carry.
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                  "real@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name",
                  "Real"], check=True)
    # An empty root commit so a later amend of the seed produces a
    # commit that shares this root as parent (see docstring).
    subprocess.run(["git", "-C", str(repo), "commit", "-q",
                    "--allow-empty", "-m", "root"], check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "seed.txt"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u",
                    "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q",
                    "-b", "issue-1", str(wt), "origin/main"],
                   check=True)
    # The lane commit's identity -- a real agent that bypassed the
    # pinned env vars (`--author`, `-c user.name=...`). The verifier
    # compares it against LANE_GIT_NAME / LANE_GIT_EMAIL.
    subprocess.run(["git", "-C", str(wt), "config", "user.email",
                  "wrong@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name",
                  "Wrong"], check=True)
    (wt / "change.txt").write_text("change\n")
    subprocess.run(["git", "-C", str(wt), "add", "change.txt"],
                   check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm",
                   "lane work"], check=True)
    # The marker that says this tree belongs to the nightly pipeline,
    # not to a person. The script's resume scan refuses any branch
    # without it.
    gitdir = subprocess.run(["git", "-C", str(wt),
                             "rev-parse", "--git-dir"],
                            check=True, capture_output=True,
                            text=True).stdout.strip()
    Path(gitdir, "nightly-owned").write_text("")
    return repo, origin, wt


def _nightly_env(tmp: Path, repo: Path, *, refuse: bool = False
                 ) -> dict[str, str]:
    """Env to run pipeline/implement.sh against the fake repo. AGENT
    is a no-op so a successful gate means the resume path never
    invokes the repair agent; the picker db is pointed at a path that
    does not exist so pick_lane.py uses its fallback without a
    network call."""
    fakebin = tmp / "fakebin"
    fakebin.mkdir(exist_ok=True)
    gh = fakebin / "gh"
    if not gh.exists():
        gh.write_text(NIGHTLY_FAKE_GH)
        gh.chmod(0o755)
    # The verify command. The resume path calls `$VERIFY $wt` and
    # only proceeds when it exits 0.
    verify = fakebin / "true"
    verify.write_text("#!/usr/bin/env bash\nexit 0\n")
    verify.chmod(0o755)
    # The agent the repair path would invoke; the script reads $AGENT.
    noop = fakebin / "noop"
    noop.write_text("#!/usr/bin/env bash\nexit 0\n")
    noop.chmod(0o755)

    home = tmp / "home"
    home.mkdir(exist_ok=True)
    state = home / "state"
    queue = home / "queue"
    state.mkdir(exist_ok=True)
    queue.mkdir(exist_ok=True)

    parent = {k: v for k, v in os.environ.items()
              if not (k.startswith("GIT_AUTHOR_")
                      or k.startswith("GIT_COMMITTER_")
                      or k.startswith("LANE_GIT_"))}
    env = {**parent,
           "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
           # The script sources ~/.config/agent-pipeline/env (its own
           # conf) and lib/common.sh sources ~/.config/agent-tools/env
           # (its conf). Point HOME at an empty dir so neither exists.
           "HOME": str(home),
           "PIPELINE_REPO": str(repo),
           "PIPELINE_GH_REPO": "owner/proj",
           "PIPELINE_WT_ROOT": str(tmp / "wt"),
           "PIPELINE_STATE_DIR": str(state),
           "PIPELINE_QUEUE": str(queue),
           "PIPELINE_VERIFY": str(verify),
           "PIPELINE_AGENT": str(noop),
           "PIPELINE_LOCK": str(tmp / "pipeline.lock"),
           # The verifier's expected identity. Refuse mode is set
           # per-case via LANE_GIT_IDENTITY.
           "LANE_GIT_NAME": "Expected",
           "LANE_GIT_EMAIL": "expected@example.invalid",
           "PIPELINE_RESUME_MAX": "1"}
    if refuse:
        env["LANE_GIT_IDENTITY"] = "refuse"
    return env


def _push_target(tmp: Path, origin: Path) -> str:
    r"""Make sure the worktree's origin remote points at the local
    bare repo, so the script's `git push -q -u origin issue-1`
    resolves to a real push (the script does not require a
    network). Returns the remote URL."""
    remote_url = subprocess.run(
        ["git", "-C", str(tmp / "wt" / "issue-1"), "remote", "get-url", "origin"],
        capture_output=True, text=True)
    if remote_url.returncode != 0 or not remote_url.stdout.strip():
        subprocess.run(["git", "-C", str(tmp / "wt" / "issue-1"),
                        "remote", "add", "origin", str(origin)],
                       check=True)
    return subprocess.run(
        ["git", "-C", str(tmp / "wt" / "issue-1"), "remote",
         "get-url", "origin"],
        check=True, capture_output=True, text=True).stdout.strip()


def test_nightly_pipeline_verifier_paths() -> list[str]:
    failures: list[str] = []

    # ----- Case 1: default mode, wrong author, push succeeds.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo, origin, wt = _setup_nightly_repo(tmp)
        remote_url = _push_target(tmp, origin)
        env = _nightly_env(tmp, repo)
        proc = subprocess.run(
            [str(PIPELINE_IMPLEMENT)], env=env,
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            failures.append(
                f"nightly case 1 (default mode, re-author): "
                f"rc={proc.returncode} stderr={proc.stderr[-400:]!r}")
        # Push happened: origin/issue-1 must exist and carry the
        # re-authored commit. The script logs the draft PR URL on
        # its own line; the verifier logs `re-authored:` to stderr.
        pushed = subprocess.run(
            ["git", "-C", str(origin), "rev-parse",
             "--verify", "refs/heads/issue-1"],
            capture_output=True, text=True)
        if pushed.returncode != 0:
            failures.append(
                f"nightly case 1: push did not happen "
                f"(origin has no issue-1): stdout={proc.stdout!r} "
                f"stderr={proc.stderr[-400:]!r}")
        else:
            log = subprocess.run(
                ["git", "-C", str(origin), "log",
                 "--format=%H %an <%ae> | %cn <%ce>", "-1",
                 "refs/heads/issue-1"],
                check=True, capture_output=True, text=True).stdout
            if "Expected <expected@example.invalid>" not in log:
                failures.append(
                    f"nightly case 1: pushed commit lacks the "
                    f"expected identity: {log!r}")
            if "Wrong" in log or "wrong@" in log:
                failures.append(
                    f"nightly case 1: pushed commit still carries "
                    f"the wrong identity: {log!r}")
        if "re-authored:" not in proc.stderr:
            failures.append(
                f"nightly case 1: verifier did not log a rewrite: "
                f"stderr={proc.stderr[-400:]!r}")
        # The script's PR-creation line is on stdout.
        if "draft https://example.invalid/pr/1" not in proc.stdout:
            failures.append(
                f"nightly case 1: stdout does not name the draft "
                f"PR (push ran but the line was lost): "
                f"stdout={proc.stdout!r}")

    # ----- Case 2: LANE_GIT_IDENTITY=refuse, push and PR skipped.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo, origin, wt = _setup_nightly_repo(tmp)
        _push_target(tmp, origin)
        env = _nightly_env(tmp, repo, refuse=True)
        proc = subprocess.run(
            [str(PIPELINE_IMPLEMENT)], env=env,
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            failures.append(
                f"nightly case 2 (refuse mode): "
                f"rc={proc.returncode} stderr={proc.stderr[-400:]!r}")
        # Push must not have happened: origin has no issue-1.
        pushed = subprocess.run(
            ["git", "-C", str(origin), "rev-parse",
             "--verify", "refs/heads/issue-1"],
            capture_output=True, text=True)
        if pushed.returncode == 0:
            failures.append(
                f"nightly case 2: branch was pushed despite the "
                f"refusal: stdout={proc.stdout!r}")
        # The script's "identity verifier refused; skipping push and PR"
        # line is on stderr. The assertion accepts both streams --
        # an operator searches for the refusal across both.
        if "identity verifier refused; skipping push and PR" not in (
                proc.stderr + "\n" + proc.stdout):
            failures.append(
                f"nightly case 2: neither stdout nor stderr names "
                f"the refusal: stdout={proc.stdout!r} "
                f"stderr={proc.stderr[-400:]!r}")
        # The verifier's refuse-mode message names the offending
        # commit. It is the operator's trail when reading stderr.
        if "refusing:" not in proc.stderr:
            failures.append(
                f"nightly case 2: verifier did not name the refusal: "
                f"stderr={proc.stderr[-400:]!r}")

    # Case 3: real amend of main's tip on the fresh path, recorded
    # base catches it. The script records lane_base as
    # `git rev-parse HEAD` immediately after `worktree add -b
    # issue-1 … origin/main`; lane_base is the seed M0. The lane
    # amends main's tip to M0' (same parent P) and rebases issue-1
    # onto the amended tip -- HEAD's lineage goes through M0' rather
    # than M0, so a fresh `merge-base origin/main HEAD` would return
    # P rather than M0. The recorded base M0 is no longer an
    # ancestor of HEAD, the verifier's ancestry refusal fires
    # (exit 2), and the script skips both the push and the PR.
    # Unlike cases 1 and 2, this case takes the fresh path: the
    # worktree is created by the script, the issue is in the queue,
    # and PIPELINE_IMPLEMENTER is a fake that does the amend +
    # rebase.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo, origin = _setup_nightly_repo_no_worktree(tmp)
        _setup_nightly_queue(tmp)
        implementer = _write_fresh_lane_implementer(tmp)
        env = _nightly_env(tmp, repo)
        env["PIPELINE_IMPLEMENTER"] = str(implementer)
        proc = subprocess.run(
            [str(PIPELINE_IMPLEMENT)], env=env,
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            failures.append(
                f"nightly case 3 (amended base): "
                f"rc={proc.returncode} stderr={proc.stderr[-400:]!r}")
        pushed = subprocess.run(
            ["git", "-C", str(origin), "rev-parse",
             "--verify", "refs/heads/issue-1"],
            capture_output=True, text=True)
        if pushed.returncode == 0:
            failures.append(
                f"nightly case 3: branch was pushed despite "
                f"the amended base: stdout={proc.stdout!r} "
                f"stderr={proc.stderr[-400:]!r}")
        if "identity verifier refused; skipping push and PR" not in (
                proc.stderr + "\n" + proc.stdout):
            failures.append(
                f"nightly case 3: neither stdout nor stderr names "
                f"the refusal: stdout={proc.stdout!r} "
                f"stderr={proc.stderr[-400:]!r}")
        # The verifier's exit-2 message names the rewritten
        # history. This is the line the round-3 review asked us to
        # pin case 3 on: "the line says history was rewritten".
        if "history rewritten" not in proc.stderr:
            failures.append(
                f"nightly case 3: verifier's history rewritten "
                f"message did not surface: "
                f"stderr={proc.stderr[-400:]!r}")

    return failures


if __name__ == "__main__":
    sys.exit(main())
