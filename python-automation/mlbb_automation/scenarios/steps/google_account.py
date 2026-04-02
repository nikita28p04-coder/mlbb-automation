"""
Step: Add Google account to the Android device.

Full implementation in Task #3. This stub allows the CLI and imports to work.
"""

from __future__ import annotations

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    email: str,
    password: str,
    device_id: str = "",
) -> None:
    """
    Add a Google account to the device via Android Settings.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        email:      Google account email.
        password:   Google account password (no 2FA).
        device_id:  Device ID for log context.
    """
    run_logger.log_step("google_account", "started", device_id=device_id)
    logger.info("google_account step — full implementation in Task #3")
    # TODO: implement in Task #3
    run_logger.log_step("google_account", "stub_ok", device_id=device_id)
