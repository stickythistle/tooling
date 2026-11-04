#!/usr/bin/env bash
# Mutation sweep for the credential-free lane and the per-role credentials.
#
# One mechanism per mutation, each recorded separately, so the handover can
# say which mechanism is doing the work. The specs live in
# tests/mutation_specs.py, because they carry tabs and quotes and a shell
# single-quoted string passes every backslash literally.
#
# The mutated copy is a detached worktree, never the working tree: a
# half-reverted tree is how a mutant gets committed. A plain `cp -a` is not
# a copy for this purpose -- it keeps the .git pointer file, so git
# commands in the copy would act on the real worktree's HEAD and index.
#
# Every mutation replaces a snippet with another of the same shape, so the
# mutated file stays syntactically valid and exactly one mechanism breaks.
# A mutant that only breaks the parse is not a kill.
set -euo pipefail

REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
SPECS="$REPO/tests/mutation_specs.py"
SCRATCH=$(mktemp -d -t tooling-129-mutants-XXXXXX)

# The scratch tree holds detached worktrees, and `rm -rf` on the directory
# leaves the registrations behind. Every one is removed by its path, which
# is the only way that also drops the registration.
cleanup() {
	local d
	for d in "$SCRATCH"/*/; do
		[ -d "$d" ] || continue
		git -C "$REPO" worktree remove --force "${d%/}" >/dev/null 2>&1 || true
	done
	rm -rf "$SCRATCH"
}
trap cleanup EXIT

pass=0
fail=0

mutate() { # name
	local name="$1"
	local copy="$SCRATCH/$name"
	rm -rf "$copy"
	# A detached worktree at HEAD, so the copy has its own index and HEAD.
	git -C "$REPO" worktree add --detach "$copy" HEAD >/dev/null 2>&1
	python3 - "$SPECS" "$copy" "$name" <<'PY'
import importlib.util
import subprocess
import sys

specs_path, copy, name = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("mutation_specs", specs_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for spec_name, file, old, new in mod.SPECS:
    if spec_name != name:
        continue
    path = f"{copy}/{file}"
    text = open(path).read()
    if old not in text:
        print(f"MUTATION TARGET NOT FOUND in {path}: {old!r}", file=sys.stderr)
        sys.exit(3)
    open(path, "w").write(text.replace(old, new, 1))
    sys.exit(0)
print(f"NO SUCH SPEC: {name}", file=sys.stderr)
sys.exit(3)
PY
	# A mutant that only breaks the parse is not a kill: the file has to
	# stay valid, so the suite fails on an assertion and not on a syntax
	# error.
	local file
	file=$(python3 - "$SPECS" "$name" <<'PY'
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("mutation_specs", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for spec_name, file, old, new in mod.SPECS:
    if spec_name == sys.argv[2]:
        print(file)
        break
PY
)
	if ! bash -n "$copy/$file" 2>/dev/null; then
		echo "MUTANT IS INVALID: $name ($file does not parse)"
		exit 3
	fi
}

expect_fail() { # name
	local name="$1"
	local out
	if out=$(cd "$SCRATCH/$name" && timeout 400 python3 tests/test_credential_free_lanes.py 2>&1); then
		echo "MUTANT SURVIVED: $name (the suite still passed)"
		fail=$((fail + 1))
		return 1
	fi
	echo "MUTANT KILLED: $name"
	echo "$out" | grep -E "^FAIL" | head -3 | sed 's/^/    /'
	pass=$((pass + 1))
	return 0
}

run_mutant() { # name description
	mutate "$1"
	expect_fail "$1" || true
}

echo "=== mutant 1: gh_as runs the call even with no usable auth ==="
run_mutant gh_as_falls_through

echo
echo "=== mutant 2: gh_as accepts the reviewer role again ==="
run_mutant gh_as_any_role

echo
echo "=== mutant 3: gh_as puts the token on the command line ==="
run_mutant token_on_argv

echo
echo "=== mutant 4: the lane keeps the parent's token ==="
run_mutant keeps_token

echo
echo "=== mutant 5: the lane keeps the parent's gh config dir ==="
run_mutant keeps_config_dir

echo
echo "=== mutant 6: the lane's gh config dir is not emptied ==="
run_mutant config_dir_not_emptied

echo
echo "=== mutant 7: the lane runs in the script's own process group ==="
run_mutant shared_process_group

echo
echo "=== mutant 8: the prompt does not say the arrangement is deliberate ==="
run_mutant silent_lane

echo
echo "=== mutant 9: the prompt does not say the lane must not use credentials it can reach ==="
run_mutant reachable_credentials

echo
echo "=== mutant 10: the prompt still claims the lane has no credentials ==="
run_mutant claims_no_credentials

echo
echo "=== mutant 11: the script reads the issue as the ambient account ==="
run_mutant ambient_issue_read

echo
echo "=== mutant 12: gh_as does not check the login it would act as ==="
run_mutant no_login_check

echo
echo "=== mutant 13: the login cache is keyed by the scope, not the credential ==="
run_mutant scope_keyed_cache

echo
echo "=== mutant 14: the login cache lives in a shared path ==="
run_mutant shared_cache_path

echo
echo "mutants killed: $pass, survived: $fail"
[ "$fail" -eq 0 ]
