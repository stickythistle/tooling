#!/usr/bin/env bash
# Mutation sweep for the condense pass's protection of skill files.
#
# One mechanism per mutation, each recorded separately, so the handover can
# say which mechanism is doing the work. The mutated copy lives in a scratch
# directory, never in the working tree.
set -euo pipefail

REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
SCRATCH=$(mktemp -d -t tooling-153-mutants-XXXXXX)
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
open(path, "w").write(text.replace(old, new))
PY
}

expect_fail() { # name
	local name="$1"
	local out
	if out=$(cd "$SCRATCH/$name" && timeout 300 python3 tests/test_condense_skill_protection.py 2>&1); then
		echo "MUTANT SURVIVED: $name (the suite still passed)"
		fail=$((fail + 1))
		return 1
	fi
	echo "MUTANT KILLED: $name"
	echo "$out" | grep -E "^FAIL" | head -3 | sed 's/^/    /'
	pass=$((pass + 1))
	return 0
}

echo "=== mutant 1: the finder stops marking a skill file's blocks ==="
mutate no_skill_finding skills/condense-prose/find_prose.py \
'        for block in found:
            if in_skill:
                block.context.append("skill-file")|||        for block in found:'
expect_fail no_skill_finding || true

echo
echo "=== mutant 2: the rule moves to the bottom of the skill ==="
mutate rule_at_the_bottom skills/condense-prose/SKILL.md \
'## What cannot be cut|||## Cut beyond what the finder sees'
expect_fail rule_at_the_bottom || true

echo
echo "=== mutant 3: the notice stops naming the four things ==="
mutate notice_vague skills/condense-prose/find_prose.py \
'            "  An instruction, a rule'"'"'s example, a trigger phrase in the "
            "description, or a negation cannot be cut or reworded."|||            "  Something was flagged here."'
expect_fail notice_vague || true

echo
echo "=== mutant 4: the before/after report is dropped ==="
mutate no_before_after skills/condense-prose/SKILL.md \
'**For every block you changed, report the sentence before and the sentence
after.**|||**Report nothing.**'
expect_fail no_before_after || true

echo
echo "=== mutant 5: the kept blocks are no longer reported ==="
mutate no_kept_blocks skills/condense-prose/SKILL.md \
'**Report the blocks you kept, too, and say why.**|||**Report the changed blocks.**'
expect_fail no_kept_blocks || true

echo
echo "=== mutant 6: the coordinator's finishing order drops the check ==="
mutate order_drops_check skills/pr-review-coordinator/SKILL.md \
'5. The coordinator checks the condense pass for lost instructions and reversed
   negations, confirms the change since its last review is prose only, then
   approves at that sha.|||5. The coordinator confirms the change since its last review is prose only,
   then approves at that sha.'
expect_fail order_drops_check || true

echo
echo "=== mutant 7: the coordinator checks after the approval ==="
mutate check_after_approval skills/pr-review-coordinator/SKILL.md \
'5. The coordinator checks the condense pass for lost instructions and reversed
   negations, confirms the change since its last review is prose only, then
   approves at that sha.|||5. The coordinator confirms the change since its last review is prose only,
   then approves at that sha, then checks the condense pass for lost
   instructions and reversed negations.'
expect_fail check_after_approval || true

echo
echo "=== mutant 8: the skill-file mark becomes a finding again ==="
# The mark is context. A finding that stands alone refuses every PR that
# touches a skill file, so the count has to ignore it.
mutate mark_is_a_finding skills/condense-prose/find_prose.py \
'                block.context.append("skill-file")|||                block.findings.append("skill-file")'
expect_fail mark_is_a_finding || true

echo
echo "mutants killed: $pass, survived: $fail"
[ "$fail" -eq 0 ]
