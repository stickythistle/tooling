#!/usr/bin/env bash
# Dispatch a diff to a Hermes profile for an independent review pass.
#
# The reviewer earns its round trip by being a different model family from
# whatever wrote the code, so its false negatives are uncorrelated with the
# author's. The tradeoff is a higher false positive rate, which the caller is
# expected to verify away.
set -euo pipefail

# readlink -f: the skill is installed as a symlink, and lib/ sits beside the
# real file, not the link.
LIB=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../../lib" && pwd)
# shellcheck source=../../../lib/common.sh
. "$LIB/common.sh"

HERMES=${HERMES_PYTHON:-${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python}
MODEL=""
BASE=""
PROFILE=""
TIMEOUT_OPT=""
EXTRA=""
JSON_OUT=""

usage() {
	cat <<'EOF'
usage: hermes-scrutinize.sh [--base REF] [--model MODEL] [--profile NAME]
                            [--extra TEXT] [--timeout SECONDS]
                            [--json FILE]

  --base REF   Review this branch against REF. Default: uncommitted changes if
               the tree is dirty, otherwise HEAD against the merge-base with
               the remote default branch.
  --model      Override the model Hermes reviews with.
  --profile    Hermes profile, which selects the model and the host it runs on.
               A hosted profile reviews with a model family unrelated to the
               local boxes, at the cost of sending the diff off the network.
  --extra      Text appended to the prompt: the change's intent and the product
               decisions behind it. Anything that narrows what to judge hides
               the defects outside it.
  --timeout    Seconds before the review is abandoned. Resolved per profile:
               the explicit value, then REVIEW_TIMEOUT_<PROFILE>, then
               HERMES_REVIEW_TIMEOUT, then 900.
  --json FILE  Also ask for the findings as a JSON array in FILE, one object
               per finding with severity, path, line, snippet, claim,
               settled_by and recommendation. The markdown findings are
               unchanged.

Prints the path to the findings file on stdout.
EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--base) BASE="$2"; shift 2 ;;
	--model) MODEL="$2"; shift 2 ;;
	--profile) PROFILE="$2"; shift 2 ;;
	--extra) EXTRA="$2"; shift 2 ;;
	--timeout) TIMEOUT_OPT="$2"; shift 2 ;;
	--json) JSON_OUT="$2"; shift 2 ;;
	-h | --help) usage; exit 0 ;;
	*) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
	esac
done

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

WORK=$(mktemp -d -t hermes-review-XXXXXX)
DIFF="$WORK/changes.diff"
FINDINGS="$WORK/FINDINGS.md"

# Written outside the repository: swe-reviewer treats any stray file in the
# workspace as a review failure, and a dirty tree would corrupt the next run.
if [[ -n "$BASE" ]]; then
	SCOPE="branch $(git rev-parse --abbrev-ref HEAD) against $BASE"
	git diff "$BASE"...HEAD >"$DIFF"
elif [[ -n "$(git status --porcelain)" ]]; then
	SCOPE="uncommitted working-tree changes"
	git diff HEAD >"$DIFF"
else
	DEFAULT_REF=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)
	SCOPE="branch $(git rev-parse --abbrev-ref HEAD) against $DEFAULT_REF"
	git diff "$DEFAULT_REF"...HEAD >"$DIFF"
fi

if [[ ! -s "$DIFF" ]]; then
	echo "no changes to review ($SCOPE)" >&2
	exit 1
fi

# The diff shares one context window with the source Hermes reads beside it.
DIFF_BYTES=$(wc -c <"$DIFF")
if [[ "$DIFF_BYTES" -gt 400000 ]]; then
	echo "diff is ${DIFF_BYTES} bytes; review it in smaller pieces with --base" >&2
	exit 1
fi

# --json asks for the same findings a second way: a JSON array for
# finding-check to locate. The snippet is the code quoted exactly as the
# file holds it, which is what the location and the blame work from.
JSON_ASK=""
if [[ -n "$JSON_OUT" ]]; then
	JSON_ASK="
Also write the same findings to $JSON_OUT as a JSON array, one object per
finding with exactly these fields: severity (high, medium or low), path,
line (the line of the snippet's first line in the file), snippet (the code
quoted, exactly as it appears in the file), claim, settled_by, and
recommendation."
fi

PROMPT=$(
	cat <<EOF
Review a proposed code change in the repository at $ROOT.

Scope: $SCOPE
The complete diff is at $DIFF. Read it first, then read the surrounding source
files in the repository for the context needed to judge each change.

Apply ONLY Phases 2, 3 and 4 of the swe-reviewer skill: static analysis, the
severity taxonomy, and the findings write-up. Skip Phases 1 and 5 entirely.

Hard constraints:
- Do not create, checkout, or delete any git branch.
- Do not stage, commit, push, or run any git command that writes.
- Do not modify, create, or delete any file inside $ROOT.
- Judge the change, not the repository: a defect that predates it and that it
  does not make worse is out of scope. What the change leaves out is not —
  a member of a set it half-covers, a consumer it never updated, and a rule its
  own doc comment states and its code contradicts are all defects of the change.

Write your findings to $FINDINGS using the swe-reviewer entry format: severity,
exact file path and line number, the offending snippet, and a concrete
Recommendation. If a change is correct, say so rather than inventing a finding.
$JSON_ASK
When the file is written, reply with one line: the count of findings per
severity class.
$EXTRA
EOF
)

PROMPT_FILE="$WORK/prompt.txt"
printf '%s' "$PROMPT" >"$PROMPT_FILE"

PROFILE_NAME="${PROFILE:-default}"
acquire_review_slot "$PROFILE_NAME"
acquire_gpu_lock "$PROFILE_NAME"
TIMEOUT=$(review_timeout_for "$PROFILE_NAME" "$TIMEOUT_OPT")

# The helper writes the prompt to a file under $WORK instead of as one
# argv string (-z). Anything over 128 KiB is otherwise unreachable; here
# the prompt is short but the path is uniform with the rest of the code.
# 8>&- 9>&-: a hermes child that holds these fds keeps the lock until it
# itself exits, long after this script has moved on (the lock spans one
# review only). timeout is the outermost wrapper because the helper is
# a sourced shell function not visible to a fresh `timeout` invocation.
set +e
HERMES_QUERY_ARGS=(-p "$PROFILE" -- --skills swe-reviewer --yolo)
[[ -n "$MODEL" ]] && HERMES_QUERY_ARGS=(-m "$MODEL" "${HERMES_QUERY_ARGS[@]}")
timeout "$TIMEOUT" "$BASH" -c '
	. "$1/lib/common.sh"
	shift
	hermes_query "$@" 8>&- 9>&-
' bash "$LIB/.." \
	"$HERMES -m hermes_cli.main" "$PROMPT_FILE" \
	"$WORK/stdout.txt" "$WORK/stderr.txt" \
	"${HERMES_QUERY_ARGS[@]}"
RC=$?
# The 8>&- 9>&- above closed both for the child only; release them here too.
exec 9>&-
exec 8>&-
set -e

if [[ "$RC" -ne 0 ]]; then
	echo "hermes exited $RC (timeout was ${TIMEOUT}s)" >&2
	tail -20 "$WORK/stderr.txt" >&2
	exit "$RC"
fi

if [[ ! -s "$FINDINGS" ]]; then
	# Hermes answered but never wrote the file — its reply is all we have.
	echo "hermes wrote no findings file; its reply follows" >&2
	cat "$WORK/stdout.txt" >&2
	exit 1
fi

echo "scope: $SCOPE" >&2
cat "$WORK/stdout.txt" >&2
echo "$FINDINGS"
