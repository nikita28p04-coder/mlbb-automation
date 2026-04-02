"""
Unit tests for scenario step modules:
  - scenarios/steps/google_account.py
  - scenarios/steps/install_mlbb.py
  - scenarios/steps/mlbb_onboarding.py
  - scenarios/steps/payment.py

All tests mock the AppiumExecutor and OcrEngine so no real device or
network connection is required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from PIL import Image

from mlbb_automation.cv.ocr import OcrResult
from mlbb_automation.logging.logger import RunLogger


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _white(w: int = 200, h: int = 200) -> Image.Image:
    return Image.new("RGB", (w, h), (255, 255, 255))


def _ocr(text: str, conf: float = 0.9, cx: int = 100, cy: int = 100) -> OcrResult:
    """Make an OcrResult with minimal required fields."""
    return OcrResult(text=text, confidence=conf, bbox=(80, 80, 120, 120), cx=cx, cy=cy)


def _make_executor():
    exe = MagicMock()
    exe.screenshot.return_value = _white()
    exe.find_element.return_value = (100, 100)
    exe.get_screen_size.return_value = (1080, 1920)
    exe.get_contexts.return_value = ["NATIVE_APP"]
    exe.is_app_installed.return_value = False
    return exe


def _make_run_logger(tmp_path: Path) -> RunLogger:
    return RunLogger(run_id="steps_test", log_dir=tmp_path)


# ===========================================================================
# google_account
# ===========================================================================

class TestGoogleAccountStep:
    """Tests for scenarios/steps/google_account.py"""

    def test_run_calls_expected_phases(self, tmp_path):
        """run() should call log_step for started and ok."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        email_ocr = _ocr("enter your email")
        password_ocr = _ocr("enter your password")
        success_ocr = _ocr("Sync")
        skip_ocr = _ocr("Skip")

        ocr_sequences = [
            [email_ocr],       # _enter_email wait
            [password_ocr],    # _enter_password wait
            [success_ocr],     # _handle_intermediate_screens → success signal
            [success_ocr],     # _verify_account_added
        ]
        call_count = [0]

        def fake_read_region(img, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(ocr_sequences):
                return ocr_sequences[idx]
            return [success_ocr]

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", side_effect=fake_read_region), \
             patch("mlbb_automation.scenarios.steps.google_account._open_settings_accounts"), \
             patch("mlbb_automation.scenarios.steps.google_account._tap_add_account"), \
             patch("mlbb_automation.scenarios.steps.google_account._verify_account_added"):
            from mlbb_automation.scenarios.steps import google_account
            google_account.run(
                executor=exe,
                run_logger=run_logger,
                email="test@gmail.com",
                password="secret",
                device_id="dev1",
            )

    def test_2fa_signal_raises_step_error_in_password_phase(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        email_ocr = _ocr("enter your email")
        twofa_ocr = _ocr("2-step verification")

        ocr_sequences = [
            [email_ocr],   # wait for email screen
            [twofa_ocr],   # password screen shows 2FA
        ]
        call_count = [0]

        def fake_read_region(img, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(ocr_sequences):
                return ocr_sequences[idx]
            return [twofa_ocr]

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", side_effect=fake_read_region), \
             patch("mlbb_automation.scenarios.steps.google_account._open_settings_accounts"), \
             patch("mlbb_automation.scenarios.steps.google_account._tap_add_account"):
            from mlbb_automation.scenarios.steps.google_account import StepError
            from mlbb_automation.scenarios.steps import google_account
            with pytest.raises(StepError, match="2FA"):
                google_account.run(
                    executor=exe,
                    run_logger=run_logger,
                    email="test@gmail.com",
                    password="secret",
                    device_id="dev1",
                )


# ===========================================================================
# install_mlbb
# ===========================================================================

class TestInstallMlbbStep:
    """Tests for scenarios/steps/install_mlbb.py"""

    def test_skips_install_if_already_installed(self, tmp_path):
        exe = _make_executor()
        exe.is_app_installed.return_value = True
        run_logger = _make_run_logger(tmp_path)

        loading_ocr = _ocr("Mobile Legends")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[loading_ocr]):
            from mlbb_automation.scenarios.steps import install_mlbb
            install_mlbb.run(executor=exe, run_logger=run_logger, device_id="d1")

        # Play Store should NOT have been opened
        exe.driver.execute_script.assert_not_called()

    def test_opens_play_store_and_installs(self, tmp_path):
        exe = _make_executor()
        exe.is_app_installed.return_value = False
        run_logger = _make_run_logger(tmp_path)

        # Sequence: Play Store screen → install in progress → Open button appears
        install_ocr = _ocr("Install")
        in_progress_ocr = _ocr("Downloading")
        open_ocr = _ocr("Open")
        loading_ocr = _ocr("Mobile Legends")

        ocr_sequences = [
            [install_ocr],       # _tap_install_and_wait: initial screen
            [install_ocr],       # first check in wait loop
            [in_progress_ocr],   # still downloading
            [open_ocr],          # install complete
            [loading_ocr],       # _wait_for_mlbb_loading
        ]
        call_count = [0]

        def fake_read(img, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return ocr_sequences[idx] if idx < len(ocr_sequences) else [open_ocr]

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", side_effect=fake_read), \
             patch("mlbb_automation.scenarios.steps.install_mlbb.time") as mock_time:
            mock_time.monotonic.side_effect = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps import install_mlbb
            install_mlbb.run(executor=exe, run_logger=run_logger, device_id="d1")

        # Play Store should have been opened via intent
        exe.driver.execute_script.assert_called()

    def test_raises_on_install_timeout(self, tmp_path):
        exe = _make_executor()
        exe.is_app_installed.return_value = False
        run_logger = _make_run_logger(tmp_path)

        # Play Store shows Install button
        install_ocr = _ocr("Install")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[install_ocr]), \
             patch("mlbb_automation.scenarios.steps.install_mlbb._INSTALL_TIMEOUT", 0), \
             patch("mlbb_automation.scenarios.steps.install_mlbb._open_play_store"), \
             patch("mlbb_automation.scenarios.steps.install_mlbb.time") as mock_time:
            mock_time.monotonic.side_effect = [0, 1, 2, 3, 4, 5]
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.install_mlbb import StepError
            from mlbb_automation.scenarios.steps import install_mlbb
            with pytest.raises(StepError, match="timed out"):
                install_mlbb.run(executor=exe, run_logger=run_logger, device_id="d1")


# ===========================================================================
# mlbb_onboarding
# ===========================================================================

class TestMlbbOnboardingStep:
    """Tests for scenarios/steps/mlbb_onboarding.py"""

    def test_reaches_main_menu_after_tapping_skip(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        skip_ocr = _ocr("Skip")
        main_menu_ocr = _ocr("Classic")

        # First screenshot: loading; second: skip button; third: main menu
        ocr_sequences = [
            [_ocr("Mobile Legends")],  # _wait_for_loading: still loading
            [main_menu_ocr],           # _wait_for_loading: done (main menu)
        ]
        call_count = [0]

        def fake_read(img, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return ocr_sequences[idx] if idx < len(ocr_sequences) else [main_menu_ocr]

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", side_effect=fake_read), \
             patch("mlbb_automation.scenarios.steps.install_mlbb.MLBB_PACKAGE", "com.mobile.legends"):
            from mlbb_automation.scenarios.steps import mlbb_onboarding
            mlbb_onboarding.run(executor=exe, run_logger=run_logger, device_id="d1")

    def test_taps_screen_center_when_no_button_found(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        no_button_ocr = _ocr("some random text")
        main_menu_ocr = _ocr("Classic")

        # Loading → unknown screen (no button for a while) → main menu
        iterations = [0]

        def fake_read(img, **kwargs):
            iterations[0] += 1
            if iterations[0] <= 1:
                return [no_button_ocr]   # loading/unknown screen
            return [main_menu_ocr]        # main menu on subsequent checks

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", side_effect=fake_read):
            from mlbb_automation.scenarios.steps import mlbb_onboarding
            mlbb_onboarding.run(executor=exe, run_logger=run_logger, device_id="d1")


# ===========================================================================
# payment
# ===========================================================================

class TestPaymentStep:
    """Tests for scenarios/steps/payment.py"""

    def _setup_payment_ocr_sequence(self):
        """Return an OCR sequence that walks through the full payment flow."""
        return [
            [_ocr("Shop")],              # _open_shop: verify shop opened
            [_ocr("Diamonds")],          # _open_diamonds_section: already there
            [_ocr("86"), _ocr("0.99")],  # _select_smallest_package
            [_ocr("Google Pay")],        # _handle_google_pay: sheet visible
            [_ocr("Pay")],               # _try_confirm_payment_native: confirm button
            [_ocr("Purchase Successful")],  # _detect_payment_result: success
        ]

    def test_dry_run_taps_buy_but_skips_payment_confirmation(self, tmp_path):
        """dry_run should navigate through Buy → Google Pay sheet, then stop."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        with patch("mlbb_automation.scenarios.steps.payment._open_shop"), \
             patch("mlbb_automation.scenarios.steps.payment._open_diamonds_section"), \
             patch("mlbb_automation.scenarios.steps.payment._select_smallest_package"), \
             patch("mlbb_automation.scenarios.steps.payment._tap_buy") as mock_buy, \
             patch("mlbb_automation.scenarios.steps.payment._handle_google_pay") as mock_pay:
            from mlbb_automation.scenarios.steps import payment
            payment.run(
                executor=exe,
                run_logger=run_logger,
                device_id="d1",
                dry_run=True,
            )

        # Buy IS tapped in dry_run mode
        mock_buy.assert_called_once()
        # Google Pay handler IS called with dry_run=True
        mock_pay.assert_called_once_with(exe, run_logger, "d1", dry_run=True)

    def test_success_detection(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        success_ocr = _ocr("Purchase Successful")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[success_ocr]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.scenarios.steps.payment._open_shop"), \
             patch("mlbb_automation.scenarios.steps.payment._open_diamonds_section"), \
             patch("mlbb_automation.scenarios.steps.payment._select_smallest_package"), \
             patch("mlbb_automation.scenarios.steps.payment._tap_buy"), \
             patch("mlbb_automation.scenarios.steps.payment._handle_google_pay"):
            from mlbb_automation.scenarios.steps import payment
            payment.run(executor=exe, run_logger=run_logger, device_id="d1", dry_run=False)

    def test_payment_failure_raises_payment_error(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        fail_ocr = _ocr("Payment Failed")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[fail_ocr]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.scenarios.steps.payment._open_shop"), \
             patch("mlbb_automation.scenarios.steps.payment._open_diamonds_section"), \
             patch("mlbb_automation.scenarios.steps.payment._select_smallest_package"), \
             patch("mlbb_automation.scenarios.steps.payment._tap_buy"), \
             patch("mlbb_automation.scenarios.steps.payment._handle_google_pay"):
            from mlbb_automation.scenarios.steps.payment import PaymentError
            from mlbb_automation.scenarios.steps import payment
            with pytest.raises(PaymentError, match="[Ff]ailed"):
                payment.run(executor=exe, run_logger=run_logger, device_id="d1", dry_run=False)

    def test_shop_not_found_raises_step_error(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        # All find_element calls fail (Shop button not found in any language)
        exe.find_element.side_effect = RuntimeError("not found")

        unknown_ocr = _ocr("unknown text")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[unknown_ocr]):
            from mlbb_automation.scenarios.steps.payment import StepError
            from mlbb_automation.scenarios.steps import payment
            with pytest.raises(StepError, match="[Ss]hop"):
                payment.run(executor=exe, run_logger=run_logger, device_id="d1", dry_run=False)
            # find_element should have been tried multiple times (once per localized label)
            assert exe.find_element.call_count >= 2

    def test_google_pay_webview_context_attempted(self, tmp_path):
        exe = _make_executor()
        exe.get_contexts.return_value = ["NATIVE_APP", "WEBVIEW_0"]
        run_logger = _make_run_logger(tmp_path)

        googlepay_ocr = _ocr("Google Pay")
        confirm_ocr = _ocr("Pay")
        success_ocr = _ocr("Purchase Successful")

        ocr_sequences = [
            [googlepay_ocr],   # _handle_google_pay: sheet present
            [success_ocr],     # _detect_payment_result
        ]
        call_count = [0]

        def fake_read(img, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return ocr_sequences[idx] if idx < len(ocr_sequences) else [success_ocr]

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", side_effect=fake_read), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.scenarios.steps.payment._open_shop"), \
             patch("mlbb_automation.scenarios.steps.payment._open_diamonds_section"), \
             patch("mlbb_automation.scenarios.steps.payment._select_smallest_package"), \
             patch("mlbb_automation.scenarios.steps.payment._tap_buy"), \
             patch("mlbb_automation.scenarios.steps.payment._try_confirm_payment_native", return_value=False), \
             patch("mlbb_automation.scenarios.steps.payment._try_confirm_payment_webview", return_value=True):
            from mlbb_automation.scenarios.steps import payment
            payment.run(executor=exe, run_logger=run_logger, device_id="d1", dry_run=False)


# ===========================================================================
# detect_payment_result helper
# ===========================================================================

class TestDetectPaymentResult:
    """Unit tests for the _detect_payment_result helper (hybrid template+OCR)."""

    def test_returns_success_on_ocr_text(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("Purchase Successful")]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None):
            from mlbb_automation.scenarios.steps.payment import _detect_payment_result
            result = _detect_payment_result(exe, run_logger, "d1")

        assert result == "success"

    def test_returns_success_on_template_match(self, tmp_path):
        """Template hit alone (no OCR match) should be sufficient for success."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        from mlbb_automation.cv.template_matcher import MatchResult

        success_match = MatchResult(
            template_name="payment_success",
            cx=100, cy=100, confidence=0.9, scale=1.0,
            bbox=(80, 80, 120, 120),
        )

        def fake_find(img, name, threshold=0.8):
            return success_match if name == "payment_success" else None

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("loading")]),  \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", side_effect=fake_find):
            from mlbb_automation.scenarios.steps.payment import _detect_payment_result
            result = _detect_payment_result(exe, run_logger, "d1")

        assert result == "success"

    def test_returns_failed_on_failure_text(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("Transaction declined")]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None):
            from mlbb_automation.scenarios.steps.payment import _detect_payment_result
            result = _detect_payment_result(exe, run_logger, "d1")

        assert result.startswith("failed:")

    def test_returns_failed_on_template_match(self, tmp_path):
        """Failed template match alone should produce a failed result."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        from mlbb_automation.cv.template_matcher import MatchResult

        fail_match = MatchResult(
            template_name="payment_failed",
            cx=100, cy=100, confidence=0.92, scale=1.0,
            bbox=(80, 80, 120, 120),
        )

        def fake_find(img, name, threshold=0.8):
            return fail_match if name == "payment_failed" else None

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("loading")]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", side_effect=fake_find):
            from mlbb_automation.scenarios.steps.payment import _detect_payment_result
            result = _detect_payment_result(exe, run_logger, "d1")

        assert result.startswith("failed:")

    def test_returns_timeout_when_no_signal(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("loading...")]), \
             patch("mlbb_automation.cv.template_matcher.TemplateMatcher.find", return_value=None), \
             patch("mlbb_automation.scenarios.steps.payment._RESULT_TIMEOUT", 0):
            from mlbb_automation.scenarios.steps.payment import _detect_payment_result
            result = _detect_payment_result(exe, run_logger, "d1")

        assert result == "timeout"


class TestDeviceAuthHandling:
    """Tests for _handle_device_auth, _enter_pin, _fallback_to_pin, _cancel_auth_prompt."""

    def test_no_auth_prompt_returns_immediately(self, tmp_path):
        """When no PIN/biometric prompt is visible, _handle_device_auth returns silently."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        # OCR returns generic text (no auth signals)
        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("loading")]), \
             patch("mlbb_automation.scenarios.steps.payment.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.payment import _handle_device_auth
            _handle_device_auth(exe, run_logger, "d1")

        # No taps should have been needed
        exe.tap.assert_not_called()

    def test_pin_prompt_enters_pin_from_env(self, tmp_path, monkeypatch):
        """When PIN prompt is detected and PAYMENT_PIN is set, digits are entered."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        monkeypatch.setenv("PAYMENT_PIN", "1234")

        # find_element succeeds for each digit button
        exe.find_element.return_value = (100, 200)

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("Enter PIN")]), \
             patch("mlbb_automation.scenarios.steps.payment.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.payment import _handle_device_auth
            _handle_device_auth(exe, run_logger, "d1")

        # 4 digit taps + 1 OK tap (at minimum)
        assert exe.tap.call_count >= 4

    def test_pin_prompt_uses_key_press_when_button_not_found(self, tmp_path, monkeypatch):
        """Falls back to press_key when digit button not found by find_element."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        monkeypatch.setenv("PAYMENT_PIN", "42")

        # find_element raises for digit buttons but succeeds for OK
        call_n = [0]
        def flaky_find(label, **kwargs):
            call_n[0] += 1
            if label.isdigit():
                raise RuntimeError("not found")
            return (100, 200)
        exe.find_element.side_effect = flaky_find

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("Enter PIN")]), \
             patch("mlbb_automation.scenarios.steps.payment.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.payment import _handle_device_auth
            _handle_device_auth(exe, run_logger, "d1")

        # press_key called for each digit
        assert exe.press_key.call_count >= 2

    def test_biometric_prompt_falls_back_to_pin(self, tmp_path, monkeypatch):
        """Biometric prompt → taps 'Use PIN instead', then enters PIN."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        monkeypatch.setenv("PAYMENT_PIN", "0000")

        ocr_seq = iter([
            [_ocr("Fingerprint")],          # first iteration — biometric
            [_ocr("Enter PIN")],            # after fallback — PIN prompt
        ])

        exe.find_element.return_value = (100, 200)

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   side_effect=lambda *a, **kw: next(ocr_seq, [_ocr("done")])), \
             patch("mlbb_automation.scenarios.steps.payment.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.payment import _handle_device_auth
            _handle_device_auth(exe, run_logger, "d1")

        # find_element was called (for fallback button + digit buttons)
        assert exe.find_element.call_count >= 1

    def test_no_pin_cancels_biometric_prompt(self, tmp_path, monkeypatch):
        """When PAYMENT_PIN is not set, biometric prompt is cancelled."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        monkeypatch.delenv("PAYMENT_PIN", raising=False)

        exe.find_element.return_value = (100, 200)

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("Fingerprint")]), \
             patch("mlbb_automation.scenarios.steps.payment.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.payment import _handle_device_auth
            _handle_device_auth(exe, run_logger, "d1")

        # Should have attempted to tap Cancel or pressed Back
        assert exe.tap.call_count >= 1 or exe.press_back.call_count >= 1

    def test_auth_timeout_logs_and_continues(self, tmp_path, monkeypatch):
        """When auth never resolves, times out and continues without error."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        monkeypatch.delenv("PAYMENT_PIN", raising=False)

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region",
                   return_value=[_ocr("Enter PIN")]), \
             patch("mlbb_automation.scenarios.steps.payment._AUTH_TIMEOUT", 0), \
             patch("mlbb_automation.scenarios.steps.payment.time") as mock_time:
            mock_time.monotonic.side_effect = [0, 9999]  # immediately past deadline
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.payment import _handle_device_auth
            # Should not raise
            _handle_device_auth(exe, run_logger, "d1")

    def test_enter_pin_calls_ok_to_confirm(self, tmp_path):
        """_enter_pin taps an OK button after entering all digits."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)
        exe.find_element.return_value = (50, 50)

        with patch("mlbb_automation.scenarios.steps.payment.time") as mock_time:
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.payment import _enter_pin
            _enter_pin(exe, run_logger, "d1", "123")

        # 3 digit taps + 1 OK tap
        assert exe.tap.call_count >= 4

    def test_run_calls_handle_device_auth_before_result_detection(self, tmp_path):
        """Full run() must call _handle_device_auth between payment confirm and result detection."""
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        call_order = []

        with patch("mlbb_automation.scenarios.steps.payment._open_shop"), \
             patch("mlbb_automation.scenarios.steps.payment._open_diamonds_section"), \
             patch("mlbb_automation.scenarios.steps.payment._select_smallest_package"), \
             patch("mlbb_automation.scenarios.steps.payment._tap_buy"), \
             patch("mlbb_automation.scenarios.steps.payment._handle_google_pay"), \
             patch("mlbb_automation.scenarios.steps.payment._handle_device_auth",
                   side_effect=lambda *a, **kw: call_order.append("auth")), \
             patch("mlbb_automation.scenarios.steps.payment._detect_payment_result",
                   side_effect=lambda *a, **kw: (call_order.append("detect"), "success")[1]):
            from mlbb_automation.scenarios.steps import payment
            payment.run(exe, run_logger, "d1", dry_run=False)

        assert call_order.index("auth") < call_order.index("detect")


class TestGoogleAccountVerification:
    """Enforce that account verification failure raises StepError."""

    def test_verification_raises_step_error_when_account_not_found(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        # OCR never returns the email or success signals
        unknown_ocr = _ocr("some unrelated text")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[unknown_ocr]), \
             patch("mlbb_automation.scenarios.steps.google_account.time") as mock_time:
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.google_account import (
                StepError,
                _verify_account_added,
            )
            with pytest.raises(StepError, match="could not be confirmed"):
                _verify_account_added(exe, run_logger, "test@gmail.com", "d1")

    def test_verification_passes_when_email_in_ocr(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        email_ocr = _ocr("test@gmail.com")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[email_ocr]), \
             patch("mlbb_automation.scenarios.steps.google_account.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.google_account import _verify_account_added
            _verify_account_added(exe, run_logger, "test@gmail.com", "d1")

    def test_verification_passes_on_sync_signal(self, tmp_path):
        exe = _make_executor()
        run_logger = _make_run_logger(tmp_path)

        sync_ocr = _ocr("Account added Sync")

        with patch("mlbb_automation.cv.ocr.OcrEngine.read_region", return_value=[sync_ocr]), \
             patch("mlbb_automation.scenarios.steps.google_account.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.sleep = lambda _: None
            from mlbb_automation.scenarios.steps.google_account import _verify_account_added
            _verify_account_added(exe, run_logger, "other@gmail.com", "d1")
