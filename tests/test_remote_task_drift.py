#!/usr/bin/env python3
"""Pin that remote-task reaches the drift report after a successful run.

The report is advisory end to end: a task without [task.mirrors] must print
nothing extra, a failing task-mirror-check must not fail the run, and a
machine without the tool on PATH must see a named skip rather than a shell
error. The ssh and rsync transport is faked with no-op scripts; the manifest
reader, the drift report, and the wiring in remote-task are the real thing,
so a regression in any of them is caught here, not just in the tool's own
tests.
"""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_TASK = REPO_ROOT / "bin" / "remote-task"
REPO_BIN = REPO_ROOT / "bin"
MANIFEST_PY = REPO_ROOT / "lib" / "manifest.py"

# The workflow runs one step more than the command, so a correct report names
# the missing step. The command is opaque (not a just recipe) so this test
# does not depend on `just` being on PATH: one-level expansion is pinned by
# tests/test_task_mirror_check.py.
WORKFLOW = textwrap.dedent("""\
    name: CI
    on: [push]
    jobs:
      rust-tests:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - run: cargo build --workspace
          - run: cargo test --workspace
    """)


def manifest_text(mirrors: bool) -> str:
    lines = [
        "[[task]]",
        'name = "bench"',
        'role = "builder"',
        'command = "cargo build --workspace"',
    ]
    if mirrors:
        lines += [
            "[task.mirrors]",
            'workflow = ".github/workflows/ci.yaml"',
            'job = "rust-tests"',
        ]
    return "\n".join(lines) + "\n"


def manifest_with_migrator(sources: list[str] | None) -> str:
    """Build a gates.toml with [task.migrator.sources] when `sources` is set.

    The block is omitted when sources is None, so the existing test fixture
    can stay a one-liner and the new tests opt in.
    """
    lines = [
        "[[task]]",
        'name = "bench"',
        'role = "builder"',
        'command = "cargo build --workspace"',
    ]
    if sources is not None:
        quoted = ", ".join(f'"{s}"' for s in sources)
        lines += ["[task.migrator]", f"sources = [{quoted}]"]
    return "\n".join(lines) + "\n"


def setup_project(tmpdir: Path, mirrors: bool) -> Path:
    """Materialise a fake project and git-init it so repo_root works."""
    wf_dir = tmpdir / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yaml").write_text(WORKFLOW)
    (tmpdir / "gates.toml").write_text(manifest_text(mirrors))
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=tmpdir, check=True,
                       capture_output=True)
    return tmpdir.resolve()


def setup_project_migrator(tmpdir: Path, sources: list[str] | None,
                            migrations: list[tuple[str, str]] | None) -> Path:
    """Materialise a fake project with optional [task.migrator] and `migrations/`.

    `migrations` is a list of (name, body) pairs that get written into
    `tmpdir/migrations/`. Passing None for either argument skips the
    corresponding setup. The directory is created empty when migrations is
    `[]`.
    """
    tmpdir.mkdir(parents=True, exist_ok=True)
    (tmpdir / "gates.toml").write_text(manifest_with_migrator(sources))
    if migrations is not None:
        mig = tmpdir / "migrations"
        mig.mkdir(exist_ok=True)
        for name, body in migrations:
            (mig / name).write_text(body)
    for source in sources or []:
        path = tmpdir / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// custom embedding site\n")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=tmpdir, check=True,
                       capture_output=True)
    return tmpdir.resolve()


# The stateful fake ssh. Lives below the existing helpers so the older
# tests -- which use a no-op ssh -- keep their contract. The script
# maintains a fake remote filesystem rooted at $FAKE_REMOTE_ROOT; every
# `ssh HOST CMD...` invocation mutates that filesystem so the test can
# assert on what remote-task sent.
#
# Intentionally narrow: only the verbs remote-task actually sends. Any
# other invocation is a test bug or a tool bug, and silent no-ops hide
# both. We log every dispatch to stderr so the trace tells the next
# reader which verb we routed.
STATEFUL_FAKE_SSH = r"""#!/bin/bash
root="${FAKE_REMOTE_ROOT:?must be set}"
host="${1:?fake ssh needs a host}"
shift
mkdir -p "$root"
# remote-task passes the whole remote command as a single argument,
# so the verb and any flags arrive in one string. Strip it down to
# the verb and the first non-flag arg; everything past that goes
# straight to the verb implementation.
cmdline="$1"
# The space check that runs before every build asks the host how much
# room it has. The fakes below run the command verbatim, so without this
# the check would measure the TEST MACHINE's real disk and refuse the
# build -- a failure that has nothing to do with what the test is about.
# The answer is "plenty of room", which is what a healthy builder looks
# like. FAKE_FREE_GB makes it something else when a test wants to see the
# refusal path.
if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
  script=$(cat)
  case "$script" in
    *bench_fs*)
      echo "==> builder $host: ${FAKE_FREE_GB:-100}G free, need space" >&2
      printf 'OK %s\n' "${FAKE_FREE_GB:-100}"
      exit 0
      ;;
  esac
fi
# Verb: the first whitespace-separated word.
verb="${cmdline%% *}"
# The build's command starts `CALLER=... flock`, so map it onto the flock verb.
if [ "$verb" = "flock" ] || [ "${cmdline#CALLER=*}" != "$cmdline" ] && [ "${cmdline%flock*}" != "$cmdline" ]; then
  verb=flock
fi
case "$verb" in
mkdir)
  # Extract the directory: skip `mkdir`, skip any `-X` flags, take the
  # first non-flag argument.
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
  # `cat PATH 2>/dev/null`: skip cat, take the first non-flag non-redir.
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
  # Parse with the remote shell's quoting rules, including spaces in paths.
  eval "set -- $cmdline"
  shift
  for a in "$@"; do
    mkdir -p "$root/$(dirname "$a")"
    touch "$root/$a"
  done
  ;;
printf)
  # printf '%s\n' CONTENT > FILE -- the only form remote-task uses.
  # Parse: skip `printf`, skip the format string ('%s\n'), take the body,
  # skip `>`, take the target. Bash passes the body wrapped in single
  # quotes from the command line; strip those so the file content
  # matches what remote-task thought it sent.
  body=""
  target=""
  state=verb
  for a in $cmdline; do
    case "$state:$a" in
      verb:printf) state=format ;;
      format:'%s\n') state=body ;;
      format:*) state=body; body="$a" ;;
      body:'>') state=target ;;
      body:*) body="${a#\'}"; body="${body%\'}" ;;
      target:*) target="$a" ;;
    esac
  done
  [ -n "$target" ] || { echo "fake ssh: printf needs a target" >&2; exit 1; }
  mkdir -p "$root/$(dirname "$target")"
  printf '%s\n' "$body" > "$root/$target"
  ;;
flock)
  # The build's heredoc form: consume stdin, do nothing.
  cat > /dev/null
  ;;
docker)
  # Container reset is inert to the staleness check.
  ;;
*)
  echo "fake ssh: unhandled verb: $verb" >&2
  cat > /dev/null
  exit 0
  ;;
esac
"""


def make_fakebin(tmpdir: Path, extra: dict | None = None,
                  stateful: bool = False) -> Path:
    """A directory of no-op stand-ins for the transport, plus any extras."""
    fake = tmpdir / "fakebin"
    fake.mkdir()
    scripts = {
        # The drift test must not touch a host or move a tree. The one
        # thing it must answer is the space check that runs before the
        # sync: a fake that said nothing would leave the check
        # unprovable, and the run would fail for a reason that has
        # nothing to do with the drift being tested.
        "ssh": "#!/bin/sh\n"
               "# The space check's script comes on stdin; answer it with\n"
               "# plenty of free space, which is what a builder with room\n"
               "# looks like.\n"
               "case \"$(cat)\" in\n"
               "  *bench_fs*) echo 'OK 100' ;;\n"
               "  *) exit 0 ;;\n"
               "esac\n",
        "rsync": "#!/bin/sh\nexit 0\n",
    }
    if stateful:
        scripts["ssh"] = STATEFUL_FAKE_SSH
    scripts.update(extra or {})
    for name, body in scripts.items():
        p = fake / name
        p.write_text(body)
        p.chmod(0o755)
    # Every ssh -- including one a case supplied for its own purpose --
    # has to answer the space check, because the check runs before
    # anything else. A case that replaces ssh to make one stage fail
    # would otherwise fail at the space check instead, and the run's
    # exit status would be the space check's rather than the one the
    # case is about.
    #
    # The wrapper answers the space check and delegates everything else
    # to the script the case supplied.
    _wrap_space_answer(fake / "ssh")
    return fake


def _wrap_space_answer(ssh_script: Path) -> None:
    """Make an ssh stand-in answer the space check, and delegate the rest.

    The check's command is an export prefix followed by `bash`, and its
    script arrives on stdin. The wrapper answers it with free space and
    exits; every other command goes to the original script.
    """
    original = ssh_script.read_text()
    body = original.split("\n", 1)[1] if "\n" in original else ""
    wrapper = (
        "#!/bin/bash\n"
        "cmdline=\"$2\"\n"
        "if [[ \"$cmdline\" == *\"BUILDER_SPACE_TREE=\"*\" bash\" ]]; then\n"
        "  script=$(cat)\n"
        "  case \"$script\" in\n"
        "    *bench_fs*)\n"
        "      printf 'OK %s\\n' \"${FAKE_FREE_GB:-100}\"\n"
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        + body
    )
    ssh_script.write_text(wrapper)
    ssh_script.chmod(0o755)


def remote_task_env(project: Path, fakebin: Path, repo_bin: bool,
                    fake_remote_root: Path | None = None) -> dict:
    """The hermetic environment a remote-task run is built in.

    Shared by run_remote_task and the kill test, which Popen's the same
    command but has to signal it mid-run: two copies of this env would
    drift, and a test that reaches a real host or lock is not hermetic.
    """
    env_file = project / "tools.env"
    env_file.write_text("")
    lock = project / "build.lock"
    path_parts = [str(fakebin)]
    if repo_bin:
        # The real task-mirror-check, as it would sit on PATH after install.
        path_parts.append(str(REPO_BIN))
    env = {
        "PATH": os.pathsep.join(path_parts + ["/usr/bin", "/bin"]),
        # Named role, fake target: host_for must not reach a real host.
        "HOST_BUILDER": "fakehost",
        # A scratch env file so this machine's operator env is never sourced.
        "TOOLS_ENV": str(env_file),
        # Keep the local build lock out of $HOME.
        "REMOTE_TASK_LOCK": str(lock),
        # $HOME is only read for defaults both of which are overridden; point
        # it at the project so nothing can reach the real one.
        "HOME": str(project),
    }
    if fake_remote_root is not None:
        env["FAKE_REMOTE_ROOT"] = str(fake_remote_root)
    return env


def run_remote_task(project: Path, fakebin: Path, repo_bin: bool,
                     fake_remote_root: Path | None = None
                     ) -> subprocess.CompletedProcess:
    """Run the real remote-task against the fake project, hermetically."""
    return subprocess.run([str(REMOTE_TASK), str(project)],
                          capture_output=True, text=True,
                          env=remote_task_env(project, fakebin, repo_bin,
                                              fake_remote_root))


def assert_pass(msg: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{msg}: {detail}")


def test_reports_drift_before_final_pass():
    """A successful run reports drift before its final verdict."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", True)
        fakebin = make_fakebin(Path(t))
        r = run_remote_task(project, fakebin, repo_bin=True)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("build reported PASS", "==> PASS (bench" in out, out)
        assert_pass("drift report present", "drift report" in out, out)
        assert_pass("missing step recommended",
                    "+ cargo test --workspace" in out, out)
        # The report is an observation after the run, not before or instead
        # of it: the PASS line comes last.
        lines = out.splitlines()
        verdicts = [line for line in lines
                    if line.startswith(("==> PASS", "==> FAIL"))]
        assert_pass("exactly one PASS, on the final line",
                    verdicts == ["==> PASS (bench: proj)"]
                    and lines[-1] == verdicts[0], out)


def test_no_mirrors_is_unchanged():
    """A task without [task.mirrors] prints exactly what it always did."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", False)
        fakebin = make_fakebin(Path(t))
        r = run_remote_task(project, fakebin, repo_bin=True)
        lines = r.stdout.splitlines()
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no drift output of any kind",
                    "drift" not in r.stdout, r.stdout)
        assert_pass("no stderr", r.stderr == "", r.stderr)
        # The run's own two lines and nothing else: the wiring must be
        # invisible to a project that has not opted in.
        assert_pass("exactly the pre-existing output lines",
                    len(lines) == 2
                    and lines[0].startswith("==> syncing")
                    and lines[1].startswith("==> PASS (bench"),
                    r.stdout)


def test_failing_tool_does_not_fail_run():
    """A task-mirror-check that exits non-zero cannot fail the build."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", True)
        fakebin = make_fakebin(Path(t), extra={
            # A tool that is on PATH but broken: the run must survive it.
            "task-mirror-check":
                "#!/bin/sh\necho marker-broken-mirror\nexit 1\n",
        })
        r = run_remote_task(project, fakebin, repo_bin=False)
        out = r.stdout
        assert_pass("exit 0 with a failing tool", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("the tool was reached",
                    "marker-broken-mirror" in out, out)
        assert_pass("build still reported PASS",
                    "==> PASS (bench" in out, out)


def test_missing_tool_is_named():
    """Without the tool on PATH the run prints a named skip, not an error."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", True)
        fakebin = make_fakebin(Path(t))
        r = run_remote_task(project, fakebin, repo_bin=False)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("named skip printed",
                    "task-mirror-check not on PATH" in out, out)
        assert_pass("no drift report attempted",
                    "drift report for" not in out, out)
        assert_pass("build still reported PASS",
                    "==> PASS (bench" in out, out)


# ---------------------------------------------------------------------------
# Migration staleness (issue #50). Each test below exercises one branch of the
# check: no migrations dir, [task.migrator] absent, first sync, matching
# fingerprint, changed fingerprint, and the touch surviving a real rsync.

MIGRATOR_SOURCES = ["crates/db/src/lib.rs", "crates/api-rest/src/lib.rs",
                    "src/main.rs"]
MIGRATIONS_V1 = [("0001_init.sql", "CREATE TABLE init (id INT);\n")]
MIGRATIONS_V2 = [("0001_init.sql", "CREATE TABLE init (id INT);\n"),
                 ("0002_added.sql", "CREATE TABLE added (id INT);\n")]


def local_fingerprint(migrations: list[tuple[str, str]], sources=None,
                      command="cargo build --workspace") -> str:
    """The same fingerprint remote-task computes locally.

    The bash side runs `find | sort -z | xargs sha256sum | sha256sum`.
    That hashes each file, joins `<hash>  \n` lines in sorted
    order, and hashes the joined blob. We mirror that with Python so the
    test can pre-populate a matching fingerprint and verify the
    no-touch branch.
    """
    import hashlib
    lines = []
    for name, body in sorted(migrations):
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        lines.append(f"{body_hash}  migrations/{name}\n")
    migration_hash = hashlib.sha256("".join(lines).encode()).hexdigest()
    values = ["guard-v2", migration_hash, command, *sorted(
        MIGRATOR_SOURCES if sources is None else sources)]
    return hashlib.sha256(("\0".join(values) + "\0").encode()).hexdigest()


def test_no_migrations_dir_is_unchanged():
    """[task.migrator] configured but no migrations/ on disk: silent.

    A project that has neither an embedded set nor a [task.migrator]
    block must look identical to one that has neither. Without a
    migrations/ directory the fingerprint is undefined and we do not
    write one.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=None)
        fakebin = make_fakebin(Path(t), stateful=True)
        fake_root = Path(t) / "remote"
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no migration touch line",
                    "migrations changed" not in out, out)
        assert_pass("no fingerprint written on remote",
                    not (fake_root / "bench" / "proj"
                         / ".migrations-fingerprint").exists(),
                    "fingerprint should not exist without migrations/")
        assert_pass("build still reported PASS",
                    "==> PASS (bench" in out, out)


def test_unconfigured_sources_are_discovered():
    """No hand-kept manifest list is needed, including integration tests."""
    for declared in (None, ["custom/embed.rs"]):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            project = setup_project_migrator(tmp / "proj", declared, MIGRATIONS_V2)
            sites = ["crates/hypatia-migrate/src/main.rs",
                     "crates/api-rest/tests/common/mod.rs",
                     "crates/db/tests/credential_rotation.rs"]
            for source in sites:
                path = project / source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('static M: Migrator = sqlx :: migrate ! ("./migrations");')
            # Excluded caches must not become embedding sources.
            for directory in ("target", "node_modules", ".git"):
                (project / directory).mkdir(exist_ok=True)
                (project / directory / "ignored.rs").write_text('sqlx::migrate!();')
            remote = tmp / "remote"
            dest = remote / "bench/proj"
            dest.mkdir(parents=True)
            (dest / ".migrations-fingerprint").write_text(local_fingerprint(MIGRATIONS_V1))
            fake = make_fakebin(tmp, stateful=True)
            result = run_remote_task(project, fake, False, remote)
            assert_pass("automatic discovery succeeds", result.returncode == 0, repr(result))
            expected = sites + (declared or [])
            assert_pass("every discovered and declared source invalidated",
                        all((dest / source).exists() for source in expected), repr(result))
            assert_pass("excluded caches ignored",
                        f"touching {len(expected)} source(s)" in result.stdout, result.stdout)


def test_stray_byte_does_not_abort_discovery():
    """A .rs file with a non-UTF-8 byte must not abort the embedding scan.

    The scan reads every .rs file; one stray byte used to raise
    UnicodeDecodeError and kill the whole run before the guard could name
    the embedding site it still has to touch. surrogateescape carries on past
    it, so the guard still finds and touches the real site when migrations
    change.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project_migrator(tmp / "proj", None, MIGRATIONS_V1)
        site = "crates/hypatia-migrate/src/main.rs"
        (project / site).parent.mkdir(parents=True, exist_ok=True)
        (project / site).write_text(
            'static M: Migrator = sqlx :: migrate ! ("./migrations");')
        # A second .rs file holding a stray non-UTF-8 byte. It is never
        # compiled and embeds no Migrator; the scan must skip over it, not
        # die on it.
        (project / "src" / "fixture.rs").parent.mkdir(parents=True, exist_ok=True)
        (project / "src" / "fixture.rs").write_bytes(b"// caf\xe9\n")
        remote = tmp / "remote"
        fake = make_fakebin(tmp, stateful=True)
        # First run records the baseline fingerprint.
        first = run_remote_task(project, fake, False, remote)
        assert_pass("first run records the baseline",
                    first.returncode == 0, repr(first))
        # The migration set moves, as a rebase would move it.
        (project / "migrations" / "0002_added.sql").write_text(
            MIGRATIONS_V2[1][1])
        result = run_remote_task(project, fake, False, remote)
        dest = remote / "bench/proj"
        assert_pass("second run exits 0 despite the stray byte",
                    result.returncode == 0, repr(result))
        assert_pass("the embedding site is discovered and touched",
                    (dest / site).exists(), repr(result))


def test_missing_sources_refuse_and_preserve_fingerprint():
    for fingerprint in (None, local_fingerprint(MIGRATIONS_V1)):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            project = setup_project_migrator(tmp / "proj", None, MIGRATIONS_V2)
            remote = tmp / "remote"
            dest = remote / "bench/proj"
            dest.mkdir(parents=True)
            sentinel = dest / ".migrations-fingerprint"
            if fingerprint is not None:
                sentinel.write_text(fingerprint)
            fake = make_fakebin(tmp, stateful=True)
            result = run_remote_task(project, fake, False, remote)
            assert_pass("missing sources refuse", result.returncode != 0, repr(result))
            assert_pass("diagnostic names directory and remedy",
                        "migrations" in result.stderr and "task.migrator" in result.stderr, repr(result))
            assert_pass("failed guard does not advance fingerprint",
                        sentinel.read_text() == fingerprint if sentinel.exists() else fingerprint is None,
                        repr(result))


def test_first_run_invalidates_unverified_cache():
    """No sentinel may mean a warm cache adopting the guard, so invalidate."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=MIGRATIONS_V1)
        fakebin = make_fakebin(Path(t), stateful=True)
        fake_root = Path(t) / "remote"
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("unverified cache is invalidated on first run",
                    "migrations changed" in out, out)
        fp_path = fake_root / "bench" / "proj" / ".migrations-fingerprint"
        assert_pass("fingerprint recorded on first sync",
                    fp_path.is_file(), "expected fingerprint to exist")
        assert_pass("fingerprint matches local migration set",
                    fp_path.read_text().strip() == local_fingerprint(MIGRATIONS_V1),
                    fp_path.read_text())
        assert_pass("all sources were touched on the remote",
                    all((fake_root / "bench" / "proj" / s).exists()
                            for s in MIGRATOR_SOURCES),
                    "expected sources to be touched")


def test_matching_fingerprint_no_touch():
    """When the local set matches what the remote last built, do nothing.

    This is the warm-cache case the issue is specifically trying to
    preserve: an unchanged tree does not trigger a rebuild. We
    pre-populate the fingerprint the local side would compute, so the
    comparison succeeds and the touch branch is skipped.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=MIGRATIONS_V1)
        fake_root = Path(t) / "remote"
        # Pre-populate the fingerprint that local_fingerprint(MIGRATIONS_V1)
        # would produce. mkdir the dest first so the fake ssh can land the
        # file. We do this by hand here rather than by invoking the tool.
        remote_dest = fake_root / "bench" / "proj"
        remote_dest.mkdir(parents=True, exist_ok=True)
        (remote_dest / ".migrations-fingerprint").write_text(
            local_fingerprint(MIGRATIONS_V1) + "\n")
        fakebin = make_fakebin(Path(t), stateful=True)
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no migration touch line on a warm cache",
                    "migrations changed" not in out, out)
        assert_pass("no source files were touched",
                    not any((fake_root / "bench" / "proj" / s).exists()
                            for s in MIGRATOR_SOURCES),
                    "expected no sources to be touched")
        assert_pass("fingerprint left unchanged",
                    (fake_root / "bench" / "proj"
                     / ".migrations-fingerprint").read_text().strip()
                    == local_fingerprint(MIGRATIONS_V1),
                    "fingerprint should not change when nothing changed")


def test_changed_migrations_touch_sources():
    """A migrations set that diverges from what the remote cached triggers a touch.

    The pre-populated fingerprint hashes the V1 set; the local tree now
    has V2. The tool names the rebuild it performs -- silent touch,
    silent rebuild is the failure mode this test exists to catch.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=MIGRATIONS_V2)
        fake_root = Path(t) / "remote"
        remote_dest = fake_root / "bench" / "proj"
        remote_dest.mkdir(parents=True, exist_ok=True)
        (remote_dest / ".migrations-fingerprint").write_text(
            local_fingerprint(MIGRATIONS_V1) + "\n")
        fakebin = make_fakebin(Path(t), stateful=True)
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("announcement line printed",
                    "migrations changed or unverified" in out, out)
        # Names the number of files, so a reviewer can see how broad the
        # rebuild will be without grepping the manifest.
        assert_pass("announcement names the count",
                    f"touching {len(MIGRATOR_SOURCES)} source(s)" in out, out)
        for src in MIGRATOR_SOURCES:
            assert_pass(f"touched {src}",
                        (fake_root / "bench" / "proj" / src).exists(),
                        f"missing touched file: {src}")
        # Fingerprint advanced to the new set, so a subsequent run with
        # the same local tree does not re-touch.
        new_fp = (fake_root / "bench" / "proj"
                  / ".migrations-fingerprint").read_text().strip()
        assert_pass("fingerprint updated to the new set",
                    new_fp == local_fingerprint(MIGRATIONS_V2),
                    f"got {new_fp!r}")
        # The announcement comes *before* the build, not after, so a
        # reviewer reading top-to-bottom sees what is about to happen.
        assert_pass("announcement precedes the build PASS",
                    out.index("migrations changed") < out.index("==> PASS"),
                    out)


# The transport is a no-op in every test above, which is what let the touch sit
# on the wrong side of the sync: a stub rsync cannot revert an mtime. This one
# runs the real rsync against a local directory standing in for the builder.
REAL_RSYNC_STUB = """#!/bin/sh
# Rewrite host:path into the fake root, then hand over to the real rsync so
# the archive flag's mtime preservation is actually exercised.
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


def test_touch_survives_a_real_rsync():
    """`rsync -a` preserves the source's mtimes, so a touch applied to the
    remote copy before the transfer is reverted by it: the file lands with its
    original timestamp, older than the artifacts in the warm target/, and cargo
    rebuilds nothing. The touch has to happen after the sync, and only a real
    rsync can show the difference."""
    if not Path("/usr/bin/rsync").exists():
        print("SKIP touch survives a real rsync: no /usr/bin/rsync")
        return
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project_migrator(
            tmp / "proj", sources=None, migrations=MIGRATIONS_V1)
        # The sources have to exist for rsync to deliver them, and to be older
        # than the run so a surviving touch is visible as a newer mtime.
        old_time = 1756700000  # a fixed point well before the test runs
        for s in MIGRATOR_SOURCES:
            f = project / s
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('static M: Migrator = sqlx::migrate!("./migrations");\n')
            os.utime(f, (old_time, old_time))
        fakebin = make_fakebin(tmp, stateful=True,
                              extra={"rsync": REAL_RSYNC_STUB})
        fake_root = tmp / "remote"
        # First sync records the baseline fingerprint.
        first = run_remote_task(project, fakebin, repo_bin=False,
                                fake_remote_root=fake_root)
        assert_pass("first automatic run succeeds", first.returncode == 0, repr(first))
        warm = run_remote_task(project, fakebin, repo_bin=False,
                               fake_remote_root=fake_root)
        assert_pass("unchanged warm run avoids rebuild", warm.returncode == 0
                    and "touching" not in warm.stdout, repr(warm))
        # The migration set moves, as a rebase would move it.
        for name, body in MIGRATIONS_V2:
            (project / "migrations" / name).write_text(body)
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("announced the touch",
                    "migrations changed" in r.stdout, r.stdout)
        for s in MIGRATOR_SOURCES:
            remote = fake_root / "bench" / "proj" / s
            local = project / s
            assert_pass(f"{s} present on the remote", remote.is_file(), str(remote))
            assert_pass(
                f"{s} is newer on the remote than in the source tree",
                remote.stat().st_mtime > local.stat().st_mtime,
                f"remote={remote.stat().st_mtime} local={local.stat().st_mtime} "
                "-- a touch before the sync is reverted by rsync -a")



def test_failed_build_retries_invalidation():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project_migrator(tmp / "proj", MIGRATOR_SOURCES, MIGRATIONS_V2)
        remote = tmp / "remote"
        dest = remote / "bench/proj"
        dest.mkdir(parents=True)
        sentinel = dest / ".migrations-fingerprint"
        old = local_fingerprint(MIGRATIONS_V1)
        sentinel.write_text(old)
        failing_ssh = STATEFUL_FAKE_SSH.replace(
            "  cat > /dev/null\n  ;;",
            "  cat >/dev/null; exit 42\n  ;;",
            1,
        )
        fake = make_fakebin(tmp, stateful=True, extra={"ssh": failing_ssh})
        failed = run_remote_task(project, fake, False, remote)
        assert_pass("failed build status", failed.returncode == 42, repr(failed))
        assert_pass("failed build leaves prior fingerprint", sentinel.read_text() == old)
        (fake / "ssh").write_text(STATEFUL_FAKE_SSH)
        retried = run_remote_task(project, fake, False, remote)
        assert_pass("retry invalidates again", retried.returncode == 0
                    and "migrations changed" in retried.stdout, repr(retried))
        assert_pass("successful retry advances fingerprint",
                    sentinel.read_text().strip() == local_fingerprint(MIGRATIONS_V2))


def test_custom_sources_validate_paths():
    for source in ("missing.rs", "../outside.rs", "target/embed.rs", "linked.rs"):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            project = setup_project_migrator(tmp / "proj", None, MIGRATIONS_V1)
            (project / "gates.toml").write_text(manifest_with_migrator([source]))
            outside = tmp / "outside.rs"
            outside.write_text("sqlx::migrate!();")
            (project / "linked.rs").symlink_to(outside)
            fake = make_fakebin(tmp, stateful=True)
            result = run_remote_task(project, fake, False, tmp / "remote")
            assert_pass("invalid source refuses: " + source,
                        result.returncode != 0 and "migration guard:" in result.stderr, repr(result))


def test_command_change_and_legacy_sentinel_invalidate():
    for old in ("legacy-hash", local_fingerprint(MIGRATIONS_V1, command="cargo test")):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            project = setup_project_migrator(tmp / "proj", MIGRATOR_SOURCES, MIGRATIONS_V1)
            remote = tmp / "remote"
            dest = remote / "bench/proj"
            dest.mkdir(parents=True)
            (dest / ".migrations-fingerprint").write_text(old)
            fake = make_fakebin(tmp, stateful=True)
            result = run_remote_task(project, fake, False, remote)
            assert_pass("changed command/legacy cache invalidated", result.returncode == 0
                        and "touching" in result.stdout, repr(result))


def test_source_with_spaces_is_one_remote_path():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        sources = ["custom dir/embed source.rs"]
        project = setup_project_migrator(tmp / "proj", sources, MIGRATIONS_V1)
        remote = tmp / "remote"
        fake = make_fakebin(tmp, stateful=True)
        result = run_remote_task(project, fake, False, remote)
        assert_pass("quoted source touched", result.returncode == 0
                    and (remote / "bench/proj" / sources[0]).is_file(), repr(result))

def test_failures_have_one_final_verdict():
    """Setup, sync, remote command, and fetch failures retain their status."""
    cases = [
        ("mkdir", {"ssh": "#!/bin/sh\nexit 17\n"}, 17),
        ("sync", {"rsync": "#!/bin/sh\nexit 23\n"}, 23),
        ("command", {"ssh": "#!/bin/sh\ncase \"$2\" in *flock*) cat >/dev/null; exit 42;; esac\n"}, 42),
        ("fetch", {"rsync": "#!/bin/sh\ncase \"$*\" in *fakehost:bench/proj/result*) exit 24;; esac\nexit 0\n"}, 24),
        ("manifest", {}, 1),
    ]
    for stage, extra, status in cases:
        with tempfile.TemporaryDirectory() as t:
            project = setup_project(Path(t) / "proj", False)
            if stage == "fetch":
                with (project / "gates.toml").open("a") as f:
                    f.write('fetch = ["result"]\n')
            if stage == "manifest":
                (project / "gates.toml").write_text("invalid = [")
            fakebin = make_fakebin(Path(t), extra=extra)
            r = run_remote_task(project, fakebin, repo_bin=False)
            assert_pass(stage + " exit status", r.returncode == status, repr(r))
            lines = r.stdout.splitlines()
            verdicts = [line for line in lines if line.startswith(("==> PASS", "==> FAIL"))]
            expected = f"==> FAIL (bench: proj, exit {status})"
            assert_pass(stage + " one final verdict",
                        verdicts == [expected] and lines[-1] == expected, r.stdout)


# Holds the build (the ssh call carrying `flock`) open to be killed. It drains
# the heredoc first, or remote-task's write gets EPIPE, and drops its stdout
# and stderr before sleeping, or communicate() waits for the sleep.
KILL_FAKE_SSH = r"""#!/bin/sh
case "$*" in
*flock*)
  cat > /dev/null
  touch "${KILL_TEST_MARKER:?}"
  exec 1>/dev/null 2>/dev/null
  sleep 30
  ;;
esac
exit 0
"""


def test_killed_run_does_not_pass():
    """SIGTERM mid-build ends in a FAIL naming the interruption, not a PASS."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project(tmp / "proj", False)
        marker = tmp / "sleep-started"
        fakebin = make_fakebin(tmp, extra={"ssh": KILL_FAKE_SSH})
        env = remote_task_env(project, fakebin, repo_bin=False)
        env["KILL_TEST_MARKER"] = str(marker)
        proc = subprocess.Popen([str(REMOTE_TASK), str(project)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, env=env)
        deadline = time.time() + 30
        while not marker.exists():
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(
                    f"remote-task died before the build started: "
                    f"rc={proc.returncode} stdout={out!r} stderr={err!r}")
            if time.time() > deadline:
                proc.kill()
                proc.communicate()
                raise AssertionError(
                    "marker never appeared: the build dispatch was not reached")
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        lines = out.splitlines()
        verdicts = [line for line in lines
                    if line.startswith(("==> PASS", "==> FAIL"))]
        assert_pass("a killed run exits non-zero",
                    proc.returncode != 0,
                    f"rc={proc.returncode} stdout={out!r} stderr={err!r}")
        assert_pass("the final line names the interruption",
                    len(verdicts) == 1 and bool(lines)
                    and lines[-1] == verdicts[0]
                    and verdicts[0].startswith("==> FAIL (bench")
                    and "interrupted" in verdicts[0],
                    f"stdout={out!r} stderr={err!r}")
        assert_pass("no PASS anywhere",
                    not any(line.startswith("==> PASS") for line in lines), out)


# ---------------------------------------------------------------------------
# Runs the remote command as a child (not exec'd). The build dispatch
# records its pid in FAKE_SSH_PIDFILE; preparatory calls (mkdir, fingerprint
# read, touch, db reset, fetch) intentionally do not, because they are
# short-lived and would race the test for the pidfile. The build cmdline
# is the one carrying `CALLER=...flock...`, and that is what the script
# uses to recognise it. With the pid narrowed to the build, the recorded
# pid is the one the test's $PPID expands to on the builder.
RUN_LOCAL_FAKE_SSH = r"""#!/bin/bash
# Mandatory env: FAKE_SSH_PIDFILE (where to write our pid) and
# FAKE_SSH_HOME (where the inner bash thinks $HOME is, so the per-test
# remote lock path lives there).
: "${FAKE_SSH_PIDFILE:?must be set}"
export HOME="$FAKE_SSH_HOME"
host="$1"; shift
cmdline="$1"
# The space check that runs before every build asks the host how much
# room it has. The fakes below run the command verbatim, so without this
# the check would measure the TEST MACHINE's real disk and refuse the
# build -- a failure that has nothing to do with what the test is about.
# The answer is "plenty of room", which is what a healthy builder looks
# like. FAKE_FREE_GB makes it something else when a test wants to see the
# refusal path.
if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
  script=$(cat)
  case "$script" in
    *bench_fs*)
      echo "==> builder $host: ${FAKE_FREE_GB:-100}G free, need space" >&2
      printf 'OK %s\n' "${FAKE_FREE_GB:-100}"
      exit 0
      ;;
  esac
fi
# Only the build dispatch carries the flock under CALLER; preparatory
# ssh calls (mkdir, fingerprint read, touch, ...) are short-lived and
# must not race the test for the pidfile. A stale PID from a finished
# prep call would have the test "kill" a long-gone process while the
# real build ssh kept running.
if [[ "$cmdline" == *CALLER=* && "$cmdline" == *flock* ]]; then
  echo $$ > "$FAKE_SSH_PIDFILE"
fi
# Real sshd starts the user shell with HOME as CWD, so a command like
# `mkdir -p bench/proj` is relative to HOME. Mirror that: cd into
# FAKE_SSH_HOME so the paths the build expands match the lock path
# the test is reading.
cd "$HOME" || exit 1
# Detach the inner bash in a new session so killing us does not take
# the build with it. The watchdog under test is what must keep the
# build from running unattended -- if the watchdog's TERM ever stops
# reaching the build, tests 2 and 3 below would silently keep running.
setsid bash -c "$cmdline" <&0 &
PID=$!
wait $PID
exit $?
"""


# Records pid and runs the cmdline verbatim. CALLER is *not* substituted:
# whatever value the script put on the wire reaches the inner bash
# unmodified, so a test that reads `$CALLER` from a marker can compare
# it to the fake ssh's pid and catch the bug where `$PPID` expands on
# the wrong machine.
RUN_LOCAL_FAKE_SSH_VERBATIM = r"""#!/bin/bash
: "${FAKE_SSH_PIDFILE:?must be set}"
export HOME="$FAKE_SSH_HOME"
host="$1"; shift
cmdline="$1"
# The space check that runs before every build asks the host how much
# room it has. The fakes below run the command verbatim, so without this
# the check would measure the TEST MACHINE's real disk and refuse the
# build -- a failure that has nothing to do with what the test is about.
# The answer is "plenty of room", which is what a healthy builder looks
# like. FAKE_FREE_GB makes it something else when a test wants to see the
# refusal path.
if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
  script=$(cat)
  case "$script" in
    *bench_fs*)
      echo "==> builder $host: ${FAKE_FREE_GB:-100}G free, need space" >&2
      printf 'OK %s\n' "${FAKE_FREE_GB:-100}"
      exit 0
      ;;
  esac
fi
# Same narrowing as RUN_LOCAL_FAKE_SSH: only the build dispatch carries
# the flock under CALLER. Without it, a test reading the pidfile while
# the build is still running captures the prep ssh's pid instead.
if [[ "$cmdline" == *CALLER=* && "$cmdline" == *flock* ]]; then
  echo $$ > "$FAKE_SSH_PIDFILE"
fi
# Run as a child, not exec'd, so the inner bash's $PPID is us -- this
# is what lets the test compare the recorded CALLER against our pid.
cd "$HOME" || exit 1
bash -c "$cmdline" <&0
exit $?
"""


# Variant of RUN_LOCAL_FAKE_SSH that sleeps 1s before every non-build
# call. The reproducer for the stale-PID bug the reviewer found in
# test_queued_waiter_with_dead_caller_never_runs: on a slow runner the
# prep ssh runs first, exits, and leaves a stale pidfile; the build
# ssh then runs under a different pid. The test reads the pidfile when
# it appears and would capture the prep call's stale pid unless the
# helper only writes for the build invocation. Delaying the prep call
# makes the bug surface deterministically on any machine.
RUN_LOCAL_FAKE_SSH_SLOW_PREP = r"""#!/bin/bash
: "${FAKE_SSH_PIDFILE:?must be set}"
export HOME="$FAKE_SSH_HOME"
host="$1"; shift
cmdline="$1"
# The space check that runs before every build asks the host how much
# room it has. The fakes below run the command verbatim, so without this
# the check would measure the TEST MACHINE's real disk and refuse the
# build -- a failure that has nothing to do with what the test is about.
# The answer is "plenty of room", which is what a healthy builder looks
# like. FAKE_FREE_GB makes it something else when a test wants to see the
# refusal path.
if [[ "$cmdline" == *"BUILDER_SPACE_TREE="*" bash" ]]; then
  script=$(cat)
  case "$script" in
    *bench_fs*)
      echo "==> builder $host: ${FAKE_FREE_GB:-100}G free, need space" >&2
      printf 'OK %s\n' "${FAKE_FREE_GB:-100}"
      exit 0
      ;;
  esac
fi
# Same narrowing as RUN_LOCAL_FAKE_SSH: only the build dispatch writes
# to the pidfile. The sleep on the prep branch is what makes the bug
# the old behaviour hid reproducible here.
if [[ "$cmdline" == *CALLER=* && "$cmdline" == *flock* ]]; then
  echo $$ > "$FAKE_SSH_PIDFILE"
else
  sleep 1
fi
cd "$HOME" || exit 1
setsid bash -c "$cmdline" <&0 &
PID=$!
wait $PID
exit $?
"""


def _manifest_with_timeout(timeout: str) -> str:
    """A gates.toml with `timeout = <value>` on the bench task."""
    return (
        "[[task]]\n"
        'name = "bench"\n'
        'role = "builder"\n'
        'command = "sleep 60"\n'
        f'timeout = {timeout}\n'
    )


def _wait_for_pidfile(path: Path, deadline_s: float = 10.0) -> int:
    """Block until `path` exists, then return the pid written to it."""
    end = time.time() + deadline_s
    while time.time() < end:
        if path.exists():
            try:
                return int(path.read_text().strip())
            except ValueError:
                pass
        time.sleep(0.05)
    raise AssertionError(
        f"fake ssh pidfile {path} did not appear within {deadline_s}s")


def _count_lock_waiters(lock_path: str,
                        exclude_pids: set[int] | None = None) -> int:
    """Count processes whose command line names `lock_path`, skipping
    `exclude_pids`. Scans /proc because `pgrep -cf` counts itself when its
    own argv matches the pattern."""
    exclude = exclude_pids or set()
    needle = lock_path.encode()
    n = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in exclude:
            continue
        try:
            cmdline = entry.joinpath("cmdline").read_bytes()
        except OSError:
            # Pids appear and disappear between iterdir and read; the
            # transient ENOENT is not the test's problem.
            continue
        if needle in cmdline:
            n += 1
    return n


def _read_proc(proc: subprocess.Popen,
               timeout: float = 5.0) -> tuple[str, str]:
    """Drain remote-task's output for a failure message, killing it if it
    stalls past `timeout`."""
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


def _wait_for_queued_waiter(remote_lock: Path, fake_ssh_pid: int,
                            proc: subprocess.Popen, marker: Path,
                            timeout_s: float = 10.0,
                            poll_interval_s: float = 0.05) -> None:
    """Block until the build's waiter is queued on `remote_lock`, failing with
    remote-task's output if the queue never forms, or fake ssh or remote-task
    exits first."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        n = _count_lock_waiters(str(remote_lock),
                                exclude_pids={os.getpid()})
        if n >= 2:
            return
        # Remote-task exited on its own (without us ever asking it to).
        # Whatever it printed is the only signal left about why.
        rt_rc = proc.poll()
        if rt_rc is not None:
            out, err = _read_proc(proc)
            raise AssertionError(
                f"remote-task exited (rc={rt_rc}) before the waiter "
                f"queued on {remote_lock}; stdout={out!r} "
                f"stderr={err!r} marker_present={marker.exists()}")
        # Fake ssh died, so the queue can no longer form: fail now.
        try:
            os.kill(fake_ssh_pid, 0)
        except ProcessLookupError:
            out, err = _read_proc(proc)
            raise AssertionError(
                f"fake ssh (pid={fake_ssh_pid}) died before the waiter "
                f"queued on {remote_lock}; rc={proc.returncode} "
                f"stdout={out!r} stderr={err!r} "
                f"marker_present={marker.exists()}")
        time.sleep(poll_interval_s)
    out, err = _read_proc(proc)
    raise AssertionError(
        f"no waiter queued on {remote_lock} within {timeout_s}s; "
        f"rc={proc.returncode} stdout={out!r} stderr={err!r} "
        f"marker_present={marker.exists()}")


def test_caller_is_captured_on_the_remote_shell():
    """CALLER must expand on the builder, not on the local machine."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        marker = tmp / "caller_marker"
        (proj_root / "gates.toml").write_text(
            "[[task]]\n"
            'name = "bench"\n'
            'role = "builder"\n'
            f'command = "echo $CALLER > {marker}"\n')
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["add", "-A"],
            ["commit", "-q", "-m", "init"],
        ):
            subprocess.run(["git", "-C", str(proj_root), *args],
                           check=True, capture_output=True)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        (fakebin / "rsync").write_text("#!/bin/sh\nexit 0\n")
        (fakebin / "rsync").chmod(0o755)
        (fakebin / "ssh").write_text(RUN_LOCAL_FAKE_SSH_VERBATIM)
        (fakebin / "ssh").chmod(0o755)
        pidfile = tmp / "fake_ssh.pid"
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["HOME"] = str(proj_root)
        env["FAKE_SSH_HOME"] = str(proj_root)
        env["FAKE_SSH_PIDFILE"] = str(pidfile)
        r = subprocess.run([str(REMOTE_TASK), str(proj_root)],
                           capture_output=True, text=True, env=env,
                           timeout=30)
        fake_ssh_pid = _wait_for_pidfile(pidfile)
        assert_pass("exit 0", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("marker was written", marker.exists(),
                    f"stdout={r.stdout!r} stderr={r.stderr!r}")
        recorded = int(marker.read_text().strip())
        assert_pass(
            "recorded CALLER equals the fake ssh's pid "
            "(proof $PPID was expanded on the builder, not locally)",
            recorded == fake_ssh_pid,
            f"recorded={recorded} fake_ssh_pid={fake_ssh_pid} "
            f"-- a bare $PPID on the local side records remote-task's "
            f"parent, not the builder's")


def test_hung_task_is_killed_at_its_limit():
    """A task that exceeds its `[[task]] timeout` ends in FAIL within seconds."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        (proj_root / "gates.toml").write_text(_manifest_with_timeout("2"))
        subprocess.run(["git", "-C", str(proj_root), "init", "-q"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "commit",
                        "-q", "-m", "init"], check=True)
        pidfile = tmp / "fake_ssh.pid"
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        for name in ("rsync", "ssh"):
            (fakebin / name).write_text(
                "#!/bin/sh\nexit 0\n" if name == "rsync"
                else RUN_LOCAL_FAKE_SSH)
            (fakebin / name).chmod(0o755)
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["FAKE_SSH_PIDFILE"] = str(pidfile)
        env["FAKE_SSH_HOME"] = str(proj_root)
        env["HOME"] = str(proj_root)
        proc = subprocess.Popen([str(REMOTE_TASK), str(proj_root)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, env=env)
        _wait_for_pidfile(pidfile)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                f"remote-task did not exit within 30s for a 2s timeout: "
                f"stderr={err!r}")
        assert_pass("non-zero exit on hung task",
                    proc.returncode != 0,
                    f"rc={proc.returncode} stdout={out!r} stderr={err!r}")
        lines = out.splitlines()
        last = lines[-1] if lines else ""
        assert_pass(
            "FAIL line names the timeout",
            last.startswith("==> FAIL (bench") and "timed out after 2s" in last,
            repr(out))
        # Second run as the lock-release proof: a hung task that
        # erroneously returned PASS would also leave the remote lock
        # held for the original 60s, and this run would queue behind
        # it (or in the worst case take its 2s timeout). Together with
        # the verdict line, it pins the actual mechanism.
        proj2_root = tmp / "proj2"
        proj2_root.mkdir()
        (proj2_root / "gates.toml").write_text(_manifest_with_timeout("2"))
        subprocess.run(["git", "-C", str(proj2_root), "init", "-q"],
                       check=True)
        subprocess.run(["git", "-C", str(proj2_root), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(proj2_root), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(proj2_root), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(proj2_root), "commit",
                        "-q", "-m", "init"], check=True)
        env2 = dict(env)
        env2["REMOTE_TASK_LOCK"] = str(tmp / "build2.lock")
        env2["FAKE_SSH_HOME"] = str(proj2_root)
        env2["HOME"] = str(proj2_root)
        second = subprocess.run([str(REMOTE_TASK), str(proj2_root)],
                               capture_output=True, text=True, env=env2,
                               timeout=30)
        assert_pass("second run reaches its own timeout (lock was free)",
                    second.returncode != 0
                    and "timed out after 2s" in second.stdout,
                    repr(second))


def test_queued_waiter_with_dead_caller_never_runs():
    """A waiter in the remote lock whose caller is gone must not run."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        marker = tmp / "marker"
        (proj_root / "gates.toml").write_text(
            "[[task]]\n"
            'name = "bench"\n'
            'role = "builder"\n'
            f'command = "touch {marker}"\n')
        subprocess.run(["git", "-C", str(proj_root), "init", "-q"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "commit",
                        "-q", "-m", "init"], check=True)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        for name in ("rsync", "ssh"):
            (fakebin / name).write_text(
                "#!/bin/sh\nexit 0\n" if name == "rsync"
                else RUN_LOCAL_FAKE_SSH)
            (fakebin / name).chmod(0o755)
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["HOME"] = str(proj_root)
        env["FAKE_SSH_HOME"] = str(proj_root)
        # The remote lock path the build expands is $HOME/bench/.lock.
        # Match the parent's FAKE_SSH_HOME so the test holds the same
        # path the build is queued on.
        remote_lock = proj_root / "bench" / ".lock"
        (proj_root / "bench").mkdir(exist_ok=True)
        # Acquire the remote lock in a background holder so the test
        # process can release it from a different fork.
        holder = subprocess.Popen(
            ["flock", str(remote_lock), "sleep", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline:
            r = subprocess.run(["flock", "-n", str(remote_lock), "true"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode != 0:
                break
            time.sleep(0.05)
        else:
            holder.kill()
            raise AssertionError("holder never acquired the remote lock")
        pidfile = tmp / "fake_ssh.pid"
        env["FAKE_SSH_PIDFILE"] = str(pidfile)
        proc = subprocess.Popen([str(REMOTE_TASK), str(proj_root)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, env=env)
        fake_ssh_pid = _wait_for_pidfile(pidfile)
        # A fixed sleep raced a slow runner: poll until the waiter is queued.
        _wait_for_queued_waiter(remote_lock, fake_ssh_pid, proc, marker)
        try:
            os.kill(fake_ssh_pid, 9)
        except ProcessLookupError:
            out, err = _read_proc(proc)
            raise AssertionError(
                f"fake ssh (pid={fake_ssh_pid}) already gone when the "
                f"test tried to kill it; rc={proc.returncode} "
                f"stdout={out!r} stderr={err!r} "
                f"marker_present={marker.exists()}")
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                f"remote-task did not exit; marker_present={marker.exists()}")
        assert_pass("no marker touched", not marker.exists(),
                    f"marker found; stdout={out!r} stderr={err!r}")
        verdict = next((ln for ln in reversed(out.splitlines())
                        if ln.startswith(("==> PASS", "==> FAIL"))), "")
        assert_pass(
            "no PASS verdict for a waiter whose caller died",
            not verdict.startswith("==> PASS"),
            repr(out))


def test_queued_waiter_survives_slow_prep_call():
    """The dead-caller waiter test holds on a slow prep ssh.

    The fix to RUN_LOCAL_FAKE_SSH narrowed its pidfile write to the build
    invocation, so the test's _wait_for_pidfile call cannot capture a
    short-lived prep ssh that has already exited. This test reproduces the
    bug deterministically by adding a 1s sleep to every non-build ssh
    call and then running the same dead-caller scenario. With the old
    "every ssh writes the pidfile" behaviour, the prep call's stale PID
    would arrive first and _wait_for_pidfile would return it; the test
    would then kill that already-dead PID, the real build ssh would
    never be killed, and the build would complete -- the very
    marker_present=True the reviewer found on CI.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        marker = tmp / "marker"
        (proj_root / "gates.toml").write_text(
            "[[task]]\n"
            'name = "bench"\n'
            'role = "builder"\n'
            f'command = "touch {marker}"\n')
        subprocess.run(["git", "-C", str(proj_root), "init", "-q"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "commit",
                        "-q", "-m", "init"], check=True)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        for name in ("rsync", "ssh"):
            (fakebin / name).write_text(
                "#!/bin/sh\nexit 0\n" if name == "rsync"
                else RUN_LOCAL_FAKE_SSH_SLOW_PREP)
            (fakebin / name).chmod(0o755)
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["HOME"] = str(proj_root)
        env["FAKE_SSH_HOME"] = str(proj_root)
        remote_lock = proj_root / "bench" / ".lock"
        (proj_root / "bench").mkdir(exist_ok=True)
        holder = subprocess.Popen(
            ["flock", str(remote_lock), "sleep", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline:
            r = subprocess.run(["flock", "-n", str(remote_lock), "true"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode != 0:
                break
            time.sleep(0.05)
        else:
            holder.kill()
            raise AssertionError("holder never acquired the remote lock")
        pidfile = tmp / "fake_ssh.pid"
        env["FAKE_SSH_PIDFILE"] = str(pidfile)
        proc = subprocess.Popen([str(REMOTE_TASK), str(proj_root)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, env=env)
        # The pidfile is only written by the build dispatch. With the
        # prep ssh delayed by 1s, the test would have observed it on the
        # old behaviour from the prep call's stale pid. Here the build is
        # the only writer, so what arrives is the build ssh's pid.
        fake_ssh_pid = _wait_for_pidfile(pidfile)
        try:
            os.kill(fake_ssh_pid, 0)
        except ProcessLookupError:
            out, err = _read_proc(proc)
            raise AssertionError(
                f"fake ssh (pid={fake_ssh_pid}) already gone before "
                f"the test could kill it -- the build ssh exited but the "
                f"test read a different pid: rc={proc.returncode} "
                f"stdout={out!r} stderr={err!r}")
        _wait_for_queued_waiter(remote_lock, fake_ssh_pid, proc, marker)
        try:
            os.kill(fake_ssh_pid, 9)
        except ProcessLookupError:
            out, err = _read_proc(proc)
            raise AssertionError(
                f"fake ssh (pid={fake_ssh_pid}) already gone when the "
                f"test tried to kill it; rc={proc.returncode} "
                f"stdout={out!r} stderr={err!r} "
                f"marker_present={marker.exists()}")
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                f"remote-task did not exit; marker_present={marker.exists()}")
        assert_pass("no marker touched even with a 1s-delayed prep ssh",
                    not marker.exists(),
                    f"marker found; stdout={out!r} stderr={err!r}")
        verdict = next((ln for ln in reversed(out.splitlines())
                        if ln.startswith(("==> PASS", "==> FAIL"))), "")
        assert_pass(
            "no PASS verdict for a waiter whose caller died",
            not verdict.startswith("==> PASS"),
            repr(out))


def test_running_task_dies_with_its_caller():
    """Killing the ssh mid-run tears down the build via the watchdog."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        marker = tmp / "marker"
        sleep_pidfile = tmp / "sleep.pid"
        # Write the sleep's own pid to a file so the test can identify
        # which sleep belongs to this run -- a generic pgrep would also
        # match unrelated processes and silently report the wrong pid.
        (proj_root / "gates.toml").write_text(
            "[[task]]\n"
            'name = "bench"\n'
            'role = "builder"\n'
            'command = "echo $$ > ' + str(sleep_pidfile)
            + r'; sleep 60; touch ' + str(marker)
            + r'"' + "\n")
        subprocess.run(["git", "-C", str(proj_root), "init", "-q"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "commit",
                        "-q", "-m", "init"], check=True)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        for name in ("rsync", "ssh"):
            (fakebin / name).write_text(
                "#!/bin/sh\nexit 0\n" if name == "rsync"
                else RUN_LOCAL_FAKE_SSH)
            (fakebin / name).chmod(0o755)
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["HOME"] = str(proj_root)
        env["FAKE_SSH_HOME"] = str(proj_root)
        pidfile = tmp / "fake_ssh.pid"
        env["FAKE_SSH_PIDFILE"] = str(pidfile)
        proc = subprocess.Popen([str(REMOTE_TASK), str(proj_root)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, env=env)
        _wait_for_pidfile(pidfile)
        try:
            _wait_for_pidfile(sleep_pidfile, deadline_s=15)
        except AssertionError:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                f"sleep pidfile never appeared: stdout={out!r} stderr={err!r}")
        sleep_pid = int(sleep_pidfile.read_text().strip())
        fake_ssh_pid = int(pidfile.read_text().strip())
        os.kill(fake_ssh_pid, 9)
        # Within 15s the sleep must be gone. The watchdog polls every
        # 5s, and the inner bash's pgrp is the recipient of its TERM.
        deadline = time.time() + 15
        gone = False
        while time.time() < deadline:
            try:
                os.kill(sleep_pid, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.2)
        try:
            out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                f"remote-task did not exit; sleep_gone={gone} "
                f"stdout={out!r} stderr={err!r}")
        assert_pass("no sleep process remains within 15s",
                    gone,
                    f"sleep pid={sleep_pid} still alive; "
                    f"stdout={out!r} stderr={err!r}")
        assert_pass("no marker touched",
                    not marker.exists(),
                    repr(out))
        verdict = next((ln for ln in reversed(out.splitlines())
                        if ln.startswith(("==> PASS", "==> FAIL"))), "")
        assert_pass("verdict is FAIL",
                    verdict.startswith("==> FAIL"),
                    repr(out))


def test_killing_wrapper_kills_running_task(sig_name: str = "TERM") -> None:
    """A TERM, INT or HUP on the wrapper alone must reach its SSH child.

    The remote watchdog fires only when the SSH session goes, so a wrapper
    that dies leaving ssh connected lets the build finish while holding the
    bench lock.
    """
    sig = {"TERM": signal.SIGTERM,
           "INT": signal.SIGINT,
           "HUP": signal.SIGHUP}[sig_name]
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        start_marker = tmp / "start"
        complete_marker = tmp / "complete"
        # Run sleep 7 -- longer than the watchdog's 5s poll, so a working
        # fix has time to fire the watchdog before the completion marker.
        # Short enough that the test can wait for the marker to NOT appear.
        (proj_root / "gates.toml").write_text(
            "[[task]]\n"
            'name = "bench"\n'
            'role = "builder"\n'
            'command = "touch ' + str(start_marker)
            + r'; sleep 7; touch ' + str(complete_marker)
            + r'"' + "\n"
            'timeout = 20\n'
        )
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["add", "-A"],
            ["commit", "-q", "-m", "init"],
        ):
            subprocess.run(["git", "-C", str(proj_root), *args],
                           check=True, capture_output=True)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        (fakebin / "rsync").write_text("#!/bin/sh\nexit 0\n")
        (fakebin / "rsync").chmod(0o755)
        (fakebin / "ssh").write_text(RUN_LOCAL_FAKE_SSH)
        (fakebin / "ssh").chmod(0o755)
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["HOME"] = str(proj_root)
        env["FAKE_SSH_HOME"] = str(proj_root)
        pidfile = tmp / "fake_ssh.pid"
        env["FAKE_SSH_PIDFILE"] = str(pidfile)
        proc = subprocess.Popen([str(REMOTE_TASK), str(proj_root)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, env=env)
        # Wait for the build to start; the start marker proves the wrapper
        # reached the remote side, so the signal sent after this point
        # exercises the wrapper-to-SSH teardown the watchdog relies on.
        deadline = time.time() + 15
        while not start_marker.exists():
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(
                    f"remote-task died before the build started: "
                    f"rc={proc.returncode} stdout={out!r} stderr={err!r}")
            if time.time() > deadline:
                proc.kill()
                proc.communicate()
                raise AssertionError("start marker never appeared")
            time.sleep(0.05)
        proc.send_signal(sig)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                f"remote-task did not exit on SIG{sig_name}; "
                f"stdout={out!r} stderr={err!r}")
        assert_pass(f"wrapper exits non-zero on SIG{sig_name}",
                    proc.returncode != 0,
                    f"rc={proc.returncode} stdout={out!r} stderr={err!r}")
        # Sleep well past the watchdog's 5s poll + the 7s build sleep so a
        # leak would still surface here.
        time.sleep(10)
        assert_pass("completion marker must NOT be written",
                    not complete_marker.exists(),
                    f"completion marker was written; "
                    f"stdout={out!r} stderr={err!r}")
        # Lock release: a non-blocking flock on the remote bench lock must
        # succeed once the build has been killed. The lock path is what the
        # build expanded -- $FAKE_SSH_HOME/bench/.lock.
        remote_lock = proj_root / "bench" / ".lock"
        deadline = time.time() + 15
        acquired = False
        while time.time() < deadline:
            r = subprocess.run(["flock", "-n", str(remote_lock), "true"],
                              capture_output=True)
            if r.returncode == 0:
                acquired = True
                break
            time.sleep(0.2)
        assert_pass("remote bench lock is released after kill",
                    acquired,
                    f"remote lock {remote_lock} still held; "
                    f"stdout={out!r} stderr={err!r}")


def test_killing_wrapper_with_term() -> None:
    test_killing_wrapper_kills_running_task("TERM")


def test_killing_wrapper_with_int() -> None:
    test_killing_wrapper_kills_running_task("INT")


def test_killing_wrapper_with_hup() -> None:
    test_killing_wrapper_kills_running_task("HUP")


def test_local_lock_wait_is_bounded():
    """`flock -w` on the local lock exits with a message naming the lock."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        (proj_root / "gates.toml").write_text(
            "[[task]]\n"
            'name = "bench"\n'
            'role = "builder"\n'
            'command = "echo hi"\n')
        subprocess.run(["git", "-C", str(proj_root), "init", "-q"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "config",
                        "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(proj_root), "add", "-A"],
                       check=True)
        subprocess.run(["git", "-C", str(proj_root), "commit",
                        "-q", "-m", "init"], check=True)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        for name in ("rsync", "ssh"):
            (fakebin / name).write_text(
                "#!/bin/sh\nexit 0\n" if name == "rsync"
                else RUN_LOCAL_FAKE_SSH)
            (fakebin / name).chmod(0o755)
        lock_path = tmp / "build.lock"
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["HOME"] = str(proj_root)
        env["FAKE_SSH_HOME"] = str(proj_root)
        env["REMOTE_TASK_LOCK"] = str(lock_path)
        env["REMOTE_TASK_LOCK_WAIT"] = "1"
        # The ssh fake requires FAKE_SSH_PIDFILE even though this test
        # exercises the local lock -- remote-task calls ssh earlier
        # (for `mkdir -p bench/proj`) and the fake aborts without the
        # marker. The marker file itself is unused here.
        env["FAKE_SSH_PIDFILE"] = str(tmp / "fake_ssh.pid")
        holder = subprocess.Popen(
            ["flock", str(lock_path), "sleep", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline:
            r = subprocess.run(["flock", "-n", str(lock_path), "true"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode != 0:
                break
            time.sleep(0.05)
        else:
            holder.kill()
            raise AssertionError("holder never acquired the local lock")
        try:
            r = subprocess.run([str(REMOTE_TASK), str(proj_root)],
                               capture_output=True, text=True, env=env,
                               timeout=20)
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait()
        assert_pass("non-zero exit on local lock timeout",
                    r.returncode != 0,
                    repr(r))
        combined = r.stderr + r.stdout
        assert_pass(
            "message names the lock path",
            str(lock_path) in combined,
            repr(r))
        assert_pass(
            "message names the timeout duration",
            "1s" in combined,
            repr(r))
        verdict = next((ln for ln in reversed(r.stdout.splitlines())
                        if ln.startswith(("==> PASS", "==> FAIL"))), "")
        assert_pass(
            "verdict names the local lock",
            "local lock" in verdict and "1s" in verdict,
            repr(r))


def test_remote_lock_wait_is_bounded():
    """`flock -w` on the remote bench lock exits with a message naming it."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        proj_root = tmp / "proj"
        proj_root.mkdir()
        (proj_root / "gates.toml").write_text(
            "[[task]]\n"
            'name = "bench"\n'
            'role = "builder"\n'
            'command = "echo hi"\n')
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["add", "-A"],
            ["commit", "-q", "-m", "init"],
        ):
            subprocess.run(["git", "-C", str(proj_root), *args],
                           check=True, capture_output=True)
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        (fakebin / "rsync").write_text("#!/bin/sh\nexit 0\n")
        (fakebin / "rsync").chmod(0o755)
        (fakebin / "ssh").write_text(RUN_LOCAL_FAKE_SSH)
        (fakebin / "ssh").chmod(0o755)
        env = remote_task_env(proj_root, fakebin, repo_bin=False)
        env["HOME"] = str(proj_root)
        env["FAKE_SSH_HOME"] = str(proj_root)
        env["FAKE_SSH_PIDFILE"] = str(tmp / "fake_ssh.pid")
        env["REMOTE_TASK_LOCK_WAIT"] = "1"
        # The build expands `$HOME/bench/.lock` against FAKE_SSH_HOME.
        # Pre-create the path the test will hold so the holder and the
        # build reach for the same file.
        remote_lock = proj_root / "bench" / ".lock"
        (proj_root / "bench").mkdir(exist_ok=True)
        holder = subprocess.Popen(
            ["flock", str(remote_lock), "sleep", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline:
            r = subprocess.run(["flock", "-n", str(remote_lock), "true"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode != 0:
                break
            time.sleep(0.05)
        else:
            holder.kill()
            raise AssertionError("holder never acquired the remote lock")
        try:
            r = subprocess.run([str(REMOTE_TASK), str(proj_root)],
                               capture_output=True, text=True, env=env,
                               timeout=20)
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait()
        assert_pass("non-zero exit on remote lock timeout",
                    r.returncode != 0,
                    repr(r))
        verdict = next((ln for ln in reversed(r.stdout.splitlines())
                        if ln.startswith(("==> PASS", "==> FAIL"))), "")
        assert_pass(
            "verdict names the timeout duration",
            "1s" in verdict,
            repr(r))
        assert_pass(
            "verdict names the remote bench lock",
            "remote bench lock" in verdict,
            repr(r))


def main() -> int:
    tests = [
        test_failures_have_one_final_verdict,
        test_killed_run_does_not_pass,
        test_reports_drift_before_final_pass,
        test_command_change_and_legacy_sentinel_invalidate,
        test_source_with_spaces_is_one_remote_path,
        test_failed_build_retries_invalidation,
        test_custom_sources_validate_paths,
        test_no_mirrors_is_unchanged,
        test_failing_tool_does_not_fail_run,
        test_missing_tool_is_named,
        test_no_migrations_dir_is_unchanged,
        test_unconfigured_sources_are_discovered,
        test_stray_byte_does_not_abort_discovery,
        test_missing_sources_refuse_and_preserve_fingerprint,
        test_first_run_invalidates_unverified_cache,
        test_matching_fingerprint_no_touch,
        test_changed_migrations_touch_sources,
        test_touch_survives_a_real_rsync,
        test_hung_task_is_killed_at_its_limit,
        test_caller_is_captured_on_the_remote_shell,
        test_queued_waiter_with_dead_caller_never_runs,
        test_queued_waiter_survives_slow_prep_call,
        test_running_task_dies_with_its_caller,
        test_killing_wrapper_with_term,
        test_killing_wrapper_with_int,
        test_killing_wrapper_with_hup,
        test_local_lock_wait_is_bounded,
        test_remote_lock_wait_is_bounded,
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
