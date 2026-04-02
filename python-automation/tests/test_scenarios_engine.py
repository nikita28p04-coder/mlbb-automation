"""
Unit tests for scenarios/engine.py (ScenarioRunner, Step, StepResult).

Tests cover:
  - Successful single and multi-step runs
  - Per-step retry on failure
  - Recovery attempt after retries exhausted
  - ScenarioAborted raised on fatal step failure
  - Skipping steps with start_from
  - Checkpoint screenshot captured after each successful step
  - StepResult fields populated correctly
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch
from pathlib import Path
import tempfile

import pytest
from PIL import Image

from mlbb_automation.scenarios.engine import (
    ScenarioAborted,
    ScenarioRunner,
    Step,
    StepResult,
)
from mlbb_automation.recovery.manager import RecoveryError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _white() -> Image.Image:
    return Image.new("RGB", (100, 100), (255, 255, 255))


def _make_executor():
    exe = MagicMock()
    exe.screenshot.return_value = _white()
    return exe


def _make_run_logger(tmp_path: Path):
    from mlbb_automation.logging.logger import RunLogger
    return RunLogger(run_id="test_run", log_dir=tmp_path)


def _make_recovery():
    recovery = MagicMock()
    recovery.attempt_recovery = MagicMock()
    return recovery


# ---------------------------------------------------------------------------
# Single successful step
# ---------------------------------------------------------------------------

class TestScenarioRunnerSuccess:
    def test_single_step_ok(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        called = []
        runner.add_step(Step(name="step_a", fn=lambda: called.append("a")))
        results = runner.run()

        assert len(results) == 1
        assert results[0].name == "step_a"
        assert results[0].status == "ok"
        assert results[0].attempts == 1
        assert results[0].error is None
        assert called == ["a"]

    def test_multi_step_run_in_order(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        order = []
        runner.add_step(Step("step_1", fn=lambda: order.append(1)))
        runner.add_step(Step("step_2", fn=lambda: order.append(2)))
        runner.add_step(Step("step_3", fn=lambda: order.append(3)))

        results = runner.run()
        assert order == [1, 2, 3]
        assert all(r.status == "ok" for r in results)

    def test_checkpoint_screenshot_saved_after_ok(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        runner.add_step(Step("step_a", fn=lambda: None))
        runner.run()

        # screenshot() called at least once for checkpoint
        exe.screenshot.assert_called()


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------

class TestScenarioRunnerRetry:
    def test_step_succeeds_on_second_attempt(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient error")

        runner.add_step(Step("flaky", fn=flaky, max_retries=3, retry_delay=0))
        results = runner.run()

        assert results[0].status == "ok"
        assert results[0].attempts == 2

    def test_step_fails_after_all_retries(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger, recovery=None)

        def always_fail():
            raise RuntimeError("always fails")

        runner.add_step(Step("bad", fn=always_fail, max_retries=2, retry_delay=0))

        with pytest.raises(ScenarioAborted) as exc_info:
            runner.run()

        result = runner._results[0]
        assert result.status == "failed"
        assert result.attempts == 2
        assert "always fails" in result.error

    def test_error_screenshot_attempted_on_failure(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        def boom():
            raise RuntimeError("boom")

        runner.add_step(Step("boom", fn=boom, max_retries=1, retry_delay=0))
        with pytest.raises(ScenarioAborted):
            runner.run()

        # Screenshot should have been attempted on failure
        exe.screenshot.assert_called()


# ---------------------------------------------------------------------------
# Recovery integration
# ---------------------------------------------------------------------------

class TestScenarioRunnerRecovery:
    def test_recovery_called_after_retries_exhausted(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        recovery = MagicMock()
        recovery.attempt_recovery.return_value = None

        # Step fails on all retries but succeeds after recovery
        call_count = []

        def step_fn():
            call_count.append(1)
            if len(call_count) <= 2:
                raise RuntimeError("fails until recovery")

        runner = ScenarioRunner(exe, run_logger, recovery=recovery)
        runner.add_step(Step("recoverable", fn=step_fn, max_retries=2, retry_delay=0))
        results = runner.run()

        assert results[0].status == "ok"
        recovery.attempt_recovery.assert_called_once_with(context="recoverable")

    def test_recovery_exhausted_raises_scenario_aborted(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        recovery = MagicMock()
        recovery.attempt_recovery.side_effect = RecoveryError("out of attempts")

        runner = ScenarioRunner(exe, run_logger, recovery=recovery)
        runner.add_step(
            Step("bad", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")),
                 max_retries=1, retry_delay=0)
        )

        with pytest.raises(ScenarioAborted):
            runner.run()

        recovery.attempt_recovery.assert_called_once()


# ---------------------------------------------------------------------------
# Fatal steps
# ---------------------------------------------------------------------------

class TestFatalSteps:
    def test_fatal_step_aborts_immediately(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        second_called = []

        runner.add_step(
            Step("bad_fatal", fn=lambda: (_ for _ in ()).throw(RuntimeError("fatal")),
                 max_retries=3, retry_delay=0, fatal=True)
        )
        runner.add_step(
            Step("should_not_run", fn=lambda: second_called.append(True))
        )

        with pytest.raises(ScenarioAborted) as exc_info:
            runner.run()

        assert second_called == []
        assert "bad_fatal" in str(exc_info.value)

    def test_fatal_step_does_not_retry(self, tmp_path):
        """A fatal step must abort on the FIRST failure — no retry attempts."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        recovery = _make_recovery()
        runner = ScenarioRunner(exe, run_logger, recovery)

        call_count = [0]

        def failing_fn():
            call_count[0] += 1
            raise RuntimeError("permanent failure")

        runner.add_step(
            Step("fatal_once", fn=failing_fn, max_retries=5, retry_delay=0, fatal=True)
        )

        with pytest.raises(ScenarioAborted):
            runner.run()

        # Must have been called exactly once — no retries, no recovery
        assert call_count[0] == 1
        recovery.attempt_recovery.assert_not_called()

    def test_non_fatal_step_retries_and_recovery(self, tmp_path):
        """A non-fatal step retries up to max_retries, then invokes recovery."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        recovery = _make_recovery()
        runner = ScenarioRunner(exe, run_logger, recovery)

        call_count = [0]

        def always_fail():
            call_count[0] += 1
            raise RuntimeError("fail")

        runner.add_step(
            Step("non_fatal", fn=always_fail, max_retries=2, retry_delay=0, fatal=False)
        )

        with pytest.raises(ScenarioAborted):
            runner.run()

        # Should retry up to max_retries, then attempt recovery
        assert call_count[0] >= 2
        recovery.attempt_recovery.assert_called()


# ---------------------------------------------------------------------------
# start_from (skip)
# ---------------------------------------------------------------------------

class TestStartFrom:
    def test_steps_before_start_from_are_skipped(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        ran = []
        runner.add_step(Step("s1", fn=lambda: ran.append("s1")))
        runner.add_step(Step("s2", fn=lambda: ran.append("s2")))
        runner.add_step(Step("s3", fn=lambda: ran.append("s3")))

        results = runner.run(start_from="s2")

        assert ran == ["s2", "s3"]
        assert results[0].status == "skipped"
        assert results[0].name == "s1"
        assert results[1].status == "ok"
        assert results[2].status == "ok"

    def test_start_from_first_step_runs_all(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        ran = []
        runner.add_step(Step("s1", fn=lambda: ran.append("s1")))
        runner.add_step(Step("s2", fn=lambda: ran.append("s2")))

        results = runner.run(start_from="s1")
        assert ran == ["s1", "s2"]
        assert all(r.status == "ok" for r in results)


# ---------------------------------------------------------------------------
# StepResult fields
# ---------------------------------------------------------------------------

class TestStepResultFields:
    def test_timestamps_populated(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        runner.add_step(Step("t", fn=lambda: None))
        results = runner.run()

        assert results[0].started_at is not None
        assert results[0].finished_at is not None

    def test_failed_result_contains_error_text(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        runner = ScenarioRunner(exe, run_logger)

        runner.add_step(
            Step("x", fn=lambda: (_ for _ in ()).throw(ValueError("bad value")),
                 max_retries=1, retry_delay=0)
        )
        with pytest.raises(ScenarioAborted):
            runner.run()

        assert "bad value" in runner._results[0].error
