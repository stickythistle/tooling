#!/usr/bin/env python3
"""Read a project's gate manifest and print one field, shell-quoted.

Every value is printed quoted for `eval`, and lists reach bash as `declare -a`
rather than as a delimiter nothing can escape: a gate command is a shell
fragment the project wrote.
"""
import shlex
import sys
import tomllib
from pathlib import Path

MANIFEST = "gates.toml"
GATES_DIR = "gates.d"


def find(start: Path) -> Path:
    """Search from `start` upward: a tool is handed a worktree by path rather
    than run from inside it."""
    for d in [start, *start.parents]:
        p = d / MANIFEST
        if p.is_file():
            return p
    sys.exit(f"no {MANIFEST} found in {start} or any parent")


def gates(path: Path) -> list[dict]:
    """Every gate the manifest declares: its own, then the ones in each file
    under gates.d/ beside it, in name order.

    One gate per file lets two branches that each add a gate merge without
    a conflict."""
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    found = list(doc.get("gates", []))
    where = {}
    for g in found:
        name = g.get("name")
        if name:
            where[name] = MANIFEST
    # Name order is the only order a merge leaves alone: a modification time
    # or a directory listing says nothing about which file came first.
    for f in sorted((path.parent / GATES_DIR).glob("*.toml")):
        label = f"{GATES_DIR}/{f.name}"
        with f.open("rb") as fh:
            sub = tomllib.load(fh)
        for key, value in sub.items():
            if key == "gates":
                continue
            kind = f"[[{key}]]" if isinstance(value, list) else f"[{key}]"
            sys.exit(f"{label}: holds a {kind}; a {GATES_DIR} file holds "
                     "[[gates]] entries only")
        for g in sub.get("gates", []):
            name = g.get("name")
            # A name is how a caller picks a gate, so two of them is a
            # manifest that cannot say which one was meant.
            if name and name in where:
                sys.exit(f"{label}: declares gate {name!r}, which "
                         f"{where[name]} also declares")
            if name:
                where[name] = label
            found.append(g)
    return found


def bash_array(name: str, values) -> str:
    items = " ".join(shlex.quote(str(v)) for v in values)
    return f"declare -a {name}=({items})"


# A missing required field is a manifest the reader cannot read, not an entry
# the script can probe later: name the table, the entry and the field so the
# operator knows what to add. Optional fields below use .get(key, "") so they
# reach bash as empty strings; this only fires on fields the caller refuses to
# proceed without.
def require(entry: dict, table: str, entry_name: object, *fields: str) -> None:
    for f in fields:
        # `in` rather than `entry[f]`: a present-but-empty value is a real
        # value, and a value the caller can fix without a guess.
        if f not in entry:
            sys.exit(f"{MANIFEST}: [[{table}]] {entry_name!r} missing required {f!r}")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: manifest.py <repo> <query> [default]")
    repo, query = Path(sys.argv[1]).resolve(), sys.argv[2]
    default = sys.argv[3] if len(sys.argv) > 3 else None

    path = find(repo)
    with path.open("rb") as fh:
        doc = tomllib.load(fh)

    if query == "--remote":
        want = default
        for entry in doc.get("remote", []):
            if entry.get("name") != want:
                continue
            # `name` is how the caller picked this entry; `role`, `command`, `send`
            # are what the caller cannot proceed without. Other fields, if any,
            # are optional.
            require(entry, "remote", want, "name", "role", "command", "send")
            print(f"REMOTE_ROLE={shlex.quote(entry['role'])}")
            print(f"REMOTE_COMMAND={shlex.quote(entry['command'])}")
            print(bash_array("REMOTE_SEND", entry.get("send", [])))
            return
        names = ", ".join(e.get("name", "?") for e in doc.get("remote", [])) or "none"
        sys.exit(f"{MANIFEST}: no [[remote]] named {want!r} (have: {names})")

    if query == "--task":
        # `name`, `role`, `command` are required. Every other field is optional
        # and reaches bash empty below -- including `full-command`, `fetch`,
        # the `database.*` block, and `mirrors`, which the caller probes for
        # emptiness. A project with no [task.mirrors] must be indistinguishable
        # from one built before the drift check existed.
        want = default
        for entry in doc.get("task", []):
            if entry.get("name") != want:
                continue
            require(entry, "task", want, "name", "role", "command")
            db = entry.get("database", {})
            mirrors = entry.get("mirrors") or {}
            # Optional sub-table naming the files that hold a sqlx::Migrator;
            # bin/remote-task says what it does with them.
            migrator = entry.get("migrator") or {}
            for var, val in [
                ("TASK_ROLE", entry["role"]),
                ("TASK_COMMAND", entry["command"]),
                ("TASK_FULL_COMMAND", entry.get("full-command", "")),
                ("TASK_DB_CONTAINER", db.get("container", "")),
                ("TASK_DB_USER", db.get("user", "")),
                ("TASK_DB_PORT", db.get("port", "")),
                ("TASK_DB_PREFIX", db.get("name-prefix", "")),
                ("TASK_MIRRORS_WORKFLOW", mirrors.get("workflow", "")),
                ("TASK_MIRRORS_JOB", mirrors.get("job", "")),
                ("TASK_MIGRATOR_DIR", migrator.get("dir", "migrations")),
                ("TASK_TIMEOUT", entry.get("timeout", "")),
            ]:
                print(f"{var}={shlex.quote(str(val))}")
            print(bash_array("TASK_FETCH", entry.get("fetch", [])))
            print(bash_array("TASK_MIRRORS_EXCEPT", mirrors.get("except", [])))
            print(bash_array("TASK_MIGRATOR_SOURCES", migrator.get("sources", [])))
            return
        names = ", ".join(e.get("name", "?") for e in doc.get("task", [])) or "none"
        sys.exit(f"{MANIFEST}: no [[task]] named {want!r} (have: {names})")

    if query == "roles":
        print(" ".join(sorted({t["role"] for t in doc.get("task", []) if "role" in t})))
        return

    if query == "gates":
        # The gates live in the manifest and in the files under gates.d/
        # beside it, so the loader answers for them.
        found = gates(path)
        # Three parallel arrays because bash has no array of records. Order is
        # the file's, so the manifest decides what runs first.
        names = [g.get("name", f"gate{i}") for i, g in enumerate(found)]
        whens = [g.get("when", "") for g in found]
        runs = [g.get("run", "") for g in found]
        if any(not r for r in runs):
            sys.exit(f"{MANIFEST}: every [[gates]] entry needs a `run`")
        print(bash_array("GATE_NAMES", names))
        print(bash_array("GATE_WHEN", whens))
        print(bash_array("GATE_RUN", runs))
        return

    node = doc
    for part in query.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is not None:
                print(default)
                return
            sys.exit(f"{MANIFEST}: no `{query}`")
        node = node[part]

    if isinstance(node, list):
        print(" ".join(shlex.quote(str(v)) for v in node))
    elif isinstance(node, bool):
        print("1" if node else "")
    else:
        print(node)


if __name__ == "__main__":
    main()
