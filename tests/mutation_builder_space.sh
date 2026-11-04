#!/usr/bin/env bash
# Mutation sweep for the builder's disk guard.
#
# One mechanism per mutation, each recorded separately, so the handover can
# say which mechanism is doing the work. The mutated copy lives in a scratch
# directory, never in the working tree: a half-reverted tree is how a mutant
# gets committed.
set -euo pipefail

REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
SCRATCH=$(mktemp -d -t tooling-136-mutants-XXXXXX)
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
# Every occurrence, not just the first: a threshold that appears in
# three places has to be wrong in all three for the mutation to mean
# what it says.
count = text.count(old)
if count == 0:
    sys.exit(3)
open(path, "w").write(text.replace(old, new))
print(f"  (replaced {count} occurrence(s))")
PY
}

expect_fail() { # name
	local name="$1"
	local out
	if out=$(cd "$SCRATCH/$name" && timeout 400 python3 tests/test_builder_space.py 2>&1); then
		echo "MUTANT SURVIVED: $name (the suite still passed)"
		fail=$((fail + 1))
		return 1
	fi
	echo "MUTANT KILLED: $name"
	echo "$out" | grep -E "^FAIL" | head -3 | sed 's/^/    /'
	pass=$((pass + 1))
	return 0
}

echo "=== mutant 1: the check ignores the space the build needs ==="
mutate ignores_need bin/builder-space \
'if awk "BEGIN{exit !(($free - $need_gb) < $min_free_gb)}"; then|||if awk "BEGIN{exit !($free < $min_free_gb)}"; then'
expect_fail ignores_need || true

echo
echo "=== mutant 2: a full builder is allowed to build ==="
mutate allows_full_builder bin/builder-space \
'	report "DISK_LOW $free"
	exit 4|||	report "OK $free"
	exit 0'
expect_fail allows_full_builder || true

echo
echo "=== mutant 3: the lane loop measures one lane ==="
mutate one_lane bin/remote-prune \
'		prune_host "$lane" || rc=$?|||		prune_host "" || rc=$?'
expect_fail one_lane || true

echo
echo "=== mutant 4: the _prove copy is not paired with its tree ==="
mutate no_prove_pairing bin/remote-prune \
'		look="$base"|||		look="$name"'
expect_fail no_prove_pairing || true

echo
echo "=== mutant 5: the landing path removes only the tree, not its prove copy ==="
mutate landing_without_prove bin/remote-prune \
'		local want=("$TREE" "${TREE}_prove")|||		local want=("$TREE")'
expect_fail landing_without_prove || true

echo
echo "=== mutant 6: the landing path prunes one lane only ==="
mutate landing_one_lane bin/remote-prune \
'if [ "$ALL_LANES" -eq 1 ] || [ -n "$TREE" ]; then|||if [ "$ALL_LANES" -eq 1 ]; then'
expect_fail landing_one_lane || true

echo
echo "=== mutant 7: remote-task does not run the check before the sync ==="
mutate no_check_before_sync bin/remote-task \
'set +e
"$BIN_DIR/builder-space" "${SPACE_ARGS[@]}"
space_rc=$?
set -e
if [ "$space_rc" -ne 0 ]; then|||set +e
space_rc=0
set -e
if [ "$space_rc" -ne 0 ]; then'
expect_fail no_check_before_sync || true

echo
echo "=== mutant 8: the disk-low code is a build failure instead ==="
mutate disk_low_is_a_build_failure bin/remote-task \
'	FAIL_REASON="builder disk low on $REMOTE (exit $space_rc)"
	echo "remote-task: $FAIL_REASON" >&2
	exit 4|||	FAIL_REASON="builder disk low on $REMOTE (exit $space_rc)"
	echo "remote-task: $FAIL_REASON" >&2
	exit 1'
expect_fail disk_low_is_a_build_failure || true

echo
echo "mutants killed: $pass, survived: $fail"
[ "$fail" -eq 0 ]
