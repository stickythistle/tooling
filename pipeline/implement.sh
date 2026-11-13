#!/usr/bin/env bash
# Implement the top bounded issue from the triage queue, verified, on a branch.
#
# Replaces the old updater cron, which wrote to updater-<date> from review notes
# with nothing checking that the result compiled. The gate here is the bench: no
# commit exists unless it passed, and no branch is pushed unless a commit does.
#
# Never opens a pull request and never touches main. A human reads the branch.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
LOCK=${PIPELINE_LOCK:-/tmp/agent-pipeline.lock}

# Every git command here names its repository. The agent this dispatches has a
# project folder bound to $REPO and has been observed creating its branch there
# rather than in the worktree it was given, so this script never adds to the
# confusion by leaving a shell sitting in $REPO.
git -C "$REPO" fetch --prune --quiet origin

# What the primary checkout was on before anything ran. Restored at exit: a
# checkout left on a feature branch makes every later "check main" read that
# branch instead, silently, and the next person to grep main gets an answer
# about someone else's work.
primary_head_before=$(git -C "$REPO" symbolic-ref --quiet --short HEAD || git -C "$REPO" rev-parse HEAD)

restore_primary() {
	local now
	now=$(git -C "$REPO" symbolic-ref --quiet --short HEAD || git -C "$REPO" rev-parse HEAD)
	if [ "$now" != "$primary_head_before" ]; then
		echo "WARNING: $REPO was left on $now; restoring $primary_head_before" >&2
		git -C "$REPO" checkout --quiet "$primary_head_before" || \
			echo "WARNING: could not restore $REPO to $primary_head_before" >&2
	fi
}
trap restore_primary EXIT

# What the bench can actually check. A chart or terraform change passes the
# whole gate without any of it having looked at the thing that changed -- the
# first autonomous run produced a correct Helm template that the bench approved
# on the strength of Rust still compiling. Until chart rendering is a CI job
# (#57), that work stays with a human.
PICK="$PIPELINE_DIR/pick_lane.py"
PROFILE=$("$PICK" --fallback fast)
# Finish what a previous night started, before starting anything new.
#
# A failed run leaves a branch, a worktree and no remote, and the selection loop
# below skips anything with either -- so a failed attempt left the queue for
# good. Naming every stranded tree costs no gate time, and not knowing they
# existed was the defect. A failure moves to the next tree: the first draft
# stopped on it, letting one unfixable tree hide the rest.
HERMES_BIN=$AGENT
STRANDED=()
for wt in "$WT_ROOT"/${BRANCH_PREFIX}*; do
	[ -d "$wt" ] || continue
	n=${wt##*/${BRANCH_PREFIX}}
	# Only trees this job created. The name and the branch say an issue is being
	# worked on, not by whom: a person's checkout matches the same glob, and the
	# repair below runs a --yolo agent in the tree and then `add -A` plus an
	# amend over whatever it finds. Adopting someone's working tree that way
	# rewrites their commit and opens a PR from it. The marker lives in the
	# worktree's gitdir rather than the tree, where `add -A` cannot sweep it in.
	[ -f "$(git -C "$wt" rev-parse --git-dir 2>/dev/null)/nightly-owned" ] || continue
	git -C "$REPO" rev-parse --verify --quiet "origin/${BRANCH_PREFIX}$n" >/dev/null && continue
	git -C "$wt" log --oneline origin/main..HEAD 2>/dev/null | grep -q . || continue
	# The same guard the selection loop below uses, and leaving it out here
	# opened a duplicate PR for work already on the trunk: the feature had
	# landed from another branch, so the patch ids differ and `git cherry`
	# still calls this tree unmerged. A closed issue is the reliable signal
	# that nobody wants this work any more.
	gh issue view "$n" --repo "$GH_REPO" --json state --jq .state 2>/dev/null | grep -q OPEN || continue
	STRANDED+=("$n")
done

if [ ${#STRANDED[@]} -gt 0 ]; then
	echo "stranded work, committed and unpushed: ${STRANDED[*]}"
fi

resumed=0
for n in "${STRANDED[@]:0:${PIPELINE_RESUME_MAX:-2}}"; do
	wt="$WT_ROOT/${BRANCH_PREFIX}$n"
	if ! $VERIFY "$wt" >"/tmp/nightly-resume-$n.log" 2>&1; then
		# Everything needed to fix it is already here: the tree, the commit,
		# and a log saying exactly what failed. #262 was one missing trailing
		# newline in a migration, held back for want of someone to add it.
		# One attempt, because a tree that cannot be repaired twice is a tree
		# that needs a person.
		echo "  issue $n: fails the gate; attempting repair from the log"
		# Only around this direct hermes call: hermes-implement below takes the
		# GPU lock itself, and holding it here too would deadlock.
		# The repair prompt is one argv string under `-z`, capped at 128 KiB;
		# it goes through a file the helper reads with `--query-file`, leaving
		# the run with the exact text the agent saw.
		REPAIR_PROMPT="The work in $wt is committed and fails the project's gate. Fix it there.

Every file you touch and every git command you run belongs to $wt. Pass git's
-C option and that path. Do not create, switch or push a branch, and do not
touch any other checkout.

The gate is:

    $VERIFY $wt

Run it, read what it reports, fix the cause, and run it again until it passes.
Then commit on the branch you are already on -- amend if the fix belongs to the
commit that is there, which it usually does.

Its output from the last run is below. Fix what it names and nothing else: this
is a repair, not a second attempt at the feature.

$(tail -60 "/tmp/nightly-resume-$n.log")"
		# The lane identity is configuration, and the repair agent
		# cannot start without it: `export_lane_identity_env` refuses
		# when the role binding cannot be read, and a repair that pins
		# no identity commits as nobody.
		#
		# The check is outside the subshell below, so the refusal skips
		# this lane and the run carries on to the next one. One lane
		# missing its configuration must not take the whole pipeline
		# down: under `set -e` a refusal inside the subshell ends it,
		# and a subshell that ends non-zero ends the script with it.
		#
		# `lane_expected_identity` resolves without exporting, so this
		# shell carries no pinned identity into the rest of the run.
		# The repository is named, because the binding may give it an
		# identity of its own.
		if ! lane_expected_identity "$GH_REPO" >/dev/null; then
			echo "  issue $n: no lane identity configured; skipping the repair" >&2
			continue
		fi
		REPAIR_PROMPT_FILE="/tmp/nightly-repair-$n.prompt"
		printf '%s' "$REPAIR_PROMPT" >"$REPAIR_PROMPT_FILE"
		(
			exec 9>"$LOCK"
			flock 9
			acquire_gpu_lock "$PROFILE"
			export_lane_build_env "$wt"
			# Pin the lane identity before the repair agent starts. The
			# env vars reach the agent via the timeout subshell; the
			# verifier before push is the backstop for one that overrode
			# them.
			#
			# The check that resolved the identity sits outside this
			# subshell: under `set -e` a refusal inside it ends the whole
			# script.
			export_lane_identity_env "$GH_REPO" || exit 1
			timeout "${PIPELINE_REPAIR_TIMEOUT:-3600}" "$BASH" -c '
				. "$1/lib/common.sh"
				shift
				hermes_query "$@" 8>&-
			' bash "$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" \
				"$HERMES_BIN" "$REPAIR_PROMPT_FILE" \
				"/tmp/nightly-repair-$n.stdout" "/tmp/nightly-repair-$n.stderr" \
				-p "$PROFILE" -- --no-restore-cwd --yolo >/dev/null 2>&1 || true
			exec 8>&-
		)

		# The agent fixes; committing the fix is mechanism, and it does not
		# reliably do it -- the first repair corrected the file and stopped,
		# leaving the gate to refuse a dirty tree and the fix to read as a
		# failure. Amend, because a repair belongs to the commit it repairs.
		if [ -n "$(git -C "$wt" status --porcelain)" ]; then
			echo "  issue $n: repair left $(git -C "$wt" status --porcelain | wc -l) path(s) uncommitted; amending"
			git -C "$wt" add -A
			git -C "$wt" commit --quiet --amend --no-edit
		fi
		if ! $VERIFY "$wt" >"/tmp/nightly-resume-$n.log" 2>&1; then
			echo "  issue $n: repair did not take; see /tmp/nightly-resume-$n.log"
			continue
		fi
		echo "  issue $n: repaired"
	fi
	# Re-author lane commits before pushing: a repair agent that
	# overrode the identity would otherwise push a commit pointing
	# at nobody. A refusal skips the push and the PR: pushing what
	# the verifier refused on would land the same wrong-author
	# commit on main. The base is the actual fork sha (merge-base
	# against origin/main), not the remote branch: this branch is
	# being pushed for the first time below, so `origin/<branch>`
	# does not exist yet, and a ref that fails to resolve makes the
	# verifier refuse with exit 3 (base unresolved) on every first push.
	#
	# Resume trees have no recorded base: the lane was created by an
	# earlier run that did not write one to disk, and we have no
	# other way to recover its fork. Merge-base is the best we can
	# do here; a resume tree that rewrote main's tip can still slip
	# through this check -- it is a known gap on the resume path.
	lane_base=$(git -C "$wt" merge-base origin/main HEAD) || {
		echo "  issue $n: could not resolve base for $wt" >&2
		continue
	}
	if ! verify_lane_identity "$wt" "$lane_base"; then
		echo "  issue $n: identity verifier refused; skipping push and PR" >&2
		continue
	fi
	git -C "$wt" push -q -u origin "${BRANCH_PREFIX}$n"
	pr=$(gh pr create --repo "$GH_REPO" --draft \
		--base main --head "${BRANCH_PREFIX}$n" \
		--title "$(git -C "$wt" log -1 --format=%s)" \
		--body "Closes #$n.

$(git -C "$wt" log -1 --format=%b)

Implemented by the nightly pipeline on an earlier run, held back by a failing
gate, and carried across once it passed. Nothing here has been reviewed by a
human." 2>&1 | tail -1) || { echo "  issue $n: pushed but could not open a PR: $pr" >&2; continue; }
	echo "  issue $n: earlier run's work now passes; draft $pr"
	resumed=$((resumed + 1))
	break
done
[ "$resumed" -gt 0 ] && exit 0


# Bounded issues only: triage flags anything needing a product decision, and an
# unattended run is the worst place to make one.
issue=""
for file in "$QUEUE"/issue-*.md; do
	[[ -e "$file" ]] || continue
	grep -q '^\*\*Verdict:\*\* still real' "$file" || continue
	n=$(basename "$file" .md); n=${n#issue-}
	gh issue view "$n" --json labels --jq '.labels[].name' 2>/dev/null |
		grep -qE "^($VERIFIABLE)$" || continue
	grep -qi '^bounded' <<<"$(sed -n '/^## Verdict on scope/,$p' "$file" | tail -n +2)" || continue
	# Skip anything already in flight or landed.
	git -C "$REPO" rev-parse --verify --quiet "origin/${BRANCH_PREFIX}$n" >/dev/null && continue
	git -C "$REPO" rev-parse --verify --quiet "${BRANCH_PREFIX}$n" >/dev/null && continue
	[[ -d "$WT_ROOT/${BRANCH_PREFIX}$n" ]] && continue
	gh issue view "$n" --json state --jq .state 2>/dev/null | grep -q OPEN || continue
	issue="$n"
	break
done

[[ -z "$issue" ]] && exit 0

branch="${BRANCH_PREFIX}$issue"
worktree="$WT_ROOT/$branch"
# A leftover branch from an interrupted night would make `add -b` fail, and
# `set -e` would end the run before the agent ever saw the issue. The selector
# above skips those, so reaching here with one means something raced: say so
# rather than dying on a git error message.
if ! git -C "$REPO" worktree add -q -b "$branch" "$worktree" origin/main 2>/tmp/nightly-wt-$issue.err; then
	echo "issue $issue: could not create worktree $worktree: $(tail -1 /tmp/nightly-wt-$issue.err)" >&2
	exit 0
fi
# Claims the tree for the resume scan above. Written after the add so a failed
# add leaves no claim, and inside the gitdir so a repair's `add -A` cannot
# stage it.
: >"$(git -C "$worktree" rev-parse --git-dir)/nightly-owned"
# The lane's fork point as the script sees it: the sha at worktree creation,
# not a merge-base computed later. A lane that amends main's tip in the
# worktree rewrites the ancestry so that `merge-base origin/main HEAD`
# returns the tip's parent, and the amended tip then looks like a lane
# commit the verifier is free to re-author. Recording the base here means
# the verifier is asked "is the recorded fork still reachable from HEAD?",
# and an amended tip breaks that. The resume path below has no such record
# (the tree it picks up is one an earlier run left behind) and stays
# on merge-base, with a comment noting why.
lane_base=$(git -C "$worktree" rev-parse HEAD)

verify="$VERIFY $worktree"
mechanisms=$(sed -n '/^## Candidate mechanisms/,/^## Verdict on scope/p' "$QUEUE/${BRANCH_PREFIX}$issue.md")

# The fast lane deliberately. This job takes only issues triage called bounded,
# and the root profile is now the dense 27B at roughly a third the speed -- an
# unattended run that has to finish before morning is the wrong place to spend
# that.
flock "$LOCK" ${PIPELINE_IMPLEMENTER:?set PIPELINE_IMPLEMENTER} \
	--profile "$PROFILE" \
	--issue "$issue" --worktree "$worktree" --verify "$verify" \
	--extra "${PIPELINE_AGENT_NOTES:-}

Triage has already costed the mechanisms below. Pick one and say in the handover which and why; if all of them are wrong, say that instead of inventing a fourth without explaining it.

$mechanisms" >/dev/null 2>&1 || true

# The branch has to have been built where it was told to build it. If the agent
# committed in $REPO instead, the worktree is empty and the primary checkout is
# sitting on the branch -- repair that here rather than leaving both wrong.
if [ "$(git -C "$REPO" symbolic-ref --quiet --short HEAD || true)" = "$branch" ]; then
	echo "issue $issue: agent worked in $REPO instead of $worktree; moving the checkout back" >&2
	git -C "$REPO" checkout --quiet "$primary_head_before"
fi

if ! git -C "$worktree" log --oneline origin/main..HEAD | grep -q .; then
	echo "issue $issue: no commit produced; worktree left at $worktree for inspection"
	exit 0
fi

if ! $VERIFY "$worktree" >/tmp/nightly-bench-$issue.log 2>&1; then
	echo "issue $issue: committed work fails the full bench; see /tmp/nightly-bench-$issue.log"
	exit 0
fi

# Re-author lane commits before pushing: the implementer pins env
# vars, this catches the agent that bypassed them. The rebase must
# leave the tree unchanged; if it doesn't, the verifier restores the
# old head and refuses. A refusal skips the push and the PR.
# The base is the lane's recorded fork (lane_base, set right after
# the `worktree add` above) rather than a fresh `merge-base origin/main
# HEAD`: a lane that amended main's tip in the worktree rewrites
# ancestry so merge-base returns the tip's parent, and the amended
# tip then looks like a lane commit the verifier is free to re-author.
# The recorded sha survives the rewrite and the verifier's ancestry
# check refuses instead.
verify_lane_identity "$worktree" "$lane_base" || {
	echo "issue $issue: identity verifier refused; skipping push and PR" >&2
	exit 0
}
git -C "$worktree" push -q -u origin "$branch"

# A branch nobody opens is where the last loop leaked: the work landed and the
# issue stayed open with nothing pointing at it. The PR is the thing that burns
# the issue down, and "Closes" is what makes merging do it rather than someone
# remembering. Draft, because the agent finished its half and a human has not
# started theirs.
pr=$(gh pr create --repo "$GH_REPO" --draft \
	--base main --head "$branch" \
	--title "$(git -C "$worktree" log -1 --format=%s)" \
	--body "Closes #$issue.

$(git -C "$worktree" log -1 --format=%b)

Implemented by the nightly pipeline from the triaged finding in $QUEUE/${BRANCH_PREFIX}$issue.md.
The gate is green on $(git -C "$worktree" rev-parse --short HEAD). Nothing here has been reviewed by a human." 2>&1 | tail -1) ||
	{ echo "issue $issue: pushed $branch but could not open a PR: $pr" >&2; exit 0; }
echo "issue $issue: $(git -C "$worktree" log -1 --format=%s) -> $branch, draft $pr (full bench green)"
