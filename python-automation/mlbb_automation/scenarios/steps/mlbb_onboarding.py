"""
Step: Launch MLBB, skip onboarding, close popups, reach main menu.

Full implementation in Task #3. This stub allows the CLI and imports to work.
"""

from __future__ import annotations

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str = "",
) -> None:
    """
    Launch MLBB and navigate to the main menu.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        device_id:  Device ID for log context.
    """
    run_logger.log_step("mlbb_onboarding", "started", device_id=device_id)
    logger.info("mlbb_onboarding step — full implementation in Task #3")
    # TODO: implement in Task #3
    run_logger.log_step("mlbb_onboarding", "stub_ok", device_id=device_id)
