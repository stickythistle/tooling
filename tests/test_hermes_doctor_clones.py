#!/usr/bin/env python3
"""Pin `hermes-doctor clones`: every clone a lane pushes from runs push-guard,
and the tools on PATH all resolve into one deployed tooling checkout.

Everything is scratch: clones, lanes.toml (via LANE_CONFIG) and a HOME holding
the two bin directories. No network, and the operator's own config is never read.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "bin" / "hermes-doctor"

# What says "this tooling repository" to the doctor: the origin the checkout it
# runs out of is configured with, or that checkout itself where it has none.
# The configured value because `remote get-url` answers through url.*.insteadOf,
# which would make this fixture depend on whose global config the host has.
ORIGIN = subprocess.run(["git", "-C", str(REPO_ROOT), "config", "--get", "remote.origin.url"],
                        capture_output=True, text=True).stdout.strip()
REFERENCE = ORIGIN or str(REPO_ROOT)

IDENTITY = {"GIT_AUTHOR_NAME": "doctor-test", "GIT_AUTHOR_EMAIL": "doctor-test@example.com",
            "GIT_COMMITTER_NAME": "doctor-test", "GIT_COMMITTER_EMAIL": "doctor-test@example.com"}


def git(cwd, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, **IDENTITY)
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True, env=env)


def run_doctor(home, config):
    """`hermes-doctor clones` with HOME, LANE_CONFIG and nothing else inherited."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANE_CONFIG": str(config)}
    return subprocess.run([str(DOCTOR), "clones"], cwd=str(home),
                          capture_output=True, text=True, env=env)


def checkout(path, origin=None):
    """A committed git checkout, optionally a copy of the tooling repository."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "doctor-test")
    git(path, "config", "user.email", "doctor-test@example.com")
    if origin:
        git(path, "remote", "add", "origin", origin)
    (path / "base.txt").write_text("base\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def install_hook(clone, body):
    """A hooks directory whose pre-push runs *body*. The doctor reads the hook's
    text, so *body* is what each case claims the hook does."""
    hooks = clone.parent / f"{clone.name}-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\n{body}\n")
    hook.chmod(0o755)
    git(clone, "config", "core.hooksPath", str(hooks))
    return hooks


def lanes_toml(path, clones):
    """The settings the doctor reads.

    They carry the keeper's address and the token file: the doctor
    reads the settings the lane reads, and the lane cannot act without
    the keeper.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    token = path.parent / "lane-token"
    token.write_text("a-token")
    token.chmod(0o600)
    lines = ['tmux_session = "0"', ""]
    for name, clone in clones:
        lines += [f'[repos."{name}"]', f'clone = "{clone}"', ""]
    lines += ["[keeper]", 'url = "http://127.0.0.1:8787"',
              f'token_file = "{token}"', ""]
    path.write_text("\n".join(lines))
    return path


def link(bindir, name, target):
    bindir.mkdir(parents=True, exist_ok=True)
    link_path = bindir / name
    link_path.symlink_to(target)
    return link_path


def deployed(root, name):
    """A checkout of the tooling repository with tools in its bin/, as the
    deployed checkout is laid out."""
    live = checkout(root / name, origin=REFERENCE)
    (live / "bin").mkdir()
    for tool in ("tool-one", "tool-two"):
        (live / "bin" / tool).write_text("#!/bin/sh\n")
    git(live, "add", "-A")
    git(live, "commit", "-q", "-m", "tools")
    return live


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_rc(label, want, r, failures):
    if r.returncode != want:
        fail(failures, label, f"got {r.returncode}, want {want}; "
                              f"stdout={r.stdout!r} stderr={r.stderr!r}")


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_status(label, status, subject, out, failures):
    """The line for *subject* carries *status*."""
    for line in out.splitlines():
        if subject in line:
            if f"[{status:4}]" not in line:
                fail(failures, label, f"{subject!r} not {status}: {line!r}")
            return
    fail(failures, label, f"no line naming {subject!r} in {out!r}")


# ----- cases ---------------------------------------------------------------

def case_hooked_clones_and_one_checkout(failures):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        home = t / "home"
        a = checkout(t / "clones" / "a")
        b = checkout(t / "clones" / "b")
        hooks_a = install_hook(a, 'exec "$(dirname "$0")/push-guard" "$@"')
        hooks_b = install_hook(b, 'exec "$HOME/tooling/hooks/push-guard" "$@"')
        live = deployed(t, "hermes-tools-live")
        head = git(live, "rev-parse", "--short", "HEAD").stdout.strip()
        link(home / "bin", "tool-one", live / "bin" / "tool-one")
        cfg = lanes_toml(t / "cfg" / "lanes.toml", [("org/a", a), ("org/b", b)])

        r = run_doctor(home, cfg)

        assert_rc("exit code", 0, r, failures)
        assert_status("clone a ok", "ok", str(a), r.stdout, failures)
        assert_status("clone b ok", "ok", str(b), r.stdout, failures)
        assert_in("hook directory named", str(hooks_a), r.stdout, failures)
        assert_in("hook directory named", str(hooks_b), r.stdout, failures)
        assert_status("one checkout ok", "ok", str(live), r.stdout, failures)
        assert_in("its HEAD is named", head, r.stdout, failures)


def case_clone_without_hooks_path(failures):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        home = t / "home"
        a = checkout(t / "clones" / "a")
        b = checkout(t / "clones" / "b")
        install_hook(b, 'exec push-guard "$@"')
        cfg = lanes_toml(t / "cfg" / "lanes.toml", [("org/a", a), ("org/b", b)])

        r = run_doctor(home, cfg)

        assert_rc("exit code", 1, r, failures)
        assert_status("unhooked clone fails", "FAIL", str(a), r.stdout, failures)
        assert_in("the fault is named", "core.hooksPath is unset", r.stdout, failures)
        assert_status("hooked clone ok", "ok", str(b), r.stdout, failures)


def case_hook_runs_something_else(failures):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        home = t / "home"
        a = checkout(t / "clones" / "a")
        hooks_a = install_hook(a, "exec git status --porcelain")

        r = run_doctor(home, lanes_toml(t / "cfg" / "lanes.toml", [("org/a", a)]))

        assert_rc("exit code", 1, r, failures)
        assert_status("clone fails", "FAIL", str(a), r.stdout, failures)
        assert_in("the hook is named", str(hooks_a / "pre-push"), r.stdout, failures)
        assert_in("the fault is named", "does not run push-guard", r.stdout, failures)


def case_hook_not_executable(failures):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        home = t / "home"
        a = checkout(t / "clones" / "a")
        hooks_a = install_hook(a, 'exec push-guard "$@"')
        (hooks_a / "pre-push").chmod(0o644)

        r = run_doctor(home, lanes_toml(t / "cfg" / "lanes.toml", [("org/a", a)]))

        assert_rc("exit code", 1, r, failures)
        assert_status("clone fails", "FAIL", str(a), r.stdout, failures)
        assert_in("the hook is named", str(hooks_a / "pre-push"), r.stdout, failures)
        assert_in("the fault is named", "is not executable", r.stdout, failures)


def case_clone_directory_is_missing(failures):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        home = t / "home"
        gone = t / "clones" / "gone"

        r = run_doctor(home, lanes_toml(t / "cfg" / "lanes.toml", [("org/gone", gone)]))

        assert_rc("exit code", 1, r, failures)
        assert_status("absent clone fails", "FAIL", str(gone), r.stdout, failures)
        assert_in("the fault is named", "clone directory is missing", r.stdout, failures)


def case_links_span_two_checkouts(failures):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        home = t / "home"
        a = checkout(t / "clones" / "a")
        install_hook(a, 'exec push-guard "$@"')
        old = deployed(t, "tooling-old")
        new = deployed(t, "tooling-new")
        link(home / "bin", "tool-one", new / "bin" / "tool-one")
        link(home / ".local" / "bin", "tool-two", old / "bin" / "tool-two")

        r = run_doctor(home, lanes_toml(t / "cfg" / "lanes.toml", [("org/a", a)]))

        assert_rc("exit code", 1, r, failures)
        assert_in("first checkout named", str(new), r.stdout, failures)
        assert_in("second checkout named", str(old), r.stdout, failures)
        assert_in("a link into the first is named",
                  str(home / "bin" / "tool-one"), r.stdout, failures)
        assert_in("a link into the second is named",
                  str(home / ".local" / "bin" / "tool-two"), r.stdout, failures)


def case_missing_lanes_toml(failures):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        home = t / "home"
        r = run_doctor(home, t / "cfg" / "lanes.toml")

        assert_rc("exit code", 2, r, failures)
        assert_in("the path is named", str(t / "cfg" / "lanes.toml"),
                  r.stdout + r.stderr, failures)


CASES = (case_hooked_clones_and_one_checkout,
         case_clone_without_hooks_path,
         case_hook_runs_something_else,
         case_hook_not_executable,
         case_clone_directory_is_missing,
         case_links_span_two_checkouts,
         case_missing_lanes_toml)


def main():
    if not DOCTOR.is_file():
        return 1
    failures: list = []
    for case in CASES:
        case(failures)
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS hermes-doctor clones: a lane clone whose core.hooksPath runs "
          "push-guard is ok; one without the setting, with a hook that runs "
          "something else, or with a hook that is not executable, or whose "
          "directory is gone, fails by name; "
          "links into one deployed checkout name it and its HEAD, links into "
          "two name both; a missing lanes.toml is exit 2 naming the path "
          "(issue #165)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
