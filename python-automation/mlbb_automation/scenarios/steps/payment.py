"""
Step: Navigate to MLBB Shop and complete a real payment via Google Pay.

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
    dry_run: bool = False,
) -> None:
    """
    Open MLBB Shop → Diamonds → smallest package → Google Pay → confirm.

    Args:
        executor:   Active AppiumExecutor session.
        run_logger: RunLogger for this automation run.
        device_id:  Device ID for log context.
        dry_run:    If True, navigate to payment screen but skip final tap.
    """
    run_logger.log_step("payment", "started", device_id=device_id, dry_run=dry_run)
    logger.info("payment step — full implementation in Task #3", dry_run=dry_run)
    # TODO: implement in Task #3
    run_logger.log_step("payment", "stub_ok", device_id=device_id)
