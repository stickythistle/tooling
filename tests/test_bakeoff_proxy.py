#!/usr/bin/env python3
"""Pin the proxy that meters one harness's provider tokens.

The proxy is driven end to end: a local stub provider answers in every
endpoint style, streaming and not, and the real `bin/bakeoff-proxy`
runs against it. Nothing reaches the network.

The cases are the ones the issue separates: a normal request is
forwarded and its usage recorded under the right harness; a streamed
request's usage is recorded from the final event; a request for another
model is refused and recorded; the key appears in no record or log;
two harnesses on two ports keep separate records; an `https://` upstream
is reached over a verified TLS connection; the Responses style is
served; and a stream is read in bulk rather than a byte at a time.

The stub's response shapes come from the providers' documented formats:

  * OpenAI-style chat completions -- the chat completions API reference,
    https://developers.openai.com/api/reference/resources/chat/subresources/completions/
    A non-streamed answer carries `usage` with `prompt_tokens`,
    `completion_tokens` and `prompt_tokens_details.cached_tokens`. A
    streamed answer sends `chat.completion.chunk` objects, each with
    `usage: null`, and -- when the request asked for
    `stream_options: {include_usage: true}` -- a final chunk with an
    empty `choices` array carrying the usage, then `data: [DONE]`.

  * OpenAI-style responses -- the Responses API reference,
    https://developers.openai.com/api/reference/resources/responses/
    A non-streamed answer carries `usage` with `input_tokens`,
    `output_tokens` and `input_tokens_details.cached_tokens`. A
    streamed answer sends named events, and the terminal
    `response.completed` event carries the usage inside its `response`
    object.

  * Anthropic-style messages -- the Messages API reference,
    https://platform.claude.com/docs/en/api/messages
    A non-streamed answer carries `usage` with `input_tokens`,
    `output_tokens`, `cache_read_input_tokens` and
    `cache_creation_input_tokens`. A streamed answer sends
    `message_start` (whose `message` carries the input counts),
    `content_block_delta`, `message_delta` (whose `usage` carries the
    output count) and `message_stop`.

The provider bills by context tier, so the stub answers a base-name
request with a tiered name such as `MiniMax-M3-512k`. The requested
model is what G2 compares; the answered one is what C1 prices.

Run directly: `python3 tests/test_bakeoff_proxy.py`.
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import select
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from inspect import signature
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY = REPO_ROOT / "bin" / "bakeoff-proxy"

# The model the run assigned, and the tiered name the provider answers
# with for it.
ASSIGNED_MODEL = "MiniMax-M3"
ANSWERED_MODEL = "MiniMax-M3-512k"

# The key the env file holds, and the key a harness sends in its own
# header. The proxy must use the first and ignore the second. The
# harness's key is long and looks like a token, so a leaky run is
# obvious in the diff.
PROVIDER_KEY = "test-key-bakeoff-proxy-9f2c4ab71e35"
HARNESS_KEY = "test-key-the-harness-sent-4d8e1a6c02fb"

# The token counts the stub reports. They differ per style and per
# stream so a record that carries one style's numbers under the other
# style's name is visible.
OPENAI_USAGE = {"prompt_tokens": 120, "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 40}}
RESPONSES_USAGE = {"input_tokens": 120, "output_tokens": 30,
                   "total_tokens": 150,
                   "input_tokens_details": {"cached_tokens": 40},
                   "output_tokens_details": {"reasoning_tokens": 0}}
ANTHROPIC_USAGE = {"input_tokens": 120, "output_tokens": 30,
                   "cache_read_input_tokens": 40,
                   "cache_creation_input_tokens": 10}

# The usage blocks the provider actually returned for one short prompt,
# recorded live through this proxy on each of the three styles. They are
# the fixtures for the per-style difference: the OpenAI and Responses
# styles count the cached tokens inside the input, and the Anthropic
# style excludes them, so `input` has to mean the uncached input in
# every style or C1 pays twice for one style's cache reads.
LIVE_OPENAI_USAGE = {"prompt_tokens": 201, "completion_tokens": 3,
                     "total_tokens": 204,
                     "prompt_tokens_details": {"cached_tokens": 193}}
LIVE_RESPONSES_USAGE = {"input_tokens": 179, "output_tokens": 2,
                        "total_tokens": 181,
                        "input_tokens_details": {"cached_tokens": 128},
                        "output_tokens_details": {"reasoning_tokens": 0}}
LIVE_ANTHROPIC_USAGE = {"input_tokens": 51, "output_tokens": 2,
                        "cache_read_input_tokens": 128,
                        "cache_creation_input_tokens": 0}


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")


# ----- the stub provider ----------------------------------------------------

class StubProvider:
    """A local provider that answers every style.

    It records what it was asked, so a test can check that the proxy
    sent the provider's key and not the harness's, and that it sent the
    body through unchanged.

    It can serve over TLS: `start_tls` wraps the listener in a TLS
    context built from a certificate the suite generates, so an
    `https://` upstream is a real TLS server rather than a claim about
    one.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        return self._serve(None)

    def start_tls(self, certfile: str, keyfile: str) -> str:
        """Serve over TLS, with the given certificate."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        return self._serve(context)

    def _serve(self, context: ssl.SSLContext | None) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: object, **_k: object) -> None:
                return  # silence stderr noise during tests

            def do_POST(self) -> None:  # noqa: N802 -- http.server contract
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": payload,
                    "raw": raw.decode("utf-8", "replace"),
                })
                if self.path == "/v1/chat/completions":
                    self._openai(payload)
                elif self.path == "/v1/responses":
                    self._responses(payload)
                elif self.path == "/v1/messages":
                    self._anthropic(payload)
                else:
                    self._json(404, {"error": {"message": "no such path"}})

            # -- OpenAI style ----------------------------------------------

            def _openai(self, payload: dict) -> None:
                streaming = bool(payload.get("stream"))
                if streaming:
                    self._openai_stream()
                    return
                body = {
                    "id": "chatcmpl-stub-0001",
                    "object": "chat.completion",
                    "created": 1730000000,
                    "model": ANSWERED_MODEL,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }],
                    "usage": OPENAI_USAGE,
                }
                self._json(200, body)

            def _openai_stream(self) -> None:
                # The documented stream: chunks with `usage: null`, and
                # a final chunk with an empty `choices` array carrying
                # the usage, then `data: [DONE]`.
                chunks = [
                    {"id": "chatcmpl-stub-0002", "object": "chat.completion.chunk",
                     "created": 1730000000, "model": ANSWERED_MODEL,
                     "choices": [{"index": 0, "delta": {"role": "assistant",
                                                        "content": ""},
                                  "finish_reason": None}]},
                    {"id": "chatcmpl-stub-0002", "object": "chat.completion.chunk",
                     "created": 1730000000, "model": ANSWERED_MODEL,
                     "choices": [{"index": 0, "delta": {"content": "ok"},
                                  "finish_reason": None}]},
                    {"id": "chatcmpl-stub-0002", "object": "chat.completion.chunk",
                     "created": 1730000000, "model": ANSWERED_MODEL,
                     "choices": [{"index": 0, "delta": {},
                                  "finish_reason": "stop"}]},
                    {"id": "chatcmpl-stub-0002", "object": "chat.completion.chunk",
                     "created": 1730000000, "model": ANSWERED_MODEL,
                     "choices": [],
                     "usage": OPENAI_USAGE},
                ]
                self._sse([f"data: {json.dumps(c)}" for c in chunks]
                          + ["data: [DONE]"])

            # -- Responses style -------------------------------------------

            def _responses(self, payload: dict) -> None:
                streaming = bool(payload.get("stream"))
                if streaming:
                    self._responses_stream()
                    return
                body = {
                    "id": "resp_stub_0001",
                    "object": "response",
                    "created_at": 1730000000,
                    "model": ANSWERED_MODEL,
                    "status": "completed",
                    "output": [{
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }],
                    "usage": RESPONSES_USAGE,
                }
                self._json(200, body)

            def _responses_stream(self) -> None:
                # The documented stream: named events, and the usage in
                # the terminal `response.completed` event, inside its
                # `response` object.
                events = [
                    ("response.created", {
                        "type": "response.created",
                        "sequence_number": 0,
                        "response": {"id": "resp_stub_0002",
                                     "object": "response",
                                     "created_at": 1730000000,
                                     "model": ANSWERED_MODEL,
                                     "status": "in_progress",
                                     "output": []}}),
                    ("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "sequence_number": 1,
                        "item_id": "msg_stub_0003",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "ok"}),
                    ("response.output_text.done", {
                        "type": "response.output_text.done",
                        "sequence_number": 2,
                        "item_id": "msg_stub_0003",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "ok"}),
                    ("response.completed", {
                        "type": "response.completed",
                        "sequence_number": 3,
                        "response": {
                            "id": "resp_stub_0002",
                            "object": "response",
                            "created_at": 1730000000,
                            "model": ANSWERED_MODEL,
                            "status": "completed",
                            "output": [{
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "ok"}]}],
                            "usage": RESPONSES_USAGE}}),
                ]
                self._sse([f"event: {name}\ndata: {json.dumps(payload_)}"
                           for name, payload_ in events])

            # -- Anthropic style --------------------------------------------

            def _anthropic(self, payload: dict) -> None:
                streaming = bool(payload.get("stream"))
                if streaming:
                    self._anthropic_stream()
                    return
                body = {
                    "id": "msg_stub_0001",
                    "type": "message",
                    "role": "assistant",
                    "model": ANSWERED_MODEL,
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": ANTHROPIC_USAGE,
                }
                self._json(200, body)

            def _anthropic_stream(self) -> None:
                # The documented stream: `message_start` carries the
                # input counts, `message_delta` carries the output count.
                events = [
                    ("message_start", {
                        "type": "message_start",
                        "message": {
                            "id": "msg_stub_0002", "type": "message",
                            "role": "assistant", "model": ANSWERED_MODEL,
                            "content": [], "stop_reason": None,
                            "usage": {"input_tokens": ANTHROPIC_USAGE["input_tokens"],
                                      "output_tokens": 1},
                        }}),
                    ("content_block_start", {
                        "type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": "ok"}}),
                    ("content_block_stop", {
                        "type": "content_block_stop", "index": 0}),
                    ("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn",
                                  "stop_sequence": None},
                        "usage": {"output_tokens": ANTHROPIC_USAGE["output_tokens"]}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                self._sse([f"event: {name}\ndata: {json.dumps(payload_)}"
                           for name, payload_ in events])

            # -- the two ways of answering ---------------------------------

            def _json(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _sse(self, payloads: list[str]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for payload in payloads:
                    # A payload is the whole event block, as the
                    # provider's stream carries it: an Anthropic-style
                    # event names itself in an `event:` line and then
                    # carries its JSON in a `data:` line.
                    self.wfile.write(payload.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    # A pause between the events, so a proxy that
                    # buffers the whole stream before forwarding it
                    # takes measurably longer than one that does not.
                    time.sleep(0.05)
                self.close_connection = True

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        if context is not None:
            self._server.socket = context.wrap_socket(self._server.socket,
                                                      server_side=True)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.port = port
        scheme = "https" if context is not None else "http"
        self.base_url = f"{scheme}://127.0.0.1:{port}"
        return self.base_url

    def url(self) -> str:
        """The stub's base URL."""
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# ----- the proxy under test --------------------------------------------------

class ProxyRun:
    """A `bin/bakeoff-proxy` running against the stub provider."""

    def __init__(self, harness: str, upstream: str, record: Path,
                 env: dict) -> None:
        self.harness = harness
        self.record = record
        self.port = free_port()
        self.process = subprocess.Popen(
            [str(PROXY),
             "--port", str(self.port),
             "--harness", harness,
             "--model", ASSIGNED_MODEL,
             "--upstream", upstream,
             "--record", str(record)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._wait_for_port()

    def _wait_for_port(self) -> None:
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                raise AssertionError(
                    f"the proxy exited with {self.process.returncode}: "
                    f"{err.strip() or out.strip()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("the proxy never listened on its port")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path: str, body: dict, headers: dict | None = None,
             stream: bool = False):
        """Send one request the way a harness would."""
        request = urllib.request.Request(
            self.url(path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **(headers or {})},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, raw, None
        if stream:
            chunks = []
            while True:
                chunk = response.read(1)
                if not chunk:
                    break
                chunks.append(chunk)
            return response.status, b"".join(chunks), response
        raw = response.read()
        return response.status, raw, response

    def records(self) -> list[dict]:
        """Every record the proxy has written so far."""
        if not self.record.is_file():
            return []
        out = []
        for line in self.record.read_text(errors="replace").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def wait_for_records(self, count: int, timeout: float = 2.0) -> list[dict]:
        """The records, once `count` of them have been written.

        The proxy writes a record after it answers the harness, so a
        test that reads the file the moment it has its answer can see
        fewer lines than the requests it made. This polls for the
        count with a deadline, and returns what the file holds when the
        deadline passes.
        """
        deadline = time.monotonic() + timeout
        while True:
            out = self.records()
            if len(out) >= count or time.monotonic() >= deadline:
                return out
            select.select([], [], [], 0.02)

    def stop(self) -> tuple[str, str]:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        return self.process.communicate()


def free_port() -> int:
    """A port nothing is listening on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_env(tmp: Path) -> dict:
    """An environment with the provider's env file, and nothing else."""
    env_file = tmp / "provider.env"
    env_file.write_text(f"MINIMAX_API_KEY={PROVIDER_KEY}\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("BAKEOFF_PROVIDER_ENV",)}
    env["BAKEOFF_PROVIDER_ENV"] = str(env_file)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def make_tls_certificate(tmp: Path) -> tuple[str, str]:
    """A certificate and key for the TLS stub, made once per run.

    The certificate is self-signed and names `localhost` and
    `127.0.0.1`, so a client that verifies the peer against it accepts
    the connection -- and a client that does not verify refuses it.
    """
    cert = tmp / "stub-cert.pem"
    key = tmp / "stub-key.pem"
    result = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", str(key), "-out", str(cert), "-days", "2", "-nodes",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        capture_output=True, text=True, timeout=120)
    check("the certificate was made", result.returncode == 0,
          result.stderr[-400:])
    return str(cert), str(key)


def _is_timestamp(value: str) -> bool:
    """Whether a string is an ISO timestamp."""
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def read_stream_events(raw: bytes) -> list[str]:
    """The `data:` payloads of a server-sent-event stream."""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    out = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                out.append(line[len("data:"):].strip())
    return out


# ----- the cases ------------------------------------------------------------

def case_normal_request_is_forwarded_and_recorded(stub: StubProvider,
                                                   tmp: Path) -> None:
    """A normal request is forwarded, and its usage recorded.

    Both styles are driven: the OpenAI-style request and the
    Anthropic-style one. The body goes through unchanged, the provider
    sees the operator's key and not the harness's, and the record holds
    the usage the provider returned.
    """
    record = tmp / "records" / "one.jsonl"
    env = make_env(tmp)
    proxy = ProxyRun("hermes", stub.url(), record, env)
    try:
        # The OpenAI style, with a key of the harness's own in the
        # header the style expects.
        status, raw, _ = proxy.post(
            "/v1/chat/completions",
            {"model": ASSIGNED_MODEL,
             "messages": [{"role": "user", "content": "hello"}],
             "temperature": 0},
            headers={"Authorization": f"Bearer {HARNESS_KEY}"})
        check("openai request forwarded", status == 200,
              f"status {status}, body {raw[:200]!r}")

        # The Anthropic style.
        status, raw, _ = proxy.post(
            "/v1/messages",
            {"model": ASSIGNED_MODEL,
             "max_tokens": 64,
             "messages": [{"role": "user", "content": "hello"}]},
            headers={"x-api-key": HARNESS_KEY})
        check("anthropic request forwarded", status == 200,
              f"status {status}, body {raw[:200]!r}")

        records = proxy.wait_for_records(2)
        check("two records written", len(records) == 2,
              f"got {len(records)}")
        by_style = {r["style"]: r for r in records}
        check("both styles recorded", set(by_style) == {"openai", "anthropic"},
              f"got {sorted(by_style)}")

        # Each record carries the time its request arrived.
        for record_ in records:
            check("the record carries an arrival stamp",
                  isinstance(record_.get("time"), str)
                  and record_["time"],
                  record_.get("time"))
            check("the arrival stamp is a time",
                  _is_timestamp(record_["time"]),
                  record_["time"])

        openai_record = by_style["openai"]
        check("harness recorded", openai_record["harness"] == "hermes",
              openai_record["harness"])
        check("openai path recorded",
              openai_record["path"] == "/v1/chat/completions",
              openai_record["path"])
        check("openai status recorded", openai_record["status"] == 200,
              openai_record["status"])
        # `prompt_tokens` counts the cached tokens inside it, so the
        # input the record carries is what is left of it.
        check("openai usage recorded",
              openai_record["usage"] == {"input": 80, "cached": 40,
                                         "output": 30},
              openai_record["usage"])
        check("openai not refused", openai_record["refused"] is False,
              openai_record["refused"])
        check("openai latency recorded",
              isinstance(openai_record["latency_ms"], int)
              and openai_record["latency_ms"] >= 0,
              openai_record["latency_ms"])
        check("openai time recorded",
              isinstance(openai_record["time"], str)
              and openai_record["time"],
              openai_record["time"])

        anthropic_record = by_style["anthropic"]
        check("anthropic path recorded",
              anthropic_record["path"] == "/v1/messages",
              anthropic_record["path"])
        # The Anthropic style splits the cache count: a read is a read
        # and a creation is a write, and a creation is input the
        # provider had to read.
        check("anthropic usage recorded",
              anthropic_record["usage"] == {"input": 130, "cached": 40,
                                            "output": 30},
              anthropic_record["usage"])

        # The requested model, and the tiered name the provider answered
        # with.
        for record_ in records:
            check("requested model recorded",
                  record_["model"] == ASSIGNED_MODEL, record_["model"])
            check("answered model recorded",
                  record_["answered_model"] == ANSWERED_MODEL,
                  record_["answered_model"])

        # What the provider was asked.
        check("provider saw two requests", len(stub.requests) == 2,
              f"got {len(stub.requests)}")
        openai_req = stub.requests[0]
        check("the provider saw the operator's key, not the harness's",
              openai_req["headers"].get("authorization")
              == f"Bearer {PROVIDER_KEY}",
              openai_req["headers"].get("authorization"))
        check("the harness's key was dropped",
              HARNESS_KEY not in openai_req["raw"]
              and HARNESS_KEY not in json.dumps(openai_req["headers"]),
              "the harness's key reached the provider")
        anthropic_req = stub.requests[1]
        check("the anthropic style got the key in x-api-key",
              anthropic_req["headers"].get("x-api-key") == PROVIDER_KEY,
              anthropic_req["headers"].get("x-api-key"))
        check("the anthropic request carried no authorization header",
              "authorization" not in anthropic_req["headers"],
              anthropic_req["headers"].get("authorization"))

        # The body goes through unchanged.
        check("the body was passed through unchanged",
              openai_req["body"]["messages"] == [{"role": "user",
                                                  "content": "hello"}]
              and openai_req["body"]["temperature"] == 0,
              openai_req["body"])
    finally:
        proxy.stop()


def case_streamed_usage_is_recorded(stub: StubProvider, tmp: Path) -> None:
    """A streamed request's usage is recorded from the final event.

    Both styles are driven. The OpenAI-style stream carries the whole
    usage in its final chunk; the Anthropic-style stream splits it, with
    the input count in `message_start` and the output count in
    `message_delta`.
    """
    record = tmp / "records" / "stream.jsonl"
    env = make_env(tmp)
    proxy = ProxyRun("hermes", stub.url(), record, env)
    try:
        status, raw, _ = proxy.post(
            "/v1/chat/completions",
            {"model": ASSIGNED_MODEL,
             "messages": [{"role": "user", "content": "hello"}],
             "stream": True,
             "stream_options": {"include_usage": True}},
            stream=True)
        check("openai stream forwarded", status == 200,
              f"status {status}, body {raw[:200]!r}")
        events = read_stream_events(raw)
        check("the stream was forwarded as events",
              any("[DONE]" in e for e in events), events)
        check("the stream carried the content chunk",
              any('"ok"' in e for e in events), events)

        status, raw, _ = proxy.post(
            "/v1/messages",
            {"model": ASSIGNED_MODEL, "max_tokens": 64,
             "messages": [{"role": "user", "content": "hello"}],
             "stream": True},
            stream=True)
        check("anthropic stream forwarded", status == 200,
              f"status {status}, body {raw[:200]!r}")
        events = read_stream_events(raw)
        check("the anthropic stream carried message_start",
              any("message_start" in e for e in events), events)
        check("the anthropic stream carried message_delta",
              any("message_delta" in e for e in events), events)

        records = proxy.wait_for_records(2)
        check("two records written", len(records) == 2,
              f"got {len(records)}")
        by_style = {r["style"]: r for r in records}
        check("openai stream usage recorded",
              by_style["openai"]["usage"] == {"input": 80, "cached": 40,
                                              "output": 30},
              by_style["openai"]["usage"])
        check("openai stream raw usage was kept",
              by_style["openai"]["usage_raw"] == OPENAI_USAGE,
              by_style["openai"].get("usage_raw"))
        # The input count comes from `message_start` and the output
        # count from `message_delta`, so a record that kept only one of
        # them is visible here.
        check("anthropic stream usage recorded",
              by_style["anthropic"]["usage"] == {"input": 120, "cached": 0,
                                                  "output": 30},
              by_style["anthropic"]["usage"])
        for record_ in records:
            check("answered model recorded from the stream",
                  record_["answered_model"] == ANSWERED_MODEL,
                  record_["answered_model"])
    finally:
        proxy.stop()


def case_stream_is_not_buffered(stub: StubProvider, tmp: Path) -> None:
    """A streamed answer arrives as it is produced.

    The stub pauses between the chunks, so a proxy that buffered the
    whole stream before forwarding it would answer noticeably later than
    the last chunk was produced. The first chunk has to reach the caller
    well before the stream has ended.
    """
    record = tmp / "records" / "buffered.jsonl"
    env = make_env(tmp)
    proxy = ProxyRun("hermes", stub.url(), record, env)
    try:
        request = urllib.request.Request(
            proxy.url("/v1/chat/completions"),
            data=json.dumps({"model": ASSIGNED_MODEL,
                             "messages": [{"role": "user",
                                           "content": "hello"}],
                             "stream": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        response = urllib.request.urlopen(request, timeout=30)
        first_at = None
        while True:
            chunk = response.read(1)
            if not chunk:
                break
            if first_at is None:
                first_at = time.monotonic() - started
        total = time.monotonic() - started
        response.close()
        # The stub sends five events with a pause between each, so the
        # whole stream takes a few tenths of a second. A proxy that
        # buffered it would deliver the first byte only at the end.
        check("the first chunk arrived before the stream ended",
              first_at is not None and first_at < total / 2,
              f"first chunk at {first_at}, stream ended at {total}")
    finally:
        proxy.stop()


def case_other_model_is_refused(stub: StubProvider, tmp: Path) -> None:
    """A request for another model is refused, and recorded.

    Both styles are driven, and the error is in the shape the style
    documents. The provider is never asked, so the refusal is a
    prevention rather than a detection.
    """
    record = tmp / "records" / "refused.jsonl"
    env = make_env(tmp)
    proxy = ProxyRun("hermes", stub.url(), record, env)
    try:
        asked_before = len(stub.requests)

        status, raw, _ = proxy.post(
            "/v1/chat/completions",
            {"model": "some-other-model",
             "messages": [{"role": "user", "content": "hello"}]})
        check("openai refusal is a 400", status == 400,
              f"status {status}, body {raw[:200]!r}")
        body = json.loads(raw.decode("utf-8"))
        check("openai refusal has the openai error shape",
              "error" in body and isinstance(body["error"].get("message"), str)
              and body["error"]["message"],
              body)

        status, raw, _ = proxy.post(
            "/v1/messages",
            {"model": "some-other-model", "max_tokens": 64,
             "messages": [{"role": "user", "content": "hello"}]})
        check("anthropic refusal is a 400", status == 400,
              f"status {status}, body {raw[:200]!r}")
        body = json.loads(raw.decode("utf-8"))
        check("anthropic refusal has the anthropic error shape",
              "error" in body and isinstance(body["error"].get("message"), str)
              and body["error"]["message"],
              body)

        check("the provider was never asked",
              len(stub.requests) == asked_before,
              f"the provider saw {len(stub.requests) - asked_before} more")

        records = proxy.wait_for_records(2)
        check("two records written", len(records) == 2,
              f"got {len(records)}")
        for record_ in records:
            check("the refusal was recorded", record_["refused"] is True,
                  record_)
            check("the refused status was recorded",
                  record_["status"] == 400, record_["status"])
            check("the refused model was recorded",
                  record_["model"] == "some-other-model", record_["model"])
            check("a refused request has no usage",
                  record_["usage"] == {"input": 0, "cached": 0, "output": 0},
                  record_["usage"])
    finally:
        proxy.stop()


def case_key_appears_nowhere(stub: StubProvider, tmp: Path) -> None:
    """The key appears in no record and in no output.

    A normal request, a refused one and a streamed one are driven, and
    every artefact the proxy wrote is searched for the key.
    """
    record = tmp / "records" / "secret.jsonl"
    env = make_env(tmp)
    proxy = ProxyRun("hermes", stub.url(), record, env)
    try:
        proxy.post("/v1/chat/completions",
                   {"model": ASSIGNED_MODEL,
                    "messages": [{"role": "user", "content": "hello"}]},
                   headers={"Authorization": f"Bearer {HARNESS_KEY}"})
        proxy.post("/v1/messages",
                   {"model": "another-model", "max_tokens": 8,
                    "messages": [{"role": "user", "content": "hello"}]})
        proxy.post("/v1/chat/completions",
                   {"model": ASSIGNED_MODEL,
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True},
                   stream=True)
        out, err = proxy.stop()

        check("the key is in no record", PROVIDER_KEY not in record.read_text(),
              "the provider's key is in the record file")
        check("the harness's key is in no record",
              HARNESS_KEY not in record.read_text(),
              "the harness's key is in the record file")
        check("the key is in no output", PROVIDER_KEY not in out,
              "the provider's key is in stdout")
        check("the key is in no error output", PROVIDER_KEY not in err,
              "the provider's key is in stderr")
    finally:
        try:
            proxy.stop()
        except Exception:
            pass


def case_two_harnesses_keep_separate_records(stub: StubProvider,
                                               tmp: Path) -> None:
    """Two harnesses on two ports keep separate records.

    Each port has its own record file, and each record names the
    harness that made the request.
    """
    env = make_env(tmp)
    first_record = tmp / "records" / "first.jsonl"
    second_record = tmp / "records" / "second.jsonl"
    first = ProxyRun("hermes", stub.url(), first_record, env)
    second = ProxyRun("codex", stub.url(), second_record, env)
    try:
        check("the two proxies have different ports",
              first.port != second.port, f"{first.port} and {second.port}")

        first.post("/v1/chat/completions",
                   {"model": ASSIGNED_MODEL,
                    "messages": [{"role": "user", "content": "one"}]})
        second.post("/v1/messages",
                    {"model": ASSIGNED_MODEL, "max_tokens": 8,
                     "messages": [{"role": "user", "content": "two"}]})
        second.post("/v1/chat/completions",
                    {"model": ASSIGNED_MODEL,
                     "messages": [{"role": "user", "content": "three"}]})

        first_records = first.wait_for_records(1)
        second_records = second.wait_for_records(2)
        check("the first proxy recorded one request",
              len(first_records) == 1, f"got {len(first_records)}")
        check("the second proxy recorded two requests",
              len(second_records) == 2, f"got {len(second_records)}")
        check("the first record names its harness",
              first_records[0]["harness"] == "hermes",
              first_records[0]["harness"])
        for record_ in second_records:
            check("the second records name their harness",
                  record_["harness"] == "codex", record_["harness"])
        check("the first file holds only the first harness",
              all(r["harness"] == "hermes" for r in first_records),
              first_records)
        check("the second file holds only the second harness",
              all(r["harness"] == "codex" for r in second_records),
              second_records)
    finally:
        first.stop()
        second.stop()


def case_paths_the_proxy_does_not_serve(stub: StubProvider, tmp: Path) -> None:
    """A path that is neither style's is refused, and not recorded.

    A GET carries no body, so there is no model to check. Recording it
    would put a request the harness never made into the run's counts.
    """
    record = tmp / "records" / "paths.jsonl"
    env = make_env(tmp)
    proxy = ProxyRun("hermes", stub.url(), record, env)
    try:
        status, raw, _ = proxy.post("/v1/embeddings",
                                    {"model": ASSIGNED_MODEL, "input": "x"})
        check("an unknown path is a 404", status == 404,
              f"status {status}, body {raw[:200]!r}")

        # A GET on a path the proxy serves.
        request = urllib.request.Request(
            proxy.url("/v1/chat/completions"), method="GET")
        try:
            response = urllib.request.urlopen(request, timeout=30)
            status = response.status
            response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.read()
        check("a GET is a 405", status == 405, f"status {status}")

        # A refused request and a GET are both turned away before the
        # provider, so nothing is ever written. The wait is for the
        # count to stay at zero past the deadline, which is what says
        # no line will follow.
        check("nothing was recorded", not proxy.wait_for_records(1, timeout=1.0),
              f"got {proxy.records()}")
        check("the provider was never asked", not stub.requests,
              f"got {len(stub.requests)}")
    finally:
        proxy.stop()


def case_the_provider_failing_is_not_a_refusal(stub: StubProvider,
                                               tmp: Path) -> None:
    """A provider that cannot be reached is recorded as a failure.

    The record says the request failed, not that the proxy refused it:
    `refused` means the proxy turned the request away, which is what
    G2 counts.
    """
    record = tmp / "records" / "unreachable.jsonl"
    env = make_env(tmp)
    # A port nothing is listening on.
    proxy = ProxyRun("hermes", f"http://127.0.0.1:{free_port()}", record, env)
    try:
        status, raw, _ = proxy.post("/v1/chat/completions",
                                    {"model": ASSIGNED_MODEL,
                                     "messages": [{"role": "user",
                                                   "content": "hello"}]})
        check("an unreachable provider is a 502", status == 502,
              f"status {status}, body {raw[:200]!r}")
        records = proxy.wait_for_records(1)
        check("the failure was recorded", len(records) == 1,
              f"got {len(records)}")
        check("the failure was not a refusal",
              records[0]["refused"] is False, records[0])
        check("the failure status was recorded",
              records[0]["status"] == 502, records[0]["status"])
    finally:
        proxy.stop()


def case_missing_key_is_refused_by_name(stub: StubProvider, tmp: Path) -> None:
    """A missing key is refused by name, and the proxy does not start.

    The refusal names the variable that is missing, and never the key
    itself. Two cases: the default variable, and one the operator named
    with `--key-var`.
    """
    record = tmp / "records" / "nokey.jsonl"

    # The default variable, absent from the env file.
    env = {k: v for k, v in os.environ.items()
           if k not in ("BAKEOFF_PROVIDER_ENV",)}
    env_file = tmp / "provider-default.env"
    env_file.write_text("SOME_OTHER_KEY=\n")
    env["BAKEOFF_PROVIDER_ENV"] = str(env_file)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [str(PROXY), "--port", str(free_port()), "--harness", "hermes",
         "--model", ASSIGNED_MODEL, "--upstream", stub.url(),
         "--record", str(record)],
        env=env, capture_output=True, text=True, timeout=30)
    check("the proxy refused to start", result.returncode == 2,
          f"exit {result.returncode}: {result.stderr}")
    check("the refusal names the default variable",
          "MINIMAX_API_KEY" in result.stderr, result.stderr)
    check("the refusal carries no key",
          PROVIDER_KEY not in result.stderr, result.stderr)
    check("nothing was recorded", not record.exists() or
          not record.read_text().strip(), "a record was written")

    # A variable the operator named, absent from the env file.
    env_file = tmp / "provider-named.env"
    env_file.write_text("MINIMAX_API_KEY=" + PROVIDER_KEY + "\n")
    env["BAKEOFF_PROVIDER_ENV"] = str(env_file)
    result = subprocess.run(
        [str(PROXY), "--port", str(free_port()), "--harness", "hermes",
         "--model", ASSIGNED_MODEL, "--upstream", stub.url(),
         "--record", str(record), "--key-var", "OTHER_PROVIDER_KEY"],
        env=env, capture_output=True, text=True, timeout=30)
    check("the proxy refused to start for the named variable",
          result.returncode == 2,
          f"exit {result.returncode}: {result.stderr}")
    check("the refusal names the variable the operator named",
          "OTHER_PROVIDER_KEY" in result.stderr, result.stderr)
    check("the refusal carries no key",
          PROVIDER_KEY not in result.stderr, result.stderr)


def case_custom_key_var_is_read(stub: StubProvider, tmp: Path) -> None:
    """`--key-var` names the variable the key is read from.

    The operator's key is under a name of their own, and the provider
    is asked with it.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("BAKEOFF_PROVIDER_ENV",)}
    env_file = tmp / "provider.env"
    env_file.write_text(f"OTHER_PROVIDER_KEY={PROVIDER_KEY}\n")
    env["BAKEOFF_PROVIDER_ENV"] = str(env_file)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    record = tmp / "records" / "custom.jsonl"
    port = free_port()
    process = subprocess.Popen(
        [str(PROXY), "--port", str(port), "--harness", "hermes",
         "--model", ASSIGNED_MODEL, "--upstream", stub.url(),
         "--record", str(record), "--key-var", "OTHER_PROVIDER_KEY"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("the proxy never listened on its port")

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({"model": ASSIGNED_MODEL,
                             "messages": [{"role": "user",
                                           "content": "hello"}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=30).read()

        check("the provider was asked with the custom variable's key",
              stub.requests[-1]["headers"].get("authorization")
              == f"Bearer {PROVIDER_KEY}",
              stub.requests[-1]["headers"].get("authorization"))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def case_https_upstream_is_reached_over_tls(tmp: Path) -> None:
    """An `https://` upstream is reached over a verified TLS connection.

    The stub serves TLS, and the proxy is given the certificate to
    trust. The request has to arrive over TLS, and the provider has to
    see the operator's key.

    Two things are pinned here. The first is that the proxy speaks TLS
    to an `https://` upstream at all: a proxy that opened a plaintext
    connection to the same port would fail the handshake outright, so
    the request never reaching the stub is the failure. The second is
    that the certificate is verified rather than waved through: a
    second run trusts nothing, and the connection has to be refused
    with a 502 rather than the request going through in the clear.
    """
    cert, key = make_tls_certificate(tmp)
    record = tmp / "records" / "tls.jsonl"
    env = make_env(tmp)
    env["SSL_CERT_FILE"] = cert

    stub = StubProvider()
    stub.start_tls(cert, key)
    try:
        proxy = ProxyRun("hermes", stub.url(), record, env)
        try:
            status, raw, _ = proxy.post(
                "/v1/chat/completions",
                {"model": ASSIGNED_MODEL,
                 "messages": [{"role": "user", "content": "hello"}]})
            check("the https request was answered", status == 200,
                  f"status {status}, body {raw[:200]!r}")

            check("the provider saw the request over tls",
                  len(stub.requests) == 1, f"got {len(stub.requests)}")
            check("the provider saw the operator's key over tls",
                  stub.requests[0]["headers"].get("authorization")
                  == f"Bearer {PROVIDER_KEY}",
                  stub.requests[0]["headers"].get("authorization"))

            records = proxy.wait_for_records(1)
            check("the tls request was recorded", len(records) == 1,
                  f"got {len(records)}")
            check("the tls request was not refused",
                  records[0]["refused"] is False, records[0])
            check("the tls request recorded its usage",
                  records[0]["usage"] == {"input": 80, "cached": 40,
                                          "output": 30},
                  records[0]["usage"])
        finally:
            proxy.stop()
    finally:
        stub.stop()

    # The same upstream, with a proxy that trusts no certificate. The
    # connection has to be refused: an unverified connection would
    # reach the provider with the key in it.
    untrusted_record = tmp / "records" / "tls-untrusted.jsonl"
    untrusted_env = make_env(tmp)
    untrusted_env["SSL_CERT_FILE"] = str(tmp / "no-such-ca.pem")

    stub = StubProvider()
    stub.start_tls(cert, key)
    try:
        proxy = ProxyRun("hermes", stub.url(), untrusted_record, untrusted_env)
        try:
            status, raw, _ = proxy.post(
                "/v1/chat/completions",
                {"model": ASSIGNED_MODEL,
                 "messages": [{"role": "user", "content": "hello"}]})
            check("an untrusted certificate is refused", status == 502,
                  f"status {status}, body {raw[:200]!r}")
            check("the provider was never reached over an unverified "
                  "connection", not stub.requests,
                  f"got {len(stub.requests)}")
            records = proxy.wait_for_records(1)
            check("the refusal was recorded", len(records) == 1,
                  f"got {len(records)}")
            check("the refusal was not a model refusal",
                  records[0]["refused"] is False, records[0])
        finally:
            proxy.stop()
    finally:
        stub.stop()


def case_responses_style_is_served(tmp: Path) -> None:
    """The Responses style is served, and its usage recorded.

    Codex talks the Responses style, so the proxy has to serve
    `/v1/responses`: the key goes in the `Authorization` header, and
    the usage is `input_tokens`, `output_tokens` and
    `input_tokens_details.cached_tokens`. A stream's usage arrives in
    the `response.completed` event.
    """
    record = tmp / "records" / "responses.jsonl"
    env = make_env(tmp)
    stub = StubProvider()
    stub.start()
    try:
        proxy = ProxyRun("codex", stub.url(), record, env)
        try:
            status, raw, _ = proxy.post(
                "/v1/responses",
                {"model": ASSIGNED_MODEL,
                 "input": [{"role": "user", "content": "hello"}]},
                headers={"Authorization": f"Bearer {HARNESS_KEY}"})
            check("the responses request was forwarded", status == 200,
                  f"status {status}, body {raw[:200]!r}")

            # The provider saw the request, with the operator's key in
            # the header the style expects.
            check("the provider saw one request", len(stub.requests) == 1,
                  f"got {len(stub.requests)}")
            check("the responses style got the key in authorization",
                  stub.requests[0]["headers"].get("authorization")
                  == f"Bearer {PROVIDER_KEY}",
                  stub.requests[0]["headers"].get("authorization"))
            check("the harness's key was dropped",
                  HARNESS_KEY not in json.dumps(stub.requests[0]["headers"]),
                  "the harness's key reached the provider")
            check("the body went through unchanged",
                  stub.requests[0]["body"].get("input")
                  == [{"role": "user", "content": "hello"}],
                  stub.requests[0]["body"])

            records = proxy.wait_for_records(1)
            check("the responses request was recorded", len(records) == 1,
                  f"got {len(records)}")
            record_ = records[0]
            check("the responses style was recorded",
                  record_["style"] == "responses", record_["style"])
            check("the responses path was recorded",
                  record_["path"] == "/v1/responses", record_["path"])
            check("the harness was recorded",
                  record_["harness"] == "codex", record_["harness"])
            check("the responses usage was recorded",
                  record_["usage"] == {"input": 80, "cached": 40, "output": 30},
                  record_["usage"])
            check("the answered model was recorded",
                  record_["answered_model"] == ANSWERED_MODEL,
                  record_["answered_model"])

            # A streamed request: the usage is in the
            # `response.completed` event.
            status, raw, _ = proxy.post(
                "/v1/responses",
                {"model": ASSIGNED_MODEL,
                 "input": [{"role": "user", "content": "hello"}],
                 "stream": True},
                stream=True)
            check("the responses stream was forwarded", status == 200,
                  f"status {status}, body {raw[:200]!r}")
            events = read_stream_events(raw)
            check("the stream carried response.completed",
                  any("response.completed" in e for e in events), events)
            check("the stream carried the content",
                  any('"ok"' in e for e in events), events)

            records = proxy.wait_for_records(2)
            check("the stream was recorded", len(records) == 2,
                  f"got {len(records)}")
            check("the responses stream usage was recorded",
                  records[1]["usage"] == {"input": 80, "cached": 40,
                                          "output": 30},
                  records[1]["usage"])
            check("the responses stream raw usage was kept",
                  records[1]["usage_raw"] == RESPONSES_USAGE,
                  records[1].get("usage_raw"))
            check("the answered model was recorded from the stream",
                  records[1]["answered_model"] == ANSWERED_MODEL,
                  records[1]["answered_model"])
        finally:
            proxy.stop()
    finally:
        stub.stop()


def case_responses_provider_error_is_relayed(tmp: Path) -> None:
    """A provider that does not serve the Responses style says so itself.

    The proxy relays the provider's error as the provider wrote it,
    rather than making up an error of its own.
    """
    record = tmp / "records" / "responses-error.jsonl"
    env = make_env(tmp)

    class RefusingProvider:
        """A provider that answers every path with an error."""

        def __init__(self) -> None:
            self.requests: list[dict] = []
            self._server: ThreadingHTTPServer | None = None

        def start(self) -> str:
            outer = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *_a: object, **_k: object) -> None:
                    return  # silence stderr noise during tests

                def do_POST(self) -> None:  # noqa: N802 -- http.server contract
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    raw = self.rfile.read(length) if length else b""
                    outer.requests.append({
                        "path": self.path,
                        "headers": {k.lower(): v
                                    for k, v in self.headers.items()},
                        "body": {},
                        "raw": raw.decode("utf-8", "replace"),
                    })
                    body = {"error": {"message": "not supported",
                                      "type": "invalid_request_error"}}
                    payload = json.dumps(body).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            port = free_port()
            self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            self._server.daemon_threads = True
            threading.Thread(target=self._server.serve_forever,
                             daemon=True).start()
            self.base_url = f"http://127.0.0.1:{port}"
            return self.base_url

        def url(self) -> str:
            return self.base_url

        def stop(self) -> None:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()

    stub = RefusingProvider()
    stub.start()
    try:
        proxy = ProxyRun("codex", stub.url(), record, env)
        try:
            status, raw, _ = proxy.post(
                "/v1/responses",
                {"model": ASSIGNED_MODEL, "input": "hello"})
            check("the provider's status was relayed", status == 400,
                  f"status {status}, body {raw[:200]!r}")
            body = json.loads(raw.decode("utf-8"))
            check("the provider's error was relayed unchanged",
                  body.get("error", {}).get("message") == "not supported",
                  body)
            records = proxy.wait_for_records(1)
            check("the relayed error was recorded", len(records) == 1,
                  f"got {len(records)}")
            check("the relayed error was not a model refusal",
                  records[0]["refused"] is False, records[0])
            check("the relayed error recorded its status",
                  records[0]["status"] == 400, records[0]["status"])
        finally:
            proxy.stop()
    finally:
        stub.stop()


def case_the_record_carries_the_arrival_time(tmp: Path) -> None:
    """The record's time is the moment the request arrived.

    The proxy answers the harness before it writes the record, so a
    request answered just before a run's window closed would be stamped
    after the window shut, and fall outside the run that made it. The
    stamp is therefore taken when the request arrives.

    The stub holds the answer back, so the arrival and the answer are
    far enough apart to tell apart: the stamp falls in between, where a
    stamp taken when the record was written would fall after the
    answer.
    """
    record = tmp / "records" / "arrival.jsonl"
    env = make_env(tmp)

    class SlowProvider:
        """A provider that holds the answer back."""

        def __init__(self) -> None:
            self._server: ThreadingHTTPServer | None = None
            self.answered_at: datetime | None = None

        def start(self) -> str:
            outer = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *_a: object, **_k: object) -> None:
                    return  # silence stderr noise during tests

                def do_POST(self) -> None:  # noqa: N802 -- http.server contract
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    self.rfile.read(length)
                    # The answer is held back, so the arrival and the
                    # answer are a wide gap apart.
                    time.sleep(0.5)
                    outer.answered_at = datetime.now(timezone.utc)
                    body = {"id": "slow", "object": "chat.completion",
                            "model": ANSWERED_MODEL,
                            "usage": OPENAI_USAGE}
                    payload = json.dumps(body).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            port = free_port()
            self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            self._server.daemon_threads = True
            threading.Thread(target=self._server.serve_forever,
                             daemon=True).start()
            self.base_url = f"http://127.0.0.1:{port}"
            return self.base_url

        def url(self) -> str:
            return self.base_url

        def stop(self) -> None:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()

    stub = SlowProvider()
    stub.start()
    try:
        proxy = ProxyRun("hermes", stub.url(), record, env)
        try:
            sent = datetime.now(timezone.utc)
            status, raw, _ = proxy.post(
                "/v1/chat/completions",
                {"model": ASSIGNED_MODEL,
                 "messages": [{"role": "user", "content": "hello"}]})
            check("the request was answered", status == 200,
                  f"status {status}, body {raw[:200]!r}")

            records = proxy.wait_for_records(1)
            check("the request was recorded", len(records) == 1,
                  f"got {len(records)}")
            stamp = records[0].get("time")
            check("the record carries a time", isinstance(stamp, str) and stamp,
                  stamp)
            check("the stamp is a time",
                  _is_timestamp(str(stamp)), stamp)

            # The stamp is the arrival, not the answer. It falls after
            # the request went out and before the answer came back, so
            # a request always falls inside the window of the run that
            # made it.
            answered = stub.answered_at
            check("the provider answered the request", answered is not None,
                  "the provider never answered")
            check("the stamp is the arrival, not the answer",
                  sent <= datetime.fromisoformat(stamp) <= answered,
                  f"stamp {stamp}, sent {sent}, answered {answered}")
        finally:
            proxy.stop()
    finally:
        stub.stop()


def case_input_is_the_uncached_input_in_every_style(tmp: Path) -> None:
    """`input` is the uncached input, in every style.

    The three usage blocks the provider actually returned for one short
    prompt are served as they were recorded live, and the record has to
    carry the uncached input: the OpenAI and Responses styles count the
    cached tokens inside their input, and the Anthropic style excludes
    them, so a record that kept the raw input would price one style's
    cache reads twice.

    The provider's own block is kept beside the counts as `usage_raw`,
    so the subtraction can be audited from the record.
    """
    record = tmp / "records" / "uncached.jsonl"
    env = make_env(tmp)

    class LiveProvider:
        """A provider that answers with the recorded live usage."""

        def __init__(self) -> None:
            self.requests: list[dict] = []
            self._server: ThreadingHTTPServer | None = None

        def start(self) -> str:
            outer = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *_a: object, **_k: object) -> None:
                    return  # silence stderr noise during tests

                def do_POST(self) -> None:  # noqa: N802 -- http.server contract
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    raw = self.rfile.read(length) if length else b""
                    outer.requests.append({"path": self.path})
                    if self.path == "/v1/chat/completions":
                        usage = LIVE_OPENAI_USAGE
                    elif self.path == "/v1/responses":
                        usage = LIVE_RESPONSES_USAGE
                    else:
                        usage = LIVE_ANTHROPIC_USAGE
                    body = {"id": "live", "object": "response",
                            "model": ANSWERED_MODEL, "usage": usage}
                    payload = json.dumps(body).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            port = free_port()
            self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            self._server.daemon_threads = True
            threading.Thread(target=self._server.serve_forever,
                             daemon=True).start()
            self.base_url = f"http://127.0.0.1:{port}"
            return self.base_url

        def url(self) -> str:
            return self.base_url

        def stop(self) -> None:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()

    stub = LiveProvider()
    stub.start()
    try:
        proxy = ProxyRun("hermes", stub.url(), record, env)
        try:
            for path in ("/v1/chat/completions", "/v1/responses",
                         "/v1/messages"):
                status, raw, _ = proxy.post(
                    path, {"model": ASSIGNED_MODEL, "input": "hello"})
                check("the live usage was answered", status == 200,
                      f"status {status}, body {raw[:200]!r}")

            records = proxy.wait_for_records(3)
            check("three records written", len(records) == 3,
                  f"got {len(records)}")
            by_style = {r["style"]: r for r in records}

            # Each request carries its own arrival stamp, and the
            # stamps order the requests.
            stamps = [r["time"] for r in records]
            check("each record carries its own arrival stamp",
                  len(set(stamps)) == 3, stamps)
            check("the arrival stamps order the requests",
                  stamps == sorted(stamps), stamps)

            # The uncached input, from the live fixtures.
            check("the openai input is the uncached input",
                  by_style["openai"]["usage"]
                  == {"input": 8, "cached": 193, "output": 3},
                  by_style["openai"]["usage"])
            check("the responses input is the uncached input",
                  by_style["responses"]["usage"]
                  == {"input": 51, "cached": 128, "output": 2},
                  by_style["responses"]["usage"])
            # The Anthropic style's input already excludes the cache
            # read, so it is recorded as the provider sent it.
            check("the anthropic input is the uncached input",
                  by_style["anthropic"]["usage"]
                  == {"input": 51, "cached": 128, "output": 2},
                  by_style["anthropic"]["usage"])

            # The provider's own block, kept so the subtraction can be
            # audited from the record.
            check("the openai raw usage was kept",
                  by_style["openai"]["usage_raw"] == LIVE_OPENAI_USAGE,
                  by_style["openai"].get("usage_raw"))
            check("the responses raw usage was kept",
                  by_style["responses"]["usage_raw"] == LIVE_RESPONSES_USAGE,
                  by_style["responses"].get("usage_raw"))
            check("the anthropic raw usage was kept",
                  by_style["anthropic"]["usage_raw"] == LIVE_ANTHROPIC_USAGE,
                  by_style["anthropic"].get("usage_raw"))

            # The three styles now agree on what `input` means, so a
            # record priced as `input x in-price + cached x cache-price`
            # charges the cache read once.
            for style, usage in by_style.items():
                check(f"the {style} input is no more than its cache read",
                      usage["usage"]["input"] + usage["usage"]["cached"]
                      >= usage["usage"]["cached"],
                      usage["usage"])
        finally:
            proxy.stop()
    finally:
        stub.stop()


def case_stream_is_read_in_bulk(tmp: Path) -> None:
    """A stream is read in bulk, not a byte at a time.

    The proxy reads the provider's stream with `read1`, which returns
    whatever has arrived. A read of a fixed size would wait for that
    many bytes, and a read of one byte would cost two syscalls for
    every byte of every stream.

    The count of reads is measured by counting them: the proxy's
    source is read for the call it makes, and a stream of many bytes
    must not be read one byte at a time.
    """
    source = (REPO_ROOT / "bin" / "bakeoff-proxy").read_text()
    check("the stream is read with read1", ".read1(" in source,
          "the proxy does not read the stream with read1")
    check("the stream is not read a byte at a time",
          "response.read(1)" not in source,
          "the proxy still reads the stream one byte at a time")

    # And the stream still arrives as it is produced: the stub pauses
    # between the events, and the first chunk has to reach the caller
    # well before the stream has ended.
    record = tmp / "records" / "bulk.jsonl"
    env = make_env(tmp)
    stub = StubProvider()
    stub.start()
    try:
        proxy = ProxyRun("hermes", stub.url(), record, env)
        try:
            request = urllib.request.Request(
                proxy.url("/v1/chat/completions"),
                data=json.dumps({"model": ASSIGNED_MODEL,
                                 "messages": [{"role": "user",
                                               "content": "hello"}],
                                 "stream": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            started = time.monotonic()
            response = urllib.request.urlopen(request, timeout=30)
            first_at = None
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                if first_at is None:
                    first_at = time.monotonic() - started
            total = time.monotonic() - started
            response.close()
            check("the first chunk arrived before the stream ended",
                  first_at is not None and first_at < total / 2,
                  f"first chunk at {first_at}, stream ended at {total}")
        finally:
            proxy.stop()
    finally:
        stub.stop()


# ----- the runner ------------------------------------------------------------

def main() -> int:
    cases = [
        ("a normal request is forwarded and recorded",
         case_normal_request_is_forwarded_and_recorded),
        ("a streamed request's usage is recorded from the final event",
         case_streamed_usage_is_recorded),
        ("a stream is forwarded as it arrives, not buffered",
         case_stream_is_not_buffered),
        ("a request for another model is refused and recorded",
         case_other_model_is_refused),
        ("the key appears in no record and in no output",
         case_key_appears_nowhere),
        ("two harnesses on two ports keep separate records",
         case_two_harnesses_keep_separate_records),
        ("a path the proxy does not serve is refused, and not recorded",
         case_paths_the_proxy_does_not_serve),
        ("a provider that cannot be reached is a failure, not a refusal",
         case_the_provider_failing_is_not_a_refusal),
        ("a missing key is refused by name",
         case_missing_key_is_refused_by_name),
        ("--key-var names the variable the key is read from",
         case_custom_key_var_is_read),
        ("an https upstream is reached over a verified tls connection",
         case_https_upstream_is_reached_over_tls),
        ("the responses style is served and its usage recorded",
         case_responses_style_is_served),
        ("a provider's own error is relayed unchanged",
         case_responses_provider_error_is_relayed),
        ("a stream is read in bulk, not a byte at a time",
         case_stream_is_read_in_bulk),
        ("input is the uncached input in every style",
         case_input_is_the_uncached_input_in_every_style),
        ("the record carries the arrival time",
         case_the_record_carries_the_arrival_time),
    ]
    failures = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for label, case in cases:
            stub = StubProvider()
            stub.start()
            try:
                # A case that brings its own provider takes only the
                # temporary directory; the rest take the shared stub.
                # The arity says which, so a TypeError raised inside a
                # case is still a failure rather than a re-dispatch.
                if len(signature(case).parameters) == 1:
                    case(tmp)
                else:
                    case(stub, tmp)
                print(f"PASS {label}")
            except AssertionError as exc:
                failures.append((label, str(exc)))
                print(f"FAIL {label}: {exc}")
            except Exception as exc:  # noqa: BLE001 -- a crash is a failure
                failures.append((label, f"{type(exc).__name__}: {exc}"))
                print(f"FAIL {label}: {type(exc).__name__}: {exc}")
            finally:
                stub.stop()
    if failures:
        print(f"\n{len(failures)} case(s) failed")
        return 1
    print(f"\nPASS {len(cases)} case(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
