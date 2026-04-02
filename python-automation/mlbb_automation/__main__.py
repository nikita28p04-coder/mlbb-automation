"""
CLI entry point for mlbb_automation.

Usage:
    python -m mlbb_automation --help
    python -m mlbb_automation run --config config.yaml
    python -m mlbb_automation devices --config config.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click

from .config.settings import load_settings
from .logging.logger import get_logger, make_run_id

logger = get_logger(__name__)


@click.group()
@click.version_option(version="0.1.0", prog_name="mlbb-automation")
def cli() -> None:
    """Mobile Legends: Bang Bang automation via Selectel Mobile Farm."""


# ---------------------------------------------------------------------------
# `devices` — list available devices on the farm
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--config", default="config.yaml", help="Path to config YAML file.")
@click.option("--platform-version", default=None, help="Filter by Android version.")
@click.option("--model", default=None, help="Filter by device model substring.")
def devices(config: str, platform_version: Optional[str], model: Optional[str]) -> None:
    """List available Android devices on the Selectel farm."""
    from .device_farm.selectel_client import create_client_from_settings

    settings = load_settings(config)
    client = create_client_from_settings(settings)

    click.echo("Fetching available devices...")
    device_list = client.list_devices(platform_version=platform_version, model=model)

    if not device_list:
        click.echo("No available devices found.")
        sys.exit(1)

    click.echo(f"\nFound {len(device_list)} available device(s):\n")
    for d in device_list:
        click.echo(
            f"  [{d.id}] {d.model} — Android {d.platform_version} — {d.status}"
        )


# ---------------------------------------------------------------------------
# `run` — execute the full automation scenario
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--config", default="config.yaml", show_default=True, help="Path to config YAML file.")
@click.option("--step", default=None, help="Run only this specific step (for debugging).")
@click.option("--dry-run", is_flag=True, default=False, help="Skip the final payment tap.")
@click.option("--device-id", default=None, help="Reserve this specific device ID.")
@click.option("--report-dir", default=None, help="Override log_dir from config.")
def run(
    config: str,
    step: Optional[str],
    dry_run: bool,
    device_id: Optional[str],
    report_dir: Optional[str],
) -> None:
    """
    Run the full MLBB automation scenario:
      1. Add Google account to device
      2. Install Mobile Legends: Bang Bang
      3. Navigate to shop and make a real payment via Google Pay
    """
    settings = load_settings(config)
    if report_dir:
        settings.log_dir = Path(report_dir)

    if dry_run:
        click.echo("DRY RUN mode — final payment tap will be skipped.")

    run_id = make_run_id()
    click.echo(f"Starting run: {run_id}")

    from .device_farm.selectel_client import create_client_from_settings
    from .actions.executor import AppiumExecutor
    from .logging.logger import RunLogger
    from .recovery.manager import RecoveryManager

    farm_client = create_client_from_settings(settings)
    run_logger = RunLogger(run_id=run_id, log_dir=settings.log_dir, log_level=settings.log_level)

    reserved = None
    success = False
    try:
        # 1. Acquire device
        click.echo("Acquiring device from Selectel farm...")
        run_logger.log_step("acquire_device", "started")
        
        if device_id:
            # Get all devices and pick the requested one
            all_devices = farm_client.list_devices()
            matching = [d for d in all_devices if d.id == device_id]
            if not matching:
                raise RuntimeError(f"Device {device_id} not available")
        
        reserved = farm_client.acquire_device(
            platform_version=settings.device_filter.platform_version,
            model=settings.device_filter.model,
        )
        run_logger.log_step(
            "acquire_device",
            "completed",
            device_id=reserved.device_info.id,
            model=reserved.device_info.model,
        )
        click.echo(f"Reserved device: {reserved.device_info.model} (id={reserved.device_info.id})")

        # 2. Start Appium session and run scenario
        with AppiumExecutor(
            reserved,
            retry_count=settings.retry_count,
            retry_delay=settings.retry_delay_seconds,
            action_timeout=settings.action_timeout_seconds,
            device_id=reserved.device_info.id,
        ) as executor:
            recovery = RecoveryManager(
                executor=executor,
                app_package="com.mobile.legends",
            )
            recovery.start_watchdog()

            try:
                _run_scenario(
                    executor=executor,
                    run_logger=run_logger,
                    settings=settings,
                    step=step,
                    dry_run=dry_run,
                    device_id=reserved.device_info.id,
                )
                success = True
            finally:
                recovery.stop_watchdog()

    except Exception as exc:
        logger.error("Run failed", error=str(exc), exc_info=True)
        click.echo(f"\nRUN FAILED: {exc}", err=True)
        if settings.save_screenshots_on_error and 'executor' in dir():
            try:
                img = executor.screenshot()
                run_logger.save_screenshot(img, label="fatal_error")
            except Exception:
                pass
    finally:
        if reserved and farm_client:
            click.echo("Releasing device...")
            farm_client.release_device(reserved)

        report_path = run_logger.finalize(success=success)
        click.echo(f"\nReport saved to: {report_path}")
        if success:
            click.echo("SUCCESS")
        else:
            sys.exit(1)


def _run_scenario(
    executor: "AppiumExecutor",
    run_logger: "RunLogger",
    settings,
    step: Optional[str],
    dry_run: bool,
    device_id: str,
) -> None:
    """
    Execute the automation scenario steps in order.

    Each step is a separate module imported on demand so that individual
    steps can be run and tested in isolation via --step flag.
    """
    # Import steps lazily to avoid circular imports and keep startup fast
    from .scenarios.steps import (
        google_account,
        install_mlbb,
        mlbb_onboarding,
        payment,
    )

    steps_registry = {
        "google_account": lambda: google_account.run(
            executor=executor,
            run_logger=run_logger,
            email=settings.google_email,
            password=settings.google_password,
            device_id=device_id,
        ),
        "install_mlbb": lambda: install_mlbb.run(
            executor=executor,
            run_logger=run_logger,
            device_id=device_id,
        ),
        "mlbb_onboarding": lambda: mlbb_onboarding.run(
            executor=executor,
            run_logger=run_logger,
            device_id=device_id,
        ),
        "payment": lambda: payment.run(
            executor=executor,
            run_logger=run_logger,
            device_id=device_id,
            dry_run=dry_run,
        ),
    }

    if step:
        # Run only the specified step
        if step not in steps_registry:
            raise ValueError(f"Unknown step: {step!r}. Valid steps: {list(steps_registry)}")
        click.echo(f"Running single step: {step}")
        steps_registry[step]()
    else:
        # Run all steps in order
        for step_name, step_fn in steps_registry.items():
            click.echo(f"  → {step_name}")
            step_fn()


if __name__ == "__main__":
    cli()
