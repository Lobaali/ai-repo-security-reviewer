"""
Tests for cli.py - output format, exit codes and error handling.

The real pipeline factory (`build_pipeline`) is replaced with one that uses
the fixture repository and a scripted model, so no network or API key is used.
"""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from secreview import cli
from secreview.config import Settings
from secreview.llm.fake_client import FakeLLMClient
from secreview.pipeline import ReviewPipeline
from secreview.repo import Snapshot, parse_github_url

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"
URL = "https://github.com/example/sample"


class FixtureFetcher:
    @contextmanager
    def checkout(self, url):
        parse_github_url(url)
        yield Snapshot(url=url, root=FIXTURE_REPO, commit_sha="c" * 40)


@pytest.fixture
def scripted(monkeypatch):
    """Call scripted([...answers]) to make the CLI use a fake model."""
    def install(script):
        def fake_build(settings: Settings) -> ReviewPipeline:
            return ReviewPipeline(settings, FakeLLMClient(script), FixtureFetcher())
        monkeypatch.setattr(cli, "build_pipeline", fake_build)
    return install


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Run every test in an empty folder with no API key, so a developer's
    own config.yaml or .env can never affect the result."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_completed_review_prints_only_json_and_exits_0(scripted, capsys):
    scripted(['{"candidates": []}'])
    code = cli.main([URL])

    out, err = capsys.readouterr()
    report = json.loads(out)                    # stdout is pure JSON...
    assert report["status"] == "completed"
    assert code == 0
    assert "status: completed" in err           # ...and logs went to stderr


def test_report_can_be_written_to_a_file(scripted, tmp_path, capsys):
    scripted(['{"candidates": []}'])
    target = tmp_path / "reports" / "out.json"
    code = cli.main([URL, "--output", str(target), "--quiet"])

    assert code == 0
    assert json.loads(target.read_text())["status"] == "completed"
    assert capsys.readouterr().out == ""        # nothing on stdout when writing to a file


def test_partial_review_exits_3(scripted, capsys, tmp_path):
    config = tmp_path / "small.yaml"
    config.write_text("limits:\n  chunk_chars: 700\n  max_model_calls: 1\n")
    scripted(['{"candidates": []}'])
    code = cli.main([URL, "--config", str(config)])

    assert json.loads(capsys.readouterr().out)["status"] == "partial"
    assert code == 3


def test_invalid_url_gives_failed_report_and_exits_1(scripted, capsys):
    scripted([])
    code = cli.main(["https://example.com/not/github"])

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert code == 1


def test_missing_api_key_gives_failed_report(capsys):
    # Uses the REAL build_pipeline, which needs OPENAI_API_KEY (unset by `isolated`).
    code = cli.main([URL])

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert "OPENAI_API_KEY" in report["errors"][0]
    assert code == 1


def test_invalid_config_gives_failed_report(capsys, tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text("limits:\n  max_model_calls: -5\n")
    code = cli.main([URL, "--config", str(config)])

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert "configuration error" in report["errors"][0]
    assert code == 1


def test_unexpected_bug_still_produces_valid_report(monkeypatch, capsys):
    class Broken:
        def run(self, url):
            raise RuntimeError("boom")
    monkeypatch.setattr(cli, "build_pipeline", lambda settings: Broken())
    code = cli.main([URL])

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert "internal error" in report["errors"][0]
    assert code == 1


def test_missing_url_is_a_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2