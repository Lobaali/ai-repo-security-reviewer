"""
models.py - the data contracts of the whole application.

Aim of this file
----------------
This file contains NO logic that talks to GitHub, the LLM, or the disk. It only
defines the shape of the data that moves between components, using Pydantic.
Pydantic checks the data the moment an object is created, so broken data (for
example a model answer with a missing field or an invalid severity) is rejected
at the boundary instead of travelling through the program.

Every other module imports from here, but this file imports from no other
module of the project. That keeps dependencies flowing one way and makes each
component testable on its own.

A finding goes through three shapes:
    Investigator agent -> CandidateFinding   "I think there is a problem here"
    EvidenceValidator  -> checks file / lines / quote against the snapshot
    Reviewer agent     -> ReviewDecision     "confirmed, here is why and how to fix it"
    Pipeline           -> Finding            candidate (evidence) + decision (judgment)

The Reviewer's shape has no file, line or quote fields on purpose: once the
evidence has been validated, the Reviewer cannot change it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Enums: fixed lists of allowed values
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """How serious a confirmed finding is. Meanings are defined in the README.

    Inheriting from `str` makes each value serialise to plain JSON text
    ("high"), and makes Pydantic reject anything outside these four values.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(str, Enum):
    """The outcome of the whole review.

    This is what makes a failed or incomplete review distinguishable from a
    completed review with zero findings.
    """

    COMPLETED = "completed"  # every in-scope file was analyzed (findings may be empty)
    PARTIAL = "partial"      # limits or errors left some in-scope code unanalyzed
    FAILED = "failed"        # the review could not run (bad URL, clone failed, no chunk analyzed)


# ---------------------------------------------------------------------------
# Agent outputs: what the LLM is asked to return
# ---------------------------------------------------------------------------
# These models parse text produced by an LLM, so they are the first line of
# defence against malformed output. Unknown extra keys are ignored (models
# sometimes add harmless fields), but every required field must be present
# and valid, otherwise Pydantic raises ValidationError.

class CandidateFinding(BaseModel):
    """A *possible* issue reported by the Investigator. Not yet trusted."""

    id: str = Field(min_length=1, description="Short id, e.g. 'c1', used by the Reviewer to refer back.")
    title: str = Field(min_length=1)
    file_path: str = Field(min_length=1, description="Path relative to the repository root.")
    start_line: int = Field(ge=1, description="1-based, inclusive.")
    end_line: int = Field(ge=1, description="1-based, inclusive.")
    quote: str = Field(min_length=1, description="Exact code text expected at those lines.")
    reason: str = Field(min_length=1, description="The Investigator's short suspicion.")

    @model_validator(mode="after")
    def _check_line_order(self) -> CandidateFinding:
        """Reject impossible ranges such as start_line=20, end_line=10.

        Runs automatically after the individual fields are validated. Whether
        the lines actually exist in the file is checked later by the
        EvidenceValidator, because that needs the repository snapshot.
        """
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class InvestigatorOutput(BaseModel):
    """The full JSON object the Investigator must return for one chunk.

    OpenAI's JSON mode always returns a JSON *object*, so the list of
    candidates is wrapped in a `candidates` key. An empty list is valid:
    it means the agent found nothing suspicious in that chunk.
    """

    candidates: list[CandidateFinding]


class ReviewDecision(BaseModel):
    """The Reviewer's verdict on one validated candidate.

    It refers to the candidate by id only and has no file/line/quote fields,
    so the Reviewer structurally cannot alter evidence that was already
    validated.
    """

    candidate_id: str = Field(min_length=1)
    confirmed: bool
    severity: Severity
    explanation: str = Field(min_length=1, description="WHY this code is a security risk.")
    remediation: str = Field(min_length=1, description="HOW to fix it.")


class ReviewerOutput(BaseModel):
    """The full JSON object the Reviewer must return for a batch of candidates."""

    decisions: list[ReviewDecision]


# ---------------------------------------------------------------------------
# Report: what the user receives
# ---------------------------------------------------------------------------
# These models are built by our own code, never directly by an LLM, so they
# are strict: an unexpected field is a bug and raises an error.

class Finding(BaseModel):
    """A confirmed finding with validated evidence."""

    model_config = ConfigDict(extra="forbid")

    title: str
    severity: Severity
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str
    explanation: str
    remediation: str

    @classmethod
    def from_parts(cls, candidate: CandidateFinding, decision: ReviewDecision) -> Finding:
        """Join the two halves of a finding.

        Evidence (title, path, lines, quote) comes from the validated
        candidate; judgment (severity, explanation, remediation) comes from
        the Reviewer. Raises ValueError if the ids do not match, so a decision
        can never be attached to the wrong candidate.
        """
        if candidate.id != decision.candidate_id:
            raise ValueError(
                f"decision {decision.candidate_id!r} does not belong to candidate {candidate.id!r}"
            )
        return cls(
            title=candidate.title,
            severity=decision.severity,
            file_path=candidate.file_path,
            start_line=candidate.start_line,
            end_line=candidate.end_line,
            quote=candidate.quote,
            explanation=decision.explanation,
            remediation=decision.remediation,
        )


class SkippedFile(BaseModel):
    """A file that was not analysed, and why (e.g. 'max_files reached')."""

    model_config = ConfigDict(extra="forbid")

    path: str
    reason: str


class Coverage(BaseModel):
    """How much of the repository was actually analysed.

    Lets the reader see whether limits or errors affected the result
    """

    model_config = ConfigDict(extra="forbid")

    files_in_scope: int = Field(ge=0, description="Python files eligible for review.")
    files_reviewed: int = Field(ge=0, description="Files whose chunks were all analyzed successfully.")
    skipped_files: list[SkippedFile] = Field(default_factory=list)
    chunks_total: int = Field(ge=0)
    chunks_reviewed: int = Field(ge=0)
    chunks_failed: int = Field(ge=0, description="Chunks lost to model errors or the call budget.")
    model_calls_used: int = Field(ge=0)
    model_calls_limit: int = Field(ge=0)
    rejected_candidates: int = Field(
        default=0, ge=0, description="Candidates dropped by evidence validation."
    )


class Report(BaseModel):
    """The top-level JSON document produced by one review."""

    model_config = ConfigDict(extra="forbid")

    repo_url: str
    commit_sha: str | None = Field(default=None, description="None only if the repository could not be fetched.")
    status: ReviewStatus
    findings: list[Finding] = Field(default_factory=list)
    coverage: Coverage | None = Field(default=None, description="None only if the review never reached the files.")
    limitations: list[str] = Field(default_factory=list, description="Plain-English scope notes.")
    errors: list[str] = Field(default_factory=list, description="What went wrong, if anything.")

    @model_validator(mode="after")
    def _check_status_consistency(self) -> Report:
        """Make contradictory reports impossible to build.

        - A FAILED review must explain why (errors) and cannot contain findings.
        - A COMPLETED or PARTIAL review must say which commit it reviewed and
          how much it covered; otherwise "zero findings" would be meaningless.
        """
        if self.status is ReviewStatus.FAILED:
            if not self.errors:
                raise ValueError("a failed report must include at least one error")
            if self.findings:
                raise ValueError("a failed report cannot contain findings")
        else:
            if self.commit_sha is None or self.coverage is None:
                raise ValueError(f"a {self.status.value} report needs commit_sha and coverage")
        return self

    @classmethod
    def failed(cls, repo_url: str, error: str, commit_sha: str | None = None) -> Report:
        """Shortcut used when the review cannot run at all (e.g. invalid URL)."""
        return cls(repo_url=repo_url, commit_sha=commit_sha, status=ReviewStatus.FAILED, errors=[error])