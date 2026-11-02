#!/usr/bin/env python3
"""Pin what pr-auto-merge decides, and what the merge ledger records.

The decision is asserted through `lib/auto_merge.py` and `lib/merge_ledger.py`
directly, and end to end through `bin/pr-auto-merge` with a stub `gh` on
PATH. No network.

The scenarios are the ones the issue names: an agreed pull request on a
repository whose protections are in force, a push after the approval, a
repository whose protections are missing or weaker than the issue asks
for, a pull request touching a path the repository protects, and shadow
mode, where nothing merges and the person's answer is recorded against
the loop's.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "bin" / "pr-auto-merge"
LEDGER_TOOL = REPO_ROOT / "bin" / "pr-merge-ledger"
CONFIG_TOOL = REPO_ROOT / "bin" / "pr-loop-config"

sys.path.insert(0, str(REPO_ROOT / "lib"))
import auto_merge  # noqa: E402
import merge_ledger  # noqa: E402

REVIEWER = "stickythistle"
IMPLEMENTER = "harbord-st"
REPO = "harbord-infra/docs"

HEAD = "1111111111111111111111111111111111111111"
HEAD2 = "2222222222222222222222222222222222222222"


# ----- builders -----------------------------------------------------------

def review(rid, state, commit, submitted, login=REVIEWER, body="a review"):
    return {
        "id": rid,
        "user": {"login": login, "type": "User"},
        "state": state,
        "commit_id": commit,
        "submitted_at": submitted,
        "body": body,
    }


def check_run(name="Gates", status="COMPLETED", conclusion: "str | None" = "SUCCESS"):
    return {"__typename": "CheckRun", "status": status,
            "conclusion": conclusion, "name": name}


def pr(head=HEAD, state="open", merged=False):
    return {"state": state, "merged": merged, "draft": False,
            "head": {"sha": head}, "base": {"sha": "0" * 40}}


def ruleset(name="loop-merge", enforcement="active", approvals=1,
            dismiss_stale=True, last_push=True, code_owner=True,
            methods=("squash", "rebase"), checks=("Gates",),
            linear=True, rule_id=7):
    """A ruleset carrying the merge authority the issue asks for."""
    rules = []
    if approvals is not None:
        rules.append({
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": approvals,
                "dismiss_stale_reviews_on_push": dismiss_stale,
                "require_last_push_approval": last_push,
                "require_code_owner_review": code_owner,
                "allowed_merge_methods": list(methods),
            },
        })
    if checks is not None:
        rules.append({
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": c} for c in checks],
                "strict_required_status_checks_policy": True,
            },
        })
    if linear:
        rules.append({"type": "required_linear_history", "parameters": {}})
    return {"id": rule_id, "name": name, "target": "branch",
            "source_type": "Repository", "source": REPO,
            "enforcement": enforcement, "rules": rules}


def config(mode="on", method: "str | None" = "squash",
           ruleset_name: "str | None" = "loop-merge",
           checks=("Gates",), protected=(), same_family: "bool | None" = True,
           codeowners="CODEOWNERS"):
    return auto_merge.RepoConfig(
        REPO, mode=mode, merge_method=method, ruleset=ruleset_name,
        required_checks=checks, protected_paths=protected,
        codeowners=codeowners, allow_same_family=same_family)


def decide(cfg, pr_obj=None, reviews=None, checks=None, rulesets=None,
           codeowners=None, paths=None, reviewer=REVIEWER,
           same_family=None, protected=()):
    """Call the pure decision with the safe defaults every scenario
    starts from: an approved head, green checks, an enforced ruleset."""
    if protected:
        # The protected paths live on the configuration, so a scenario
        # that names them changes the configuration it decides with.
        cfg = auto_merge.RepoConfig(
            cfg.repo, mode=cfg.mode, merge_method=cfg.merge_method,
            ruleset=cfg.ruleset, required_checks=cfg.required_checks,
            protected_paths=tuple(protected), codeowners=cfg.codeowners,
            allow_same_family=cfg.allow_same_family)
    return auto_merge.decide(
        cfg,
        pr_obj if pr_obj is not None else pr(),
        reviews if reviews is not None else [review(1, "APPROVED", HEAD, "2026-10-20T00:00:00Z")],
        checks if checks is not None else [check_run()],
        rulesets if rulesets is not None else [ruleset()],
        codeowners,
        paths if paths is not None else [],
        reviewer,
        head=(pr_obj or pr()).get("head", {}).get("sha"),
        same_family=same_family,
    )


# ----- the decision scenarios ---------------------------------------------

def decision_scenarios():
    """(name, kwargs, expected action, expected substring in the reason)."""
    out = []

    # The happy path: a repository in `on`, everything in force.
    out.append(("on_enables", dict(cfg=config(mode="on")),
                "enable", ""))

    # A repository in `on` that has no merge method configured. The
    # merge method is how the repository says which way it merges.
    out.append(("no_merge_method", dict(cfg=config(mode="on", method=None)),
                "block", "no merge_method"))

    # A repository in `on` that names no ruleset. The protections are
    # unknown, and an unknown prerequisite is not a satisfied one.
    out.append(("no_ruleset_named", dict(cfg=config(mode="on", ruleset_name=None)),
                "block", "no ruleset"))

    # The ruleset the configuration names does not exist.
    out.append(("ruleset_absent", dict(cfg=config(mode="on"),
                                       rulesets=[ruleset(name="other")]),
                "block", "no ruleset named"))

    # The ruleset exists but is not enforced. `evaluate` is GitHub's
    # dry-run mode: the rules are reported, not applied.
    out.append(("ruleset_disabled",
                dict(cfg=config(mode="on"),
                     rulesets=[ruleset(enforcement="disabled")]),
                "block", "not enforced"))
    out.append(("ruleset_evaluate",
                dict(cfg=config(mode="on"),
                     rulesets=[ruleset(enforcement="evaluate")]),
                "block", "not enforced"))

    # The ruleset requires no approving review.
    out.append(("ruleset_no_approvals",
                dict(cfg=config(mode="on"),
                     rulesets=[ruleset(approvals=0)]),
                "block", "requires no approving review"))

    # The ruleset does not require the most recent push to be approved.
    out.append(("ruleset_no_last_push",
                dict(cfg=config(mode="on"),
                     rulesets=[ruleset(last_push=False)]),
                "block", "most recent push"))

    # The ruleset does not dismiss stale approvals on push, so an
    # approval survives a push and means nothing about the new head.
    out.append(("ruleset_no_dismiss_stale",
                dict(cfg=config(mode="on"),
                     rulesets=[ruleset(dismiss_stale=False)]),
                "block", "dismiss stale approvals"))

    # The ruleset does not require linear history.
    out.append(("ruleset_not_linear",
                dict(cfg=config(mode="on"),
                     rulesets=[ruleset(linear=False)]),
                "block", "linear history"))

    # The ruleset names no required status check.
    out.append(("ruleset_no_checks",
                dict(cfg=config(mode="on"),
                     rulesets=[ruleset(checks=())]),
                "block", "no required status check"))

    # The configuration asks for a check the ruleset does not require.
    # The configuration believes it is protected where it is not.
    out.append(("check_not_in_ruleset",
                dict(cfg=config(mode="on", checks=("Gates", "Extra")),
                     checks=[check_run(), check_run(name="Extra")],
                     rulesets=[ruleset(checks=("Gates",))]),
                "block", "does not require the check"))

    # The ruleset does not allow the configured merge method.
    out.append(("method_not_allowed",
                dict(cfg=config(mode="on", method="squash"),
                     rulesets=[ruleset(methods=("rebase",))]),
                "block", "allows rebase"))

    # No approval at all.
    out.append(("no_review", dict(cfg=config(mode="on"), reviews=[]),
                "block", "no approving review"))

    # The last review asks for changes.
    out.append(("changes_requested",
                dict(cfg=config(mode="on"),
                     reviews=[review(1, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z")]),
                "block", "asks for changes"))

    # The reviews arrive newest-first, as GitHub returns them. The
    # latest verdict is the one the reviewer left last, not the one that
    # happens to be last in the list: reading the list as it arrives
    # would take the oldest verdict for the current one.
    out.append(("newest_first_ordering",
                dict(cfg=config(mode="on"),
                     reviews=[
                         review(2, "CHANGES_REQUESTED", HEAD, "2026-10-21T00:00:00Z"),
                         review(1, "APPROVED", HEAD, "2026-10-20T00:00:00Z"),
                     ]),
                "block", "asks for changes"))

    # The same ordering, with the approval genuinely the later verdict:
    # the approval is the current one, whichever position it arrives in.
    out.append(("newest_first_approval_last",
                dict(cfg=config(mode="on"),
                     reviews=[
                         review(2, "CHANGES_REQUESTED", HEAD, "2026-10-20T00:00:00Z"),
                         review(1, "APPROVED", HEAD, "2026-10-21T00:00:00Z"),
                     ]),
                "enable", ""))

    # The intervening push: the approval is on the old head.
    out.append(("intervening_push",
                dict(cfg=config(mode="on"),
                     pr_obj=pr(head=HEAD2),
                     reviews=[review(1, "APPROVED", HEAD, "2026-10-20T00:00:00Z")]),
                "block", "not on the head"))

    # A check has not finished.
    out.append(("check_running",
                dict(cfg=config(mode="on"),
                     checks=[check_run(status="IN_PROGRESS", conclusion=None)]),
                "block", "has not finished"))

    # The required check is not reported at all.
    out.append(("check_missing",
                dict(cfg=config(mode="on"), checks=[check_run(name="Other")]),
                "block", "not reported"))

    # The pull request touches a path the repository protects.
    out.append(("protected_path",
                dict(cfg=config(mode="on", protected=(".github/workflows/",)),
                     paths=[".github/workflows/gates.yaml"]),
                "block", "protected path"))

    # The same path, but the protection is CODEOWNERS rather than the
    # configured list.
    out.append(("codeowner_path",
                dict(cfg=config(mode="on"),
                     codeowners="/.github/workflows/ @a-person\n",
                     paths=[".github/workflows/gates.yaml"]),
                "block", "protected path"))

    # A protected path is labelled whatever the repository's ruleset
    # says. The label is about the pull request, not about the
    # repository's protections: a pull request that touches a workflow
    # file is one a person has to merge even when the ruleset that
    # would have held it is missing.
    out.append(("protected_path_without_ruleset",
                dict(cfg=config(mode="on", ruleset_name=None),
                     protected=(".github/workflows/",),
                     paths=[".github/workflows/gates.yaml"]),
                "block", "protected path"))

    # A protected path in shadow mode is labelled too.
    out.append(("protected_path_in_shadow",
                dict(cfg=config(mode="shadow"),
                     protected=(".github/workflows/",),
                     paths=[".github/workflows/gates.yaml"]),
                "block", "protected path"))

    # A path nobody protects is not a reason to refuse.
    out.append(("unprotected_path",
                dict(cfg=config(mode="on"),
                     codeowners="/other/ @a-person\n",
                     paths=["src/main.rs"]),
                "enable", ""))

    # Shadow mode: the decision is recorded, nothing is enabled.
    out.append(("shadow_records",
                dict(cfg=config(mode="shadow")), "shadow", ""))

    # Shadow mode does not need a ruleset: nothing merges, so a missing
    # ruleset costs nothing. The decision is still recorded, because the
    # record is what the disagreement rate is measured from.
    out.append(("shadow_needs_no_ruleset",
                dict(cfg=config(mode="shadow", ruleset_name=None)),
                "shadow", ""))

    # The same-family question, undecided. In `on` mode that refuses.
    out.append(("same_family_undecided",
                dict(cfg=config(mode="on", same_family=None)),
                "block", "undecided"))

    # The same-family question, answered no.
    out.append(("same_family_no",
                dict(cfg=config(mode="on", same_family=False)),
                "block", "answered no"))

    # The implementer and reviewer share a family and the configuration
    # does not permit it.
    out.append(("same_family_true_but_not_allowed",
                dict(cfg=config(mode="on", same_family=False),
                     same_family=True),
                "block", "share a model family"))

    # The same-family question answered yes, and the families differ.
    out.append(("same_family_allowed",
                dict(cfg=config(mode="on", same_family=True),
                     same_family=False),
                "enable", ""))

    # In shadow mode the same-family question costs nothing: nothing
    # merges, so an undecided question cannot cause a wrong merge.
    out.append(("shadow_same_family_undecided",
                dict(cfg=config(mode="shadow", same_family=None)),
                "shadow", ""))

    return out


def check_decision_scenarios(failures):
    for name, kwargs, want_action, want_text in decision_scenarios():
        try:
            got = decide(**kwargs)
        except Exception as exc:  # noqa: BLE001 - the point is to report it
            failures.append(f"{name}: decide() raised {exc!r}")
            continue
        if got.action != want_action:
            failures.append(
                f"{name}: decide() gave action {got.action!r}, "
                f"expected {want_action!r} ({got.line})")
            continue
        if want_text and want_text not in got.reason:
            failures.append(
                f"{name}: the reason {got.reason!r} does not name "
                f"{want_text!r}")
        if want_action == "enable":
            if got.head != HEAD:
                failures.append(
                    f"{name}: the decision names head {got.head!r}, "
                    f"expected {HEAD!r}")
            if got.method != "squash":
                failures.append(
                    f"{name}: the decision names method {got.method!r}, "
                    f"expected 'squash'")


# ----- the ruleset reader -------------------------------------------------

def check_ruleset_reader(failures):
    """A ruleset whose fields are missing is unreadable, and an
    unreadable rule protects nothing."""
    cases = [
        ("no name", {"enforcement": "active", "rules": []}, "name"),
        ("no enforcement", {"name": "r", "rules": []}, "enforcement"),
        ("rules not a list", {"name": "r", "enforcement": "active",
                              "rules": {}}, "rules"),
        ("rule not a dict", {"name": "r", "enforcement": "active",
                             "rules": ["x"]}, "a rule"),
        ("approval count missing",
         {"name": "r", "enforcement": "active",
          "rules": [{"type": "pull_request", "parameters": {}}]},
         "required_approving_review_count"),
        ("approval count a string",
         {"name": "r", "enforcement": "active",
          "rules": [{"type": "pull_request",
                     "parameters": {"required_approving_review_count": "1"}}]},
         "required_approving_review_count"),
        ("dismiss flag missing",
         {"name": "r", "enforcement": "active",
          "rules": [{"type": "pull_request",
                     "parameters": {"required_approving_review_count": 1}}]},
         "dismiss_stale_reviews_on_push"),
        ("status check context missing",
         {"name": "r", "enforcement": "active",
          "rules": [{"type": "required_status_checks",
                     "parameters": {"required_status_checks": [{}]}}]},
         "context"),
    ]
    for label, payload, expected in cases:
        try:
            auto_merge.parse_ruleset(payload)
            failures.append(f"ruleset {label}: parse_ruleset accepted it")
        except auto_merge.Malformed as exc:
            if expected not in str(exc):
                failures.append(
                    f"ruleset {label}: the error {exc!s} does not name "
                    f"{expected!r}")

    # A well-formed ruleset reads out what it holds.
    parsed = auto_merge.parse_ruleset(ruleset())
    if parsed.required_approving_review_count != 1:
        failures.append("the ruleset reader lost the approval count")
    if not parsed.dismiss_stale_reviews_on_push:
        failures.append("the ruleset reader lost dismiss_stale_reviews_on_push")
    if not parsed.require_last_push_approval:
        failures.append("the ruleset reader lost require_last_push_approval")
    if not parsed.linear_history:
        failures.append("the ruleset reader lost linear history")
    if parsed.required_status_checks != ("Gates",):
        failures.append("the ruleset reader lost the required checks")

    # A ruleset with no pull request rule holds no merge authority. It
    # is well-formed, so it is not malformed -- it simply is not the
    # ruleset the configuration named.
    other = auto_merge.parse_ruleset(
        {"name": "other", "enforcement": "active",
         "rules": [{"type": "required_linear_history", "parameters": {}}]})
    if other.required_approving_review_count != 0:
        failures.append(
            "a ruleset with no pull_request rule reported an approval count")


# ----- the configuration reader -------------------------------------------

def check_config_reader(failures):
    doc = {
        "owner/repo": {
            "auto_merge": "on",
            "merge_method": "rebase",
            "ruleset": "loop-merge",
            "required_checks": ["Gates"],
            "protected_paths": ["gates.toml"],
        },
    }
    cfg = auto_merge.parse_config("owner/repo", doc)
    if cfg.mode != "on" or cfg.merge_method != "rebase":
        failures.append("the configuration reader lost a field")
    if cfg.required_checks != ("Gates",):
        failures.append("the configuration reader lost required_checks")
    if cfg.protected_paths != ("gates.toml",):
        failures.append("the configuration reader lost protected_paths")
    if cfg.allow_same_family is not None:
        failures.append(
            "a configuration that says nothing about allow_same_family "
            "invented an answer")

    # A repository with no table gets the safe defaults.
    missing = auto_merge.parse_config("owner/other", doc)
    if missing.mode != auto_merge.SHADOW:
        failures.append("an unconfigured repository is not in shadow mode")
    if missing.merge_method is not None:
        failures.append("an unconfigured repository has a merge method")
    if missing.ruleset is not None:
        failures.append("an unconfigured repository names a ruleset")

    bad = [
        ("bad mode", {"auto_merge": "maybe"}, "auto_merge"),
        ("bad method", {"merge_method": "merge"}, "merge_method"),
        ("empty ruleset", {"ruleset": "  "}, "ruleset"),
        ("bad checks", {"required_checks": [1]}, "required_checks"),
        ("bad paths", {"protected_paths": [""]}, "protected_paths"),
        ("bad same family", {"allow_same_family": "yes"},
         "allow_same_family"),
        ("not a table", [], "not a table"),
    ]
    for label, table, expected in bad:
        try:
            auto_merge.parse_config("owner/repo", {"owner/repo": table})
            failures.append(f"configuration {label}: accepted it")
        except auto_merge.ConfigError as exc:
            if expected not in str(exc):
                failures.append(
                    f"configuration {label}: the error {exc!s} does not name "
                    f"{expected!r}")

    # A table that is not named owner/name is refused: it is either a
    # typo in a repository name or a heading the file was never meant
    # to carry.
    for name in ("settings", "owner", "/repo", "owner/", ""):
        try:
            auto_merge.parse_config(name, {})
            failures.append(
                f"configuration {name!r}: accepted a table that is not a "
                f"repository name")
        except auto_merge.ConfigError as exc:
            if "owner/name" not in str(exc):
                failures.append(
                    f"configuration {name!r}: the error {exc!s} does not "
                    f"say the name must be owner/name")

    # The example file is the file an operator copies, so it has to
    # parse and every repository in it has to be valid. A broken
    # example is a broken first step.
    example = REPO_ROOT / "pr-loop.toml.example"
    try:
        with example.open("rb") as fh:
            doc = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001 - the point is to report it
        failures.append(f"pr-loop.toml.example does not parse: {exc!r}")
        doc = {}
    for name in doc:
        try:
            config = auto_merge.parse_config(name, doc)
            if config.mode != auto_merge.SHADOW:
                failures.append(
                    f"pr-loop.toml.example puts {name} in "
                    f"{config.mode!r}; no repository starts in 'on'")
        except auto_merge.ConfigError as exc:
            failures.append(
                f"pr-loop.toml.example: {exc}")
    if not doc:
        failures.append("pr-loop.toml.example configures no repository")


# ----- CODEOWNERS ---------------------------------------------------------

def check_codeowners(failures):
    text = (
        "# a comment\n"
        "\n"
        "/docs/ @docs-owner\n"
        "/.github/workflows/ @a-person\n"
        "*.sql @dba\n"
        "/secrets/ @security\n"
    )
    rules = auto_merge.parse_codeowners(text)
    if auto_merge.owners_for(".github/workflows/gates.yaml", rules) != ("@a-person",):
        failures.append(
            "CODEOWNERS: a workflow path did not find its owner")
    if auto_merge.owners_for("db/schema.sql", rules) != ("@dba",):
        failures.append("CODEOWNERS: a *.sql path did not find its owner")
    if auto_merge.owners_for("README.md", rules) != ():
        failures.append("CODEOWNERS: an unowned path found an owner")
    # A later line wins, which is how CODEOWNERS reads.
    rules2 = auto_merge.parse_codeowners("/a/ @one\n/a/b/ @two\n")
    if auto_merge.owners_for("a/b/c.txt", rules2) != ("@two",):
        failures.append("CODEOWNERS: the last matching pattern did not win")
    # A negated pattern removes the owners.
    rules3 = auto_merge.parse_codeowners("/a/ @one\n!/a/b/ @two\n")
    if auto_merge.owners_for("a/b/c.txt", rules3) != ():
        failures.append("CODEOWNERS: a negated pattern did not remove the owner")


# ----- the merge ledger ---------------------------------------------------

def check_ledger(failures):
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "merges.jsonl"

        # A decision is recorded once. The same decision again is a
        # no-op, so a rerun does not append a second line.
        if not merge_ledger.record_decision(path, REPO, 42, HEAD, "shadow",
                                             "squash"):
            failures.append("the ledger did not record a first decision")
        if merge_ledger.record_decision(path, REPO, 42, HEAD, "shadow",
                                        "squash"):
            failures.append("the ledger recorded the same decision twice")

        # The person merges the head the loop agreed on: agreement.
        merge_ledger.record_outcome(path, REPO, 42, HEAD, "merged", "a-person")
        stats = merge_ledger.disagreement_rate(path)
        if stats["answered"] != 1 or stats["agreed"] != 1:
            failures.append(
                f"the ledger counted a merge as {stats}; expected one "
                f"agreement")
        if stats["rate"] != 0.0:
            failures.append("the ledger's rate is not zero for one agreement")

        # The person asks for changes: disagreement.
        merge_ledger.record_decision(path, REPO, 43, HEAD, "shadow", "squash")
        merge_ledger.record_outcome(path, REPO, 43, HEAD,
                                    "changes_requested", "a-person")
        stats = merge_ledger.disagreement_rate(path)
        if stats["disagreed"] != 1:
            failures.append(
                f"the ledger counted a change request as {stats}; expected "
                f"one disagreement")

        # A decision nobody answered is not agreement. Counting it as
        # agreement would make the rate fall as the loop got slower.
        merge_ledger.record_decision(path, REPO, 44, HEAD, "shadow", "squash")
        stats = merge_ledger.disagreement_rate(path)
        if stats["answered"] != 2:
            failures.append(
                f"an unanswered decision was counted as answered: {stats}")
        if stats["unanswered"] != 1:
            failures.append(f"an unanswered decision was not counted: {stats}")

        # A person who merged something other than the agreed head has
        # disagreed, and the ledger has to be able to say so.
        merge_ledger.record_decision(path, REPO, 45, HEAD, "shadow", "squash")
        merge_ledger.record_outcome(path, REPO, 45, HEAD2, "merged", "a-person")
        pairs = [p for p in merge_ledger.reconcile(path)
                 if p["pr"] == "45"]
        if not pairs:
            failures.append("the ledger lost the decision about PR 45")
        elif pairs[0]["agrees"]:
            failures.append(
                "the ledger counted a merge of another head as agreement")

        # A malformed line is skipped, not fatal.
        with path.open("a") as fh:
            fh.write("not json at all\n")
            fh.write('{"repo": "x"}\n')
        entries = merge_ledger.load(path)
        if any(e.get("head") == HEAD2 and e.get("decision") == "would_merge"
               for e in entries):
            failures.append("a malformed line was read as a decision")
        if len(entries) < 4:
            failures.append("a malformed line cost the ledger its good rows")

        # An outcome with an unknown name is refused.
        try:
            merge_ledger.record_outcome(path, REPO, 46, HEAD, "approved")
            failures.append("the ledger accepted an unknown outcome")
        except ValueError:
            pass

        # The one-line summary names what the ledger holds. It reads
        # every key it prints, so a typo in one is a crash rather than a
        # wrong number. The call is guarded so a crash there is
        # reported as a failure and the rest of the suite still runs.
        try:
            text = merge_ledger.summary(path)
        except Exception as exc:  # noqa: BLE001 - the point is to report it
            failures.append(f"the ledger's summary raised {exc!r}")
            text = ""
        for word in ("decision(s)", "answered", "agreed", "disagreed",
                     "unanswered", "disagreement rate"):
            if word not in text:
                failures.append(
                    f"the ledger's summary does not name {word!r}: {text!r}")

        # An empty ledger has a summary too, and no rate: nothing has
        # been decided, so there is no disagreement to report.
        empty = Path(t) / "empty.jsonl"
        stats = merge_ledger.disagreement_rate(empty)
        if stats["decisions"] != 0 or stats["rate"] is not None:
            failures.append(f"an empty ledger reported {stats}")
        if "unknown" not in merge_ledger.summary(empty):
            failures.append(
                "the summary of an empty ledger does not say the rate is "
                "unknown")


# ----- end to end through the driver --------------------------------------

STUB = r"""#!/usr/bin/env bash
# A stub `gh` that answers from files in $STUB_DATA.
printf '%s\n' "$*" >> "$STUB_DATA/gh.log"
api() {
  endpoint=""
  method=""
  jq=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -X) method="$2"; shift 2 ;;
      --jq) jq="$2"; shift 2 ;;
      --paginate|--slurp) shift ;;
      -*) shift ;;
      *) if [ -z "$endpoint" ]; then endpoint="$1"; fi; shift ;;
    esac
  done
  case "$endpoint" in
    */rulesets) cat "$STUB_DATA/rulesets.json"; exit 0 ;;
    */pulls/*/files) cat "$STUB_DATA/files.json"; exit 0 ;;
    */pulls/*/auto-merge) cat "$STUB_DATA/auto_merge.json"; exit 0 ;;
    */pulls/*/reviews) cat "$STUB_DATA/reviews.json"; exit 0 ;;
    */contents/*)
      if [ -n "$jq" ]; then
        python3 -c 'import json,sys
data = json.load(open(sys.argv[1]))
expr = sys.argv[2]
if expr == ".content":
    print(data.get("content", ""))
else:
    print(data)
' "$STUB_DATA/contents.json" "$jq"
      else
        cat "$STUB_DATA/contents.json"
      fi
      exit 0 ;;
    */pulls/*)
      # The pull request is read twice: once for the decision, and once
      # again immediately before anything is written. When
      # $STUB_DATA/pr_moved.json exists, the second read answers with
      # it -- a head that moved while the decision was being made.
      if [ -f "$STUB_DATA/pr_moved.json" ]; then
        if [ -f "$STUB_DATA/pr_read" ]; then
          cat "$STUB_DATA/pr_moved.json"
        else
          touch "$STUB_DATA/pr_read"
          cat "$STUB_DATA/pr.json"
        fi
      else
        cat "$STUB_DATA/pr.json"
      fi
      exit 0 ;;
    *) echo "gh: unexpected endpoint $endpoint" >&2; exit 1 ;;
  esac
}
case "$1" in
  api) shift; api "$@" ;;
  pr)
    case "$2" in
      view) cat "$STUB_DATA/checks.json" ;;
      edit) cat "$STUB_DATA/label.json" ;;
      *) echo "gh: unexpected pr subcommand $2" >&2; exit 1 ;;
    esac
    ;;
  *) echo "gh: unexpected command $1" >&2; exit 1 ;;
esac
"""


def write_stub_gh(bin_dir: Path) -> None:
    gh = bin_dir / "gh"
    gh.write_text(STUB)
    gh.chmod(0o755)


def install(data_dir: Path, pr_obj, reviews, checks, rulesets, files,
            contents=None, auto_merge_response=None):
    (data_dir / "pr.json").write_text(json.dumps(pr_obj))
    (data_dir / "reviews.json").write_text(json.dumps([reviews]))
    (data_dir / "checks.json").write_text(
        json.dumps({"statusCheckRollup": checks}))
    # The rulesets endpoint is read with `--paginate --slurp`, so the
    # stub answers with the pages wrapped in an outer array.
    (data_dir / "rulesets.json").write_text(json.dumps([rulesets]))
    (data_dir / "files.json").write_text(json.dumps(files))
    # The contents endpoint answers with base64, as GitHub does.
    if contents is None:
        payload = {"content": ""}
    elif isinstance(contents, str):
        payload = {"content": base64.b64encode(contents.encode()).decode()}
    else:
        payload = contents
    (data_dir / "contents.json").write_text(json.dumps(payload))
    (data_dir / "auto_merge.json").write_text(
        json.dumps(auto_merge_response
                   if auto_merge_response is not None
                   else {"auto_merge": {"enabled_by": {"login": REVIEWER}}}))
    (data_dir / "label.json").write_text(json.dumps({}))


def run_driver(data_dir: Path, bin_dir: Path, *extra: str,
               config_path: Path | None = None, ledger: Path | None = None,
               env_extra: dict | None = None):
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_DATA"] = str(data_dir)
    env["REVIEWER_LOGIN"] = REVIEWER
    env.pop("IMPLEMENTER_LOGINS", None)
    if config_path is not None:
        env["PR_LOOP_CONFIG"] = str(config_path)
    if ledger is not None:
        env["MERGE_LEDGER"] = str(ledger)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(DRIVER), REPO, "42", *extra],
        capture_output=True, text=True, env=env,
    )


def write_config(path: Path, **kwargs):
    lines = []
    for key, value in kwargs.items():
        if isinstance(value, list):
            rendered = ", ".join(f'"{v}"' for v in value)
            lines.append(f"{key} = [{rendered}]")
        elif isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif value is None:
            continue
        else:
            lines.append(f'{key} = "{value}"')
    path.write_text(f'["{REPO}"]\n' + "\n".join(lines) + "\n")


def check_driver(failures):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        write_stub_gh(bin_dir)

        good_reviews = [review(1, "APPROVED", HEAD, "2026-10-20T00:00:00Z")]

        # ---- `on` mode enables auto-merge through GitHub ----
        data = tmp / "on"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [ruleset()], [])
        config_path = tmp / "on.toml"
        write_config(config_path, auto_merge="on", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"],
                     allow_same_family=True)
        ledger = tmp / "on-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=config_path, ledger=ledger)
        if r.returncode != 0:
            failures.append(
                f"the driver exited {r.returncode} in on mode: "
                f"{r.stderr.strip()!r}")
        elif not r.stdout.startswith("ENABLE"):
            failures.append(f"the driver printed {r.stdout.strip()!r}")
        # The write went to GitHub's auto-merge endpoint, not to the
        # merge endpoint. Nothing in this repository merges a pull
        # request.
        log = (data / "gh.log").read_text()
        if "auto-merge" not in log:
            failures.append("the driver did not call the auto-merge endpoint")
        if "/merge" in log and "auto-merge" not in log:
            failures.append("the driver called the merge endpoint")
        if ledger.exists():
            entries = merge_ledger.load(ledger)
            if not entries or entries[0].get("decision") != "would_merge":
                failures.append("the driver recorded no would-merge decision")

        # ---- shadow mode merges nothing and records the decision ----
        data = tmp / "shadow"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [ruleset()], [])
        config_path = tmp / "shadow.toml"
        write_config(config_path, auto_merge="shadow", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"])
        ledger = tmp / "shadow-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=config_path, ledger=ledger)
        if r.returncode != 0:
            failures.append(
                f"the driver exited {r.returncode} in shadow mode: "
                f"{r.stderr.strip()!r}")
        elif not r.stdout.startswith("SHADOW"):
            failures.append(f"the driver printed {r.stdout.strip()!r}")
        log = (data / "gh.log").read_text()
        if "auto-merge" in log:
            failures.append("the driver enabled auto-merge in shadow mode")
        entries = merge_ledger.load(ledger) if ledger.exists() else []
        if not entries:
            failures.append("the driver recorded no shadow decision")
        elif entries[0].get("mode") != "shadow":
            failures.append("the shadow decision did not record the mode")

        # ---- a protected path is labelled and not merged ----
        data = tmp / "protected"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [ruleset()],
                [{"filename": ".github/workflows/gates.yaml"}])
        config_path = tmp / "protected.toml"
        write_config(config_path, auto_merge="on", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"],
                     protected_paths=[".github/workflows/"],
                     allow_same_family=True)
        ledger = tmp / "protected-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=config_path, ledger=ledger)
        if r.returncode != 1:
            failures.append(
                f"the driver exited {r.returncode} for a protected path; "
                f"expected 1")
        if "protected path" not in r.stdout:
            failures.append(
                f"the driver printed {r.stdout.strip()!r} for a protected path")
        log = (data / "gh.log").read_text()
        if "auto-merge" in log:
            failures.append("the driver enabled auto-merge on a protected path")
        if "pr-loop:needs-merge" not in log:
            failures.append(
                "the driver did not label a protected pull request "
                "pr-loop:needs-merge")

        # ---- a protected path is labelled even without a ruleset ----
        #
        # The label is about the pull request, not about the
        # repository's protections. A pull request that touches a
        # workflow file is one a person has to merge, whatever the
        # repository's ruleset says -- and a repository whose ruleset is
        # missing must still say so on the pull request.
        data = tmp / "protected-noruleset"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [],
                [{"filename": ".github/workflows/gates.yaml"}])
        config_path = tmp / "protected-noruleset.toml"
        write_config(config_path, auto_merge="on", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"],
                     protected_paths=[".github/workflows/"],
                     allow_same_family=True)
        ledger = tmp / "protected-noruleset-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=config_path, ledger=ledger)
        if r.returncode != 1:
            failures.append(
                f"the driver exited {r.returncode} for a protected path on "
                f"a repository with no ruleset; expected 1")
        log = (data / "gh.log").read_text()
        if "pr-loop:needs-merge" not in log:
            failures.append(
                "the driver did not label a protected pull request "
                "pr-loop:needs-merge on a repository with no ruleset")

        # ---- an intervening push stops the merge ----
        data = tmp / "pushed"
        data.mkdir()
        # The first read says HEAD; the re-read before the write says
        # HEAD2. The head moved while the decision was being made.
        install(data, pr(head=HEAD2), good_reviews, [check_run()], [ruleset()],
                [])
        config_path = tmp / "pushed.toml"
        write_config(config_path, auto_merge="on", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"],
                     allow_same_family=True)
        ledger = tmp / "pushed-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=config_path, ledger=ledger)
        if r.returncode != 1:
            failures.append(
                f"the driver exited {r.returncode} for an approval on "
                f"another head; expected 1")
        log = (data / "gh.log").read_text()
        if "auto-merge" in log:
            failures.append("the driver enabled auto-merge on a stale head")

        # ---- the head moves between the decision and the write ----
        #
        # The approval is on the head the decision was made about, but a
        # push lands before anything is written. The driver re-reads the
        # pull request immediately before the write and refuses, because
        # enabling auto-merge for a commit nobody agreed to is the one
        # thing this tool must never do.
        data = tmp / "moved"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [ruleset()], [])
        (data / "pr_moved.json").write_text(json.dumps(pr(head=HEAD2)))
        config_path = tmp / "moved.toml"
        write_config(config_path, auto_merge="on", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"],
                     allow_same_family=True)
        ledger = tmp / "moved-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=config_path, ledger=ledger)
        if r.returncode != 1:
            failures.append(
                f"the driver exited {r.returncode} when the head moved "
                f"while it was deciding; expected 1")
        if "head moved" not in (r.stdout + r.stderr):
            failures.append(
                f"the driver did not say the head moved: "
                f"{r.stdout.strip()!r} {r.stderr.strip()!r}")
        log = (data / "gh.log").read_text()
        if "auto-merge" in log:
            failures.append(
                "the driver enabled auto-merge after the head moved")

        # ---- a repository with no protections never merges ----
        data = tmp / "unprotected"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [], [])
        config_path = tmp / "unprotected.toml"
        write_config(config_path, auto_merge="on", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"],
                     allow_same_family=True)
        ledger = tmp / "unprotected-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=config_path, ledger=ledger)
        if r.returncode != 1:
            failures.append(
                f"the driver exited {r.returncode} for a repository with no "
                f"ruleset; expected 1")
        log = (data / "gh.log").read_text()
        if "auto-merge" in log:
            failures.append(
                "the driver enabled auto-merge on a repository with no "
                "ruleset")

        # ---- an unconfigured repository is in shadow mode ----
        data = tmp / "unconfigured"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [ruleset()], [])
        ledger = tmp / "unconfigured-ledger.jsonl"
        r = run_driver(data, bin_dir, config_path=tmp / "absent.toml",
                       ledger=ledger)
        if r.returncode != 0:
            failures.append(
                f"the driver exited {r.returncode} for an unconfigured "
                f"repository: {r.stderr.strip()!r}")
        elif not r.stdout.startswith("SHADOW"):
            failures.append(
                f"an unconfigured repository gave {r.stdout.strip()!r}")
        log = (data / "gh.log").read_text()
        if "auto-merge" in log:
            failures.append(
                "the driver enabled auto-merge on an unconfigured repository")

        # ---- --check changes nothing ----
        data = tmp / "check"
        data.mkdir()
        install(data, pr(), good_reviews, [check_run()], [ruleset()], [])
        config_path = tmp / "check.toml"
        write_config(config_path, auto_merge="on", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"],
                     allow_same_family=True)
        ledger = tmp / "check-ledger.jsonl"
        r = run_driver(data, bin_dir, "--check", config_path=config_path,
                       ledger=ledger)
        if r.returncode != 0:
            failures.append(
                f"--check exited {r.returncode}: {r.stderr.strip()!r}")
        log = (data / "gh.log").read_text()
        if "auto-merge" in log:
            failures.append("--check enabled auto-merge")
        if ledger.exists():
            failures.append("--check wrote to the ledger")


# ----- the ledger and config tools ----------------------------------------

def check_tools(failures):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        ledger = tmp / "ledger.jsonl"

        # The ledger tool records an outcome and reports the rate.
        r = subprocess.run(
            [sys.executable, str(LEDGER_TOOL), "--ledger", str(ledger),
             "record-outcome", REPO, "42", HEAD, "merged", "--by", "a-person"],
            capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"pr-merge-ledger exited {r.returncode}: {r.stderr.strip()!r}")

        # An outcome with an unknown name is refused and says why.
        r = subprocess.run(
            [sys.executable, str(LEDGER_TOOL), "--ledger", str(ledger),
             "record-outcome", REPO, "42", HEAD, "approved"],
            capture_output=True, text=True)
        if r.returncode == 0:
            failures.append("pr-merge-ledger accepted an unknown outcome")
        if "merged" not in r.stderr:
            failures.append(
                "pr-merge-ledger's refusal does not name the outcomes")

        # The rate is reported, and so is the reconciliation. Both read
        # the ledger the outcome went into.
        r = subprocess.run(
            [sys.executable, str(LEDGER_TOOL), "--ledger", str(ledger),
             "rate"],
            capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"pr-merge-ledger rate exited {r.returncode}: "
                f"{r.stderr.strip()!r}")
        if "disagreement rate" not in r.stdout:
            failures.append(
                f"pr-merge-ledger rate printed {r.stdout.strip()!r}")

        r = subprocess.run(
            [sys.executable, str(LEDGER_TOOL), "--ledger", str(ledger),
             "reconcile"],
            capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"pr-merge-ledger reconcile exited {r.returncode}: "
                f"{r.stderr.strip()!r}")

        # A ledger that does not exist has no rate, and saying so is not
        # an error: nothing has been decided yet.
        r = subprocess.run(
            [sys.executable, str(LEDGER_TOOL), "--ledger",
             str(tmp / "absent.jsonl"), "rate"],
            capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"pr-merge-ledger rate exited {r.returncode} for a ledger "
                f"that does not exist")

        # The config tool validates the file and reports the settings.
        config_path = tmp / "pr-loop.toml"
        write_config(config_path, auto_merge="shadow", merge_method="squash",
                     ruleset="loop-merge", required_checks=["Gates"])
        r = subprocess.run(
            [sys.executable, str(CONFIG_TOOL), "--config", str(config_path),
             REPO],
            capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"pr-loop-config exited {r.returncode}: {r.stderr.strip()!r}")
        if "auto_merge=shadow" not in r.stdout:
            failures.append(f"pr-loop-config printed {r.stdout.strip()!r}")

        # A broken repository in the file is refused, and the refusal
        # names it.
        broken = tmp / "broken.toml"
        broken.write_text(f'["{REPO}"]\nauto_merge = "maybe"\n')
        r = subprocess.run(
            [sys.executable, str(CONFIG_TOOL), "--config", str(broken), REPO],
            capture_output=True, text=True)
        if r.returncode == 0:
            failures.append("pr-loop-config accepted a broken configuration")

        # A file whose second repository is broken is refused even when
        # the first is asked about.
        mixed = tmp / "mixed.toml"
        mixed.write_text(
            f'["{REPO}"]\nauto_merge = "shadow"\n'
            '["owner/other"]\nmerge_method = "merge"\n')
        r = subprocess.run(
            [sys.executable, str(CONFIG_TOOL), "--config", str(mixed), REPO],
            capture_output=True, text=True)
        if r.returncode == 0:
            failures.append(
                "pr-loop-config accepted a file whose second repository is "
                "broken")

        # --list names every repository the file configures.
        r = subprocess.run(
            [sys.executable, str(CONFIG_TOOL), "--config", str(mixed),
             "--list"],
            capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"pr-loop-config --list exited {r.returncode}: "
                f"{r.stderr.strip()!r}")
        if REPO not in r.stdout or "owner/other" not in r.stdout:
            failures.append(
                f"pr-loop-config --list printed {r.stdout.strip()!r}")

        # A table that is not named owner/name is refused: it is either
        # a typo in a repository name or a heading the file was never
        # meant to carry.
        heading = tmp / "heading.toml"
        heading.write_text('[settings]\nauto_merge = "on"\n')
        r = subprocess.run(
            [sys.executable, str(CONFIG_TOOL), "--config", str(heading)],
            capture_output=True, text=True)
        if r.returncode == 0:
            failures.append(
                "pr-loop-config accepted a table that is not a repository")

        # A repository the file does not name is shadow mode with no
        # protections, and the tool says so.
        r = subprocess.run(
            [sys.executable, str(CONFIG_TOOL), "--config", str(config_path),
             "owner/absent"],
            capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"pr-loop-config exited {r.returncode} for a repository the "
                f"file does not name: {r.stderr.strip()!r}")
        if "auto_merge=shadow" not in r.stdout:
            failures.append(
                f"pr-loop-config printed {r.stdout.strip()!r} for a "
                f"repository the file does not name")


# ----- the source is free of a merge call ---------------------------------

def check_no_merge(failures):
    """Nothing in this repository merges a pull request.

    The driver's only write to GitHub is the auto-merge endpoint, and
    the auto-merge endpoint is not a merge: it asks GitHub to merge
    later, under the repository's ruleset. A call to the merge endpoint
    would be a tool holding the merge rule itself, which is the thing
    the ruleset exists to prevent.
    """
    text = DRIVER.read_text()
    if "/merge" in text.replace("/auto-merge", ""):
        failures.append("the driver names the merge endpoint")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "pulls/{" in stripped and "/merge" in stripped:
            failures.append(f"the driver calls the merge endpoint: {line!r}")


def main() -> int:
    failures = []
    # Each check is guarded: one that raises is a failure of its own,
    # and the checks after it still run. A suite that stops at the first
    # crash reports one problem where it could have reported all of
    # them.
    checks = (check_decision_scenarios, check_ruleset_reader,
              check_config_reader, check_codeowners, check_ledger,
              check_driver, check_tools, check_no_merge)
    for check in checks:
        try:
            check(failures)
        except Exception as exc:  # noqa: BLE001 - the point is to report it
            failures.append(f"{check.__name__} raised {exc!r}")

    if failures:
        print(f"FAIL {len(failures)} failure(s)")
        for f in failures:
            print(f" - {f}")
        return 1
    print("PASS auto-merge decisions, ledger and tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
