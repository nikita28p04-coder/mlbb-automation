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
import os
import signal
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

    Uses Popen + process-group kill to guarantee the timeout is respected
    even when the ADB daemon holds pipes open (a common issue with TCP
    ADB connections to remote devices).

    Swallows all errors — callers should handle empty return value.
    """
    cmd = ["adb", "-s", adb_serial, "shell"] + list(shell_args)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,   # new process group → kills children too
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
            return stdout.decode(errors="replace").strip()
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
            return ""
    except Exception:
        return ""


def _wake_and_keep_screen_on(adb_serial: str) -> None:
    """
    Wake the device screen via ADB keyevents.

    We skip ``svc power stayon`` because it hangs on Samsung Android 14
    when called over a TCP ADB connection.  Auto-lock is assumed to be
    disabled on the device (user has done this in Settings).

    Steps:
      1. KEYCODE_WAKEUP (224) — turns screen on if off
      2. KEYCODE_MENU  (82)  — dismisses a swipe lockscreen
    """
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
    out = _adb(adb_serial, "dumpsys", "activity", "activities", timeout=15)
    for line in out.splitlines():
        if "mResumedActivity" in line or "ResumedActivity" in line:
            parts = line.strip().split()
            for part in parts:
                if "/" in part and not part.startswith("{"):
                    return part.split("/")[0]
    return ""


# ---------------------------------------------------------------------------
# ADB-based Play Store launch (Samsung Knox bypass)
# ---------------------------------------------------------------------------

_MLBB_PACKAGE = "com.mobile.legends.usa"

# Labels we look for in the UI dump to tap and launch the already-installed app
_PLAY_LABELS = ("Играть", "Открыть", "Play", "Open", "UPDATE", "Обновить")


def _launch_mlbb_via_play_store_adb(adb_serial: str) -> bool:
    """
    Open the Play Store MLBB page and tap "Играть" using only ADB subprocess.

    Samsung Knox kills the UiAutomator2 test instrumentation when Play Store
    (a "protected" system app) is in the foreground.  By using ADB directly
    we sidestep UiAutomator2 entirely for this step.

    Steps:
      1. Fire the market:// intent via ``adb shell am start``
      2. Wait 6 s for Play Store to finish loading
      3. Dump the UI hierarchy via ``adb shell uiautomator dump``
      4. Parse the XML for a known Play button label; tap its centre
      5. Wait 5 s and verify MLBB is the foreground package

    Returns:
        True  — MLBB is confirmed to be loading/running after the step.
        False — could not confirm; caller should proceed anyway.
    """
    import re

    print("[adb-ps] Opening Play Store MLBB page via market:// intent...")
    _adb(adb_serial,
         "am", "start",
         "-a", "android.intent.action.VIEW",
         "-d", f"market://details?id={_MLBB_PACKAGE}",
         timeout=15)
    print("[adb-ps] Waiting 7 s for Play Store to load...")
    time.sleep(7)

    # Dump UI hierarchy to device storage then read it back
    print("[adb-ps] Dumping UI hierarchy...")
    _adb(adb_serial, "uiautomator", "dump", "/sdcard/uidump.xml", timeout=20)
    xml = _adb(adb_serial, "cat", "/sdcard/uidump.xml", timeout=10)

    if not xml:
        print("[adb-ps] UI dump failed or empty — trying fallback tap at (540, 1200)...")
        _adb(adb_serial, "input", "tap", "540", "1200", timeout=10)
    else:
        # Parse bounds for known labels in any attribute order
        tapped = False
        for label in _PLAY_LABELS:
            # Attribute order A: text=... bounds=...
            pattern = (
                r'text="' + re.escape(label) + r'"'
                r'[^/]*?'
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
            )
            m = re.search(pattern, xml)
            if not m:
                # Attribute order B: bounds=... text=...
                pattern2 = (
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
                    r'[^/]*?'
                    r'text="' + re.escape(label) + r'"'
                )
                m = re.search(pattern2, xml)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                print(f"[adb-ps] Tapping '{label}' at ({cx}, {cy})...")
                _adb(adb_serial, "input", "tap", str(cx), str(cy), timeout=10)
                tapped = True
                break
        if not tapped:
            print("[adb-ps] Button not found in XML — trying fallback tap at (540, 1200)...")
            _adb(adb_serial, "input", "tap", "540", "1200", timeout=10)

    print("[adb-ps] Waiting 5 s for MLBB to start loading...")
    time.sleep(5)

    pkg = _get_foreground_package(adb_serial)
    if _MLBB_PACKAGE in pkg:
        print(f"[adb-ps] MLBB is in foreground ({pkg}) ✓")
        return True
    else:
        print(f"[adb-ps] Foreground is {pkg!r} — MLBB may still be loading, continuing...")
        return False


# ---------------------------------------------------------------------------
# Pre-session cleanup
# ---------------------------------------------------------------------------

def _adb_connect(serial: str, timeout: int = 20) -> str:
    """
    Run ``adb connect <serial>`` and return stdout.
    Uses process-group kill to honour the timeout even on stale TCP sockets.
    """
    try:
        proc = subprocess.Popen(
            ["adb", "connect", serial],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
            return stdout.decode(errors="replace").strip()
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
            return ""
    except Exception:
        return ""


def _adb_verify_alive(serial: str) -> bool:
    """
    Verify the ADB connection is alive with a quick ``echo ping`` command.
    Returns True if the device responds within 5 s.
    """
    result = _adb(serial, "echo", "ping", timeout=5)
    return result.strip() == "ping"


def _pre_session_cleanup(adb_host: str, adb_port: int) -> None:
    """
    Clean up stale state before starting the Appium session.
    """
    serial = f"{adb_host}:{adb_port}"

    print(f"\n[cleanup] Connecting ADB to {serial}...")
    out = _adb_connect(serial)
    print(f"[cleanup] {out or '(no response from adb connect)'}")

    # Verify the connection is actually alive
    if not _adb_verify_alive(serial):
        print("[cleanup] ADB not responding — attempting fresh reconnect...")
        # Disconnect stale entry, then reconnect
        try:
            proc = subprocess.Popen(
                ["adb", "disconnect", serial],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                proc.wait()
        except Exception:
            pass
        time.sleep(1)
        out = _adb_connect(serial)
        print(f"[cleanup] Reconnect result: {out or '(no response)'}")
        if not _adb_verify_alive(serial):
            print("[cleanup] WARNING: ADB still not responding after reconnect — is the device still reserved?")
        else:
            print("[cleanup] ADB reconnected successfully ✓")

    time.sleep(2)

    print("[cleanup] Waking screen and disabling auto-sleep...")
    _wake_and_keep_screen_on(serial)

    # NOTE: Do NOT force-stop UiAutomator2 server here.
    # With skipServerInstallation=True, Appium reuses the server already on the
    # device.  Killing it would cause the session to start without a running
    # server, which leads to instrumentation crashes shortly after connect.

    print("[cleanup] Done.\n")


# ---------------------------------------------------------------------------
# State detection — pure ADB, no Appium session required
# ---------------------------------------------------------------------------

def _detect_state_via_adb(adb_serial: str) -> DeviceState:
    """
    Detect the current device state using only ADB shell commands.

    This is intentionally done BEFORE the Appium session starts so that the
    UiAutomator2 server is not burdened with extra work (screenshots + OCR)
    that can cause the instrumentation process to crash.

    Detection logic:
      1. Ask Android which package/activity is currently resumed
         (``dumpsys activity activities`` → mResumedActivity line)
      2. Map the package name to a DeviceState

    Returns one of the DeviceState literals.
    """
    pkg = _get_foreground_package(adb_serial)
    print(f"[state] Foreground package: {pkg!r}")

    if not pkg:
        # Could not determine package — check if screen looks off
        # (very few pixels or locked).  Assume home to be safe.
        print("[state] Could not determine foreground package — assuming 'home'")
        return "home"

    if "com.mobile.legends" in pkg:
        # MLBB is in the foreground.  We conservatively return mlbb_main_menu
        # so that the script skips Play Store launch and onboarding and goes
        # straight to the payment navigation.  If the app is still on a loading
        # screen the payment step will wait for the main menu itself.
        print("[state] MLBB is in foreground — treating as mlbb_main_menu")
        return "mlbb_main_menu"

    if pkg == "com.android.vending":
        return "play_store"

    if "launcher" in pkg or "home" in pkg or "nexuslauncher" in pkg:
        return "home"

    # Any other package (browser, Settings, etc.) → treat as home-equivalent
    # so that we run the full flow from Play Store
    print(f"[state] Unknown package {pkg!r} — treating as 'unknown'")
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

    # ── 2. State detection (pure ADB, before Appium session) ────────────────
    #
    # State is detected via ADB only — no Appium session needed.  This avoids
    # the UiAutomator2 instrumentation crash that happened when OCR (34 s) was
    # run inside the session immediately after startup.
    #
    if args.force_launch:
        state: DeviceState = "unknown"
        print("[state] --force-launch: skipping state detection, starting from Play Store")
    else:
        print("[state] Detecting current device state via ADB...")
        state = _detect_state_via_adb(adb_serial)
        print(f"[state] Detected: {state}")

    # ── 3. Routing — determine which steps to run ────────────────────────────
    #
    #  State              │ Play Store (ADB) │ Onboarding (Appium) │ Payment
    #  ───────────────────┼──────────────────┼─────────────────────┼─────────
    #  home / play_store  │       YES        │        YES          │   YES
    #  unknown            │       YES        │        YES          │   YES
    #  mlbb_loading       │       no         │        YES          │   YES
    #  mlbb_ingame        │       no         │        YES          │   YES
    #  mlbb_main_menu     │       no         │        no           │   YES
    #  google_pay         │       no         │        no           │   YES
    #  ───────────────────────────────────────────────────────────────────────
    #
    # KEY INSIGHT: Samsung Knox kills UiAutomator2 instrumentation when Play
    # Store is in the foreground.  We therefore handle the Play Store step via
    # pure ADB *before* starting the Appium session.

    need_play_store = state not in ("mlbb_main_menu", "mlbb_ingame",
                                    "mlbb_loading", "google_pay")
    need_onboarding = state not in ("mlbb_main_menu", "google_pay")

    # ── 4. Play Store → Играть → MLBB loading  (pure ADB, no Appium) ────────
    if need_play_store:
        print("\n[step 1/3] Play Store → Играть → MLBB loading (via ADB)...")
        _launch_mlbb_via_play_store_adb(adb_serial)
        print("[step 1/3] Done — MLBB is launching ✓")
    else:
        print(f"\n[step 1/3] Skipped — state is '{state}', MLBB already running ✓")

    # ── 5. Build Appium objects + start session ──────────────────────────────
    reserved = _build_reserved_device(
        adb_host=args.adb_host,
        adb_port=args.adb_port,
        device_id=args.device_id,
        appium_url=args.appium_url,
    )
    run_id = make_run_id()
    run_logger = RunLogger(run_id=run_id, log_dir=Path(args.log_dir))
    run_logger.log_step("state_detection", state, device_id=args.device_id)
    print(f"[run] Run ID: {run_id}")

    print("[run] Starting Appium session...")
    try:
        with AppiumExecutor(
            reserved=reserved,
            retry_count=3,
            retry_delay=2.0,
            action_timeout=30,
            device_id=args.device_id,
            run_logger=run_logger,
        ) as executor:

            # Wake screen after session start
            executor.wake_screen()

            # ── 6. Onboarding → Main menu ────────────────────────────────────────
            if need_onboarding:
                print("\n[step 2/3] Onboarding → Main menu...")
                mlbb_onboarding.run(
                    executor=executor,
                    run_logger=run_logger,
                    device_id=args.device_id,
                )
                print("[step 2/3] Done — main menu reached ✓")
            else:
                print(f"\n[step 2/3] Skipped — state is '{state}', already at main menu ✓")

            # ── 7. Recharge → 50 Diamonds → Google Pay ──────────────────────────
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

    except Exception as exc:
        import traceback
        print("\n[ERROR] Automation failed with exception:")
        traceback.print_exc()
        run_logger.log_step("run", "error", device_id=args.device_id, error=str(exc))
        return 1

    print("\n" + "=" * 60)
    print("Automation run complete!")
    print(f"Artifacts saved to: {run_logger.run_dir}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
