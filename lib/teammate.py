#!/usr/bin/env python3
"""The teammate check: a model of another family reads the change.

`pr-ready --teammate` runs it, posts what it finds where the implementer
has to answer it, and refuses while a finding is unanswered.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import roles  # noqa: E402

TOOLS_ROOT = Path(__file__).resolve().parent.parent
COMMON_SH = TOOLS_ROOT / "lib" / "common.sh"
FINDING_CHECK = TOOLS_ROOT / "bin" / "finding-check"

# The context a reviewer judges against, the prompt and a finding's id
# come from the review loop that never shipped: a check that re-raises
# what was already answered never converges.

# The marker a teammate review carries in its body: the backend, the
# model and the head, so a review at this head is recognisable and the
# run is skipped.
REVIEW_MARKER = "<!-- teammate "

# The marker an inline comment carries: the id of the finding it holds.
FINDING_MARKER = "<!-- teammate-finding id="

# The marker as a pattern, so a review is recognised by the head it
# names.
REVIEW_MARKER_RE = re.compile(r"<!--\s*teammate\b[^>]*\bhead=([0-9a-f]+)\s*-->")


class Refusal(Exception):
    """A condition the check cannot honor.

    `detail` says what to change. The message reaches the operator
    through `pr-ready`, which refuses on it.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Teammate:
    """The teammate for one run: the backend it runs on and the model."""

    backend: str
    model: str


# ----- the operator's gh ---------------------------------------------------

def gh_json(args: list[str]) -> object:
    """One `gh` call, parsed.

    A failure is a refusal: the check cannot tell whether a finding was
    answered, and an unread answer counts as unanswered.
    """
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"gh exited {proc.returncode}"
        raise Refusal(f"gh {' '.join(args[:2])} failed: {reason}")
    if not proc.stdout.strip():
        return []
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Refusal(f"gh {' '.join(args[:2])} did not return JSON: {exc}")


def gh_as_implementer(args: list[str]) -> str:
    """Run `gh` as the implementer, and return its stdout.

    `gh_as` refuses unless the binding names the login an implementer
    for the repository. It is a shell function: a shell sources the file
    that holds it and calls it.
    """
    script = '. "$1"; shift; gh_as "$@"'
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(COMMON_SH), "implementer", *args],
        capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"gh exited {proc.returncode}"
        raise Refusal(f"the teammate review could not be posted: {reason}")
    return proc.stdout


# ----- picking the teammate ------------------------------------------------

def rotation_path() -> Path:
    """Where the round-robin state lives.

    The position is the check's own state, not the binding's: the
    binding orders the candidates, the check decides which one is next.
    """
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "agent-tools" / "teammate-rotation.json"


def read_rotation(path: Path) -> dict:
    """The last backend used, or nothing.

    A file that cannot be read is no position: the first candidate is
    taken, which is where a rotation starts anyway.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_rotation(path: Path, candidate: roles.Candidate) -> None:
    """Record the backend this run took."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "last_backend": candidate.backend,
        "last_model": candidate.model,
        "updated_at": now_iso(),
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(row, sort_keys=True) + "\n")
    tmp.replace(path)


def pick_candidate(eligible: list[roles.Candidate], policy: str,
                   path: Path, dry_run: bool) -> roles.Candidate:
    """The candidate this run takes.

    `fixed` always takes the first. `round-robin` takes the one after
    the last used, and the position is kept between runs.
    """
    if policy == "fixed":
        return eligible[0]
    last = read_rotation(path).get("last_backend")
    index = 0
    for i, candidate in enumerate(eligible):
        if candidate.backend == last:
            index = i + 1
            break
    chosen = eligible[index % len(eligible)]
    if not dry_run:
        write_rotation(path, chosen)
    return chosen


def select_teammate(binding: roles.Roles, implementer_model: str,
                     backend: str, dry_run: bool = False) -> Teammate:
    """The teammate for this run.

    A named backend is the one the operator asked for; without a name
    the binding's policy picks from its eligible candidates.

    Either way the model is of another family than the implementer's.
    """
    implementer_family = binding.family(implementer_model)
    if not binding.candidates:
        raise Refusal(
            "the role binding names no teammate candidates: add them under "
            "[teammate]")
    if backend:
        matches = [c for c in binding.candidates if c.backend == backend]
        if not matches:
            raise Refusal(
                f"the role binding names no teammate candidate with the "
                f"backend {backend!r}")
        candidate = matches[0]
        family = binding.family(candidate.model)
        if family == implementer_family:
            raise Refusal(
                f"the named backend {backend!r} runs {candidate.model!r}, "
                f"which is in the implementer's family {implementer_family!r}: "
                f"the teammate has to be another family")
        return Teammate(candidate.backend, candidate.model)

    eligible = binding.teammate_candidates(implementer_model)
    if not eligible:
        raise Refusal(
            f"no teammate candidate is eligible: every candidate is in the "
            f"implementer's family {implementer_family!r}")
    chosen = pick_candidate(eligible, binding.policy, rotation_path(),
                             dry_run=dry_run)
    return Teammate(chosen.backend, chosen.model)


# ----- running the backend -------------------------------------------------

def kill_group(proc: subprocess.Popen) -> None:
    """Take the backend's whole process group down.

    A signal to the shell alone leaves the model it started running, and
    a model still running holds whatever it holds.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def run_backend(binding: roles.Roles, teammate: Teammate, prompt: str,
                worktree: Path, out_path: Path) -> list:
    """Run the backend and read the findings it wrote.

    The command is the operator's, from the binding's table. It is run
    in the worktree, so the model reads the change it is asked about.
    """
    kind, sep, arg = teammate.backend.partition(":")
    if not sep:
        raise Refusal(
            f"the backend {teammate.backend!r} names no kind and argument")
    spec = binding.backend(kind)
    prompt_path = out_path.with_name(out_path.name + ".prompt")
    prompt_path.write_text(prompt)
    command = (spec.command
               .replace("{arg}", arg)
               .replace("{prompt}", str(prompt_path))
               .replace("{out}", str(out_path)))
    proc = subprocess.Popen(
        ["bash", "-c", command], cwd=str(worktree),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        # Its own session, so the timeout below takes the model with it
        # and the shell that started it.
        start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=spec.timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        proc.communicate()
        raise Refusal(
            f"the teammate backend {teammate.backend!r} timed out after "
            f"{spec.timeout} seconds")
    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip().splitlines()
        reason = detail[-1] if detail else "no output"
        raise Refusal(
            f"the teammate backend {teammate.backend!r} exited "
            f"{proc.returncode}: {reason}")
    try:
        raw = out_path.read_text()
    except OSError as exc:
        raise Refusal(
            f"the teammate backend {teammate.backend!r} wrote no findings "
            f"file: {exc}")
    try:
        findings = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(
            f"the teammate backend {teammate.backend!r} wrote findings that "
            f"are not JSON: {exc.msg}")
    if not isinstance(findings, list):
        raise Refusal(
            f"the teammate backend {teammate.backend!r} wrote findings that "
            f"are not a JSON array")
    return findings


# ----- locating the findings -----------------------------------------------

def check_findings(worktree: Path, findings: list, base: str) -> list:
    """Locate every finding in the tree.

    `finding-check` corrects a drifted line and says whether the location
    is inside the change. A finding it cannot locate is dropped: a claim
    about code nobody can find has to be disproved by somebody.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(findings, fh)
        path = fh.name
    try:
        proc = subprocess.run(
            [sys.executable, str(FINDING_CHECK), str(worktree), path,
             "--base", base],
            capture_output=True, text=True)
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise Refusal(f"finding-check failed on the teammate's findings: "
                      f"{detail or 'it exited non-zero'}")
    try:
        annotated = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise Refusal(f"finding-check printed nothing readable: {exc}")
    if not isinstance(annotated, list):
        raise Refusal("finding-check printed something other than a list")
    return annotated


# ----- a finding's id ------------------------------------------------------

def normalise_snippet(snippet: str) -> str:
    """The snippet in the form the id is derived from.

    `finding-check` matches a quoted line after collapsing its
    whitespace, and the id is derived from that same form, so
    re-indenting a quoted line keeps the id.
    """
    return re.sub(r"\s+", " ", snippet.strip())


def finding_id(finding: dict) -> str:
    """A stable id for a finding.

    The path, the corrected line and the snippet make a finding the same
    finding when the reviewer says it again. A hash of those three is
    short and stable across runs.
    """
    path = str(finding.get("path", ""))
    line = finding.get("line", "")
    snippet = normalise_snippet(str(finding.get("snippet", "")))
    raw = f"{path}\x1f{line}\x1f{snippet}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ----- the context the next round judges against ----------------------------

def login_of(entry: dict) -> str:
    user = entry.get("user")
    if isinstance(user, dict):
        return str(user.get("login") or "")
    return ""


def severity_of(body: str) -> str:
    """The severity a posted comment carries.

    The body leads with `**high**`. A body with no severity is low: an
    unlabelled finding is reported, not blocking.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("**") and stripped.endswith("**"):
            word = stripped.strip("*").strip().lower()
            if word in ("high", "medium", "low"):
                return word
    return "low"


def claim_of(body: str) -> str:
    """The claim a posted comment carries.

    The body is the severity, the marker, the claim, the evidence and the
    recommendation. What is left once the rest is taken off is the claim.
    """
    parts = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("**"):
            continue
        if "<!--" in stripped:
            continue
        if stripped.startswith("Settled by:"):
            continue
        if stripped.startswith("Recommendation:"):
            continue
        parts.append(stripped)
    return " ".join(parts)


def finding_id_in(body: str) -> str:
    """The id a posted comment carries, or empty."""
    match = re.search(r"<!--\s*teammate-finding\s+id=([0-9a-f]+)\s*-->", body)
    return match.group(1) if match else ""


def collect_context(reviews: list, inline: list,
                    conversation: list) -> dict:
    """What the teammate saw last time, and what came of it.

    A reviewer that sees none of it raises the findings that were
    already answered, and the loop never converges.
    """
    previous = None
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if REVIEW_MARKER_RE.search(str(review.get("body") or "")):
            previous = review
    if previous is None:
        return {"findings": [], "answers": [], "comments": []}

    findings = []
    by_id = {}
    for comment in inline:
        if not isinstance(comment, dict):
            continue
        if comment.get("pull_request_review_id") != previous.get("id"):
            continue
        body = str(comment.get("body") or "")
        finding = {
            "path": comment.get("path") or "",
            "line": comment.get("line") or comment.get("original_line") or 0,
            "severity": severity_of(body),
            "claim": claim_of(body),
            "id": finding_id_in(body),
        }
        findings.append(finding)
        by_id[comment.get("id")] = finding

    answers = []
    for comment in inline:
        if not isinstance(comment, dict):
            continue
        parent = comment.get("in_reply_to_id")
        if parent not in by_id:
            continue
        finding = by_id[parent]
        answers.append({
            "path": finding["path"],
            "line": finding["line"],
            "verdict": "answered",
            "reason": claim_of(str(comment.get("body") or "")),
        })

    since = str(previous.get("submitted_at") or "")
    comments = []
    for comment in conversation:
        if not isinstance(comment, dict):
            continue
        at = str(comment.get("created_at") or "")
        if since and at and at < since:
            continue
        body = str(comment.get("body") or "").strip()
        if not body:
            continue
        comments.append({"author": login_of(comment) or "someone",
                         "body": body, "created_at": at})
    return {"findings": findings, "answers": answers, "comments": comments}


# ----- the prompt -----------------------------------------------------------

SCHEMA_INSTRUCTION = """\
Write your findings to {path} as a JSON array. Each entry is an object
with exactly these fields:

- "severity": one of "high", "medium", "low".
- "path": the path of the file, relative to the repository root.
- "line": the line the finding is about, as a number.
- "snippet": the code the finding is about, quoted exactly as it is in
  the file. Quote the lines that show the defect, not a summary of them.
- "claim": what is wrong, in one sentence.
- "settled_by": the evidence that settles the finding -- the test, the
  run, or the reading of the source that shows it.
- "recommendation": the concrete change that fixes it.

A finding whose snippet is not in the file is dropped before anyone
reads it, so quote the file exactly. If the change is correct, write an
empty array rather than inventing a finding.

A harness that cannot write files answers with the array itself and
nothing else, in place of writing the file.

When the file is written, reply with one line: the count of findings per
severity."""


def render_findings(findings: list) -> str:
    """The previous findings, as text the reviewer can read."""
    if not findings:
        return "There is no previous review of this pull request."
    parts = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        parts.append("- {path}:{line} ({severity}): {claim}".format(
            path=finding.get("path", ""),
            line=finding.get("line", ""),
            severity=finding.get("severity", ""),
            claim=finding.get("claim", "")))
    if not parts:
        return "There is no previous review of this pull request."
    return "\n".join(parts)


def render_answers(answers: list) -> str:
    """The answers to the previous findings.

    The reason is the part that matters: a reviewer that sees only that
    a finding was answered cannot tell a fix that was argued down on the
    merits from one that was missed.
    """
    if not answers:
        return "The implementer has not answered a review yet."
    parts = []
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        parts.append("- {path}:{line} - {verdict}".format(
            path=answer.get("path", ""),
            line=answer.get("line", ""),
            verdict=answer.get("verdict", "")))
        reason = answer.get("reason")
        if reason:
            parts.append(f"  Reason: {reason}")
    if not parts:
        return "The implementer has not answered a review yet."
    return "\n".join(parts)


def render_comments(comments: list) -> str:
    """What a person has said since the last review."""
    if not comments:
        return "Nobody has commented since the last review."
    parts = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body", "")).strip()
        if body:
            parts.append("- {author}: {body}".format(
                author=comment.get("author", "someone"), body=body))
    if not parts:
        return "Nobody has commented since the last review."
    return "\n".join(parts)


def build_prompt(diff: str, findings_path: str, previous: dict, head: str,
                 base: str, worktree: str) -> str:
    """The whole prompt.

    The diff comes first: it is the evidence. Then what the reviewer has
    to judge it against -- the previous findings, the answers to them,
    and what a person has said since.
    """
    parts = [
        ("Review a proposed code change in the repository at {wt}.\n"
         "\n"
         "The commit under review is {head}. The change is {head} against "
         "{base}.\n"
         "\n"
         "The complete diff is below. Read it first, then read the "
         "surrounding source files in the repository for the context "
         "needed to judge each change.").format(
            wt=worktree or "the repository under review",
            head=head or "the commit named below",
            base=base or "its base"),
        "## The diff\n\n```diff\n" + diff.rstrip() + "\n```",
        "## The previous review\n\n" + render_findings(previous.get("findings", [])),
        "## The implementer's answers\n\n" + render_answers(previous.get("answers", [])),
        "## What a person has said since\n\n" + render_comments(previous.get("comments", [])),
    ]
    parts.append(
        "Judge the change, not the repository: a defect that predates it "
        "and that it does not make worse is out of scope. What the change "
        "leaves out is not -- a member of a set it half-covers, a consumer "
        "it never updated, and a rule its own doc comment states and its "
        "code contradicts are all defects of the change.")
    parts.append(
        "A finding you raise has to be about this commit. A finding that "
        "was already raised and is still open is not raised again; a "
        "finding the implementer answered with a reason is not raised "
        "again unless that reason is wrong, and then the reply says why.")
    parts.append(SCHEMA_INSTRUCTION.format(path=findings_path))
    return "\n\n".join(parts) + "\n"


# ----- the review that is posted -------------------------------------------

def review_marker(teammate: Teammate, head: str) -> str:
    """The marker the review's body carries."""
    return (f"{REVIEW_MARKER}backend={teammate.backend} "
            f"model={teammate.model} head={head} -->")


def review_body(teammate: Teammate, head: str, count: int) -> str:
    """The body of the review.

    A review with no findings is still posted, so the head counts as
    checked.
    """
    marker = review_marker(teammate, head)
    if count:
        text = (f"The teammate check raised {count} finding(s). Each one is "
                "attached below, and each has to be answered before this "
                "pull request is ready.")
    else:
        text = "The teammate check raised no findings."
    return f"{marker}\n\n{text}\n"


def finding_comment(finding: dict) -> str:
    """The body of one inline comment."""
    severity = finding.get("severity") or "low"
    claim = finding.get("claim") or ""
    settled = finding.get("settled_by") or ""
    recommendation = finding.get("recommendation") or ""
    parts = [f"**{severity}** {FINDING_MARKER}{finding_id(finding)} -->", ""]
    if claim:
        parts.append(claim)
        parts.append("")
    if settled:
        parts.append(f"Settled by: {settled}")
        parts.append("")
    if recommendation:
        parts.append(f"Recommendation: {recommendation}")
    return "\n".join(parts).rstrip() + "\n"


def build_comments(findings: list) -> list:
    """One comment per finding.

    A finding inside the change is a comment on its line; one outside it
    is a comment on the file, because the API takes only lines in the
    diff.
    """
    comments = []
    for finding in findings:
        body = finding_comment(finding)
        if finding.get("in_diff"):
            comments.append({
                "path": finding.get("path", ""),
                "line": finding.get("line", 0),
                "side": "RIGHT",
                "body": body,
            })
        else:
            comments.append({
                "path": finding.get("path", ""),
                "subject_type": "file",
                "body": body,
            })
    return comments


def teammate_review_at_head(reviews: list, head: str) -> dict | None:
    """The teammate review at this head, if there is one.

    The marker names the head, so a review of an earlier head is stale:
    the change moved, and the review has to be made again.
    """
    for review in reviews:
        if not isinstance(review, dict):
            continue
        match = REVIEW_MARKER_RE.search(str(review.get("body") or ""))
        if match and match.group(1) == head:
            return review
    return None


# ----- the threads nobody has answered -------------------------------------

THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    reviewThreads(first:100, after:$after){
      pageInfo{hasNextPage endCursor}
      nodes{isResolved path line originalLine
        comments(first:2){totalCount nodes{body}}}}}}}
"""


def dig(payload, *keys):
    """Walk a chain of JSON objects, answering None wherever it stops."""
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def thread_location(thread: dict) -> str:
    """Where a thread sits, as path:line."""
    path = thread.get("path") or "?"
    line = thread.get("line")
    if not isinstance(line, int):
        line = thread.get("originalLine")
    if not isinstance(line, int):
        line = "?"
    return f"{path}:{line}"


def thread_is_unanswered(thread: dict) -> bool:
    """A teammate thread nobody has answered.

    A thread is answered when it is resolved or when somebody replied to
    it: the rule `merge-check` uses, so the two tools agree about what is
    outstanding.
    """
    if thread.get("isResolved"):
        return False
    comments = thread.get("comments")
    comments = comments if isinstance(comments, dict) else {}
    total = comments.get("totalCount")
    if not isinstance(total, int):
        total = len(comments.get("nodes") or [])
    if total > 1:
        return False
    nodes = comments.get("nodes") or []
    first = nodes[0] if nodes else None
    body = str(first.get("body") or "") if isinstance(first, dict) else ""
    return FINDING_MARKER in body


def unanswered_teammate_threads(repo: str, pr: int) -> list[str]:
    """The teammate's findings nobody has answered, as path:line."""
    owner, _, name = repo.partition("/")
    threads: list[dict] = []
    cursor = None
    for _page in range(100):
        args = ["api", "graphql", "-f", f"query={THREADS_QUERY}",
                "-F", f"owner={owner}", "-F", f"name={name}",
                "-F", f"number={pr}"]
        if cursor:
            args += ["-F", f"after={cursor}"]
        payload = gh_json(args)
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list) and errors:
            message = errors[0].get("message") if isinstance(errors[0], dict) else ""
            raise Refusal(f"the review threads could not be read: "
                          f"{message or 'the query failed'}")
        page = dig(payload, "data", "repository", "pullRequest", "reviewThreads")
        if not isinstance(page, dict):
            raise Refusal("the review threads could not be read: no threads "
                          "in the response")
        nodes = page.get("nodes")
        if not isinstance(nodes, list):
            raise Refusal("the review threads could not be read: the page "
                          "carried no nodes")
        threads += [n for n in nodes if isinstance(n, dict)]
        info = page.get("pageInfo")
        if not isinstance(info, dict):
            raise Refusal("the review threads could not be read: no pageInfo")
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise Refusal("the review threads could not be read: the next "
                          "page came with no cursor")
    else:
        raise Refusal("the review threads could not be read: pagination "
                      "never ended")
    return [thread_location(t) for t in threads if thread_is_unanswered(t)]


# ----- the ledger -----------------------------------------------------------

def ledger_path() -> Path:
    """Where the findings ledger lives."""
    override = os.environ.get("FINDINGS_LEDGER")
    if override:
        return Path(override).expanduser()
    return Path.home() / "queue" / "findings" / "findings.jsonl"


def record_run(row: dict) -> None:
    """Append the run to the findings ledger.

    The file is `findings-ledger`'s, in its format: one JSON object per
    line. The row carries the stage and the model, so the stages can be
    compared against each other later.
    """
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


# ----- the check ------------------------------------------------------------

def git_diff(worktree: Path, base: str) -> str:
    """The change under review, as a diff."""
    proc = subprocess.run(
        ["git", "-C", str(worktree), "diff", f"{base}...HEAD"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise Refusal(f"the change could not be read: the base {base!r} does "
                      f"not resolve in the worktree: {detail}")
    return proc.stdout


def plural(count: int, word: str) -> str:
    """The count and the word, pluralised."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def check(args) -> int:
    """Run the teammate check on one pull request."""
    binding = roles.load()
    teammate = select_teammate(binding, args.implementer_model, args.backend,
                                dry_run=args.dry_run)
    print(f"teammate: {teammate.backend} runs {teammate.model}")

    reviews = gh_json(["api", f"repos/{args.repo}/pulls/{args.pr}/reviews"])
    outstanding = unanswered_teammate_threads(args.repo, args.pr)
    if outstanding:
        raise Refusal(
            "the teammate's findings are unanswered: "
            + ", ".join(outstanding))

    if teammate_review_at_head(reviews, args.head):
        print(f"teammate: the head {args.head[:7]} already carries a "
              f"teammate review; the backend is not run again")
        return 0

    inline = gh_json(["api", f"repos/{args.repo}/pulls/{args.pr}/comments"])
    conversation = gh_json(
        ["api", f"repos/{args.repo}/issues/{args.pr}/comments"])
    previous = collect_context(reviews, inline, conversation)

    worktree = Path(args.worktree)
    with tempfile.TemporaryDirectory(prefix="teammate-") as tmp:
        out_path = Path(tmp) / "findings.json"
        prompt = build_prompt(
            git_diff(worktree, args.base),
            str(out_path),
            previous,
            args.head,
            args.base,
            str(worktree),
        )
        findings = run_backend(binding, teammate, prompt, worktree, out_path)
        annotated = check_findings(worktree, findings, args.base)

    located = [f for f in annotated if f.get("status") == "located"]
    dropped = [f for f in annotated if f.get("status") != "located"]
    for finding in dropped:
        print(f"teammate: dropped a finding at "
              f"{finding.get('path', '?')}:{finding.get('line', '?')}: "
              f"{finding.get('status', 'unknown')} "
              f"{finding.get('reason', '')}".rstrip())

    body = review_body(teammate, args.head, len(located))
    comments = build_comments(located)

    if args.dry_run:
        print(f"teammate: {plural(len(located), 'finding')} would be "
              f"posted; nothing is posted in a dry run")
        if located:
            raise Refusal(
                f"the teammate check would raise "
                f"{plural(len(located), 'finding')} to answer: "
                + ", ".join(f"{c['path']}:{c.get('line', '?')}" for c in comments))
        return 0

    review = post_review(args.repo, args.pr, args.head, body, comments)
    record_run({
        "repo": args.repo,
        "pr": args.pr,
        "head_sha": args.head,
        "id": str(review.get("id") or f"teammate-{args.head[:12]}"),
        "kind": "teammate_run",
        "author": login_of(review) if isinstance(review, dict) else "",
        "created_at": now_iso(),
        "html_url": (review.get("html_url") or "") if isinstance(review, dict) else "",
        "body": "",
        "defect_class": "",
        "loop_stage": "teammate",
        "model": teammate.model,
        "backend": teammate.backend,
        "implementer_model": args.implementer_model,
        "reviewer_model": binding.owner_model,
        "findings": len(findings),
        "posted": len(located),
    })
    print(f"teammate: posted {plural(len(located), 'finding')} at "
          f"{args.head[:7]}")
    if located:
        raise Refusal(
            f"the teammate raised {plural(len(located), 'finding')} to answer: "
            + ", ".join(f"{c['path']}:{c.get('line', '?')}" for c in comments))
    return 0


def post_review(repo: str, pr: int, head: str, body: str,
                comments: list) -> dict:
    """Post the review, as the implementer."""
    payload = {"commit_id": head, "body": body, "event": "COMMENT",
               "comments": comments}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        out = gh_as_implementer(
            ["api", f"repos/{repo}/pulls/{pr}/reviews", "--method", "POST",
             "--input", path])
    finally:
        os.unlink(path)
    try:
        return json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return {}


def now_iso() -> str:
    """The time, as the ledger writes it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="run the teammate check on a pull request")
    c.add_argument("--repo", required=True, help="owner/name")
    c.add_argument("--pr", required=True, type=int)
    c.add_argument("--worktree", required=True, help="the checkout to read")
    c.add_argument("--head", required=True, help="the head the check reads")
    c.add_argument("--base", default="origin/main")
    c.add_argument("--implementer-model", required=True,
                   help="the model the implementer ran")
    c.add_argument("--backend", default="",
                   help="the backend to run, as kind:arg")
    c.add_argument("--dry-run", action="store_true",
                   help="run the check and post nothing")
    args = parser.parse_args(argv)
    try:
        return check(args)
    except (Refusal, roles.Refusal) as exc:
        print(exc.detail, file=sys.stderr)
        return 1
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
