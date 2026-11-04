---
name: condense-prose
description: Condense docstrings, code comments, markdown docs, and PR bodies down to the non-obvious facts. Use when asked to cut wordiness, tighten prose, review a diff for bloat, or before opening a PR.
---

# Condense prose

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

## Expect several passes

PR #69 went 37 → 23 → 17 → 20 words across four rounds, and its `docs/testing.md` section went 21 → 11 → 9 → 7 lines. Each round cut something the previous round had argued was load-bearing, including wording a reviewer had proposed. Open with the barest version instead of defending the middle one.

## Rules of engagement

- Don't `gh pr edit`, push, or post review replies unless asked in so many words. Draft to a file and hand it over.
- Deleting prose a reviewer asked for is the author's call. Flag it; don't decide it.
