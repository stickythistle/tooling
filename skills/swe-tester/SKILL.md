---
name: swe-tester
description: Judges whether a change's tests would fail if that change were reverted, and writes ones that would. Use when adding tests to a diff, when deciding whether existing tests are evidence of anything, or when a suite passed and the change is still wrong. Distinct from swe-updater, which runs the caller's verification command rather than asking what the tests prove.
version: 1.1.0
metadata:
  hermes:
    tags: [software-engineering, testing, verification]
    category: software-engineering
---

# Software Engineer Tester (`swe-tester`)

## The only question

**Would this test fail if the change were reverted?**

A test that passes against the broken version is not evidence about the change.
It is evidence that something compiles. Every failure below is a way of
answering that question wrongly while producing a green run.

## Ways a test passes against the broken version

**It exercises a different layer than the defect.** A handler called a validator
with an argument it should not have. The test called the validator directly and
passed either way. Test the layer where the mistake lives: if the defect is in
what a caller passes, a test of the callee cannot see it.

**It reimplements the code under test.** One test copied the handler's `match`
into its own body and asserted on the copy — it reduced to
`assert!(Ok(()).is_ok())` and never called the handler at all. If the test
contains the logic it is checking, it is checking itself.

**It asserts one direction where the change has two.** A partial update must
change the field the caller sent *and* preserve the one they omitted. A test
asserting only the first passes against a version that writes NULL over
everything unmentioned — which is worse than the bug it replaced.

**The starting value is the default.** "Omitted fields survive" is unfalsifiable
if the field starts empty and the bug writes empty. One field defaulted to `{}`,
so every test of it passed while it was being destroyed. Start from a value the
bug would visibly change, and name the field in the assertion.

**Another part of the system is the oracle.** A count endpoint was checked
against the length of a list response. The list paginates at 100, so the
assertion breaks at 101 rows for a reason unrelated to either. Compare against a
number the test itself put there.

**The fixtures vary nothing.** Thirteen tests covered a workflow parser; every
fixture had one job, named the same, placed first — the single arrangement in
which the bug could not appear. Against a real file it found nothing at all.
Fixtures shaped by the implementation confirm the implementation.

**The comparison is looser than the claim.** A check for a leaked directory
matched `/bin` as a substring, which lives inside `$HOME/.local/bin`, so it
failed the correct value. Split on the separator the data actually uses.

**The fixture cannot discriminate.** Two rows sharing a value made a
deduplicating assertion identical whether the filter worked or not. Before
trusting a passing case, change one input and confirm the result changes.

**The scenario cannot discriminate either.** A scenario can pass because some
other mechanism also catches the bug. When it exists to exercise one check,
assert the precondition that check depends on: for a check on changed blobs,
that the two blobs really differ.

**A hand-written fixture agrees with the code.** A fixture written in the same
sitting as the fix shares its assumptions about the data. Build it from the
producer: run the real tool and capture its output. For a diff, run `git diff`
in a real repository and read the blob shas from `git rev-parse`; for a parser,
feed it a real file.

**A reproduction script asserts the bug.** The script handed over with a
finding usually asserts the buggy outcome, so running it unchanged proves the
bug survived, not that it is gone. Take its cases and invert their expectations
against the fixed tree. Say in the handover which script you adapted and how,
so the coordinator knows the finding was re-checked, not copied.

## Before believing a green run

**The suite must have run.** A build step failing before any test executes exits
non-zero, which a revert-and-expect-failure check reads as proof. Confirm tests
ran and how many.

**Read the exit code without a pipe.** `cmd | tail` reports `tail`'s status.

**An agent's report of what it verified is a claim.** Reports on record: "the
artifacts gate passed", true and true only because it was the one gate that ran;
"the PR is left in draft", when no PR existed. Trust what a report says it
skipped; check what it says it completed.

## Writing one

Name the field under test in the assertion message, start from a value the
defect would visibly change, and assert both what must change and what must not.
Then revert the change and watch it fail. That run is the evidence; everything
before it is a prediction.

When the fix has two independent parts, break one at a time and record each
run: a test that fails only with both broken pins neither. The handover quotes
the failing run — the edit and the message it printed.
