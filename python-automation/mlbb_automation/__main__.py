"""
CLI entry point for mlbb_automation.

Usage:
    python -m mlbb_automation --help
    python -m mlbb_automation check --config config.yaml
    python -m mlbb_automation run --config config.yaml
    python -m mlbb_automation run --config config.yaml --dry-run
    python -m mlbb_automation run --config config.yaml --step google_account
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
# `check` — pre-flight validation of credentials and connectivity
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--config", default="config.yaml", show_default=True, help="Path to config YAML file.")
def check(config: str) -> None:
    """
    Validate configuration and connectivity before a real run.

    Checks:
      1. Required settings are present (Selectel API key, Google credentials)
      2. Selectel API is reachable and the key is valid
      3. At least one Android device is available on the farm
      4. Template images directory is present
    """
    from .device_farm.selectel_client import create_client_from_settings
    import os

    all_ok = True

    def _row(label: str, ok: bool, detail: str = "") -> None:
        nonlocal all_ok
        icon = click.style("✓", fg="green") if ok else click.style("✗", fg="red")
        line = f"  {icon}  {label}"
        if detail:
            line += f"  ({detail})"
        click.echo(line)
        if not ok:
            all_ok = False

    click.echo("\nPre-flight check\n" + "─" * 40)

    # ── 1. Settings ──────────────────────────────────────────────────────────
    try:
        settings = load_settings(config)
        _row("Config file / env vars loaded", True, config)
    except Exception as exc:
        _row("Config file / env vars loaded", False, str(exc))
        click.echo("\n" + click.style("FAILED", fg="red") + " — fix config errors before proceeding.")
        sys.exit(1)

    _row(
        "MLBB_SELECTEL_API_KEY set",
        bool(settings.selectel_api_key),
        "hidden" if settings.selectel_api_key else "missing",
    )
    _row(
        "MLBB_GOOGLE_EMAIL set",
        bool(settings.google_email),
        settings.google_email if settings.google_email else "missing",
    )
    _row(
        "MLBB_GOOGLE_PASSWORD set",
        bool(settings.google_password),
        "hidden" if settings.google_password else "missing",
    )
    _row(
        "MLBB_PAYMENT_PIN set (optional)",
        True,
        "set" if settings.payment_pin else "not set — PIN/biometric prompts will be cancelled",
    )

    # ── 2. Selectel API connectivity ─────────────────────────────────────────
    click.echo()
    try:
        client = create_client_from_settings(settings)
        devices = client.list_devices()
        _row("Selectel API reachable", True, settings.selectel_api_url)
        _row(
            "Available devices on farm",
            len(devices) > 0,
            f"{len(devices)} device(s) found" if devices else "no devices available",
        )
        if devices:
            for d in devices[:3]:
                click.echo(f"       [{d.id}] {d.model} — Android {d.platform_version}")
            if len(devices) > 3:
                click.echo(f"       … and {len(devices) - 3} more")
    except Exception as exc:
        _row("Selectel API reachable", False, str(exc))
        _row("Available devices on farm", False, "skipped — API unreachable")

    # ── 3. Template images ───────────────────────────────────────────────────
    click.echo()
    templates_dir = os.path.join(
        os.path.dirname(__file__), "templates"
    )
    has_templates = os.path.isdir(templates_dir)
    if has_templates:
        pngs = [f for f in os.listdir(templates_dir) if f.endswith(".png")]
        _row("Templates directory present", True, f"{len(pngs)} PNG file(s)")
        # Warn about placeholder images (64×32)
        placeholders = []
        try:
            from PIL import Image
            for fname in pngs:
                path = os.path.join(templates_dir, fname)
                img = Image.open(path)
                if img.size == (64, 32):
                    placeholders.append(fname)
        except ImportError:
            pass
        if placeholders:
            click.echo(
                click.style(
                    f"\n  ⚠  {len(placeholders)} template(s) are still placeholder images "
                    "and need real device screenshots:",
                    fg="yellow",
                )
            )
            for p in placeholders:
                click.echo(f"       • {p}")
    else:
        _row("Templates directory present", False, templates_dir)

    # ── Summary ───────────────────────────────────────────────────────────────
    click.echo("\n" + "─" * 40)
    if all_ok:
        click.echo(click.style("All checks passed — ready to run.", fg="green"))
    else:
        click.echo(click.style("Some checks failed — see above.", fg="red"))
        sys.exit(1)


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
      3. Skip onboarding and reach main menu
      4. Navigate to shop and make a real payment via Google Pay
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
    from .scenarios.watchdog import Watchdog

    farm_client = create_client_from_settings(settings)
    run_logger = RunLogger(run_id=run_id, log_dir=settings.log_dir, log_level=settings.log_level)

    reserved = None
    executor = None
    success = False
    try:
        # 1. Acquire device
        click.echo("Acquiring device from Selectel farm...")
        run_logger.log_step("acquire_device", "started")

        if device_id:
            available = farm_client.list_devices()
            if not any(d.id == device_id for d in available):
                raise RuntimeError(
                    f"Device '{device_id}' not found or not available. "
                    f"Available IDs: {[d.id for d in available]}"
                )
            reserved = farm_client.acquire_device_by_id(device_id)
        else:
            reserved = farm_client.acquire_device(
                platform_version=settings.device_filter.platform_version,
                model=settings.device_filter.device_model,
            )
        run_logger.log_step(
            "acquire_device",
            "completed",
            device_id=reserved.device_info.id,
            model=reserved.device_info.model,
        )
        click.echo(f"Reserved device: {reserved.device_info.model} (id={reserved.device_info.id})")

        # 2. Start Appium session
        executor = AppiumExecutor(
            reserved,
            retry_count=settings.retry_count,
            retry_delay=settings.retry_delay_seconds,
            action_timeout=settings.action_timeout_seconds,
            device_id=reserved.device_info.id,
            run_logger=run_logger,
        )
        with executor:
            # RecoveryManager: freeze detection + app relaunch
            recovery = RecoveryManager(
                executor=executor,
                app_package="com.mobile.legends",
            )
            recovery.start_watchdog()

            # Watchdog: background thread that auto-dismisses Android popups
            # and MLBB dialogs (permission requests, update prompts, ad banners)
            with Watchdog(executor, run_logger=run_logger):
                try:
                    _run_scenario(
                        executor=executor,
                        run_logger=run_logger,
                        settings=settings,
                        step=step,
                        dry_run=dry_run,
                        device_id=reserved.device_info.id,
                        recovery=recovery,
                    )
                    success = True
                finally:
                    recovery.stop_watchdog()

    except Exception as exc:
        logger.error("Run failed", error=str(exc), exc_info=True)
        click.echo(f"\nRUN FAILED: {exc}", err=True)
        if settings.save_screenshots_on_error and executor is not None:
            try:
                img = executor.screenshot()
                run_logger.save_screenshot(img, label="fatal_error")
            except Exception as screenshot_exc:
                logger.warning("Failed to save error screenshot %s", str(screenshot_exc))
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
    recovery: "RecoveryManager",
) -> None:
    """
    Build the ScenarioRunner and execute the scenario steps.

    If --step is provided, only that step is executed (for single-step debugging).
    Otherwise all four steps run in order.
    """
    from .scenarios.steps import (
        google_account,
        install_mlbb,
        mlbb_onboarding,
        payment,
    )
    from .scenarios.engine import ScenarioRunner, Step

    # Build step closures — each binds its own arguments
    all_steps = [
        Step(
            name="google_account",
            fn=lambda: google_account.run(
                executor=executor,
                run_logger=run_logger,
                email=settings.google_email,
                password=settings.google_password,
                device_id=device_id,
            ),
            max_retries=settings.retry_count,
        ),
        Step(
            name="install_mlbb",
            fn=lambda: install_mlbb.run(
                executor=executor,
                run_logger=run_logger,
                device_id=device_id,
            ),
            max_retries=settings.retry_count,
        ),
        Step(
            name="mlbb_onboarding",
            fn=lambda: mlbb_onboarding.run(
                executor=executor,
                run_logger=run_logger,
                device_id=device_id,
            ),
            max_retries=settings.retry_count,
        ),
        Step(
            name="payment",
            fn=lambda: payment.run(
                executor=executor,
                run_logger=run_logger,
                device_id=device_id,
                dry_run=dry_run,
                payment_pin=settings.payment_pin,
            ),
            # Payment step uses the same retry/recovery model as other steps.
            # Retries are safe for UI-navigation failures (Shop navigation,
            # Google Pay sheet not appearing) because no money is charged until
            # the Pay button is confirmed.  PaymentError (charge declined after
            # confirmation) is raised directly from payment.run() and will be
            # caught by the runner; ScenarioRunner will attempt recovery, then
            # give up after max_retries — which is the correct behavior
            # (operator reviews the report and decides whether to retry manually).
            max_retries=settings.retry_count,
        ),
    ]

    runner = ScenarioRunner(executor=executor, run_logger=run_logger, recovery=recovery)

    if step:
        # Single-step mode — find the requested step
        matching = [s for s in all_steps if s.name == step]
        if not matching:
            valid = [s.name for s in all_steps]
            raise ValueError(f"Unknown step: {step!r}. Valid steps: {valid}")
        click.echo(f"Running single step: {step}")
        runner.add_step(matching[0])
    else:
        # Full run — add all steps in order
        click.echo("Running all scenario steps:")
        for s in all_steps:
            click.echo(f"  → {s.name}")
            runner.add_step(s)

    results = runner.run()

    # Print results summary
    click.echo("\nStep results:")
    for result in results:
        icon = "✓" if result.status == "ok" else ("–" if result.status == "skipped" else "✗")
        click.echo(f"  {icon} {result.name}: {result.status} (attempts: {result.attempts})")
        if result.error:
            click.echo(f"       error: {result.error}")


if __name__ == "__main__":
    cli()
