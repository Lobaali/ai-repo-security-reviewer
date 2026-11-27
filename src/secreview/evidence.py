"""
evidence.py - deterministic verification of the evidence behind each finding.

Aim of this file
----------------
LLMs can invent file names, get line numbers wrong, or paraphrase code instead
of quoting it. 

This module does exactly: 
  - It never calls a model and never touches the network.
  - It only READS files inside the cloned snapshot folder.
  - It returns accepted and rejected candidates, each rejection with a reason,
    so the report can say how many candidates were dropped and why.

Note: passing validation proves the evidence is REAL, not that the code is
vulnerable. Judging the risk is the Reviewer agent's job.

Rules applied to each CandidateFinding (in order, first failure wins):
  1. The path is a safe relative path that stays inside the repository
     (no absolute paths, no "..", no symlinks pointing elsewhere).
  2. The path is one of the files that were selected for review.
  3. The file exists and can be read.
  4. The line range exists in the file (end_line <= number of lines).
  5. The quote spans exactly the cited lines (a 1-line quote cannot be cited
     as lines 10-50).
  6. The quote appears exactly within those lines. Only leading/trailing
     whitespace of the whole quote is ignored, because models often drop
     indentation; everything else must match character for character.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from secreview.models import CandidateFinding


@dataclass(frozen=True)
class RejectedCandidate:
    """A candidate that failed validation, with a human-readable reason."""

    candidate: CandidateFinding
    reason: str


@dataclass
class ValidationResult:
    """Output of EvidenceValidator.validate(): the two groups of candidates."""

    accepted: list[CandidateFinding] = field(default_factory=list)
    rejected: list[RejectedCandidate] = field(default_factory=list)


class EvidenceValidator:
    """Checks candidates against one repository snapshot.

    Args:
        repo_root: folder containing the cloned repository (the snapshot).
        allowed_paths: relative POSIX paths (e.g. "app/utils.py") of the files
            selected for review. A finding citing any other file is rejected,
            even if that file exists (e.g. a README containing injected text).
    """

    def __init__(self, repo_root: Path, allowed_paths: set[str]) -> None:
        self._root = repo_root.resolve()
        self._allowed = set(allowed_paths)
        # Cache of file path -> list of lines, so a file cited by several
        # candidates is read from disk only once.
        self._lines_cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ public

    def validate(self, candidates: list[CandidateFinding]) -> ValidationResult:
        """Split candidates into accepted and rejected (with reasons)."""
        result = ValidationResult()
        for candidate in candidates:
            reason = self.check(candidate)
            if reason is None:
                result.accepted.append(candidate)
            else:
                result.rejected.append(RejectedCandidate(candidate, reason))
        return result

    def check(self, candidate: CandidateFinding) -> str | None:
        """Validate one candidate.

        Returns None if the evidence is valid, otherwise the reason it was
        rejected. Returning a reason instead of raising keeps the caller
        simple: one bad candidate never stops the others from being checked.
        """
        # Rules 1-2: the path must be safe and in the reviewed set.
        path_error = self._check_path(candidate.file_path)
        if path_error:
            return path_error

        # Rule 3: the file must be readable text.
        lines = self._read_lines(candidate.file_path)
        if lines is None:
            return f"file {candidate.file_path!r} could not be read as UTF-8 text"

        # Rule 4: the cited lines must exist.
        if candidate.end_line > len(lines):
            return (
                f"line range {candidate.start_line}-{candidate.end_line} is out of bounds "
                f"({candidate.file_path!r} has {len(lines)} lines)"
            )

        # Rule 5: the quote must cover exactly the cited range.
        quote = _normalise_newlines(candidate.quote).strip()
        if not quote:
            return "quote is empty or whitespace only"
        quote_line_count = len(quote.split("\n"))
        cited_line_count = candidate.end_line - candidate.start_line + 1
        if quote_line_count != cited_line_count:
            return (
                f"quote has {quote_line_count} line(s) but the cited range "
                f"{candidate.start_line}-{candidate.end_line} has {cited_line_count}"
            )

        # Rule 6: the quote must appear inside exactly those lines.
        # Lines are 1-based in the report, Python lists are 0-based.
        cited_text = "\n".join(lines[candidate.start_line - 1 : candidate.end_line])
        if quote not in cited_text:
            return (
                f"quote not found at {candidate.file_path}:"
                f"{candidate.start_line}-{candidate.end_line}"
            )

        return None

    # ----------------------------------------------------------------- helpers

    def _check_path(self, file_path: str) -> str | None:
        """Rules 1-2. Returns a rejection reason, or None if the path is fine.

        The order matters: unsafe paths are rejected before the file system is
        touched, so a path like "../../etc/passwd" is never even opened.
        """
        posix = PurePosixPath(file_path)
        if posix.is_absolute() or "\\" in file_path:
            return f"path {file_path!r} must be relative and use '/' separators"
        if ".." in posix.parts:
            return f"path {file_path!r} must not contain '..'"
        if file_path not in self._allowed:
            return f"file {file_path!r} was not part of the reviewed files"

        # Defence in depth: even an allowed name could be a symlink that
        # points outside the snapshot. Resolve it and make sure it stays inside.
        full_path = self._root / posix
        if full_path.is_symlink():
            return f"path {file_path!r} is a symlink"
        try:
            resolved = full_path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            return f"file {file_path!r} does not exist in the snapshot"
        if not resolved.is_relative_to(self._root):
            return f"path {file_path!r} resolves outside the repository"
        if not resolved.is_file():
            return f"path {file_path!r} is not a regular file"
        return None

    def _read_lines(self, file_path: str) -> list[str] | None:
        """Read a file as a list of lines (cached). Returns None if not UTF-8."""
        if file_path not in self._lines_cache:
            try:
                text = (self._root / file_path).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                return None
            # splitlines() handles \n, \r\n and \r, so Windows line endings work.
            self._lines_cache[file_path] = text.splitlines()
        return self._lines_cache[file_path]


def _normalise_newlines(text: str) -> str:
    """Convert Windows (\\r\\n) and old Mac (\\r) line endings to \\n."""
    return text.replace("\r\n", "\n").replace("\r", "\n")