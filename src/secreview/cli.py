"""
cli.py - the command-line entry point: `secreview <github-url>`.

Aim of this file
----------------
The only module that deals with the terminal. It:
  1. parses the command-line arguments;
  2. sends LOGS to stderr, so stdout carries nothing but the JSON report
  3. loads settings and the API key, building the real components
     (OpenAIClient, RepoFetcher, ReviewPipeline);
  4. runs the review and writes the JSON report to stdout or a file;
  5. returns an exit code that reflects the review status.

Even when something goes wrong before the review can start (missing API key,
invalid config, unexpected bug), a valid JSON report with status "failed" is
still produced, so scripts consuming the output always get the same format.

Exit codes:
    0  completed      the whole in-scope code was reviewed (findings may be empty)
    1  failed         the review could not be performed
    2  usage error    wrong command-line arguments (argparse's standard code)
    3  partial        some in-scope code was not reviewed (see coverage/errors)

Examples:
    secreview https://github.com/pallets/flask
    secreview https://github.com/pallets/flask --output reports/flask.json
    secreview https://github.com/pallets/flask --config my-config.yaml -v
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from secreview.config import ConfigError, Settings, load_settings
from secreview.llm.openai_client import OpenAIClient
from secreview.models import Report, ReviewStatus
from secreview.pipeline import ReviewPipeline
from secreview.repo import RepoFetcher

logger = logging.getLogger("secreview")

EXIT_CODES = {
    ReviewStatus.COMPLETED: 0,
    ReviewStatus.FAILED: 1,
    ReviewStatus.PARTIAL: 3,
}
DEFAULT_CONFIG = Path("config.yaml")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. `argv` defaults to the real command line (tests pass a list)."""
    args = _parse_args(argv)
    _configure_logging(args.verbose, args.quiet)

    report = _run(args)

    try:
        _write_report(report, args.output)
    except OSError as exc:
        logger.error("could not write the report to %s: %s", args.output, exc)
        return 1
    logger.info("status: %s, findings: %d", report.status.value, len(report.findings))
    return EXIT_CODES[report.status]


def build_pipeline(settings: Settings) -> ReviewPipeline:
    """Create the real components from settings. Tests replace this function
    to run the CLI without network access or an API key."""
    llm = OpenAIClient(
        api_key=settings.require_api_key(),
        model=settings.model.name,
        timeout_seconds=settings.model.request_timeout_seconds,
        max_retries=settings.model.max_retries,
    )
    fetcher = RepoFetcher(settings.repository.clone_timeout_seconds)
    return ReviewPipeline(settings, llm, fetcher)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Define and read the command-line options."""
    parser = argparse.ArgumentParser(
        prog="secreview",
        description="Static, multi-agent security review of a public GitHub repository (Python code).",
    )
    parser.add_argument("url", help="repository URL, e.g. https://github.com/owner/repo")
    parser.add_argument(
        "-c", "--config", type=Path, default=None,
        help="YAML settings file (default: ./config.yaml if present, otherwise built-in defaults)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="write the JSON report to this file instead of stdout",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="debug logs on stderr")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors on stderr")
    return parser.parse_args(argv)


def _configure_logging(verbose: bool, quiet: bool) -> None:
    """All logs go to stderr. Third-party HTTP logs are kept quiet unless -v."""
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    if not verbose:
        for noisy in ("openai", "httpx", "httpx2", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _run(args: argparse.Namespace) -> Report:
    """Load settings, build the pipeline and run it, turning every problem
    that prevents the review into a 'failed' report."""
    try:
        config_path = args.config
        if config_path is None and DEFAULT_CONFIG.is_file():
            config_path = DEFAULT_CONFIG
        settings = load_settings(config_path)
        logger.info("model: %s, max model calls: %d", settings.model.name, settings.limits.max_model_calls)
        pipeline = build_pipeline(settings)
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return Report.failed(args.url, f"configuration error: {exc}")

    try:
        return pipeline.run(args.url)
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        # Expected problems are handled inside the pipeline; this only catches
        # bugs, so the user still gets a valid report and the details go to the log.
        logger.exception("unexpected internal error")
        return Report.failed(args.url, f"internal error: {exc.__class__.__name__}: {exc}")


def _write_report(report: Report, output: Path | None) -> None:
    """Write the report as indented JSON to a file, or to stdout."""
    text = report.model_dump_json(indent=2) + "\n"
    if output is None:
        sys.stdout.write(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    logger.info("report written to %s", output)


if __name__ == "__main__":
    sys.exit(main())