"""
Tests for evidence.py:
"Rejection of evidence with an incorrect file, line range or quote."

All tests run offline against the small fixture repository in
tests/fixtures/sample_repo. No model, no network.

Reference: in app/utils.py, line 7 is
    return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)
"""

from pathlib import Path

import pytest

from secreview.evidence import EvidenceValidator
from secreview.models import CandidateFinding

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"
REVIEWED_FILES = {"app/utils.py", "app/routes.py"}
VULNERABLE_LINE = 'return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)'


def make_candidate(**overrides) -> CandidateFinding:
    """A valid candidate pointing at the real vulnerable line; tests override
    one field at a time to make it invalid."""
    data = {
        "id": "c1",
        "title": "Command injection",
        "file_path": "app/utils.py",
        "start_line": 7,
        "end_line": 7,
        "quote": VULNERABLE_LINE,
        "reason": "host reaches a shell",
    }
    data.update(overrides)
    return CandidateFinding(**data)


@pytest.fixture
def validator() -> EvidenceValidator:
    return EvidenceValidator(FIXTURE_REPO, REVIEWED_FILES)


# ------------------------------------------------------------- accepted cases

def test_accepts_exact_evidence(validator):
    assert validator.check(make_candidate()) is None


def test_accepts_quote_with_original_indentation(validator):
    # Models may or may not include leading whitespace; both are fine.
    assert validator.check(make_candidate(quote="    " + VULNERABLE_LINE)) is None


def test_accepts_partial_quote_within_the_line(validator):
    assert validator.check(make_candidate(quote='f"ping -c 1 {host}", shell=True')) is None


def test_accepts_multi_line_quote(validator):
    quote = "def ping(host):\n    # Vulnerable on purpose: user-controlled input reaches a shell."
    assert validator.check(make_candidate(start_line=5, end_line=6, quote=quote)) is None


def test_accepts_windows_line_endings_in_quote(validator):
    quote = "def ping(host):\r\n    # Vulnerable on purpose: user-controlled input reaches a shell."
    assert validator.check(make_candidate(start_line=5, end_line=6, quote=quote)) is None


# ----------------------------------------------------------- wrong file cases

def test_rejects_file_that_does_not_exist():
    # Allowed by name, but missing from the snapshot (e.g. a hallucinated file).
    validator = EvidenceValidator(FIXTURE_REPO, REVIEWED_FILES | {"app/missing.py"})
    reason = validator.check(make_candidate(file_path="app/missing.py"))
    assert reason is not None and "does not exist" in reason


def test_rejects_existing_but_unreviewed_file():
    # app/utils.py exists, but this validator was told only routes.py was reviewed.
    validator = EvidenceValidator(FIXTURE_REPO, {"app/routes.py"})
    reason = validator.check(make_candidate())
    assert reason is not None and "not part of the reviewed files" in reason


@pytest.mark.parametrize(
    "bad_path",
    ["../../etc/passwd", "/etc/passwd", "app/../../secret.py", "app\\utils.py"],
)
def test_rejects_unsafe_paths(validator, bad_path):
    assert validator.check(make_candidate(file_path=bad_path)) is not None


def test_rejects_symlink_pointing_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("API_KEY = 'x'\n")
    (repo / "link.py").symlink_to(outside)
    validator = EvidenceValidator(repo, {"link.py"})
    reason = validator.check(
        make_candidate(file_path="link.py", start_line=1, end_line=1, quote="API_KEY = 'x'")
    )
    assert reason is not None and "symlink" in reason


# ---------------------------------------------------- wrong line range cases

def test_rejects_line_range_past_end_of_file(validator):
    reason = validator.check(make_candidate(start_line=90, end_line=90))
    assert reason is not None and "out of bounds" in reason


def test_rejects_range_wider_than_quote(validator):
    # A one-line quote cannot justify citing lines 1-11.
    reason = validator.check(make_candidate(start_line=1, end_line=11))
    assert reason is not None and "cited range" in reason


def test_rejects_real_quote_at_wrong_line(validator):
    # The quote exists in the file, but on line 7, not line 8.
    reason = validator.check(make_candidate(start_line=8, end_line=8))
    assert reason is not None and "quote not found" in reason


# ---------------------------------------------------------- wrong quote cases

def test_rejects_paraphrased_quote(validator):
    reason = validator.check(make_candidate(quote="subprocess.run(cmd, shell=True)"))
    assert reason is not None and "quote not found" in reason


def test_rejects_whitespace_only_quote(validator):
    reason = validator.check(make_candidate(quote="   "))
    assert reason is not None and "empty" in reason


# ------------------------------------------------------------ batch behaviour

def test_validate_splits_accepted_and_rejected(validator):
    good = make_candidate(id="good")
    bad = make_candidate(id="bad", quote="this code is not in the file")
    result = validator.validate([good, bad])

    assert [c.id for c in result.accepted] == ["good"]
    assert [r.candidate.id for r in result.rejected] == ["bad"]
    assert "quote not found" in result.rejected[0].reason