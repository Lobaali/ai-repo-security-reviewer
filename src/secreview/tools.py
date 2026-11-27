"""
tools.py - the read-only tools the agents may call while investigating.

Aim of this file
----------------
The agents see one chunk of code at a time. To judge a suspicious line they
often need context from elsewhere ("where does `host` come from?"). These two
tools give them that, safely:

    search_code(text)                     find lines containing some text
    read_file(path, start_line, end_line) read part of a reviewed file

Safety design:
  - READ-ONLY: the tools can only return text. Nothing can be written,
    executed, installed or fetched from the network. Even if repository
    content tricks a model into calling a tool, the worst outcome is that
    the model reads more of the same repository.
  - SANDBOXED BY CONSTRUCTION: the files are loaded into memory once, and
    the tools only look up paths in that in-memory dictionary. A path like
    "../../etc/passwd" is simply not a key, so it can never reach the disk.
  - SAME SCOPE AS THE REVIEW: only the files selected for review are loaded,
    so tools, evidence validation and the coverage report all agree on what
    "the reviewed code" means.
  - BOUNDED OUTPUT: every result is cut to max_output_chars, so one tool call
    cannot flood the model's context window.
  - NEVER CRASHES THE AGENT: bad arguments become an "ERROR: ..." text result
    the model can read and correct, instead of an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

from secreview.llm.interfaces import ToolCall, ToolSpec

MAX_SEARCH_MATCHES = 30        # matches returned by one search_code call
MAX_SEARCH_TEXT = 200          # longest search string accepted
MAX_SHOWN_LINE = 200           # characters shown per matching line in search results


class RepoTools:
    """The tool set for one review, bound to the selected files.

    Args:
        files: relative path -> list of lines, for every selected file.
        max_output_chars: cap on the length of any single tool result.
    """

    def __init__(self, files: dict[str, list[str]], max_output_chars: int) -> None:
        self._files = files
        self._max_output = max_output_chars

    @classmethod
    def load(cls, root: Path, paths: list[str], max_output_chars: int) -> RepoTools:
        """Read the selected files from the snapshot into memory.

        The selector already checked these files are regular UTF-8 files, so
        any file that has become unreadable is simply left out.
        """
        files: dict[str, list[str]] = {}
        for rel in paths:
            try:
                files[rel] = (root / rel).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
        return cls(files, max_output_chars)

    # ------------------------------------------------------------- interface

    def specs(self) -> list[ToolSpec]:
        """Describe the tools to the model (name, purpose, JSON arguments)."""
        return [
            ToolSpec(
                name="search_code",
                description=(
                    "Search the reviewed repository files for lines containing an exact, "
                    "case-sensitive text (not a regex). Returns 'path:line: code' for up to "
                    f"{MAX_SEARCH_MATCHES} matches. Use it to find where a function is defined "
                    "or called, or where a variable comes from."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Exact text to look for, e.g. 'def ping'."}
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="read_file",
                description=(
                    "Read lines of one reviewed repository file, with line numbers. "
                    "Paths are relative to the repository root, e.g. 'app/routes.py'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "start_line", "end_line"],
                    "additionalProperties": False,
                },
            ),
        ]

    def execute(self, call: ToolCall) -> str:
        """Run one tool call requested by the model and return its text result.

        Always returns a string. Problems (unknown tool, invalid JSON
        arguments, missing file) are returned as "ERROR: ..." so the model can
        read the problem and try again within its tool budget.
        """
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return "ERROR: tool arguments were not valid JSON."
        if not isinstance(args, dict):
            return "ERROR: tool arguments must be a JSON object."

        if call.name == "search_code":
            result = self.search_code(args.get("text"))
        elif call.name == "read_file":
            result = self.read_file(args.get("path"), args.get("start_line"), args.get("end_line"))
        else:
            result = f"ERROR: unknown tool {call.name!r}. Available tools: search_code, read_file."
        return self._truncate(result)

    # ----------------------------------------------------------------- tools

    def search_code(self, text: object) -> str:
        """Plain substring search across the loaded files.

        A plain substring (not a regular expression) is deliberate: it is
        predictable, and a malicious pattern cannot make it run for a long
        time (regex "catastrophic backtracking").
        """
        if not isinstance(text, str) or not text.strip():
            return "ERROR: 'text' must be a non-empty string."
        if len(text) > MAX_SEARCH_TEXT:
            return f"ERROR: 'text' must be at most {MAX_SEARCH_TEXT} characters."

        matches: list[str] = []
        for path in sorted(self._files):
            for number, line in enumerate(self._files[path], start=1):
                if text in line:
                    matches.append(f"{path}:{number}: {line.strip()[:MAX_SHOWN_LINE]}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        matches.append(f"[stopped after {MAX_SEARCH_MATCHES} matches]")
                        return "\n".join(matches)
        return "\n".join(matches) if matches else f"No matches for {text!r} in the reviewed files."

    def read_file(self, path: object, start_line: object, end_line: object) -> str:
        """Return lines start_line..end_line of one loaded file, numbered like
        the chunks ("12| code"), so the model can cite them in findings."""
        if not isinstance(path, str) or path not in self._files:
            return (
                f"ERROR: {path!r} is not one of the reviewed files. "
                "Use search_code to find the right path."
            )
        if not _is_positive_int(start_line) or not _is_positive_int(end_line):
            return "ERROR: start_line and end_line must be positive integers."
        if end_line < start_line:
            return "ERROR: end_line must be greater than or equal to start_line."

        lines = self._files[path]
        if start_line > len(lines):
            return f"ERROR: {path} has only {len(lines)} lines."
        end = min(end_line, len(lines))
        body = "\n".join(f"{n}| {lines[n - 1]}" for n in range(start_line, end + 1))
        return f"{path} (lines {start_line}-{end} of {len(lines)}):\n{body}"

    # --------------------------------------------------------------- helpers

    def _truncate(self, text: str) -> str:
        """Cut a result to max_output_chars and say so, so the model knows to
        ask for a smaller range instead of assuming it saw everything."""
        if len(text) <= self._max_output:
            return text
        return text[: self._max_output] + f"\n[output truncated at {self._max_output} characters]"


def _is_positive_int(value: object) -> bool:
    """True for 1, 2, 3... (and not for True/False, which Python treats as ints)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1