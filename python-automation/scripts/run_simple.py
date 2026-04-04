#!/usr/bin/env python3
"""
Simplified runner: Play Store → MLBB launch → Onboarding → Payment.

Assumes:
  - Google account is already added on the device
  - MLBB is already installed (or will be installed by Play Store)
  - Device is already reserved / ADB is accessible

Usage:
    # With default config (reads config.yaml)
    python scripts/run_simple.py --adb-port 9049

    # Full explicit example
    python scripts/run_simple.py \\
        --adb-host adb.mobfarm.selectel.ru \\
        --adb-port 9049 \\
        --device-id samsung-a13-001 \\
        --appium-url http://localhost:4723 \\
        --dry-run

    # Skip payment (stop after reaching Google Pay sheet)
    python scripts/run_simple.py --adb-port 9049 --dry-run

Options:
    --adb-host      ADB TCP host (default: adb.mobfarm.selectel.ru)
    --adb-port      ADB TCP port — REQUIRED
    --device-id     Human-readable device label for logs (default: selectel-device)
    --appium-url    Appium server URL (default: http://localhost:4723)
    --dry-run       Navigate to Google Pay sheet but skip final payment tap
    --payment-pin   Device unlock PIN for Google Pay auth (optional)
    --log-dir       Directory for run artifacts (default: ./run_artifacts)
    --no-onboarding Skip the onboarding step (use if already on main menu)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Ensure the python-automation package is importable when running from the
# scripts/ directory or from the project root.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from mlbb_automation.device_farm.base import DeviceInfo, ReservedDevice
from mlbb_automation.actions.executor import AppiumExecutor
from mlbb_automation.logging.logger import RunLogger, get_logger
from mlbb_automation.scenarios.steps import install_mlbb, mlbb_onboarding, payment

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Simplified MLBB automation: Play Store → launch → payment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--adb-host",
        default="adb.mobfarm.selectel.ru",
        help="ADB TCP host (default: adb.mobfarm.selectel.ru)",
    )
    p.add_argument(
        "--adb-port",
        type=int,
        required=True,
        help="ADB TCP port from Selectel reservation (e.g. 9049)",
    )
    p.add_argument(
        "--device-id",
        default="selectel-device",
        help="Human-readable device label used in logs (default: selectel-device)",
    )
    p.add_argument(
        "--appium-url",
        default="http://localhost:4723",
        help="Appium server URL (default: http://localhost:4723)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Navigate to Google Pay sheet but skip final payment confirmation",
    )
    p.add_argument(
        "--payment-pin",
        default=None,
        help="Device unlock PIN for Google Pay authentication (optional)",
    )
    p.add_argument(
        "--log-dir",
        default="./run_artifacts",
        help="Directory for run artifacts / screenshots (default: ./run_artifacts)",
    )
    p.add_argument(
        "--no-onboarding",
        action="store_true",
        help="Skip onboarding step — use when device is already on the MLBB main menu",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Pre-session cleanup
# ---------------------------------------------------------------------------

def _pre_session_cleanup(adb_host: str, adb_port: int) -> None:
    """
    Clean up stale state before starting the Appium session.

    1. Remove all ADB port forwards (leftover from a previous session)
    2. Connect ADB to the device (so that subsequent `adb shell` calls work)
    3. Force-stop UiAutomator2 server and GMS processes that can block Appium
    """
    print(f"\n[cleanup] Removing stale ADB port forwards...")
    try:
        subprocess.run(["adb", "forward", "--remove-all"], timeout=10, check=False)
    except Exception as exc:
        print(f"[cleanup] Warning: adb forward --remove-all failed: {exc}")

    print(f"[cleanup] Connecting ADB to {adb_host}:{adb_port}...")
    try:
        result = subprocess.run(
            ["adb", "connect", f"{adb_host}:{adb_port}"],
            capture_output=True, text=True, timeout=30,
        )
        print(f"[cleanup] {result.stdout.strip()}")
    except Exception as exc:
        print(f"[cleanup] Warning: adb connect failed: {exc}")
        print("[cleanup] Proceeding anyway — AppiumExecutor will retry ADB connect.")

    # Give ADB a moment to settle after TCP connect
    time.sleep(2)

    print("[cleanup] Force-stopping UiAutomator2 server and GMS...")
    _pkgs_to_kill = [
        "io.appium.uiautomator2.server",
        "io.appium.uiautomator2.server.test",
        "com.google.android.gms",
    ]
    for pkg in _pkgs_to_kill:
        try:
            subprocess.run(
                ["adb", "-s", f"{adb_host}:{adb_port}", "shell", "am", "force-stop", pkg],
                timeout=10, check=False, capture_output=True,
            )
        except Exception:
            pass

    print("[cleanup] Pre-session cleanup complete.\n")


# ---------------------------------------------------------------------------
# Build ReservedDevice
# ---------------------------------------------------------------------------

def _build_reserved_device(
    adb_host: str,
    adb_port: int,
    device_id: str,
    appium_url: str,
) -> ReservedDevice:
    """Construct a ReservedDevice from explicit connection parameters."""
    device_info = DeviceInfo(
        id=device_id,
        name="Samsung Galaxy A13",
        platform="Android",
        platform_version="14",
        model="Samsung Galaxy A13",
        status="reserved",
    )
    capabilities = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:noReset": True,
        "appium:newCommandTimeout": 300,
        "appium:adbExecTimeout": 60000,
        "appium:uiautomator2ServerInstallTimeout": 120000,
        "appium:uiautomator2ServerLaunchTimeout": 120000,
        "appium:settings[waitForSelectorTimeout]": 5000,
        "appium:skipServerInstallation": False,
    }
    return ReservedDevice(
        device_info=device_info,
        appium_url=appium_url,
        capabilities=capabilities,
        adb_host=adb_host,
        adb_port=adb_port,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()

    print("=" * 60)
    print("MLBB Simplified Automation: Play Store → Launch → Payment")
    print("=" * 60)
    print(f"  ADB:      {args.adb_host}:{args.adb_port}")
    print(f"  Appium:   {args.appium_url}")
    print(f"  Device:   {args.device_id}")
    print(f"  Dry run:  {args.dry_run}")
    print(f"  Log dir:  {args.log_dir}")
    print()

    # 1. Pre-session cleanup
    _pre_session_cleanup(args.adb_host, args.adb_port)

    # 2. Build objects
    reserved = _build_reserved_device(
        adb_host=args.adb_host,
        adb_port=args.adb_port,
        device_id=args.device_id,
        appium_url=args.appium_url,
    )

    run_logger = RunLogger(
        log_dir=Path(args.log_dir),
        device_id=args.device_id,
    )

    # 3. Run the scenario
    print("[run] Starting Appium session...")
    with AppiumExecutor(
        reserved=reserved,
        retry_count=3,
        retry_delay=2.0,
        action_timeout=30,
        device_id=args.device_id,
        run_logger=run_logger,
    ) as executor:

        # Step 1: Open Play Store → tap Играть → MLBB loads
        print("\n[step 1/3] Play Store → Играть → MLBB loading...")
        install_mlbb.run(
            executor=executor,
            run_logger=run_logger,
            device_id=args.device_id,
            open_via_play_store=True,
        )
        print("[step 1/3] Done — MLBB is loading ✓")

        # Step 2: Navigate through onboarding to main menu
        if not args.no_onboarding:
            print("\n[step 2/3] Onboarding → Main menu...")
            mlbb_onboarding.run(
                executor=executor,
                run_logger=run_logger,
                device_id=args.device_id,
            )
            print("[step 2/3] Done — main menu reached ✓")
        else:
            print("\n[step 2/3] Onboarding skipped (--no-onboarding)")

        # Step 3: Navigate to Shop → Crystals/Diamonds → select pack → pay
        print(f"\n[step 3/3] Shop → Crystals → pay (dry_run={args.dry_run})...")
        payment.run(
            executor=executor,
            run_logger=run_logger,
            device_id=args.device_id,
            dry_run=args.dry_run,
            payment_pin=args.payment_pin,
        )
        if args.dry_run:
            print("[step 3/3] Dry run complete — Google Pay sheet reached ✓")
        else:
            print("[step 3/3] Payment complete ✓")

    print("\n" + "=" * 60)
    print("Automation run complete!")
    print(f"Screenshots saved to: {run_logger.run_dir}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
