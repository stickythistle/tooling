#!/usr/bin/env python3
"""Pin the lane keeper: one owner for the record, and a dashboard that shows it.

The record is what every lane is counted by, so the tool that writes it
must not be the lane. The service owns the database, and a token's role
decides what a request may do: a reader asks, a reporter says what a
lane's pane shows, a writer changes the record.

The claim's own rules stay where they were, so two callers racing for
the last unit of a pool still get one winner, and a claim for an issue
that is already leased is refused and names the holder.

Every case starts the service on 127.0.0.1 with port 0, in a thread, and
talks to it over loopback with urllib.
"""
import hashlib
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import lane_keeper  # noqa: E402

# The tokens the cases use. The settings hold their digests.
TOKENS = {"writer": "writer-token", "reporter": "reporter-token",
          "reader": "reader-token"}

# The repository the lanes are for.
REPO = "stickythistle/tooling"


def digest(token: str) -> str:
    """The digest the settings hold for a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class World:
    """One case's service: the settings, the record and the server."""

    def __init__(self, tmp: Path, pool_size: int = 1, tls: bool = False):
        self.tmp = tmp
        self.db = tmp / "lanes.db"
        self.config = tmp / "lane-keeper.toml"
        self.cert = tmp / "cert.pem"
        self.key = tmp / "key.pem"
        if tls:
            self.cert, self.key = make_cert(tmp)
        self.write_config(pool_size, tls)
        self.settings = lane_keeper.load_settings(self.config)
        self.server = lane_keeper.make_server(self.settings)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self.thread.start()

    def write_config(self, pool_size: int, tls: bool) -> None:
        """The settings file."""
        lines = [f"database = \"{self.db}\"", "",
                 "[listen]", "host = \"127.0.0.1\"", "port = 0", "",
                 "[pools]",
                 f"minimax = {pool_size}", "", "[profiles.minimax]",
                 "pool = \"minimax\"", "cost = 1", "model = \"MiniMax\"", ""]
        for role, token in TOKENS.items():
            lines.append("[[tokens]]")
            lines.append(f"role = \"{role}\"")
            lines.append(f"sha256 = \"{digest(token)}\"")
            lines.append("")
        if tls:
            lines.append("[tls]")
            lines.append(f"cert = \"{self.cert}\"")
            lines.append(f"key = \"{self.key}\"")
            lines.append("")
        self.config.write_text("\n".join(lines))

    def close(self) -> None:
        """Stop the server."""
        self.server.shutdown()
        self.server.server_close()

    def call(self, path: str, token: str | None = None, method: str = "GET",
             body: dict | None = None, scheme: str = "http",
             context: ssl.SSLContext | None = None) -> tuple[int, str]:
        """One request, and the answer."""
        url = f"{scheme}://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30,
                                        context=context) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def claim_body(self, issue: int = 5, window: str | None = None,
                   repo: str = REPO, **extra) -> dict:
        """What a claim asks for."""
        body = {
            "repo": repo,
            "issue": issue,
            "harness": "hermes",
            "profile": "minimax",
            "branch": f"lane/{issue}",
            "base": "origin/main",
            "worktree": str(self.tmp / "wt" / f"tooling-{issue}"),
            "clone": str(self.tmp / "clone"),
            "tmux_session": "0",
            "window": window or f"tooling-{issue}",
            "session": "coord-a",
            "harness_session": "abc123",
        }
        body.update(extra)
        return body

    def claim(self, issue: int = 5, **extra) -> tuple[int, str]:
        """A claim, as the writer makes it."""
        return self.call("/api/claim", token=TOKENS["writer"], method="POST",
                         body=self.claim_body(issue, **extra))


# ----- the cases ------------------------------------------------------------

def test_no_token(world: World) -> list[str]:
    """A request without a token, or with an unknown one, is 401."""
    failures = []
    endpoints = [("/", "GET", None),
                 ("/api/now", "GET", None),
                 ("/api/history", "GET", None),
                 ("/api/claim", "POST", world.claim_body()),
                 ("/api/lease/1/state", "POST", {"state": "idle"})]
    for path, method, body in endpoints:
        for token in (None, "a-token-nobody-has"):
            status, answer = world.call(path, token=token, method=method,
                                        body=body)
            if status != 401:
                failures.append(f"no token: {method} {path} with the token "
                                f"{token!r} answered {status}, not 401: "
                                f"{answer[:120]!r}")
    return failures


def test_reader(world: World) -> list[str]:
    """A reader may ask, and may not change."""
    failures = []
    status, answer = world.call("/api/now", token=TOKENS["reader"])
    if status != 200:
        failures.append(f"reader: GET /api/now answered {status}, not 200: "
                        f"{answer[:120]!r}")
    status, answer = world.call("/api/claim", token=TOKENS["reader"],
                                method="POST", body=world.claim_body())
    if status != 403:
        failures.append(f"reader: POST /api/claim answered {status}, not 403: "
                        f"{answer[:120]!r}")
    status, answer = world.call("/api/lease/1/release", token=TOKENS["reader"],
                                method="POST", body={})
    if status != 403:
        failures.append(f"reader: POST a release answered {status}, not 403: "
                        f"{answer[:120]!r}")
    return failures


def test_reporter(world: World) -> list[str]:
    """A reporter says what a lane's pane shows, and nothing else."""
    failures = []
    status, answer = world.claim()
    if status != 200:
        return [f"reporter: the claim failed: {answer!r}"]
    lease = json.loads(answer)["lease"]

    status, answer = world.call(f"/api/lease/{lease}/state",
                                token=TOKENS["reporter"], method="POST",
                                body={"state": "idle"})
    if status != 200:
        failures.append(f"reporter: the state of a live lease answered "
                        f"{status}, not 200: {answer[:120]!r}")

    status, answer = world.call(f"/api/lease/{lease}/release",
                                token=TOKENS["writer"], method="POST", body={})
    if status != 200:
        failures.append(f"reporter: the release answered {status}, not 200: "
                        f"{answer[:120]!r}")

    status, answer = world.call(f"/api/lease/{lease}/state",
                                token=TOKENS["reporter"], method="POST",
                                body={"state": "idle"})
    if status != 409:
        failures.append(f"reporter: the state of a released lease answered "
                        f"{status}, not 409: {answer[:120]!r}")

    status, answer = world.call("/api/claim", token=TOKENS["reporter"],
                                method="POST", body=world.claim_body(issue=6))
    if status != 403:
        failures.append(f"reporter: POST /api/claim answered {status}, not "
                        f"403: {answer[:120]!r}")
    return failures


def test_race(world: World) -> list[str]:
    """Two claims for the last unit of a pool: one wins, one is refused."""
    failures = []
    answers = []
    lock = threading.Lock()

    def claim(issue: int) -> None:
        status, answer = world.claim(issue)
        with lock:
            answers.append((status, answer))

    threads = [threading.Thread(target=claim, args=(issue,))
               for issue in (5, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = sorted(status for status, _ in answers)
    if statuses != [200, 409]:
        failures.append(f"race: the two claims answered {statuses}, not one "
                        f"200 and one 409")
        return failures
    refused = [answer for status, answer in answers if status == 409]
    payload = json.loads(refused[0])
    if payload.get("key") != "pool":
        failures.append(f"race: the refusal does not name the pool: {payload!r}")
    if "detail" not in payload:
        failures.append(f"race: the refusal carries no detail: {payload!r}")
    return failures


def test_same_issue(world: World) -> list[str]:
    """A claim for an issue that is leased is refused, and names the holder."""
    failures = []
    status, answer = world.claim(issue=5)
    if status != 200:
        return [f"same issue: the first claim failed: {answer!r}"]
    lease = json.loads(answer)["lease"]
    status, answer = world.claim(issue=5, window="tooling-5-again",
                                 worktree=str(world.tmp / "wt" / "again"))
    if status != 409:
        failures.append(f"same issue: the second claim answered {status}, not "
                        f"409: {answer[:120]!r}")
        return failures
    payload = json.loads(answer)
    if payload.get("key") != "lease":
        failures.append(f"same issue: the refusal does not name the lease: "
                        f"{payload!r}")
    if str(lease) not in payload.get("detail", ""):
        failures.append(f"same issue: the refusal does not name the holder: "
                        f"{payload!r}")
    return failures


def test_default_branch(world: World) -> list[str]:
    """A claim that names no branch gets the one the record names."""
    failures = []
    body = world.claim_body(issue=5)
    body.pop("branch")
    status, answer = world.call("/api/claim", token=TOKENS["writer"],
                                method="POST", body=body)
    if status != 200:
        failures.append(f"default branch: the claim answered {status}, not "
                        f"200: {answer[:120]!r}")
        return failures
    payload = json.loads(answer)
    if not payload.get("branch"):
        failures.append(f"default branch: the claim names no branch: "
                        f"{payload!r}")
    return failures


def test_history(world: World) -> list[str]:
    """A finished lane is history: its outcome, its pull request, and when."""
    failures = []
    status, answer = world.claim(issue=5)
    if status != 200:
        return [f"history: the claim failed: {answer!r}"]
    lease = json.loads(answer)["lease"]
    status, answer = world.call(f"/api/lease/{lease}/finish",
                                token=TOKENS["writer"], method="POST",
                                body={"outcome": "merged", "pr": 12,
                                      "pr_state": "MERGED"})
    if status != 200:
        failures.append(f"history: the finish answered {status}, not 200: "
                        f"{answer[:120]!r}")
    status, answer = world.call("/api/history", token=TOKENS["reader"])
    if status != 200:
        failures.append(f"history: GET /api/history answered {status}, not "
                        f"200: {answer!r}")
        return failures
    records = json.loads(answer)["leases"]
    if len(records) != 1:
        failures.append(f"history: the history holds {len(records)} leases, "
                        f"not one: {records!r}")
        return failures
    record = records[0]
    if record.get("outcome") != "merged":
        failures.append(f"history: the lease does not read as merged: "
                        f"{record!r}")
    if record.get("pr") != 12:
        failures.append(f"history: the lease records no pull request: "
                        f"{record!r}")
    if record.get("pr_state") != "MERGED":
        failures.append(f"history: the lease records no pull request state: "
                        f"{record!r}")
    if not record.get("finished_at"):
        failures.append(f"history: the lease records no finishing time: "
                        f"{record!r}")
    # A lane that ends twice keeps the ending it was given.
    status, answer = world.call(f"/api/lease/{lease}/finish",
                                token=TOKENS["writer"], method="POST",
                                body={"outcome": "abandoned", "pr": 13,
                                      "pr_state": "CLOSED"})
    if status != 200:
        failures.append(f"history: the second finish answered {status}, not "
                        f"200: {answer[:120]!r}")
    records = json.loads(world.call("/api/history", token=TOKENS["reader"])[1])["leases"]
    if records and records[0].get("outcome") != "merged":
        failures.append(f"history: the second ending replaced the first: "
                        f"{records[0]!r}")
    return failures


def test_dashboard(world: World) -> list[str]:
    """The dashboard shows both sections, and escapes what the record holds."""
    failures = []
    status, answer = world.claim(issue=5,
                                 window="<script>alert(1)</script>")
    if status != 200:
        return [f"dashboard: the claim failed: {answer!r}"]
    status, page = world.call("/", token=TOKENS["reader"])
    if status != 200:
        failures.append(f"dashboard: GET / answered {status}, not 200: "
                        f"{page[:120]!r}")
        return failures
    for section in ("id=\"now\"", "id=\"history\""):
        if section not in page:
            failures.append(f"dashboard: the page has no {section} section")
    if "&lt;script&gt;" not in page:
        failures.append("dashboard: the window name is not escaped")
    if "<script>alert" in page:
        failures.append("dashboard: the page carries the window name's own "
                        "script tag")
    if "http-equiv=\"refresh\"" not in page:
        failures.append("dashboard: the page does not refresh itself")
    # The page changes nothing: a write on it is refused.
    status, answer = world.call("/", token=TOKENS["writer"], method="POST",
                                body={})
    if status != 405:
        failures.append(f"dashboard: POST / answered {status}, not 405: "
                        f"{answer[:120]!r}")
    return failures


def test_tls(world: World) -> list[str]:
    """With a certificate and a key, the service answers HTTPS."""
    failures = []
    cert, key = world.cert, world.key
    settings = replace(lane_keeper.load_settings(world.config),
                       cert=cert, key=key)
    server = lane_keeper.make_server(settings)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        context = ssl.create_default_context(cafile=str(cert))
        request = urllib.request.Request(
            f"https://127.0.0.1:{port}/api/now", method="GET")
        request.add_header("Authorization", f"Bearer {TOKENS['reader']}")
        with urllib.request.urlopen(request, timeout=30,
                                    context=context) as response:
            status = response.status
            body = response.read().decode("utf-8")
        if status != 200:
            failures.append(f"tls: the answer over HTTPS was {status}, not 200")
        if "leases" not in body:
            failures.append(f"tls: the answer over HTTPS is not the record: "
                            f"{body!r}")
    except Exception as exc:  # noqa: BLE001 -- the case reports it
        failures.append(f"tls: the service did not answer over HTTPS: {exc}")
    finally:
        server.shutdown()
        server.server_close()
    return failures


def make_cert(tmp: Path) -> tuple[Path, Path]:
    """A certificate and a key for 127.0.0.1, made by openssl."""
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is not on PATH")
    cert = tmp / "tls-cert.pem"
    key = tmp / "tls-key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True)
    return cert, key


CASES = [
    ("no-token", test_no_token, 1),
    ("reader", test_reader, 1),
    ("reporter", test_reporter, 1),
    ("race", test_race, 1),
    ("same-issue", test_same_issue, 2),
    ("default-branch", test_default_branch, 1),
    ("history", test_history, 1),
    ("dashboard", test_dashboard, 1),
    ("tls", test_tls, 1),
]


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for name, case, pool_size in CASES:
            directory = tmp / name
            directory.mkdir()
            world = World(directory, pool_size=pool_size, tls=(name == "tls"))
            try:
                failures.extend(case(world))
            except AssertionError as exc:
                failures.append(f"{name}: unexpected assertion error: {exc}")
            finally:
                world.close()
    if failures:
        print("FAIL:")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"PASS {len(CASES)} lane keeper cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
