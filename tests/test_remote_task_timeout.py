#!/usr/bin/env python3
"""Pin that a caller's TASK_TIMEOUT wins over the manifest's (issue #281).

`lib/manifest.py` prints every field, with `TASK_TIMEOUT=''` when timeout is
absent. `bin/remote-task` evals that output, so the caller's value is
overwritten with an empty string, and the 3600 default takes over.

The fix saves the caller's value before the eval and applies precedence:
caller > manifest > 3600. `timeout = 0` must still reach `timeout(1)` as 0,
which coreutils reads as "no limit". Only an empty or unset value falls
through.

These tests run the real `bin/remote-task` with a fake `ssh` on PATH
(`make_project`, `make_fakebin`, `run_remote_task`, copied from
`test_remote_task_sync.py`). The fake ssh records the final `exec ssh ...
timeout -k 30 N ...` command line so the test can read N.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_TASK = REPO_ROOT / "bin" / "remote-task"
REPO_BIN = REPO_ROOT / "bin"

# Fake transport but real rsync: `host:path` is rewritten into the
# fake remote root so the real rsync operates locally.
REAL_RSYNC_STUB = """#!/bin/sh
set -e
args=""
for a in "$@"; do
  case "$a" in
    *:*) a="$FAKE_REMOTE_ROOT/${a#*:}" ;;
  esac
  args="$args $a"
done
exec /usr/bin/rsync $args
"""

# Fake ssh that answers the space check, prepares the tree, and records
# the timeout value from the build dispatch command line.
FAKE_SSH = """#!/bin/bash
root="${FAKE_REMOTE_ROOT:?must be set}"
cmdline="$2"
if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
  script=$(cat)
  case "$script" in
    *bench_fs*)
      printf 'OK %s\\n' "${FAKE_FREE_GB:-100}"
      exit 0
      ;;
  esac
fi
verb="${cmdline%% *}"
case "$verb" in
mkdir)
  d=""
  for a in $cmdline; do
    case "$a" in
      mkdir) ;;
      -*) ;;
      *) d="$a"; break ;;
    esac
  done
  [ -n "$d" ] || { echo "fake ssh: mkdir needs a path" >&2; exit 1; }
  mkdir -p "$root/$d"
  ;;
cat)
  f=""
  for a in $cmdline; do
    case "$a" in
      cat) ;;
      2*) ;;
      ">"*) ;;
      *) f="$a"; break ;;
    esac
  done
  cat "$root/$f" 2>/dev/null || true
  ;;
touch)
  eval "set -- $cmdline"
  shift
  for a in "$@"; do
    mkdir -p "$root/$(dirname "$a")"
    touch "$root/$a"
  done
  ;;
printf)
  body=""
  target=""
  state=verb
  for a in $cmdline; do
    case "$state:$a" in
      verb:printf) state=format ;;
      format:'%s\\n') state=body ;;
      format:*) state=body; body="$a" ;;
      body:'>') state=target ;;
      body:*) body="${a#\\'}"; body="${body%\\'}" ;;
      target:*) target="$a" ;;
    esac
  done
  [ -n "$target" ] || { echo "fake ssh: printf needs a target" >&2; exit 1; }
  mkdir -p "$root/$(dirname "$target")"
  printf '%s\\n' "$body" > "$root/$target"
  ;;
flock)
  cat > /dev/null
  ;;
docker)
  ;;
*)
  # The dispatch command line looks like:
  #   fakehost CALLER=\$PPID flock -w 7200 -E 75 \$HOME/bench/.lock timeout -k 30 N bash ...
  # Extract N from `timeout -k 30 N` and record it.
  if echo "$cmdline" | grep -q 'timeout -k 30'; then
      # Split on spaces and find the number after "-k 30"
      set -- $cmdline
      while [ $# -gt 0 ]; do
        if [ "$1" = "timeout" ] && [ "$2" = "-k" ] && [ "$3" = "30" ] && [ $# -ge 4 ]; then
            echo "$4" > "$root/.timeout-recorded"
            break
        fi
        shift
      done
  fi
  cat > /dev/null
  ;;
esac
"""


def make_project(tmpdir: Path, timeout_field: str = "") -> Path:
    """A git-init'd project with a bench task, so repo_root works.

    `timeout_field` is an extra line inserted into the [[task]] block,
    e.g. `timeout = 60` or `timeout = 0`. Omit for no timeout field.
    """
    project = tmpdir / "proj"
    project.mkdir(parents=True)
    task_block = (
        "[[task]]\n"
        'name = "bench"\n'
        'role = "builder"\n'
        'command = "cargo build --workspace"\n'
    )
    if timeout_field:
        task_block += f"{timeout_field}\n"
    (project / "gates.toml").write_text(task_block)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=project, check=True,
                       capture_output=True)
    return project.resolve()


def make_fakebin(tmpdir: Path) -> Path:
    """A directory of stand-ins for the transport: a real rsync, a fake ssh."""
    fake = tmpdir / "fakebin"
    fake.mkdir()
    for name, body in (("ssh", FAKE_SSH), ("rsync", REAL_RSYNC_STUB)):
        path = fake / name
        path.write_text(body)
        path.chmod(0o755)
    return fake


def run_remote_task(project: Path, fakebin: Path,
                     remote_root: Path) -> subprocess.CompletedProcess:
    """Run the real remote-task against the fake project, hermetically."""
    env = {
        "PATH": os.pathsep.join([str(fakebin), str(REPO_BIN), "/usr/bin", "/bin"]),
        "HOST_BUILDER": "fakehost",
        "TOOLS_ENV": str(project / "tools.env"),
        "REMOTE_TASK_LOCK": str(project / "build.lock"),
        "HOME": str(project),
        "FAKE_REMOTE_ROOT": str(remote_root),
    }
    (project / "tools.env").write_text("")
    return subprocess.run([str(REMOTE_TASK), str(project)],
                          capture_output=True, text=True, env=env)


def extract_timeout(remote_root: Path) -> str:
    """Read the recorded timeout value from the fake ssh's log."""
    record = remote_root / ".timeout-recorded"
    if not record.exists():
        return ""
    content = record.read_text().strip()
    # The file may contain just the number (e.g. "7200") or the full
    # command line (e.g. "timeout -k 30 7200 bash ...").
    # If it's a plain number, return it directly.
    if content.isdigit():
        return content
    # Otherwise extract N from `timeout -k 30 N`.
    parts = content.split()
    for i, p in enumerate(parts):
        if p == "-k" and i + 2 < len(parts):
            return parts[i + 2]
    return ""


def assert_pass(msg: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{msg}: {detail}")


def test_caller_7200_no_manifest_timeout():
    """caller 7200 with no manifest timeout → 7200.

    Without the fix, eval wipes the caller's 7200 and the 3600 default
    takes over.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = make_project(tmp)
        remote_root = tmp / "remote"
        remote_root.mkdir()
        fakebin = make_fakebin(tmp)
        env_override = {"TASK_TIMEOUT": "7200"}

        env = {
            "PATH": os.pathsep.join([str(fakebin), str(REPO_BIN), "/usr/bin", "/bin"]),
            "HOST_BUILDER": "fakehost",
            "TOOLS_ENV": str(project / "tools.env"),
            "REMOTE_TASK_LOCK": str(project / "build.lock"),
            "HOME": str(project),
            "FAKE_REMOTE_ROOT": str(remote_root),
            "TASK_TIMEOUT": "7200",
        }
        (project / "tools.env").write_text("")
        result = subprocess.run(
            [str(REMOTE_TASK), str(project)],
            capture_output=True, text=True, env=env,
        )
        assert_pass("remote-task exits 0", result.returncode == 0,
                     f"stdout={result.stdout!r} stderr={result.stderr!r}")
        recorded = extract_timeout(remote_root)
        assert_pass("timeout is 7200", recorded == "7200",
                     f"recorded={recorded!r}")


def test_caller_7200_manifest_60():
    """caller 7200 with manifest 60 → 7200.

    The manifest says 60 but the caller's explicit env wins.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = make_project(tmp, 'timeout = 60')
        remote_root = tmp / "remote"
        remote_root.mkdir()
        fakebin = make_fakebin(tmp)

        env = {
            "PATH": os.pathsep.join([str(fakebin), str(REPO_BIN), "/usr/bin", "/bin"]),
            "HOST_BUILDER": "fakehost",
            "TOOLS_ENV": str(project / "tools.env"),
            "REMOTE_TASK_LOCK": str(project / "build.lock"),
            "HOME": str(project),
            "FAKE_REMOTE_ROOT": str(remote_root),
            "TASK_TIMEOUT": "7200",
        }
        (project / "tools.env").write_text("")
        result = subprocess.run(
            [str(REMOTE_TASK), str(project)],
            capture_output=True, text=True, env=env,
        )
        assert_pass("remote-task exits 0", result.returncode == 0,
                     f"stdout={result.stdout!r} stderr={result.stderr!r}")
        recorded = extract_timeout(remote_root)
        assert_pass("timeout is 7200 (caller wins)", recorded == "7200",
                     f"recorded={recorded!r}")


def test_no_caller_manifest_60():
    """caller unset with manifest 60 → 60.

    No caller override, so the manifest's value is used.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = make_project(tmp, 'timeout = 60')
        remote_root = tmp / "remote"
        remote_root.mkdir()
        fakebin = make_fakebin(tmp)

        env = {
            "PATH": os.pathsep.join([str(fakebin), str(REPO_BIN), "/usr/bin", "/bin"]),
            "HOST_BUILDER": "fakehost",
            "TOOLS_ENV": str(project / "tools.env"),
            "REMOTE_TASK_LOCK": str(project / "build.lock"),
            "HOME": str(project),
            "FAKE_REMOTE_ROOT": str(remote_root),
        }
        # Explicitly unset TASK_TIMEOUT
        env.pop("TASK_TIMEOUT", None)
        (project / "tools.env").write_text("")
        result = subprocess.run(
            [str(REMOTE_TASK), str(project)],
            capture_output=True, text=True, env=env,
        )
        assert_pass("remote-task exits 0", result.returncode == 0,
                     f"stdout={result.stdout!r} stderr={result.stderr!r}")
        recorded = extract_timeout(remote_root)
        assert_pass("timeout is 60 (manifest wins)", recorded == "60",
                     f"recorded={recorded!r}")


def test_no_caller_no_manifest():
    """caller unset with no manifest timeout → 3600.

    Neither caller nor manifest set a value, so the 3600 default applies.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = make_project(tmp)
        remote_root = tmp / "remote"
        remote_root.mkdir()
        fakebin = make_fakebin(tmp)

        env = {
            "PATH": os.pathsep.join([str(fakebin), str(REPO_BIN), "/usr/bin", "/bin"]),
            "HOST_BUILDER": "fakehost",
            "TOOLS_ENV": str(project / "tools.env"),
            "REMOTE_TASK_LOCK": str(project / "build.lock"),
            "HOME": str(project),
            "FAKE_REMOTE_ROOT": str(remote_root),
        }
        env.pop("TASK_TIMEOUT", None)
        (project / "tools.env").write_text("")
        result = subprocess.run(
            [str(REMOTE_TASK), str(project)],
            capture_output=True, text=True, env=env,
        )
        assert_pass("remote-task exits 0", result.returncode == 0,
                     f"stdout={result.stdout!r} stderr={result.stderr!r}")
        recorded = extract_timeout(remote_root)
        assert_pass("timeout is 3600 (default)", recorded == "3600",
                     f"recorded={recorded!r}")


def test_no_caller_manifest_0():
    """caller unset with manifest 0 → 0.

    `timeout = 0` must reach `timeout(1)` as 0, which coreutils reads
    as "no limit". Only an empty or unset value falls through to 3600.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = make_project(tmp, 'timeout = 0')
        remote_root = tmp / "remote"
        remote_root.mkdir()
        fakebin = make_fakebin(tmp)

        env = {
            "PATH": os.pathsep.join([str(fakebin), str(REPO_BIN), "/usr/bin", "/bin"]),
            "HOST_BUILDER": "fakehost",
            "TOOLS_ENV": str(project / "tools.env"),
            "REMOTE_TASK_LOCK": str(project / "build.lock"),
            "HOME": str(project),
            "FAKE_REMOTE_ROOT": str(remote_root),
        }
        env.pop("TASK_TIMEOUT", None)
        (project / "tools.env").write_text("")
        result = subprocess.run(
            [str(REMOTE_TASK), str(project)],
            capture_output=True, text=True, env=env,
        )
        assert_pass("remote-task exits 0", result.returncode == 0,
                     f"stdout={result.stdout!r} stderr={result.stderr!r}")
        recorded = extract_timeout(remote_root)
        assert_pass("timeout is 0 (no limit)", recorded == "0",
                     f"recorded={recorded!r}")


def main() -> int:
    tests = [
        test_caller_7200_no_manifest_timeout,
        test_caller_7200_manifest_60,
        test_no_caller_manifest_60,
        test_no_caller_no_manifest,
        test_no_caller_manifest_0,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
        except Exception as e:
            failures.append(f"{t.__name__}: {type(e).__name__}: {e}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f" - {f}")
        return 1
    print(f"\nPASS: all {len(tests)} tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
