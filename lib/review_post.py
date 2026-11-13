#!/usr/bin/env python3
"""Post one owner-written review, or refuse the whole review.

Every finding must be located at the named commit on a line GitHub accepts.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EVENTS = ("REQUEST_CHANGES", "APPROVE", "COMMENT")
REDACTION_MARKER = "*" * 3
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class PostError(Exception):
    """The review cannot be posted as written."""


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def gh(args: list[str]) -> subprocess.CompletedProcess:
    result = run(["gh", *args])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PostError(f"gh failed: {detail or 'no error output'}")
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="review-post",
        description="post one owner-written pull request review",
    )
    parser.add_argument("repository", help="OWNER/REPO")
    parser.add_argument("pr", help="pull request number")
    parser.add_argument("findings", help="findings JSON file")
    parser.add_argument("--sha", required=True, help="commit under review")
    parser.add_argument("--event", required=True, choices=EVENTS)
    parser.add_argument("--clone", required=True, dest="clone_dir",
                        help="local clone of the repository")
    parser.add_argument("--body", help="review body file")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the payload without posting")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repository):
        parser.error("repository must be OWNER/REPO")
    if not args.pr.isdigit() or int(args.pr) < 1:
        parser.error("pull request must be a positive number")
    if not args.sha:
        parser.error("--sha must not be empty")
    return args


def load_findings(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise PostError(f"cannot read findings file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PostError(f"cannot parse findings file {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise PostError(f"findings file {path} must contain a JSON array")

    findings: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PostError(f"finding[{index}] must be a JSON object")
        missing = [name for name in ("path", "line", "snippet", "body")
                   if name not in item]
        if missing:
            names = ", ".join(missing)
            raise PostError(f"finding[{index}] is missing: {names}")
        finding_path = item["path"]
        line = item["line"]
        snippet = item["snippet"]
        body = item["body"]
        if not isinstance(finding_path, str) or not finding_path:
            raise PostError(f"finding[{index}] path must be a non-empty string")
        if Path(finding_path).is_absolute() or ".." in Path(finding_path).parts:
            raise PostError(f"finding[{index}] path must be inside the repository")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise PostError(f"finding[{index}] line must be a positive integer")
        if not isinstance(snippet, str) or not snippet:
            raise PostError(f"finding[{index}] snippet must be a non-empty string")
        if not isinstance(body, str):
            raise PostError(f"finding[{index}] body must be a string")
        findings.append({
            "path": Path(finding_path).as_posix(),
            "line": line,
            "snippet": snippet,
            "body": body,
        })
    return findings


def pr_head(repository: str, pr: str) -> str:
    result = gh(["pr", "view", pr, "--repo", repository,
                 "--json", "headRefOid"])
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PostError("gh returned invalid JSON for the pull request head") from exc
    head = document.get("headRefOid") if isinstance(document, dict) else None
    if not isinstance(head, str) or not head:
        raise PostError("gh returned no pull request head")
    return head


def json_values(text: str) -> list[Any]:
    """Decode one JSON value or the concatenated values from gh pagination."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            return values
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise PostError("gh returned invalid JSON for the pull request files") from exc
        values.append(value)


def hunk_lines(patch: str) -> set[int]:
    lines: set[int] = set()
    for line in patch.splitlines():
        match = HUNK_HEADER.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count:
            lines.update(range(start, start + count))
    return lines


def pull_request_patch(repository: str, pr: str) -> dict[str, set[int]]:
    result = gh(["api", f"repos/{repository}/pulls/{pr}/files", "--paginate"])
    files: list[Any] = []
    for value in json_values(result.stdout):
        if not isinstance(value, list):
            raise PostError("gh returned invalid pull request files")
        files.extend(value)

    paths: dict[str, set[int]] = {}
    for item in files:
        if not isinstance(item, dict):
            raise PostError("gh returned an invalid pull request file")
        path = item.get("filename")
        patch = item.get("patch")
        if patch is None:
            patch = ""
        if not isinstance(path, str) or not isinstance(patch, str):
            raise PostError("gh returned an invalid pull request file")
        paths.setdefault(path, set()).update(hunk_lines(patch))
    return paths


def finding_check_inputs(findings: list[dict]) -> list[dict]:
    """Give finding-check the fields it requires without changing its job."""
    return [{
        **finding,
        "severity": "low",
        "claim": finding["body"],
        "settled_by": "owner",
        "recommendation": "",
    } for finding in findings]


def remove_worktree(clone: Path, worktree: Path) -> str | None:
    result = run(["git", "-C", str(clone), "worktree", "remove",
                  "--force", str(worktree)])
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip()
    return f"cannot remove the temporary worktree: {detail or 'git failed'}"


def run_finding_check(clone: Path, sha: str, findings: list[dict],
                      temp: Path) -> list[dict]:
    worktree = temp / "worktree"
    added = run(["git", "-C", str(clone), "worktree", "add", "--detach",
                 str(worktree), sha])
    if added.returncode != 0:
        detail = added.stderr.strip() or added.stdout.strip()
        raise PostError(f"cannot create a detached worktree at {sha}: "
                        f"{detail or 'git failed'}")

    failure: BaseException | None = None
    annotated: list[dict] = []
    try:
        check_input = temp / "finding-check.json"
        check_input.write_text(json.dumps(finding_check_inputs(findings)))
        result = run([
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "bin" / "finding-check"),
            str(worktree), str(check_input),
        ])
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise PostError(f"finding-check failed: {detail or 'no error output'}")
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PostError("finding-check returned invalid JSON") from exc
        if not isinstance(document, list) or len(document) != len(findings):
            raise PostError("finding-check returned an invalid findings list")
        annotated = document
    except BaseException as exc:
        failure = exc
    cleanup_error = remove_worktree(clone, worktree)
    if failure is not None:
        if cleanup_error:
            raise PostError(f"{failure}; {cleanup_error}") from failure
        raise failure
    if cleanup_error:
        raise PostError(cleanup_error)
    return annotated


def locate_findings(clone: Path, sha: str, findings: list[dict]) -> list[dict]:
    if not findings:
        return []
    with tempfile.TemporaryDirectory(prefix="review-post-") as directory:
        annotated = run_finding_check(clone, sha, findings, Path(directory))

    located: list[dict] = []
    for index, (finding, result) in enumerate(zip(findings, annotated)):
        path = finding["path"]
        line = finding["line"]
        label = f"finding[{index}] {path}:{line}"
        status = result.get("status")
        if status == "not-found":
            raise PostError(f"{label}: snippet was not found at {sha}")
        if status == "path-missing":
            raise PostError(f"{label}: path was not found at {sha}")
        if status not in ("located", "redaction-artifact"):
            reason = result.get("reason", "finding-check rejected the finding")
            raise PostError(f"{label}: {reason}")
        corrected = result.get("line")
        if isinstance(corrected, bool) or not isinstance(corrected, int):
            raise PostError(f"{label}: finding-check returned no valid line")
        located.append({**finding, "line": corrected})
    return located


def read_body(path: str | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text()
    except (OSError, UnicodeError) as exc:
        raise PostError(f"cannot read review body {path}: {exc}") from exc


def build_payload(sha: str, event: str, body: str,
                  findings: list[dict]) -> dict:
    return {
        "commit_id": sha,
        "event": event,
        "body": body,
        "comments": [{
            "path": finding["path"],
            "line": finding["line"],
            "side": "RIGHT",
            "body": finding["body"],
        } for finding in findings],
    }


def post_review(repository: str, pr: str, payload: dict) -> tuple[str, str]:
    payload_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         suffix=".json", delete=False) as handle:
            payload_path = Path(handle.name)
            json.dump(payload, handle)
        result = gh(["api", "-X", "POST",
                     f"repos/{repository}/pulls/{pr}/reviews",
                     "--input", str(payload_path)])
    finally:
        if payload_path is not None:
            payload_path.unlink(missing_ok=True)

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        response = None
    if isinstance(response, dict):
        review_id = response.get("id")
        state = response.get("state", "")
    else:
        text = result.stdout.strip()
        review_id = text if text.isdigit() else None
        state = ""
    if review_id is None:
        raise PostError("GitHub posted the review but returned no review id")
    return str(review_id), str(state)


def post(args: argparse.Namespace) -> None:
    actual_head = pr_head(args.repository, args.pr)
    if actual_head != args.sha:
        raise PostError(f"the pull request head moved: requested {args.sha}, "
                        f"but the pull request is at {actual_head}")

    findings = load_findings(Path(args.findings))
    located = locate_findings(Path(args.clone_dir), args.sha, findings)
    if located:
        patch = pull_request_patch(args.repository, args.pr)
        for index, finding in enumerate(located):
            if finding["line"] not in patch.get(finding["path"], set()):
                raise PostError(
                    f"finding[{index}] {finding['path']}:{finding['line']}: "
                    "line is not a new-side line of any GitHub diff hunk")
    for index, finding in enumerate(located):
        if REDACTION_MARKER in finding["snippet"]:
            raise PostError(f"finding[{index}] {finding['path']}:"
                            f"{finding['line']}: quote contains a redaction marker")

    body = read_body(args.body)
    if args.event == "APPROVE" and located:
        raise PostError("APPROVE cannot carry findings")
    if args.event == "REQUEST_CHANGES" and not located and not body.strip():
        raise PostError("REQUEST_CHANGES needs at least one finding or a body")

    payload = build_payload(args.sha, args.event, body, located)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return

    review_id, state = post_review(args.repository, args.pr, payload)
    print(review_id)
    if state:
        print(f"review state: {state}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        post(args)
    except PostError as exc:
        print(f"review-post: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
