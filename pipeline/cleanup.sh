#!/usr/bin/env bash
# Midday: clear out what landed, escalate what is stuck.
#
# Worktrees reached 71 and branches about 90, and a worktree makes its issue
# permanently ineligible, so that backlog was draining the queue the implement
# phase picks from. Clearing merged work is what keeps the loop able to pick up
# work at all.
#
# Runs late morning, after the night's work rather than racing it.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

STALE_DAYS=${PIPELINE_STALE_DAYS:-3}

git -C "$REPO" fetch --prune --quiet origin

removed=() stranded=() stale=() dirty=()

for wt in "$WT_ROOT"/${BRANCH_PREFIX}*; do
	[ -d "$wt" ] || continue
	n=${wt##*/${BRANCH_PREFIX}}
	branch="${BRANCH_PREFIX}$n"

	# Uncommitted work is never this job's to discard. Two trees held the only
	# copy of an experiment when this was written.
	if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
		dirty+=("$n")
		continue
	fi

	# Merged by content, not by ancestry: a squash merge lands the same patch
	# under a new sha, so an ancestry test calls landed work unmerged and keeps
	# it forever. `git cherry` compares patch ids, which is the question being
	# asked here.
	if [ -z "$(git -C "$wt" cherry origin/main HEAD 2>/dev/null | grep '^+')" ]; then
		git -C "$REPO" worktree remove "$wt" 2>/dev/null || continue
		git -C "$REPO" branch -D "$branch" >/dev/null 2>&1 || true
		git -C "$REPO" push --quiet origin --delete "$branch" 2>/dev/null || true
		removed+=("$n")
		continue
	fi

	if ! git -C "$REPO" rev-parse --verify --quiet "origin/$branch" >/dev/null; then
		age=$(( ( $(date +%s) - $(git -C "$wt" log -1 --format=%ct) ) / 86400 ))
		if [ "$age" -ge "$STALE_DAYS" ]; then
			stale+=("$n (${age}d)")
		else
			stranded+=("$n (${age}d)")
		fi
	fi
done

# A triage entry for a closed issue is work the implement phase will consider
# and discard on every run, forever.
pruned=0
for f in "$QUEUE"/issue-*.md; do
	[ -e "$f" ] || continue
	n=$(basename "$f" .md); n=${n#issue-}
	state=$(gh issue view "$n" --repo "$GH_REPO" --json state --jq .state 2>/dev/null || true)
	if [ "$state" = "CLOSED" ]; then
		rm -f "$f"
		pruned=$((pruned + 1))
	fi
done

[ ${#removed[@]} -gt 0 ] && echo "merged and cleared: ${removed[*]}"
[ "$pruned" -gt 0 ] && echo "queue entries pruned for closed issues: $pruned"
[ ${#stranded[@]} -gt 0 ] && echo "stranded, still fresh: ${stranded[*]}"
[ ${#dirty[@]} -gt 0 ] && echo "left alone, uncommitted changes: ${dirty[*]}"

# Did each phase actually do its job last night?
#
# The trap in common.sh catches a phase that dies loudly; this catches one that
# dies quietly or never starts. Six nights of every job dying on line 11 looked
# exactly like six quiet nights.
#
# Each check asks whether a phase left the artifact the next phase reads, never
# whether it "ran" -- the question cron already answers wrongly.
broken=()

STATE=$STATE_DIR/last-reviewed
head_sha=$(git -C "$REPO" rev-parse origin/main)
last_sha=$(cat "$STATE" 2>/dev/null || true)
if [ "$head_sha" != "$last_sha" ]; then
	# Legitimate for a few hours: main moved and review runs at 02:00. Past a
	# day it means review is not keeping up with the trunk.
	age=$(( ( $(date +%s) - $(git -C "$REPO" log -1 --format=%ct origin/main) ) / 3600 ))
	[ "$age" -gt 25 ] && broken+=("review has not covered origin/main ($(git -C "$REPO" rev-parse --short origin/main), ${age}h old)")
fi

# An agent update re-bundles each profile's skills tree and drops the local
# skills the phases dispatch by name, so a lane can stop working without any
# phase having run to notice. Checked here because this is the job that already
# asks whether the pipeline is still delivering.
if ! skills_missing=$("$PIPELINE_DIR/../bin/hermes-skills-sync" --check 2>&1); then
	broken+=("$(printf '%s' "$skills_missing" | tail -1); run hermes-skills-sync")
fi

newest_review=$(git -C "$REPO" for-each-ref --format='%(refname:short)' --sort=-refname 'refs/remotes/origin/reviewer-*' | head -1)
if [ -n "$newest_review" ]; then
	d=${newest_review##*reviewer-}
	[ -s "$QUEUE/review-$d.md" ] || broken+=("triage has not processed $newest_review")
fi

if [ ${#broken[@]} -gt 0 ]; then
	printf 'PHASE NOT DELIVERING: %s\n' "${broken[@]}"
fi

# The remote half of what this job already does locally; a bench directory
# outlives the worktree it mirrors and nothing else asks.
#
# --all-lanes: the lanes are separate hosts with separate benches, and a
# prune that reached only HOST_BUILDER left the other lanes filling up.
# That is how six gates failed out of disk across three lanes while one
# lane looked fine.
# Its exit code cannot end this run: the builder can be down, which says nothing
# about the worktrees this job cleaned.
if command -v remote-prune >/dev/null 2>&1; then
	prune_out=$(remote-prune --prune --all-lanes "$REPO" 2>&1) || true
	[ -n "$prune_out" ] && printf '%s\n' "$prune_out"
else
	echo "remote-prune not on PATH; the builder's bench was not pruned"
fi

# The escalation. Everything above is housekeeping; this is the line that says
# work has been sitting long enough that nobody is coming for it.
if [ ${#stale[@]} -gt 0 ]; then
	echo "STRANDED ${STALE_DAYS}+ DAYS, needs a human: ${stale[*]}"
fi
exit 0
