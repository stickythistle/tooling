#!/usr/bin/env python3
"""Record what the loop would have merged, and what a person did instead.

Shadow mode is the point: the loop decides, and a person merges. The
decision is only worth having if the person's answer is recorded
against it, so this ledger holds both sides of the exchange and the
rate at which they disagree.

One file, newline-delimited JSON, one line per decision. A later line
reconciles an earlier one: the decision line carries the loop's
would-merge verdict, and the outcome line carries what a person did
(merged, asked for changes, closed) with the sha they acted on.

Entries are keyed by (repo, pr, head), so a rerun of the same decision
does not append a second line, and a decision about a new head is a new
line rather than an edit of the old one. A push after the decision
changes the head, and the decision about the old head is still worth
keeping: it says what the loop was about to do when the push arrived.

The file is read and written by whoever holds it, so a malformed line
is skipped rather than fatal: a person editing this by hand can
corrupt one row without losing the rest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

LEDGER = Path(os.environ.get(
    "MERGE_LEDGER", Path.home() / "queue" / "merges" / "merges.jsonl"))

# The outcomes a person can give a pull request the loop was about to
# merge. `merged` is agreement. The other two are disagreement, and
# they are told apart because they say different things: a change
# request says the work was wrong, a close says it was not wanted.
OUTCOMES = ("merged", "changes_requested", "closed")


def entry_key(entry: dict) -> tuple:
    """The identity of a line in the ledger.

    The decision kind is part of it: a decision and the outcome that
    answers it name the same repository, pull request and head, and
    keying on those alone would treat the outcome as a second copy of
    the decision and drop it.
    """
    return (entry["repo"], str(entry["pr"]), entry["head"],
            entry.get("decision", ""))


def load(path: Path) -> list:
    """Read every decision in the ledger.

    A line that is not JSON, or that lacks the fields a decision needs,
    is skipped: a partial read is better than refusing the whole file.
    """
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if not all(k in entry for k in ("repo", "pr", "head")):
            continue
        out.append(entry)
    return out


def append(path: Path, entry: dict) -> bool:
    """Append one decision. Returns True when a line was written.

    A decision already in the ledger is not written again, so a rerun
    of the same decision is a no-op rather than a second line saying
    the same thing.

    The line is written in one call, so a process killed mid-write
    leaves either a complete line or a truncated one, never a
    half-record pretending to be valid.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for existing in load(path):
        if entry_key(existing) == entry_key(entry):
            return False
    with path.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return True


def record_decision(path: Path, repo: str, pr, head: str, mode: str,
                    method: str | None, reason: str = "") -> bool:
    """Record the loop's would-merge decision for one pull request.

    `mode` is the repository's mode: `shadow` for a decision that was
    never acted on, `on` for one that was. The reason is what the
    decision was based on, so a disagreement later can be read against
    the grounds the loop gave.
    """
    entry = {
        "repo": repo,
        "pr": str(pr),
        "head": head,
        "decision": "would_merge",
        "mode": mode,
        "merge_method": method or "",
        "reason": reason,
    }
    return append(path, entry)


def record_outcome(path: Path, repo: str, pr, head: str, outcome: str,
                    by: str = "") -> bool:
    """Record what a person did with a pull request the loop had decided
    about.

    `outcome` is one of `merged`, `changes_requested` or `closed`. The
    head is the head the outcome happened on: a person who merges
    something other than the agreed head has disagreed with the
    decision, and the ledger has to be able to say so.

    The outcome is recorded against the decision for the same head. When
    the outcome names a different head, it is a disagreement: the
    decision was about one commit and the person did something else.
    """
    if outcome not in OUTCOMES:
        raise ValueError(
            f"outcome is {outcome!r}; it must be one of "
            f"{', '.join(repr(o) for o in OUTCOMES)}")
    decisions = [e for e in load(path)
                 if e["repo"] == repo and str(e["pr"]) == str(pr)
                 and e.get("decision") == "would_merge"]
    if not decisions:
        # An outcome with no decision to reconcile against is a person
        # acting on a pull request the loop never spoke about. It is
        # still recorded, so the ledger holds the whole story, but it
        # is marked as having nothing to reconcile.
        entry = {
            "repo": repo,
            "pr": str(pr),
            "head": head,
            "decision": "outcome",
            "outcome": outcome,
            "by": by,
            "reconciled": False,
        }
        return append(path, entry)

    # The decision this outcome answers is the one for the same head.
    # The most recent such decision wins: a head can be decided twice
    # (the loop decided, then the head moved and it decided again), and
    # the later decision is the one a person is answering.
    same_head = [e for e in decisions if e["head"] == head]
    chosen = same_head[-1] if same_head else decisions[-1]
    entry = {
        "repo": repo,
        "pr": str(pr),
        "head": head,
        "decision": "outcome",
        "outcome": outcome,
        "by": by,
        "reconciled": True,
        "against_head": chosen["head"],
        "agrees": chosen["head"] == head and outcome == "merged",
    }
    return append(path, entry)


def reconcile(path: Path) -> list:
    """Pair every decision with the outcome that answered it.

    A decision is answered by the first outcome for the same
    (repo, pr) whose head is the decision's head. An outcome on a
    different head answers no decision: it is a person who merged
    something the loop had not agreed to.
    """
    decisions = [e for e in load(path) if e.get("decision") == "would_merge"]
    outcomes = [e for e in load(path) if e.get("decision") == "outcome"]
    pairs = []
    for decision in decisions:
        answer = None
        for outcome in outcomes:
            if (outcome["repo"] == decision["repo"]
                    and str(outcome["pr"]) == str(decision["pr"])
                    and outcome.get("against_head") == decision["head"]):
                answer = outcome
                break
        pairs.append({
            "repo": decision["repo"],
            "pr": decision["pr"],
            "head": decision["head"],
            "mode": decision.get("mode", ""),
            "merge_method": decision.get("merge_method", ""),
            "outcome": answer.get("outcome") if answer else None,
            "agrees": bool(answer.get("agrees")) if answer else None,
            "answered": answer is not None,
        })
    return pairs


def disagreement_rate(path: Path) -> dict:
    """How often the loop's decision and the person's answer differed.

    Only decisions that a person has answered count. An unanswered
    decision is not agreement: nothing has been said against it, and
    nothing has been said for it either. Counting it as agreement would
    make the rate fall as the loop got slower, which is the wrong
    direction for the number that decides a rollout.

    The rate is reported as a fraction and as counts, and the counts are
    the ones to read: a rate over three decisions is not evidence.
    """
    pairs = reconcile(path)
    answered = [p for p in pairs if p["answered"]]
    agree = sum(1 for p in answered if p["agrees"])
    disagree = len(answered) - agree
    unanswered = len(pairs) - len(answered)
    rate = (disagree / len(answered)) if answered else None
    return {
        "decisions": len(pairs),
        "answered": len(answered),
        "agreed": agree,
        "disagreed": disagree,
        "unanswered": unanswered,
        "rate": rate,
    }


def summary(path: Path) -> str:
    """One line naming what the ledger holds."""
    stats = disagreement_rate(path)
    rate = "unknown" if stats["rate"] is None else f"{stats['rate']:.3f}"
    return (f"{stats['decisions']} decision(s), {stats['answered']} answered, "
            f"{stats['agreed']} agreed, {stats['disagreed']} disagreed, "
            f"{stats['unanswered']} unanswered, disagreement rate {rate}")
