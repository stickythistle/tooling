---
name: brief
description: Reads a written brief as the work item, and acts on it rather than on the paired skill's defaults. Use when a lane is handed a brief file instead of a plain issue, or when a task needs its own format, mechanism and acceptance criteria. Pairs with swe-updater, swe-reviewer and the other skills.
version: 1.0.0
metadata:
  hermes:
    tags: [software-engineering, briefs, planning, delegation]
    category: software-development
---

# Brief (`brief`)

A brief is a written statement of one piece of work: what to do, what to
read first, how to do it, and what "done" means. It replaces the issue
text.

The brief is not a suggestion. It is the work item, and the paired skill
is the procedure that carries it out.

## The brief outranks the skill

Where the brief and the paired skill disagree, the brief wins. The
skill knows how to implement; the brief knows what. A brief that says
"add a migration" and a skill that says "write tests first" do not
conflict. A brief that says "do not add a migration; the schema is
changing" and a skill that says "write a migration" do, and the brief is
the one that knows why.

This cuts one way only. The skill's *procedure* still applies where the
brief is silent: how to verify, how to commit, what to leave alone, how
to report what was not done. The brief overrides the skill's defaults,
not the skill's discipline.

## The sections

A brief has seven sections. A section that does not apply is written as
`None` rather than left out, so a missing section is a decision and not
an oversight.

**Goal.** What the work is, in one or two sentences. Not a title: the
sentence a reader would use to tell whether the work was done. If the
Goal cannot be written without naming the mechanism, the brief has
already decided something it should have left open.

**Context.** The files to read before acting, one per line, each with
why it matters. These are paths, not pasted content: a brief that
quotes the code it wants changed has a copy that goes stale, and the
stale copy is what gets implemented. A large input -- a diff, a
previous review, a schema -- is a file the brief names, never text
pasted into it.

**Mechanism.** How to do the work, and the tempting wrong way to do it.
Name the wrong one explicitly. A lane that is told only the right
mechanism will often find the wrong one first and defend it; a lane
that is told "not in the parser, in the caller" does not spend a run
there. The wrong mechanism is the one an experienced engineer would
reach for, not an obviously wrong one.

**Constraints.** What must not change, and what must not be touched.
The boundaries: files that are off limits, an interface that has to
keep its shape, a performance budget, a dependency that cannot be
added. A constraint the lane breaks while satisfying the Goal is a
failure even when the Goal is met.

**Tests.** The test that must fail first, and how to run it. If the
brief names no test, the paired skill's own verification applies. If it
names one, that is the test to watch fail before the change and pass
after it. A brief with a test named is a brief whose work can be
checked by someone who was not there.

**Acceptance.** What a person checking the work would look at, and what
they would see if it were done. Each line is checkable: "the migration
runs on a fresh database", not "the migration is correct". Acceptance
is separate from Tests because the tests may not cover everything that
has to be true.

**Outputs.** The files the work produces: the handover, a generated
file, a configuration change. A named output that is not produced is a
missing output, not an oversight.

## Read the whole context before acting

Every file in Context is read before the first edit. A lane that starts
editing after reading two of the four files has decided from half the
evidence, and the half it skipped is usually the half that would have
changed the decision.

The Goal says what the work is. The Context says what the work has to
fit into, and the fit is often the whole difficulty: the change looks
obvious until the file that consumes it is read.

## Stop rather than guess

When the brief contradicts the code -- the brief names a function that
does not exist, a path that has moved, a behaviour that is the opposite
of what the brief says -- write `BLOCKED.md` in the worktree and stop.

`BLOCKED.md` says what contradicts what, in the brief's terms and the
code's, and what would resolve it. It is not a complaint and not a
question: it is a report of a fact the person who wrote the brief does
not have.

A lane that guesses past a contradiction produces work that is wrong in
a way nobody can see, because the work looks finished. A lane that
stops produces a report of the contradiction, which is the cheapest
possible outcome.

The same applies to a brief that is silent where it matters and a
Constraint that cannot be satisfied: report it, do not work around it
by writing something outside the worktree.

## Composition

The brief skill pairs with the skill that carries the work out:

    --skills brief,swe-updater

A skill cannot take an argument, so the brief arrives through the
prompt: the driver names the brief's path, and the brief is read from
that path. Pair it with `swe-reviewer` for a review whose findings are
also set out as a brief, and with `swe-tester` for a test whose
assertion the brief has already decided.

Loaded alone, `brief` does nothing useful: it defines a format and how
to read one. It is a modifier on another skill, never a replacement for
one.

## Pitfalls

**Treating the brief as the issue text.** A brief is not a question to
answer. It is a decision already made, and the work is carrying it out
rather than working out what to do.

**Pasting the brief into the prompt.** The brief is a file. A brief
over 128 KiB cannot be an argument, and one pasted into the prompt is
one that cannot be read in full.

**Adding a mechanism the brief did not ask for.** The brief says what
to do; it does not always say how, and the how is not an invitation to
improve it. A brief that wanted the mechanism improved would have said
so in the Mechanism section.

**Reading the Goal and skipping the Constraints.** The Goal is the
part that is easy to act on. The Constraints are the part that makes
the work acceptable, and they are checked at the end.

**Treating `BLOCKED.md` as a failure.** It is a report, and it is the
right outcome when the brief and the code disagree. A run that stops
with a report has cost one run. A run that guesses has cost the work
and the review of it.

## Verification

Before finishing: every file in Context was read, every Acceptance line
is checkable and holds, every Output exists, and nothing outside the
Constraints changed. If the brief and the code disagreed, `BLOCKED.md`
is written and nothing else was.
