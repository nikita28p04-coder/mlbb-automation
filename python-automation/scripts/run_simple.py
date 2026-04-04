#!/usr/bin/env python3
"""
Simplified runner: Play Store → MLBB launch → Onboarding → Payment.

Assumes:
  - Google account is already added on the device
  - MLBB is already installed (or will be installed by Play Store)
  - Device is already reserved / ADB is accessible

Smart state detection:
  The script detects the current device state at startup and skips
  steps already completed:
    - Already on MLBB main menu  → go straight to payment
    - MLBB loading / in-game     → skip Play Store launch, do onboarding
    - Play Store / home / locked → full flow (launch → onboard → pay)

Usage:
    python scripts/run_simple.py \\
        --adb-host adb.mobfarm.selectel.ru \\
        --adb-port 9049 \\
        --device-id samsung-a13 \\
        --dry-run

Options:
    --adb-host      ADB TCP host (default: adb.mobfarm.selectel.ru)
    --adb-port      ADB TCP port — REQUIRED
    --device-id     Human-readable device label for logs (default: selectel-device)
    --appium-url    Appium server URL (default: http://localhost:4723)
    --dry-run       Navigate to Google Pay sheet but skip final payment tap
    --payment-pin   Device unlock PIN for Google Pay auth (optional)
    --log-dir       Directory for run artifacts (default: ./run_artifacts)
    --force-launch  Always start from Play Store even if already on main menu
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from mlbb_automation.device_farm.base import DeviceInfo, ReservedDevice
from mlbb_automation.actions.executor import AppiumExecutor
from mlbb_automation.logging.logger import RunLogger, get_logger, make_run_id
from mlbb_automation.scenarios.steps import install_mlbb, mlbb_onboarding, payment

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Device state type
# ---------------------------------------------------------------------------

DeviceState = Literal[
    "locked",        # screen off or lock screen (no content visible)
    "home",          # Android home/launcher
    "play_store",    # Google Play Store is in foreground
    "mlbb_loading",  # MLBB loading / patch download screen
    "mlbb_main_menu",# MLBB main menu — ready for payment navigation
    "mlbb_ingame",   # MLBB in another sub-screen (shop, events, etc.)
    "google_pay",    # Google Pay billing sheet is open
    "unknown",       # Unrecognised state
]

# OCR signal sets (lowercase) used in state detection
_MAIN_MENU_SIGNALS = (
    "classic", "ranked", "brawl", "battle",
    "подготовка", "герои", "сумка", "обычный",
)
_LOADING_SIGNALS = (
    "loading", "moonton", "downloading", "updating", "patch", "загрузка",
)
_GOOGLE_PAY_SIGNALS = (
    "google play", "купить",
)
_PLAY_STORE_SIGNALS = (
    "играть", "установить", "обновить",
)

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
    )
    p.add_argument(
        "--appium-url",
        default="http://localhost:4723",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Navigate to Google Pay sheet but skip final payment tap",
    )
    p.add_argument(
        "--payment-pin",
        default=None,
    )
    p.add_argument(
        "--log-dir",
        default="./run_artifacts",
    )
    p.add_argument(
        "--force-launch",
        action="store_true",
        help="Always start from Play Store, ignore detected state",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# ADB helpers (pre-session, no Appium required)
# ---------------------------------------------------------------------------

def _adb(adb_serial: str, *shell_args: str, timeout: int = 10) -> str:
    """
    Run ``adb -s <serial> shell <args>`` and return stdout (stripped).
    Swallows all errors — callers should handle empty return value.
    """
    try:
        result = subprocess.run(
            ["adb", "-s", adb_serial, "shell"] + list(shell_args),
            capture_output=True, text=True, timeout=timeout,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _wake_and_keep_screen_on(adb_serial: str) -> None:
    """
    Wake the device screen and prevent it from sleeping while ADB is
    connected.

    Steps:
      1. ``svc power stayon true``  — keeps screen on while connected
      2. KEYCODE_WAKEUP (224)       — turns screen on if off
      3. KEYCODE_MENU  (82)         — dismisses swipe lockscreen
    """
    print("[screen] Keeping screen on (svc power stayon true)...")
    _adb(adb_serial, "svc", "power", "stayon", "true")

    print("[screen] Sending WAKEUP + MENU key events...")
    _adb(adb_serial, "input", "keyevent", "224")  # KEYCODE_WAKEUP
    time.sleep(0.5)
    _adb(adb_serial, "input", "keyevent", "82")   # KEYCODE_MENU (unlock swipe)
    time.sleep(0.5)
    print("[screen] Screen should now be on and unlocked.")


def _get_foreground_package(adb_serial: str) -> str:
    """
    Return the package name of the currently foregrounded app.

    Parses ``dumpsys activity activities`` for the resumed activity line.
    Returns empty string on failure.
    """
    try:
        result = subprocess.run(
            ["adb", "-s", adb_serial, "shell",
             "dumpsys", "activity", "activities"],
            capture_output=True, text=True, timeout=15,
        )
        for line in result.stdout.splitlines():
            if "mResumedActivity" in line or "ResumedActivity" in line:
                # Line format: ... u0 com.package.name/.Activity t42}
                parts = line.strip().split()
                for part in parts:
                    if "/" in part and not part.startswith("{"):
                        return part.split("/")[0]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Pre-session cleanup
# ---------------------------------------------------------------------------

def _pre_session_cleanup(adb_host: str, adb_port: int) -> None:
    """
    Clean up stale state before starting the Appium session.
    """
    serial = f"{adb_host}:{adb_port}"

    print(f"\n[cleanup] Connecting ADB to {serial}...")
    try:
        result = subprocess.run(
            ["adb", "connect", serial],
            capture_output=True, text=True, timeout=30,
        )
        print(f"[cleanup] {result.stdout.strip()}")
    except Exception as exc:
        print(f"[cleanup] Warning: adb connect failed: {exc}")

    time.sleep(2)

    print("[cleanup] Waking screen and disabling auto-sleep...")
    _wake_and_keep_screen_on(serial)

    print("[cleanup] Force-stopping stale UiAutomator2 server...")
    for pkg in ["io.appium.uiautomator2.server", "io.appium.uiautomator2.server.test"]:
        try:
            subprocess.run(
                ["adb", "-s", serial, "shell", "am", "force-stop", pkg],
                timeout=10, check=False, capture_output=True,
            )
        except Exception:
            pass

    print("[cleanup] Done.\n")


# ---------------------------------------------------------------------------
# State detection (requires active Appium session)
# ---------------------------------------------------------------------------

def _detect_device_state(
    executor: AppiumExecutor,
    adb_serial: str,
    run_logger: RunLogger,
) -> DeviceState:
    """
    Detect current device state by combining:
      1. Foreground package name (via ADB)
      2. OCR text scan of a screenshot

    Returns one of the DeviceState literals.
    """
    from mlbb_automation.cv.ocr import OcrEngine

    # Wake screen before detection (belt+suspenders)
    executor.wake_screen()
    time.sleep(0.5)

    pkg = _get_foreground_package(adb_serial)
    print(f"[state] Foreground package: {pkg!r}")

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="state_detection")

    ocr = OcrEngine()
    results = ocr.read_region(img)
    texts = " ".join(r.text.lower() for r in results)

    print(f"[state] OCR texts (first 200): {texts[:200]!r}")

    # Google Pay billing sheet (appears over any app)
    if all(s in texts for s in ("купить",)) and "google" in texts:
        return "google_pay"

    # MLBB is in the foreground
    if "com.mobile.legends" in pkg:
        if any(s in texts for s in _MAIN_MENU_SIGNALS):
            return "mlbb_main_menu"
        if any(s in texts for s in _LOADING_SIGNALS):
            return "mlbb_loading"
        # MLBB is running but sub-state unclear → treat as loading/navigating
        return "mlbb_ingame"

    # Google Play Store
    if pkg == "com.android.vending":
        return "play_store"

    # Home / Launcher
    if "launcher" in pkg or "home" in pkg:
        return "home"

    # Very few OCR results usually means blank/lock screen
    if len(results) < 3:
        return "locked"

    # Play Store signals visible even if package detection failed
    if any(s in texts for s in _PLAY_STORE_SIGNALS) and "google" in texts:
        return "play_store"

    # MLBB main menu signals visible
    if any(s in texts for s in _MAIN_MENU_SIGNALS):
        return "mlbb_main_menu"

    return "unknown"


# ---------------------------------------------------------------------------
# Build ReservedDevice
# ---------------------------------------------------------------------------

def _build_reserved_device(
    adb_host: str,
    adb_port: int,
    device_id: str,
    appium_url: str,
) -> ReservedDevice:
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
        "appium:skipServerInstallation": True,
        "appium:ignoreHiddenApiPolicyError": True,
        "appium:skipDeviceInitialization": True,
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
    adb_serial = f"{args.adb_host}:{args.adb_port}"

    print("=" * 60)
    print("MLBB Simplified Automation: Play Store → Launch → Payment")
    print("=" * 60)
    print(f"  ADB:          {adb_serial}")
    print(f"  Appium:       {args.appium_url}")
    print(f"  Device:       {args.device_id}")
    print(f"  Dry run:      {args.dry_run}")
    print(f"  Force launch: {args.force_launch}")
    print(f"  Log dir:      {args.log_dir}")
    print()

    # ── 1. Pre-session cleanup + screen wake ────────────────────────────────
    _pre_session_cleanup(args.adb_host, args.adb_port)

    # ── 2. Build driver objects ─────────────────────────────────────────────
    reserved = _build_reserved_device(
        adb_host=args.adb_host,
        adb_port=args.adb_port,
        device_id=args.device_id,
        appium_url=args.appium_url,
    )
    run_id = make_run_id()
    run_logger = RunLogger(run_id=run_id, log_dir=Path(args.log_dir))
    print(f"[run] Run ID: {run_id}")

    # ── 3. Start Appium session ─────────────────────────────────────────────
    print("[run] Starting Appium session...")
    with AppiumExecutor(
        reserved=reserved,
        retry_count=3,
        retry_delay=2.0,
        action_timeout=30,
        device_id=args.device_id,
        run_logger=run_logger,
    ) as executor:

        # Wake screen again now that Appium is connected (belt+suspenders)
        executor.wake_screen()

        # ── 4. State detection ───────────────────────────────────────────────
        if args.force_launch:
            state: DeviceState = "unknown"
            print("[state] --force-launch: skipping state detection, starting from Play Store")
        else:
            print("[state] Detecting current device state...")
            state = _detect_device_state(executor, adb_serial, run_logger)
            print(f"[state] Detected: {state}")
            run_logger.log_step("state_detection", state, device_id=args.device_id)

        # ── 5. Smart routing ─────────────────────────────────────────────────
        #
        #  State                     → Steps to run
        #  ─────────────────────────────────────────────────────────────────
        #  mlbb_main_menu / google_pay  skip 1+2, run payment only
        #  mlbb_loading / mlbb_ingame   skip 1 (Play Store), run 2+3
        #  anything else                full flow: 1 + 2 + 3
        #  ─────────────────────────────────────────────────────────────────

        skip_launch   = state in ("mlbb_main_menu", "mlbb_ingame",
                                  "mlbb_loading", "google_pay")
        skip_onboard  = state in ("mlbb_main_menu", "google_pay")

        if not skip_launch:
            print("\n[step 1/3] Play Store → Играть → MLBB loading...")
            install_mlbb.run(
                executor=executor,
                run_logger=run_logger,
                device_id=args.device_id,
                open_via_play_store=True,
            )
            print("[step 1/3] Done — MLBB launched ✓")
        else:
            print(f"\n[step 1/3] Skipped — state is '{state}', MLBB already running ✓")

        if not skip_onboard:
            print("\n[step 2/3] Onboarding → Main menu...")
            mlbb_onboarding.run(
                executor=executor,
                run_logger=run_logger,
                device_id=args.device_id,
            )
            print("[step 2/3] Done — main menu reached ✓")
        else:
            print(f"\n[step 2/3] Skipped — state is '{state}', already at main menu ✓")

        print(f"\n[step 3/3] Recharge → 50 Diamonds → pay (dry_run={args.dry_run})...")
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
    print(f"Artifacts saved to: {run_logger.run_dir}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
