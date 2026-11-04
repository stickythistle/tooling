# Sourced by every tool in bin/.

# Sourced here rather than left to the shell profile, so cron jobs and agent
# subprocesses get the values too.
ENV_FILE=${TOOLS_ENV:-$HOME/.config/agent-tools/env}
# An if, not `[ -r ] &&`: under `set -e` the && chain returns 1 when the file is
# absent and takes the whole script with it.
if [ -r "$ENV_FILE" ]; then
	# shellcheck source=/dev/null
	. "$ENV_FILE"
fi
# A lane and every tool it runs must find the same records from whatever
# directory each one runs in, which only an absolute path guarantees.
case "${AGENT_STATE_DIR:-/}" in
/*) ;;
*)
	echo "AGENT_STATE_DIR must be an absolute path, not '$AGENT_STATE_DIR'" >&2
	exit 2
	;;
esac

# -P: bin/ is reached through a symlink, and a logical cd resolves ../lib
# against the link and lands outside the checkout.
TOOLS_LIB=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# ----- skills -----------------------------------------------------------
#
# A lane dispatches skills by name, and the agent resolves each name from
# the profile's own tree. Two copies of a skill are compared by the content
# of the skill directory.

# The `version:` field of a skill file, or empty when there is none. It
# labels a skill for a human. It never decides whether two copies match.
skill_version() {
	local file="$1"
	[ -r "$file" ] || return 0
	sed -n 's/^version:[[:space:]]*//p' "$file" | head -1 | tr -d '"'"'"' \r'
}

# The digest of a skill directory: the hash of every file under it, by
# path. File names are part of the digest because a skill's references and
# scripts are part of what the lane loads.
skill_digest() {
	local dir="$1"
	[ -d "$dir" ] || return 0
	(
		cd "$dir" || exit 1
		find . -type f ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 \
			| LC_ALL=C sort -z \
			| xargs -0 -r sha256sum
	) | sha256sum | cut -d' ' -f1
}

# The root home of the agent: the directory that holds `skills/` and
# `profiles/`. The layout is:
#   <root>/skills                    the root tree
#   <root>/profiles/<p>/skills       a profile's tree
#
# HERMES_HOME names whichever home is active.
hermes_root_home() {
	local hh="${HERMES_HOME:-}"
	if [ -z "$hh" ]; then
		printf '%s\n' "$HOME/.hermes"
		return 0
	fi
	if [ -d "$hh/skills" ]; then
		# HERMES_HOME is the root home itself: it holds the skills tree
		# every profile inherits from. Checked before the profiles
		# directory, because a home can hold both.
		printf '%s\n' "$hh"
		return 0
	fi
	if [ -d "$hh/profiles" ]; then
		printf '%s\n' "$hh"
		return 0
	fi
	# HERMES_HOME is a profile home: <root>/profiles/<p>.
	printf '%s\n' "$(dirname "$(dirname "$hh")")"
}

# The skills tree of a profile, or of the root tree when the profile is
# empty.
skills_tree() {
	local profile="$1"
	if [ -z "$profile" ]; then
		printf '%s/skills\n' "$(hermes_root_home)"
		return 0
	fi
	# The profile's own home, when HERMES_HOME is it.
	local hh="${HERMES_HOME:-}"
	if [ -n "$hh" ] && [ "$(basename "$hh")" = "$profile" ] && [ ! -d "$hh/profiles" ] && [ -d "$hh/skills" ]; then
		printf '%s/skills\n' "$hh"
		return 0
	fi
	printf '%s/profiles/%s/skills\n' "$(hermes_root_home)" "$profile"
}

# The directory a tree keeps a skill in, or empty when the tree carries
# no copy.
#
# The repository keeps `skills/<name>/`. Agent trees keep
# `skills/<category>/<name>/`. The search walks the whole tree.
#
# A live copy wins over an archived one. The archived copy sorts first,
# so taking the first match would aim the check and the sync at it and
# leave the live one stale. The archive is used only when it is the tree's
# sole copy.
find_skill_dir() {
	local tree="$1" name="$2" hit archived=""
	[ -d "$tree" ] || return 0
	# `find`, not a glob. A glob skips dot-prefixed entries. `.archive/`
	# is where an operator files a skill.
	while IFS= read -r hit; do
		[ -r "$hit/SKILL.md" ] || continue
		case "$hit" in
		*/.archive/*) [ -n "$archived" ] || archived="$hit" ;;
		*) printf '%s\n' "$hit"; return 0 ;;
		esac
	done < <(find "$tree" -mindepth 1 -maxdepth 3 -type d -name "$name" 2>/dev/null | LC_ALL=C sort)
	[ -n "$archived" ] && printf '%s\n' "$archived"
	return 0
}

# The path of a skill's SKILL.md inside a tree, or empty when the tree
# carries no such skill.
find_skill_file() {
	local tree="$1" name="$2" dir
	dir=$(find_skill_dir "$tree" "$name")
	[ -n "$dir" ] || return 0
	printf '%s/SKILL.md\n' "$dir"
}

# The version of a skill in a tree, or empty when the tree carries no
# such skill or the skill carries no version.
tree_skill_version() {
	local tree="$1" name="$2" file
	file=$(find_skill_file "$tree" "$name")
	[ -n "$file" ] || return 0
	skill_version "$file"
}

# The version of a skill in the repository, or empty when the repository
# ships it without a version.
repo_skill_version() {
	local repo="$1" name="$2"
	tree_skill_version "$repo/skills" "$name"
}

# `check_skill_versions <repo> <profile> <name...>`: refuse when a
# dispatched skill's installed copy differs from the repository's copy.
#
# A mismatch stops the run before it starts, naming the skill and what
# differs. A copy the tree never carried is a mismatch too: the lane
# dispatches a skill the agent cannot load. A skill the repository leaves
# out is left alone.
#
# The names arrive as one comma-separated list. The agent splits it and
# trims each name. The check trims and drops empties the same way.
#
# Prints one line per mismatch and returns 1 when there was one.
check_skill_versions() {
	local repo="$1" profile="$2"
	shift 2
	local name repo_dir tree_dir repo_digest tree_digest tree
	local failed=0
	tree=$(skills_tree "$profile")
	for name in "$@"; do
		name=$(printf '%s' "$name" | tr -d '[:space:]')
		[ -n "$name" ] || continue
		repo_dir=$(find_skill_dir "$repo/skills" "$name")
		# The repository ships no such skill. There is nothing to be out
		# of date with.
		[ -n "$repo_dir" ] || continue
		tree_dir=$(find_skill_dir "$tree" "$name")
		if [ -z "$tree_dir" ]; then
			echo "skill '$name' is not installed in ${profile:-the root tree}" >&2
			echo "  the lane would load no rules for it; run hermes-skills-sync to install the repository's version" >&2
			failed=1
			continue
		fi
		repo_digest=$(skill_digest "$repo_dir")
		tree_digest=$(skill_digest "$tree_dir")
		[ -n "$repo_digest" ] && [ "$repo_digest" = "$tree_digest" ] && continue
		echo "skill '$name' in ${profile:-the root tree} is not the repository's copy" >&2
		echo "  the lane would run the old rules; run hermes-skills-sync to install the repository's version" >&2
		failed=1
	done
	[ "$failed" -eq 0 ] || return 1
	return 0
}

# `manifest <repo> <query> [default]`. Without a default a missing field is
# fatal, which is the right outcome for a gate that would otherwise guess.
manifest() { python3 "$TOOLS_LIB/manifest.py" "$@"; }

# Turn any string into a portable env-var suffix: upper case, with every
# non-alphanumeric collapsed to a single underscore. A repo like "owner/name"
# becomes OWNER_NAME, matching what an env var holding a path expects.
env_name() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -c '[:alnum:]' '_' | tr -s '_'; }

# `profile_name_ok` -- whether a profile name may be used as one component
# of a path or a git ref.
#
# A profile name goes into a branch, a worktree path and a lock file, so
# it has to be one path component and one git ref component at once.
#
# Sanitising it would map two different profiles onto the same name --
# `review.a` and `reviewa` both become `reviewa` -- and they would then
# share a branch, a worktree and a slot lock. So the name is checked.
profile_name_ok() { # <profile>
	[[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]
}

# Roles are named for what a host can do, so a project asks for `cluster` and
# never for anyone's hostname.
#
# A lane is an alternative builder: REMOTE_LANE selects HOST_BUILDER_<LANE>
# and leaves every other role alone. There is no fallback, because the env file
# above may set HOST_BUILDER unconditionally, and falling back would send a
# lane's build to the default host silently.
host_for() {
	local var
	var="HOST_$(env_name "$1")"
	if [ "$1" = builder ] && [ -n "${REMOTE_LANE:-}" ]; then
		var="${var}_$(env_name "$REMOTE_LANE")"
	fi
	if [ -z "${!var:-}" ]; then
		echo "set $var to an ssh target for the '$1' role (see hosts.env.example)" >&2
		return 1
	fi
	printf '%s' "${!var}"
}

# Builders whose memory comes from one pool share a dispatch lock. A lane
# without LANE_POOL_<LANE> shares it too: a wrong shared lock costs
# parallelism, a wrong private one costs the protection the lock exists for.
lock_for() {
	local var pool=build
	if [ -n "${REMOTE_TASK_LOCK:-}" ]; then
		printf '%s' "$REMOTE_TASK_LOCK"
		return
	fi
	if [ -n "${REMOTE_LANE:-}" ]; then
		var="LANE_POOL_$(env_name "$REMOTE_LANE")"
		pool=${!var:-build}
	fi
	printf '%s' "$HOME/.local/state/agent-tools/$pool.lock"
}

# A project that declares a builder task compiles there, so its lanes are
# refused a local rustc.
export_lane_build_env() {
	local roles
	roles=$(manifest "$1" roles 2>/dev/null) || return 0
	case " $roles " in
	*" builder "*) export RUSTC_WRAPPER="$TOOLS_LIB/lane-rustc" ;;
	esac
}

# `pick_builder_lane <tree>` returns 0 holding the first idle lane's pool lock,
# with REMOTE_LANE, BUILD_LOCK and lockfd set; 1 if all stay busy; 2 if a lock
# cannot be opened. It polls, because blocking on one lane waits behind a build
# while another sits idle. The tree's last lane goes first, since moving a tree
# costs a cold compile.
pick_builder_lane() {
	local tree=$1 lane last deadline lanes=()
	local record="$HOME/.local/state/agent-tools/builder-lane/$tree"
	read -ra lanes <<<"$BUILDER_LANES"
	last=$(cat "$record" 2>/dev/null || true)
	if [ -n "$last" ] && [[ " ${lanes[*]} " == *" $last "* ]]; then
		lanes=("$last" "${lanes[@]}")
	fi
	mkdir -p "$(dirname "$record")"
	deadline=$((SECONDS + LOCK_WAIT))
	while :; do
		for lane in "${lanes[@]}"; do
			REMOTE_LANE=$lane
			BUILD_LOCK=$(lock_for)
			mkdir -p "$(dirname "$BUILD_LOCK")" || return 2
			exec {lockfd}>"$BUILD_LOCK" || return 2
			if flock -n "$lockfd"; then
				printf '%s\n' "$lane" >"$record"
				return 0
			fi
			exec {lockfd}>&-
		done
		[ "$SECONDS" -lt "$deadline" ] || return 1
		sleep "${PICK_LANE_POLL:-10}"
	done
}

# A non-interactive ssh reads no profile, so a version manager that puts tools on
# PATH from an interactive shell puts nothing there. Override for a host that
# keeps its shims elsewhere.
# The dollars stay literal here and expand on the far side: these name the
# remote account's home and the remote PATH, and expanding them locally sends
# this machine's paths to a host that has none of them.
: "${REMOTE_PATH:=\$HOME/.local/share/mise/shims:\$HOME/.local/bin:\$PATH}"

# A stable per-tree name for remote directories and databases, so two branches
# never share one.
tree_name() { basename "$1" | tr -c 'A-Za-z0-9_' '_' | tr -s '_' | sed 's/_$//'; }

repo_root() { (cd "${1:-$PWD}" && git rev-parse --show-toplevel); }

# The identity a lane's commits must carry. The caller override is
# LANE_GIT_NAME / LANE_GIT_EMAIL. Setting either one selects it;
# setting only one is incomplete and refuses rather than mixing in
# the worktree's config. With neither set, fall back to `git -C
# <worktree> config user.name` / user.email. Both fields must
# resolve; either empty returns non-zero so the caller can refuse
# before the agent starts. No host, account or project name is
# interpolated -- the identity is the operator's, not ours.
lane_expected_identity() { # <worktree>  -> stdout: "<name>|<email>"
	local wt="$1" name="" email=""
	if [ -n "${LANE_GIT_NAME:-}${LANE_GIT_EMAIL:-}" ]; then
		name=${LANE_GIT_NAME:-}
		email=${LANE_GIT_EMAIL:-}
	else
		name=$(git -C "$wt" config --get user.name 2>/dev/null || true)
		email=$(git -C "$wt" config --get user.email 2>/dev/null || true)
	fi
	if [ -z "$name" ] || [ -z "$email" ]; then
		echo "cannot pin lane identity: set LANE_GIT_NAME and LANE_GIT_EMAIL, or configure user.name and user.email in $wt" >&2
		return 1
	fi
	printf '%s|%s\n' "$name" "$email"
}

# Resolve the lane identity and export it as the env vars git reads
# before user.name / user.email. Env vars win over `-c user.name=...`
# and the worktree config, so the common case lands authored by the
# expected identity. `--author` and `GIT_AUTHOR_NAME=...` on the
# agent's command line bypass this; the verifier is the backstop.
export_lane_identity_env() { # <worktree>
	local identity name email
	identity=$(lane_expected_identity "$1") || return $?
	name=${identity%%|*}
	email=${identity##*|}
	export GIT_AUTHOR_NAME="$name"
	export GIT_AUTHOR_EMAIL="$email"
	export GIT_COMMITTER_NAME="$name"
	export GIT_COMMITTER_EMAIL="$email"
}

# For each commit the lane added between BASE and HEAD, check that the
# author and committer match the expected identity. When they don't,
# re-author the lane's own commits, or refuse if LANE_GIT_IDENTITY=refuse.
# Each rewrite is logged to stderr and, when a handover path is given,
# appended to the handover.
#
# Returns 0 when every lane commit matches, otherwise non-zero:
#   1  identity mismatch in refuse mode;
#   2  base the lane rewrote out of ancestry (HEAD no longer carries
#      the base as an ancestor);
#   3  base does not resolve in the worktree;
#   4  the rewrite changed the tree (the old head is restored);
#   5  the rewrite could not start.
# BASE is read with `git -C`, not interpolated, so a sha is exact.
#
# The ancestry check is the first thing: a lane that amended the base
# makes HEAD no longer carry BASE as an ancestor, and replaying onto
# the original base would look like a clean fast-forward.
#
# The remote default branch, or nothing. The remote's HEAD is the
# canonical answer, and origin/main is the fallback for a clone that
# has no HEAD set.
lane_default_branch() { # <worktree> [default_branch]
	local wt="$1" default="${2:-}"
	if [ -z "$default" ]; then
		default=$(git -C "$wt" symbolic-ref --quiet --short \
			refs/remotes/origin/HEAD 2>/dev/null || echo "")
	fi
	if [ -z "$default" ] && git -C "$wt" rev-parse --verify --quiet \
			"origin/main" >/dev/null 2>&1; then
		default="origin/main"
	fi
	printf '%s' "$default"
}

# The `--not <default>` arguments for a log over the lane's own commits.
#
# Only a default branch that resolves is excluded: a ref that does not
# exist would make every log call fail, and a check that cannot run
# must not look like a check that passed.
lane_default_exclusion() { # <worktree> [default_branch]
	local wt="$1" default
	default=$(lane_default_branch "$wt" "${2:-}")
	if [ -n "$default" ] && git -C "$wt" rev-parse --verify --quiet \
			"$default" >/dev/null 2>&1; then
		printf -- '--not\n%s\n' "$default"
	fi
}

# The commits the lane is responsible for: the ones on its own
# first-parent chain, minus the ones the remote default branch already
# has. Both halves are needed, and they cover different mistakes:
#
#   --first-parent  A merge's second parent is somebody else's commits.
#     A lane whose job is to merge a branch with foreign authors pulls
#     thousands of commits into the range, and every one of them fails
#     the identity check.
#
#   --not <default>  A lane that rebased onto a newer default branch
#     picks up that branch's commits, which land in the range on the
#     first-parent chain. Re-authoring them gives the branch rewritten
#     copies of commits already on main -- the same tree, new shas, and
#     a history duplicated on the pull request.
lane_own_commits() { # <worktree> <base> [default_branch]
	local wt="$1" base="$2" not_args=()
	mapfile -t not_args < <(lane_default_exclusion "$wt" "${3:-}")
	# --reverse puts the commits oldest-first: the rewrite below
	# rebuilds each commit with its parents looked up in the set of the
	# ones already rewritten, so a parent must be rewritten before its
	# child.
	git -C "$wt" log --reverse --first-parent --format='%H' "$base..HEAD" \
		"${not_args[@]}"
}

verify_lane_identity() { # <worktree> <base> [handover_path] [default_branch]
	local wt="$1" base="$2" handover="${3:-}" default="${4:-}"
	local identity name email old_head new_head mismatch=0 rewrites=() rc=0 base_sha head_sha
	# The base must resolve to a sha; refusing is the only safe answer
	# when it does not (a typo, a branch not pushed yet).
	if ! base_sha=$(git -C "$wt" rev-parse --verify "$base" 2>/dev/null); then
		echo "refusing: base '$base' does not resolve in $wt" >&2
		return 3
	fi
	head_sha=$(git -C "$wt" rev-parse HEAD)
	# Nothing to check: a lane that exited at BASE did not commit.
	# An empty range makes `git log` print an error, so the comparison
	# below treats it as a no-op.
	[ "$base_sha" = "$head_sha" ] && return 0
	# A lane that amended or rebased the base is rewritten history.
	# The verifier must not launder that into a clean fast-forward by
	# replaying the rewritten commit onto the original base.
	if ! git -C "$wt" merge-base --is-ancestor "$base_sha" HEAD; then
		echo "refusing: base $base_sha is not an ancestor of $head_sha in $wt (history rewritten)" >&2
		return 2
	fi
	identity=$(lane_expected_identity "$wt") || return 1
	name=${identity%%|*}
	email=${identity##*|}
	# The lane's own commits, computed once and used for both the check
	# and the rewrite below. The two must agree, or the verifier would
	# refuse to fix a commit it is not allowed to touch, or touch a
	# commit it did not check.
	local -a own=()
	mapfile -t own < <(lane_own_commits "$wt" "$base" "$default")
	if [ "${#own[@]}" -eq 0 ]; then
		# The lane's range has no commits of its own: it committed
		# nothing, or its whole range is the default branch's (a pure
		# rebase). Returning here also keeps the `git log --no-walk`
		# below from being called with no commit arguments, which would
		# otherwise default to HEAD.
		return 0
	fi
	# Format %x1f separates the fields so the parsing below is
	# straightforward; `name <email>` alone could collide with a name
	# that contains ` <`.
	# The check runs over `own` -- the same set the rewrite runs over
	# below -- so the two cannot disagree about whose commits are in
	# scope. `--no-walk` keeps the log to exactly those commits.
	local check_sha
	while IFS=$'\x1f' read -r check_sha a_name a_email c_name c_email; do
		[ -z "$check_sha" ] && continue
		if [ "$a_name" != "$name" ] || [ "$a_email" != "$email" ] \
			|| [ "$c_name" != "$name" ] || [ "$c_email" != "$email" ]; then
			mismatch=1
			if [ "${LANE_GIT_IDENTITY:-}" = refuse ]; then
				echo "refusing: commit $check_sha has author '$a_name <$a_email>' / committer '$c_name <$c_email>', expected '$name <$email>'" >&2
				return 1
			fi
			break
		fi
	done < <(git -C "$wt" log --no-walk --format='%H%x1f%an%x1f%ae%x1f%cn%x1f%ce' "${own[@]}")
	if [ "$mismatch" = 0 ]; then return 0; fi
	old_head=$(git -C "$wt" rev-parse HEAD)
	# The pinned identity is scoped to the rewrite alone
	# (`env GIT_AUTHOR_NAME=… git …`) rather than exported into this
	# function's caller: exporting would leak into the calling shell
	# and every subprocess that follows (the gate, the push).
	#
	# The rewrite rebuilds the lane's own commits and nothing else. A
	# rebase is the wrong tool here, for two reasons:
	#
	#   It rewrites everything it replays. A lane that rebased onto a
	#   newer default branch has that branch's commits in the range, and
	#   a rebase gives them new shas: the branch ends up carrying
	#   rewritten copies of commits already on main, and a later merge
	#   duplicates main's history on the pull request.
	#
	#   It drops merges. A plain `git rebase` flattens a merge, so a
	#   lane whose job was to merge a branch with foreign authors would
	#   have its merge undone by the verifier that was supposed to be
	#   checking it.
	#
	# `git commit-tree` rebuilds one commit at a time: the same tree,
	# the same message, the same parents, and the identity the caller
	# expects. A commit's sha covers its parents, so a rewritten
	# commit's children are rebuilt with the new parent -- which is
	# why the commits are rewritten oldest first and each one's parents
	# are looked up in the map of the ones already rewritten.
	local -a rewritten=() mapped=()
	local old new tree message parent parents_arg commit_sha
	local -A parent_map=()
	for commit_sha in "${own[@]}"; do
		tree=$(git -C "$wt" rev-parse "$commit_sha^{tree}") || return 5
		message=$(git -C "$wt" log -1 --format=%B "$commit_sha")
		parents_arg=()
		for parent in $(git -C "$wt" log -1 --format=%P "$commit_sha"); do
			parents_arg+=(-p "${parent_map[$parent]:-$parent}")
		done
		if ! new=$(printf '%s' "$message" | env GIT_AUTHOR_NAME="$name" \
				GIT_AUTHOR_EMAIL="$email" GIT_COMMITTER_NAME="$name" \
				GIT_COMMITTER_EMAIL="$email" \
				git -C "$wt" commit-tree "$tree" "${parents_arg[@]}"); then
			echo "refusing: could not re-author commit $commit_sha in $wt" >&2
			return 5
		fi
		parent_map["$commit_sha"]="$new"
		rewritten+=("$new")
		mapped+=("$commit_sha")
	done
	new_head="${parent_map[$old_head]:-$old_head}"
	if [ "$new_head" = "$old_head" ]; then
		echo "refusing: the re-author produced no new commit in $wt" >&2
		return 5
	fi
	# The branch moves to the rewritten head. The tree is the same by
	# construction -- every commit was rebuilt with its own tree -- so
	# this is a ref update and not a checkout.
	if ! git -C "$wt" update-ref HEAD "$new_head" "$old_head"; then
		echo "refusing: could not move $wt to the re-authored head" >&2
		return 5
	fi
	# Trees must be byte-identical: the rewrite is a re-author, not
	# a redraft. A non-empty diff means something went wrong that we
	# cannot safely hand the expected identity for; restore the old
	# head and refuse.
	if [ -n "$(git -C "$wt" diff "$old_head" "$new_head")" ]; then
		git -C "$wt" update-ref HEAD "$old_head" "$new_head" || true
		echo "refusing: re-author in $wt changed the tree; restored $old_head" >&2
		return 4
	fi
	# Record each rewritten commit (old sha -> new sha) so a reader of
	# the run directory knows the branch was rewritten. The pair is
	# by position, because the rewritten commit has new parents and is
	# otherwise indistinguishable from the old.
	local i
	for ((i = 0; i < ${#mapped[@]}; i++)); do
		# Sanity: lengths must match; otherwise the rewrite dropped a
		# commit (an unlikely failure mode, but the cost of catching it
		# is one line and the cost of missing it is a silent rewrite
		# that produced a different shape).
		[ $i -lt ${#rewritten[@]} ] || break
		rewrites+=("${mapped[$i]} -> ${rewritten[$i]}")
	done
	for r in "${rewrites[@]}"; do
		echo "re-authored: $r" >&2
		if [ -n "$handover" ]; then
			# Append: the handover may already exist with its own
			# content, and the verifier writer who rotates through
			# rounds must see every rewrite, not just the last.
			printf '%s\n' "- re-authored: $r" >>"$handover"
		fi
	done
	return 0
}

# Returns holding a review slot for PROFILE on fd 9, waiting for one first.
# The wait sits outside the review's timeout, because a review queued inside a
# single-slot model server spends its timeout waiting and dies having written
# nothing. The caller closes fd 9, and passes `9>&-` to anything it spawns, or
# that process holds the slot for its whole life.
# The state directory, because a lane sets its own TMPDIR.
acquire_review_slot() {
	local profile="$1" slots_var slots lock i dir printed=0
	slots_var="REVIEW_SLOTS_$(env_name "$profile")"
	slots=${!slots_var:-1}
	dir="${AGENT_STATE_DIR:-$HOME/.local/state/agent-tools}"
	mkdir -p "$dir"
	# The lock file is a path, so the profile name is one component of
	# it. The name is used as it is given.
	while true; do
		for ((i = 1; i <= slots; i++)); do
			lock="$dir/hermes-review-${profile}-${i}.lock"
			exec 9>"$lock"
			if flock -n 9; then
				return 0
			fi
			exec 9>&-
		done
		if [ "$printed" -eq 0 ]; then
			echo "waiting for a review slot on profile $profile (have $slots)" >&2
			printed=1
		fi
		sleep 2
	done
}

# Returns holding the lock for PROFILE's GPU group (GPU_GROUP_<PROFILE>, or
# GPU_GROUP_DEFAULT for the root profile) on fd 8, or holding nothing when no
# group is set. Two models sharing GPUs swap on every alternating call, so
# their callers queue here instead, outside their timeouts. Closing fd 8
# releases it.
acquire_gpu_lock() {
	local profile="$1" var group dir printed=0
	var="GPU_GROUP_$(env_name "${profile:-DEFAULT}")"
	group="${!var:-}"
	[ -z "$group" ] && return 0
	dir="${AGENT_STATE_DIR:-$HOME/.local/state/agent-tools}"
	mkdir -p "$dir"
	while true; do
		exec 8>"$dir/gpu-$group.lock"
		if flock -n 8; then
			return 0
		fi
		exec 8>&-
		if [ "$printed" -eq 0 ]; then
			echo "waiting for GPU group $group (profile $profile)" >&2
			printed=1
		fi
		sleep 2
	done
}

# The explicit value, then REVIEW_TIMEOUT_<PROFILE>, then HERMES_REVIEW_TIMEOUT,
# then 900. A caller passes the explicit value only when its own --timeout was
# given: forwarding its default would silently outrank the per-profile value.
review_timeout_for() {
	local profile="$1" explicit="${2:-}" var
	if [ -n "$explicit" ]; then
		printf '%s\n' "$explicit"
		return
	fi
	var="REVIEW_TIMEOUT_$(env_name "$profile")"
	if [ -n "${!var:-}" ]; then
		printf '%s\n' "${!var}"
		return
	fi
	printf '%s\n' "${HERMES_REVIEW_TIMEOUT:-900}"
}

# `hermes_query <agent_cmd> <prompt_file> <out_stdout> <out_stderr> [args...]`
# runs the agent CLI with `chat --query-file FILE --oneshot -Q` and writes
# stdout/stderr to the given paths. The prompt is read from a file the
# caller owns (never an argv string), so a 200 KB brief with quotes, $(...)
# and backticks survives intact instead of bumping into MAX_ARG_STRLEN.
#
# Argument convention. `<agent_cmd>` is a single string the helper
# word-splits itself; the helper adds nothing to the agent's argv
# (no -m, no chat). The remaining args after the four positionals are
# split on the `--` sentinel:
#
#   hermes_query "AGENT_CMD" PROMPT OUT ERR -p PROFILE -- --skills X --yolo
#
# Everything before `--` is pre-chat (profile / model overrides) and goes
# before the literal `chat` subcommand. Everything after `--` is post-chat
# (--skills, --yolo, --no-restore-cwd, --continue, --resume) and goes
# after `--query-file FILE --oneshot -Q`. A single `--` with nothing on
# either side is fine: an empty pre or post is allowed. The two sentinels
# split each side, so the helper takes exactly the args the caller
# composed in two arrays.
#
# Callers compose AGENT_CMD in the shape the agent itself expects:
#
#   - bin/ tools and hermes-scrutinize.sh: "$HERMES -m hermes_cli.main"
#   - pipeline phases: "$PIPELINE_AGENT" (a path plus args, split as
#     one command line by the helper the same way `$HERMES` was
#     split before this PR).
#
# This contract means the helper never adds `-m hermes_cli.main`
# itself. A previous shape did: the helper took a single program
# name and added `-m hermes_cli.main` after it. That broke callers
# whose agent is already a multi-word command (the pipeline's
# $PIPELINE_AGENT is `…/python -m hermes_cli.main`), and combined
# with the helper returning 0 unconditionally, the nightly review
# and triage did nothing while reporting no error.
#
# `chat -Q` writes one `session_id: <id>` line before the answer. Older
# versions wrote it to stdout; this one writes it to stderr, after a
# blank line (`cli.py`: `print(f"\nsession_id: {id}", file=sys.stderr)`).
#
# The stdout copy is filtered out on its way back to the file the caller
# named, because a caller whose downstream code reads stdout must not get
# a parsed-as-json answer that suddenly starts with `session_id:`. That
# filter is by position -- the first line -- so it costs nothing when
# the agent wrote the id to stderr. It is kept because an older hermes
# still writes it there, and because the answer itself could carry the
# shape.
#
# A caller that needs the session id must read it from stderr and scan
# for the line rather than take the first one. The id is gone on a killed
# attempt, because the CLI prints it once the conversation ends; a run
# that needs the session survives a kill by naming it up front, as
# `bin/hermes-implement` does.
#
# The helper does NOT close fd 8 (GPU group lock) or fd 9 (review slot):
# callers that need those closed in the child add `8>&- 9>&-` to their
# own invocation. hermes-implement keeps them open so the GPU lock spans
# retries; the review scripts close them so the child cannot outlive its
# slot.
#
# The helper returns the agent's exit status, so callers reading the
# code (`local rc=$?`, `RC=$?`) see what the agent actually did --
# a `timeout`-killed agent's 124, or a hermes crash's 1, rather
# than the 0 the helper's last `case` would otherwise leave behind.
hermes_query() {
	local agent_cmd="$1" prompt_file="$2" out_stdout="$3" out_stderr="$4"
	shift 4
	# Word-split the agent command into argv. The command is what the
	# caller wants the helper to run -- nothing is appended, so a
	# pipeline that names `python -m hermes_cli.main` gets exactly
	# that, and a bin/ tool that names `hermes -m hermes_cli.main`
	# gets the module on its own -m. read -ra splits on $IFS (default
	# whitespace), which matches the way `$HERMES -p ...` was split
	# before this helper existed.
	local -a agent_words
	read -ra agent_words <<<"$agent_cmd"
	local pre=() post=()
	local phase=pre
	for arg in "$@"; do
		if [ "$phase" = pre ] && [ "$arg" = "--" ]; then
			phase=post
			continue
		fi
		if [ "$phase" = pre ]; then
			pre+=("$arg")
		else
			post+=("$arg")
		fi
	done
	# An unset agent is the caller's bug, not ours: refuse with a name so
	# the fix is the line they wrote.
	if [ "${#agent_words[@]}" -eq 0 ]; then
		echo "hermes_query: agent command is empty" >&2
		return 2
	fi
	if [ -z "$prompt_file" ] || [ ! -r "$prompt_file" ]; then
		echo "hermes_query: prompt file not readable: $prompt_file" >&2
		return 2
	fi
	"${agent_words[@]}" "${pre[@]}" chat \
		--query-file "$prompt_file" --oneshot -Q \
		"${post[@]}" \
		>"$out_stdout" 2>"$out_stderr"
	# Save the agent's status before anything else. The function would
	# otherwise return the status of the final `case` below, which is
	# always 0, and a failed agent (or one that `timeout` killed) would
	# look like a success to every caller that reads the code: the
	# retry loop and the 124 handling in bin/hermes-implement, the
	# `RC=$?` checks in bin/hermes-review-pr and hermes-scrutinize.sh.
	local rc=$?
	# Drop the `session_id: <id>` line that `chat -Q` writes on its first stdout
	# line, ahead of the answer. Read only the first line so the marker is
	# identified by *position*, not by pattern match -- an answer whose own
	# first line happens to start with `session_id:` would otherwise have its
	# real content rewritten. head/tail keeps the rest of the file untouched.
	local first
	first=$(head -n 1 "$out_stdout" 2>/dev/null || true)
	case "$first" in
	'session_id: '*)
		tail -n +2 "$out_stdout" >"$out_stdout.tmp"
		cat "$out_stdout.tmp" >"$out_stdout"
		rm -f "$out_stdout.tmp"
		;;
	esac
	return "$rc"
}
