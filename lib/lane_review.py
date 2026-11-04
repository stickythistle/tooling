#!/usr/bin/env python3
"""The lane-review driver's JSON contract: decisions, findings, and the ledger.

`bin/lane-review` drives the loop between an agent lane and a reviewer: it
gathers the facts (hygiene checks, a review pass, `finding-check`'s
annotations), and a person or a model decides what to do about each finding.
This module holds everything where the answer is a JSON document rather than
a file's presence: the decision file's schema and its validation, the brief a
fix round is dispatched with, the answers carried forward, and the per-round
ledger line.

Three rules the code below is built around.

**A decision names a finding.** A finding has no id of its own -- the
reviewer wrote a path, a line and a snippet -- so the driver derives one from
those three and the decision file refers to findings by that id. An id
derived from content survives a re-run of the reviewer; an ordinal does not.

**A decision is bound to the commit it was made about.** The decision file
records the reviewed commit, and a decision whose commit is not the head the
driver is about to act on is refused. A stale decision authorising a changed
head is the failure this exists to prevent: the person read one diff and the
lane is about to fix a different one. A file that names no commit at all is
refused too: the head is the one field the driver cannot infer, so a
decision file without it authorises nothing.

**One malformed entry must not abort the batch around it.** A reviewer's JSON
can carry any type in any field. A decision file is written by a person under
time pressure, so a bad entry is annotated and named, and the rest of the file
is still read.
"""
import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

# The decisions a finding can carry. `accept` sends the finding to a fix
# lane, `reject` keeps the source as it is and carries the reason forward,
# and `defer` hands the finding to another issue.
DECISIONS = ("accept", "reject", "defer")

# Fields every decision must carry, per verdict.
#
# `accept` needs the mechanism to use and the wrong one to forbid: a lane
# left to choose picks wrong about a third of the time, and the wrong
# mechanism is the one nobody thought to name.
# `reject` needs a reason, because a rejection that cannot be read back
# looks like a decision nobody made.
# `defer` needs the issue number, so the finding is not simply lost.
REQUIRED_BY_DECISION = {
    "accept": ("mechanism", "forbid"),
    "reject": ("reason",),
    "defer": ("issue",),
}


def die(message: str) -> NoReturn:
    sys.stderr.write("lane-review: " + message + "\n")
    sys.exit(2)


def _type_name(value: object) -> str:
    """Python counts `True` as an int; a boolean names no line."""
    if isinstance(value, bool):
        return "bool"
    return type(value).__name__


# ----- finding ids --------------------------------------------------------

def normalise_snippet(snippet: str) -> str:
    """The snippet as `finding-check` compares it.

    `finding-check` matches a quoted line after collapsing its
    whitespace, so two quotes that differ only in indentation are the
    same line to it. The id has to be derived from the same form: an id
    that shifted when the reviewer re-indented the line it quoted would
    name a finding the driver has never seen, and the decision file
    written against the first run's ids would be refused against the
    second run's.
    """
    return re.sub(r"\s+", " ", snippet.strip())


def finding_id(finding: dict) -> str:
    """A stable id for a finding, derived from what identifies it.

    The path, the corrected line and the snippet are what make a finding
    the same finding after the reviewer says it again. A hash of those three
    is short, stable across runs, and does not shift when another finding
    is added to the batch.

    The snippet goes in normalised, the way `finding-check` locates it:
    a reviewer that re-indents the line it quotes has raised the same
    finding, and an id that moved with the indentation would break the
    decision file written against the earlier run.

    The severity and the claim are deliberately not in it: a reviewer who
    disagrees about how bad it is, or restates it in other words, has still
    raised the same finding, and re-deciding it is the point.
    """
    path = str(finding.get("path", ""))
    line = finding.get("line", "")
    snippet = normalise_snippet(str(finding.get("snippet", "")))
    raw = f"{path}\x1f{line}\x1f{snippet}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ----- decisions ----------------------------------------------------------

def load_decisions(path: Path, expected_ids: list[str]) -> dict:
    """Read and validate a decision file against the findings it answers.

    Returns a report:
      {"decisions": {id: decision}, "missing": [...], "invalid": [...],
       "unknown": [...], "head": <sha or None>, "malformed": [...]}

    `missing` names findings with no decision; `invalid` names decisions
    that cannot be acted on; `unknown` names decisions for findings that are
    not in the batch. Every one of them is a refusal: the driver does not
    go on while any finding lacks a decision, and it does not silently
    ignore a decision it cannot use.
    """
    report = {"decisions": {}, "missing": [], "invalid": [],
              "unknown": [], "head": None, "malformed": []}
    if not path.is_file():
        report["missing"] = list(expected_ids)
        return report
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        report["malformed"].append(f"{path}: {exc}")
        report["missing"] = list(expected_ids)
        return report

    # A file that is not an object is malformed as a whole. A list is the
    # shape the findings themselves have, so it is not guessed at.
    if not isinstance(raw, dict):
        report["malformed"].append(
            f"{path}: must be a JSON object, got {_type_name(raw)}")
        report["missing"] = list(expected_ids)
        return report

    head = raw.get("head")
    if head is not None and not isinstance(head, str):
        report["malformed"].append(
            f"{path}: 'head' must be a string, got {_type_name(head)}")
        head = None
    elif head is None:
        # A decision file that names no commit authorises every commit.
        # The `head` field is the one field the driver cannot infer: it
        # is the only thing that binds a decision to the diff the
        # decider read. A file without it is not a decision about this
        # commit, so it is refused rather than taken as consent.
        report["malformed"].append(
            f"{path}: 'head' is missing; a decision file must name the "
            f"commit it was made about")
    report["head"] = head

    entries = raw.get("decisions")
    if entries is None:
        report["missing"] = list(expected_ids)
        return report
    if not isinstance(entries, list):
        report["malformed"].append(
            f"{path}: 'decisions' must be an array, got {_type_name(entries)}")
        report["missing"] = list(expected_ids)
        return report

    known = set(expected_ids)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            report["invalid"].append(
                f"decisions[{index}]: must be an object, got "
                f"{_type_name(entry)}")
            continue
        fid = entry.get("id")
        if not isinstance(fid, str) or not fid:
            report["invalid"].append(
                f"decisions[{index}]: 'id' must be a non-empty string, got "
                f"{_type_name(fid)}")
            continue
        if fid not in known:
            report["unknown"].append(
                f"decisions[{index}]: no finding has id {fid!r}")
            continue
        if fid in report["decisions"]:
            report["invalid"].append(
                f"decisions[{index}]: finding {fid!r} is decided twice")
            continue
        decision = entry.get("decision")
        if not isinstance(decision, str) or decision not in DECISIONS:
            report["invalid"].append(
                f"decisions[{index}]: 'decision' must be one of "
                f"{list(DECISIONS)}, got {decision!r}")
            continue
        required = REQUIRED_BY_DECISION[decision]
        missing_fields = [f for f in required
                          if not isinstance(entry.get(f), str)
                          or not entry[f].strip()]
        if missing_fields:
            report["invalid"].append(
                f"decisions[{index}]: a {decision!r} decision needs "
                f"{', '.join(repr(f) for f in missing_fields)}")
            continue
        if decision == "defer" and not re.fullmatch(r"[0-9]+",
                                                    entry["issue"].strip()):
            report["invalid"].append(
                f"decisions[{index}]: 'issue' must be an issue number, got "
                f"{entry['issue']!r}")
            continue
        report["decisions"][fid] = {
            "id": fid,
            "decision": decision,
            **{f: entry[f] for f in required},
        }

    report["missing"] = [i for i in expected_ids if i not in report["decisions"]]
    return report


def read_decisions(path: Path, findings: list[dict]) -> dict:
    """The validated decisions of a round, keyed by finding id.

    The decision file is the one a person writes, so it is read through the
    same validation the driver uses before acting on it. A caller that
    acted on a decision file without validating it would dispatch a fix for
    a decision that named no mechanism, or fix a finding twice.
    """
    expected = [finding_id(f) for f in findings if isinstance(f, dict)]
    return load_decisions(path, expected)["decisions"]


# ----- briefs -------------------------------------------------------------

def _finding_line(finding: dict) -> str:
    """One finding as the brief states it: where, what, and what to do."""
    path = finding.get("path", "?")
    line = finding.get("line", "?")
    snippet = (finding.get("snippet") or "").strip()
    claim = (finding.get("claim") or "").strip()
    recommendation = (finding.get("recommendation") or "").strip()
    out = [f"{path}:{line}", ""]
    if snippet:
        out.append("The offending code:")
        out.append("```")
        out.append(snippet)
        out.append("```")
        out.append("")
    if claim:
        out.append(f"The defect: {claim}")
        out.append("")
    if recommendation:
        out.append(f"The reviewer's recommendation: {recommendation}")
        out.append("")
    return "\n".join(out)


def fix_brief(findings: list[dict], decisions: dict,
               rejections: list[dict], round_no: int,
               base: str, head: str) -> str:
    """The brief a fix round is dispatched with.

    It names each accepted finding's mechanism and the wrong mechanism to
    forbid, because a lane left to choose picks wrong about a third of the
    time. It carries every rejection forward as a decision already made, so
    the same finding is not raised again in the next review.
    """
    by_id = {finding_id(f): f for f in findings}
    accepted = [d for d in decisions.values() if d["decision"] == "accept"]
    parts = [
        f"Review round {round_no} of the lane on {base}..{head}.",
        "",
        "The findings below were decided by the reviewer of record. Each one "
        "is accepted: apply the fix it names, using the mechanism it names.",
        "Do not re-open a decision, and do not widen the change beyond the "
        "accepted findings.",
        "",
    ]
    for index, decision in enumerate(accepted, 1):
        finding = by_id.get(decision["id"], {})
        parts.append(f"## Accepted finding {index} (id {decision['id']})")
        parts.append("")
        parts.append(_finding_line(finding).rstrip())
        parts.append("")
        parts.append(f"Fix it this way: {decision['mechanism']}")
        parts.append("")
        parts.append(f"Do not fix it this way: {decision['forbid']}")
        parts.append("")
    if rejections:
        parts.append("## Already decided: do not raise these again")
        parts.append("")
        parts.append("Each of these was rejected by the reviewer of record. "
                     "The code stays as it is. Do not report them again; a "
                     "finding that has been decided is not a finding.")
        parts.append("")
        for rejection in rejections:
            finding = rejection.get("finding", {})
            parts.append(
                f"- {finding.get('path', '?')}:{finding.get('line', '?')} "
                f"-- {rejection.get('reason', '')}".rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def rejection_records(findings: list[dict], decisions: dict) -> list[dict]:
    """Every rejection, with the finding it was about.

    A rejection is carried forward into the next round's answers, so the
    reviewer does not raise it again. The finding's own text travels with
    it: a reason without the finding it answers cannot stop the next
    reviewer from raising it.
    """
    by_id = {finding_id(f): f for f in findings}
    out = []
    for decision in decisions.values():
        if decision["decision"] != "reject":
            continue
        out.append({
            "id": decision["id"],
            "reason": decision["reason"],
            "finding": by_id.get(decision["id"], {}),
        })
    return out


def answers_text(rejections: list[dict]) -> str:
    """The answers carried into the next review.

    This is the `--answers` text `hermes-review-pr` folds into the prompt
    above the standing priors: a decision made once does not have to be
    rediscovered.
    """
    if not rejections:
        return ""
    lines = [
        "DECISIONS ALREADY MADE. These findings were reviewed and rejected. "
        "The code is as it is on purpose. Do not report them again.",
        "",
    ]
    for rejection in rejections:
        finding = rejection.get("finding", {})
        lines.append(
            f"- {finding.get('path', '?')}:{finding.get('line', '?')}: "
            f"{rejection.get('reason', '')}".rstrip())
    return "\n".join(lines).rstrip() + "\n"


# ----- the ledger ---------------------------------------------------------

def ledger_line(round_no: int, head: str, base: str, model: str,
                brief: str, findings: list[dict], decisions: dict,
                survivors: list[dict]) -> dict:
    """One round's ledger line.

    The counts are what the ledger is for: `findings` is what the reviewer
    raised, `surviving` is what `finding-check` confirmed against the
    source, and `false_assertions` is the difference -- a reviewer whose
    claims do not survive is a reviewer worth knowing about. `model` and
    `brief` are on the line so which model reviewed what can be decided from
    measurement rather than from memory.
    """
    accepted = sum(1 for d in decisions.values() if d["decision"] == "accept")
    rejected = sum(1 for d in decisions.values() if d["decision"] == "reject")
    deferred = sum(1 for d in decisions.values() if d["decision"] == "defer")
    return {
        "round": round_no,
        "at": _now(),
        "head": head,
        "base": base,
        "model": model,
        "brief": brief,
        "findings": len(findings),
        "surviving": len(survivors),
        "false_assertions": len(findings) - len(survivors),
        "accepted": accepted,
        "rejected": rejected,
        "deferred": deferred,
    }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def append_ledger(path: Path, entry: dict) -> None:
    """Write one round's line to the ledger.

    One line per round, not one per event: a round is reviewed, then
    decided, and the second step adds to the first rather than starting
    a second record of the same round. The line is therefore replaced
    when the round's decisions land, keyed by its round number.

    Newline-delimited JSON: a process killed mid-write leaves either a
    complete last line or a truncated one, never a half-record pretending
    to be valid.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = path.read_text().splitlines()
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            existing = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(existing, dict):
            continue
        if existing.get("round") == entry.get("round"):
            lines[index] = json.dumps(entry, sort_keys=True)
            replaced = True
            break
    if not replaced:
        lines.append(json.dumps(entry, sort_keys=True))
    path.write_text("\n".join(l for l in lines if l.strip()) + "\n")


def read_ledger(path: Path) -> list[dict]:
    """Every round the ledger holds, skipping a malformed line.

    A person editing the file by hand can corrupt one row without losing
    the rest, and a partial read is better than refusing the whole file.
    """
    if not path.exists():
        return []
    rounds = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            rounds.append(entry)
    return rounds


# ----- the state file -----------------------------------------------------

def load_state(path: Path) -> dict:
    """The driver's state, or an empty one."""
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ----- the review prompt --------------------------------------------------

# The findings schema `finding-check` reads. The review is asked for this
# and nothing else, so the answer can be checked against the source rather
# than taken on trust -- a reviewer that writes prose cannot be located.
FINDING_SCHEMA = """[
  {
    "severity": "high | medium | low",
    "path": "path/to/file.ext, relative to the repository root",
    "line": 42,
    "snippet": "the exact lines of the offending code, copied verbatim",
    "claim": "what is wrong, in one sentence",
    "settled_by": "what settles this question: a test, a grep, a reading",
    "recommendation": "the concrete change to make"
  }
]"""


def build_prompt(diff_path: Path, root: Path, answers: str,
                 priors: str = "") -> str:
    """The prompt the review is asked with.

    It is hermes-review-pr's prompt with one change: the answer is the JSON
    schema `finding-check` reads rather than prose. Everything else is kept
    -- the scope, the hard constraints, the instruction to read the
    surrounding source -- because those are what made the review worth a
    model.

    The priors are the project's standing domain facts, the same ones
    hermes-review-pr folds in, and the answers are the decisions already
    made. Both narrow what the reviewer reports, and a decision made once
    does not have to be rediscovered.
    """
    extra = ""
    if priors.strip():
        extra += (
            "\n\nTHE PROJECT'S STANDING FACTS. These are true of this "
            "repository.\nTreat them as settled and do not report them.\n\n"
            + priors.strip() + "\n")
    if answers.strip():
        extra += (
            "\n\nDECISIONS ALREADY MADE. These findings were reviewed and "
            "rejected.\nThe code is as it is on purpose. Do not report them "
            "again.\n\n" + answers.strip() + "\n")
    return f"""Review a proposed code change in the repository at {root}.

The complete diff is at {diff_path}. Read it first, then read the surrounding
source files in the repository for the context needed to judge each change.

Apply ONLY Phases 2, 3 and 4 of the swe-reviewer skill: static analysis, the
severity taxonomy, and the findings write-up. Skip Phases 1 and 5 entirely.

Hard constraints:
- Do not create, checkout, or delete any git branch.
- Do not stage, commit, push, or run any git command that writes.
- Do not modify, create, or delete any file inside {root}.
- Judge the change, not the repository: a defect that predates it and that it
  does not make worse is out of scope. What the change leaves out is not --
  a member of a set it half-covers, a consumer it never updated, and a rule its
  own doc comment states and its code contradicts are all defects of the change.

Answer with a JSON array and nothing else. Each element is one finding, with
exactly these fields:

{FINDING_SCHEMA}

The array may be empty: a correct change has no findings, and saying so is a
result. Do not wrap the array in prose, do not add a "CORRECT" section, and do
not write any file. The answer is read by a tool, not by a person.
{extra}"""


def extract_findings(text: str) -> list:
    """The findings array in a reviewer's answer.

    A reviewer writes prose around its JSON, so the array is found rather
    than assumed: the first `[` that opens a JSON array, through its
    matching `]`. A model that answered with anything else produced no
    findings, and that is reported as a failure of the review rather than
    as a clean review.
    """
    start = text.find("[")
    if start == -1:
        raise ValueError("no JSON array in the answer")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:index + 1])
    raise ValueError("the JSON array in the answer is not closed")


# ----- survivors ----------------------------------------------------------

def survivors(findings: list[dict]) -> list[dict]:
    """The findings that survived `finding-check`.

    A finding that located in the source and sits inside the change is a
    claim about the change. One that did not locate, or that lies outside
    the diff, is not: it is the reviewer's false assertion, and it is
    counted as such rather than carried into a decision.
    """
    out = []
    for finding in findings:
        if finding.get("status") == "located" and finding.get("in_diff"):
            out.append(finding)
    return out


# ----- command line -------------------------------------------------------

def cmd_decisions(args) -> int:
    """Validate a decision file and report what it says.

    Exit 0 when every finding has a usable decision, 1 when one does not.
    The report goes to stdout as JSON so the driver reads it without
    parsing prose.
    """
    findings = json.loads(Path(args.findings).read_text())
    expected = [finding_id(f) for f in findings
                if isinstance(f, dict)]
    report = load_decisions(Path(args.decisions), expected)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.head is not None and report["head"] is not None \
            and report["head"] != args.head:
        sys.stderr.write(
            f"lane-review: the decision file is about commit "
            f"{report['head']}, not {args.head}\n")
        return 1
    # A file that names no commit is refused, and the malformed entry
    # says so. It is not read as "about this one": the head is the one
    # field the driver cannot infer, and a decision file without it
    # authorises nothing.
    ok = not (report["missing"] or report["invalid"] or report["unknown"]
              or report["malformed"])
    return 0 if ok else 1


def cmd_brief(args) -> int:
    """Print the brief a fix round is dispatched with."""
    findings = json.loads(Path(args.findings).read_text())
    decisions = read_decisions(Path(args.decisions), findings)
    rejections = json.loads(Path(args.rejections).read_text()) \
        if Path(args.rejections).is_file() else []
    sys.stdout.write(fix_brief(findings, decisions, rejections,
                               args.round, args.base, args.head))
    return 0


def cmd_answers(args) -> int:
    """Print the answers carried into the next review."""
    rejections = json.loads(Path(args.rejections).read_text()) \
        if Path(args.rejections).is_file() else []
    sys.stdout.write(answers_text(rejections))
    return 0


def cmd_ledger(args) -> int:
    """Write one round's ledger line.

    The review writes it first, with the counts the review produced; the
    decisions land later and add to the same line. One line per round is
    what makes "which model reviewed what" answerable by reading the
    ledger.
    """
    findings = json.loads(Path(args.findings).read_text())
    decisions = read_decisions(Path(args.decisions), findings) \
        if args.decisions and Path(args.decisions).is_file() else {}
    survivors_list = survivors(findings)
    entry = ledger_line(args.round, args.head, args.base, args.model,
                        args.brief, findings, decisions, survivors_list)
    append_ledger(Path(args.ledger), entry)
    print(json.dumps(entry, sort_keys=True))
    return 0


def cmd_ids(args) -> int:
    """Print each finding's id, one per line: `<id> <path>:<line>`."""
    findings = json.loads(Path(args.findings).read_text())
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        print(f"{finding_id(finding)} {finding.get('path', '?')}"
              f":{finding.get('line', '?')}")
    return 0


def cmd_round_head(args) -> int:
    """Print the commit a round was reviewed at, from the ledger.

    The ledger is the durable record of what each round was about, so it
    is what a later step reads to learn the commit a round's decisions
    were made about. The state file's `head` cannot answer it: the state
    moves on with the rounds, and the `--done` step needs the commit
    *this* round was reviewed at, not the current tip.

    Exit 0 when the round has a head, 1 when it does not -- a ledger
    with no line for the round is a refusal, not an empty answer.
    """
    for entry in read_ledger(Path(args.ledger)):
        if entry.get("round") != args.round:
            continue
        head = entry.get("head")
        if isinstance(head, str) and head:
            print(head)
            return 0
        sys.stderr.write(
            f"lane-review: the ledger's line for round {args.round} "
            f"names no commit\n")
        return 1
    sys.stderr.write(
        f"lane-review: the ledger holds no line for round {args.round}\n")
    return 1


def cmd_count(args) -> int:
    """Print how many findings carry the asked-for status.

    `--in-diff` narrows it further to the findings that are inside the
    change: a finding that located but sits outside the diff is about the
    repository, not about this change.
    """
    findings = json.loads(Path(args.findings).read_text())
    total = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if args.status and finding.get("status") != args.status:
            continue
        if args.in_diff and finding.get("in_diff") is not True:
            continue
        total += 1
    print(total)
    return 0


def cmd_rejections(args) -> int:
    """Write the rejections of a round, each with the finding it was about."""
    findings = json.loads(Path(args.findings).read_text())
    decisions = read_decisions(Path(args.decisions), findings)
    records = rejection_records(findings, decisions)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(records, indent=2, sort_keys=True)
                              + "\n")
    return 0


def cmd_accepted_count(args) -> int:
    """Print how many findings were accepted."""
    decisions = json.loads(Path(args.decisions).read_text())
    entries = decisions.get("decisions", []) \
        if isinstance(decisions, dict) else []
    print(sum(1 for d in entries
              if isinstance(d, dict) and d.get("decision") == "accept"))
    return 0


def cmd_state_get(args) -> int:
    """Print one field of the driver's state."""
    state = load_state(Path(args.state))
    value = state.get(args.field, "")
    print("" if value is None else value)
    return 0


def cmd_state_set(args) -> int:
    """Set one field of the driver's state, keeping the rest.

    A value that looks like a whole number is stored as one: the round
    is counted, and a state file holding `"round": "2"` makes every
    reader that compares it against a number guess.
    """
    state = load_state(Path(args.state))
    value: object = args.value
    if re.fullmatch(r"-?[0-9]+", args.value):
        value = int(args.value)
    state[args.field] = value
    save_state(Path(args.state), state)
    return 0


def cmd_prompt(args) -> int:
    """Write the review's prompt."""
    answers = ""
    if Path(args.answers).is_file():
        answers = Path(args.answers).read_text()
    priors = ""
    if args.priors and Path(args.priors).is_file():
        priors = Path(args.priors).read_text()
    prompt = build_prompt(Path(args.diff), Path(args.root), answers, priors)
    Path(args.out).write_text(prompt)
    return 0


def cmd_extract(args) -> int:
    """Write the findings array out of a reviewer's answer.

    A reviewer answers with prose around its JSON, and an answer with no
    array in it is a failed review, not a clean one. It is reported as such
    so a caller never reads "no findings" as "no defects".
    """
    text = Path(args.stdout).read_text()
    try:
        findings = extract_findings(text)
    except (ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"lane-review: {exc}\n")
        return 1
    if not isinstance(findings, list):
        sys.stderr.write("lane-review: the answer's JSON is not an array\n")
        return 1
    Path(args.out).write_text(json.dumps(findings, indent=2) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decisions",
                       help="validate a decision file against its findings")
    d.add_argument("--findings", required=True)
    d.add_argument("--decisions", required=True)
    d.add_argument("--head", help="the commit the decisions must be about")
    d.set_defaults(func=cmd_decisions)

    b = sub.add_parser("brief", help="print the fix round's brief")
    b.add_argument("--findings", required=True)
    b.add_argument("--decisions", required=True)
    b.add_argument("--rejections", required=True)
    b.add_argument("--round", type=int, required=True)
    b.add_argument("--base", required=True)
    b.add_argument("--head", required=True)
    b.set_defaults(func=cmd_brief)

    a = sub.add_parser("answers", help="print the answers for the next review")
    a.add_argument("--rejections", required=True)
    a.set_defaults(func=cmd_answers)

    lg = sub.add_parser("ledger", help="write one round's ledger line")
    lg.add_argument("--findings", required=True)
    lg.add_argument("--decisions",
                    help="the round's decisions, when they have landed")
    lg.add_argument("--ledger", required=True)
    lg.add_argument("--round", type=int, required=True)
    lg.add_argument("--head", required=True)
    lg.add_argument("--base", required=True)
    lg.add_argument("--model", default="")
    lg.add_argument("--brief", default="")
    lg.set_defaults(func=cmd_ledger)

    i = sub.add_parser("ids", help="print each finding's id")
    i.add_argument("--findings", required=True)
    i.set_defaults(func=cmd_ids)

    rh = sub.add_parser("round-head",
                        help="print the commit a round was reviewed at")
    rh.add_argument("--ledger", required=True)
    rh.add_argument("--round", type=int, required=True)
    rh.set_defaults(func=cmd_round_head)

    c = sub.add_parser("count", help="count findings by status")
    c.add_argument("--findings", required=True)
    c.add_argument("--status")
    c.add_argument("--in-diff", action="store_true")
    c.set_defaults(func=cmd_count)

    r = sub.add_parser("rejections", help="write a round's rejections")
    r.add_argument("--findings", required=True)
    r.add_argument("--decisions", required=True)
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_rejections)

    ac = sub.add_parser("accepted-count",
                        help="print how many findings were accepted")
    ac.add_argument("--decisions", required=True)
    ac.set_defaults(func=cmd_accepted_count)

    sg = sub.add_parser("state-get", help="print one field of the state")
    sg.add_argument("state")
    sg.add_argument("field")
    sg.set_defaults(func=cmd_state_get)

    ss = sub.add_parser("state-set", help="set one field of the state")
    ss.add_argument("state")
    ss.add_argument("field")
    ss.add_argument("value")
    ss.set_defaults(func=cmd_state_set)

    pr = sub.add_parser("prompt", help="write the review's prompt")
    pr.add_argument("--diff", required=True)
    pr.add_argument("--root", required=True)
    pr.add_argument("--answers", required=True)
    pr.add_argument("--priors",
                    help="the project's standing facts, folded into the prompt")
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=cmd_prompt)

    ex = sub.add_parser("extract", help="write the findings out of an answer")
    ex.add_argument("--stdout", required=True)
    ex.add_argument("--out", required=True)
    ex.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
