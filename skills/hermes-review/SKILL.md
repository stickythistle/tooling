---
name: hermes-review
description: Send a diff to a Hermes profile for an independent second-model review, then verify its findings against real source before reporting. Also drives the review-fix loop over a branch an agent lane implemented, from review through fixes to a PR marked ready. Use when the user asks to have work "scrutinized by hermes", cross-checked by a second model, reviewed by something other than Claude, or reviewed until clean. Triggers - "hermes review", "scrutinize this", "second opinion on this diff", "have hermes look at it", "review until clean", "review each lane".
---

# Hermes review

A reviewer from a different model family than the author has different blind
spots, which is the entire reason to spend the round trip. It also costs
accuracy. Treat every finding as a claim to check, not a result.

## Run it

```bash
~/.claude/skills/hermes-review/scripts/hermes-scrutinize.sh \
  [--base REF] [--profile NAME] [--model MODEL] [--extra TEXT] [--timeout SECONDS]
```

Run it from inside the repository under review. With no `--base` it reviews
uncommitted changes, falling back to the branch against the remote default.
It prints the findings file path on stdout and a severity tally on stderr.

`--profile` picks the reviewer. Choose one whose model family differs from the
one that wrote the change. A hosted profile sends the diff off the network.

A profile runs `REVIEW_SLOTS_<PROFILE>` reviews at once, one by default, and a
queued review waits without spending its timeout, so start reviews as work
arrives. A slow local model needs `REVIEW_TIMEOUT_<PROFILE>` well above the 900s
default: one has run past it on a five-line diff while still making progress.
`--base` sets only the far side of the diff, so a branch that is not checked out
needs a worktree.

A non-zero exit, or no findings file, means no review happened, never that the
review found nothing. Reviewers have timed out silently and read a diff
backwards.

## Verify before reporting

Read the findings file, then check every finding against the real source:

- **Line numbers drift.** A finding's substance can be correct while its cited
  line is tens of lines off. Locate the construct by content, then report the
  line you confirmed.
- **Redaction manufactures findings.** Hermes scrubs secret-shaped strings out
  of what it reads, so a literal like `Bearer <token>` reaches the model as
  `Bearer ***`. It then reports the mangled text as a defect in the source. Any
  finding whose evidence is the presence of `***` is an artifact. Confirm the
  real bytes on disk before believing it.

Report each surviving finding at its corrected location, and say how many were
discarded and why: the discard count is how the user calibrates the next run.

Hermes also emits a `CORRECT` section attesting to what it judged sound. Do not
pass that along as verification; it is one unreliable model's opinion, with the
same false-negative risk as the findings' false positives.

## A stack of pull requests

A stacked pull request is reviewed against its own base, so a defect that
appears only when two of them combine is invisible to both: the lower PR's
diff never shows the upper PR's value, and the upper PR's diff never shows
the lower PR's drop.

`hermes-review-pr --stack` reviews the top of the stack against the default
branch, once, and attributes each finding to a pull request of the stack:

    hermes-review-pr --pr 391 --stack

The stack is found from the PR's own base, and naming any PR of it reviews
the top. A stack that forks -- two open PRs based on the same member's head
-- has no single top and is refused, naming the siblings.

**The attribution is the point:** without it the review names a line and
nobody can say which PR to fix. A finding belongs to the PR whose diff
*introduced* the cited line, not the last PR to touch it, because the fix
goes in the introducer. A finding on a line no PR changed belongs to the
stack as a whole.

The review writes `REVIEW_NOTES.md` and `STACK_ATTRIBUTION.json` into the
worktree and prints the breakdown: each cited `path:line` with the PR that
owns it and the reason -- `introduced`, `stack`, or `unattributable` (the
path is not in the reviewed tree).

## Review until clean

When an agent lane implemented the branch, the review repeats until it finds
nothing, and every edit goes through a lane. Your attention goes on judging
findings; the lanes do the typing.

1. **Review the lane's commit** with `hermes-scrutinize.sh --base origin/main
   --profile <reviewer>`, and verify the findings as above. `--extra` carries
   the domain: what the change is for, who calls it and how, and the product
   decisions a reader could not recover from the code, such as which of two
   defensible behaviours is wanted. Never tell the reviewer what is already
   verified or what to judge. It answers the question it is asked, so every
   defect outside that question goes unreported however plain it would be.
2. **Review the diff yourself too.** A reviewer model reads a diff's design
   consequences well and sweeps the repository for what the diff breaks badly:
   a struct that gained a field while only some of its initializers did, a
   consumer the change never updated. That sweep is yours. For a change to
   identity, state or concurrency, check its brief's case table for a missing
   combination before reading the code: a lane never writes a case it was not
   given, so the code cannot show the gap.
3. **Send the surviving fixes to a fresh implementer**, `hermes-implement
   --profile <implementer>` on the same worktree. State each fix's mechanism
   and name the wrong one it is likely to reach for; a lane left to choose
   picks wrong about a third of the time. When a finding is rejected because
   of a product decision, that decision goes into the next round's `--extra`,
   not the verdict; one that belongs to another issue becomes that issue.

   A brief for a change to identity, state or concurrency carries a table, one
   row per combination of input and existing state, each with its outcome. A
   brief that names only the main path gets that path written and tested, and
   nothing else. A contract the change advertises is tested at the layer that
   advertises it. A route's error body tested one layer down passed while the
   route dropped it.
4. **Repeat from step 1** on the new commit. Stop when neither the reviewer
   nor you find anything worth changing.
5. **Condense the prose, then run `pr-ready`.** Run the `condense-prose`
   skill over the PR body and the diff's own comments yourself, because prose
   is where the lanes are weakest. `pr-ready` refuses the move while the prose
   finder still flags the diff.

A change you can prove outright, such as a rendered config, still gets the
reviewer pass. The proof covers only what you thought to check.

## Draft and Ready

Open the PR as Draft as soon as there is a branch worth pointing at: Draft says
work is still under way. Ready says no further change is expected, and hands the
PR to final review. So a PR stays Draft while a review round is open, a fix lane
is running, or anything known is still to land on its branch, however green it
is. A PR that goes Ready and then changes has spent its final reviewer's time on
a version that did not ship.
