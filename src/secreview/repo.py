"""
repo.py - getting a snapshot of a public GitHub repository onto disk.

Aim of this file
----------------
This is the only module that talks to GitHub. It:
  1. validates the URL the user typed (only https://github.com/<owner>/<repo>);
  2. shallow-clones the repository into a temporary folder, with a timeout;
  3. reads the exact commit SHA that was cloned;
  4. deletes the folder when the review is finished.

Everything after this works on that folder (the "snapshot"), so every file,
line and quote in the report refers to one fixed commit.

Safety decisions:
  - The clone URL is rebuilt from the validated owner/repo parts; the raw user
    string is never passed to git, so it cannot inject git options.
  - git is called with an argument list (no shell), so nothing is interpreted
    by a shell.
  - Cloning only downloads files. Git hooks are never copied by a clone, and
    we additionally disable hooks, submodules, LFS downloads and symlinks
    (symlinks are checked out as small text files instead).
  - Credential prompts and helpers are disabled: a private or missing repo
    fails immediately instead of hanging or using the user's own credentials.
  - Nothing in the cloned folder is ever executed or installed.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# owner: GitHub usernames/orgs use letters, digits and single hyphens (max 39 chars).
# repo:  letters, digits, '.', '_' and '-'. An optional ".git" and trailing "/" are allowed.
# Anything else (http://, other hosts, /tree/main, ?query, user:pass@) is rejected.
_GITHUB_URL = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RepoError(Exception):
    """Base class: any problem getting the repository. The pipeline turns it
    into a report with status 'failed'."""


class InvalidRepoUrlError(RepoError):
    """The URL is not a public GitHub repository URL we accept."""


class RepoFetchError(RepoError):
    """The URL looked valid but cloning failed (not found, private, timeout, no git)."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoRef:
    """A validated owner/repo pair."""

    owner: str
    name: str

    @property
    def clone_url(self) -> str:
        """Canonical https URL rebuilt from validated parts (never the raw input)."""
        return f"https://github.com/{self.owner}/{self.name}.git"


@dataclass(frozen=True)
class Snapshot:
    """A cloned repository at one exact commit."""

    url: str          # the URL as the user gave it (goes into the report)
    root: Path        # folder containing the checked-out files
    commit_sha: str   # full 40-character SHA of the reviewed commit


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def parse_github_url(url: str) -> RepoRef:
    """Validate a repository URL and split it into owner and repo name.

    Accepts:  https://github.com/owner/repo  (optionally with .git or a trailing /)
    Rejects:  other hosts, http://, SSH URLs, sub-pages such as /tree/main,
              query strings, embedded credentials, and the names '.' / '..'.

    Raises:
        InvalidRepoUrlError: with a message saying what format is expected.
    """
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise InvalidRepoUrlError(
            f"not a supported repository URL: {url!r} "
            "(expected https://github.com/<owner>/<repo>)"
        )
    owner, name = match.group("owner"), match.group("repo")
    if name in {".", ".."}:
        raise InvalidRepoUrlError(f"invalid repository name in URL: {url!r}")
    return RepoRef(owner=owner, name=name)


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------

# Type of the function used to run commands. In production it is
# subprocess.run; tests pass a fake so they never touch the network.
Runner = Callable[..., subprocess.CompletedProcess]


class RepoFetcher:
    """Clones repositories into temporary folders.

    Args:
        clone_timeout_seconds: maximum time allowed for `git clone`.
        runner: function used to run git (injectable for tests).
    """

    def __init__(self, clone_timeout_seconds: float, runner: Runner = subprocess.run) -> None:
        self._timeout = clone_timeout_seconds
        self._run = runner

    @contextmanager
    def checkout(self, url: str) -> Iterator[Snapshot]:
        """Clone `url` and yield a Snapshot; always delete the folder afterwards.

        Usage:
            with fetcher.checkout(url) as snapshot:
                ...review snapshot.root...
            # folder is gone here, even if the review raised an error

        Raises:
            InvalidRepoUrlError: the URL is not acceptable (nothing is downloaded).
            RepoFetchError: cloning or reading the commit failed.
        """
        ref = parse_github_url(url)
        workdir = Path(tempfile.mkdtemp(prefix="secreview-"))
        try:
            repo_dir = workdir / "repo"
            self._clone(ref, repo_dir)
            sha = self._read_commit_sha(repo_dir)
            logger.info("cloned %s/%s at %s", ref.owner, ref.name, sha)
            yield Snapshot(url=url, root=repo_dir, commit_sha=sha)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ----------------------------------------------------------------- helpers

    def _clone(self, ref: RepoRef, dest: Path) -> None:
        """Run a shallow, hardened `git clone`."""
        command = [
            "git",
            "-c", "core.hooksPath=/dev/null",   # never run any git hooks
            "-c", "core.symlinks=false",        # check out symlinks as plain files
            "-c", "credential.helper=",         # never use the user's stored credentials
            "-c", "submodule.recurse=false",    # do not fetch submodules
            "clone",
            "--depth", "1",                     # latest commit only, no history
            "--single-branch",
            "--no-tags",
            "--",                               # nothing after this is treated as an option
            ref.clone_url,
            str(dest),
        ]
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",         # fail instead of asking for a password
            "GIT_LFS_SKIP_SMUDGE": "1",         # do not download large LFS files
        }
        logger.info("cloning %s (timeout %ss)", ref.clone_url, self._timeout)
        result = self._run_git(command, env)
        if result.returncode != 0:
            raise RepoFetchError(_describe_clone_failure(ref, result.stderr))

    def _read_commit_sha(self, repo_dir: Path) -> str:
        """Return the full SHA of the checked-out commit."""
        result = self._run_git(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], env=None)
        sha = (result.stdout or "").strip()
        if result.returncode != 0 or not _SHA.match(sha):
            raise RepoFetchError("cloned repository but could not read its commit SHA")
        return sha

    def _run_git(self, command: list[str], env: dict[str, str] | None) -> subprocess.CompletedProcess:
        """Run one git command with the timeout, converting OS-level problems
        into RepoFetchError so callers only handle one error type."""
        try:
            return self._run(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepoFetchError(f"git timed out after {self._timeout} seconds") from exc
        except FileNotFoundError as exc:
            raise RepoFetchError("git is not installed or not on PATH") from exc


def _describe_clone_failure(ref: RepoRef, stderr: str | None) -> str:
    """Turn git's error output into a short, user-friendly message.

    GitHub answers 'not found' for both missing and private repositories, so
    the message mentions both. Only the last line of git's output is kept, to
    keep the report short.
    """
    text = (stderr or "").strip()
    lowered = text.lower()
    if "not found" in lowered or "could not read username" in lowered or "authentication" in lowered:
        return f"repository {ref.owner}/{ref.name} was not found or is not public"
    if "could not resolve host" in lowered or "unable to access" in lowered:
        return "could not reach github.com (network problem)"
    last_line = text.splitlines()[-1] if text else "no error output"
    return f"git clone failed: {last_line[:200]}"