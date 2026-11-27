"""
Tests for repo.py

No network is used: RepoFetcher receives a fake `runner` instead of
subprocess.run, so we control exactly what "git" returns.
"""

import subprocess
from pathlib import Path

import pytest

from secreview.repo import (
    InvalidRepoUrlError,
    RepoFetcher,
    RepoFetchError,
    parse_github_url,
)

FAKE_SHA = "a" * 40


# ---------------------------------------------------------------- fake git

class FakeGit:
    """Pretends to be git. Records every command it receives.

    Args:
        clone_returncode / clone_stderr: what `git clone` should return.
        sha_output: what `git rev-parse HEAD` should print.
        raise_on_clone: an exception to raise instead (e.g. a timeout).
    """

    def __init__(self, clone_returncode=0, clone_stderr="", sha_output=FAKE_SHA + "\n",
                 raise_on_clone=None):
        self.clone_returncode = clone_returncode
        self.clone_stderr = clone_stderr
        self.sha_output = sha_output
        self.raise_on_clone = raise_on_clone
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "clone" in command:
            if self.raise_on_clone:
                raise self.raise_on_clone
            if self.clone_returncode == 0:
                # Simulate a successful clone: create the folder with one file.
                dest = Path(command[-1])
                dest.mkdir(parents=True)
                (dest / "main.py").write_text("print('hello')\n")
            return subprocess.CompletedProcess(command, self.clone_returncode, "", self.clone_stderr)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, self.sha_output, "")
        raise AssertionError(f"unexpected git command: {command}")


# ---------------------------------------------------------- URL validation

@pytest.mark.parametrize(
    "url, owner, name",
    [
        ("https://github.com/pallets/flask", "pallets", "flask"),
        ("https://github.com/pallets/flask.git", "pallets", "flask"),
        ("https://github.com/pallets/flask/", "pallets", "flask"),
        ("  https://github.com/my-org/my_repo.v2  ", "my-org", "my_repo.v2"),
    ],
)
def test_accepts_valid_github_urls(url, owner, name):
    ref = parse_github_url(url)
    assert (ref.owner, ref.name) == (owner, name)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "http://github.com/pallets/flask",            # not https
        "https://gitlab.com/pallets/flask",           # other host
        "https://github.com/pallets",                 # no repo
        "https://github.com/pallets/flask/tree/main", # sub-page
        "https://github.com/pallets/flask?tab=x",     # query string
        "https://user:pass@github.com/pallets/flask", # embedded credentials
        "git@github.com:pallets/flask.git",           # SSH
        "https://github.com/pallets/..",              # path trick
        "https://github.com/--upload-pack=evil/flask",# git option injection
    ],
)
def test_rejects_invalid_urls(url):
    with pytest.raises(InvalidRepoUrlError):
        parse_github_url(url)


def test_invalid_url_never_calls_git():
    git = FakeGit()
    with pytest.raises(InvalidRepoUrlError):
        with RepoFetcher(10, runner=git).checkout("http://evil.example/x"):
            pass
    assert git.commands == []


# ------------------------------------------------------------ successful clone

def test_checkout_yields_snapshot_and_cleans_up():
    git = FakeGit()
    url = "https://github.com/pallets/flask"
    with RepoFetcher(10, runner=git).checkout(url) as snapshot:
        assert snapshot.commit_sha == FAKE_SHA
        assert snapshot.url == url
        assert (snapshot.root / "main.py").is_file()
        root = snapshot.root
    assert not root.exists(), "temporary clone folder must be deleted"


def test_clone_command_is_hardened_and_uses_rebuilt_url():
    git = FakeGit()
    with RepoFetcher(10, runner=git).checkout("https://github.com/pallets/flask/"):
        pass
    clone = git.commands[0]
    assert clone[0] == "git"
    assert "--depth" in clone and "1" in clone
    assert "core.hooksPath=/dev/null" in clone
    assert "core.symlinks=false" in clone
    # The URL comes after "--" and is the canonical form, not the raw input.
    assert clone[clone.index("--") + 1] == "https://github.com/pallets/flask.git"


def test_cleans_up_even_if_review_raises():
    git = FakeGit()
    with pytest.raises(RuntimeError):
        with RepoFetcher(10, runner=git).checkout("https://github.com/a/b") as snapshot:
            root = snapshot.root
            raise RuntimeError("review crashed")
    assert not root.exists()


# --------------------------------------------------------------- clone failures

def test_missing_or_private_repo():
    git = FakeGit(clone_returncode=128, clone_stderr="remote: Repository not found.\nfatal: ...") #Arrange
    with pytest.raises(RepoFetchError, match="not found or is not public"): #Assert
        with RepoFetcher(10, runner=git).checkout("https://github.com/a/missing"): #Act
            pass


def test_network_failure():
    git = FakeGit(clone_returncode=128, clone_stderr="fatal: unable to access ... Could not resolve host")
    with pytest.raises(RepoFetchError, match="network"):
        with RepoFetcher(10, runner=git).checkout("https://github.com/a/b"):
            pass


def test_clone_timeout():
    git = FakeGit(raise_on_clone=subprocess.TimeoutExpired(cmd="git", timeout=10))
    with pytest.raises(RepoFetchError, match="timed out"):
        with RepoFetcher(10, runner=git).checkout("https://github.com/a/b"):
            pass


def test_git_not_installed():
    git = FakeGit(raise_on_clone=FileNotFoundError("git"))
    with pytest.raises(RepoFetchError, match="not installed"):
        with RepoFetcher(10, runner=git).checkout("https://github.com/a/b"):
            pass


def test_unreadable_commit_sha():
    git = FakeGit(sha_output="not-a-sha\n")
    with pytest.raises(RepoFetchError, match="commit SHA"):
        with RepoFetcher(10, runner=git).checkout("https://github.com/a/b"):
            pass