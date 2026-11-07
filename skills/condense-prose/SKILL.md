---
name: condense-prose
description: Condense docstrings, code comments, markdown docs, and PR bodies down to the non-obvious facts. Use when asked to cut wordiness, tighten prose, review a diff for bloat, or before opening a PR.
---

# Condense prose

## What cannot be cut

Read this before the finder, and before you cut anything.

**In a skill file, an instruction, a rule's example, a trigger phrase in the
description, or a negation cannot be cut or reworded.** Only restatements,
filler and history can.

A skill file is not prose that describes behaviour. It *is* the behaviour:
a lane loads it and acts on it. So in a skill file:

| | |
|---|---|
| an instruction | "run the gate before the commit" is the gate running. Cut it and the gate does not run |
| a rule's example | the example is often the only place the rule says what it means. "a route's response is tested through the route, not through the repo function under it" is not decoration |
| a trigger phrase in the description | the description is how the skill is chosen at all. "Triggers - 'hermes review', 'scrutinize this'" is what makes it load. Cut a phrase and the skill stops being found |
| a negation | "say what the reviewer did **not** check" and "name what was checked" are opposites. A negation carries the meaning; a rewrite that keeps the words can reverse it |

The rest of this skill is about ordinary prose. A skill file is not ordinary
prose, and the finder treats it as such: an `oversize` flag on a skill file
is a question about whether a rule can lose its example, not an instruction
to shorten it.

**If a flagged block has none of those to remove, it stays, and you report it
as kept.** Say which block and why you kept it. A block you left alone
because it was protected is a result; a block you left alone because you ran
out of time is a gap, and the two must not look the same.

`find_prose.py` finds candidates mechanically. Every deletion is still a judgment call.

```bash
python3 ~/.claude/skills/condense-prose/find_prose.py --diff origin/main   # scope to a change
python3 ~/.claude/skills/condense-prose/find_prose.py docs python          # scope to paths
python3 ~/.claude/skills/condense-prose/find_prose.py body.md --json       # a drafted PR body
```

Findings, most-mechanical first:

| Finding | Means | Usual action |
|---|---|---|
| `provenance` | a link to the agent session that wrote the change | delete the line. A PR body, commit or doc records what the software does, never how the work was made |
| `duplicate` | byte-identical block in N files | keep the copy in the file that owns the concern, delete N-1 |
| `enforced-in-raise` | a backticked token in the prose also appears in a `raise` string | delete the prose; the error message is the enforcement |
| `echoes-code` | comment words are the next line's identifiers | delete |
| `echoes-prose` | table row repeats a paragraph in the same file | delete the row; a one-row table is a sentence |
| `trailing-clause` | a bullet continues past its action with `, so` / `, which` / `, and` / `—` | truncate at the action. Decisive on a changelog or PR-body bullet; advisory in a doc, where a bullet is allowed to be a sentence |
| `near-duplicate` | ≥85% similar across files | collapse to one, or accept the drift |
| `roadmap` | future/deferred/TODO wording | delete; the tracker owns future work |
| `negative-space` | describes what a thing isn't or where it isn't | delete or restructure until self-evident |
| `oversize` | over `--min-words` (40) | rewrite as one flat declarative per surviving fact |

A table is never oversize: its length comes from its row count, and a row is judged on its own.

Under `--diff`, `oversize` is decided by the lines the diff added. A block that
grew by one paragraph is judged on that paragraph; the lines it already had
stay available to the other findings, but they do not count toward size. A
block the diff only deleted from is not oversize either: the diff wrote none of
it.

A block in a `skills/*/SKILL.md` carries the context mark `skill-file`. It is
not a finding: the block is still scanned, and a restatement in a skill file
is still reported. The mark says how to read a finding there.

A block in a `tests/` file is never `oversize`: a test docstring states its
setup and the response shape it pins, and the limit cuts the fact the test
exists for. Other findings still apply there.

## Cut beyond what the finder sees

- **Same rule stated three ways.** Keep one. "Verification accumulates … a requirement is verified by the set of tests that mark it … so a partial test still marks it" is one sentence, not three.
- **Heading colon-clauses.** `### Requirement markers: which requirement a test verifies` — the term already said it.
- **Colon-appositives in bullets.** `Added \`pytest_requirement_markers\`: registers one marker per requirement` — the module name already said it.
- **Compound bullets.** `Marked X, and gave Y its entry` is two edits. Split, then truncate each.
- **Pointer chains.** File A points to B, B points back to A. Keep one direction.
- **Restated intro facts.** A table cell repeating the file's opening paragraph is the copy that drifts.
- **Fancy words.** "marker vocabularies" → "markers". "supplies validity" → "fails collection".

## Floors

Calibrate against what survived, not what reads well:

| Medium | Before | After |
|---|---|---|
| doc paragraph | "A mark and the requirement's own `references:` entry are the two directions of one link: mark `REQ_0001` and REQ_0001 names the test file back." | "Links are bidirectional." |
| doc rule | "A UID that no active item in `requirements/` produces fails collection — a typo, or a UID a requirements-tree edit left stale." | "`--strict-markers` fails collection for an invalid requirement." |
| table cell | "`REQ_0001`. Immutable once merged to `main`, never reused. Renumber freely before merge; CI's uniqueness check catches collisions." | "`REQ_0001`. CI's uniqueness check catches collisions." |
| module docstring | 4 stanzas: summary, marker-name derivation, `--strict-markers` location, policy pointer | one line: summary + policy pointer, under the line-length limit |
| PR bullet | "Replaced the Doorstop UID separator (`sep: '-'` → `sep: _`) and renamed the 14 items, so a UID is usable as a pytest marker name." | "Replaced the Doorstop UID separator (`sep: '-'` → `sep: _`)." |

A two-line replacement usually loses; a one-line replacement usually survives.

## Keep

- Gotchas a reader cannot infer: `tryfirst=True` running before pytest's `-m` deselection.
- Why a `continue`/early return exists, when the reason lives in another system's semantics.
- One pointer to the policy doc per file.
- In a PR body: one bullet per edit, then the verification evidence with its exact error strings. Nothing else — rationale belongs in the review thread and the tracker.

## Process

1. Run the finder scoped to the change.
2. Read each flagged block in its file — a duplicate is only safe to delete once you know which file owns the fact.
3. Rewrite, don't trim. Target one flat declarative sentence per fact that survives.
4. Verify: run the project's test gate and formatter, check the line-length limit. Prose edits must not change behavior, and a reflowed docstring can break a lint.
5. Report per-file before/after line counts, plus anything cut that a reviewer had explicitly asked for — that needs their sign-off, not yours.

## Report what you changed, sentence by sentence

**For every block you changed, report the sentence before and the sentence
after.** The coordinator reads those pairs instead of reconstructing them
from the diff, and the difference is the whole point: a diff shows that
words went, and only the pair shows that the *meaning* went with them.

Write the report to a file and hand it over. One entry per changed block:

    skills/pr-review-coordinator/SKILL.md:167
    - "Say in the approval what the reviewer did not check, such as a merge order two PRs share."
    + "Name what was checked, such as a merge order two PRs share."
    kept: negation — "not" is the meaning, and the rewrite reversed it.

Two lines per block, and the text on both sides is the sentence as it
stands in the file — not a paraphrase. A pair whose "after" reads like the
"before" with a word missing is exactly the case the coordinator needs to
see.

**Report the blocks you kept, too, and say why.** A block kept because it
carries an instruction, an example, a trigger phrase or a negation is a
result. A block kept because you had no time is a gap, and the two must not
look the same in the report. The `kept:` line says which.

Nothing else in the report: the before/after pairs and the kept blocks, per
file, in the order the finder flagged them. The per-file word counts go in
the same file.

## Expect several passes

PR #69 went 37 → 23 → 17 → 20 words across four rounds, and its `docs/testing.md` section went 21 → 11 → 9 → 7 lines. Each round cut something the previous round had argued was load-bearing, including wording a reviewer had proposed. Open with the barest version instead of defending the middle one.

## Rules of engagement

- Don't `gh pr edit`, push, or post review replies unless asked in so many words. Draft to a file and hand it over.
- Deleting prose a reviewer asked for is the author's call. Flag it; don't decide it.
