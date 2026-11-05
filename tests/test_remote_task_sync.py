#!/usr/bin/env python3
"""Pin that remote-task never benches a stale build (issue #168).

`rsync -a` preserves the source's mtimes. A file edited locally before
the last remote build -- or restored from git with an older mtime --
therefore lands on the builder with a timestamp older than the artifacts
in the warm `target/`, cargo's fingerprint check skips the rebuild, and
the bench reports on a stale build.

The fix drops `-t` from the archive flag and adds `--checksum`: only
files whose content differs are transferred, and the receiver gives them
the current time. A file whose content did not change is not
transferred, so it keeps its remote mtime and the warm cache stays warm.

Both cases below run the real rsync against a local directory standing in
for the builder, because a stubbed rsync cannot show the difference
between the two behaviours.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_TASK = REPO_ROOT / "bin" / "remote-task"
REPO_BIN = REPO_ROOT / "bin"

# The transport is faked, but the sync is not: the real rsync runs against
# a local directory so the archive flag's mtime behaviour is the thing
# under test. `host:path` is rewritten into the fake remote root, and
# the real rsync does the rest.
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

# The ssh fake answers the space check and the tree preparation, and
# swallows the build dispatch. Only the sync is real.
#
# Written as a plain string rather than a raw one so the shell's own
# quoting survives into the file: the `printf` branch strips the single
# quotes bash adds around the body, and a raw Python string would have
# mangled the backslashes that make those quotes literal.
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
  echo "fake ssh: unhandled verb: $verb" >&2
  cat > /dev/null
  ;;
esac
"""


def make_project(tmpdir: Path) -> Path:
    """A git-init'd project with a bench task, so repo_root works."""
    project = tmpdir / "proj"
    project.mkdir(parents=True)
    (project / "gates.toml").write_text(
        "[[task]]\n"
        'name = "bench"\n'
        'role = "builder"\n'
        'command = "cargo build --workspace"\n'
    )
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


def assert_pass(msg: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{msg}: {detail}")


# A fixed point well before the test runs, so a file carrying it is
# unambiguously older than anything the test itself created.
OLD_TIME = 1756700000
# The marker standing in for a build artifact in the warm target/. It is
# newer than the stale source, so cargo's fingerprint check would skip a
# rebuild if the source arrived with its old timestamp.
MARKER_TIME = OLD_TIME + 100000


def test_stale_mtime_edit_reaches_the_builder_with_a_fresh_mtime():
    """A source edited locally and then given an older mtime must arrive
    with a timestamp newer than the artifacts in the warm target/.

    With `rsync -a` the file lands with the source's own mtime, older
    than the marker, and cargo rebuilds nothing: the bench reports on the
    stale build.
    """
    if not Path("/usr/bin/rsync").exists():
        print("SKIP stale mtime edit: no /usr/bin/rsync")
        return
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = make_project(tmp)
        source = project / "src" / "main.rs"
        source.parent.mkdir(parents=True)
        source.write_text('fn main() { println!("old"); }\n')

        remote_root = tmp / "remote"
        dest = remote_root / "bench" / "proj"
        dest.mkdir(parents=True)
        # The warm target/ and its artifact, newer than the stale source.
        target = dest / "target"
        target.mkdir()
        marker = target / "marker"
        marker.write_text("artifact\n")
        os.utime(marker, (MARKER_TIME, MARKER_TIME))

        fakebin = make_fakebin(tmp)
        first = run_remote_task(project, fakebin, remote_root)
        assert_pass("first sync succeeds", first.returncode == 0,
                    f"stdout={first.stdout!r} stderr={first.stderr!r}")

        # The edit, then the stale timestamp: the shape a `git checkout`
        # or an editor that restores an mtime leaves behind.
        source.write_text('fn main() { println!("new"); }\n')
        os.utime(source, (OLD_TIME, OLD_TIME))

        second = run_remote_task(project, fakebin, remote_root)
        assert_pass("second sync succeeds", second.returncode == 0,
                    f"stdout={second.stdout!r} stderr={second.stderr!r}")

        remote_source = dest / "src" / "main.rs"
        assert_pass("the edit reached the builder",
                    remote_source.read_text() == 'fn main() { println!("new"); }\n',
                    remote_source.read_text())
        assert_pass(
            "the edited source is newer on the builder than the artifact "
            "it was built from",
            remote_source.stat().st_mtime > marker.stat().st_mtime,
            f"source={remote_source.stat().st_mtime} "
            f"marker={marker.stat().st_mtime} "
            "-- a source older than the artifact is what cargo's "
            "fingerprint check skips, and the bench then reports a stale build")


def test_unchanged_file_keeps_its_remote_mtime():
    """A file whose content did not change is not transferred.

    The remote keeps its own mtime, which is what keeps the warm cache
    warm: stamping every file with the current time on every run would
    make cargo rebuild the whole tree every time.

    The source carries an mtime of its own here, so the remote's mtime
    survives only if the file is not transferred at all. A file whose
    size and mtime already matched was skipped before this change too;
    what `--checksum` adds is that a file whose content is unchanged is
    skipped even when its mtime differs.
    """
    if not Path("/usr/bin/rsync").exists():
        print("SKIP unchanged file: no /usr/bin/rsync")
        return
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = make_project(tmp)
        source = project / "src" / "main.rs"
        source.parent.mkdir(parents=True)
        source.write_text('fn main() { println!("same"); }\n')

        remote_root = tmp / "remote"
        dest = remote_root / "bench" / "proj"
        dest.mkdir(parents=True)
        # A remote copy of the file, with a timestamp of its own.
        remote_source = dest / "src" / "main.rs"
        remote_source.parent.mkdir(parents=True)
        remote_source.write_text('fn main() { println!("same"); }\n')
        remote_time = MARKER_TIME + 5000
        os.utime(remote_source, (remote_time, remote_time))
        # The source carries a different timestamp of its own, so the
        # remote mtime can only survive if the file is not transferred at
        # all. A stamp that merely differs would not prove the point.
        os.utime(source, (OLD_TIME, OLD_TIME))

        fakebin = make_fakebin(tmp)
        run = run_remote_task(project, fakebin, remote_root)
        assert_pass("sync succeeds", run.returncode == 0,
                    f"stdout={run.stdout!r} stderr={run.stderr!r}")
        assert_pass(
            "the unchanged file keeps the remote mtime",
            remote_source.stat().st_mtime == remote_time,
            f"mtime={remote_source.stat().st_mtime} expected={remote_time} "
            "-- an unchanged file that was re-stamped would force a "
            "full rebuild on every run")


def main() -> int:
    tests = [
        test_stale_mtime_edit_reaches_the_builder_with_a_fresh_mtime,
        test_unchanged_file_keeps_its_remote_mtime,
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
