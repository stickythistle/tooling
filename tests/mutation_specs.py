#!/usr/bin/env python3
"""The mutation specs for the credential-free lane sweep.

The specs live here rather than in the shell script because they carry
tabs and quotes, and a shell single-quoted string passes every backslash
literally. Each spec is a triple: the name, the file, and the (old,
new) pair that breaks one mechanism.

The old and new are separated by a marker that cannot appear in the
code, and the replacement is the same shape as the original, so the
mutated file still parses.
"""
import subprocess
import sys

SEPARATOR = "@@@@@"

SPECS = [
    ("gh_as_falls_through", "lib/common.sh",
     '\t\tif [[ -z "$login" ]]; then',
     '\t\tif false; then'),

    ("gh_as_any_role", "lib/common.sh",
     "\timplementer) ;;",
     "\treviewer | implementer) ;;"),

    ("token_on_argv", "lib/common.sh",
     '\t\tGH_TOKEN="$token" gh "$@"',
     '\t\tgh "$@" "$token"'),

    ("keeps_token", "bin/hermes-implement",
     "\texec env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN \\",
     "\texec env -u GITHUB_ENTERPRISE_TOKEN \\"),

    ("keeps_config_dir", "bin/hermes-implement",
     '\t\tGH_CONFIG_DIR="$LANE_GH_DIR" \\',
     '\t\tGH_CONFIG_DIR="${GH_CONFIG_DIR:-}" \\'),

    ("config_dir_not_emptied", "lib/common.sh",
     '\trm -f "$dir/hosts.yml"',
     '\t:'),

    ("shared_process_group", "bin/hermes-implement",
     "\texec env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN \\",
     "\tenv -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN \\"),

    ("silent_lane", "bin/hermes-implement",
     "**An accidental \\`gh\\` call in this run fails, and that is deliberate.**",
     "**An accidental \\`gh\\` call in this run fails.**"),

    ("reachable_credentials", "bin/hermes-implement",
     "- You run as the same operating-system user as the dispatcher, so credentials",
     "- Credentials may be reachable on this host. Nothing else changes here"),

    ("claims_no_credentials", "bin/hermes-implement",
     "**An accidental \\`gh\\` call in this run fails, and that is deliberate.** \\`gh\\`",
     "**You have no GitHub credentials.** \\`gh\\`"),

    ("ambient_issue_read", "bin/hermes-implement",
     'gh_as implementer "${GH_ARGS[@]}"',
     'gh "${GH_ARGS[@]}"'),

    ("no_login_check", "lib/common.sh",
     '\t*" $login "*) ;;',
     '\t*) ;;'),

    # The cache of resolved logins must be keyed by the credential, or a
    # second token inherits the first token's answer.
    ("scope_keyed_cache", "lib/common.sh",
     '\tkey=$(printf \'%s\' "${GH_TOKEN:-}|${GH_CONFIG_DIR:-}" | sha256sum | cut -d" " -f1)',
     '\tkey="scope"'),

    # The cache must not live in a path another user can read.
    ("shared_cache_path", "lib/common.sh",
     '\tcache_dir=$(mktemp -d "${TMPDIR:-/tmp}/gh-as-cache.XXXXXX") || cache_dir=""',
     '\tcache_dir="${TMPDIR:-/tmp}"'),
]


def main(argv):
    if len(argv) != 4:
        print("usage: mutate.py <file> <old> <new>", file=sys.stderr)
        return 2
    path, old, new = argv[1], argv[2], argv[3]
    text = open(path).read()
    if old not in text:
        print(f"MUTATION TARGET NOT FOUND in {path}: {old!r}", file=sys.stderr)
        return 3
    open(path, "w").write(text.replace(old, new, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
