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

**Tools establish facts, models answer one closed question over facts the tool
gathered, and decisions stay with whoever can be held to them.** The layering
is what makes a loop that nobody watches still trustworthy. `lane-review` asks
the tool for the facts: whether the lane has settled, whether the commit
carries an agent's redaction rewrite, which files the issue never
mentioned,
where the reviewer's quoted code actually is. It then asks a model exactly one
question -- what is wrong with this diff -- and hands the answer back to
`finding-check`, which checks it against the source rather than taking it on
trust. What to do about each finding is not the model's to decide and not the
tool's either: a person writes `round-N/decisions.json`, and the driver
refuses to go on while any finding lacks one. The decision file names the
commit it was made about, so a decision about one diff cannot authorise a
later one. Every step that needed no judgement runs; every step that did
stops, with the reason named.

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
| `lane-review`     | review a lane's branch and stop at every step that needs a judgement |

Exit 3 means *unprovable*: the gate gathered no evidence either way.
`gate-verify` reports it on its own line, because a gate that measured nothing
must not appear in a list headed "passed".

## The lane review loop

`lane-review <worktree>` takes a branch an agent lane wrote to a draft pull
request, running the steps that need no judgement and stopping at the ones
that do.

    lane-review <wt>              # check the commit, review it, write round-N/findings.json
    lane-review ids <wt>          # the finding ids a decision file refers to
    lane-review <wt> --apply      # dispatch the accepted fixes to a lane
    lane-review <wt> --done       # squash, push, and open the pull request as a Draft

The first run refuses a commit that carries an agent's redaction rewrite, or
that
changes a file the issue never mentioned; it waits for the lane to settle
first, because a review of a moving tree reviews nothing. It then asks a
reviewer for findings in the JSON schema `finding-check` reads, checks each
one against the source, and writes `round-N/findings.json`. It exits 1: the
findings need decisions.

A person writes `round-N/decisions.json`. Each finding is `accept`ed with the
mechanism to use and the wrong mechanism to forbid, `reject`ed with a reason,
or `defer`red to an issue number. The driver refuses to go on while any
finding lacks a decision, and refuses a decision file that names another
commit.

`--apply` hands the accepted findings to a lane and carries every rejection
into the next review, so a finding that was decided is not raised again.
`--done` refuses while the last round accepted anything; then it squashes at
the fork point, pushes, and opens the pull request as a Draft. Ready stays a
separate act (`pr-ready`): Ready means no further change is expected.

Every round appends to the ledger under `$AGENT_STATE_DIR/lane-review/`, with
the findings, how many survived, how many were the reviewer's false
assertions, and which model reviewed what — so which reviewer to trust can be
decided from measurement. The state lives outside the worktree: a review's
findings are not part of the change under review.

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
