"""Parsing test-runner output into classified results.

Both Playwright and Vitest can emit JSON, which is the only sane way to read
results — scraping human-readable output breaks the first time either project
adjusts its formatting.

The interesting work is classification. A test runner reports pass or fail; it
cannot tell you that the failure was "could not sign in" rather than "the
product is broken". That distinction is drawn here, from the error text, and it
is what stops an unreachable database from being reported as a regression.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from testtrout.domain.run import Attempt, Classification, Evidence, ScenarioResult

# Ordered most specific first. A message can match several patterns, and the
# most specific reading is almost always the right one — "could not sign in"
# beats the generic network failure it may also mention.
_PATTERNS: tuple[tuple[Classification, re.Pattern[str]], ...] = (
    (
        Classification.CONTRACT_MISMATCH,
        re.compile(r"unmatched (outbound )?request|no substitution contract|blockedbyclient", re.I),
    ),
    (
        Classification.AUTH_FAILURE,
        re.compile(r"could not sign in|invalid login credentials|sign-in (failed|rejected)", re.I),
    ),
    (
        Classification.ENVIRONMENT_FAILURE,
        re.compile(
            r"is not set|econnrefused|enotfound|net::err_connection|"
            r"failed to fetch|fetch failed|socket hang up|getaddrinfo",
            re.I,
        ),
    ),
    (
        Classification.DEPENDENCY_FAILURE,
        re.compile(r"could not read the signed-in user|supabase.*(unavailable|5\d\d)", re.I),
    ),
    # Checked before the timeout pattern on purpose. An assertion failure's
    # message is unambiguous, while "timeout" appears in plenty of stack frames
    # that have nothing to do with one.
    (
        Classification.ASSERTION_FAILURE,
        re.compile(
            r"assertionerror|expected .+ to (be|equal|contain|have)|"
            r"\.to(Be|Equal|Contain|HaveLength|BeVisible|HaveURL)\b",
            re.I,
        ),
    ),
    (
        Classification.TIMEOUT,
        # Deliberately narrow. `r"timeout"` alone matched any stack frame from a
        # file with "timeout" in its path and reported real assertion failures
        # as timeouts.
        re.compile(
            r"test timed out|timed out (in|after)|timeout of \d+\s*ms exceeded|"
            r"exceeded timeout|waiting for .+ exceeded",
            re.I,
        ),
    ),
)

# Only the head of a message is classified. Stack traces are long, full of
# unrelated file paths, and matching against all of them is how an assertion
# failure ends up labelled a timeout.
_CLASSIFY_HEAD_CHARS = 300


def classify_message(message: str) -> Classification:
    """Classify a failure from its message.

    Defaults to :attr:`Classification.ASSERTION_FAILURE` — the only class that
    counts as a product signal — because an unrecognised failure in a test that
    ran is most likely a real one. Defaulting the other way would let genuine
    regressions hide behind "probably the environment".
    """
    # The first line *is* the message; everything after it is context. Try it
    # alone first, so a clear "expected X to be Y" is not overruled by a stack
    # frame further down that happens to mention a network error.
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    for classification, pattern in _PATTERNS:
        if pattern.search(first_line):
            return classification

    head = message[:_CLASSIFY_HEAD_CHARS]
    for classification, pattern in _PATTERNS:
        if pattern.search(head):
            return classification
    return Classification.ASSERTION_FAILURE


def _scenario_id_from(title: str, fallback: str) -> str:
    """Recover the scenario id from a test file path."""
    stem = Path(fallback).stem
    for suffix in (".spec", ".test"):
        stem = stem.removesuffix(suffix)
    return stem.replace("_", ":", 2) if stem.startswith("scenario_") else stem


def parse_playwright(report: Path, evidence_dir: Path) -> list[ScenarioResult]:
    """Parse Playwright's JSON reporter output."""
    payload = _load(report)
    if payload is None:
        return []

    results: list[ScenarioResult] = []
    for spec in _walk_specs(payload.get("suites") or []):
        for test in spec.get("tests") or []:
            for result in test.get("results") or []:
                message = _first_error(result)
                status = result.get("status")
                classification = (
                    Classification.PASSED
                    if status == "passed"
                    else Classification.SKIPPED
                    if status == "skipped"
                    else Classification.TIMEOUT
                    if status == "timedOut"
                    else classify_message(message)
                )
                duration = float(result.get("duration") or 0) / 1000.0
                scenario_id = _scenario_id_from(spec.get("title", ""), spec.get("file", ""))
                results.append(
                    ScenarioResult(
                        scenario_id=scenario_id,
                        title=spec.get("title", ""),
                        classification=classification,
                        attempts=[
                            Attempt(
                                index=0,
                                classification=classification,
                                duration_seconds=duration,
                                message=message[:400],
                            )
                        ],
                        duration_seconds=duration,
                        message=message.splitlines()[0][:300] if message else "",
                        detail=message[:2000],
                        evidence=_playwright_evidence(result, evidence_dir),
                    )
                )
    return results


def parse_vitest(report: Path) -> list[ScenarioResult]:
    """Parse Vitest's JSON reporter output."""
    payload = _load(report)
    if payload is None:
        return []

    results: list[ScenarioResult] = []
    for suite in payload.get("testResults") or []:
        file_path = suite.get("name") or ""
        for assertion in suite.get("assertionResults") or []:
            message = "\n".join(assertion.get("failureMessages") or [])
            status = assertion.get("status")
            classification = (
                Classification.PASSED
                if status == "passed"
                else Classification.SKIPPED
                if status in {"skipped", "pending", "todo"}
                else classify_message(message)
            )
            duration = float(assertion.get("duration") or 0) / 1000.0
            results.append(
                ScenarioResult(
                    scenario_id=_scenario_id_from(assertion.get("title", ""), file_path),
                    title=assertion.get("fullName") or assertion.get("title", ""),
                    classification=classification,
                    attempts=[
                        Attempt(
                            index=0,
                            classification=classification,
                            duration_seconds=duration,
                            message=message[:400],
                        )
                    ],
                    duration_seconds=duration,
                    message=message.splitlines()[0][:300] if message else "",
                    detail=message[:2000],
                )
            )
    return results


def _load(report: Path) -> dict[str, Any] | None:
    """Read a JSON report, tolerating an absent or malformed file."""
    if not report.is_file():
        return None
    try:
        loaded = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _walk_specs(suites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten Playwright's nested suite structure into specs."""
    specs: list[dict[str, Any]] = []
    for suite in suites:
        specs.extend(suite.get("specs") or [])
        specs.extend(_walk_specs(suite.get("suites") or []))
    return specs


def _first_error(result: dict[str, Any]) -> str:
    """The first error message on a Playwright result."""
    errors = result.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "")
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return ""


def _playwright_evidence(result: dict[str, Any], evidence_dir: Path) -> Evidence:
    """Collect attachment paths produced for a failing test."""
    evidence = Evidence()
    for attachment in result.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        path = attachment.get("path")
        if not path:
            continue
        name = str(attachment.get("name") or "")
        if name == "trace":
            evidence.trace = str(path)
        elif name == "screenshot":
            evidence.screenshot = str(path)
        elif name == "video":
            evidence.video = str(path)
    return evidence
