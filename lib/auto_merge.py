#!/usr/bin/env python3
"""Decide whether GitHub may be told to auto-merge a pull request.

Nothing here merges anything. The decision is *whether to ask GitHub to
enable auto-merge for one exact head*, and GitHub's own ruleset is the
authority that finally merges it. A tool that merged directly would hold
the merge rule itself, and that rule is exactly what a ruleset is for:
one approving review of the most recent push, stale approvals dismissed,
the required checks green, linear history.

The head is bound to the write: GitHub's `enablePullRequestAutoMerge`
mutation takes the head the decision was made about, and refuses the
request when the pull request's head has moved on. The ruleset holds
the merge authority; the head is what says which commit that authority
is being asked about.

Three things decide, and each can refuse:

- **The repository's configuration** (`pr-loop.toml`): the merge method,
  the ruleset that holds the authority, the required checks, and the
  mode. `auto_merge = "shadow"` is the default; `"on"` is the only mode
  in which auto-merge is enabled at all.

- **The protections actually in force**: the ruleset the configuration
  names must exist, be enforced, and carry the required review and check
  rules. A missing or weaker ruleset is a refusal, not a warning. An
  unknown prerequisite is never treated as satisfied.

- **The pull request's own state**: the approval must be on the head
  that is being enabled, the required checks must be green on that head,
  and no path the repository protects may have been touched.

The decision is a value, not a side effect, so it is tested against
recorded API responses without a network.
"""
from __future__ import annotations

import fnmatch
import re

# The gate's own readers. `pr_gate` holds what a review is and what a
# check is; this module holds what to do about them. It does not import
# this one back, so there is no cycle.
#
# `pending_checks` asks only whether a check has finished. A check run
# that has finished and failed reports `status: "COMPLETED"` with
# `conclusion: "FAILURE"`, so "finished" is not "green". The green
# reader is `failing_checks`; `pr-loop-gate` keeps using
# `pending_checks`, whose question is the one it means to ask.
from pr_gate import failing_checks, pending_checks, verdict_reviews

# The mode a repository runs in. `shadow` records what the loop *would*
# have done and leaves the merge to a person. `on` asks GitHub to enable
# auto-merge. There is no third mode: a repository that is neither has
# not been configured, and an unconfigured repository never merges.
SHADOW = "shadow"
ON = "on"
MODES = (SHADOW, ON)

# The merge methods a repository may be configured for. `merge` is
# excluded on purpose: the issue asks for squash where PRs are not
# stacked and rebase where they are, and a merge commit is neither.
MERGE_METHODS = ("squash", "rebase")

# The label the loop puts on a pull request. `needs-merge` marks a PR
# that a person has to merge, because it touches a path the repository
# protects or because a protection is missing.
LABEL_NEEDS_MERGE = "pr-loop:needs-merge"

# The ruleset rule types this module reads. A ruleset that carries none
# of them holds no merge authority.
RULE_PULL_REQUEST = "pull_request"
RULE_REQUIRED_STATUS_CHECKS = "required_status_checks"
RULE_REQUIRED_LINEAR_HISTORY = "required_linear_history"


class ConfigError(Exception):
    """The repository's configuration cannot be read or is not safe.

    Raised, and the run stops: a repository whose mode or merge method
    is unknown must not be enabled for auto-merge.
    """


class Malformed(Exception):
    """A fetched entry carries a shape outside the API's.

    Raised, and the run stops: a ruleset whose review rule is missing a
    field is a rule whose strength cannot be read.
    """

    def __init__(self, what: str) -> None:
        super().__init__(f"malformed {what}")
        self.what = what


# ----- configuration ------------------------------------------------------

class RepoConfig:
    """One repository's settings, read from `pr-loop.toml`.

    The defaults are the safe ones: shadow mode, no ruleset named, and
    no merge method. A repository that names nothing is a repository
    whose protections are unknown, and an unknown prerequisite is never
    satisfied.
    """

    def __init__(self, repo: str, mode: str = SHADOW,
                 merge_method: str | None = None,
                 ruleset: str | None = None,
                 required_checks: tuple = (),
                 protected_paths: tuple = (),
                 codeowners: str = "CODEOWNERS",
                 allow_same_family: bool | None = None) -> None:
        self.repo = repo
        self.mode = mode
        self.merge_method = merge_method
        self.ruleset = ruleset
        self.required_checks = tuple(required_checks)
        self.protected_paths = tuple(protected_paths)
        self.codeowners = codeowners
        # None means "undecided". The issue asks whether a pull request
        # may auto-merge when its implementer and reviewer share a model
        # family, and no answer has been given. An undecided answer is
        # not a permission, so the decision refuses when the question
        # arises and the answer is not an explicit yes.
        self.allow_same_family = allow_same_family

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"RepoConfig(repo={self.repo!r}, mode={self.mode!r}, "
                f"merge_method={self.merge_method!r}, ruleset={self.ruleset!r})")


def parse_config(repo: str, doc: dict) -> RepoConfig:
    """Read one repository's table out of a `pr-loop.toml` document.

    `doc` is the whole document; the repository's own table is looked up
    by name. A repository with no table gets the defaults, which are the
    safe ones: shadow, no ruleset, no merge method.

    A repository's table is named `owner/name`. A table with any other
    name is refused: it is either a typo in a repository name or a
    setting the file was never meant to carry, and reading it as a
    repository would give a repository the settings of a heading.
    """
    if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise ConfigError(
            f"{repo!r} is not a repository name; it must be owner/name")
    table = doc.get(repo)
    if table is None:
        # A repository with no entry is a repository nobody configured.
        # It gets shadow mode and no protections, so nothing enables.
        return RepoConfig(repo)
    if not isinstance(table, dict):
        raise ConfigError(f"{repo}: the table is not a table")

    mode = table.get("auto_merge", SHADOW)
    if mode not in MODES:
        raise ConfigError(
            f"{repo}: auto_merge is {mode!r}; it must be one of "
            f"{', '.join(repr(m) for m in MODES)}")

    merge_method = table.get("merge_method")
    if merge_method is not None and merge_method not in MERGE_METHODS:
        raise ConfigError(
            f"{repo}: merge_method is {merge_method!r}; it must be one of "
            f"{', '.join(repr(m) for m in MERGE_METHODS)}")

    ruleset = table.get("ruleset")
    if ruleset is not None and not isinstance(ruleset, str):
        raise ConfigError(f"{repo}: ruleset must be a name")
    if ruleset is not None and not ruleset.strip():
        raise ConfigError(f"{repo}: ruleset is empty")

    checks = table.get("required_checks", ())
    if isinstance(checks, str):
        checks = (checks,)
    if not isinstance(checks, (list, tuple)):
        raise ConfigError(f"{repo}: required_checks must be a list")
    for check in checks:
        if not isinstance(check, str) or not check:
            raise ConfigError(
                f"{repo}: required_checks holds {check!r}; every entry must "
                f"be a non-empty name")

    protected = table.get("protected_paths", ())
    if isinstance(protected, str):
        protected = (protected,)
    if not isinstance(protected, (list, tuple)):
        raise ConfigError(f"{repo}: protected_paths must be a list")
    for path in protected:
        if not isinstance(path, str) or not path:
            raise ConfigError(
                f"{repo}: protected_paths holds {path!r}; every entry must "
                f"be a non-empty pattern")

    codeowners = table.get("codeowners", "CODEOWNERS")
    if not isinstance(codeowners, str) or not codeowners:
        raise ConfigError(f"{repo}: codeowners must be a path")

    same_family = table.get("allow_same_family")
    if same_family is not None and not isinstance(same_family, bool):
        raise ConfigError(f"{repo}: allow_same_family must be true or false")

    return RepoConfig(repo, mode=mode, merge_method=merge_method,
                       ruleset=ruleset, required_checks=tuple(checks),
                       protected_paths=tuple(protected),
                       codeowners=codeowners,
                       allow_same_family=same_family)


# ----- rulesets ------------------------------------------------------------

class Ruleset:
    """The merge authority a repository's ruleset puts in force.

    Only the facts this module needs are kept. `pull_request` is the
    rule that holds the merge authority; the other two are the checks
    and the linear history.
    """

    def __init__(self, name: str, enforcement: str,
                 required_approving_review_count: int = 0,
                 dismiss_stale_reviews_on_push: bool = False,
                 require_last_push_approval: bool = False,
                 require_code_owner_review: bool = False,
                 allowed_merge_methods: tuple = (),
                 required_status_checks: tuple = (),
                 linear_history: bool = False,
                 id: int | None = None) -> None:
        self.name = name
        self.enforcement = enforcement
        self.required_approving_review_count = required_approving_review_count
        self.dismiss_stale_reviews_on_push = dismiss_stale_reviews_on_push
        self.require_last_push_approval = require_last_push_approval
        self.require_code_owner_review = require_code_owner_review
        self.allowed_merge_methods = tuple(allowed_merge_methods)
        self.required_status_checks = tuple(required_status_checks)
        self.linear_history = linear_history
        self.id = id

    @property
    def enforced(self) -> bool:
        """True when the ruleset actually applies.

        `disabled` does not hold anything. `evaluate` is GitHub's
        dry-run mode: the rules are reported but not enforced, so
        nothing is protected by them.
        """
        return self.enforcement == "active"


def parse_ruleset(payload: dict) -> Ruleset:
    """Read the merge authority out of one ruleset's API object.

    Every field this module relies on is checked for shape. A ruleset
    whose `pull_request` rule is missing `required_approving_review_count`
    is a rule whose strength cannot be read, and an unreadable rule
    protects nothing.
    """
    if not isinstance(payload, dict):
        raise Malformed("ruleset")
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise Malformed("ruleset: name")
    enforcement = payload.get("enforcement")
    if not isinstance(enforcement, str) or not enforcement:
        raise Malformed("ruleset: enforcement")

    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise Malformed("ruleset: rules")

    approvals = 0
    dismiss_stale = False
    last_push = False
    code_owner = False
    methods: tuple = ()
    checks: tuple = ()
    linear = False
    saw_pr_rule = False

    for rule in rules:
        if not isinstance(rule, dict):
            raise Malformed("ruleset: a rule")
        kind = rule.get("type")
        params = rule.get("parameters")
        if kind == RULE_REQUIRED_LINEAR_HISTORY:
            linear = True
            continue
        if kind == RULE_REQUIRED_STATUS_CHECKS:
            if not isinstance(params, dict):
                raise Malformed("ruleset: required_status_checks")
            names = params.get("required_status_checks")
            if not isinstance(names, list):
                raise Malformed("ruleset: required_status_checks list")
            out = []
            for entry in names:
                if not isinstance(entry, dict):
                    raise Malformed("ruleset: a required status check")
                context = entry.get("context")
                if not isinstance(context, str) or not context:
                    raise Malformed("ruleset: a required status check context")
                out.append(context)
            checks = tuple(out)
            continue
        if kind != RULE_PULL_REQUEST:
            continue
        saw_pr_rule = True
        if not isinstance(params, dict):
            raise Malformed("ruleset: pull_request")
        count = params.get("required_approving_review_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise Malformed("ruleset: required_approving_review_count")
        approvals = count
        for field, target in (("dismiss_stale_reviews_on_push", "dismiss"),
                              ("require_last_push_approval", "last_push"),
                              ("require_code_owner_review", "code_owner")):
            value = params.get(field)
            if not isinstance(value, bool):
                raise Malformed(f"ruleset: {field}")
            if target == "dismiss":
                dismiss_stale = value
            elif target == "last_push":
                last_push = value
            else:
                code_owner = value
        allowed = params.get("allowed_merge_methods")
        if not isinstance(allowed, list):
            raise Malformed("ruleset: allowed_merge_methods")
        for method in allowed:
            if not isinstance(method, str) or not method:
                raise Malformed("ruleset: an allowed merge method")
        methods = tuple(allowed)

    if not saw_pr_rule:
        # A ruleset with no pull request rule holds no merge authority,
        # but it is a well-formed ruleset that simply is not the one the
        # configuration names. The caller checks `enforced` and the
        # absence of a pull request rule, so this is reported as zero
        # approvals rather than as malformed.
        approvals = 0

    return Ruleset(name, enforcement,
                   required_approving_review_count=approvals,
                   dismiss_stale_reviews_on_push=dismiss_stale,
                   require_last_push_approval=last_push,
                   require_code_owner_review=code_owner,
                   allowed_merge_methods=methods,
                   required_status_checks=checks,
                   linear_history=linear,
                   id=payload.get("id") if isinstance(payload.get("id"), int)
                   else None)


def find_ruleset(rulesets: list, name: str) -> Ruleset | None:
    """Return the ruleset with this name, or None.

    A repository may have several rulesets; the configuration names the
    one that holds the merge authority.
    """
    for payload in rulesets:
        if isinstance(payload, dict) and payload.get("name") == name:
            return parse_ruleset(payload)
    return None


def find_ruleset_checks(rulesets: list, name: str) -> Ruleset | None:
    """Return the ruleset with this name, read for its checks only.

    The check union is the one thing the shadow path needs from a
    ruleset, and it needs only two facts: whether the ruleset is
    enforced, and which status checks it requires. `parse_ruleset`
    refuses a ruleset whose *review* rule carries a field of the wrong
    type, and that refusal is right for `on` mode -- where the merge
    authority has to be read in full -- but it would abort a shadow
    decision over a field the shadow path never consults. A decision
    that is not recorded is a repository that is silently missing from
    the disagreement rate.

    So this reader is strict about the facts it uses and tolerant
    about the ones it does not: a `dismiss_stale_reviews_on_push` that
    is not a boolean is ignored here, and the required check names are
    still read. When the *checks* cannot be read the refusal is kept,
    because a ruleset whose required checks are unknown must not be
    treated as one that requires none: that would silently drop checks
    the ruleset does require, which is the fail-open direction.
    """
    for payload in rulesets:
        if not isinstance(payload, dict) or payload.get("name") != name:
            continue
        return Ruleset(payload.get("name", name),
                       _ruleset_enforcement(payload),
                       required_status_checks=_ruleset_check_names(payload))
    return None


def _ruleset_enforcement(payload: dict) -> str:
    """The enforcement state of a ruleset, or "" when it cannot be read.

    A ruleset whose enforcement cannot be read is treated as one that
    holds no authority: only an `active` ruleset contributes checks to
    the union, so an unreadable enforcement contributes none, and the
    refusal that matters -- the ruleset is not enforced -- is the one
    `on` mode makes.
    """
    enforcement = payload.get("enforcement")
    if not isinstance(enforcement, str):
        return ""
    return enforcement


def _ruleset_check_names(payload: dict) -> tuple:
    """The status check contexts a ruleset requires.

    The `required_status_checks` rule is read the way `parse_ruleset`
    reads it: a rule that is not a list, an entry that is not an
    object, or a context that is not a string all refuse. The check
    names are the fact the shadow path cannot do without, so an
    unreadable one is a refusal and not an empty answer.
    """
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise Malformed("ruleset: rules")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") != RULE_REQUIRED_STATUS_CHECKS:
            continue
        params = rule.get("parameters")
        if not isinstance(params, dict):
            raise Malformed("ruleset: required_status_checks")
        names = params.get("required_status_checks")
        if not isinstance(names, list):
            raise Malformed("ruleset: required_status_checks list")
        out = []
        for entry in names:
            if not isinstance(entry, dict):
                raise Malformed("ruleset: a required status check")
            context = entry.get("context")
            if not isinstance(context, str) or not context:
                raise Malformed("ruleset: a required status check context")
            out.append(context)
        return tuple(out)
    return ()


# ----- CODEOWNERS ----------------------------------------------------------

# A CODEOWNERS line: one or more patterns, then one or more owners.
# A `#` starts a comment. `!` negates a pattern, so a pattern may start
# with it. The patterns are gitignore-style, so a leading `/` anchors to
# the repository root and a pattern with no slash matches at any depth.
#
# The order is the file's: `pattern owner1 owner2`, which is what
# GitHub's own documentation uses and what every CODEOWNERS file in the
# wild is written in.
_CODEOWNER_LINE = re.compile(
    r"^\s*(?P<patterns>(?:!?[^\s#][^\s#]*\s*)+?)\s*"
    r"(?P<owners>(?:@[^\s#]+)(?:\s+@[^\s#]+)*)\s*$")


def parse_codeowners(text: str) -> list:
    """Read a CODEOWNERS file into (owners, pattern) pairs, in order.

    The last matching pattern wins, which is how CODEOWNERS itself
    reads: a later line overrides an earlier one for the same path.
    """
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _CODEOWNER_LINE.match(line)
        if not match:
            continue
        owners = match.group("owners").split()
        patterns = match.group("patterns").split()
        for pattern in patterns:
            out.append((tuple(owners), pattern))
    return out


def matches_pattern(path: str, pattern: str) -> bool:
    """True when a repository path is covered by a CODEOWNERS pattern.

    The rules are gitignore's, which is what CODEOWNERS uses:
      - a pattern with no slash matches at any depth (`*.py` matches
        `a/b/c.py`);
      - a pattern with a slash is anchored to the repository root;
      - a leading `!` negates, and the last matching pattern wins.
    """
    if pattern.startswith("!"):
        return False
    if pattern.endswith("/"):
        # A directory pattern covers everything under it.
        pattern = pattern.rstrip("/") + "/**"
    if "/" in pattern.strip("/") or pattern.startswith("/"):
        pattern = pattern.lstrip("/")
        if fnmatch.fnmatchcase(path, pattern):
            return True
        # `dir/*` in CODEOWNERS matches the directory's contents at any
        # depth below it, so `a/b.py` matches `dir/*`.
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            if path.startswith(prefix + "/"):
                return True
        return False
    # No slash: matches the basename at any depth.
    return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)


def owners_for(path: str, rules: list) -> tuple:
    """Return the owners that apply to a path, or ().

    The last matching pattern wins, and a negated pattern removes the
    owners an earlier pattern gave.
    """
    owners: tuple = ()
    for rule_owners, pattern in rules:
        if pattern.startswith("!"):
            if matches_pattern(path, pattern[1:]):
                owners = ()
            continue
        if matches_pattern(path, pattern):
            owners = rule_owners
    return owners


def protected_paths_hit(paths: list, rules: list, configured: tuple) -> list:
    """Return the changed paths that a repository protects.

    A path is protected when CODEOWNERS names an owner for it, or when
    it matches one of the repository's configured `protected_paths`.
    Both are the same question asked two ways: the configured patterns
    are the ones an operator has declared sensitive even when no
    CODEOWNERS file names them, and the CODEOWNERS rules are the ones
    the repository already carries.
    """
    hit = []
    for path in paths:
        if any(matches_pattern(path, pattern) for pattern in configured):
            hit.append(path)
            continue
        if owners_for(path, rules):
            hit.append(path)
    return hit


# ----- the decision --------------------------------------------------------

class Decision:
    """What to do about one pull request.

    `action` is one of:
      - `"enable"`: ask GitHub to enable auto-merge for `head`;
      - `"shadow"`: record the would-merge decision and leave the merge
        to a person;
      - `"block"`: refuse, and say why.
    """

    def __init__(self, action: str, head: str | None = None,
                 reason: str = "", method: str | None = None,
                 needs_merge: bool = False, protected: tuple = ()) -> None:
        self.action = action
        self.head = head
        self.reason = reason
        self.method = method
        self.needs_merge = needs_merge
        self.protected = tuple(protected)

    @property
    def line(self) -> str:
        """The one line a driver prints and a ledger records."""
        if self.action == "enable":
            return f"ENABLE {self.head} {self.method}"
        if self.action == "shadow":
            return f"SHADOW {self.head}"
        return f"BLOCK {self.reason}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Decision({self.line!r})"


def _block(reason: str, **kwargs) -> Decision:
    return Decision("block", reason=reason, **kwargs)


def decide(config: RepoConfig, pr: dict, reviews: list, checks: list,
           rulesets: list, codeowners_text: str | None,
           changed_paths: list, reviewer: str,
           ruleset: Ruleset | None = None,
           head: str | None = None,
           same_family: bool | None = None) -> Decision:
    """Decide what to do about one pull request.

    `pr` is the parsed `repos/R/pulls/N` object, `reviews` the fetched
    reviews, `checks` the head's check rollup, `rulesets` the raw
    ruleset objects, `codeowners_text` the CODEOWNERS file at the head
    (or None when there is none), and `changed_paths` the paths the pull
    request touches.

    `same_family` says whether the implementer and the reviewer run the
    same model family. It is None when the caller does not know, which
    is not the same as False: an unknown answer is an undecided
    question, and the configuration decides what to do about one.

    The order of the refusals matters. Each one names what is missing,
    because the operator's next action differs for each.
    """
    if head is None:
        head = _pr_head(pr)

    # A merged or closed pull request has nothing to decide.
    if pr.get("merged") is True:
        return _block("already merged")
    if pr.get("state") == "closed":
        return _block("already closed")

    # ----- the approval -----
    # `verdict_reviews` sorts by (submitted_at, id), so the last verdict
    # is the reviewer's latest one. GitHub returns reviews newest-first,
    # so taking the last of the list as returned would read the oldest
    # verdict as the current one.
    verdicts = verdict_reviews(reviews, reviewer)
    if not verdicts:
        return _block(f"no approving review from {reviewer}")
    verdict = verdicts[-1]
    if verdict["state"] != "APPROVED":
        return _block(f"the last review from {reviewer} asks for changes")
    if verdict["commit_id"] != head:
        # The approval is on another commit. This is the intervening
        # push: the head moved after the approval, so the approval says
        # nothing about what is on the head now.
        return _block(
            f"the approval is on {verdict['commit_id'][:7]}, not on the head "
            f"{head[:7]}")

    # ----- the ruleset -----
    #
    # Resolved before the checks, because the checks the decision
    # depends on are the ones the configuration names *and* the ones the
    # ruleset requires. The ruleset is the authority that finally
    # merges, so a check it requires is a check the decision has to
    # know about even when the configuration does not list it.
    #
    # The ruleset is only *required* in `on` mode. In shadow mode
    # nothing merges, so a missing ruleset costs nothing: the decision
    # is still recorded, with the method it knows, because the record
    # is what the disagreement rate is measured from -- and a
    # repository nobody configured is a repository whose rate has not
    # started yet, not one that is refused.
    #
    # The two modes read the ruleset differently, and the difference is
    # about how much of it each one needs. `on` mode validates the
    # merge authority in full, so it uses the strict reader and a
    # ruleset it cannot read is a refusal. Shadow mode needs only the
    # checks, so it uses the lenient one: a ruleset whose *review* rule
    # carries a field of the wrong type is still readable for the
    # checks it requires, and the decision is still recorded. Reading
    # it strictly there would cost the record -- and a repository
    # missing from the ledger is one whose disagreement rate is
    # measured as if it had never been considered.
    if ruleset is None and config.ruleset:
        if config.mode == ON:
            ruleset = find_ruleset(rulesets, config.ruleset)
        else:
            ruleset = find_ruleset_checks(rulesets, config.ruleset)

    # ----- the checks -----
    #
    # A check has to be green, not merely finished. A check run that
    # has finished and failed reports `status: "COMPLETED"` with
    # `conclusion: "FAILURE"`, so asking only whether it finished
    # would let a red pull request through.
    if pending_checks(checks):
        return _block("a required check has not finished")

    # The checks the decision depends on: the ones the configuration
    # names, and the ones the ruleset requires. A ruleset that requires
    # a check the configuration does not list still has to be
    # satisfied, because the ruleset is what will do the merging.
    #
    # Only an *enforced* ruleset contributes: `disabled` and
    # `evaluate` hold no authority, so a check such a ruleset names is
    # not one anybody is waiting for. The refusal that names the
    # unenforced ruleset is made further down, and it is the truer one.
    required = list(config.required_checks)
    if ruleset is not None and ruleset.enforced:
        for check in ruleset.required_status_checks:
            if check not in required:
                required.append(check)
    if required:
        # A check that is not in the rollup at all is one nobody has
        # reported; a check that is in it and did not pass is one that
        # says no. The two are different problems for the operator, so
        # they are named differently.
        reported = {c.get("name") or c.get("context")
                    for c in checks if isinstance(c, dict)}
        missing = [c for c in required if c not in reported]
        if missing:
            return _block(
                f"required check(s) not reported: {', '.join(missing)}")
        bad = [c for c in failing_checks(checks) if c in required]
        if bad:
            return _block(
                f"required check(s) did not pass: {', '.join(bad)}")

    # ----- the protected paths -----
    #
    # Before the mode branch, because the label is about the pull
    # request, not about the repository's ruleset: a pull request that
    # touches a protected path is one a person has to merge, whatever
    # else the repository's protections say. A repository whose ruleset
    # is missing must still label the pull request that touches a
    # workflow file.
    rules = parse_codeowners(codeowners_text) if codeowners_text else []
    hit = protected_paths_hit(changed_paths, rules, config.protected_paths)
    if hit:
        # The pull request is approved but must not be merged by the
        # loop: it touches a path the repository protects, so GitHub
        # will hold it for a code owner's review. The loop labels it and
        # leaves it to a person.
        return Decision("block",
                        reason=("touches a protected path: "
                                + ", ".join(sorted(hit))),
                        head=head, needs_merge=True, protected=tuple(hit))

    # ----- the protections -----
    #
    # The merge method and the ruleset are what `on` mode needs and
    # `shadow` mode does not. In `on` mode the ruleset is the authority
    # that will do the merging, so a repository whose ruleset is
    # missing or weaker than the issue asks for must not be enabled: an
    # unknown prerequisite is not a satisfied one.
    #
    # In `shadow` mode nothing merges, so a missing ruleset costs
    # nothing here. The decision is still recorded, with the method it
    # knows, because the record is what the disagreement rate is
    # measured from -- and a repository nobody configured is a
    # repository whose rate has not started yet, not one that is
    # refused.
    if config.mode == ON:
        if not config.merge_method:
            return _block("no merge_method is configured for this repository")
        if not config.ruleset:
            return _block("no ruleset is configured for this repository")

        # The ruleset was looked up above, where the checks could be
        # read against it. A repository that names a ruleset which does
        # not exist has protections that are unknown.
        if ruleset is None:
            return _block(
                f"the repository has no ruleset named {config.ruleset!r}")
        if not ruleset.enforced:
            return _block(
                f"the ruleset {ruleset.name!r} is {ruleset.enforcement!r}, "
                f"not enforced")

        # The ruleset must hold the merge authority the issue names:
        # one approving review, the most recent push approved, stale
        # approvals dismissed, the required checks, and linear history.
        if ruleset.required_approving_review_count < 1:
            return _block(
                f"the ruleset {ruleset.name!r} requires no approving review")
        if not ruleset.require_last_push_approval:
            return _block(
                f"the ruleset {ruleset.name!r} does not require the most "
                f"recent push to be approved")
        if not ruleset.dismiss_stale_reviews_on_push:
            return _block(
                f"the ruleset {ruleset.name!r} does not dismiss stale "
                f"approvals on push")
        if not ruleset.linear_history:
            return _block(
                f"the ruleset {ruleset.name!r} does not require linear "
                f"history")
        if not ruleset.required_status_checks:
            return _block(
                f"the ruleset {ruleset.name!r} names no required status "
                f"check")
        for check in config.required_checks:
            if check not in ruleset.required_status_checks:
                return _block(
                    f"the ruleset {ruleset.name!r} does not require the "
                    f"check {check!r}")
        if config.merge_method not in ruleset.allowed_merge_methods:
            allowed = ", ".join(ruleset.allowed_merge_methods) or "none"
            return _block(
                f"the ruleset {ruleset.name!r} allows {allowed}, not "
                f"{config.merge_method!r}")

    # ----- the same-family question -----
    # The issue asks whether a pull request may auto-merge when its
    # implementer and reviewer share a model family. No answer has been
    # given. An unanswered question is not a permission, so a repository
    # whose answer is not an explicit yes cannot enable auto-merge.
    # The check only bites in `on` mode: in shadow mode nothing merges,
    # so an undecided question costs nothing there.
    if config.mode == ON:
        if same_family is True and config.allow_same_family is not True:
            # The families match and the configuration does not permit
            # that. This is checked first, because it names the
            # condition that actually applies.
            if config.allow_same_family is None:
                return _block(
                    "the same-family implementer/reviewer question is "
                    "undecided, and the implementer and the reviewer share "
                    "a model family; set allow_same_family = true to "
                    "permit it, or leave the repository in shadow")
            return _block(
                "the implementer and the reviewer share a model family, and "
                "allow_same_family does not permit it")
        if config.allow_same_family is None:
            return _block(
                "the same-family implementer/reviewer question is undecided; "
                "set allow_same_family = true to permit auto-merge, or "
                "leave the repository in shadow")
        if config.allow_same_family is False:
            return _block(
                "the same-family implementer/reviewer question is answered "
                "no; auto-merge is not permitted for this repository")

    if config.mode == ON:
        return Decision("enable", head=head, method=config.merge_method)
    return Decision("shadow", head=head, method=config.merge_method)


def _pr_head(pr: dict) -> str:
    """Return the head sha of the pull request."""
    head = pr.get("head")
    if not isinstance(head, dict):
        raise Malformed("pulls: head")
    sha = head.get("sha")
    if not isinstance(sha, str) or not sha:
        raise Malformed("pulls: head.sha")
    return sha
