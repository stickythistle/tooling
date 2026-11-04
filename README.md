# agent-tools

A verification harness for handing coding work to an LLM agent and finding out
whether what came back is real.

An agent's own report of success is worth very little: across roughly twenty
dispatched issues its diagnosis was usually right and its choice of mechanism
was wrong about a third of the time, and neither a careful read nor a green
compiler distinguished the two.

## The idea

**A gate the agent can run itself is worth more than a review it cannot.** Given
no verification command it commits code that has never compiled; given one it
reads the failure and fixes it unprompted.

**A gate must exercise the thing that changed.** `gate-verify` picks its gates
from the diff, and each gate runs the project's own command rather than a copy
that drifts.

**The project describes itself; the tools describe the procedure.** Anything
naming a crate, a recipe, a path or a database lives in a `gates.toml` committed
to the project being gated. Anything naming a machine lives in the operator's
environment file. What is left is the same everywhere.

**Draft is the interlock between agent work and human review.** A pull request
stays draft while an agent is still working it. `pr-ready` owns that transition
and refuses it while the tree is dirty, the head is unpushed, CI is red or
unfinished, or `gate-verify` fails. A check that never started (no steps, no log) is
promoted with an annotation naming which local gates ran, so the reviewer is
told what they are getting rather than left to infer it.

**Review, triage and implement are one pipeline, not three jobs.** Each phase
hands the next a file rather than a conclusion. Findings that survive triage
become issues, and the branch fixing one opens a draft PR saying `Closes #N`.

**A phase does the deciding; the model does the judging.** Left to decide for
itself whether anything had changed, the agent produced five duplicate reviews
out of fourteen and three stray clones of the repository inside its own working
directory.

**A branch's state is a comparison, not a reading.** Whether a merge broke
`main` is no property of the merge alone: it is red where the commit before
it was green. A merge onto an already-red branch broke nothing.

`main-watch` reads both commits' checks and says which case it is, so a red
that was already there is reported, not blamed on the merge that
landed on top of it.

A check that has not finished is neither green nor red, and a branch whose
checks could not be read has an unknown state -- never a green one.

## Layout

    bin/       the gates, and the drivers that dispatch an issue to an agent
    lib/       the manifest reader and shared host resolution
    pipeline/  the phases: review -> triage -> implement overnight, cleanup at midday
    skills/    the procedures the drivers invoke, and the ones a Claude session loads

`~/.hermes/scripts` symlinks to `pipeline/`, because `hermes cron` rejects a
script resolving outside that directory. Hence `cd -P` throughout: a logical `cd`
walks `../bin` relative to the link and lands outside the checkout.

## The gates

`gate-verify <worktree>` diffs the branch against its base, runs every gate whose
`when` matches a changed path, and reports all of them rather than stopping at
the first failure.

| | |
|---|---|
| `remote-task`     | sync the tree to a host, run a project command under a lock, fetch artefacts back |
| `remote-run`      | send part of the tree to a host that has a tool this one lacks, run the project's recipe there |
| `lane-watch`      | follow a running hermes lane's conversation as it happens |
| `gate-prove`      | revert the source, keep the tests, require a failure — the tests depend on the change, not that the change is right |
| `gate-untested`   | fail a branch that adds surface with no test anywhere |
| `redaction-check` | fail a branch carrying an agent's `***` rewrite into the source |
| `pg-explain`      | show whether an index is usable by a query, and whether the planner picks it |
| `pr-ready`        | take a PR out of draft, refusing while the work behind it is unfinished |
| `issue-grade`     | grade open issues by size and context, and route each to a lane |

## Review findings

Two tools sit between a reviewer model and the person deciding.

`finding-check <worktree> <findings.json>` locates each finding in the
tree: it corrects a drifted line number, flags a `***` the reviewer wrote
into its own quote, and says whether the location is inside the change. It
never drops a finding — a malformed entry comes back annotated in its place.

`finding-verify <worktree> <findings.json>` then verifies the located ones.
It gathers the evidence first — the enclosing block, the diff hunk, and the
output of `settled_by` when that is one of four read-only forms (`file:`,
`grep:`, `git-log:`, `git-show:`) — and asks the model for one verdict per
finding: `holds`, `does-not-hold` or `cannot-settle`. An answer whose quote
is not in the evidence is recorded as `cannot-settle`. Every finding comes
back, with `verdict` and `evidence` added; the decider still decides.

The diff hunk is the branch's diff against its base, so `--base REF` names
the base and defaults to `origin/main`; the default is resolved to the fork
point, the same way `finding-check` resolves it. A `git-log:` or `git-show:`
revision is resolved to a commit before it runs, so a git option cannot
reach the argv of a form documented as read-only.

The profile is `VERIFY_PROFILE`, and the model it names is only used after
it clears the known-answer controls in `VERIFY_CONTROLS`: archived findings
with known verdicts, one of them false. A profile that fails the false one
is not used.

    VERIFY_PROFILE=small \
    VERIFY_CONTROLS=/path/to/controls.json \
    VERIFY_MODEL='<tool-free model command>' \
    finding-verify /path/to/worktree findings.json

`VERIFY_MODEL` is the model command; it is split into argv and the prompt
arrives on its stdin, so no command text is evaluated. It must be a call
that gives the model no tools, and it is run as given, so the tool cannot
enforce that; which command it is depends on the operator's agent.

Exit 3 means *unprovable*: the gate gathered no evidence either way.
`gate-verify` reports it on its own line, because a gate that measured nothing
must not appear in a list headed "passed".

## Setup

    cp hosts.env.example ~/.config/agent-tools/env    # then edit
    cp gates.toml.example /path/to/project/gates.toml # then edit, and commit it
    export PATH="$PWD/bin:$PATH"

Both example files document every field.

## What you have to wire in

`pipeline/pipeline.env.example` is the full list. Four are yours to supply:

- **a gate**: one command taking a worktree, non-zero when the tree is not fit to
  push. Everything the pipeline claims about correctness comes from it, so make
  it the command your CI runs. `gate-verify` is one.
- **an agent** that takes a prompt and may edit a working tree.
- **a triager** and an **implementer** driving that agent for one finding and one
  issue respectively.
- **a small, fast model as the dedup judge**, asked only where two findings share
  a quarter of their words and may or may not mean the same thing. It never sees
  code, so use the smallest model you have, and not the one doing the reviewing.

Leaving the judge unset files a duplicate when wording drifts far enough. Every
other omission fails loudly at startup.

## Limits

`gate-prove` answers whether the tests depend on the diff, never whether the
diff is correct: a wrong fix and its test fail together when reverted and land
as a pass. It also cannot tell a suite that fails to compile from one whose
assertion fired — both exit 1. Its verdict rests on that command having passed on the
unmodified tree first. A command that could not start is reported as unprovable.

Each gate inherits the blind spots of the command it runs. `gate-verify` refuses
a branch no gate matches rather than reporting a pass it did not earn.

## License

MIT. See [LICENSE](LICENSE).
