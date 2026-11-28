"""
Tests for pipeline.py - the whole review, end to end, fully offline.

Covers the five cases:
  1. a successful review produces a valid report
  2. evidence with an incorrect file, line range or quote is rejected
  3. malformed model output is handled
  4. a model timeout / request failure is handled
  5. a completed review with zero findings is reported as completed
plus repository failures, the call budget, and permanent model errors.

How: the fetcher is replaced by FixtureFetcher (yields the local fixture repo
instead of cloning) and the model by FakeLLMClient (scripted answers). Each
script lists, in order, exactly what the "model" answers to each request.

Fixture layout (with the default chunk size, both files are in "chunk-1"):
  app/utils.py  line 7: return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)
  app/routes.py line 9: host = request.args.get("host")
With SMALL_CHUNKS, routes.py is "chunk-1" and utils.py is "chunk-2".
"""

import json
from contextlib import contextmanager
from pathlib import Path

from secreview.config import Limits, Settings
from secreview.llm.fake_client import FakeLLMClient, tool_call_response
from secreview.llm.interfaces import LLMError
from secreview.models import Report, ReviewStatus
from secreview.pipeline import ReviewPipeline
from secreview.repo import RepoFetchError, Snapshot, parse_github_url

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"
URL = "https://github.com/example/sample"
SHA = "b" * 40
VULNERABLE_LINE = 'return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)'
SMALL_CHUNKS = {"chunk_chars": 700}   # splits the fixture into two chunks


# ------------------------------------------------------------------- helpers

class FixtureFetcher:
    """Stands in for RepoFetcher: validates the URL like the real one, then
    yields the local fixture folder instead of cloning."""

    def __init__(self, root: Path = FIXTURE_REPO, error: Exception | None = None) -> None:
        self.root = root
        self.error = error

    @contextmanager
    def checkout(self, url: str):
        parse_github_url(url)            # same URL rules as production
        if self.error:
            raise self.error
        yield Snapshot(url=url, root=self.root, commit_sha=SHA)


def make_pipeline(script, fetcher=None, **limit_overrides):
    """Build a pipeline with a scripted model; returns (pipeline, fake_model)."""
    fake = FakeLLMClient(script)
    settings = Settings(limits=Limits(**limit_overrides))
    return ReviewPipeline(settings, fake, fetcher or FixtureFetcher()), fake


def candidates(*items) -> str:
    """Investigator answer as JSON text."""
    return json.dumps({"candidates": list(items)})


def candidate(**overrides) -> dict:
    data = {
        "id": "c1",
        "title": "Command injection via shell=True",
        "file_path": "app/utils.py",
        "start_line": 7,
        "end_line": 7,
        "quote": VULNERABLE_LINE,
        "reason": "host may come from the request",
    }
    data.update(overrides)
    return data


def decisions(*items) -> str:
    """Reviewer answer as JSON text."""
    return json.dumps({"decisions": list(items)})


def decision(candidate_id="chunk-1:c1", confirmed=True, severity="high") -> dict:
    return {
        "candidate_id": candidate_id,
        "confirmed": confirmed,
        "severity": severity,
        "explanation": "host comes from request.args and reaches a shell unvalidated.",
        "remediation": "Pass an argument list without shell=True and validate host.",
    }


def assert_valid_json_report(report: Report) -> dict:
    """The report must survive a round trip through JSON and our schema."""
    data = json.loads(report.model_dump_json())
    Report.model_validate(data)
    return data


# ------------------------------------------------------- 1. successful review

def test_successful_review_produces_valid_report():
    pipeline, fake = make_pipeline([
        tool_call_response("search_code", text="ping("),                       # investigator looks around
        candidates(candidate()),                                               # investigator answers
        tool_call_response("read_file", path="app/routes.py", start_line=8, end_line=10),  # reviewer checks
        decisions(decision()),                                                 # reviewer answers
    ])
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.COMPLETED
    assert report.commit_sha == SHA
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert (finding.file_path, finding.start_line, finding.quote) == ("app/utils.py", 7, VULNERABLE_LINE)
    assert finding.severity.value == "high"
    assert report.coverage.files_reviewed == report.coverage.files_in_scope == 2
    assert report.coverage.model_calls_used == 4
    assert report.errors == []
    assert fake.remaining == 0
    data = assert_valid_json_report(report)
    assert set(data["findings"][0]) == {
        "title", "severity", "file_path", "start_line", "end_line", "quote", "explanation", "remediation",
    }

# ------------------------------------------- 2. incorrect evidence is rejected

def test_incorrect_file_lines_and_quote_are_rejected_before_review():
    pipeline, fake = make_pipeline([
        candidates(
            candidate(id="wrong-file", file_path="app/missing.py"),
            candidate(id="wrong-lines", start_line=90, end_line=90),
            candidate(id="wrong-quote", quote="subprocess.call(cmd, shell=True)"),
        ),
        # No reviewer answer scripted: with no valid candidates it must not be called.
    ])
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.COMPLETED
    assert report.findings == []
    assert report.coverage.rejected_candidates == 3
    assert len(fake.requests) == 1          # investigator only


def test_only_valid_evidence_reaches_the_reviewer():
    pipeline, fake = make_pipeline([
        candidates(candidate(), candidate(id="fake", quote="os.system(cmd)")),
        decisions(decision()),
    ])
    report = pipeline.run(URL)

    reviewer_task = fake.requests[1].messages[1].content
    assert "os.system(cmd)" not in reviewer_task
    assert len(report.findings) == 1
    assert report.coverage.rejected_candidates == 1


# ------------------------------------------------ 3. malformed model output

def test_malformed_output_is_repaired_once():
    pipeline, _ = make_pipeline(["this is not json", candidates()])
    report = pipeline.run(URL)
    assert report.status is ReviewStatus.COMPLETED


def test_malformed_output_twice_fails_that_chunk_only():
    pipeline, _ = make_pipeline(
        [
            "not json", '{"candidates": "wrong type"}',   # chunk-1: invalid, then invalid repair
            candidates(),                                 # chunk-2: fine
        ],
        **SMALL_CHUNKS,
    )
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.PARTIAL
    assert report.coverage.chunks_failed == 1
    assert report.coverage.files_reviewed == 1
    assert "chunk-1" in report.errors[0] and "invalid" in report.errors[0]
    assert [s.path for s in report.coverage.skipped_files] == ["app/routes.py"]
    assert_valid_json_report(report)


def test_malformed_output_on_only_chunk_is_a_failed_review():
    pipeline, _ = make_pipeline(["not json", "still not json"])
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.FAILED
    assert report.findings == []
    assert report.errors


# ------------------------------------------- 4. timeout / request failure

def test_model_timeout_marks_chunk_failed_and_continues():
    pipeline, _ = make_pipeline(
        [
            LLMError("model request timed out (after retries)", retryable=True),  # chunk-1
            candidates(candidate()),                                               # chunk-2 investigator
            decisions(decision(candidate_id="chunk-2:c1")),                        # chunk-2 reviewer
        ],
        **SMALL_CHUNKS,
    )
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.PARTIAL
    assert len(report.findings) == 1
    assert any("timed out" in error for error in report.errors)
    assert report.coverage.chunks_reviewed == 1 and report.coverage.chunks_failed == 1


def test_permanent_model_error_stops_the_review():
    pipeline, fake = make_pipeline(
        [LLMError("model API rejected the credentials", retryable=False)],
        **SMALL_CHUNKS,
    )
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.FAILED
    assert len(fake.requests) == 1                     # chunk-2 was never attempted
    assert report.coverage.chunks_failed == 2
    assert any("credentials" in error for error in report.errors)


def test_call_budget_exhaustion_is_reported_as_partial():
    pipeline, _ = make_pipeline([candidates()], max_model_calls=1, **SMALL_CHUNKS)
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.PARTIAL
    assert report.coverage.model_calls_used == 1
    assert any("max_model_calls" in error for error in report.errors)
    assert any("stopped early" in note for note in report.limitations)


# ------------------------------------------------ 5. zero findings, completed

def test_zero_findings_is_completed_not_failed():
    pipeline, fake = make_pipeline([candidates()])
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.COMPLETED
    assert report.findings == []
    assert report.errors == []
    assert report.coverage.files_reviewed == report.coverage.files_in_scope == 2
    assert len(fake.requests) == 1                     # no candidates -> no reviewer call
    assert assert_valid_json_report(report)["status"] == "completed"


# ------------------------------------------------------ repository failures

def test_invalid_url_fails_without_calling_the_model():
    pipeline, fake = make_pipeline([])
    report = pipeline.run("http://not-github.example/x")

    assert report.status is ReviewStatus.FAILED
    assert report.commit_sha is None and report.coverage is None
    assert fake.requests == []


def test_unreachable_repository_fails():
    fetcher = FixtureFetcher(error=RepoFetchError("repository example/sample was not found or is not public"))
    pipeline, _ = make_pipeline([], fetcher=fetcher)
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.FAILED
    assert "not found" in report.errors[0]


def test_repository_without_python_files_fails(tmp_path):
    (tmp_path / "README.md").write_text("# not python\n")
    pipeline, fake = make_pipeline([], fetcher=FixtureFetcher(root=tmp_path))
    report = pipeline.run(URL)

    assert report.status is ReviewStatus.FAILED
    assert "no Python files" in report.errors[0]
    assert fake.requests == []