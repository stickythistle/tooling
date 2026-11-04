#!/usr/bin/env bash
# Phase 1 of the nightly pipeline: find things worth fixing.
#
# "Has anything changed?" is a SHA comparison, and leaving it to the agent cost
# five duplicate reviews in fourteen, three of them consecutive nights on
# 9bd846a.
#
# It compares against the state file rather than the notes' `Reviewed commit:`
# line: three reviews omitted that line and the rest used two formats for it.
# Control flow that parses model output inherits model variance.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

WORK=${PIPELINE_REVIEW_WORK:?set PIPELINE_REVIEW_WORK to a checkout the reviewer may reset}
STATE=$STATE_DIR/last-reviewed
HERMES=$AGENT
# Falls back to the local lane when the metered one is close to its ceiling.
PICK="$PIPELINE_DIR/pick_lane.py"
PROFILE=$("$PICK" --fallback default)
mkdir -p "$(dirname "$STATE")"

git -C "$WORK" fetch --prune --quiet origin
DEFAULT=$(git -C "$WORK" symbolic-ref --quiet --short refs/remotes/origin/HEAD | sed 's|^origin/||')
DEFAULT=${DEFAULT:-main}

# A run that starts on yesterday's review branch, with three stale clones the
# previous runs left behind, is a run that improvises. It improvised by cloning
# the repository into its own working directory, three separate times. Start
# every run from the same known state instead.
git -C "$WORK" checkout --quiet "$DEFAULT"
git -C "$WORK" reset --quiet --hard "origin/$DEFAULT"
# -ff, not -f: the runs that improvised left *clones* behind, and plain clean
# refuses to remove a nested git repository. Three of them survived a -fd.
git -C "$WORK" clean --quiet -ffd

HEAD_SHA=$(git -C "$WORK" rev-parse HEAD)
LAST_SHA=$(cat "$STATE" 2>/dev/null || true)

# The common case, and the one the model must never be woken for.
if [ "$HEAD_SHA" = "$LAST_SHA" ]; then
	exit 0
fi

if [ -n "$LAST_SHA" ] && git -C "$WORK" cat-file -e "$LAST_SHA^{commit}" 2>/dev/null; then
	RANGE="$LAST_SHA..$HEAD_SHA"
	SCOPE="the $(git -C "$WORK" rev-list --count "$RANGE") commit(s) in $RANGE"
	CHANGED=$(git -C "$WORK" diff --name-only "$RANGE")
else
	RANGE=""
	SCOPE="the whole repository (no previous review to diff against)"
	CHANGED=$(git -C "$WORK" ls-files)
fi

BRANCH="reviewer-$(date +%F)"
PREV_NOTES=""
# The newest review branch, not yesterday's: a review only happens on a day the
# trunk moved, so yesterday usually does not exist. Asking for it by date drops
# the history silently and the review reads as though nothing was ever found.
PREV_BRANCH=$(git -C "$WORK" for-each-ref --format='%(refname:short)' --sort=-refname 'refs/remotes/origin/reviewer-*' | head -1)
[ -n "$PREV_BRANCH" ] && PREV_NOTES=$(git -C "$WORK" show "$PREV_BRANCH:REVIEW_NOTES.md" 2>/dev/null || true)

git -C "$WORK" checkout --quiet -B "$BRANCH"

# The prompt is one argv string, and Linux caps one at 128 KiB. The previous
# review only grows, and a whole-repository scope lists every file, so both go
# in files the prompt names. Outside $WORK, which the run resets and cleans.
CTX=$(mktemp -d -t review-ctx-XXXXXX)
AGENT_OUT=$(mktemp -t review-agent-XXXXXX.log)
trap 'exec 8>&-; rm -rf "$AGENT_OUT" "$CTX"' EXIT
printf '%s\n' "$CHANGED" >"$CTX/changed-files.txt"
if [ -n "$PREV_NOTES" ]; then printf '%s\n' "$PREV_NOTES" >"$CTX/previous-review.md"; fi

PROMPT="Review the repository at $WORK and write REVIEW_NOTES.md at its root.

The branch, the commit and the push are already handled: you are on $BRANCH and
the only thing you produce is that one file. Do not run git checkout, git
commit, git push, or git branch. Do not clone anything.

Scope: $SCOPE.
Reviewed commit: $HEAD_SHA

Files touched since the last review, one per line: $CTX/changed-files.txt

Follow the swe-reviewer skill for what to look for, how to classify, and the
shape of the file. Every finding needs a precise path:line, because the next
phase re-checks each one against the tree and files an issue for the ones that
survive -- a finding it cannot locate is a finding it will discard.

$(if [ -n "$PREV_NOTES" ]; then printf 'The previous review is in %s. Carry every one of its findings forward\nverbatim, including the ones you believe are fixed.\n\nDo not re-check them against the tree. The next phase verifies every finding\nand files issues from its verdicts, so checking here is work done twice by two\nmodels that can disagree -- and the one that disagrees here is the one nobody\nreads. Dropping a finding you judge fixed is how a real one disappears without\nanything recording the decision.' "$CTX/previous-review.md"; fi)"

# The agent prompt is itself one argv string under `-z`, capped at 128 KiB.
# The previous review and the changed-file list already travel as files,
# so the prompt itself does too: a `--query-file PATH` is uniform and
# leaves the run with a record of exactly what was asked. A file, not
# stdin: keeping the prompt as a file after the run gives the next phase
# the same shape for "what did the previous run see".
PROMPT_FILE="$CTX/prompt.txt"
printf '%s' "$PROMPT" >"$PROMPT_FILE"

# The GPU group lock is taken in addition to the review's own constraints:
# this phase runs hermes, and hermes holds the GPUs. The wait sits outside
# the timeout, so a swap queued behind a reviewer cannot drain the timeout.
acquire_gpu_lock "$PROFILE"

# The reviewer's own skill, checked against the repository before the agent
# is woken. A merged change to a skill never reaches the profile's tree
# unless something installs it, and the reviewer would then review under
# rules the repository has replaced.
#
# The refusal is the same shape as the unlinked-PR refusal: the phase stops
# before the agent starts, naming the skill that differs.
if ! check_skill_versions "${SKILLS_REPO:-$PIPELINE_DIR/..}" "$PROFILE" swe-reviewer; then
	echo "review $HEAD_SHA: refusing to start; swe-reviewer is not the repository's copy (run hermes-skills-sync)" >&2
	exit 1
fi

# timeout wraps the call. PIPELINE_AGENT is the generic name the phases
# share, and the helper takes the agent command as $1 so the same path
# serves any agent the operator chooses.
timeout "${PIPELINE_REVIEW_TIMEOUT:-5400}" "$BASH" -c '
	. "$1/lib/common.sh"
	shift
	hermes_query "$@" 8>&- 9>&-
' bash "$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" \
	"$HERMES" "$PROMPT_FILE" \
	"$AGENT_OUT" "$CTX/agent.stderr" \
	-p "$PROFILE" -- \
	--no-restore-cwd --skills swe-reviewer --yolo || true

if [ ! -s "$WORK/REVIEW_NOTES.md" ]; then
	# stdout, per common.sh: the job runs --no-agent, so stderr is dropped on
	# delivery and a diagnostic written there reaches nobody.
	echo "review $HEAD_SHA: no REVIEW_NOTES.md produced; branch $BRANCH left unpushed"
	# The agent names its own refusal on the way out -- an unknown skill, a
	# quota wall, a timeout. Without it the operator has only the absence of a
	# file, which is the same symptom for every one of those causes.
	echo "last 20 lines of the agent run:"
	tail -n 20 "$AGENT_OUT"
	exit 1
fi

# Only that one file, whatever else the run touched.
git -C "$WORK" add REVIEW_NOTES.md
git -C "$WORK" checkout --quiet -- . 2>/dev/null || true
git -C "$WORK" clean --quiet -ffd
if git -C "$WORK" diff --cached --quiet; then
	echo "review $HEAD_SHA: REVIEW_NOTES.md unchanged from the last review; nothing pushed" >&2
	exit 0
fi

git -C "$WORK" commit --quiet -m "review: $(date +%F)"
git -C "$WORK" push --quiet -u origin "$BRANCH"

# Last, so a failure anywhere above leaves the next run willing to retry this
# commit rather than skipping it as already reviewed.
printf '%s\n' "$HEAD_SHA" > "$STATE"
echo "review $HEAD_SHA: $(grep -ciE '^###? ' "$WORK/REVIEW_NOTES.md") finding(s) pushed to $BRANCH"
