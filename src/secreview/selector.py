"""
selector.py - choosing which files to review and splitting them into chunks.

Aim of this file
----------------
when a repository is too big for one model request, we need
a simple strategy for choosing files and splitting work, with explicit limits,
and we must report when those limits affect coverage.

This module is pure logic over a folder: no network, no model. It decides
WHAT gets reviewed (the agents later decide how deeply to look). Steps:

  1. Find in-scope files: *.py files, outside ignored folders (.git, virtual
     environments, tests, build output...). Empty files are ignored.
  2. Rank them so security-relevant files come first (auth, views, db...).
  3. Apply the limits in rank order: max_file_bytes, max_files,
     max_total_chars. Every file left out is recorded with its reason.
  4. Render each selected file with line numbers ("12| code") so the model
     can cite exact lines, and pack the result into chunks of at most
     chunk_chars characters. Small files share a chunk; a large file is
     split across several chunks at line boundaries.

The output, a Selection, tells the pipeline what to send to the agents
(chunks), which files the evidence validator should accept (selected_paths),
and what to report as not reviewed (skipped).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from secreview.config import Limits
from secreview.models import SkippedFile

# Folders never reviewed: version control, dependencies, virtual environments,
# caches, build output, and test code (test code is not shipped to production,
# so findings there are rarely actionable; documented as a limitation).
IGNORED_DIRS = {
    ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env", "virtualenv",
    "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    "build", "dist", ".eggs",
    "tests", "test", "testing",
}

# Words in a file path that suggest security-relevant code. Each word found
# in the lowercase path adds 1 to the file's priority score.
RISK_KEYWORDS = (
    "auth", "login", "password", "passwd", "token", "secret", "crypt", "key",
    "session", "cookie", "admin", "permission", "user", "account",
    "api", "view", "route", "handler", "controller", "endpoint", "server", "app",
    "request", "upload", "download", "file", "path",
    "exec", "shell", "command", "subprocess", "eval",
    "sql", "query", "db", "database", "model", "orm",
    "settings", "config", "serial", "pickle", "template", "xml", "yaml",
)


# ---------------------------------------------------------------------------
# Output data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChunkPart:
    """One file (or one slice of a file) inside a chunk."""

    path: str        # relative POSIX path, e.g. "app/utils.py"
    start_line: int  # 1-based, inclusive
    end_line: int    # 1-based, inclusive


@dataclass(frozen=True)
class Chunk:
    """One unit of work for the Investigator: text to analyse plus a record
    of exactly which file lines it contains."""

    id: str                 # "chunk-1", "chunk-2", ...
    text: str               # rendered code with headers and line numbers
    parts: tuple[ChunkPart, ...]

    @property
    def paths(self) -> set[str]:
        """Files (fully or partly) contained in this chunk."""
        return {part.path for part in self.parts}


@dataclass
class Selection:
    """Everything the pipeline needs to know about what will be reviewed."""

    chunks: list[Chunk] = field(default_factory=list)
    selected_paths: list[str] = field(default_factory=list)   # in rank order
    skipped: list[SkippedFile] = field(default_factory=list)
    files_in_scope: int = 0        # non-empty .py files outside ignored folders
    out_of_scope_files: int = 0    # other files (not Python): never reviewed


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

class FileSelector:
    """Applies the scope rules and limits to a repository folder.

    Args:
        limits: the processing limits from config (max_files, chunk_chars...).
    """

    def __init__(self, limits: Limits) -> None:
        self._limits = limits

    def select(self, root: Path) -> Selection:
        """Run steps 1-4 described in the module docstring."""
        selection = Selection()

        # Step 1: find candidate files.
        python_files, selection.out_of_scope_files = self._find_files(root)

        # Step 2: rank them. Sorting is deterministic: same repo -> same order.
        python_files.sort(key=lambda rel: (-risk_score(rel), rel.count("/"), rel))

        # Step 3: apply limits in rank order.
        selected: list[tuple[str, list[str]]] = []   # (path, lines)
        total_chars = 0
        for rel in python_files:
            full = root / rel
            size = full.stat().st_size
            if size > self._limits.max_file_bytes:
                selection.files_in_scope += 1
                selection.skipped.append(
                    SkippedFile(path=rel, reason=f"larger than max_file_bytes ({size} bytes)")
                )
                continue
            try:
                text = full.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                selection.files_in_scope += 1
                selection.skipped.append(SkippedFile(path=rel, reason="not valid UTF-8 text"))
                continue
            if not text.strip():
                continue  # empty files (e.g. bare __init__.py) contain nothing to review

            selection.files_in_scope += 1
            if len(selected) >= self._limits.max_files:
                selection.skipped.append(SkippedFile(path=rel, reason="max_files limit reached"))
                continue
            if total_chars + len(text) > self._limits.max_total_chars:
                # Keep going: a smaller, lower-ranked file may still fit.
                selection.skipped.append(SkippedFile(path=rel, reason="max_total_chars limit reached"))
                continue

            selected.append((rel, text.splitlines()))
            total_chars += len(text)

        # Step 4: render and pack into chunks.
        selection.selected_paths = [rel for rel, _ in selected]
        selection.chunks = self._build_chunks(selected)
        return selection

    # ----------------------------------------------------------------- helpers

    def _find_files(self, root: Path) -> tuple[list[str], int]:
        """Walk the folder and return (python file paths, count of other files).

        Paths are relative to root and use '/' on every OS, matching the paths
        the model will cite and the evidence validator will check. Symlinks
        are skipped (the clone already turns them into plain files; this is a
        second safety layer).
        """
        python_files: list[str] = []
        other_files = 0
        for dirpath, dirnames, filenames in os.walk(root):
            # Editing dirnames in place stops os.walk from entering ignored folders.
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in IGNORED_DIRS and not d.endswith(".egg-info")
            )
            for name in sorted(filenames):
                full = Path(dirpath) / name
                if full.is_symlink() or not full.is_file():
                    continue
                if name.endswith(".py"):
                    python_files.append(full.relative_to(root).as_posix())
                else:
                    other_files += 1
        return python_files, other_files

    def _build_chunks(self, files: list[tuple[str, list[str]]]) -> list[Chunk]:
        """Pack rendered file slices into chunks of at most chunk_chars.

        A slice is a run of consecutive lines from one file. Slices are added
        to the current chunk until the next one would not fit; then a new
        chunk starts. A single line longer than chunk_chars still gets its own
        chunk (it cannot be split without breaking the line numbers).
        """
        budget = self._limits.chunk_chars
        chunks: list[Chunk] = []
        current_text: list[str] = []
        current_parts: list[ChunkPart] = []
        current_size = 0

        def flush() -> None:
            nonlocal current_text, current_parts, current_size
            if current_parts:
                chunks.append(
                    Chunk(
                        id=f"chunk-{len(chunks) + 1}",
                        text="".join(current_text),
                        parts=tuple(current_parts),
                    )
                )
            current_text, current_parts, current_size = [], [], 0

        for path, lines in files:
            for start, end in _slice_ranges(lines, budget, path):
                block = render_slice(path, lines, start, end)
                if current_size + len(block) > budget:
                    flush()
                current_text.append(block)
                current_parts.append(ChunkPart(path, start, end))
                current_size += len(block)
        flush()
        return chunks


# ---------------------------------------------------------------------------
# Rendering helpers (module-level so they are easy to test and reuse)
# ---------------------------------------------------------------------------

def risk_score(rel_path: str) -> int:
    """How many risk keywords appear in the path. Higher = reviewed earlier."""
    lowered = rel_path.lower()
    return sum(1 for word in RISK_KEYWORDS if word in lowered)


def render_slice(path: str, lines: list[str], start: int, end: int) -> str:
    """Render lines start..end (1-based, inclusive) of one file for the model.

    Example:
        === FILE: app/utils.py (lines 5-7 of 11) ===
        5| def ping(host):
        6|     # comment
        7|     return subprocess.run(...)

    Every code line starts with "<number>| ", so text inside the file can
    never look like a real "=== FILE ===" header. That makes it harder for
    untrusted repository content to impersonate our own structure.
    """
    header = f"=== FILE: {path} (lines {start}-{end} of {len(lines)}) ===\n"
    body = "".join(f"{n}| {lines[n - 1]}\n" for n in range(start, end + 1))
    return header + body + "\n"


def _slice_ranges(lines: list[str], budget: int, path: str) -> list[tuple[int, int]]:
    """Split one file into line ranges whose rendered size fits the budget.

    Returns 1-based inclusive (start, end) pairs covering every line once.
    """
    ranges: list[tuple[int, int]] = []
    header_size = len(f"=== FILE: {path} (lines {len(lines)}-{len(lines)} of {len(lines)}) ===\n\n")
    start = 1
    size = header_size
    for n, line in enumerate(lines, start=1):
        line_size = len(f"{n}| {line}\n")
        if size + line_size > budget and n > start:
            ranges.append((start, n - 1))
            start, size = n, header_size
        size += line_size
    if lines:
        ranges.append((start, len(lines)))
    return ranges