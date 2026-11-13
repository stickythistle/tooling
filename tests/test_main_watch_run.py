#!/usr/bin/env python3
"""Pin the caller that watches `main` and reports the breaks.

The run is driven end to end: a stub `gh` and a stub `main-watch` on
PATH, and a local http.server standing in for Telegram. Nothing
reaches the network, and the real `~/.local/state` is never touched --
`XDG_STATE_HOME` points at a temporary directory.

The cases are the ones the issue separates: a break is reported once
and only once, a pending commit holds the position, a red run is one
message and not one per commit, a failed fetch or a failed send exit
non-zero and move nothing, and every missing piece of configuration is
refused by name.

Run directly: `python3 tests/test_main_watch_run.py`.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "bin" / "main-watch-run"

# The recorded responses the stub answers with. Each names the command
# it was recorded from, and each is trimmed to the fields the tool
# reads.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "main_watch_run"

# The token the stub Telegram is given. A leaky run is obvious in the
# diff, so it is long and looks like a token.
TEST_TOKEN = "test-token-main-watch-run-0c7e1f9a4b2d"
TEST_CHAT = "123456789"

# The token the stub relay is given. It is the host's own client
# token, so it is a secret of the same kind as the bot token.
TEST_RELAY_TOKEN = "test-token-main-watch-relay-3f9a1c7e5b2d"


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")


# ----- the Telegram stub ---------------------------------------------------

class TelegramStub:
    """A local http.server that records the messages sent to it.

    `status` is the HTTP status every request gets, so a test can make
    the send fail. The messages are recorded with the path they came
    in on, so a test can check that the token was in it (it has to be)
    and that the body named what it should.
    """

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object, **_k: object) -> None:
                return  # silence stderr noise during tests

            def do_POST(self) -> None:  # noqa: N802 -- http.server contract
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                fields = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
                outer.requests.append({
                    "path": self.path,
                    "chat_id": (fields.get("chat_id") or [""])[0],
                    "text": (fields.get("text") or [""])[0],
                })
                body = json.dumps({"ok": outer.status == 200}).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.port = port
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def messages(self) -> list[str]:
        return [r["text"] for r in self.requests]


# ----- the relay stub -------------------------------------------------------

class RelayStub:
    """A local http.server that stands in for a notify relay.

    `statuses` is the list of statuses it answers with, one per
    request, so a relay that is scripted to fail once and then work
    answers 500 and then 202. The last status repeats when there are
    more requests than statuses, and `default` answers the requests
    past the end of the list.

    Every request is recorded with its URL, its authorization header
    and its body, so a test can check where the token went.
    """

    def __init__(self, statuses: list[int] | None = None,
                 default: int = 202) -> None:
        self.statuses = list(statuses or [])
        self.default = default
        self.requests: list[dict] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object, **_k: object) -> None:
                return  # silence stderr noise during tests

            def do_POST(self) -> None:  # noqa: N802 -- http.server contract
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                host = self.headers.get("Host", "")
                outer.requests.append({
                    "url": f"http://{host}{self.path}",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "content_type": self.headers.get("Content-Type", ""),
                    "body": raw.decode("utf-8", "replace"),
                })
                index = len(outer.requests) - 1
                if index < len(outer.statuses):
                    status = outer.statuses[index]
                else:
                    status = outer.default
                body = b'{"accepted": true}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.port = port
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def texts(self) -> list[str]:
        """The text of every message that came in."""
        out = []
        for request in self.requests:
            try:
                payload = json.loads(request["body"])
            except json.JSONDecodeError:
                out.append("")
                continue
            out.append(payload.get("text", "")
                       if isinstance(payload, dict) else "")
        return out


def closed_port_url() -> str:
    """A URL on a port with nothing listening on it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# ----- the gh and main-watch stubs ------------------------------------------

# The stub `gh` counts its positional arguments the way `gh api` does
# and refuses more than one. `gh api` takes the endpoint as its only
# positional argument, so a call that passes the ref as a second one
# dies before it reaches the API. A stub that ignored the extra
# arguments would answer a call no real `gh` accepts, and the defect
# it hides is the one this stub is here to catch.
GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_DATA/gh.log"
subcommand="$1"
shift
positionals=()
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *) positionals+=("$arg") ;;
  esac
done
if [[ "$(cat "$STUB_DATA/strict_argv")" == "yes" && ${#positionals[@]} -ne 1 ]]; then
  echo "accepts 1 arg(s), received ${#positionals[@]}" >&2
  exit 1
fi
endpoint="${positionals[0]}"
fails_for="$(cat "$STUB_DATA/fails_for")"
if [[ -n "$fails_for" && "$endpoint" == *"$fails_for"* ]]; then
  echo "gh: could not reach the API" >&2
  exit 1
fi
if [[ "$(cat "$STUB_DATA/gh_fails")" == "yes" ]]; then
  echo "gh: could not reach the API" >&2
  exit 1
fi
case "$endpoint" in
  */compare/*)
    python3 "$STUB_DATA/compare.py" "$endpoint"
    ;;
  */pulls)
    python3 "$STUB_DATA/pulls.py" "$endpoint"
    ;;
  */commits\\?*)
    # The commits on a branch, named by a query string.
    python3 "$STUB_DATA/commits.py" "$endpoint" "$@"
    ;;
  */commits/main)
    # The head of a branch: one commit object.
    python3 "$STUB_DATA/head.py" "$endpoint"
    ;;
  */commits/*)
    # One commit, with the parents the caller walks.
    python3 "$STUB_DATA/commit.py" "$endpoint"
    ;;
  */commits)
    python3 "$STUB_DATA/commits.py" "$endpoint" "$@"
    ;;
  *)
    echo "stub gh: unhandled: $*" >&2
    exit 1
    ;;
esac
"""

# The head of a branch is one commit object, as the recorded response
# is. The stub answers with the recorded response, so the shape it
# gives the tool is the shape GitHub gives it.
HEAD_STUB = """import json, os, sys
data = os.environ["STUB_DATA"]
recorded = json.load(open(data + "/gh_api_commits_main.json"))
recorded.pop("_source", None)
sha = open(data + "/head_sha").read().strip()
if sha:
    recorded["sha"] = sha
print(json.dumps(recorded))
"""

# The commits endpoint answers with the commits the stub was told
# about, newest first. `--slurp` wraps the page in an outer array, the
# way `gh api --paginate --slurp` does.
COMMITS_STUB = """import json, os, sys
data = os.environ["STUB_DATA"]
commits = json.load(open(data + "/commits.json"))
window = int(open(data + "/window").read().strip() or 0)
if window > 0:
    commits = commits[:window]
if "--slurp" in sys.argv:
    print(json.dumps([commits]))
else:
    print(json.dumps(commits))
"""

# The compare endpoint answers with the recorded response, trimmed to
# the fields the tool reads. A test can give it a list of its own, and
# it can make it fail the way the API fails: a 404 for a commit the
# endpoint does not know, and a 502 for a gateway that is down.
COMPARE_STUB = """import json, os, sys
data = os.environ["STUB_DATA"]
endpoint = sys.argv[1]
failure = open(data + "/compare_failure").read().strip()
if failure == "404":
    # The recorded body of the 404 the endpoint answers for a commit
    # it does not know, and the message `gh` prints beside it.
    recorded = json.load(open(data + "/gh_api_compare_404.json"))
    recorded.pop("_source", None)
    print(json.dumps(recorded))
    sys.stderr.write("gh: Not Found (HTTP 404)\\n")
    sys.exit(1)
if failure == "502":
    sys.stderr.write("gh: Bad Gateway (HTTP 502)\\n")
    sys.exit(1)
recorded = json.load(open(data + "/gh_api_compare.json"))
recorded.pop("_source", None)
try:
    between = json.load(open(data + "/compare_commits.json"))
except FileNotFoundError:
    commits = json.load(open(data + "/commits.json"))
    base = endpoint.split("/compare/")[1].split("...")[0]
    shas = [c["sha"] for c in commits]
    # The commits between the stored commit and the head, oldest
    # first. The list on `main` is newest first, so the newer commits
    # are the ones before the stored commit in it.
    start = shas.index(base) if base in shas else len(shas)
    between = [{"sha": s} for s in reversed(shas[:start])]
recorded["commits"] = between
recorded["ahead_by"] = len(between)
status = open(data + "/compare_status").read().strip()
recorded["status"] = status
total = open(data + "/compare_total").read().strip()
recorded["total_commits"] = int(total) if total else len(between)
print(json.dumps(recorded))
"""

# The pull requests a commit came from: the recorded response, trimmed
# to the fields the tool reads.
PULLS_STUB = """import json, os, sys
data = os.environ["STUB_DATA"]
recorded = json.load(open(data + "/gh_api_commits_pulls.json"))
recorded.pop("_source", None)
pulls = recorded["pulls"]
sha = sys.argv[1].split("/commits/")[-1].split("/")[0]
canned = {}
try:
    canned = json.load(open(data + "/pulls.json"))
except FileNotFoundError:
    canned = {}
print(json.dumps(canned.get(sha, pulls)))
"""

# The one-commit endpoint answers with a commit and its parents, the
# shape the real `main-watch` walks.
COMMIT_STUB = """import json, os, sys
data = os.environ["STUB_DATA"]
commits = json.load(open(data + "/commits.json"))
sha = sys.argv[1].split("/commits/")[-1].split("/")[0]
shas = [c["sha"] for c in commits]
index = shas.index(sha) if sha in shas else len(shas)
parents = [{"sha": s} for s in shas[index + 1:index + 2]]
print(json.dumps({"sha": sha, "parents": parents}))
"""

# The stub `main-watch` answers from the canned verdicts, and records
# what it was asked about.
WATCH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_DATA/main-watch.log"
sha="$2"
python3 "$STUB_DATA/verdict.py" "$sha"
"""

VERDICT_STUB = """import json, os, sys
verdicts = json.load(open(os.environ["STUB_DATA"] + "/verdicts.json"))
sha = sys.argv[1]
if sha not in verdicts:
    sys.stderr.write("stub main-watch: no verdict for " + sha + "\\n")
    sys.exit(1)
print(json.dumps(verdicts[sha]))
"""


def write_stubs(tmp: Path, *, commits: list[str], verdicts: dict,
                pulls: dict, gh_fails: bool = False,
                window: int = 0, fails_for: str = "",
                strict_argv: bool = True,
                compare_status: str = "ahead", compare_total: str = "",
                compare_failure: str = "",
                compare_commits: list[str] | None = None,
                head: str = "") -> Path:
    """Put a stub `gh` and a stub `main-watch` on PATH.

    `commits` is the list of shas on `main`, newest first, as the API
    prints it. `verdicts` maps a sha to the verdict `main-watch` reports
    for it. `pulls` maps a sha to the pull requests it came from.

    The stub answers the recorded responses, and these change what it
    answers with: `window` is how many commits the commits endpoint
    returns, `head` is the sha it answers with, `compare_status` is
    what the compare endpoint answers (`ahead`, `identical` or
    `diverged`), `compare_commits` is the list it returns, and
    `compare_total` is the number of commits it counted when that is
    more than the list. `compare_failure` makes it fail the way the API
    fails: `404` for a commit it does not know, `502` for a gateway
    that is down. `fails_for` names the endpoint whose call fails, and
    `strict_argv` refuses a `gh api` call with more than one positional
    argument, the way the real `gh` does.
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    data_dir = tmp / "data"
    data_dir.mkdir(exist_ok=True)

    (data_dir / "commits.json").write_text(
        json.dumps([{"sha": sha} for sha in commits]))
    (data_dir / "verdicts.json").write_text(json.dumps(verdicts))
    (data_dir / "pulls.json").write_text(json.dumps(pulls))
    # The file's *contents* decide whether the stub fails: the file is
    # always there, so testing that it exists, or that it is not
    # empty, fails every run.
    (data_dir / "gh_fails").write_text("yes" if gh_fails else "no")
    (data_dir / "window").write_text(str(window))
    (data_dir / "fails_for").write_text(fails_for)
    (data_dir / "strict_argv").write_text("yes" if strict_argv else "no")
    (data_dir / "compare_status").write_text(compare_status)
    (data_dir / "compare_total").write_text(compare_total)
    (data_dir / "compare_failure").write_text(compare_failure)
    # The head of `main` is the first commit of the list, unless the
    # test names another.
    (data_dir / "head_sha").write_text(head or (commits[0] if commits else ""))
    if compare_commits is not None:
        (data_dir / "compare_commits.json").write_text(
            json.dumps([{"sha": sha} for sha in compare_commits]))

    (data_dir / "commits.py").write_text(COMMITS_STUB)
    (data_dir / "commit.py").write_text(COMMIT_STUB)
    (data_dir / "compare.py").write_text(COMPARE_STUB)
    (data_dir / "pulls.py").write_text(PULLS_STUB)
    (data_dir / "verdict.py").write_text(VERDICT_STUB)
    (data_dir / "head.py").write_text(HEAD_STUB)

    # The recorded responses the stub answers with. Each names the
    # command it was recorded from, and each is trimmed to the fields
    # the tool reads.
    for name in ("gh_api_commits_main.json", "gh_api_compare.json",
                 "gh_api_commits_pulls.json", "gh_api_compare_404.json"):
        (data_dir / name).write_text(
            (FIXTURES / name).read_text())

    gh = bin_dir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)

    watch = bin_dir / "main-watch"
    watch.write_text(WATCH_STUB)
    watch.chmod(0o755)

    return bin_dir


def verdict(kind: str, failing: list[str] | None = None) -> dict:
    """One verdict, in the shape `main-watch --json` prints."""
    return {
        "commit": "x",
        "kind": kind,
        "broke": kind == "broke",
        "parent_state": "green",
        "failing": [{"name": name, "url": ""} for name in (failing or [])],
        "uncompared": [],
    }


def write_env(tmp: Path, *, token: str = TEST_TOKEN, chat: str = TEST_CHAT,
              path: str | None = "env") -> str:
    """The env file the operator writes, and the path to it."""
    lines = []
    if token is not None:
        lines.append(f"TELEGRAM_BOT_TOKEN={token}")
    if chat is not None:
        lines.append(f"TELEGRAM_CHAT_ID={chat}")
    env_path = tmp / (path or "env")
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return str(env_path)


def write_relay_env(tmp: Path, urls: list[str], token: str | None,
                    path: str = "relay-env") -> str:
    """The env file that selects the relay path, and the path to it.

    `token` is None when the file carries the URLs and no token, which
    is a configuration the tool has to refuse.
    """
    lines = [f"NOTIFY_RELAY_URLS={' '.join(urls)}"]
    if token is not None:
        lines.append(f"NOTIFY_RELAY_TOKEN={token}")
    env_path = tmp / path
    env_path.write_text("\n".join(lines) + "\n")
    return str(env_path)


def run_runner(tmp: Path, bin_dir: Path, telegram_url: str,
               env_file: str | None, *args: str):
    """Run the tool with the stubs in place."""
    env = {**os.environ,
           "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "STUB_DATA": str(tmp / "data"),
           "XDG_STATE_HOME": str(tmp / "state"),
           "HOME": str(tmp / "home"),
           "MAIN_WATCH_TELEGRAM_API": telegram_url,
           # The tool judges with the checkout's own `main-watch`, so
           # the stub is named here.
           "MAIN_WATCH_BIN": str(bin_dir / "main-watch")}
    if env_file is not None:
        env["MAIN_WATCH_ENV"] = env_file
    else:
        env.pop("MAIN_WATCH_ENV", None)
    (tmp / "home").mkdir(exist_ok=True)
    return subprocess.run(
        [str(RUNNER), *args], capture_output=True, text=True, check=False,
        env=env,
    )


def run_relay_runner(tmp: Path, bin_dir: Path, env_file: str | None,
                     *args: str):
    """Run the tool with a relay in the env file.

    The relay path skips the Telegram API, so no stub is named.
    """
    return run_runner(tmp, bin_dir, "", env_file, *args)


def state_file(tmp: Path, repo: str = "owner/repo") -> Path:
    """The file the position is kept in."""
    return tmp / "state" / "main-watch" / repo.replace("/", "__")


def judged(tmp: Path) -> list[str]:
    """The commits `main-watch` was asked about, oldest first."""
    log = tmp / "data" / "main-watch.log"
    if not log.exists():
        return []
    return [ln.split()[1] for ln in log.read_text().splitlines() if ln.strip()]


def gh_calls(tmp: Path) -> list[str]:
    """The `gh` command lines the stub was asked to run."""
    log = tmp / "data" / "gh.log"
    if not log.exists():
        return []
    return [ln for ln in log.read_text().splitlines() if ln.strip()]


# ----- the cases ------------------------------------------------------------

def test_the_gh_call_carries_the_ref_in_the_endpoint() -> None:
    """The ref rides on the endpoint, not beside it.

    `gh api` takes one positional argument. A second one -- the ref as
    `sha=main` beside the endpoint -- is refused by the real `gh`
    before it reaches the API, so every run would die on the first
    call. The stub counts positionals the way `gh api` does and
    refuses more than one, so this case fails if the call shape moves
    back.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_env(tmp)

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            calls = gh_calls(tmp)
            check("gh was called", bool(calls), str(calls))
            check("the stub refused no call",
                  "accepts 1 arg(s)" not in result.stderr, result.stderr)
            check("the commits endpoint names the branch",
                  any("repos/owner/repo/commits/main" in c for c in calls),
                  str(calls))
            check("no gh call carried the ref beside the endpoint",
                  not any(c.endswith("commits sha=main") for c in calls),
                  str(calls))
        finally:
            stub.stop()


def test_the_commits_between_the_position_and_the_head_are_judged() -> None:
    """A compare endpoint answering `ahead` is the list to judge.

    The compare endpoint answers with the commits between the stored
    commit and the head, oldest first. That list is exactly what the
    run judges, and it is the whole gap: a run that was away long
    enough judges every commit that came after it, and nothing else.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            # The compare endpoint answers with three commits. The
            # commits endpoint answers with a different list, so a run
            # that judged that list instead would judge the wrong
            # commits.
            commits = ["sha-other", "sha-yours", "sha-base"]
            verdicts = {sha: verdict("green") for sha in commits}
            verdicts["sha-b"] = verdict("broke", ["Gates"])
            verdicts["sha-c"] = verdict("green")
            verdicts["sha-a"] = verdict("green")
            bin_dir = write_stubs(
                tmp,
                commits=commits,
                verdicts=verdicts,
                pulls={},
                compare_status="ahead",
                compare_commits=["sha-a", "sha-b", "sha-c"],
            )
            env_file = write_env(tmp)
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-base\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            order = judged(tmp)
            check("the three commits between were judged, oldest first",
                  order == ["sha-a", "sha-b", "sha-c"], str(order))
            check("the stored commit was not judged again",
                  "sha-base" not in order, str(order))
            check("the break was reported", len(stub.messages) == 1,
                  str(stub.messages))
        finally:
            stub.stop()


def test_a_rewritten_branch_resets_to_the_head_with_one_message() -> None:
    """A stored commit the endpoint does not know resets the position.

    The branch was rewritten under the position, so the commits the
    position stood for are gone. Only the head is judged, the operator
    hears that the position was reset, and the head is stored.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            commits = ["sha-new", "sha-mid", "sha-old"]
            verdicts = {sha: verdict("green") for sha in commits}
            verdicts["sha-mid"] = verdict("broke", ["Gates"])
            bin_dir = write_stubs(
                tmp,
                commits=commits,
                verdicts=verdicts,
                pulls={},
                compare_failure="404",
            )
            env_file = write_env(tmp)
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-gone\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            order = judged(tmp)
            check("only the head was judged", order == ["sha-new"], str(order))
            check("one message was sent", len(stub.messages) == 1,
                  str(stub.messages))
            check("the message says the position was reset",
                  "reset" in stub.messages[0].lower(), stub.messages[0])
            check("the position is the head",
                  state.read_text().strip() == "sha-new", state.read_text())
        finally:
            stub.stop()


def test_a_failing_compare_holds_the_position() -> None:
    """A compare endpoint that fails holds the position.

    A rate limit, an auth failure or a gateway that is down is not a
    rewrite. The run exits 1, nothing is judged, and the position stays
    where it was.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            commits = ["sha-new", "sha-old"]
            verdicts = {sha: verdict("green") for sha in commits}
            verdicts["sha-new"] = verdict("broke", ["Gates"])
            bin_dir = write_stubs(
                tmp,
                commits=commits,
                verdicts=verdicts,
                pulls={},
                compare_failure="502",
            )
            env_file = write_env(tmp)
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-old\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run failed", result.returncode == 1,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the failure is named", result.stderr.startswith("ERR "),
                  result.stderr)
            check("nothing was judged", judged(tmp) == [], str(judged(tmp)))
            check("the position did not move",
                  state.read_text().strip() == "sha-old", state.read_text())
            check("nothing was sent", len(stub.messages) == 0,
                  str(stub.messages))
        finally:
            stub.stop()


def test_a_capped_compare_list_holds_the_position() -> None:
    """A compare endpoint that returned fewer commits than it counted holds.

    The endpoint caps the list it returns. Judging the part it gave
    would skip the rest of the gap, so the run holds the position and
    names the count instead.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            commits = ["sha-c", "sha-b", "sha-a", "sha-base"]
            verdicts = {sha: verdict("green") for sha in commits}
            verdicts["sha-b"] = verdict("broke", ["Gates"])
            bin_dir = write_stubs(
                tmp,
                commits=commits,
                verdicts=verdicts,
                pulls={},
                compare_status="ahead",
                compare_commits=["sha-a", "sha-b", "sha-c"],
                # The endpoint counted four commits and returned three.
                compare_total="4",
            )
            env_file = write_env(tmp)
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-base\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run failed", result.returncode == 1,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the failure names the count", "4" in result.stderr,
                  result.stderr)
            check("nothing was judged", judged(tmp) == [], str(judged(tmp)))
            check("the position did not move",
                  state.read_text().strip() == "sha-base", state.read_text())
            check("nothing was sent", len(stub.messages) == 0,
                  str(stub.messages))
        finally:
            stub.stop()


def test_one_failing_repository_does_not_starve_the_others() -> None:
    """A repository that fails does not stop the ones after it.

    The issue's caller takes a list of repositories and judges each
    one. A repository with an auth problem must not starve every
    repository after it, on every tick, forever. The run exits
    non-zero and names the failure, and the other repositories are
    still judged.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
                fails_for="repos/bad/repo/commits",
                # The call shape is measured by another case; this one
                # measures what one failing repository does to the rest.
                strict_argv=False,
            )
            env_file = write_env(tmp)

            result = run_runner(tmp, bin_dir, url, env_file,
                                "bad/repo", "good/repo")
            check("the run failed", result.returncode == 1,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the failure is named", "bad/repo" in result.stderr,
                  result.stderr)
            # The repository after the failing one was still judged.
            order = judged(tmp)
            check("the second repository was judged",
                  "sha-new" in order, str(order))
            check("the break was reported", len(stub.messages) == 1,
                  str(stub.messages))
        finally:
            stub.stop()


def test_a_break_is_reported_once_and_the_next_run_is_silent() -> None:
    """A commit that broke `main` sends one message, and only one.

    The second run judges the same commits, and the position it kept
    stops it. A tool that reported on every run would page the
    operator once per timer tick.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={
                    "sha-new": verdict("broke", ["Gates"]),
                    "sha-old": verdict("green"),
                },
                pulls={"sha-new": [{"number": 42, "title": "A change",
                                     "html_url": "u"}]},
            )
            env_file = write_env(tmp)

            first = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the first run succeeded", first.returncode == 0,
                  f"exit {first.returncode}: {first.stderr!r}")
            check("one message was sent", len(stub.messages) == 1,
                  str(stub.messages))
            text = stub.messages[0]
            check("the repository is named", "owner/repo" in text, text)
            check("the commit is named", "sha-new"[:7] in text, text)
            check("the pull request is named", "#42" in text, text)
            check("the failing check is named", "Gates" in text, text)

            second = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the second run succeeded", second.returncode == 0,
                  f"exit {second.returncode}: {second.stderr!r}")
            check("the second run sent nothing",
                  len(stub.messages) == 1, str(stub.messages))
        finally:
            stub.stop()


def test_a_pending_commit_holds_the_position() -> None:
    """A pending head is not judged past, and is judged on the next run.

    Advancing past it would skip the commit whose checks have not
    finished, and a red that arrives later would never be reported.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={
                    "sha-new": verdict("pending"),
                    "sha-old": verdict("green"),
                },
                pulls={},
            )
            env_file = write_env(tmp)

            first = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the first run succeeded", first.returncode == 0,
                  f"exit {first.returncode}: {first.stderr!r}")
            check("nothing was sent", len(stub.messages) == 0,
                  str(stub.messages))
            # The position stayed where it was: the pending commit was
            # not judged, so nothing was recorded about it.
            state = state_file(tmp)
            check("no position was written", not state.exists(),
                  state.read_text() if state.exists() else "")

            # The checks finish, and the commit turns out to have broken
            # the branch. The next run judges it.
            (tmp / "data" / "verdicts.json").write_text(json.dumps({
                "sha-new": verdict("broke", ["Gates"]),
                "sha-old": verdict("green"),
            }))
            second = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the second run succeeded", second.returncode == 0,
                  f"exit {second.returncode}: {second.stderr!r}")
            check("the pending commit was judged on the next run",
                  len(stub.messages) == 1, str(stub.messages))
            check("the break is the message", "Gates" in stub.messages[0],
                  stub.messages[0])
        finally:
            stub.stop()


def test_a_run_of_red_commits_sends_one_message() -> None:
    """Three `already_red` commits send one message, not three.

    The red run is one event: the operator wants to know that `main` is
    red, once. A message per commit is spam.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-c", "sha-b", "sha-a"],
                verdicts={
                    "sha-a": verdict("already_red", ["Gates"]),
                    "sha-b": verdict("already_red", ["Gates"]),
                    "sha-c": verdict("already_red", ["Gates"]),
                },
                pulls={"sha-a": [{"number": 7, "title": "First", "html_url": "u"}]},
            )
            env_file = write_env(tmp)
            # A position behind all three, so all three are judged and
            # the run of red commits is the whole batch. With no
            # position only the head would be judged, and there would
            # be no run to speak of.
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-base\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("one message was sent", len(stub.messages) == 1,
                  str(stub.messages))
            # The message names the commit the run started at, which is
            # the oldest of the three.
            check("the start of the run is named",
                  "sha-a"[:7] in stub.messages[0], stub.messages[0])
            check("the pull request is named", "#7" in stub.messages[0],
                  stub.messages[0])
            check("the failing check is named", "Gates" in stub.messages[0],
                  stub.messages[0])
            # All three were judged, oldest first.
            order = judged(tmp)
            check("all three were judged, oldest first",
                  order == ["sha-a", "sha-b", "sha-c"], str(order))
        finally:
            stub.stop()


def test_a_failed_fetch_moves_nothing() -> None:
    """A failed `gh` call exits non-zero and leaves the position alone.

    A position that moved past commits nobody judged would lose the
    report of a break that is sitting in them.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
                gh_fails=True,
            )
            env_file = write_env(tmp)
            # A position from an earlier run, so the test can tell the
            # failed run did not move it.
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-old\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run failed", result.returncode != 0,
                  f"exit {result.returncode}")
            check("the failure is named", result.stderr.startswith("ERR "),
                  result.stderr)
            check("the position did not move",
                  state.read_text().strip() == "sha-old",
                  state.read_text())
            check("nothing was sent", len(stub.messages) == 0,
                  str(stub.messages))
        finally:
            stub.stop()


def test_a_failed_send_moves_nothing_and_leaks_no_token() -> None:
    """A Telegram failure exits non-zero, moves nothing, and prints no token.

    The token is in the URL, and any error the request raises may
    carry it. A tool that prints that error prints the token.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub(status=500)
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={
                    "sha-new": verdict("broke", ["Gates"]),
                    "sha-old": verdict("green"),
                },
                pulls={},
            )
            env_file = write_env(tmp)

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run failed", result.returncode != 0,
                  f"exit {result.returncode}")
            check("the failure is named", result.stderr.startswith("ERR "),
                  result.stderr)
            check("the token is in no output",
                  TEST_TOKEN not in result.stdout
                  and TEST_TOKEN not in result.stderr,
                  f"{result.stdout!r} {result.stderr!r}")
            # The position did not move: the commit is judged again on
            # the next run, so the break is reported then.
            state = state_file(tmp)
            check("the position did not move",
                  not state.exists() or state.read_text().strip() == "sha-old",
                  state.read_text() if state.exists() else "")
        finally:
            stub.stop()


def test_every_missing_configuration_is_refused_by_name() -> None:
    """Each missing piece of configuration exits 2 with one line.

    The four shapes: the variable is not set, the file does not exist,
    the token is missing or empty, and the chat id is missing or empty.
    """
    cases = [
        ("the variable is not set", None, None, None),
        ("the file does not exist", "missing-file", None, None),
        ("the token is missing", "env", None, TEST_CHAT),
        ("the token is empty", "env", "", TEST_CHAT),
        ("the chat id is missing", "env", TEST_TOKEN, None),
        ("the chat id is empty", "env", TEST_TOKEN, ""),
    ]
    for label, env_file, token, chat in cases:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            stub = TelegramStub()
            url = stub.start()
            try:
                bin_dir = write_stubs(
                    tmp, commits=["sha-new"],
                    verdicts={"sha-new": verdict("broke", ["Gates"])},
                    pulls={},
                )
                if env_file is None:
                    path = None
                elif env_file == "missing-file":
                    path = str(tmp / "missing-file")
                else:
                    path = write_env(tmp, token=token, chat=chat)

                result = run_runner(tmp, bin_dir, url, path, "owner/repo")
                check(f"{label}: the run refused", result.returncode == 2,
                      f"exit {result.returncode}: {result.stderr!r}")
                lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
                check(f"{label}: one line was printed", len(lines) == 1,
                      str(lines))
                check(f"{label}: the line names what is missing",
                      bool(lines) and lines[0].startswith("ERR "), str(lines))
                check(f"{label}: the token is in no output",
                      TEST_TOKEN not in result.stdout
                      and TEST_TOKEN not in result.stderr,
                      f"{result.stdout!r} {result.stderr!r}")
                check(f"{label}: nothing was sent", len(stub.messages) == 0,
                      str(stub.messages))
            finally:
                stub.stop()


def test_the_position_starts_at_the_head_with_no_state() -> None:
    """With no position, only the head is judged.

    The run starts where the watch starts, not at the beginning of
    history: a repository with a thousand commits on `main` must not be
    judged all of them at once.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old", "sha-ancient"],
                verdicts={
                    "sha-new": verdict("green"),
                    "sha-old": verdict("broke", ["Gates"]),
                    "sha-ancient": verdict("broke", ["Gates"]),
                },
                pulls={},
            )
            env_file = write_env(tmp)

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("nothing was sent", len(stub.messages) == 0,
                  str(stub.messages))
            order = judged(tmp)
            check("only the head was judged", len(order) == 1, str(order))
            check("the head was judged", "sha-new" in order, str(order))
            state = state_file(tmp)
            check("the head was stored",
                  state.read_text().strip() == "sha-new",
                  state.read_text())
        finally:
            stub.stop()


def test_the_commits_are_judged_oldest_first() -> None:
    """The commits are judged from the oldest to the newest.

    A branch is judged in the order it was built: the first commit that
    broke it is the one that broke it.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-c", "sha-b", "sha-a"],
                verdicts={
                    "sha-a": verdict("green"),
                    "sha-b": verdict("broke", ["Gates"]),
                    "sha-c": verdict("green"),
                },
                pulls={"sha-b": [{"number": 9, "title": "The break",
                                   "html_url": "u"}]},
            )
            env_file = write_env(tmp)
            # A position behind all three, so all three are judged.
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-base\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            order = judged(tmp)
            check("oldest first", order == ["sha-a", "sha-b", "sha-c"],
                  str(order))
            check("the break was reported once", len(stub.messages) == 1,
                  str(stub.messages))
            check("the position is the newest commit",
                  state.read_text().strip() == "sha-c", state.read_text())
        finally:
            stub.stop()


def test_a_green_commit_between_two_red_runs_sends_two_messages() -> None:
    """Two red runs separated by a green commit send two messages.

    The run starts again after the branch went green, so the operator
    hears about the branch going red twice.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-e", "sha-d", "sha-c", "sha-b", "sha-a"],
                verdicts={
                    "sha-a": verdict("already_red", ["Gates"]),
                    "sha-b": verdict("already_red", ["Gates"]),
                    "sha-c": verdict("green"),
                    "sha-d": verdict("already_red", ["Gates"]),
                    "sha-e": verdict("already_red", ["Gates"]),
                },
                pulls={},
            )
            env_file = write_env(tmp)
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-base\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("two messages were sent", len(stub.messages) == 2,
                  str(stub.messages))
            # One for each run, and each names the commit its run
            # started at.
            check("the first run is named", "sha-a"[:7] in stub.messages[0],
                  stub.messages[0])
            check("the second run is named", "sha-d"[:7] in stub.messages[1],
                  stub.messages[1])
        finally:
            stub.stop()


def test_the_position_is_written_only_after_a_message_went_out() -> None:
    """The position moves after the message, never before it.

    A position written first would lose the report when the send
    failed, and the commit would never be judged again.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub(status=500)
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={
                    "sha-new": verdict("broke", ["Gates"]),
                    "sha-old": verdict("green"),
                },
                pulls={},
            )
            env_file = write_env(tmp)
            state = state_file(tmp)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("sha-old\n")

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run failed", result.returncode != 0,
                  f"exit {result.returncode}")
            check("the position stayed where it was",
                  state.read_text().strip() == "sha-old",
                  state.read_text())
            # And the commit after the failed one was never judged: the
            # run stopped at the failure.
            order = judged(tmp)
            check("only the failed commit was judged",
                  all("sha-old" not in sha for sha in order), str(order))
        finally:
            stub.stop()


def test_the_recorded_head_is_one_commit_object() -> None:
    """The head of `main` is one commit object, as GitHub answers it.

    `gh api repos/R/commits/main` answers with an object, not a list.
    A tool that reads it as a list fails on every first run, because
    every repository starts with no stored position. The recorded
    response is what the stub answers with, so this case fails if the
    reading goes back to a list.
    """
    recorded = json.loads(
        (FIXTURES / "gh_api_commits_main.json").read_text())
    check("the recorded response is one object",
          isinstance(recorded, dict), type(recorded).__name__)
    check("the recorded response names its source",
          recorded.get("_source", "").startswith("gh api "),
          str(recorded.get("_source")))
    check("the recorded response carries a sha",
          isinstance(recorded.get("sha"), str) and recorded["sha"],
          str(recorded.get("sha")))

    # The reading the tool used before: a list, and the head the first
    # of it. The recorded response is not a list, so it refuses.
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp, commits=["sha-new"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_env(tmp)

            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            # The head the stub answered with is the recorded one, so
            # the tool read a sha out of an object.
            check("the recorded head was judged",
                  judged(tmp) == ["sha-new"], str(judged(tmp)))
        finally:
            stub.stop()


def test_the_token_is_never_printed() -> None:
    """Nothing the tool prints carries the token.

    The refusal and the failure paths are the two that carry text the
    tool did not write.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub(status=500)
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp, commits=["sha-new"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_env(tmp)
            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run failed", result.returncode != 0,
                  f"exit {result.returncode}")
            check("the token is in no output",
                  TEST_TOKEN not in result.stdout
                  and TEST_TOKEN not in result.stderr,
                  f"{result.stdout!r} {result.stderr!r}")
            # The token was in the request, which is the whole point of
            # the scrub: the request had it, the output did not.
            check("the stub saw the token",
                  any(TEST_TOKEN in r["path"] for r in stub.requests),
                  str(stub.requests))
        finally:
            stub.stop()


def test_the_scrub_takes_the_token_out_of_a_message() -> None:
    """The scrub takes the token out of a string that carries it.

    The scrub is what stands between a token-bearing URL and the
    operator's terminal, so it is driven with one here rather than
    only through the paths where an error happens to carry the URL.
    """
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("main_watch_run", str(RUNNER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    text = f"failed to reach https://api.telegram.org/bot{TEST_TOKEN}/sendMessage"
    scrubbed = module.redact(text, TEST_TOKEN)
    check("the token is not in the scrubbed text",
          TEST_TOKEN not in scrubbed, scrubbed)
    check("the message still names the URL",
          "api.telegram.org" in scrubbed, scrubbed)
    check("the token is marked as taken out",
          "[redacted]" in scrubbed, scrubbed)


def test_the_checkouts_own_main_watch_wins_over_the_path() -> None:
    """A stale `main-watch` on PATH does not judge the commits.

    The tool judges with the copy in this checkout, so a stale one
    earlier on the timer's PATH cannot decide what a commit means.
    `MAIN_WATCH_BIN` names another copy when the operator wants one.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = TelegramStub()
        url = stub.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            # A `main-watch` on PATH that answers with something else
            # entirely. It must not be the one that judges.
            stale = tmp / "stale-main-watch"
            stale.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$STUB_DATA/stale.log\"\n"
                "echo '{\"kind\": \"green\"}'\n")
            stale.chmod(0o755)
            env_file = write_env(tmp)

            # The stub on PATH is named explicitly, so the checkout's
            # own copy is not what runs here.
            result = run_runner(tmp, bin_dir, url, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the named copy judged the commit",
                  "sha-new" in judged(tmp), str(judged(tmp)))

            # Without the name, the checkout's own copy wins over the
            # one on PATH.
            (tmp / "data" / "verdicts.json").write_text(json.dumps(
                {"sha-new": verdict("broke", ["Gates"])}))
            shutil.copy(stale, bin_dir / "main-watch")
            (bin_dir / "main-watch").chmod(0o755)
            env = {**os.environ,
                   "PATH": f"{bin_dir}:{os.environ['PATH']}",
                   "STUB_DATA": str(tmp / "data"),
                   "XDG_STATE_HOME": str(tmp / "state2"),
                   "HOME": str(tmp / "home"),
                   "MAIN_WATCH_TELEGRAM_API": url,
                   "MAIN_WATCH_ENV": env_file}
            env.pop("MAIN_WATCH_BIN", None)
            second = subprocess.run([str(RUNNER), "owner/repo"],
                                    capture_output=True, text=True,
                                    check=False, env=env)
            check("the run succeeded", second.returncode == 0,
                  f"exit {second.returncode}: {second.stderr!r}")
            stale_log = tmp / "data" / "stale.log"
            check("the copy on PATH did not judge",
                  not stale_log.exists(),
                  stale_log.read_text() if stale_log.exists() else "")
        finally:
            stub.stop()


def test_the_relay_delivers_the_message() -> None:
    """A 202 from the first relay delivers, and the second is left alone.

    The relay delivers the message itself, so an accepted message ends
    this tool's part.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        first = RelayStub()
        second = RelayStub()
        first_url = first.start()
        second_url = second.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(tmp, [first_url, second_url],
                                       TEST_RELAY_TOKEN)

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the first relay was asked", len(first.requests) == 1,
                  str(first.requests))
            check("the second relay was not asked", second.requests == [],
                  str(second.requests))
            check("one message was delivered", len(first.texts) == 1,
                  str(first.texts))
            text = first.texts[0]
            check("the repository is named", "owner/repo" in text, text)
            check("the failing check is named", "Gates" in text, text)
            # The position moved: the message went out.
            check("the position moved",
                  state_file(tmp).read_text().strip() == "sha-new",
                  state_file(tmp).read_text())
        finally:
            first.stop()
            second.stop()


def test_a_failing_relay_falls_through_to_the_next() -> None:
    """A 500 from the first relay falls through to the second.

    A relay that is down is the relay's problem, so the next URL is
    tried and the message is delivered there.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        first = RelayStub(statuses=[500])
        second = RelayStub()
        first_url = first.start()
        second_url = second.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(tmp, [first_url, second_url],
                                       TEST_RELAY_TOKEN)

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the first relay was asked", len(first.requests) == 1,
                  str(first.requests))
            check("the second relay was asked", len(second.requests) == 1,
                  str(second.requests))
            check("the message reached the second relay",
                  len(second.texts) == 1, str(second.texts))
            check("the token is in no output",
                  TEST_RELAY_TOKEN not in result.stdout
                  and TEST_RELAY_TOKEN not in result.stderr,
                  f"{result.stdout!r} {result.stderr!r}")
        finally:
            first.stop()
            second.stop()


def test_an_unreachable_relay_falls_through_to_the_next() -> None:
    """A relay that cannot be reached falls through to the next.

    A closed port is the relay being down, not the client being
    refused, so the next URL is tried.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        second = RelayStub()
        second_url = second.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(
                tmp, [closed_port_url(), second_url], TEST_RELAY_TOKEN)

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the second relay was asked", len(second.requests) == 1,
                  str(second.requests))
            check("the message reached the second relay",
                  len(second.texts) == 1, str(second.texts))
        finally:
            second.stop()


def test_a_refused_relay_fails_the_run_and_names_it() -> None:
    """A 4xx fails the run, naming the relay and its status.

    The relay refused this client: a revoked or a mistyped token. That
    is a fault in this host's configuration, so it stays loud, and no
    other relay is tried -- a working fallback would hide a revoked
    token behind it.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        first = RelayStub(statuses=[401])
        second = RelayStub()
        first_url = first.start()
        second_url = second.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(tmp, [first_url, second_url],
                                       TEST_RELAY_TOKEN)

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run failed", result.returncode == 1,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the failure names the relay", first_url in result.stderr,
                  result.stderr)
            check("the failure names the status", "401" in result.stderr,
                  result.stderr)
            check("the second relay was not asked", second.requests == [],
                  str(second.requests))
            check("the position did not move", not state_file(tmp).exists(),
                  state_file(tmp).read_text()
                  if state_file(tmp).exists() else "")
            check("the token is in no output",
                  TEST_RELAY_TOKEN not in result.stdout
                  and TEST_RELAY_TOKEN not in result.stderr,
                  f"{result.stdout!r} {result.stderr!r}")
        finally:
            first.stop()
            second.stop()


def test_every_relay_failing_names_each_of_them() -> None:
    """When every relay has failed, the failure names each one.

    The operator has to know which relays were tried and what each of
    them said, or the fix is a guess.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        first = RelayStub(statuses=[500])
        second = RelayStub(statuses=[503])
        first_url = first.start()
        second_url = second.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(tmp, [first_url, second_url],
                                       TEST_RELAY_TOKEN)

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run failed", result.returncode == 1,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the first relay is named", first_url in result.stderr,
                  result.stderr)
            check("the second relay is named", second_url in result.stderr,
                  result.stderr)
            check("the first status is named", "500" in result.stderr,
                  result.stderr)
            check("the second status is named", "503" in result.stderr,
                  result.stderr)
        finally:
            first.stop()
            second.stop()


def test_the_relay_token_is_in_the_header_and_nowhere_else() -> None:
    """The token rides in the authorization header, and nowhere else.

    The relay reads the token from the header. A token in the URL or
    in the body would be written down somewhere the relay keeps, and
    a log is a place a token must not be.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        relay = RelayStub()
        url = relay.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(tmp, [url], TEST_RELAY_TOKEN)

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the relay was asked", len(relay.requests) == 1,
                  str(relay.requests))
            request = relay.requests[0]
            check("the token is in the header",
                  request["authorization"] == f"Bearer {TEST_RELAY_TOKEN}",
                  str(request))
            check("the token is not in the URL",
                  TEST_RELAY_TOKEN not in request["url"], str(request))
            check("the token is not in the body",
                  TEST_RELAY_TOKEN not in request["body"], str(request))
            check("the send is at the relay's send path",
                  request["path"] == "/send", str(request))
            check("the body is json",
                  request["content_type"] == "application/json",
                  str(request))
            body = json.loads(request["body"])
            check("the body carries the text", "text" in body, str(body))
            check("the body carries nothing else", list(body) == ["text"],
                  str(body))
        finally:
            relay.stop()


def test_an_env_file_with_only_the_relay_variables_loads() -> None:
    """The relay path needs no Telegram credentials.

    The point of the relay is that this host holds no bot token, so an
    env file with the relay variables alone is a complete
    configuration.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        relay = RelayStub()
        url = relay.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(tmp, [url], TEST_RELAY_TOKEN)
            check("the env file carries no Telegram key",
                  "TELEGRAM" not in Path(env_file).read_text(),
                  Path(env_file).read_text())

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run succeeded", result.returncode == 0,
                  f"exit {result.returncode}: {result.stderr!r}")
            check("the message was delivered", len(relay.texts) == 1,
                  str(relay.texts))
        finally:
            relay.stop()


def test_relay_urls_without_a_token_are_refused_by_name() -> None:
    """The relay URLs without a token are refused, naming the variable.

    A relay path with no token would post a message the relay refuses,
    once per run. The refusal names the variable, so the operator
    knows which one to write.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        relay = RelayStub()
        url = relay.start()
        try:
            bin_dir = write_stubs(
                tmp,
                commits=["sha-new", "sha-old"],
                verdicts={"sha-new": verdict("broke", ["Gates"])},
                pulls={},
            )
            env_file = write_relay_env(tmp, [url], None)

            result = run_relay_runner(tmp, bin_dir, env_file, "owner/repo")
            check("the run refused", result.returncode == 2,
                  f"exit {result.returncode}: {result.stderr!r}")
            lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
            check("one line was printed", len(lines) == 1, str(lines))
            check("the line names the variable",
                  bool(lines) and "NOTIFY_RELAY_TOKEN" in lines[0], str(lines))
            check("nothing was sent", relay.requests == [],
                  str(relay.requests))
        finally:
            relay.stop()


def main() -> int:
    failures: list[str] = []
    for name, fn in [
        ("the gh call carries the ref in the endpoint",
         test_the_gh_call_carries_the_ref_in_the_endpoint),
        ("the commits between the position and the head are judged",
         test_the_commits_between_the_position_and_the_head_are_judged),
        ("a rewritten branch resets to the head with one message",
         test_a_rewritten_branch_resets_to_the_head_with_one_message),
        ("a failing compare holds the position",
         test_a_failing_compare_holds_the_position),
        ("a capped compare list holds the position",
         test_a_capped_compare_list_holds_the_position),
        ("one failing repository does not starve the others",
         test_one_failing_repository_does_not_starve_the_others),
        ("a break is reported once and the next run is silent",
         test_a_break_is_reported_once_and_the_next_run_is_silent),
        ("a pending commit holds the position",
         test_a_pending_commit_holds_the_position),
        ("a run of red commits sends one message",
         test_a_run_of_red_commits_sends_one_message),
        ("a failed fetch moves nothing",
         test_a_failed_fetch_moves_nothing),
        ("a failed send moves nothing and leaks no token",
         test_a_failed_send_moves_nothing_and_leaks_no_token),
        ("every missing configuration is refused by name",
         test_every_missing_configuration_is_refused_by_name),
        ("the position starts at the head with no state",
         test_the_position_starts_at_the_head_with_no_state),
        ("the commits are judged oldest first",
         test_the_commits_are_judged_oldest_first),
        ("a green commit between two red runs sends two messages",
         test_a_green_commit_between_two_red_runs_sends_two_messages),
        ("the position is written only after a message went out",
         test_the_position_is_written_only_after_a_message_went_out),
        ("the recorded head is one commit object",
         test_the_recorded_head_is_one_commit_object),
        ("the token is never printed",
         test_the_token_is_never_printed),
        ("the scrub takes the token out of a message",
         test_the_scrub_takes_the_token_out_of_a_message),
        ("the checkout's own main-watch wins over the path",
         test_the_checkouts_own_main_watch_wins_over_the_path),
        ("the relay delivers the message",
         test_the_relay_delivers_the_message),
        ("a failing relay falls through to the next",
         test_a_failing_relay_falls_through_to_the_next),
        ("an unreachable relay falls through to the next",
         test_an_unreachable_relay_falls_through_to_the_next),
        ("a refused relay fails the run and names it",
         test_a_refused_relay_fails_the_run_and_names_it),
        ("every relay failing names each of them",
         test_every_relay_failing_names_each_of_them),
        ("the relay token is in the header and nowhere else",
         test_the_relay_token_is_in_the_header_and_nowhere_else),
        ("an env file with only the relay variables loads",
         test_an_env_file_with_only_the_relay_variables_loads),
        ("relay urls without a token are refused by name",
         test_relay_urls_without_a_token_are_refused_by_name),
    ]:
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{name}: {e}")
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures.append(f"{name}: {e!r}")
            print(f"FAIL {name}: {e!r}")
        else:
            print(f"ok   {name}")
    if failures:
        return 1
    print("PASS main_watch_run: the cases the issue separates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
