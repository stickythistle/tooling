#!/usr/bin/env python3
"""Flag prose blocks that are candidates for deletion, ranked by how safe the cut is.

Deletion is a judgment call; this only finds candidates. Exit status is always 0 —
a finding is a question, not a failure.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

HASH_SUFFIXES = {".toml", ".yml", ".yaml", ".sh", ".cfg", ".ini", ".cmake"}
# `#[...]` attributes are code, not comments, so Rust needs no lexer of its own.
SLASH_SUFFIXES = {".h", ".hpp", ".cc", ".cpp", ".hujson", ".js", ".ts", ".tsx", ".rs"}
# `--` needs its own lexer: the hash lexer would keep the second dash.
SQL_SUFFIXES = {".sql"}
PROSE_SUFFIXES = {".py", ".md"} | HASH_SUFFIXES | SLASH_SUFFIXES | SQL_SUFFIXES
# Interpreters whose scripts are conventionally installed without a suffix, so
# the shebang is the only thing that says what the file is.
SHEBANG_HASH = ("sh", "bash", "zsh", "ksh", "dash", "python", "ruby", "perl")


def hash_commented(path: Path, rev: str | None = None) -> bool:
    """Whether a file with no known suffix, like `bin/gate-verify` or
    `hosts.env.example`, opens with a #-shebang or a `#` comment.

    `@generated` files such as `Cargo.lock` open with a comment nobody wrote.
    """
    try:
        if rev:
            first = run(["git", "show", f"{rev}:{path}"]).split("\n", 1)[0]
        else:
            with path.open("r", errors="replace") as fh:
                first = fh.readline(200)
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError):
        return False
    first = first.rstrip("\n")
    if "@generated" in first:
        return False
    if first.startswith("#!"):
        return any(name in first for name in SHEBANG_HASH)
    return first == "#" or first.startswith("# ")

# Ranked most-mechanical first: a duplicate is a fact in N places, an oversize block
# is only a suspicion.
FINDING_ORDER = [
    "provenance",
    "duplicate",
    "enforced-in-raise",
    "echoes-code",
    "echoes-prose",
    "trailing-clause",
    "near-duplicate",
    "roadmap",
    "negative-space",
    "oversize",
]

ROADMAP = re.compile(
    r"\b(TODO|FIXME|for now|in the future|eventually|milestone|deferred|"
    r"coming soon|next step|(?:for|until|handled?|addressed?|revisit(?:ed)?|"
    r"tighten(?:ed)?|left)\s+later|will (?:be|become|land|move|need))\b",
    re.IGNORECASE,
)
# A link back to the session that wrote the change: provenance, never product history.
PROVENANCE = re.compile(r"claude\.ai/code/session_")
# Tool directives and ASCII dividers are not prose.
PRAGMA = re.compile(r"^(noqa|type:|pylint|ruff|mypy|flake8|fmt:|isort:|pragma|-\*-)", re.I)
DIVIDER = re.compile(r"[-=~#*_]{3,}")
NEGATIVE_SPACE = re.compile(
    r"\b(is not|are not|isn't|aren't|does not|doesn't|do not|don't|no longer|"
    r"rather than|instead of|not part of|lives elsewhere|nothing here|"
    r"there is no|has no)\b",
    re.IGNORECASE,
)
# A changelog bullet ends at the action; what follows a comma or dash is rationale.
TRAILING_CLAUSE = re.compile(
    r",\s+(so|which|and|because|since|making|keeping|letting|allowing|ensuring|"
    r"that way|thereby)\b|\s+—\s+|\s+--\s+",
    re.IGNORECASE,
)
BACKTICKED = re.compile(r"``([^`]+)``|`([^`]+)`")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
STOPWORDS = frozenset(
    "a an the and or but if is are was were be been being to of in on for with "
    "that this it its as at by from so no not can will each every one two per "
    "when what which while how why has have had do does did than then there "
    "here into via same only also all any".split()
)


@dataclass
class Block:
    path: str
    line: int
    end_line: int
    kind: str
    text: str
    findings: list[str] = field(default_factory=list)
    # The block's source lines, kept only for tables: a table's rows are
    # judged one at a time, and the `--diff` filter judges only the
    # rows the diff touched, so each row has to carry the line it sits
    # on.
    lines: list[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def key(self) -> str:
        return " ".join(re.sub(r"[^a-z0-9 ]", " ", self.text.lower()).split())

    @property
    def content_words(self) -> set[str]:
        return {w.lower() for w in WORD.findall(self.text)} - STOPWORDS

    def preview(self, width: int = 78) -> str:
        flat = " ".join(self.text.split())
        return flat if len(flat) <= width else flat[: width - 1] + "…"


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def changed_lines(base: str, paths: list[str]) -> dict[str, set[int]]:
    """path -> line numbers the diff touches. ``base`` may be ``BASE..HEAD``.

    A single ref is rewritten to its merge base so ``git diff`` against it
    includes uncommitted working-tree edits; an explicit ``A..B`` is left.
    """
    if ".." in base:
        revs = base.split("..")
    else:
        merge_base = run(["git", "merge-base", base, "HEAD"]).strip()
        revs = [merge_base]
    out = run(["git", "diff", "-U0", "--no-color", *revs, "--", *paths])
    touched: dict[str, set[int]] = defaultdict(set)
    current = None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        # A deleted file: its hunks belong to no file in the tree.
        elif line.startswith("+++ /dev/null"):
            current = None
        elif line.startswith("@@") and current:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or 1)
                # `+start,count` covers start..start+count-1. A pure deletion
                # (count 0) touches both lines that border its gap.
                if count:
                    touched[current].update(range(start, start + count))
                else:
                    touched[current].update({start, start + 1})
    return touched


def python_blocks(rel: str, src: str) -> tuple[list[Block], str]:
    """Docstrings and comment groups, plus every string raised in the file."""
    blocks: list[Block] = []
    raised: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                doc = ast.get_docstring(node, clean=True)
                if doc:
                    literal = node.body[0].value
                    blocks.append(
                        Block(rel, literal.lineno, literal.end_lineno, "docstring", doc)
                    )
            if isinstance(node, ast.Raise):
                raised += [
                    n.value
                    for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                ]
    blocks += comment_blocks(rel, src, "python")
    return blocks, " ".join(raised)


def comment_blocks(rel: str, src: str, lexer: str) -> list[Block]:
    """Comment rows grouped into blocks. ``lexer`` is python, hash, or slash."""
    rows: list[tuple[int, str]] = []
    if lexer == "python":
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT:
                    rows.append((tok.start[0], tok.string.lstrip("#").strip()))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
    elif lexer == "hash":
        for lineno, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") and not stripped.startswith("#!"):
                rows.append((lineno, stripped.lstrip("#").strip()))
    elif lexer == "sql":
        # SQL comments are `--` lines; strip exactly the two dashes. PRAGMA
        # below drops a `-- noqa: ...` directive (matches `^noqa`) before it
        # reaches the grouping loop, so a migration can mix prose headers with
        # lint hints.
        for lineno, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("--") and not stripped.startswith("---"):
                rows.append((lineno, stripped[2:].strip()))
    else:
        in_block = False
        for lineno, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if in_block:
                rows.append((lineno, stripped.removesuffix("*/").lstrip("*").strip()))
                in_block = "*/" not in stripped
            elif stripped.startswith("//"):
                rows.append((lineno, stripped.lstrip("/").strip()))
            elif stripped.startswith("/*"):
                body = stripped.removeprefix("/*").removesuffix("*/").strip()
                rows.append((lineno, body))
                in_block = "*/" not in stripped

    blocks: list[Block] = []
    for lineno, text in rows:
        if PRAGMA.match(text):
            continue
        if blocks and blocks[-1].end_line == lineno - 1:
            blocks[-1].end_line = lineno
            blocks[-1].text += " " + text
        elif text:
            blocks.append(Block(rel, lineno, lineno, "comment", text))

    for block in blocks:
        # A label fenced by rules is a section banner: navigation, not narration.
        if DIVIDER.search(block.text):
            block.kind = "banner"
        block.text = DIVIDER.sub(" ", block.text).strip()
    return [b for b in blocks if len(b.text.split()) >= 3]


def markdown_blocks(rel: str, src: str) -> list[Block]:
    blocks: list[Block] = []
    fenced = False
    buffer: list[str] = []
    start = 0
    for lineno, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.strip():
            if not buffer:
                start = lineno
            buffer.append(line)
            continue
        if buffer:
            blocks += _markdown_blocks(rel, start, buffer)
            buffer = []
    if buffer:
        blocks += _markdown_blocks(rel, start, buffer)
    return [b for b in blocks if b.text]


LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _markdown_blocks(rel: str, start: int, lines: list[str]) -> list[Block]:
    """One block per paragraph, table, or list item — a list is many facts, not one."""
    if not any(LIST_ITEM.match(line) for line in lines):
        return [_markdown_block(rel, start, start + len(lines) - 1, lines)]

    items: list[Block] = []
    for offset, line in enumerate(lines):
        if LIST_ITEM.match(line) or not items:
            items.append(Block(rel, start + offset, start + offset, "list-item", line.strip()))
        else:
            items[-1].end_line = start + offset
            items[-1].text += " " + line.strip()
    return items


def _markdown_block(rel: str, start: int, end: int, lines: list[str]) -> Block:
    text = " ".join(line.strip() for line in lines)
    kind = "markdown"
    if lines[0].lstrip().startswith("#"):
        kind = "heading"
    elif lines[0].lstrip().startswith("|"):
        kind = "table"
    # A table keeps its lines, so its rows can carry the line they sit
    # on for the `--diff` filter.
    return Block(rel, start, end, kind, text, lines=list(lines) if kind == "table" else [])


def table_rows(block: Block) -> list[tuple[int, str, list[str]]]:
    """The table's rows, one per line, with the line each sits on.

    Each entry is `(line, text, cells)`: the whole line, so its cells are
    judged together, and the cells are kept so one can be judged alone.
    """
    rows: list[tuple[int, str, list[str]]] = []
    for offset, line in enumerate(block.lines):
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        text = " ".join(cell for cell in cells if cell)
        if not text:
            continue
        # The header separator carries no prose. The header row is a row,
        # and a header repeating the prose above the table is an echo of it.
        if set(text) <= set("-: "):
            continue
        rows.append((block.line + offset, text, cells))
    return rows


def _echoes(words: set[str], prose: list[Block]) -> bool:
    """True when these words repeat one of the paragraphs.

    The measure is the share of *these* words found in the paragraph.
    """
    for p in prose:
        if not p.content_words:
            continue
        if overlap(words, p.content_words) >= 0.7:
            return True
    return False


def _row_echoes(row: str, cells: list[str], prose: list[Block]) -> bool:
    """True when a row, or one of its cells, repeats a paragraph."""
    row_words = {w.lower() for w in WORD.findall(row)} - STOPWORDS
    if len(row_words) >= 3 and _echoes(row_words, prose):
        return True
    for cell in cells:
        cell_words = {w.lower() for w in WORD.findall(cell)} - STOPWORDS
        # A cell with fewer than three content words is a name or a
        # value, and is skipped.
        if len(cell_words) < 3:
            continue
        if _echoes(cell_words, prose):
            return True
    return False


def stems(text: str) -> set[str]:
    """Content words with plurals folded in, so `document` matches `documents`."""
    words = set()
    for raw in WORD.findall(text):
        word = raw.lower()
        if word in STOPWORDS:
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return words


def overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a) if a else 0.0


def next_code_line(src_lines: list[str], block: Block) -> str:
    for line in src_lines[block.end_line : block.end_line + 3]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def identifiers(code: str) -> set[str]:
    parts: set[str] = set()
    for token in WORD.findall(code):
        parts.update(p.lower() for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", token) if p)
    return parts - STOPWORDS


def restated_in_raise(block: Block, raised: str) -> bool:
    """True when one sentence of the block says what a raise in the same file says.

    A shared backticked token alone is a coincidence — the docstring of a helper
    names the same parameters its guard rejects.
    """
    raised_words = stems(raised)
    if len(raised_words) < 3:
        return False
    for sentence in re.split(r"(?<=[.;:])\s+", block.text):
        quoted = {
            (m.group(1) or m.group(2)).strip() for m in BACKTICKED.finditer(sentence)
        }
        if not any(len(q) > 2 and q in raised for q in quoted):
            continue
        words = stems(sentence)
        if words and overlap(words, raised_words) >= 0.5:
            return True
    return False


def collect(
    paths: list[Path],
    root: Path,
    min_words: int,
    rev: str | None = None,
    touched: dict[str, set[int]] | None = None,
) -> list[Block]:
    """Scan ``paths``; with ``rev`` set, read them out of that commit.

    With ``touched`` set, only the lines a diff named are judged.
    """
    blocks: list[Block] = []
    for path in paths:
        try:
            src = (
                run(["git", "show", f"{rev}:{path}"]) if rev else path.read_text()
            )
        except (OSError, UnicodeDecodeError, subprocess.CalledProcessError):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        lines = src.splitlines()
        if path.suffix == ".py" or (not path.suffix and src.startswith("#!") and "python" in src.split("\n", 1)[0]):
            found, raised = python_blocks(rel, src)
        elif path.suffix == ".md":
            found, raised = markdown_blocks(rel, src), ""
        elif path.suffix == ".sql":
            found, raised = comment_blocks(rel, src, "sql"), ""
        else:
            lexer = "slash" if path.suffix in SLASH_SUFFIXES else "hash"
            found, raised = comment_blocks(rel, src, lexer), ""

        prose = [b for b in found if b.kind not in {"table", "heading"}]
        for block in found:
            # A table's length comes from its row count, so no table is
            # oversize. The per-row checks below still apply to it.
            if block.kind != "table" and block.words > min_words:
                block.findings.append("oversize")
            if PROVENANCE.search(block.text):
                block.findings.append("provenance")
            if ROADMAP.search(block.text):
                block.findings.append("roadmap")
            if NEGATIVE_SPACE.search(block.text):
                block.findings.append("negative-space")
            if raised and restated_in_raise(block, raised):
                block.findings.append("enforced-in-raise")
            if block.kind == "list-item" and TRAILING_CLAUSE.search(block.text):
                block.findings.append("trailing-clause")
            if block.kind == "comment" and block.words <= 15:
                code = identifiers(next_code_line(lines, block))
                if code and overlap(block.content_words, code) >= 0.6:
                    block.findings.append("echoes-code")
            if block.kind == "table":
                judged = table_rows(block)
                if touched is not None:
                    # A row the diff never touched stays unjudged.
                    diff_lines = touched.get(block.path, set())
                    judged = [
                        (lineno, text, cells)
                        for lineno, text, cells in judged
                        if lineno in diff_lines
                    ]
                for _, row, cells in judged:
                    # The row is the unit: a row that restates the
                    # paragraph is an echo. A cell is judged the same
                    # way, because a row's own cells dilute its share
                    # of the paragraph.
                    if _row_echoes(row, cells, prose):
                        block.findings.append("echoes-prose")
                        break
        blocks += found
    return blocks


def cross_file_duplicates(blocks: list[Block]) -> list[list[Block]]:
    groups: dict[str, list[Block]] = defaultdict(list)
    for block in blocks:
        if block.words >= 4:
            groups[block.key].append(block)

    exact = []
    for members in groups.values():
        if len({b.path for b in members}) > 1:
            for block in members:
                block.findings.append("duplicate")
            exact.append(members)

    flagged = {id(b) for group in exact for b in group}
    unique = [b for b in blocks if b.words >= 8 and id(b) not in flagged]
    near: list[list[Block]] = []
    for i, left in enumerate(unique):
        for right in unique[i + 1 :]:
            if left.path == right.path:
                continue
            if SequenceMatcher(None, left.key, right.key).ratio() >= 0.85:
                left.findings.append("near-duplicate")
                right.findings.append("near-duplicate")
                near.append([left, right])
    return exact + near


def rank_findings(blocks: list[Block]) -> None:
    """Order each block's findings so the first one is the most mechanical cut."""
    for block in blocks:
        block.findings = sorted(
            dict.fromkeys(block.findings), key=lambda f: FINDING_ORDER.index(f)
        )


def report(blocks: list[Block], groups: list[list[Block]]) -> None:
    flagged = [b for b in blocks if b.findings]
    by_kind: dict[str, list[Block]] = defaultdict(list)
    for block in flagged:
        by_kind[block.findings[0]].append(block)

    grouped_ids = {id(b) for group in groups for b in group}
    for group in groups:
        kind = group[0].findings[0]
        print(
            f"{kind}: {len(group)} copies, {group[0].words} words each "
            f"— keep one, delete {len(group) - 1}"
        )
        for block in group:
            print(f"  {block.path}:{block.line}")
        print(f"  {group[0].preview()}")
        print()

    for kind in FINDING_ORDER:
        rest = [
            b for b in by_kind.get(kind, []) if id(b) not in grouped_ids
        ]
        for block in rest:
            tags = ",".join(block.findings)
            print(f"{tags}: {block.path}:{block.line} ({block.words} words, {block.kind})")
            print(f"  {block.preview()}")
            print()

    words = sum(b.words for b in flagged)
    print(
        f"{len(flagged)} of {len(blocks)} prose blocks flagged, {words} words in play."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories")
    parser.add_argument("--diff", metavar="REF", help="only blocks the diff vs REF touches")
    parser.add_argument("--min-words", type=int, default=40, help="oversize threshold")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path.cwd()
    touched: dict[str, set[int]] = {}
    candidates: list[Path] = []
    rev = args.diff.split("..")[-1] if args.diff and ".." in args.diff else None
    if args.diff:
        touched = changed_lines(args.diff, args.paths)
        candidates = [
            Path(p)
            for p in touched
            if (Path(p).suffix in PROSE_SUFFIXES or hash_commented(Path(p), rev))
            and (rev or Path(p).is_file())
        ]
    else:
        for raw in args.paths:
            path = Path(raw)
            if path.is_dir():
                candidates += [
                    p
                    for p in path.rglob("*")
                    if (p.suffix in PROSE_SUFFIXES or (p.is_file() and hash_commented(p)))
                    and ".git" not in p.parts
                ]
            elif path.suffix in PROSE_SUFFIXES or hash_commented(path):
                candidates.append(path)

    blocks = collect(
        sorted(set(candidates)),
        root,
        args.min_words,
        rev,
        touched if args.diff else None,
    )
    if args.diff:
        blocks = [
            b
            for b in blocks
            if touched.get(b.path, set()) & set(range(b.line, b.end_line + 1))
        ]
    groups = cross_file_duplicates(blocks)
    rank_findings(blocks)

    if args.json:
        json.dump(
            [
                {
                    "path": b.path,
                    "line": b.line,
                    "kind": b.kind,
                    "words": b.words,
                    "findings": b.findings,
                    "text": b.text,
                }
                for b in blocks
                if b.findings
            ],
            sys.stdout,
            indent=2,
        )
        print()
    else:
        report(blocks, groups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
