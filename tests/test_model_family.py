#!/usr/bin/env python3
"""Pin the refusal that stops a review being run by the author's own model.

A reviewer from the author's own family agrees with the author, so the
check has to refuse before the model runs. The suite drives the real
module against real profile directories built in a tempdir, so the
assertions are over what the tool reads and not over what it looks like
it reads.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "lib" / "model_family.py"


def _load_module():
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    tmp.close()
    shutil.copy(str(MODULE_PATH), tmp.name)
    spec = importlib.util.spec_from_file_location("_mf_under_test",
                                                  tmp.name)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH} as a module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mf = _load_module()


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def make_profile(root: Path, name: str, model: str) -> None:
    """A profile directory with a config.yaml naming a model."""
    d = root / name
    d.mkdir(parents=True)
    (d / "config.yaml").write_text(
        f"model:\n  default: {model}\n  provider: custom\n"
        f"agent:\n  max_turns: 90\n")


def with_profiles(failures, models: dict, env_extra: dict | None = None):
    """A tempdir of profiles, with HERMES_PROFILES_DIR pointing at it."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for name, model in models.items():
        make_profile(root, name, model)
    old = os.environ.get("HERMES_PROFILES_DIR")
    os.environ["HERMES_PROFILES_DIR"] = str(root)
    for name in ("REVIEWER_MODEL_FAMILY", "IMPLEMENTER_MODEL_FAMILY"):
        os.environ.pop(name, None)
    if env_extra:
        os.environ.update(env_extra)
    return tmp


def case_the_same_family_is_refused(failures):
    """Two profiles declared to be one family are refused, and the
    refusal names both of them."""
    tmp = with_profiles(failures, {"reviewer": "muse-glimmer-30b-dual",
                                    "muse": "muse-glimmer-1m"},
                        {"REVIEWER_MODEL_FAMILY": "muse-glimmer",
                         "IMPLEMENTER_MODEL_FAMILY": "muse-glimmer"})
    try:
        reason = mf.check("reviewer", "muse")
    finally:
        tmp.cleanup()
    if reason is None:
        fail(failures, "same-family: it allowed the review")
        return
    if "reviewer" not in reason or "muse" not in reason:
        fail(failures, "same-family: the refusal does not name both",
             reason)


def case_different_families_are_allowed(failures):
    """Two families that differ are allowed."""
    tmp = with_profiles(failures, {"reviewer": "muse-glimmer-30b-dual",
                                    "athens": "qwen3.8-27b-dual"},
                        {"REVIEWER_MODEL_FAMILY": "muse-glimmer",
                         "IMPLEMENTER_MODEL_FAMILY": "qwen3"})
    try:
        reason = mf.check("reviewer", "athens")
    finally:
        tmp.cleanup()
    if reason is not None:
        fail(failures, "different-family: it refused", reason)


def case_the_same_profile_is_refused(failures):
    """The same profile cannot review its own work, whatever the
    operator declares."""
    tmp = with_profiles(failures, {"reviewer": "muse-glimmer-30b-dual"},
                        {"REVIEWER_MODEL_FAMILY": "muse-glimmer",
                         "IMPLEMENTER_MODEL_FAMILY": "qwen3"})
    try:
        reason = mf.check("reviewer", "reviewer")
    finally:
        tmp.cleanup()
    if reason is None:
        fail(failures, "same-profile: it allowed the review")
        return
    if "same profile" not in reason:
        fail(failures, "same-profile: the refusal does not say what is "
             "wrong", reason)


def case_an_undeclared_family_is_refused(failures):
    """A family nobody has declared is a family nobody has said, and
    refusing to run is better than pretending the two differ."""
    tmp = with_profiles(failures, {"reviewer": "muse-glimmer-30b-dual",
                                    "athens": "qwen3.8-27b-dual"})
    try:
        reason = mf.check("reviewer", "athens")
    finally:
        tmp.cleanup()
    if reason is None:
        fail(failures, "undeclared: it allowed the review")
        return
    if "REVIEWER_MODEL_FAMILY" not in reason:
        fail(failures, "undeclared: the refusal does not name the variable "
             "to set", reason)
    # It names the model it could not place, so the operator can see
    # which one the declaration is about.
    if "muse-glimmer-30b-dual" not in reason:
        fail(failures, "undeclared: the refusal does not name the model",
             reason)


def case_a_profile_with_no_model_is_refused(failures):
    """A profile whose config names no model cannot be placed."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "reviewer").mkdir()
    (root / "reviewer" / "config.yaml").write_text("agent:\n  max_turns: 9\n")
    make_profile(root, "athens", "qwen3.8-27b-dual")
    old = os.environ.get("HERMES_PROFILES_DIR")
    os.environ["HERMES_PROFILES_DIR"] = str(root)
    os.environ["IMPLEMENTER_MODEL_FAMILY"] = "qwen3"
    os.environ.pop("REVIEWER_MODEL_FAMILY", None)
    try:
        reason = mf.check("reviewer", "athens")
    finally:
        if old is None:
            os.environ.pop("HERMES_PROFILES_DIR", None)
        else:
            os.environ["HERMES_PROFILES_DIR"] = old
        os.environ.pop("REVIEWER_MODEL_FAMILY", None)
        os.environ.pop("IMPLEMENTER_MODEL_FAMILY", None)
        tmp.cleanup()
    if reason is None:
        fail(failures, "no-model: it allowed the review")


def case_the_model_is_read_from_the_profiles_own_config(failures):
    """The model comes from the profile's own file, so two profiles with
    the same model are two profiles that agree."""
    tmp = with_profiles(failures, {"a": "muse-glimmer-30b-dual",
                                    "b": "muse-glimmer-30b-dual"})
    try:
        if mf.profile_model("a") != "muse-glimmer-30b-dual":
            fail(failures, "model-read: the model read",
                 mf.profile_model("a"))
        if mf.profile_model("b") != "muse-glimmer-30b-dual":
            fail(failures, "model-read: the second model read",
                 mf.profile_model("b"))
        if mf.profile_model("does-not-exist") != "":
            fail(failures, "model-read: a missing profile read a model")
    finally:
        tmp.cleanup()


def case_the_command_refuses_before_the_model_runs(failures):
    """The command is what the driver calls, and it exits non-zero on a
    refusal so the driver stops."""
    with tempfile.TemporaryDirectory() as t:
        env = dict(os.environ)
        env["HERMES_PROFILES_DIR"] = t
        env["REVIEWER_MODEL_FAMILY"] = "muse-glimmer"
        env["IMPLEMENTER_MODEL_FAMILY"] = "muse-glimmer"
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--reviewer", "a",
             "--implementer", "b"],
            capture_output=True, text=True, env=env)
        if result.returncode != 2:
            fail(failures, "command: exit code", result.returncode)
        if "refusing" not in result.stderr:
            fail(failures, "command: the refusal is not on stderr",
                 result.stderr[:200])


def main() -> int:
    if not MODULE_PATH.is_file():
        print(f"FAIL module not built: {MODULE_PATH}")
        return 1
    failures: list = []
    for case in CASES:
        try:
            case(failures)
        except Exception as exc:  # noqa: BLE001
            fail(failures, f"{case.__name__} raised",
                 f"{type(exc).__name__}: {exc}")
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS model_family: the same family refused; different families "
          "allowed; the same profile refused; an undeclared family refused; "
          "the model read; the command")
    return 0


CASES = (case_the_same_family_is_refused,
         case_different_families_are_allowed,
         case_the_same_profile_is_refused,
         case_an_undeclared_family_is_refused,
         case_a_profile_with_no_model_is_refused,
         case_the_model_is_read_from_the_profiles_own_config,
         case_the_command_refuses_before_the_model_runs)


if __name__ == "__main__":
    sys.exit(main())
