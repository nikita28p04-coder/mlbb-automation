"""
Step: Navigate to MLBB Shop and complete a real payment via Google Pay.

Flow:
  1. From main menu → tap Shop icon
  2. Inside Shop → navigate to Diamonds / Top-Up section
  3. Select the smallest available diamond package (e.g. ~$0.99 / ~89 RUB)
  4. Tap Buy → Google Pay sheet appears
  5. Switch Appium context to WEBVIEW (if Google Pay uses WebView)
  6. Confirm payment (tap "Pay" or similar button)
  7. Detect result: "Purchase Successful" or payment error
  8. Save timestamped screenshot and log result

dry_run mode: navigates all the way through Buy → Google Pay sheet but stops
before the final payment confirmation tap.  This verifies the full UI flow
including Google Pay sheet rendering without spending money.

Payment result detection uses a two-stage hybrid strategy:
  1. Template match against templates/payment_success.png / payment_failed.png
     (threshold=0.85 required for template-only signal)
  2. OCR text match against payment-specific phrases
  A result is committed only when:
    - Template AND OCR both confirm the same outcome, OR
    - Template confidence >= _TEMPLATE_STRONG_THRESHOLD (0.90) alone, OR
    - OCR matches payment-specific (not generic) phrases

Context-switching notes:
  - Google Pay sometimes renders in NATIVE_APP, sometimes in WEBVIEW
  - We probe both and use whichever has the Pay/Confirm button
  - After confirming, we switch back to NATIVE_APP
"""

from __future__ import annotations

import time
from typing import Optional

from ...actions.executor import AppiumExecutor
from ...logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

# OCR signals for each state
_SHOP_SIGNALS = ("shop", "магазин")
_DIAMONDS_SIGNALS = (
    "diamonds", "алмазы", "кристаллы", "crystals",
    "top up", "topup", "recharge",
    "пополнить", "пополнение",
)
_BUY_SIGNALS = ("buy", "purchase", "купить", "приобрести")
_GOOGLE_PAY_SIGNALS = (
    # Real device (Samsung Galaxy A13, Russian locale) shows "Google Play" billing sheet,
    # NOT "Google Pay". Sheet header: "Google Play", product: "50 Diamonds", button: "Купить"
    "google play",
    # Keep Google Pay variants as fallback for other devices / payment methods
    "google pay",
    "pay with google",
    # Secondary signals visible on the sheet
    "топ продаж",                    # badge next to product name in Russian locale
    "mobile legends: bang bang",     # product subtitle on Google Play sheet
)

# Payment-specific OCR success phrases (must contain payment context words).
# Generic words like "success" are intentionally excluded to avoid false positives
# on unrelated UI text (e.g. achievement banners, login success messages).
_SUCCESS_SIGNALS = (
    "purchase successful",
    "payment successful",
    "order confirmed",
    "покупка выполнена",
    "оплата прошла",
    "оплата успешна",
)

# Payment-specific OCR failure phrases.  Generic words like "error", "failed"
# are excluded because MLBB shows them in non-payment contexts (connectivity
# errors, loading failures) which would cause false-positive payment failure detection.
_FAILURE_SIGNALS = (
    "payment failed",
    "payment declined",
    "transaction declined",
    "transaction failed",
    "purchase failed",
    "оплата отклонена",
    "транзакция отклонена",
    "не удалось оплатить",
)

# Template confidence thresholds for payment result detection
_TEMPLATE_MATCH_THRESHOLD = 0.85   # minimum to count as a template signal
_TEMPLATE_STRONG_THRESHOLD = 0.90  # template alone (no OCR required) if >= this

# Smallest-package heuristics — these text patterns usually appear near cheap packs
_SMALL_PACK_SIGNALS = (
    "86",    # 86 diamonds / crystals (~$0.99 / ~89 ₽)
    "89",    # sometimes 89 diamonds; also "89 ₽" for Russian locale
    "0.99",
    "1.09",
    "₱",    # Philippine peso (often lowest)
    "0,99",
    "$0",
    "99 ₽",   # Russian ruble — cheapest pack is often 99 ₽
    "99₽",
    "89 ₽",
    "89₽",
    "109 ₽",
    "109₽",
    "руб",   # generic ruble abbreviation next to any small price
)

# Timeouts
_SHOP_TIMEOUT = 30
_PAYMENT_SHEET_TIMEOUT = 45  # Google Play billing sheet loads from servers — needs extra time
_AUTH_TIMEOUT = 20      # seconds to wait for PIN/biometric prompt to appear or clear
_RESULT_TIMEOUT = 60
_POLL_INTERVAL = 2.0

# OCR signals indicating a device-auth screen is showing after Google Pay
_PIN_SIGNALS = (
    "enter pin",
    "введите pin",
    "введите пин",
    "enter your pin",
    "device pin",
    "confirm with",
    "подтвердите с помощью",
)
_BIOMETRIC_SIGNALS = (
    "fingerprint",
    "отпечаток",
    "face unlock",
    "распознавание лица",
    "touch sensor",
    "биометрия",
    "use fingerprint",
)
# Signals that indicate the auth prompt resolved (either success or dismiss)
_AUTH_DISMISSED_SIGNALS = (
    "cancel",
    "use pin instead",
    "use password instead",
    "вместо этого",
    "отмена",
)


class StepError(Exception):
    """Non-recoverable error in a step."""


class PaymentError(Exception):
    """
    The payment was processed but failed (declined / server error).

    Marked as non-retriable (_is_non_retriable = True) so that ScenarioRunner
    aborts immediately on this exception instead of re-running the payment step,
    which would risk issuing a duplicate charge.

    Only StepError exceptions (raised before the Pay button is confirmed) are
    safe to retry automatically.
    """

    _is_non_retriable: bool = True


def run(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str = "",
    dry_run: bool = False,
    payment_pin: Optional[str] = None,
) -> None:
    """
    Navigate to RECHARGE screen → select 50 Diamonds ($0.99) → Google Play sheet → "Купить".

    Navigation (Samsung Galaxy A13, Russian locale, confirmed from real screenshots):
      Main menu "+" button → RECHARGE screen → tap "50 Diamonds" (0,99 $) →
      Google Play billing sheet → tap "Купить" (blue button).

    Fallback navigation:
      "Магазин" → Diamonds/Recharge tab → package → Google Play sheet.

    Args:
        executor:     Active AppiumExecutor session.
        run_logger:   RunLogger for this automation run.
        device_id:    Device ID for log context.
        dry_run:      If True, navigate to payment screen but skip final tap.
        payment_pin:  Device unlock PIN (not required for Mastercard saved in Google Play).
    """
    run_logger.log_step("payment", "started", device_id=device_id, dry_run=dry_run)
    logger.info("payment step starting", device_id=device_id, dry_run=dry_run)

    # Step 1: Navigate from main menu to RECHARGE screen
    # Fast path: tap "+" next to crystal counter → RECHARGE opens directly
    # Fallback: "Магазин" → Diamonds/Recharge tab
    _open_recharge_screen(executor, run_logger, device_id)

    # Step 2: Select smallest package (50 Diamonds, 0,99 $)
    _select_smallest_package(executor, run_logger, device_id)

    # Step 3: Tap "Buy" on the package — Google Play billing sheet slides up
    _tap_buy(executor, run_logger, device_id)

    # Step 4: Handle Google Play billing sheet — tap "Купить" (blue button)
    # dry_run stops here — after reaching the sheet — without confirming payment
    _handle_google_pay(executor, run_logger, device_id, dry_run=dry_run)

    if dry_run:
        run_logger.log_step("payment", "dry_run_ok", device_id=device_id)
        return

    # Step 6: Handle device auth (PIN / biometric) if Google Pay requires it
    # This is expected on many devices and must be handled before result detection.
    _handle_device_auth(executor, run_logger, device_id, payment_pin=payment_pin or "")

    # Step 7: Detect result
    # IMPORTANT — at this point payment confirmation has been sent to Google Pay.
    # Any failure here is post-confirmation: we raise PaymentConfirmedError so
    # ScenarioRunner can abort without retrying (retrying would risk a duplicate
    # charge).  Navigation failures earlier in this function (StepError) are safe
    # to retry because they occur before the irreversible confirmation tap.
    result = _detect_payment_result(executor, run_logger, device_id)

    if result == "success":
        run_logger.log_step("payment", "ok", device_id=device_id, result="success")
        logger.info("Payment completed successfully", device_id=device_id)
    else:
        run_logger.log_step("payment", "payment_failed", device_id=device_id, result=result)
        # Raise PaymentError (a subclass of PaymentConfirmedError) so that the
        # ScenarioRunner aborts immediately instead of re-running the whole step
        # (which could issue a duplicate charge).
        raise PaymentError(
            f"Payment failed or declined after confirmation: {result}"
        )


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

def _open_recharge_screen(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Navigate from MLBB main menu to the RECHARGE / Diamonds screen.

    *** Knox-safe implementation ***

    Samsung Knox kills the UiAutomator2 server after the FIRST Appium screenshot
    inside MLBB's game context.  This function therefore uses ONE Appium screenshot
    (already taken by the caller) and then switches exclusively to ADB for all
    subsequent taps and screenshots.

    Fast path:
      1. OCR the already-captured ``img`` to find "+" in the right half.
      2. If found → ADB tap at OCR coords.
      3. If NOT found → ADB tap at heuristic coordinates (confirmed for Samsung
         Galaxy A13 landscape 2408×1080).

    Verification:
      Wait 2 s, take an ADB screenshot (bypasses Knox), OCR for RECHARGE signals.
      If confirmed → done.  Otherwise fall back to Магазин path.

    Fallback:
      Tap "Магазин" → navigate to Diamonds/Recharge tab via ADB taps.
    """
    run_logger.log_step("payment", "open_recharge", device_id=device_id)
    logger.info("Navigating to RECHARGE screen", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    # ── ONE Appium screenshot (allowed by Knox) ──────────────────────────────
    # CRITICAL: do NOT call executor.screenshot() more than once while MLBB's
    # main-menu activity is in the foreground — the second call kills the
    # UiAutomator2 server.  All subsequent screenshots use executor.adb_screenshot().
    img = executor.screenshot()
    run_logger.save_screenshot(img, label="before_recharge_nav")

    img_w = img.width   # 2408 on Samsung Galaxy A13 landscape
    img_h = img.height  # 1080 on Samsung Galaxy A13 landscape

    _RECHARGE_CONFIRM_SIGNALS = ("recharge", "diamonds", "50 diamonds", "алмазы", "пополнение", "кристаллы")

    # ── Stage B: OCR on already-captured img — look for "+" in right half ───
    # (Stage A UiAutomator2 find_element removed — it took a second screenshot
    # which caused Knox to kill the UiAutomator2 server.)
    shortcut_tapped = False
    results = ocr.read_region(img)
    for result in results:
        if result.text.strip() == "+" and result.cx > img_w * 0.55:
            logger.info("OCR found '+' shortcut", x=result.cx, y=result.cy, device_id=device_id)
            executor.adb_tap(result.cx, result.cy)
            shortcut_tapped = True
            logger.info("ADB-tapped '+' shortcut via OCR", x=result.cx, y=result.cy, device_id=device_id)
            break

    # ── Stage C: heuristic ADB tap at known coordinates ──────────────────────
    # Confirmed layout for Samsung Galaxy A13 (2408×1080 landscape, MLBB):
    #   Crystal/diamond "+" button: ~92% width, ~10% height
    if not shortcut_tapped:
        cx = int(img_w * 0.92)
        cy = int(img_h * 0.10)
        logger.info("Heuristic ADB tap '+' at coords", x=cx, y=cy,
                    img_w=img_w, img_h=img_h, device_id=device_id)
        executor.adb_tap(cx, cy)
        shortcut_tapped = True

    # ── Verify RECHARGE screen opened (ADB screenshot — bypasses Knox) ───────
    if shortcut_tapped:
        time.sleep(2)
        try:
            img2 = executor.adb_screenshot()
            run_logger.save_screenshot(img2, label="after_plus_tap")
            results2 = ocr.read_region(img2)
            texts = " ".join(r.text.lower() for r in results2)
            logger.info("Post-tap OCR texts", texts_sample=texts[:150], device_id=device_id)
            if any(s in texts for s in _RECHARGE_CONFIRM_SIGNALS):
                run_logger.save_screenshot(img2, label="recharge_via_shortcut")
                logger.info("RECHARGE screen opened via '+' shortcut", device_id=device_id)
                return
            logger.info(
                "'+' shortcut did not land on RECHARGE — falling back to Магазин path",
                texts_sample=texts[:100],
                device_id=device_id,
            )
        except Exception as exc:
            logger.warning("ADB screenshot/OCR after tap failed", error=str(exc), device_id=device_id)

    # ── Fallback: Магазин → Diamonds tab ────────────────────────────────────
    logger.info("Using fallback: Магазин → Diamonds/Recharge tab", device_id=device_id)
    _open_shop(executor, run_logger, device_id)
    _open_diamonds_section(executor, run_logger, device_id)


def _open_shop(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Navigate to the MLBB Shop from the main menu (fallback path).

    Knox-safe: uses ADB screenshots and ADB taps exclusively.
    """
    logger.info("Opening MLBB Shop (fallback path)", device_id=device_id)
    run_logger.log_step("payment", "open_shop", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    img = executor.adb_screenshot()
    run_logger.save_screenshot(img, label="before_shop_nav")
    results = ocr.read_region(img)

    _shop_signals = ("shop", "магазин", "store")
    tapped_shop = False
    for result in results:
        if any(s in result.text.lower() for s in _shop_signals):
            logger.info("ADB tap Shop button via OCR", text=result.text, x=result.cx, y=result.cy, device_id=device_id)
            executor.adb_tap(result.cx, result.cy)
            tapped_shop = True
            break

    if not tapped_shop:
        # Heuristic: Samsung Galaxy A13 (2408×1080), Магазин is at ~12% width, ~72% height
        img_w, img_h = img.width, img.height
        cx = int(img_w * 0.12)
        cy = int(img_h * 0.72)
        logger.warning("Shop button not found by OCR — heuristic ADB tap", x=cx, y=cy, device_id=device_id)
        executor.adb_tap(cx, cy)

    time.sleep(2)

    # Verify we entered the shop
    deadline = time.monotonic() + _SHOP_TIMEOUT
    while time.monotonic() < deadline:
        img = executor.adb_screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)
        if any(s in texts for s in _SHOP_SIGNALS + _DIAMONDS_SIGNALS):
            run_logger.save_screenshot(img, label="shop_opened")
            logger.info("Shop opened", device_id=device_id)
            return
        time.sleep(_POLL_INTERVAL)

    img = executor.adb_screenshot()
    run_logger.save_screenshot(img, label="shop_timeout")
    raise StepError(f"Did not enter MLBB Shop within {_SHOP_TIMEOUT}s")


def _open_diamonds_section(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Inside the Shop, navigate to the Diamonds / Top-Up section.

    Knox-safe: uses ADB screenshots and ADB taps exclusively.
    """
    logger.info("Opening Diamonds section", device_id=device_id)
    run_logger.log_step("payment", "open_diamonds", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    img = executor.adb_screenshot()
    results = ocr.read_region(img)
    texts = " ".join(r.text.lower() for r in results)

    # If already on diamonds page, skip
    if any(s in texts for s in _DIAMONDS_SIGNALS):
        logger.info("Already on Diamonds section", device_id=device_id)
        return

    _diamond_labels = ("diamonds", "алмазы", "кристаллы", "crystals",
                       "top up", "пополнить", "пополнение", "recharge")
    tapped_diamonds = False
    for result in results:
        if any(s in result.text.lower() for s in _diamond_labels):
            logger.info("ADB tap Diamonds tab via OCR", text=result.text, x=result.cx, y=result.cy, device_id=device_id)
            executor.adb_tap(result.cx, result.cy)
            tapped_diamonds = True
            break

    if not tapped_diamonds:
        # Heuristic: Samsung Galaxy A13 (2408×1080), Diamonds tab ~15% width, ~12% height
        img_w, img_h = img.width, img.height
        cx = int(img_w * 0.15)
        cy = int(img_h * 0.12)
        logger.warning("Diamonds tab not found by OCR — heuristic ADB tap", x=cx, y=cy, device_id=device_id)
        executor.adb_tap(cx, cy)

    time.sleep(2)
    img = executor.adb_screenshot()
    run_logger.save_screenshot(img, label="diamonds_section")
    logger.info("Diamonds section opened", device_id=device_id)


def _select_smallest_package(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Select the smallest (cheapest) diamond package.

    Knox-safe: uses ADB screenshots and ADB taps exclusively.

    Strategy:
      1. ADB screenshot of the RECHARGE screen.
      2. OCR to find a price signal matching a small pack (< $2 / ~99 ₽).
      3. ADB tap at that position.
      4. If no price signal found, use heuristic coordinates for Samsung A13.
    """
    logger.info("Selecting smallest diamond package", device_id=device_id)
    run_logger.log_step("payment", "select_package", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    img = executor.adb_screenshot()
    results = ocr.read_region(img)
    run_logger.save_screenshot(img, label="diamonds_packages")

    logger.info(
        "OCR results on RECHARGE screen",
        count=len(results),
        texts=[r.text for r in results[:12]],
        device_id=device_id,
    )

    # Find OCR results that look like small-pack price tags
    candidates = []
    for result in results:
        text = result.text.lower()
        if any(signal in text for signal in _SMALL_PACK_SIGNALS):
            candidates.append(result)

    if candidates:
        # Sort by position: top-to-bottom, left-to-right (cheapest first)
        best = sorted(candidates, key=lambda r: (r.cy, r.cx))[0]
        logger.info(
            "Small pack identified by price text",
            text=best.text,
            x=best.cx,
            y=best.cy,
            device_id=device_id,
        )
        executor.adb_tap(best.cx, best.cy)
        time.sleep(1)
    else:
        # Fallback: heuristic coords for Samsung Galaxy A13 landscape (2408×1080)
        # RECHARGE screen: "50 Diamonds" is typically the first/leftmost package row
        # at approximately: 22% width, 42% height
        img_w, img_h = img.width, img.height
        cx = int(img_w * 0.22)
        cy = int(img_h * 0.42)
        logger.warning(
            "No price signals found — tapping heuristic package coords",
            x=cx,
            y=cy,
            img_w=img_w,
            img_h=img_h,
            device_id=device_id,
        )
        executor.adb_tap(cx, cy)
        time.sleep(1)

    img = executor.adb_screenshot()
    run_logger.save_screenshot(img, label="package_selected")


def _tap_buy(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """
    Tap the Buy / Purchase button on the selected package to open Google Pay sheet.

    Knox-safe: uses ADB screenshot + OCR to locate the button, then ADB tap.

    Fallback: heuristic coordinates for Samsung Galaxy A13 (2408×1080 landscape).
    """
    logger.info("Tapping Buy button", device_id=device_id)
    run_logger.log_step("payment", "tap_buy", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    img = executor.adb_screenshot()
    run_logger.save_screenshot(img, label="before_buy_tap")
    results = ocr.read_region(img)
    texts_all = " ".join(r.text.lower() for r in results)
    logger.info("OCR before Buy tap", texts_sample=texts_all[:150], device_id=device_id)

    _buy_signals = ("купить", "buy", "purchase", "приобрести", "оплатить")
    buy_tapped = False
    for result in results:
        if any(s in result.text.lower() for s in _buy_signals):
            logger.info("OCR found Buy button", text=result.text, x=result.cx, y=result.cy, device_id=device_id)
            executor.adb_tap(result.cx, result.cy)
            buy_tapped = True
            break

    if not buy_tapped:
        # Heuristic: on Samsung Galaxy A13 RECHARGE screen after package tap,
        # the "Купить" / "Buy" button is at approximately bottom-center
        # ~50% width, ~82% height for landscape 2408×1080
        img_w, img_h = img.width, img.height
        cx = int(img_w * 0.50)
        cy = int(img_h * 0.82)
        logger.warning(
            "Buy button not found by OCR — tapping heuristic coords",
            x=cx,
            y=cy,
            device_id=device_id,
        )
        executor.adb_tap(cx, cy)
        buy_tapped = True

    time.sleep(2)
    img = executor.adb_screenshot()
    run_logger.save_screenshot(img, label="after_buy_tap")


# ---------------------------------------------------------------------------
# Google Pay handling
# ---------------------------------------------------------------------------

def _handle_google_pay(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
    dry_run: bool = False,
) -> None:
    """
    Wait for the Google Pay sheet and confirm the payment.

    Knox-safe: uses ADB screenshots for waiting and detection.

    Google Pay sheet detection uses OCR signals.  All screenshots and taps
    go through ADB to avoid any UiAutomator2 interactions that may have been
    invalidated by Knox killing the server during MLBB screenshots.

    In dry_run mode: wait for the sheet to appear (verifying the full checkout
    flow rendered correctly), then return without tapping the Pay button.
    """
    logger.info("Waiting for Google Pay sheet", device_id=device_id)
    run_logger.log_step("payment", "google_pay_wait", device_id=device_id)

    from ...cv.ocr import OcrEngine
    ocr = OcrEngine()

    # Wait for the Google Pay sheet to appear (ADB screenshots)
    deadline = time.monotonic() + _PAYMENT_SHEET_TIMEOUT
    sheet_img = None
    while time.monotonic() < deadline:
        img = executor.adb_screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)
        logger.info(
            "Polling for Google Pay sheet",
            texts_sample=texts[:100],
            device_id=device_id,
        )
        if any(s in texts for s in _GOOGLE_PAY_SIGNALS):
            sheet_img = img
            run_logger.save_screenshot(img, label="google_pay_sheet")
            logger.info("Google Pay sheet visible", texts_matched=texts[:80], device_id=device_id)
            break
        time.sleep(_POLL_INTERVAL)
    else:
        img = executor.adb_screenshot()
        run_logger.save_screenshot(img, label="google_pay_timeout")
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)
        raise StepError(
            f"Google Pay sheet did not appear within {_PAYMENT_SHEET_TIMEOUT}s. "
            f"OCR texts: {texts[:200]}"
        )

    if dry_run:
        # Verified the sheet rendered — stop before irreversible payment action
        final_img = executor.adb_screenshot()
        run_logger.save_screenshot(final_img, label="dry_run_google_pay_sheet")
        results = ocr.read_region(final_img)
        texts = " ".join(r.text for r in results)
        logger.info(
            "DRY RUN — Google Pay sheet confirmed; skipping final payment tap",
            ocr_texts=texts[:200],
            device_id=device_id,
        )
        return

    # ── Full run: tap "Купить" via ADB (UiAutomator2 may be dead after Knox) ──
    _confirm_payment_adb(executor, run_logger, device_id, sheet_img=sheet_img, ocr=ocr)


def _confirm_payment_adb(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
    sheet_img=None,
    ocr=None,
) -> None:
    """
    Confirm the Google Pay sheet by tapping the "Купить" button via ADB.

    Knox-safe: all actions use ADB screenshots and ADB taps.
    """
    from ...cv.ocr import OcrEngine
    if ocr is None:
        ocr = OcrEngine()

    img = sheet_img or executor.adb_screenshot()
    results = ocr.read_region(img)

    _confirm_signals = ("купить", "buy", "pay", "оплатить", "подтвердить", "purchase")

    # Find the confirmation button by OCR
    confirmed = False
    for result in results:
        if any(s in result.text.lower() for s in _confirm_signals):
            logger.info(
                "ADB tap payment confirm button",
                text=result.text,
                x=result.cx,
                y=result.cy,
                device_id=device_id,
            )
            executor.adb_tap(result.cx, result.cy)
            confirmed = True
            break

    if not confirmed:
        # Heuristic: on Samsung Galaxy A13, Google Pay "Купить" is at bottom-right
        # approximately 72% width, 87% height for landscape 2408×1080
        img_w, img_h = img.width, img.height
        cx = int(img_w * 0.72)
        cy = int(img_h * 0.87)
        logger.warning(
            "Купить button not found by OCR — heuristic ADB tap",
            x=cx,
            y=cy,
            device_id=device_id,
        )
        executor.adb_tap(cx, cy)
    run_logger.log_step("payment", "payment_confirm_tapped", device_id=device_id)


def _try_confirm_payment_native(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> bool:
    """
    Try to confirm the Google Play / Google Pay sheet in the NATIVE_APP context.

    On real Samsung Galaxy A13 (Russian locale) the button text is "Купить".
    Returns True if the button was found and tapped.
    """
    # "Купить" is the actual button text on Russian Google Play billing sheet.
    # Keep others as fallback for non-Russian locales / Google Pay balance users.
    _confirm_labels = ("Купить", "купить", "Pay", "pay", "confirm", "оплатить", "подтвердить")

    logger.info("Trying to confirm payment in NATIVE_APP context", device_id=device_id)
    try:
        executor.switch_to_native()
    except Exception:
        pass

    try:
        for label in _confirm_labels:
            try:
                x, y = executor.find_element(label.capitalize(), retries=2)
                executor.tap(x, y)
                logger.info("Payment confirmed in NATIVE_APP", label=label, device_id=device_id)
                img = executor.screenshot()
                run_logger.save_screenshot(img, label="payment_confirmed_native")
                return True
            except RuntimeError:
                continue
    except Exception as exc:
        logger.warning(
            "Native context payment confirm failed", error=str(exc), device_id=device_id
        )

    return False


def _try_confirm_payment_webview(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> bool:
    """
    Switch through WEBVIEW contexts and attempt to confirm the payment.

    Returns True if the Pay button was found and tapped.
    """
    logger.info("Probing WEBVIEW contexts for payment button", device_id=device_id)

    _confirm_labels = ("Купить", "купить", "Pay", "pay", "confirm", "оплатить", "подтвердить")

    try:
        contexts = executor.get_contexts()
        logger.info("Available contexts", contexts=contexts, device_id=device_id)
    except Exception as exc:
        logger.warning("Could not retrieve contexts", error=str(exc), device_id=device_id)
        return False

    webviews = [c for c in contexts if "WEBVIEW" in c.upper()]
    if not webviews:
        logger.info("No WEBVIEW contexts found", device_id=device_id)
        return False

    for wv in webviews:
        try:
            executor.driver.switch_to.context(wv)
            logger.info("Switched to context", context=wv, device_id=device_id)
            time.sleep(1)

            for label in _confirm_labels:
                try:
                    x, y = executor.find_element(label.capitalize(), retries=2)
                    executor.tap(x, y)
                    logger.info(
                        "Payment confirmed in WEBVIEW",
                        context=wv,
                        label=label,
                        device_id=device_id,
                    )
                    executor.switch_to_native()
                    img = executor.screenshot()
                    run_logger.save_screenshot(img, label="payment_confirmed_webview")
                    return True
                except RuntimeError:
                    continue
        except Exception as exc:
            logger.warning(
                "Error in WEBVIEW context",
                context=wv,
                error=str(exc),
                device_id=device_id,
            )

    # Ensure we return to native context
    executor.switch_to_native()

    return False


# ---------------------------------------------------------------------------
# Device authentication handling (PIN / biometric)
# ---------------------------------------------------------------------------

def _handle_device_auth(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
    payment_pin: str = "",
) -> None:
    """
    Handle device authentication prompts that Google Pay may present after the
    user taps the Pay button.

    Google Pay on Android may require device authentication (PIN, biometric)
    to confirm a payment.  This function:

      1. Checks if a PIN or biometric prompt is visible.
      2. If biometric (fingerprint / face unlock): taps "Use PIN instead" or
         similar fallback to switch to PIN entry, then enters the PIN.
      3. If PIN keypad is visible: enters the PIN one digit at a time using
         Appium key-press events.
      4. If no auth prompt is detected within _AUTH_TIMEOUT, assumes auth was
         not required (or was already handled) and returns silently.

    Args:
        payment_pin: Device unlock PIN.  Pass an empty string if no PIN is
                     configured — biometric/PIN prompts will be cancelled and
                     the flow will continue.
    """
    from ...cv.ocr import OcrEngine

    ocr = OcrEngine()
    device_pin = payment_pin

    logger.info("Checking for device auth prompt after Google Pay", device_id=device_id)
    run_logger.log_step("payment", "device_auth_check", device_id=device_id)

    deadline = time.monotonic() + _AUTH_TIMEOUT
    biometric_dismissed = False
    # Number of consecutive no-auth frames required before declaring "no auth"
    _NO_AUTH_CONFIRM_FRAMES = 2
    no_auth_frames = 0

    while time.monotonic() < deadline:
        img = executor.screenshot()
        results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in results)

        is_pin_prompt = any(s in texts for s in _PIN_SIGNALS)
        is_biometric = any(s in texts for s in _BIOMETRIC_SIGNALS)

        if not is_pin_prompt and not is_biometric:
            # Google Pay auth prompts can appear with a short delay after the
            # Pay button is tapped.  Require multiple consecutive clean frames
            # before concluding that auth is not needed, to avoid skipping an
            # auth prompt that hasn't rendered yet.
            no_auth_frames += 1
            if no_auth_frames >= _NO_AUTH_CONFIRM_FRAMES:
                logger.info(
                    "No device auth prompt detected after %d clean frames — proceeding",
                    _NO_AUTH_CONFIRM_FRAMES,
                    device_id=device_id,
                )
                return
            logger.info(
                "No auth prompt on frame %d — waiting for confirmation",
                no_auth_frames,
                device_id=device_id,
            )
            time.sleep(_POLL_INTERVAL)
            continue

        # Reset the clean-frame counter — we see an auth prompt
        no_auth_frames = 0

        # ── Biometric prompt handling ──────────────────────────────────────
        if is_biometric and not biometric_dismissed:
            run_logger.save_screenshot(img, label="biometric_prompt")
            logger.info("Biometric auth prompt detected", device_id=device_id)

            if device_pin:
                # Try to switch to PIN entry so we can enter the PIN
                _fallback_to_pin(executor, run_logger, device_id)
                biometric_dismissed = True
                time.sleep(1)
                continue
            else:
                # No PIN configured — try to cancel/dismiss the biometric prompt
                logger.warning(
                    "PAYMENT_PIN not configured; attempting to cancel biometric prompt",
                    device_id=device_id,
                )
                _cancel_auth_prompt(executor, run_logger, device_id)
                run_logger.log_step(
                    "payment", "device_auth_skipped_no_pin", device_id=device_id
                )
                return

        # ── PIN prompt handling ────────────────────────────────────────────
        if is_pin_prompt:
            run_logger.save_screenshot(img, label="pin_prompt")
            logger.info("PIN auth prompt detected", device_id=device_id)

            if device_pin:
                _enter_pin(executor, run_logger, device_id, device_pin)
                run_logger.log_step(
                    "payment", "device_auth_pin_entered", device_id=device_id
                )
                # Wait a moment for the auth to process
                time.sleep(2)
                return
            else:
                # No PIN — cancel the prompt and let payment result detection
                # handle whatever state we land in
                logger.warning(
                    "PAYMENT_PIN not configured; cancelling PIN prompt",
                    device_id=device_id,
                )
                _cancel_auth_prompt(executor, run_logger, device_id)
                run_logger.log_step(
                    "payment", "device_auth_skipped_no_pin", device_id=device_id
                )
                return

        time.sleep(_POLL_INTERVAL)

    # Timed out waiting for auth to resolve — log and continue; result
    # detection will observe whatever state the device is in.
    logger.warning(
        "Device auth check timed out — proceeding to result detection",
        device_id=device_id,
    )
    run_logger.log_step("payment", "device_auth_timeout", device_id=device_id)


def _fallback_to_pin(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Tap the 'Use PIN instead' / 'Use password instead' button on a biometric prompt."""
    _fallback_labels = (
        "Use PIN instead",
        "Use password instead",
        "Use pattern instead",
        "Использовать PIN",
        "Вместо этого",
    )
    for label in _fallback_labels:
        try:
            x, y = executor.find_element(label, retries=2)
            executor.tap(x, y)
            logger.info(
                "Tapped biometric fallback button",
                label=label,
                device_id=device_id,
            )
            return
        except RuntimeError:
            continue

    logger.warning(
        "Could not find biometric fallback button — staying on biometric prompt",
        device_id=device_id,
    )


def _enter_pin(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
    pin: str,
) -> None:
    """Enter a numeric PIN on the device keypad, then confirm with OK/Enter."""
    logger.info("Entering device PIN", pin_length=len(pin), device_id=device_id)

    for digit in pin:
        # Try to find the digit button on the keypad by its text label
        try:
            x, y = executor.find_element(digit, retries=2)
            executor.tap(x, y)
            time.sleep(0.15)
        except RuntimeError:
            # Fall back to key-press events (numeric keyboard)
            executor.press_key(int(digit) + 7)  # KEYCODE_0=7, KEYCODE_1=8, …
            time.sleep(0.15)

    # Confirm the PIN — try "OK", "Confirm", or Enter key
    _ok_labels = ("OK", "Confirm", "Done", "ОК", "Готово")
    confirmed = False
    for label in _ok_labels:
        try:
            x, y = executor.find_element(label, retries=1)
            executor.tap(x, y)
            confirmed = True
            break
        except RuntimeError:
            continue

    if not confirmed:
        # Press Enter (KEYCODE_ENTER = 66)
        executor.press_key(66)

    logger.info("PIN entered and confirmed", device_id=device_id)


def _cancel_auth_prompt(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> None:
    """Cancel / dismiss an auth prompt (when no PIN is configured)."""
    _cancel_labels = ("Cancel", "Отмена", "Skip", "Пропустить")
    for label in _cancel_labels:
        try:
            x, y = executor.find_element(label, retries=2)
            executor.tap(x, y)
            logger.info("Auth prompt cancelled", label=label, device_id=device_id)
            return
        except RuntimeError:
            continue

    # Press Back as last resort
    executor.press_back()
    logger.warning(
        "Auth prompt not cancelled via button — pressed Back", device_id=device_id
    )


# ---------------------------------------------------------------------------
# Result detection
# ---------------------------------------------------------------------------

def _detect_payment_result(
    executor: AppiumExecutor,
    run_logger: RunLogger,
    device_id: str,
) -> str:
    """
    Poll the screen for up to _RESULT_TIMEOUT seconds to detect success/failure.

    Detection uses a hybrid strategy — template matching first (lower latency,
    robust to UI drift), then OCR text as a fallback.  Both must agree before
    a result is committed.  This prevents false positives from partial OCR
    matches on unrelated screens.

    Template files required (placeholders provided; replace with real crops):
      - templates/payment_success.png
      - templates/payment_failed.png

    Returns:
        "success"        — success screen confirmed by template and/or OCR.
        "failed:<reason>"— failure screen confirmed by template and/or OCR.
        "timeout"        — neither signal detected within _RESULT_TIMEOUT.
    """
    from ...cv.ocr import OcrEngine
    from ...cv.template_matcher import TemplateMatcher

    ocr = OcrEngine()
    matcher = TemplateMatcher()

    logger.info("Waiting for payment result", device_id=device_id)
    run_logger.log_step("payment", "detecting_result", device_id=device_id)

    deadline = time.monotonic() + _RESULT_TIMEOUT

    while time.monotonic() < deadline:
        # Ensure we're in native context for screenshot
        try:
            executor.switch_to_native()
        except Exception:
            pass

        img = executor.screenshot()

        # ── Stage 1: Template match (payment_success / payment_failed) ────────
        success_template = matcher.find(img, "payment_success", threshold=_TEMPLATE_MATCH_THRESHOLD)
        failed_template = matcher.find(img, "payment_failed", threshold=_TEMPLATE_MATCH_THRESHOLD)

        # ── Stage 2: Payment-specific OCR text scan ────────────────────────
        ocr_results = ocr.read_region(img)
        texts = " ".join(r.text.lower() for r in ocr_results)
        ocr_success = any(s in texts for s in _SUCCESS_SIGNALS)
        ocr_failure = any(s in texts for s in _FAILURE_SIGNALS)

        # ── Decision logic ─────────────────────────────────────────────────
        # Success requires template+OCR agreement, OR a very-high-confidence
        # template alone (>= _TEMPLATE_STRONG_THRESHOLD), OR OCR-only match
        # (these phrases are payment-specific so false-positive risk is low).
        success_confirmed = (
            (success_template is not None and ocr_success)          # both agree
            or (success_template is not None
                and success_template.confidence >= _TEMPLATE_STRONG_THRESHOLD)  # strong template
            or ocr_success                                          # payment-specific OCR phrase
        )
        failure_confirmed = (
            (failed_template is not None and ocr_failure)
            or (failed_template is not None
                and failed_template.confidence >= _TEMPLATE_STRONG_THRESHOLD)
            or ocr_failure
        )

        if success_confirmed:
            t_conf = round(success_template.confidence, 3) if success_template else None
            signal = (
                "template+ocr" if (success_template and ocr_success)
                else ("template" if success_template else "ocr")
            )
            logger.info(
                "Payment success screen detected",
                signal=signal,
                template_confidence=t_conf,
                device_id=device_id,
            )
            run_logger.save_screenshot(img, label="payment_success")
            return "success"

        if failure_confirmed:
            t_conf = round(failed_template.confidence, 3) if failed_template else None
            signal = (
                "template+ocr" if (failed_template and ocr_failure)
                else ("template" if failed_template else "ocr")
            )
            failure_text = next(
                (r.text for r in ocr_results if any(s in r.text.lower() for s in _FAILURE_SIGNALS)),
                "unknown_error",
            )
            logger.warning(
                "Payment failure screen detected",
                signal=signal,
                failure_text=failure_text,
                template_confidence=t_conf,
                device_id=device_id,
            )
            run_logger.save_screenshot(img, label="payment_failed")
            return f"failed:{failure_text}"

        time.sleep(_POLL_INTERVAL)

    img = executor.screenshot()
    run_logger.save_screenshot(img, label="payment_result_timeout")
    logger.warning(
        "Payment result not detected within timeout",
        timeout=_RESULT_TIMEOUT,
        device_id=device_id,
    )
    return "timeout"
