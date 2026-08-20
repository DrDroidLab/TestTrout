"""Execution results and how failures are classified.

The classification taxonomy is the load-bearing part of this module. A test
suite that reports "3 failed" teaches a team to ignore it; one that
distinguishes "the product broke" from "the database was unreachable" from
"this test is flaky" is one they act on.

Classification is deterministic wherever possible — exit codes, reporter
output, and repeat behaviour decide it, not a model. The model's only role is
explaining a failure that has *already* been classified, which is the
difference between an explanation and a guess.

One rule runs through everything here: an inconclusive result is never
upgraded to a pass. If the environment fell over, that is what gets reported.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Classification(StrEnum):
    """Why a scenario ended the way it did."""

    PASSED = "passed"

    ASSERTION_FAILURE = "assertion_failure"
    """The product did not behave as the scenario asserted. The only class that
    is a candidate for a real regression."""

    AUTH_FAILURE = "auth_failure"
    """Could not sign in. Says nothing about the product."""

    ENVIRONMENT_FAILURE = "environment_failure"
    """The deployment was unreachable, or the toolchain could not start."""

    DEPENDENCY_FAILURE = "dependency_failure"
    """A service the test needs — usually the database — failed."""

    CONTRACT_MISMATCH = "contract_mismatch"
    """An outbound request matched no substitution contract and was refused.
    Never treated as a pass: a mock that silently matches nothing is how a
    suite starts reporting green while testing nothing."""

    TIMEOUT = "timeout"

    FLAKE = "flake"
    """Inconsistent across repeats of identical input. Auto-quarantined."""

    SKIPPED = "skipped"

    INCONCLUSIVE = "inconclusive"
    """Nothing reliable can be said. Never resolved to a pass."""

    @property
    def is_product_signal(self) -> bool:
        """Whether this says anything about the application under test.

        Only an assertion failure does. Everything else is about the harness,
        the environment, or the test itself — and reporting those as product
        failures is how a suite loses credibility.
        """
        return self is Classification.ASSERTION_FAILURE

    @property
    def is_success(self) -> bool:
        """Whether the scenario met its expectations."""
        return self is Classification.PASSED


class RunStatus(StrEnum):
    """The overall verdict for a run."""

    PASS = "pass"
    WARNING = "warning"
    """Non-blocking failures, quarantined scenarios, or approved limitations."""
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    """Environment or dependency problems prevented a reliable decision."""


class Evidence(BaseModel):
    """Artifacts backing a result.

    Paths, not contents. Traces and videos are large, and inlining them would
    make the run record unreadable and unreviewable.
    """

    model_config = ConfigDict(extra="forbid")

    trace: str | None = None
    screenshot: str | None = None
    video: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    reproduce: str | None = Field(
        default=None, description="A single command that reruns exactly this scenario."
    )


class Attempt(BaseModel):
    """One execution of one scenario.

    Kept individually rather than collapsed into a pass/fail, because the
    *pattern* across attempts is what distinguishes a real failure from a
    flake, and that distinction is only visible if the attempts survive.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    classification: Classification
    duration_seconds: float = 0.0
    message: str = ""


class ScenarioResult(BaseModel):
    """What happened to one scenario during a run."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    title: str = ""
    classification: Classification = Classification.INCONCLUSIVE
    attempts: list[Attempt] = Field(default_factory=list)
    duration_seconds: float = 0.0
    message: str = ""
    detail: str = Field(default="", description="Reporter output, trimmed.")
    evidence: Evidence = Field(default_factory=Evidence)

    @property
    def consistent(self) -> bool:
        """Whether every attempt reached the same conclusion.

        Inconsistency is the definition of a flake, and it is more informative
        than any single attempt.
        """
        if len(self.attempts) < 2:
            return True
        return len({a.classification for a in self.attempts}) == 1

    @property
    def passed(self) -> bool:
        """Whether this scenario met its expectations."""
        return self.classification.is_success


class RunRecord(BaseModel):
    """One execution of the suite. Written to ``.trout/runs/``.

    Not committed: a run describes what happened at one moment against one
    deployment, and the artifacts are large.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    id: str
    started_at: str
    finished_at: str = ""
    entrypoint: str = ""
    base_url: str = ""
    role: str | None = None
    isolation: str = ""
    results: list[ScenarioResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def status(self) -> RunStatus:
        """The overall verdict.

        Ordered so that an inconclusive run can never be reported as a pass:
        environment problems are checked before failures, and failures before
        success.
        """
        if not self.results:
            return RunStatus.INCONCLUSIVE

        classifications = [r.classification for r in self.results]
        if any(
            c
            in {
                Classification.ENVIRONMENT_FAILURE,
                Classification.DEPENDENCY_FAILURE,
                Classification.AUTH_FAILURE,
                Classification.INCONCLUSIVE,
            }
            for c in classifications
        ):
            return RunStatus.INCONCLUSIVE
        if any(c.is_product_signal for c in classifications):
            return RunStatus.FAIL
        if any(
            c in {Classification.FLAKE, Classification.CONTRACT_MISMATCH, Classification.TIMEOUT}
            for c in classifications
        ):
            return RunStatus.WARNING
        return RunStatus.PASS

    @property
    def counts(self) -> dict[str, int]:
        """Result counts by classification."""
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.classification.value] = counts.get(result.classification.value, 0) + 1
        return counts

    @property
    def regressions(self) -> list[ScenarioResult]:
        """Results that say something about the product."""
        return [r for r in self.results if r.classification.is_product_signal]

    @property
    def duration_seconds(self) -> float:
        """Total execution time."""
        return round(sum(r.duration_seconds for r in self.results), 1)
