#!/usr/bin/env bash
# Mutation sweep for the execution review lane.
#
# One mechanism per mutation, each recorded separately, so the handover can
# say which mechanism is doing the work.
#
# The mutated copy lives in a scratch directory, never in the working tree:
# a half-reverted tree is how a mutant gets committed.
set -euo pipefail

REPO=/home/hermes/wt/tooling-93
SCRATCH=$(mktemp -d -t tooling-93-mutants-XXXXXX)
trap 'rm -rf "$SCRATCH"' EXIT

pass=0
fail=0

mutate() { # name file python-snippet
	local name="$1" file="$2" snippet="$3"
	local copy="$SCRATCH/$name"
	rm -rf "$copy"
	cp -a "$REPO" "$copy"
	python3 - "$copy/$file" "$snippet" <<'PY'
import sys
path, snippet = sys.argv[1], sys.argv[2]
old, new = snippet.split("|||")
text = open(path).read()
if old not in text:
    print(f"MUTATION TARGET NOT FOUND in {path}: {old!r}")
    sys.exit(3)
open(path, "w").write(text.replace(old, new, 1))
PY
}

expect_fail() { # name
	local name="$1"
	local out
	if out=$(cd "$SCRATCH/$name" && timeout 500 python3 tests/test_review_execute.py 2>&1); then
		echo "MUTANT SURVIVED: $name (the suite still passed)"
		fail=$((fail + 1))
		return 1
	fi
	echo "MUTANT KILLED: $name"
	echo "$out" | grep -E "^(FAIL|ok)" | grep "^FAIL" | head -3 | sed 's/^/    /'
	pass=$((pass + 1))
	return 0
}

echo "=== mutant 1: the head binding is loosened to an ancestor check ==="
mutate head_binding lib/review_execute.py \
'    if actual != head:|||    if False:'
expect_fail head_binding || true

echo
echo "=== mutant 2: a reproduction that passes is kept instead of dropped ==="
mutate keeps_passing lib/review_execute.py \
'    if status == 0:|||    if False:'
expect_fail keeps_passing || true

echo
echo "=== mutant 3: a finding without a reproduction is kept ==="
mutate keeps_unreproduced lib/review_execute.py \
'    if not isinstance(command, str) or not command.strip():|||    if False:'
expect_fail keeps_unreproduced || true

echo
echo "=== mutant 4: the tally counts only the kept findings ==="
mutate tally_counts_all lib/review_execute.py \
'        tally[status] = tally.get(status, 0) + 1|||        tally[status] = 1 if status == "reproduced" else 0'
expect_fail tally_counts_all || true

echo
echo "=== mutant 5: the tree under review is left dirty by a reproduction ==="
mutate dirties_the_tree lib/review_execute.py \
'    env["PYTHONDONTWRITEBYTECODE"] = "1"|||    env["PYTHONDONTWRITEBYTECODE"] = "0"'
expect_fail dirties_the_tree || true

echo
echo "=== mutant 6: the shell-operator check is a substring search again ==="
mutate crude_operator_check lib/review_execute.py \
'    for token in tokens:
        if token in operators:|||    for token in [command]:
        if token in operators:'
expect_fail crude_operator_check || true

echo
echo "=== mutant 7: the driver stops refusing a worktree at the wrong head ==="
mutate driver_ignores_head bin/review-execute \
'if [[ "$ACTUAL_HEAD" != "$HEAD_SHA" ]]; then|||if false; then'
expect_fail driver_ignores_head || true

echo
echo "=== mutant 8: an exit that means the command never ran is trusted ==="
mutate trusts_a_failed_start lib/review_execute.py \
'    failure = exit_failure(status)
    if failure is not None:|||    failure = None
    if failure is not None:'
expect_fail trusts_a_failed_start || true

echo
echo "=== mutant 9: the tree is not compared before and after the run ==="
mutate ignores_a_changed_tree lib/review_execute.py \
'    changed = tree_state(worktree) != before|||    changed = False'
expect_fail ignores_a_changed_tree || true

echo
echo "=== mutant 10: the worktree is not put back after a reproduction changed it ==="
mutate leaves_the_tree_dirty lib/review_execute.py \
'            restore_worktree(worktree, head)|||            pass'
expect_fail leaves_the_tree_dirty || true

echo
echo "=== mutant 11: the kept finding keeps the model's text as observed ==="
mutate keeps_the_models_text lib/review_execute.py \
'            "observed": output,|||            "observed": finding.get("observed"),'
expect_fail keeps_the_models_text || true

echo
echo "=== mutant 12: the git subcommand is read from the wrong token ==="
mutate misreads_the_git_subcommand lib/review_execute.py \
'        sub = git_subcommand(tokens)|||        sub = tokens[1] if len(tokens) > 1 else None'
expect_fail misreads_the_git_subcommand || true

echo
echo "=== mutant 13: the clean leaves the ignored files behind ==="
mutate keeps_ignored_files lib/review_execute.py \
'"clean", "-ffdx"|||"clean", "-ffd"'
expect_fail keeps_ignored_files || true

echo
echo "=== mutant 14: an exit of 126 is trusted ==="
mutate trusts_126 lib/review_execute.py \
'    if status == 126:
        return "the reproduction could not be executed (126)"|||    if status == -126:
        return "the reproduction could not be executed (126)"'
expect_fail trusts_126 || true

echo
echo "=== mutant 15: the restore keys on the reason string again ==="
mutate restores_on_the_reason lib/review_execute.py \
'        if result.get("tree_changed"):|||        if result.get("reason") == "the reproduction changed the tree under review":'
expect_fail restores_on_the_reason || true

echo
echo "=== mutant 16: a dirty worktree is accepted ==="
mutate accepts_a_dirty_worktree lib/review_execute.py \
'    if dirty.strip():|||    if False:'
expect_fail accepts_a_dirty_worktree || true

echo
echo "mutants killed: $pass, survived: $fail"
[ "$fail" -eq 0 ]
