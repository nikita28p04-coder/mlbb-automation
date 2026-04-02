"""
Scenario engine for MLBB automation.

ScenarioRunner executes a list of Step objects sequentially, with:
  - Checkpoint after each step (written to the run report)
  - Per-step retry (up to step.max_retries attempts)
  - Recovery via RecoveryManager on critical failures
  - Final JSON report written by RunLogger.finalize()

Usage:
    runner = ScenarioRunner(executor, run_logger, recovery_manager)
    runner.add_step(Step("google_account", google_account_fn, max_retries=2))
    runner.add_step(Step("install_mlbb", install_mlbb_fn))
    result = runner.run()  # StepResult list
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from ..actions.executor import AppiumExecutor
from ..logging.logger import RunLogger, get_logger
from ..recovery.manager import RecoveryError, RecoveryManager

logger = get_logger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 3.0


@dataclass
class Step:
    """
    A single scenario step.

    name:        Unique slug used in logs and checkpoints.
    fn:          Callable that performs the step. Receives no arguments —
                 bind any needed dependencies with functools.partial or a closure.
    max_retries: Maximum retry count on non-fatal failures (default 3).
    retry_delay: Seconds to wait between retries.
    fatal:       If True, failure immediately aborts the run without recovery.
    """

    name: str
    fn: Callable[[], None]
    max_retries: int = _DEFAULT_MAX_RETRIES
    retry_delay: float = _DEFAULT_RETRY_DELAY
    fatal: bool = False


@dataclass
class StepResult:
    """Outcome record for a single step execution."""

    name: str
    status: str  # "ok" | "failed" | "skipped"
    attempts: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ScenarioAborted(Exception):
    """Raised when a fatal step fails or recovery is exhausted."""


class ScenarioRunner:
    """
    Executes a list of Steps in order with retry and recovery support.

    Args:
        executor:  AppiumExecutor for the active session (used by recovery).
        run_logger: RunLogger for checkpoint events and final report.
        recovery:  Optional RecoveryManager; used when a step fails after all
                   retries to attempt device-level recovery before giving up.
    """

    def __init__(
        self,
        executor: AppiumExecutor,
        run_logger: RunLogger,
        recovery: Optional[RecoveryManager] = None,
    ) -> None:
        self._executor = executor
        self._run_logger = run_logger
        self._recovery = recovery
        self._steps: List[Step] = []
        self._results: List[StepResult] = []

    def add_step(self, step: Step) -> None:
        """Append a step to the run queue."""
        self._steps.append(step)

    def run(self, start_from: Optional[str] = None) -> List[StepResult]:
        """
        Execute all registered steps in order.

        Args:
            start_from: If given, skip all steps before this step name
                        (useful for resuming after a checkpoint failure).

        Returns:
            List of StepResult for every step (including skipped ones).

        Raises:
            ScenarioAborted: If a fatal step fails or recovery is exhausted.
        """
        skip = start_from is not None
        self._results = []

        for step in self._steps:
            if skip:
                if step.name == start_from:
                    skip = False
                else:
                    self._results.append(StepResult(name=step.name, status="skipped"))
                    continue

            result = self._run_step(step)
            self._results.append(result)

            if result.status == "failed":
                # Fatal steps already raised ScenarioAborted inside _run_step.
                # This branch handles non-fatal steps that exhausted all retries
                # and recovery attempts.
                raise ScenarioAborted(
                    f"Step '{step.name}' failed after {result.attempts} attempt(s): {result.error}"
                )

        return self._results

    def _run_step(self, step: Step) -> StepResult:
        """
        Execute a single step with retry and recovery.

        Returns a StepResult with status="ok" on success or status="failed"
        when all retries (and a recovery attempt) are exhausted.
        """
        from datetime import datetime, timezone

        started_at = datetime.now(timezone.utc).isoformat()
        self._run_logger.log_step(step.name, "started")
        logger.info("step_start", step=step.name)

        last_error: Optional[str] = None
        attempts = 0

        for attempt in range(1, step.max_retries + 1):
            attempts = attempt
            try:
                step.fn()
                finished_at = datetime.now(timezone.utc).isoformat()
                self._run_logger.log_step(step.name, "ok", attempts=attempt)
                logger.info("step_ok", step=step.name, attempt=attempt)
                # Checkpoint: take a screenshot of the successful step
                self._checkpoint_screenshot(step.name)
                return StepResult(
                    name=step.name,
                    status="ok",
                    attempts=attempt,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            except ScenarioAborted:
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "step_failed_attempt",
                    step=step.name,
                    attempt=attempt,
                    max_retries=step.max_retries,
                    error=last_error,
                )
                self._run_logger.log_step(
                    step.name, "retry", attempt=attempt, error=last_error
                )

                # Save error screenshot
                try:
                    img = self._executor.screenshot()
                    self._run_logger.log_error(
                        f"Step '{step.name}' attempt {attempt} failed: {last_error}",
                        step=step.name,
                        exc=exc,
                        screenshot=img,
                    )
                except Exception:
                    pass

                # Fatal steps abort immediately — no retry or recovery
                if step.fatal:
                    raise ScenarioAborted(
                        f"Fatal step '{step.name}' failed (attempt {attempt}): {last_error}"
                    )

                # Exceptions tagged as non-retriable abort immediately to prevent
                # dangerous repeat execution (e.g. duplicate payment charges).
                # A class signals this by defining _is_non_retriable = True.
                if getattr(type(exc), "_is_non_retriable", False):
                    raise ScenarioAborted(
                        f"Non-retriable error in step '{step.name}' "
                        f"(attempt {attempt}): {last_error}"
                    )

                if attempt < step.max_retries:
                    time.sleep(step.retry_delay)
                    continue

                # All retries exhausted — attempt device-level recovery
                if self._recovery is not None:
                    logger.warning(
                        "step_attempting_recovery", step=step.name
                    )
                    try:
                        self._recovery.attempt_recovery(context=step.name)
                        # Give the device a moment to settle after recovery
                        time.sleep(3)
                        # Try the step one final time post-recovery
                        step.fn()
                        finished_at = datetime.now(timezone.utc).isoformat()
                        self._run_logger.log_step(
                            step.name, "ok_after_recovery", attempts=attempt
                        )
                        logger.info(
                            "step_ok_after_recovery", step=step.name
                        )
                        self._checkpoint_screenshot(step.name)
                        return StepResult(
                            name=step.name,
                            status="ok",
                            attempts=attempt + 1,
                            started_at=started_at,
                            finished_at=finished_at,
                        )
                    except (RecoveryError, Exception) as rec_exc:
                        last_error = f"{last_error} | recovery: {rec_exc}"

        from datetime import datetime, timezone
        finished_at = datetime.now(timezone.utc).isoformat()
        self._run_logger.log_step(step.name, "failed", error=last_error, attempts=attempts)
        logger.error("step_failed", step=step.name, attempts=attempts, error=last_error)
        return StepResult(
            name=step.name,
            status="failed",
            attempts=attempts,
            error=last_error,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _checkpoint_screenshot(self, step_name: str) -> None:
        """Best-effort checkpoint screenshot after each successful step."""
        try:
            img = self._executor.screenshot()
            self._run_logger.save_screenshot(img, label=f"checkpoint_{step_name}")
        except Exception as exc:
            logger.warning("checkpoint_screenshot_failed", step=step_name, error=str(exc))
