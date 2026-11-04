---
name: swe-review-fix
description: Answers one posted code review on a pull request branch — fixes each comment, adds the test, commits, writes a reply per comment. Never pushes, rewrites history, or answers on GitHub itself.
version: 1.1.0
---

# Review round (`swe-review-fix`)

The branch already implements its issue; a reviewer has read it and left
comments. This round turns those comments into commits the reviewer can check
one by one. The caller pushes, and posts your replies, only after its own gate
passes, so a reply naming a commit is a claim the caller will test.

## What each item gets

The brief has two kinds of items:

- **Body items, headed `### review`.** The text under that heading is the
  reviewer's summary or numbered list. Each numbered or distinct request in the
  body is its own item; answer each one under `"comment_id": "review"`.
- **Line comments, headed `### comment <id> at <path>:<line>`.** Each one is
  its own item.

Replies may only use `review` or a line-comment id from the brief; an id the
lane invents is refused with a reason and skipped.

For each item, answer in one of two ways:

- **Fix it.** Make the change the item asks for, by the mechanism it names
  when it names one. Add or change a test that fails without the fix, at the
  layer the item talks about: a route's response is tested through the route,
  not through the repo function under it. One commit per item, named by the
  reply.
- **Push back.** When the item is wrong about the code, say why with the
  evidence: the file and line that shows it, or the command and its output.
  Change nothing for it. A reviewer would rather read a correct refusal than
  a fix for a bug that is not there.

An item marked P3 can be answered with a reason it is left alone, when fixing
it would widen the PR.

## The rules of the branch

- Commit each finished, verified comment on its own before starting the next.
- New commits on top of the branch's head. Never amend, rebase, reset or
  squash: the caller refuses to push a branch whose history moved, and the
  reviewer reads this round as the commits added since their review.
- Never push, never open or edit a pull request, never post on GitHub. The
  caller pushes the branch and posts your replies.
- Commit subjects say what the code now does. No review numbers, round
  numbers, or reviewer names in commits or source. The caller checks the
  commit message: "Round 2" is a refusal, "round-robin" passes.
- Run the verification command the brief gives until it passes, as in
  `swe-updater`'s Phase 4.

## Replies

Write the replies file the brief names, as a JSON array with one object per
item. Body items use `"comment_id": "review"`. Line comments use their numeric
id from the brief's heading:

```json
[
  {"comment_id": "review", "body": "**Item 1**: fixed in 1a2b3c4 ..."},
  {"comment_id": "review", "body": "**Item 2**: pushed back ..."},
  {"comment_id": 123, "body": "Fixed in 4d5e6f7: ..."}
]
```

Each reply names the short sha of the commit that fixes the item, or the
reason it is left alone. The validator drops a reply whose sha the branch
lacks. The file posts without commits, so a push-back round answers.

One or two sentences: the reviewer has the diff.

For body items, write one entry per item under `"comment_id": "review"`, and
start each reply by quoting the item it answers. The driver joins them into a
single PR comment, separated by blank lines.

Then write HANDOVER.md as `swe-updater` describes, listing each item id with
fixed or pushed back.
