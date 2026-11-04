#!/usr/bin/env python3
"""Pin the builder's disk guard.

A builder that fills up mid-build does not fail as a build failure. It
fails as a linker bus error, an rsync ENOSPC, or a failed .rmeta write,
and it takes the database on the same disk down with it. Twice a full
disk took a builder's CI Postgres down.

Three things are pinned here:

- **The lane loop.** `remote-prune --all-lanes` measures every lane in
  BUILDER_LANES and reports each one separately. A prune that measured
  only the default builder left the other lanes filling up.
- **The `_prove` pairing.** A `_prove` directory is judged by the tree
  it belongs to, so it is pruned when that tree lands. They are the same
  branch's build cache twice over, and the `_prove` copy is the larger.
- **The threshold.** A builder below the floor is either given the space
  back or refused with the disk-low code -- and never fails part way
  through a compile.

The fake ssh answers the check's protocol rather than running the script:
running it would measure the test machine's real disk.

Run directly: `python3 tests/test_builder_space.py`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILDER_SPACE = REPO_ROOT / "bin" / "builder-space"
REMOTE_PRUNE = REPO_ROOT / "bin" / "remote-prune"
REMOTE_TASK = REPO_ROOT / "bin" / "remote-task"


def git(cwd: Path, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_fake_ssh(tmp: Path, *, free_gb: str = "100",
                  hosts: tuple = ("fakehost",),
                  bench: tuple = (),
                  check_rc: int = 0) -> Path:
    """A fake ssh answering the space check and the bench listing.

    `free_gb` is what the fake builder reports as free. `hosts` names the
    hosts it answers for: the space check is asked about one host, and a
    lane loop asks about several, so the answer has to name the lane it
    is for. `bench` is the directory listing the bench returns.
    """
    fake = tmp / "fakebin"
    fake.mkdir(exist_ok=True)
    host_list = " ".join(hosts)
    bench_block = "\n".join(f'echo "{b}"' for b in bench)
    # The fake RUNS the measurement script, with `df` and `stat` stubbed.
    #
    # The earlier version answered with a verdict of its own, which meant
    # the decision under test was the fake's rather than the script's: a
    # mutation to the script's threshold arithmetic changed nothing,
    # because the fake never asked the script. Running the script with
    # df stubbed is the only way the threshold is exercised.
    #
    # The stub lives in a directory ahead of PATH on the remote, so the
    # script's `df` and `stat` calls find it.
    stub = fake / "stubbin"
    stub.mkdir(exist_ok=True)
    free_bytes = int(float(free_gb) * 1048576)
    (stub / "df").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # `df -P <path>`: the POSIX one-line format. The free space is
        # the fake builder's.
        for a in "$@"; do
          case "$a" in
            -P) ;;
            *) path="$a" ;;
          esac
        done
        echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
        echo "/dev/fake 100000000 50000000 {free_bytes} 50% $path"
        """))
    (stub / "df").chmod(0o755)
    (stub / "stat").write_text(textwrap.dedent("""\
        #!/bin/bash
        # `stat -c %Y <path>`: the mtime of the tree's target dir. The
        # fake's trees are old, so they qualify as idle.
        for a in "$@"; do
          case "$a" in
            -*) ;;
            *) last="$a" ;;
          esac
        done
        echo 1000000
        """))
    (stub / "stat").chmod(0o755)
    script = f"""#!/bin/bash
host="$1"; shift
cmdline="$1"
if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
  script=$(cat)
  case "$script" in
    *bench_fs*)
      # The script runs, with df and stat stubbed. The bench directory
      # has to exist for df to answer about it.
      #
      # The values the script reads come from the command line's export
      # prefix, exactly as they do on a real host. The fake parses them
      # out of the cmdline and puts them in the script's environment,
      # because a script that read them from anywhere else would be
      # testing a different script.
      mkdir -p "$HOME/bench"
      env_args=""
      for a in $cmdline; do
        case "$a" in
          BUILDER_SPACE_*) env_args="$env_args $a" ;;
        esac
      done
      PATH="{stub}:$PATH" HOME="$HOME" env $env_args bash -c "$script"
      exit $?
      ;;
  esac
fi
case "$cmdline" in
  *"ls -1 "*)
    {bench_block}
    exit 0
    ;;
  *"test -d "*)
    # The landing path asks whether a tree exists. Answer yes for the
    # trees the fake bench holds.
    for d in {host_list}; do :; done
    exit 0
    ;;
esac
exit 0
"""
    p = fake / "ssh"
    p.write_text(script)
    p.chmod(0o755)
    return fake


def run_space(fake: Path, tmp: Path, *args: str,
              env_extra: dict | None = None):
    env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "HOME": str(tmp),
        "TOOLS_ENV": str(tmp / "no-env"),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run([str(BUILDER_SPACE), *args],
                          capture_output=True, text=True, env=env, timeout=60)


# ---------------------------------------------------------------------------
# The threshold path.

def test_a_builder_with_room_passes_silently():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fake = make_fake_ssh(tmp, free_gb="120")
        r = run_space(fake, tmp, "--host", "fakehost", "--tree", "proj")
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr!r}"
        # Silent on success: the check is infrastructure, and a project
        # that never heard of it sees nothing new.
        assert r.stdout == "", r.stdout
        assert r.stderr == "", r.stderr


def test_a_builder_below_the_floor_is_refused_with_the_disk_low_code():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fake = make_fake_ssh(tmp, free_gb="3")
        r = run_space(fake, tmp, "--host", "fakehost", "--tree", "proj",
                      "--min-free-gb", "20")
        # The disk-low code: distinct from a test failure, so a gate and
        # a lane report it as infrastructure.
        assert r.returncode == 4, (
            f"expected the disk-low code 4, got {r.returncode}: "
            f"{r.stdout!r} {r.stderr!r}")
        assert "disk" in r.stderr.lower(), r.stderr
        # The refusal names the builder and the space it had.
        assert "fakehost" in r.stderr, r.stderr


def test_a_builder_that_cannot_be_measured_is_not_a_pass():
    # Unprovable is not a pass. Exit 3 is this repo's code for "gathered
    # no evidence either way".
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text("#!/bin/sh\nexit 0\n")
        (fake / "ssh").chmod(0o755)
        r = run_space(fake, tmp, "--host", "fakehost", "--tree", "proj")
        assert r.returncode != 0, (
            f"an unmeasurable builder passed: {r.stdout!r} {r.stderr!r}")
        assert r.returncode == 3, r.returncode


def test_the_check_counts_both_trees():
    # A cold gate for one branch is two trees of 35 GB. A check that
    # counted one of them would pass a build that was going to fill the
    # disk.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fake = make_fake_ssh(tmp, free_gb="60")
        r = run_space(fake, tmp, "--host", "fakehost", "--tree", "proj")
        # 60 G free, and the check needs 70 (two trees of 35). It must
        # refuse even though 60 is more than the 20 G floor on its own.
        assert r.returncode == 4, (
            f"a builder with 60G free passed a build that needs 70G: "
            f"{r.stdout!r} {r.stderr!r}")


def test_a_low_disk_check_leaves_a_tree_that_holds_the_bench_lock():
    # The acceptance criterion: a low-space check must not remove another
    # branch's tree while that branch is holding the bench lock.
    #
    # The builder cannot tell a live tree from an abandoned one -- the
    # worktrees live on the hosts that dispatch builds, not on the
    # builder -- so the check asks `remote-prune` instead, which judges
    # trees against the caller's worktrees and takes the bench lock a
    # build holds. A tree in flight is therefore never a candidate.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        # The builder's bench, with two trees on it. `wt_locked` is held
        # under the bench lock by a build in flight.
        bench = tmp / "bench"
        (bench / "wt_locked").mkdir(parents=True)
        (bench / "wt_free").mkdir(parents=True)
        fake = make_fake_ssh(tmp, free_gb="3", bench=("wt_locked", "wt_free"))
        # The removal inside `flock` refuses the tree a build in flight
        # holds. This is the bench lock doing its job.
        (fake / "flock").write_text(textwrap.dedent("""\
            #!/bin/bash
            # `flock <lock> <cmd...>`: the lock is held by a build in
            # flight for `wt_locked`, so removing it is refused.
            cat > /dev/null
            for a in "$@"; do
              case "$a" in
                *wt_locked*) echo "wt_locked is held" >&2; exit 1 ;;
              esac
            done
            exit 0
        """))
        (fake / "flock").chmod(0o755)
        r = run_space(fake, tmp, "--host", "fakehost", "--tree", "proj",
                      env_extra={
            "HOST_BUILDER": "fakehost",
            "REMOTE_PRUNE_IDLE_DAYS": "0",
        })
        # The locked tree survived: the check freed nothing it had no
        # right to free.
        assert (bench / "wt_locked").is_dir(), (
            "a low-disk check removed a tree held under the bench lock")
        # And the build was refused rather than started, because the
        # space did not come back.
        assert r.returncode == 4, (
            f"expected the disk-low code 4, got {r.returncode}: "
            f"{r.stdout!r} {r.stderr!r}")


def test_a_tree_name_this_tooling_could_not_produce_is_refused():
    # The tree name goes into a remote command that removes directories.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fake = make_fake_ssh(tmp)
        r = run_space(fake, tmp, "--host", "fakehost",
                      "--tree", "evil; touch /tmp/pwned")
        assert r.returncode == 2, f"exit {r.returncode}: {r.stderr!r}"
        assert "refusing" in r.stderr, r.stderr


def test_the_health_check_runs_and_its_failure_is_the_disk_low_code():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fake = tmp / "fakebin"
        fake.mkdir()
        # A health check that fails: the database is down.
        (fake / "ssh").write_text(textwrap.dedent("""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
              script=$(cat)
              case "$script" in
                *bench_fs*) printf 'OK 100\\n'; exit 0 ;;
              esac
            fi
            if [[ "$cmdline" == *pg_isready* ]]; then
              exit 1
            fi
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        r = run_space(fake, tmp, "--host", "fakehost", "--tree", "proj",
                      "--check", "pg_isready")
        assert r.returncode == 4, (
            f"a failed health check did not report the disk-low code: "
            f"exit {r.returncode} {r.stderr!r}")
        assert "health check" in r.stderr, r.stderr


# ---------------------------------------------------------------------------
# The lane loop.

def setup_project(tmp: Path) -> Path:
    """A project with a bare origin and two worktrees."""
    project = tmp / "proj"
    project.mkdir()
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", "--quiet",
                    "--initial-branch=main", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main",
                    str(project)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(project, "config", k, v)
    (project / "gates.toml").write_text(
        '[[task]]\nname = "bench"\nrole = "builder"\ncommand = "echo bench"\n')
    git(project, "remote", "add", "origin", str(origin))
    (project / "README").write_text("seed\n")
    git(project, "add", "README")
    git(project, "commit", "--quiet", "-m", "seed")
    git(project, "push", "--quiet", "origin", "main")
    return project


def run_prune(project: Path, fake: Path, tmp: Path, *args: str,
              env_extra: dict | None = None):
    env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "HOME": str(tmp),
        "TOOLS_ENV": str(tmp / "no-env"),
        "HOST_BUILDER": "fakehost",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run([str(REMOTE_PRUNE), *args, str(project)],
                          capture_output=True, text=True, env=env, timeout=60)


def test_a_dry_run_lists_candidates_on_every_lane():
    # The acceptance criterion: with two lanes configured, a dry run
    # lists candidates on both.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        # Two lanes, two hosts, each with its own bench listing.
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(textwrap.dedent("""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            case "$cmdline" in
              *"ls -1 "*)
                if [[ "$host" == "laneA-host" ]]; then
                  echo "wt_landed"
                else
                  echo "wt_other"
                fi
                exit 0
                ;;
              *"du -sb "*) echo "1000" ;;
              bash)
                # `bash -s -- <name>` is the bench-info query: it answers
                # "<mtime> <size>". The mtime is now, which is strictly
                # after the branch was created, so the fresh-branch
                # signal does not fire and the tree is judged on its
                # commits alone.
                shift 3
                if [ "$#" -eq 1 ]; then
                  echo "$(date +%s) 1000"
                else
                  cat > /dev/null; echo "0"
                fi
                ;;
            esac
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        r = run_prune(project, fake, tmp, "--all-lanes", env_extra={
            "BUILDER_LANES": "laneA laneB",
            "HOST_BUILDER_LANEA": "laneA-host",
            "HOST_BUILDER_LANEB": "laneB-host",
            "REMOTE_PRUNE_IDLE_DAYS": "0",
        })
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr!r}"
        out = r.stdout
        # Each lane is named, so a reader knows which host a candidate
        # is on.
        assert "lane: laneA" in out, out
        assert "lane: laneB" in out, out
        # And each lane's host is named.
        assert "laneA-host" in out, out
        assert "laneB-host" in out, out


def test_one_unreachable_lane_does_not_stop_the_others():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(textwrap.dedent("""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            if [[ "$host" == "dead-host" ]]; then
              exit 7
            fi
            case "$cmdline" in
              *"ls -1 "*) echo "wt_landed" ;;
              *"du -sb "*) echo "1000" ;;
              bash)
                # `bash -s -- <name>` is the bench-info query: it answers
                # "<mtime> <size>". The mtime is now, which is strictly
                # after the branch was created, so the fresh-branch
                # signal does not fire and the tree is judged on its
                # commits alone.
                shift 3
                if [ "$#" -eq 1 ]; then
                  echo "$(date +%s) 1000"
                else
                  cat > /dev/null; echo "0"
                fi
                ;;
            esac
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        r = run_prune(project, fake, tmp, "--all-lanes", env_extra={
            "BUILDER_LANES": "dead live",
            "HOST_BUILDER_DEAD": "dead-host",
            "HOST_BUILDER_LIVE": "live-host",
            "REMOTE_PRUNE_IDLE_DAYS": "0",
        })
        # The dead lane is reported, and the live one was still measured.
        assert "dead-host" in r.stdout, r.stdout
        assert "live-host" in r.stdout, r.stdout
        assert "cannot reach dead-host" in r.stderr, r.stderr


# ---------------------------------------------------------------------------
# The `_prove` pairing.

def test_a_prove_tree_is_pruned_with_the_tree_it_belongs_to():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        # A worktree whose branch landed, so its tree is prunable.
        wt = tmp / "wt-landed"
        subprocess.run(["git", "-C", str(project), "worktree", "add",
                        "--quiet", str(wt), "-b", "landed-branch", "main"],
                       check=True, capture_output=True)
        git(project, "fetch", "--quiet", "origin")

        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(textwrap.dedent("""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            case "$cmdline" in
              *"ls -1 "*)
                echo "wt_landed"
                echo "wt_landed_prove"
                exit 0
                ;;
              *"du -sb "*) echo "1000" ;;
              bash)
                # `bash -s -- <name>` is the bench-info query: it answers
                # "<mtime> <size>". The mtime is now, which is strictly
                # after the branch was created, so the fresh-branch
                # signal does not fire and the tree is judged on its
                # commits alone.
                shift 3
                if [ "$#" -eq 1 ]; then
                  echo "$(date +%s) 1000"
                else
                  cat > /dev/null; echo "0"
                fi
                ;;
              *flock*) cat ;;
            esac
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        r = run_prune(project, fake, tmp, env_extra={
            "REMOTE_PRUNE_IDLE_DAYS": "0",
        })
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr!r}"
        out = r.stdout
        landed = out.split("landed, safe to remove")[-1].split("==>")[0]
        assert "wt_landed" in landed, out
        # The prove copy goes with it. Without this, the largest tree on
        # the builder was kept forever, because it has no worktree behind
        # it and "no worktree means keep" applied to it.
        assert "wt_landed_prove" in landed, (
            f"the _prove copy was not pruned with its tree: {out!r}")


def test_a_prove_tree_whose_base_is_absent_is_kept():
    # A prove run that outlived its tree. There is nothing to pair it
    # with, so it stays -- uncertainty means keep.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(textwrap.dedent("""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            case "$cmdline" in
              *"ls -1 "*) echo "orphan_prove" ;;
              *"du -sb "*) echo "1000" ;;
              bash)
                # `bash -s -- <name>` is the bench-info query: it answers
                # "<mtime> <size>". The mtime is now, which is strictly
                # after the branch was created, so the fresh-branch
                # signal does not fire and the tree is judged on its
                # commits alone.
                shift 3
                if [ "$#" -eq 1 ]; then
                  echo "$(date +%s) 1000"
                else
                  cat > /dev/null; echo "0"
                fi
                ;;
            esac
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        r = run_prune(project, fake, tmp, env_extra={
            "REMOTE_PRUNE_IDLE_DAYS": "0",
        })
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr!r}"
        assert "orphan_prove" not in r.stdout.split(
            "landed, safe to remove")[-1].split("==>")[0], r.stdout
        assert "no local worktree" in r.stdout, r.stdout


def test_the_landing_path_removes_the_tree_and_its_prove_copy():
    # The acceptance criterion: after a PR's branch lands, its tree and
    # its _prove cache are gone from every lane.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        removed = tmp / "removed.txt"
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            case "$cmdline" in
              *"test -d "*)
                # The tree exists.
                exit 0
                ;;
              *"rm -rf "*)
                echo "$cmdline" >> "{removed}"
                exit 0
                ;;
            esac
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        r = run_prune(project, fake, tmp, "--prune", "--tree", "wt_landed",
                      env_extra={
            "BUILDER_LANES": "laneA laneB",
            "HOST_BUILDER_LANEA": "laneA-host",
            "HOST_BUILDER_LANEB": "laneB-host",
        })
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr!r}"
        body = removed.read_text() if removed.exists() else ""
        lines = [ln for ln in body.splitlines() if "rm -rf" in ln]
        # Both trees, on both lanes: four removals.
        assert len(lines) == 4, (
            f"expected two trees on two lanes, got {len(lines)}: {body!r}")
        assert sum(1 for ln in lines if ln.endswith("bench/wt_landed")) == 2, (
            f"the tree was not removed on every lane: {body!r}")
        assert sum(1 for ln in lines
                   if ln.endswith("bench/wt_landed_prove")) == 2, (
            f"the _prove copy was not removed on every lane: {body!r}")
        # And each lane was reported.
        assert "lane: laneA" in r.stdout, r.stdout
        assert "lane: laneB" in r.stdout, r.stdout


def test_the_landing_path_is_a_dry_run_without_prune():
    # Without `--prune` nothing is removed, on any lane. This tool is a
    # dry run by default, and a landing path that deleted a tree on every
    # lane from a bare `--tree` deleted a build cache nobody asked to
    # delete.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        removed = tmp / "removed.txt"
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            case "$cmdline" in
              *"test -d "*) exit 0 ;;
              *"rm -rf "*)
                echo "$cmdline" >> "{removed}"
                exit 0
                ;;
            esac
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        r = run_prune(project, fake, tmp, "--tree", "wt_landed",
                      env_extra={
            "BUILDER_LANES": "laneA laneB",
            "HOST_BUILDER_LANEA": "laneA-host",
            "HOST_BUILDER_LANEB": "laneB-host",
        })
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr!r}"
        # The directories still exist: nothing was removed.
        body = removed.read_text() if removed.exists() else ""
        assert "rm -rf" not in body, (
            f"a dry run removed a tree: {body!r}")
        # And the candidates were reported instead.
        assert "wt_landed" in r.stdout, r.stdout
        assert "would remove" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# remote-task runs the check before the sync.

def test_remote_task_refuses_the_build_when_the_builder_is_full():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp)
        fake = tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(textwrap.dedent("""\
            #!/bin/bash
            host="$1"; shift
            cmdline="$1"
            if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
              script=$(cat)
              case "$script" in
                *bench_fs*) printf 'DISK_LOW 2\\n'; exit 4 ;;
              esac
            fi
            exit 0
        """))
        (fake / "ssh").chmod(0o755)
        (fake / "rsync").write_text("#!/bin/sh\nexit 0\n")
        (fake / "rsync").chmod(0o755)
        env = {
            "PATH": f"{fake}:/usr/bin:/bin",
            "HOME": str(tmp),
            "TOOLS_ENV": str(tmp / "no-env"),
            "HOST_BUILDER": "fakehost",
            "BUILDER_MIN_FREE_GB": "20",
        }
        r = subprocess.run([str(REMOTE_TASK), str(project)],
                           capture_output=True, text=True, env=env, timeout=60)
        # The disk-low code, not a build failure.
        assert r.returncode == 4, (
            f"expected the disk-low code 4, got {r.returncode}: "
            f"{r.stdout!r} {r.stderr!r}")
        assert "disk" in r.stderr.lower(), r.stderr
        # And the build never started.
        assert "syncing" not in r.stdout, r.stdout


def main() -> int:
    failures: list[str] = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures.append(f"{name}: {exc}")
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
