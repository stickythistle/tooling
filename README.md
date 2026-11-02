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

**GitHub holds the merge rule, not a tool.** A repository's ruleset requires one
approving review of the most recent push, dismisses stale approvals on push,
requires the status checks and linear history. `pr-auto-merge` asks GitHub to
*enable* auto-merge for one exact head; GitHub merges later, under the ruleset.
Nothing in this repository merges a pull request. A repository whose protections
are unknown is never enabled, because an unknown prerequisite is not a satisfied
one.

**Review, triage and implement are one pipeline, not three jobs.** Each phase
hands the next a file rather than a conclusion. Findings that survive triage
become issues, and the branch fixing one opens a draft PR saying `Closes #N`.

**A phase does the deciding; the model does the judging.** Left to decide for
itself whether anything had changed, the agent produced five duplicate reviews
out of fourteen and three stray clones of the repository inside its own working
directory.

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

Exit 3 means *unprovable*: the gate gathered no evidence either way.
`gate-verify` reports it on its own line, because a gate that measured nothing
must not appear in a list headed "passed".

The drivers are not gates, but they belong to the same set:

| | |
|---|---|
| `pr-loop-gate`   | print the one line that says what a pull request needs next |
| `pr-auto-merge`  | ask GitHub to auto-merge an agreed head, or record that it would have |
| `pr-loop-config` | read and validate the per-repository `pr-loop.toml` |
| `pr-merge-ledger`| record a person's outcome against the loop's decision, and report the rate |

## Governed auto-merge

An agreed pull request waits on a person to merge it, even when nothing is
left to decide. The rule that decides is a repository's *ruleset*, not a
script:

    one approving review, of the most recent push
    stale approvals dismissed on push
    the required status checks (Gates)
    linear history

`pr-auto-merge` asks GitHub to enable auto-merge for one exact head. The
request names the head it means, and GitHub refuses it when the pull
request's head has moved on. GitHub merges later, when the ruleset is
satisfied, and a push after the approval dismisses it and stops the
merge. No script here merges anything.

    pr-auto-merge <owner/repo> <pr>          # decide, and act
    pr-auto-merge <owner/repo> <pr> --check  # decide, and change nothing
    pr-loop-config <owner/repo>              # what the configuration says
    pr-merge-ledger rate                     # the disagreement rate

### The configuration

`pr-loop.toml` holds one table per repository, in
`~/.config/hermes-tools/pr-loop.toml`. Copy `pr-loop.toml.example` and edit
it. The merge method, the ruleset that holds the authority, the required
checks and the mode all live there.

| | |
|---|---|
| `auto_merge` | `"shadow"` (the default) or `"on"` |
| `merge_method` | `"squash"` or `"rebase"` |
| `ruleset` | the name of the ruleset that holds the merge authority |
| `required_checks` | the checks the ruleset must require, and that must be green |
| `protected_paths` | paths a loop must not merge on its own |
| `codeowners` | where the CODEOWNERS file lives |
| `allow_same_family` | whether the implementer and reviewer may share a model family |

A repository with no table is a repository nobody configured: shadow mode, no
protections, nothing merges.

### Shadow first

`auto_merge = "shadow"` records the would-merge decision in the merge ledger
and leaves the merge to a person. The person's answer is recorded against it:

    pr-merge-ledger record-outcome <owner/repo> <pr> <head> merged --by NAME

The disagreement rate counts only the decisions a person answered. An
unanswered decision is not agreement: nothing has been said against it, and
nothing has been said for it either. Counting it as agreement would make the
rate fall as the loop got slower, which is the wrong direction for the number
that decides a rollout.

### What the tool refuses

Every one of these is a refusal, and each names what is missing:

- the ruleset is absent, not enforced, or weaker than the list above;
- the approval is on another commit, or a push landed while the decision was
  being made;
- a required check has not finished, or is not in the ruleset's list;
- the pull request touches a path the repository protects -- it is approved,
  but labelled `pr-loop:needs-merge` and left to a person;
- `allow_same_family` has not been answered. An unanswered question is not a
  permission.

## Setup

    cp hosts.env.example ~/.config/agent-tools/env    # then edit
    cp gates.toml.example /path/to/project/gates.toml # then edit, and commit it
    cp pr-loop.toml.example ~/.config/hermes-tools/pr-loop.toml  # then edit
    export PATH="$PWD/bin:$PATH"

Every example file documents every field.

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

`pr-auto-merge` reads the protections from GitHub and refuses when they are
not what the issue asks for, but it cannot create them. The ruleset has to
exist on the repository, and a repository whose ruleset was never written
never auto-merges -- which is the safe direction, and also why nothing here
is a substitute for writing the ruleset.

`pr-merge-ledger`'s rate counts only the decisions a person answered, and a
rate over a handful of decisions is not evidence. The tool reports the counts
beside the rate so a small sample reads as the small sample it is.

## License

MIT. See [LICENSE](LICENSE).
