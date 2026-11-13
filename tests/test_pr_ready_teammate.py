#!/usr/bin/env python3
"""Pin the teammate check in pr-ready.

The check is optional and off by default. With --teammate it runs a model
of another family than the implementer's over the change, posts what it
finds where the implementer has to answer it, and refuses while any of it
is unanswered.

What is pinned here:

- without --teammate, pr-ready behaves as it always did;
- the candidate the check takes is the one the binding's policy names, and
  a rotation moves on to the next one;
- a binding whose every candidate is in the implementer's family refuses,
  and the refusal names that family;
- a backend that fails, times out or writes nothing readable refuses, and
  the refusal names the backend;
- a finding that cannot be located is dropped and reported, and the rest
  are posted with their markers;
- a head that already carries a teammate review skips the second review;
- a teammate finding nobody has answered refuses by path:line, and the
  same pull request passes once somebody answers it;
- a run with no findings still posts a review, so the head counts as
  checked;
- every run leaves a row in the findings ledger, with the stage and the
  model.

No network: a stub gh answers every call, a stub backend writes canned
findings, and the real finding-check locates them in a real checkout.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_READY = REPO_ROOT / "bin" / "pr-ready"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "tests"))

from roles_fixture import roles_file  # noqa: E402

# The repository the stub gh reports, and the login it acts as.
REPO_SLUG = "example-org/tooling"
LOGIN = "lane"

# The families the binding names. The implementer runs in "a", the owner
# reviews in "b", and "c" is the family neither of them uses.
MODELS = {"model-a": "a", "model-b": "b", "model-c": "c"}


def fail(failures: list[str], msg: str, detail: str = "") -> None:
    failures.append(f"{msg}: {detail}" if detail else msg)


# --------------------------------------------------------------------------
# The bench.

def git_env() -> dict:
    """The environment a commit in the fixture carries.

    The identity is in the environment, so the suite changes no
    configuration anywhere.
    """
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Lane",
        "GIT_AUTHOR_EMAIL": "lane@example.invalid",
        "GIT_COMMITTER_NAME": "Lane",
        "GIT_COMMITTER_EMAIL": "lane@example.invalid",
    })
    return env


def make_repo(tmpdir: Path) -> tuple[Path, str, str]:
    """A checkout with a base commit and a branch that changes a file.

    The remote-tracking ref the tools resolve their base against is
    written by hand: the suite runs no network.
    """
    tmpdir.mkdir(parents=True, exist_ok=True)
    repo = tmpdir / "repo"
    repo.mkdir()
    env = git_env()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, env=env)
    (repo / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"],
                   check=True, env=env)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True,
                          env=env).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "update-ref",
                    "refs/remotes/origin/main", base], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "issue-80"],
                   check=True, env=env)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def total(items):\n    return sum(items)\n")
    subprocess.run(["git", "-C", str(repo), "add", "src/app.py"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"],
                   check=True, env=env)
    oid = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True,
                         env=env).stdout.strip()
    return repo, "issue-80", oid


STUB_GH = r'''#!/usr/bin/env python3
"""A stub gh: every answer comes from a file in $STUB_DATA."""
import json
import os
import re
import subprocess
import sys

DATA = os.environ["STUB_DATA"]
ARGS = sys.argv[1:]


def read(name, default=""):
    path = os.path.join(DATA, name)
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read()
    return default


def jread(name, default):
    text = read(name, "")
    if not text.strip():
        return default
    return json.loads(text)


with open(os.path.join(DATA, "gh.log"), "a") as fh:
    fh.write(" ".join(ARGS) + "\n")

if ARGS[:1] == ["repo"]:
    print(read("repo_slug", "example-org/tooling").strip())
    sys.exit(0)

if ARGS[:1] == ["pr"]:
    what = ARGS[1]
    number = next((a for a in ARGS[2:] if a.isdigit()), "0")
    if what == "view":
        payload = jread("pr_view_%s.json" % number,
                        jread("pr_view.json", {}))
        query = ""
        previous = ""
        for arg in ARGS:
            if previous == "--jq":
                query = arg
            previous = arg
        text = json.dumps(payload)
        if query:
            out = subprocess.run(["jq", "-r", query], input=text,
                                 capture_output=True, text=True)
            sys.stdout.write(out.stdout)
        else:
            print(text)
        sys.exit(0)
    if what == "checks":
        print(read("pr_checks.json", "[]"))
        sys.exit(0)
    if what == "comment":
        previous = ""
        for arg in ARGS:
            if previous == "--body-file":
                with open(os.path.join(DATA, "comment_body.txt"), "w") as fh:
                    fh.write(read(arg))
            previous = arg
        sys.exit(0)
    if what == "ready":
        with open(os.path.join(DATA, "ready_called"), "w") as fh:
            fh.write(number)
        sys.exit(0)
    sys.exit(0)

if ARGS[:1] == ["api"]:
    endpoint = ARGS[1]
    if endpoint == "user":
        print(read("login", "lane").strip())
        sys.exit(0)
    if endpoint == "graphql":
        print(read("threads.json", json.dumps({
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [], "pageInfo": {"hasNextPage": False}}}}}})))
        sys.exit(0)
    if endpoint.endswith("/actions/workflows"):
        print(read("workflow_count", "0").strip())
        sys.exit(0)
    posted = re.search(r"/pulls/(\d+)/reviews$", endpoint)
    if posted:
        if "POST" in ARGS:
            previous = ""
            for arg in ARGS:
                if previous == "--input":
                    with open(os.path.join(DATA, "posted_review.json"),
                              "w") as fh:
                        fh.write(read(arg))
                previous = arg
            print(json.dumps({"id": 4242, "html_url": "",
                              "user": {"login": read("login", "lane").strip()}}))
            sys.exit(0)
        print(read("reviews_%s.json" % posted.group(1),
                   read("reviews.json", "[]")))
        sys.exit(0)
    inline = re.search(r"/pulls/(\d+)/comments$", endpoint)
    if inline:
        print(read("inline_comments_%s.json" % inline.group(1),
                   read("inline_comments.json", "[]")))
        sys.exit(0)
    issue = re.search(r"/issues/(\d+)/comments$", endpoint)
    if issue:
        print(read("issue_comments_%s.json" % issue.group(1),
                   read("issue_comments.json", "[]")))
        sys.exit(0)
    print("[]")
    sys.exit(0)

if ARGS[:1] == ["run"]:
    print(read("run_%s.json" % ARGS[2], '{"jobs":[]}'))
    sys.exit(0)

sys.exit(0)
'''

STUB_BACKEND = r'''#!/usr/bin/env bash
# A stub backend: it writes the findings the scenario canned to the file
# it is given. The argument names the behaviour: fail, slow, garbage, or
# anything else, which writes the canned findings.
printf '%s\n' "$1" >> "$STUB_DATA/backend_ran"
case "$1" in
fail)
	echo "the backend refused" >&2
	exit 3
	;;
slow)
	sleep 30
	;;
garbage)
	printf 'not json at all\n' > "$3"
	exit 0
	;;
esac
if [ -f "$STUB_DATA/findings.json" ]; then
	cp "$STUB_DATA/findings.json" "$3"
else
	printf '[]\n' > "$3"
fi
'''

STUB_GATE_VERIFY = r'''#!/usr/bin/env bash
case "$2" in
--list)
	echo '  bench       remote-task $REPO'
	;;
*)
	echo "==> all gates passed: bench"
	;;
esac
'''


def write_stubs(bin_dir: Path) -> Path:
    """The stubs a run needs: gh, the backend, and gate-verify."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(STUB_GH)
    gh.chmod(0o755)
    gate = bin_dir / "gate-verify"
    gate.write_text(STUB_GATE_VERIFY)
    gate.chmod(0o755)
    backend = bin_dir / "backend.sh"
    backend.write_text(STUB_BACKEND)
    backend.chmod(0o755)
    return backend


def write_binding(path: Path, backend: Path, *, candidates, policy="fixed",
                  models=None, owner_model=None, backends=None) -> Path:
    """A binding the check reads: the candidates, the policy, the backends."""
    kinds = backends if backends is not None else {
        "stub": {"command": f"bash {backend} {{arg}} {{prompt}} {{out}}",
                 "timeout": 60},
    }
    return roles_file(
        path,
        owners={"example-org": "the-owner"},
        logins=(LOGIN,),
        models=models if models is not None else MODELS,
        teammate=True,
        policy=policy,
        candidates=candidates,
        backends=kinds,
        owner_model=owner_model,
    )


def install(data_dir: Path, oid: str, branch: str, *prs: int) -> None:
    """The fixtures a run reads: the pull request, its checks, the login."""
    data_dir.mkdir(parents=True, exist_ok=True)
    view = {"state": "OPEN", "isDraft": True, "headRefName": branch,
            "headRefOid": oid, "mergeable": "MERGEABLE"}
    for pr in prs or (42,):
        (data_dir / f"pr_view_{pr}.json").write_text(json.dumps(view))
    shutil.copy(FIXTURES / "gh_pr_checks_pass.json",
                data_dir / "pr_checks.json")
    (data_dir / "login").write_text(LOGIN + "\n")
    (data_dir / "repo_slug").write_text(REPO_SLUG + "\n")


def run_pr_ready(worktree: Path, bin_dir: Path, data_dir: Path,
                 binding: Path, *extra: str, pr: int = 42,
                 state: Path | None = None,
                 ledger: Path | None = None) -> subprocess.CompletedProcess:
    """Run pr-ready with the stubs first on PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_DATA"] = str(data_dir)
    # A finder left uninstalled: the prose passes are beside this suite's
    # subject.
    env["CONDENSE_PROSE"] = str(data_dir / "no-finder.py")
    env["ROLES_CONFIG"] = str(binding)
    env["XDG_STATE_HOME"] = str(state or data_dir / "state")
    env["FINDINGS_LEDGER"] = str(ledger or data_dir / "ledger.jsonl")
    env["HOME"] = str(data_dir)
    env["TOOLS_ENV"] = str(data_dir / "no-env")
    env["AGENT_STATE_DIR"] = str(data_dir / "agent-state")
    for var in ("GH_IMPLEMENTER_TOKEN", "GH_IMPLEMENTER_CONFIG_DIR",
                "PR_READY_BASE", "GH_TOKEN", "GH_CONFIG_DIR"):
        env.pop(var, None)
    return subprocess.run(
        ["bash", str(PR_READY), str(pr), str(worktree), "--no-condense",
         *extra],
        capture_output=True, text=True, env=env)


def backend_ran(data_dir: Path) -> list[str]:
    """The arguments the backend was run with, in order."""
    path = data_dir / "backend_ran"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line]


def posted_review(data_dir: Path) -> dict:
    """The review the stub gh was asked to post."""
    path = data_dir / "posted_review.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def ledger_rows(ledger: Path) -> list[dict]:
    """The rows the findings ledger holds."""
    if not ledger.exists():
        return []
    rows = []
    for line in ledger.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# The findings a scenario posts: one in the file and one absent, so the
# check has something to drop.
TWO_FINDINGS = [
    {
        "severity": "high",
        "path": "src/app.py",
        "line": 1,
        "snippet": "def total(items):",
        "claim": "the total is never checked, so a bad item passes",
        "settled_by": "the caller asserts on it",
        "recommendation": "assert on the total before returning it",
    },
    {
        "severity": "low",
        "path": "src/app.py",
        "line": 99,
        "snippet": "a line that is not in the file at all",
        "claim": "a finding about code that is not there",
        "settled_by": "nothing settles it",
        "recommendation": "nothing fixes it",
    },
]


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        worktree, branch, oid = make_repo(tmp / "src")
        bin_dir = tmp / "bin"
        backend = write_stubs(bin_dir)

        # Scenario 1: without --teammate, pr-ready behaves as it always
        # did: it promotes, and nothing of the teammate's happens.
        data = tmp / "data_without_teammate"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_without_teammate.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding)
        if r.returncode != 0:
            fail(failures, "without --teammate: pr-ready refused",
                 f"rc={r.returncode} stderr={r.stderr.strip()!r}")
        elif not (data / "ready_called").exists():
            fail(failures, "without --teammate: the PR was not promoted")
        if backend_ran(data):
            fail(failures, "without --teammate: the backend ran",
                 str(backend_ran(data)))
        if ledger_rows(data / "ledger.jsonl"):
            fail(failures, "without --teammate: the ledger holds a row")

        # Scenario 2: the implementer runs in family "a", the owner
        # reviews in family "b", and the candidates are in "a", "b" and
        # "c". The candidate in the implementer's family is skipped and
        # the one that differs from the owner's comes first, so "c" runs.
        data = tmp / "data_candidate"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_candidate.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-a"},
                        {"backend": "stub:two", "model": "model-b"},
                        {"backend": "stub:three", "model": "model-c"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate", "--implementer-model", "model-a")
        ran = backend_ran(data)
        if ran != ["three"]:
            fail(failures, "candidate: the backend that ran", str(ran))
        if r.returncode != 0:
            fail(failures, "candidate: the run refused",
                 f"rc={r.returncode} stderr={r.stderr.strip()!r}")

        # Scenario 3: a named backend is the one that runs, whatever the
        # policy would have picked.
        data = tmp / "data_named_backend"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_named.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"},
                        {"backend": "stub:two", "model": "model-c"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate=stub:two", "--implementer-model", "model-a")
        ran = backend_ran(data)
        if ran != ["two"]:
            fail(failures, "named backend: the backend that ran", str(ran))

        # An unknown backend is refused by name.
        data = tmp / "data_unknown_backend"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_unknown.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate=stub:nine", "--implementer-model", "model-a")
        if r.returncode == 0:
            fail(failures, "unknown backend: the run passed",
                 f"stderr={r.stderr.strip()!r}")
        elif "stub:nine" not in r.stderr:
            fail(failures, "unknown backend: the refusal does not name it",
                 r.stderr.strip())
        if backend_ran(data):
            fail(failures, "unknown backend: the backend ran",
                 str(backend_ran(data)))

        # The model the implementer ran is required: the teammate has to
        # be of another family than it.
        data = tmp / "data_no_implementer_model"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_no_model.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding, "--teammate")
        if r.returncode == 0:
            fail(failures, "no implementer model: the run passed",
                 f"stderr={r.stderr.strip()!r}")
        elif "--implementer-model" not in r.stderr:
            fail(failures, "no implementer model: the refusal does not "
                 "name the flag", r.stderr.strip())

        # Scenario 4: a rotation takes the next candidate, and the
        # position is kept between runs on two pull requests.
        data_a = tmp / "data_rotation_a"
        data_b = tmp / "data_rotation_b"
        install(data_a, oid, branch, 42)
        install(data_b, oid, branch, 43)
        binding = write_binding(
            tmp / "roles_rotation.toml", backend, policy="round-robin",
            candidates=[{"backend": "stub:two", "model": "model-b"},
                        {"backend": "stub:three", "model": "model-c"}],
            owner_model="model-b")
        state = tmp / "state"
        run_pr_ready(worktree, bin_dir, data_a, binding, "--teammate",
                     "--implementer-model", "model-a", pr=42, state=state)
        run_pr_ready(worktree, bin_dir, data_b, binding, "--teammate",
                     "--implementer-model", "model-a", pr=43, state=state)
        ran = backend_ran(data_a) + backend_ran(data_b)
        if ran != ["three", "two"]:
            fail(failures, "rotation: the backends that ran, in order",
                 str(ran))

        # Scenario 4: every candidate is in the implementer's family. The
        # refusal names that family.
        data = tmp / "data_no_candidate"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_no_candidate.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-a"},
                        {"backend": "stub:two", "model": "model-a"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate", "--implementer-model", "model-a")
        if r.returncode == 0:
            fail(failures, "no candidate: the run passed",
                 f"stderr={r.stderr.strip()!r}")
        elif "family" not in r.stderr or "'a'" not in r.stderr:
            fail(failures, "no candidate: the refusal does not name the "
                 "implementer's family", r.stderr.strip())
        if backend_ran(data):
            fail(failures, "no candidate: the backend ran",
                 str(backend_ran(data)))

        # Scenario 5: a backend that fails, one that times out and one
        # that writes nothing readable each refuse, and each refusal
        # names the backend.
        for kind, wanted in (("fail", "exited"), ("slow", "timed out"),
                              ("garbage", "not JSON")):
            data = tmp / f"data_backend_{kind}"
            install(data, oid, branch)
            binding = write_binding(
                tmp / f"roles_backend_{kind}.toml", backend,
                candidates=[{"backend": f"{kind}:x", "model": "model-b"}],
                owner_model="model-b",
                backends={
                    kind: {"command": f"bash {backend} {kind} {{prompt}} "
                                      f"{{out}}",
                           "timeout": 2},
                })
            r = run_pr_ready(worktree, bin_dir, data, binding,
                             "--teammate", "--implementer-model", "model-a")
            if r.returncode == 0:
                fail(failures, f"backend {kind}: the run passed",
                     f"stderr={r.stderr.strip()!r}")
                continue
            if f"{kind}:x" not in r.stderr:
                fail(failures, f"backend {kind}: the refusal does not name "
                     f"the backend", r.stderr.strip())
            if wanted not in r.stderr:
                fail(failures, f"backend {kind}: the refusal does not say "
                     f"what happened", r.stderr.strip())

        # Scenario 6: two findings, one of which cannot be located. The
        # other is posted with its markers, and the dropped one is
        # reported.
        data = tmp / "data_findings"
        install(data, oid, branch)
        (data / "findings.json").write_text(json.dumps(TWO_FINDINGS))
        binding = write_binding(
            tmp / "roles_findings.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate", "--implementer-model", "model-a")
        review = posted_review(data)
        comments = review.get("comments") or []
        if len(comments) != 1:
            fail(failures, "findings: the comments posted",
                 f"{len(comments)}: {json.dumps(review)[:400]}")
        else:
            body = comments[0].get("body") or ""
            if "<!-- teammate-finding id=" not in body:
                fail(failures, "findings: the comment carries no finding "
                     "marker", body)
            if "src/app.py" not in comments[0].get("path", ""):
                fail(failures, "findings: the comment is on another path",
                     str(comments[0].get("path")))
        head_marker = f"head={oid}"
        if head_marker not in (review.get("body") or ""):
            fail(failures, "findings: the review body carries no head "
                 "marker", review.get("body") or "")
        if "dropped a finding" not in r.stderr:
            fail(failures, "findings: the dropped finding was not reported",
                 r.stderr.strip())

        # Scenario 7: a head that already carries a teammate review skips
        # the second review.
        data = tmp / "data_already_reviewed"
        install(data, oid, branch)
        (data / "reviews.json").write_text(json.dumps([{
            "id": 99,
            "user": {"login": LOGIN},
            "submitted_at": "2026-01-02T00:00:00Z",
            "body": f"<!-- teammate backend=stub:one model=model-b "
                    f"head={oid} -->\n\nThe teammate check raised no "
                    f"findings.\n",
        }]))
        binding = write_binding(
            tmp / "roles_already.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate", "--implementer-model", "model-a")
        if backend_ran(data):
            fail(failures, "already reviewed: the backend ran again",
                 str(backend_ran(data)))
        if r.returncode != 0:
            fail(failures, "already reviewed: the run refused",
                 f"rc={r.returncode} stderr={r.stderr.strip()!r}")
        elif not (data / "ready_called").exists():
            fail(failures, "already reviewed: the PR was not promoted")

        # Scenario 8: a teammate finding nobody has answered refuses, by
        # path:line, and the same pull request passes once somebody has
        # answered it.
        data = tmp / "data_unanswered"
        install(data, oid, branch)
        (data / "reviews.json").write_text(json.dumps([{
            "id": 99,
            "user": {"login": LOGIN},
            "submitted_at": "2026-01-02T00:00:00Z",
            "body": f"<!-- teammate backend=stub:one model=model-b "
                    f"head={oid} -->\n\nThe teammate check raised one "
                    f"finding.\n",
        }]))
        thread = {
            "isResolved": False,
            "path": "src/app.py",
            "line": 7,
            "originalLine": 7,
            "comments": {
                "totalCount": 1,
                "nodes": [{"body": "**high** <!-- teammate-finding "
                                   "id=abc123def456 -->\n\nthe claim\n"}],
            },
        }
        (data / "threads.json").write_text(json.dumps({
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [thread],
                "pageInfo": {"hasNextPage": False}}}}}}))
        binding = write_binding(
            tmp / "roles_unanswered.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate", "--implementer-model", "model-a")
        if r.returncode == 0:
            fail(failures, "unanswered: the run passed",
                 f"stderr={r.stderr.strip()!r}")
        elif "src/app.py:7" not in r.stderr:
            fail(failures, "unanswered: the refusal does not name the "
                 "thread", r.stderr.strip())
        if (data / "ready_called").exists():
            fail(failures, "unanswered: the PR was promoted")

        # The same pull request, once the finding has been answered.
        thread["comments"]["totalCount"] = 2
        thread["comments"]["nodes"].append({"body": "answered"})
        (data / "threads.json").write_text(json.dumps({
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [thread],
                "pageInfo": {"hasNextPage": False}}}}}}))
        (data / "ready_called").unlink(missing_ok=True)
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate", "--implementer-model", "model-a")
        if r.returncode != 0:
            fail(failures, "answered: the run refused",
                 f"rc={r.returncode} stderr={r.stderr.strip()!r}")
        elif not (data / "ready_called").exists():
            fail(failures, "answered: the PR was not promoted")

        # Scenario 9: --check runs the check and posts nothing: a verdict
        # that changes nothing on the pull request.
        data = tmp / "data_dry_run"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_dry.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding, "--check",
                         "--teammate", "--implementer-model", "model-a")
        if posted_review(data):
            fail(failures, "dry run: a review was posted",
                 json.dumps(posted_review(data)))
        if ledger_rows(data / "ledger.jsonl"):
            fail(failures, "dry run: the ledger holds a row")
        if r.returncode != 0:
            fail(failures, "dry run: the run refused",
                 f"rc={r.returncode} stderr={r.stderr.strip()!r}")

        # Scenario 10: a run with no findings still posts a review, so the
        # head counts as checked, and pr-ready passes.
        data = tmp / "data_no_findings"
        install(data, oid, branch)
        binding = write_binding(
            tmp / "roles_no_findings.toml", backend,
            candidates=[{"backend": "stub:one", "model": "model-b"}],
            owner_model="model-b")
        r = run_pr_ready(worktree, bin_dir, data, binding,
                         "--teammate", "--implementer-model", "model-a")
        review = posted_review(data)
        if not review:
            fail(failures, "no findings: no review was posted")
        else:
            if review.get("comments"):
                fail(failures, "no findings: the review carries comments",
                     json.dumps(review.get("comments")))
            if f"head={oid}" not in (review.get("body") or ""):
                fail(failures, "no findings: the review carries no head "
                     "marker", review.get("body") or "")
        if r.returncode != 0:
            fail(failures, "no findings: the run refused",
                 f"rc={r.returncode} stderr={r.stderr.strip()!r}")
        elif not (data / "ready_called").exists():
            fail(failures, "no findings: the PR was not promoted")

        # Scenario 11: the run leaves a row in the findings ledger, with
        # the stage and the model.
        rows = ledger_rows(data / "ledger.jsonl")
        if not rows:
            fail(failures, "ledger: the run left no row")
        else:
            row = rows[-1]
            if row.get("loop_stage") != "teammate":
                fail(failures, "ledger: the stage", str(row.get("loop_stage")))
            if row.get("model") != "model-b":
                fail(failures, "ledger: the model", str(row.get("model")))
            if row.get("implementer_model") != "model-a":
                fail(failures, "ledger: the implementer's model",
                     str(row.get("implementer_model")))
            if row.get("pr") != 42:
                fail(failures, "ledger: the pull request",
                     str(row.get("pr")))

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS the teammate check: off by default; the binding's policy "
          "picks the candidate; a backend that fails is refused by name; "
          "a finding that cannot be located is dropped; a head that "
          "already carries a review is not reviewed again; an unanswered "
          "finding refuses by path:line; every run is recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
