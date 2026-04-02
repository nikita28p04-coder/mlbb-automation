"""
Step: Install Mobile Legends: Bang Bang from Google Play Store.

Full implementation in Task #3. This stub allows the CLI and imports to work.
"""

from __future__ import annotations

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

MLBB_PACKAGE = "com.mobile.legends"


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str = "",
) -> None:
    """
    Open Google Play Store and install MLBB.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        device_id:  Device ID for log context.
    """
    run_logger.log_step("install_mlbb", "started", device_id=device_id)
    logger.info("install_mlbb step — full implementation in Task #3")
    # TODO: implement in Task #3
    run_logger.log_step("install_mlbb", "stub_ok", device_id=device_id)
