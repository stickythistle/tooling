---
name: pr-review-coordinator
description: Drive pull requests to mergeable from the reviewer's seat - review as the reviewer account, hand each review to an agent lane on the agent host with agent-fix-pr, check what comes back, repeat until approved, with a hard round cap; and brief an agent lane on new work before its PR exists, via `hermes-implement --issue N --extra BRIEF`. Use when asked to "address the review with lanes", "iterate until mergeable", "kick off fix lanes", to run the review loop across a set of PRs, or to "brief this lane".
---

# PR review coordinator

Three identities, and keeping them apart is what makes the loop readable on
GitHub:

| Role | Who | Where |
|---|---|---|
| Author | the account the PR's commits come from | the agent host's git and `gh` |
| Implementer | an agent lane, by profile | the agent host, through `agent-fix-pr` |
| Reviewer | the reviewer account, through `gh` here | this host |

Every review is posted as the reviewer and every reply comes from the author's
side, so a thread reads as a conversation. The ledger `agent-fix-pr` keeps on
the agent host, not this conversation, is the record of what each round did.

## A pull request's life

1. The lane opens a draft.
2. The coordinator reviews the draft with `COMMENT` reviews, and the fix rounds
   run on the draft.
3. When nothing is left to change, a finishing run condenses the prose, squashes
   and pushes.
4. The coordinator waits for CI, then runs `pr-ready`, which takes the PR out of
   draft when the gates and the prose pass.
5. The coordinator confirms the change since its last review is prose only, then
   approves at that sha.
6. The coordinator merges.

A draft is reviewed but never approved.

## Briefing a lane on new work

New work starts as an issue. Write a brief and pass it as
`hermes-implement --issue N --extra BRIEF`. The brief is the step before the
loop. Name the brief sections inline (Goal, Context, Mechanism, Constraints,
Tests, Acceptance, Outputs).

**Check every mechanism before prescribing it.** Each instruction
(command line, flag, value form) is checked at every call site against
tree and settings. Prescribe only where you checked it; state the goal
and constraint otherwise.

**Give the real shapes.** For every value crossing a boundary (env var,
config entry, CLI argument, agent command), Context lists the forms
(single word, command with arguments, empty, with spaces); Tests cover
each. Read the value from the live source.

**Every invariant gets a test.** Each "stays as it is" in Constraints
names the behaviour concretely (exit on failure, on timeout, fds,
retries) with a matching Acceptance row. An invariant without a test is
guidance the lane cannot honour.

**Failure paths are acceptance rows.** For a new wrapper or helper,
the brief names what the caller sees when the wrapped command fails,
times out, or goes missing. A silent failure path hides the bug.

**Size work to the attempt timeout.** Split an issue into runs that
each fit. Commit each verified item before starting the next.

**Search first.** Before briefing, search existing issues. The brief names
its issue.

**Check the brief against the escalation list before dispatch**, not after the
merge. Anything touching auth, access, a runner allow-list or security goes to
the user first.

**Name the wrong mechanism.** In Mechanism, write the tempting wrong
approach next to the right one and say why each fails. The lane has
only the right answer otherwise and has nothing to look out for.

## Per PR, one at a time

Finish one PR before starting the next. Take stacked PRs parent first: a fix to
the parent changes what the child is reviewed against.

`agent-fix-pr` refuses a PR whose body links no issue (`Closes #N` or
`Part of #N`) with exit 2, so check the body before the first review.

1. **Review the head.** Note the head sha first, and post the review against
   it (`commit_id`). The first round reads the whole diff; later rounds read
   `start..end` from the ledger, then check each earlier comment against the
   new code and its reply. Confirm every finding against the source before
   posting it: cite the file and line, and the concrete input that breaks.

   **Run what changed.** A new wrapper: drive it with a failing stub
   and report the output. A changed call site: run it against the
   live value, not the example.

   **Model reviewers are a second opinion.** `hermes-review-pr` runs are
   never a substitute for the coordinator's review: verify each finding
   before relaying it. A slow local profile needs `--timeout` well above
   the default.

2. **Write each comment as a brief.** Severity (`[P1]` breaks a user-visible
   path, `[P2]` a real defect off the main path, `[P3]` a latent risk), the
   failure, then the mechanism to fix it, and the tempting wrong mechanism by
   name. A lane left to choose its mechanism picks wrong about a third of the
   time. For a change to identity, state or concurrency, include the table of
   input × existing state → outcome; a lane tests only the rows it is given.
3. **Decide.** Nothing worth changing: the content is agreed. Dispatch the
   finishing run (see Lane-side merge work), then approve at its final sha.
   Otherwise post a `COMMENT` review and keep its id.
4. **Dispatch the round:**

   ```bash
   ssh <agent-host> '<runtime settings> <tooling>/bin/agent-fix-pr \
     --repo OWNER/NAME --pr N --review ID --profile <profile> --detach'
   ```

   The runtime settings are the operator's, kept on the agent host rather than
   in this repository: `PIPELINE_IMPLEMENTER` (the agent's driver, with any
   options it needs to load the `swe-review-fix` skill) and, for an agent that
   can fall back to another model, `AGENT_FIX_PR_MODELS`. Before dispatching,
   count the profile's live sessions: a hosted model has a concurrency limit,
   and a session past it may be answered by a fallback.

   **Cross-repo PRs need `--worktree`.** Until `agent-fix-pr` picks the clone
   per repository, a PR on a repo other than the one the operator's settings
   name needs `--worktree`.
5. **Wait** with a background poll on `agent-fix-pr --status --pr N`, which
   exits 5 while a round runs. Rounds take tens of minutes; do not poll in the
   foreground.

   **Name the poll pattern carefully.** A background poll matching processes
   by name must not match its own command line. `'[a]gent-fix-pr'` matches
   the process name but not the poll's own command line. Or poll
   `--status` or the round's pid.
6. **Read the round.** The ledger's last line has `verdict`, `gate`, `pushed`,
   `replied` and `models`. A model in `models` other than the profile's own
   means the round ran on a fallback: judge its work as you would a weaker
   model's. Then read the diff `start..end`: grep it for `***`, which some
   agents write over secret-shaped text in the source, and check each fix used
   the mechanism the comment named.

   A round that committed and was not pushed for a reason outside its work,
   such as a gate log showing its tools could not start, is fixed on the host
   and taken up with `agent-fix-pr --resume`, which does not spend a round.
   A gate that ran and failed on the lane's code is a finished round: review
   what it left, and say what failed in the next review. So is `answered`: the
   lane pushed back on every comment and committed nothing, and its replies are
   on the threads. Rule on each one. A round that did not push still counts.
7. Back to step 1.

## The cap

`agent-fix-pr` refuses a sixth round per PR (`--max-rounds`). When a PR
reaches the cap without an approval, **halt the whole run**: dispatch nothing
further on any PR, and report to the user:

- each round's review id, verdict, and what the lane did with each comment;
- which findings recurred, and whether the brief or the lane is at fault: a
  finding that comes back in new words usually means the mechanism was never
  stated;
- what you would change before running the loop again.

The cap counts rounds, not findings. Five rounds that each fix their comments
but surface new ones is a design problem in the PR, which is the user's call,
not another round's.

## Merge readiness

Approval means the reviewer has no change to ask for. It comes after `pr-ready` has taken the PR out of draft and after the prose-only check, at the final sha. It does not merge. Say in the approval what the reviewer did not check, such as a merge order two PRs share.

The merge note lists every call the coordinator made on its own.

## Lane-side merge work

The coordinator never pushes a PR branch. Implementation, the first
push, opening the draft, rebasing, squashing and reauthoring are lane
work. The coordinator reads, reviews, approves and merges. Pushing the
head it approves is approving its own work.

When the content is agreed, the lane runs these in order, and only these:

1. `condense-prose` over the PR's own lines and the PR body. Lane profiles
   ship without the skill; the brief points at
   `skills/condense-prose/SKILL.md`.
2. The squash, with the coordinator's subject and body. The lane
   commits under its own configured git identity. The brief never sets
   one (`-c user.name`, `--author`).
3. The push.

The coordinator runs `pr-ready N <worktree>` after the push, once CI's checks
have finished (`gh pr checks N --watch`): it refuses while a check is still
running. It takes the PR out of draft when the gates and the prose pass.

The coordinator then checks the change the finishing run made, from the
approved head to the new head, and approves at the final sha. A draft is never
approved, and approval comes last.

### What the finishing run changed

`condense-prose` edits comments, so the patch-id after a finishing run never
matches the approved head's. The check is per file instead, and it starts by
reading the whole diff. Docs, terms and prose files may differ.

**1. Read the full finishing diff, first.** For every file the finishing run
changed, read the diff between the approved head and the new head. The read is
the check; the filters below only narrow it.

```bash
git diff <approved-sha>..<new-sha> -- <file>
```

**2. For every changed line that is not plainly code, confirm what it is.** A
line is ignorable only when it is a real comment, and only when it is a real
comment *outside* any string, raw string or heredoc. A line that starts with a
comment marker is not a comment: the marker can be literal data.

- A shell heredoc body is data, not a comment:

  ```bash
  #!/usr/bin/env bash
  cat <<'PAYLOAD'
  # endpoint=staging
  PAYLOAD
  ```

  That `#` line is the script's output. Changing `staging` to `production`
  changes behaviour.
- A Rust raw string's body is data, not a comment:

  ```rust
  let s = r#"
      // endpoint=staging
  "#;
  ```
- A removed blank line may be literal data too: a blank line inside a heredoc
  or a raw string.

**3. The regex comparison is a helper, never the proof.** The filters below
say where to look. They cannot tell a comment from a comment-shaped string, so
a passing diff is not a passing check. Narrow the file with the filter, then
confirm every ignored line by hand against step 2.

| Extension | Filter |
|---|---|
| `.rs` | `^\s*//` |
| `.sql` | `^\s*--` |
| `.sh`, `.py`, `.toml`, `.yaml` | `^\s*#` |
| anything else | none: compare the file whole |

Drop blank lines in every case. The `.rs` filter covers `///` and `//!` as well
as `//`. The `^\s*#` filter drops a `#!` first line, so prepend it back:

```bash
{ git show <sha>:<file> | head -n1 | grep -E '^#!'; \
  git show <sha>:<file> | grep -vE '^\s*#' | grep -vE '^\s*$'; }
```

One filter per language, never a combined one. A combined `^\s*(#|//|--)`
strips Rust's `#[...]` attribute lines, so a finishing run that deletes
`#[serde(default)]` or `#[sqlx::test(migrator = …)]` leaves the file looking
prose-only and the check passes on a code change.

```bash
# Rust: attributes are code and survive the filter.
diff <(git show <approved-sha>:<file> | grep -vE '^\s*//' | grep -vE '^\s*$') \
     <(git show <new-sha>:<file>       | grep -vE '^\s*//' | grep -vE '^\s*$') \
  || echo "CHANGED: <file>"
```

**4. Anything ambiguous is a review, not an approval.** A line whose lexical
context you cannot settle from the diff — a comment marker inside a string, a
heredoc, a macro, a generated file, a blank line whose meaning is not visible
— gets the normal treatment: a review round against the change, and a reply
from the lane. Do not approve a finishing run you could not read. A
syntax-aware comparison settles the same question, and the answer is still the
coordinator's to give.

If the base moved, make the same comparison against each file's own head.

### Rules for the finishing brief

- **Keep the attribution line.** The PR body's attribution line stays:
  `condense-prose` flags it as provenance, and a lane following the skill
  removes it. The brief names it as required.
- **Read only the last gate line.** The gate's verdict is its last `==>` line.
  The prove gate prints `==> FAIL (bench: …_prove …)` for its negative control
  on every passing run; a lane that reads it as failure stops a finished run.
  A branch whose diff earns no gate prints `nothing to gate:` and exits 0 with
  no `==>` line; that is its pass.
- **Run the gate in the foreground** and wait for it.
- **Push with a lease.** Use
  `--force-with-lease=<branch>:<reviewed sha>`. Stop on a rebase conflict
  rather than resolving it.

Squash order follows merge order, parents first. Until `agent-fix-pr`
has a finishing mode, run steps 1–3 as one `hermes-implement` brief
naming the PR, the approved sha, the subject and body, the four rules
above, and the prose-only check the coordinator will run.

## A lane that stops short

A run that ends without producing its outputs is not finished work. The
coordinator reads what was committed and writes a **continuation brief**. It
names those commits, says they are not to be redone, and lists only the
remaining steps.

Rerunning the original brief redoes finished work and risks a second,
different implementation.

## Parallel lanes

Lanes that each add a migration, or edit the same files, run one after
another. When two lanes' migration numbers collide, the second renumbers in
its continuation or finishing run, after the first merges. Lanes whose files
don't overlap may run in parallel.

## Cleanup and pausing

After a merge, remove the lane's worktree, its local branch, the gate's prove
worktrees and the bench directories for the branch, each by name. Never by
pattern, never before the merge.

**Pause lanes** means in-flight runs finish and nothing new is dispatched,
including review rounds and finishing runs, until the user resumes.

## Skill freshness

The coordinator loads this skill from a local checkout of the tooling
repo. After a merge to `skills/`, update it to current main before the
next round.

When reporting a round, name its commit: a reviewer reading a finding
against a skill they cannot see is reading the wrong report.

## Interim notes

These are how the loop runs while their fixes have not landed. Each is
written so it can be deleted when its fix does.

- Review items go in line comments, not in the review body, until the
  review driver answers body items.
- Two model reviewers on one PR need separate clones. The review branch
  is named per PR; the second fails on a branch that already exists.
