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
| `remote-prune`    | clear what a builder no longer needs: landed trees, their `_prove` copies, and idle incremental caches |
| `builder-space`   | refuse a build the builder has no room for, before it starts |
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

## A builder that fills up

A builder that fills its disk mid-build does not fail as a build failure. It
fails as a linker bus error, an rsync `ENOSPC`, or a failed `.rmeta` write —
and it takes the CI Postgres on the same disk down with it. The container
stayed in restart backoff after the space came back, so the next gate failed
against it with "container is not running": an infrastructure failure wearing
a project's clothes.

Four things guard against it.

**The space check runs before the sync.** `builder-space` asks the builder
how much room it has and refuses the build when the answer is no. What
matters is what is left *after* the build, so the check counts the tree
being built and its `_prove` copy: a cold gate for one branch is two trees
of 35 GB, and a host with 60 GB free does not have room for it. Below the
floor the check drops the caches that belong to no open work, oldest first,
under the same lock a build holds; if it still cannot make room the build is
refused with **exit 4**, which a gate and a lane report as infrastructure
rather than as a failing test.

**The prune reaches every lane.** `remote-prune --all-lanes` measures each
lane in `BUILDER_LANES` and reports them separately. The lanes are separate
hosts with separate benches, and a prune that measured only `HOST_BUILDER`
left the others filling up — which is how six gates failed out of disk
across three lanes while one lane looked fine.

**A `_prove` tree goes with its tree.** `gate-prove` runs in a scratch
worktree named `<caller>-prove`, and the builder holds it as `<name>_prove`:
the same branch's build cache a second time over, and the larger of the two.
It has no worktree behind it, so "no worktree means keep" used to apply to
it and the biggest trees on the builder were kept forever. Now the pair is
judged together — pruned when the tree lands, and dropped with the tree's
incremental cache when the tree is idle.

**And on landing.** A branch that merged has a dead cache on every builder
it was built on, and the trees are known by name rather than guessed from
`git cherry`. `remote-prune --prune --tree <name>` removes that tree and
its `_prove` copy on every lane, in the same coordinator step as the merge.

## The review that runs the code

`hermes-review-pr --phase review` sends the diff to a second model, and that
model reads. It finds what is visible in the diff, and it cannot find a race,
an input shape the tests never try, or a helper that swallows an exit.

`hermes-review-pr --phase execute` is the lane that runs it. The reviewer
gets the diff, the repository's hazard notes (`AGENTS.md`, or whatever
`REVIEW_HAZARDS` points at), and the bench: `remote-task --task bench` for
the recipe, `gate-verify` for the gates the diff earned.

One rule holds the lane to account: **a finding carries the command that
reproduces it and the output that command produced.** The command is run
again against the reviewed head before the finding reaches the findings
file.

Nothing is dropped silently — every finding lands in one of four statuses,
and the tally counts them all:

| | |
|---|---|
| `reproduced` | the command ran and failed on the reviewed head |
| `not-reproduced` | the command ran and passed |
| `no-reproduction` | the finding carried no command |
| `reproduction-error` | the command did not run, or changed the tree |

A command that never ran says nothing about the defect, so it is no
reproduction. Not found, not executable, timed out, killed by a signal, or
refused before it started are all `reproduction-error`.

A kept finding's `observed` is the re-run's own output; the reviewer's text
is kept beside it as `claimed_output`.

What is enforced is the outcome, not the spelling of the command. The tree
is compared before and after every reproduction, and one that changed it is
a `reproduction-error`; the tree then goes back to the reviewed head.

The worktree must be clean before the lane starts. A restore puts the tree
back with `reset --hard` and `clean -ffdx`, so on a developer's working tree
it would take their uncommitted edits. The lane refuses a dirty tree.

A reproduction still runs with this process's environment and can reach the
network. This lane prevents neither: a write is caught after the fact, and a
network call by neither the tree check nor the program list.

Preventing it means a sandbox, which is the owner's call because it changes
what runs on the host. The list of refused programs is a first pass, not a
guarantee.

This lane needs a shell because a reproduction is a shell command the
reviewed project defines. `bin/finding-verify` in #181 takes the opposite
approach, a closed allow-list parsed into argv.

The phase records the head it ran against, and refuses to run when the
worktree's head differs from that head — exactly, never by ancestry.

The reproductions run in a separate detached worktree at the reviewed head,
because the artifact worktree carries the review's own commits and a
reproduction run there would be evidence about a commit nobody reviewed.

## Two identities, and a lane with neither

The loop has two GitHub accounts, and the two are different people: the
implementer authors the pull request, and the reviewer is the login the
operator's role binding maps the repository's owner to.

One login cannot approve a pull request it authored, so a verdict is a
review state only when the reviewer is somebody else — and a reply posted by
the implementer has to be told apart from a person speaking.

**The roles live in one file.** `roles.toml` — `~/.config/agent-tools/roles.toml`,
or `$ROLES_CONFIG` — says who reviews, which logins the implementer may
act as, and the identity a lane's commits must carry. Every tool reads it
through one module.

The implementer's logins are per repository: an owner reviews the one it
owns, and implements on the others.

`roles.toml.example` in the agent-tools checkout is the template.

**Credentials per role.** `gh_as implementer ...` runs `gh` as the
implementer, for one call, with that role's own credentials:
`GH_IMPLEMENTER_TOKEN` or `GH_IMPLEMENTER_CONFIG_DIR`. Unset, the role uses
the host's own `gh` login. A role with no credentials at all is **refused,
naming the variable to set**.

**The login is checked, not the variable's presence.** `gh_as` resolves the
login the call would act as — `gh api user` under the same credentials — and
refuses unless the binding names it, naming both. On the coordinator host
the ambient login is the reviewer, so an implementer call there would
otherwise act as the reviewer with no error at all.

**A lane's `gh` is pointed at an empty config.** A lane dispatched by
`hermes-implement` runs with a `gh` config directory that holds no hosts and
with no token in its environment, so an accidental `gh` call inside the lane
fails.

The lane runs as the same operating-system user as the dispatcher, so
credentials that user can reach are still on the host: the empty config
makes an accidental call fail, it does not make them unreadable. A lane
is told that, and must not use them.

## Watching `main`

`main-watch-run` runs `main-watch` over a timer and says what broke `main`.
Merging a pull request without a rebase can turn `main` red without any one
pull request's CI ever having run, so something has to look after the branch
itself.

    main-watch-run owner/repo [owner/repo...]

It judges every `main` commit since the commit it last judged, and sends one
message when a commit broke the branch. The message goes to a notify relay
when the env file names one, and to Telegram otherwise. The position it keeps
is per repository, under
`${XDG_STATE_HOME:-$HOME/.local/state}/main-watch/<owner>__<repo>`, so a second
run over the same commits judges nothing and says nothing.

A branch rewritten under the position resets it to the head, and says so. A
`gh` call that fails holds the position instead, so the next run judges the
same commits again.

**The env file.** `MAIN_WATCH_ENV` names a file with two keys in it:

    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...

A missing variable, a missing file, or either key missing or empty is refused
by name and exits 2. The token never appears in any output.

**Or a relay.** Where a notify relay holds the bot token, the host holds a
client token of its own instead, and that token can be cut off without
touching the other hosts. Two keys select that path:

    NOTIFY_RELAY_URLS=https://relay-one https://relay-two
    NOTIFY_RELAY_TOKEN=...

`NOTIFY_RELAY_URLS` is a space-separated, ordered list. The relay is used
when the list is not empty; without it, the Telegram path is used.

With the list set, `NOTIFY_RELAY_TOKEN` is required and the Telegram keys are
not; its absence is refused by name and exits 2. The relay token never appears
in any output.

The message is posted to `<url>/send`, with the token in an
`Authorization: Bearer` header and the text as a JSON `{"text": ...}` body.
The relay is reached over TLS, with its certificate verified against the
host's trust store.

- A `202` means the relay took the message and delivers it itself; any other
  2xx is taken as accepted too.
- A `4xx` means the relay refused this client: the run fails, naming the
  relay and its status. No other relay is tried: a working fallback would
  hide a revoked token behind it.
- A `5xx` or an unreachable relay falls through to the next URL.
- When every URL has failed, the run fails naming each one.

**The timer.** Any timer that can run a command will do. A crontab entry on the
host, running as the user whose state directory holds the position:

    */10 * * * * MAIN_WATCH_ENV=$HOME/.config/main-watch/env \
        $HOME/tooling/bin/main-watch-run owner/repo >/dev/null

**What the host needs.** An authenticated `gh`, because the commits and the
pull requests a commit came from are read through it. `gh auth status` on that
host must be a logged-in account with read access to the repository watched.

`MAIN_WATCH_BIN` names another `main-watch` to judge with. Without it the copy
in this checkout is used, so a stale `main-watch` earlier on the timer's `PATH`
cannot decide what a commit means.

## Metering a bake-off

`bakeoff-proxy` is a local pass-through between one harness and the provider.
The provider allows a single API key, so its own metering cannot separate one
harness's tokens from another's. The proxy answers with the provider's own
`usage`.

    bakeoff-proxy --port P --harness NAME --model M --upstream URL --record FILE

Each harness gets its own port and its own record file. The proxy forwards each
request to the provider with the one key, streaming included, and records one
JSON line per request:

| | |
|---|---|
| `harness` | the harness this port belongs to |
| `time` | when the request arrived |
| `model` | the model that was requested, which is what G2 compares |
| `answered_model` | the model the provider answered with, which is what C1 prices |
| `style` | the endpoint style the harness spoke |
| `path` | the path the harness posted to |
| `status` | the status the provider answered with |
| `latency_ms` | how long the request took |
| `usage` | the token counts, `input` uncached |
| `usage_raw` | the provider's block, as it came |
| `refused` | whether the proxy refused the request |

`input` is the uncached input in every style. The OpenAI and Responses styles
count the cached tokens inside their input, so the record subtracts them; the
Anthropic style's input already excludes the cache read.

An `https://` upstream is reached over TLS, with the provider's certificate
verified against the system trust store, so the provider's key crosses a TLS
connection.

The provider bills by context tier, so the answer may carry a larger tier's
name than the one that was asked for. The share of requests in the larger tier
measures how well a harness manages context.

A request for any model other than the one the run assigned is refused with an
error the harness sees, in the shape its style documents, and the attempt is
recorded. The provider is never asked, so G2 is prevented.

A stream is forwarded as it arrives, so a harness that renders as tokens arrive
sees them arrive. The usage comes from the end of the stream, in the event
each style ends with.

A provider that cannot serve an endpoint the harness called answers with its
own error, and the proxy relays that error as the provider wrote it.

**The env file.** `BAKEOFF_PROVIDER_ENV` names a file holding the provider's
key:

    MINIMAX_API_KEY=...

`--key-var` names a variable of your own instead. A missing variable, a missing
file, or the variable missing or empty is refused by name and exits 2. The key
never appears in a record, a log or an error.

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
