"""
pipeline.py - the orchestrator: runs one review from URL to Report.

Aim of this file
----------------
This is the only place where the components meet. It contains no git, OpenAI
or file-format code; it only decides the ORDER of the steps and what the final
status is:

    fetch (repo.py) -> select (selector.py) -> for each chunk:
        Investigator -> EvidenceValidator -> Reviewer -> Finding
    -> build the Report (models.py)

Failure handling: nothing here crashes the review.
  - Repository problems (bad URL, not found, timeout)     -> status "failed"
  - One chunk fails (timeout, malformed output after repair)
                                     -> that chunk is recorded, review continues
  - The model call budget runs out   -> remaining chunks recorded as not reviewed
  - Permanent model errors (bad key, unknown model)
                                     -> stop immediately; nothing else can succeed

Status rules (failed/incomplete must differ from "no findings"):
  - failed    : the repo could not be fetched, it has no reviewable Python code,
                or no chunk at all was analysed successfully
  - partial   : some in-scope code was not analysed (limits, failed chunks, errors)
  - completed : every in-scope file was analysed; findings may be empty

All dependencies (fetcher, model client) are passed in, so tests can run the
whole flow offline with a local fixture folder and a scripted model.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from secreview.agents import AgentError, BudgetExhaustedError, CallBudget, Investigator, Reviewer
from secreview.config import Settings
from secreview.evidence import EvidenceValidator
from secreview.llm.interfaces import LLMClient, LLMError
from secreview.models import (
    Coverage,
    Finding,
    Report,
    ReviewStatus,
    Severity,
    SkippedFile,
)
from secreview.repo import RepoError, Snapshot
from secreview.selector import Chunk, FileSelector, Selection
from secreview.tools import RepoTools

logger = logging.getLogger(__name__)

# Report order: most severe first.
_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}


class Fetcher(Protocol):
    """Anything that can turn a URL into a Snapshot (RepoFetcher, or a test fake)."""

    def checkout(self, url: str) -> AbstractContextManager[Snapshot]: ...


@dataclass
class _ReviewState:
    """Running totals collected while the chunks are processed."""

    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reviewed_chunks: set[str] = field(default_factory=set)
    failed_chunks: dict[str, str] = field(default_factory=dict)   # chunk id -> reason
    rejected_by_validator: int = 0
    rejected_by_reviewer: int = 0
    stop_reason: str | None = None


class ReviewPipeline:
    """Runs reviews with the given settings, model client and fetcher.

    Args:
        settings: validated settings (limits are used here).
        llm: the model client shared by both agents.
        fetcher: object with checkout(url) -> context manager yielding a Snapshot.
    """

    def __init__(self, settings: Settings, llm: LLMClient, fetcher: Fetcher) -> None:
        self._settings = settings
        self._llm = llm
        self._fetcher = fetcher

    def run(self, url: str) -> Report:
        """Review one repository. Always returns a valid Report, never raises
        for expected problems (repository, model, or output errors)."""
        try:
            with self._fetcher.checkout(url) as snapshot:
                return self._review(snapshot)
        except RepoError as exc:
            logger.error("repository error: %s", exc)
            return Report.failed(url, str(exc))

    # ------------------------------------------------------------- main flow

    def _review(self, snapshot: Snapshot) -> Report:
        limits = self._settings.limits
        selection = FileSelector(limits).select(snapshot.root)
        logger.info(
            "selected %d of %d in-scope files into %d chunk(s)",
            len(selection.selected_paths), selection.files_in_scope, len(selection.chunks),
        )

        budget = CallBudget(limits.max_model_calls)
        tools = RepoTools.load(snapshot.root, selection.selected_paths, limits.tool_output_max_chars)
        validator = EvidenceValidator(snapshot.root, set(selection.selected_paths))
        investigator = Investigator(self._llm, tools, budget, limits.investigator_max_tool_calls)
        reviewer = Reviewer(self._llm, tools, budget, limits.reviewer_max_tool_calls)

        state = _ReviewState()
        for chunk in selection.chunks:
            if state.stop_reason:
                state.failed_chunks[chunk.id] = f"not reviewed: {state.stop_reason}"
                continue
            self._review_chunk(chunk, investigator, validator, reviewer, state)

        return self._build_report(snapshot, selection, state, budget)

    def _review_chunk(self, chunk: Chunk, investigator: Investigator, validator: EvidenceValidator,
                      reviewer: Reviewer, state: _ReviewState) -> None:
        """Investigate -> validate -> review one chunk, recording the outcome in state."""
        logger.info("reviewing %s (%d file part(s))", chunk.id, len(chunk.parts))
        try:
            candidates = investigator.run(chunk)

            checked = validator.validate(candidates)
            state.rejected_by_validator += len(checked.rejected)
            for rejected in checked.rejected:
                logger.info("evidence rejected for %s: %s", rejected.candidate.id, rejected.reason)

            decisions = reviewer.run(checked.accepted)
            for candidate in checked.accepted:
                decision = decisions.get(candidate.id)
                if decision is None:
                    state.errors.append(f"{candidate.id}: the Reviewer gave no decision; candidate not reported")
                elif decision.confirmed:
                    state.findings.append(Finding.from_parts(candidate, decision))
                else:
                    state.rejected_by_reviewer += 1
            state.reviewed_chunks.add(chunk.id)

        except BudgetExhaustedError as exc:
            state.stop_reason = str(exc)
            state.failed_chunks[chunk.id] = f"not completed: {exc}"
            state.errors.append(f"{chunk.id}: {exc}; remaining chunks were not reviewed")
        except LLMError as exc:
            state.failed_chunks[chunk.id] = f"model error: {exc}"
            state.errors.append(f"{chunk.id}: {exc}")
            if not exc.retryable:
                state.stop_reason = str(exc)
        except AgentError as exc:
            state.failed_chunks[chunk.id] = f"agent error: {exc}"
            state.errors.append(f"{chunk.id}: {exc}")

    # ---------------------------------------------------------------- report

    def _build_report(self, snapshot: Snapshot, selection: Selection, state: _ReviewState,
                      budget: CallBudget) -> Report:
        """Turn the collected state into a validated Report with the right status."""
        # A selected file counts as reviewed only if EVERY chunk containing it succeeded.
        incomplete_files: dict[str, str] = {}
        for chunk in selection.chunks:
            if chunk.id in state.failed_chunks:
                for path in sorted(chunk.paths):
                    incomplete_files.setdefault(path, f"{chunk.id} {state.failed_chunks[chunk.id]}")
        files_reviewed = len([p for p in selection.selected_paths if p not in incomplete_files])

        coverage = Coverage(
            files_in_scope=selection.files_in_scope,
            files_reviewed=files_reviewed,
            skipped_files=selection.skipped
            + [SkippedFile(path=p, reason=r) for p, r in incomplete_files.items()],
            chunks_total=len(selection.chunks),
            chunks_reviewed=len(state.reviewed_chunks),
            chunks_failed=len(state.failed_chunks),
            model_calls_used=budget.used,
            model_calls_limit=budget.limit,
            rejected_candidates=state.rejected_by_validator,
        )
        limitations = self._limitations(selection, state)
        findings = _sorted_unique(state.findings)

        if not selection.chunks:
            reason = (
                "no Python files found in the repository"
                if selection.files_in_scope == 0
                else "no in-scope Python file could be reviewed (all skipped by limits)"
            )
            return Report(repo_url=snapshot.url, commit_sha=snapshot.commit_sha,
                          status=ReviewStatus.FAILED, coverage=coverage,
                          limitations=limitations, errors=[reason])

        if not state.reviewed_chunks:
            return Report(repo_url=snapshot.url, commit_sha=snapshot.commit_sha,
                          status=ReviewStatus.FAILED, coverage=coverage, limitations=limitations,
                          errors=state.errors or ["no chunk could be analysed"])

        incomplete = bool(state.failed_chunks or selection.skipped or state.errors)
        status = ReviewStatus.PARTIAL if incomplete else ReviewStatus.COMPLETED
        logger.info("review %s: %d finding(s), %d model call(s)", status.value, len(findings), budget.used)
        return Report(repo_url=snapshot.url, commit_sha=snapshot.commit_sha, status=status,
                      findings=findings, coverage=coverage, limitations=limitations,
                      errors=state.errors)

    @staticmethod
    def _limitations(selection: Selection, state: _ReviewState) -> list[str]:
        """Plain-English notes about what this review does and does not cover."""
        notes = [
            "Static review only: no code was executed, so findings are not proven exploitable at runtime.",
            "Only Python (.py) files are reviewed; tests, virtual environments and build output are excluded.",
        ]
        if selection.out_of_scope_files:
            notes.append(f"{selection.out_of_scope_files} non-Python file(s) were not reviewed.")
        if selection.skipped:
            notes.append(
                f"{len(selection.skipped)} in-scope file(s) were skipped by limits or encoding "
                "(see coverage.skipped_files)."
            )
        if state.failed_chunks:
            notes.append(
                f"{len(state.failed_chunks)} chunk(s) were not fully analysed "
                "(see errors and coverage.skipped_files)."
            )
        if state.stop_reason:
            notes.append(f"The review stopped early: {state.stop_reason}.")
        if state.rejected_by_reviewer:
            notes.append(f"{state.rejected_by_reviewer} candidate(s) were judged not exploitable by the Reviewer.")
        return notes


def _sorted_unique(findings: list[Finding]) -> list[Finding]:
    """Drop duplicates (same file and lines, e.g. reported from two chunks) and
    order by severity, then file and line, so reports are stable and readable."""
    seen: set[tuple[str, int, int]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.file_path, finding.start_line, finding.end_line)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return sorted(unique, key=lambda f: (_SEVERITY_ORDER[f.severity], f.file_path, f.start_line))