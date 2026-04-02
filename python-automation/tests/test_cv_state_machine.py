"""
Unit tests for StateMachine (cv/state_machine.py).

Verifies BFS path planning, transition execution, timeout behaviour,
and retry logic — all without a real device or driver.
"""

from __future__ import annotations

import time
from typing import List
from unittest.mock import MagicMock, patch, call

import pytest
from PIL import Image

from mlbb_automation.cv.screen_detector import ScreenDetector, ScreenState
from mlbb_automation.cv.state_machine import StateMachine, Transition, NavigationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _white() -> Image.Image:
    return Image.new("RGB", (100, 100), (255, 255, 255))


def _mock_executor(screenshots: List[Image.Image] = None):
    """Return a mock executor whose screenshot() cycles through the given images."""
    exe = MagicMock()
    if screenshots is None:
        screenshots = [_white()]
    exe.screenshot.side_effect = screenshots + [screenshots[-1]] * 100
    return exe


def _make_machine(
    executor,
    detect_sequence: List[ScreenState],
    poll_interval: float = 0.01,
    transition_timeout: float = 0.5,
) -> StateMachine:
    """
    Build a StateMachine where detect() returns states from detect_sequence in order.
    """
    detector = MagicMock(spec=ScreenDetector)
    detector.detect.side_effect = detect_sequence + [detect_sequence[-1]] * 50

    machine = StateMachine(
        executor=executor,
        detector=detector,
        poll_interval=poll_interval,
        transition_timeout=transition_timeout,
    )
    return machine


# ---------------------------------------------------------------------------
# BFS path planning
# ---------------------------------------------------------------------------

class TestBfsPlanning:
    def test_no_path_needed_when_already_at_target(self):
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[ScreenState.MLBB_SHOP],
        )
        # Should return immediately without firing any transitions
        machine.navigate_to(ScreenState.MLBB_SHOP)

    def test_single_step_path(self):
        """MLBB_SHOP → MLBB_SHOP_DIAMONDS via one transition."""
        exe = _mock_executor()
        action_called = {"n": 0}

        def my_action(e):
            action_called["n"] += 1

        machine = _make_machine(
            exe,
            # detect returns: SHOP (initial), then SHOP_DIAMONDS (after transition)
            detect_sequence=[
                ScreenState.MLBB_SHOP,
                ScreenState.MLBB_SHOP_DIAMONDS,
            ],
        )
        # Replace registered transitions with our single known transition
        machine._transitions = [
            Transition(
                source=ScreenState.MLBB_SHOP,
                target=ScreenState.MLBB_SHOP_DIAMONDS,
                action=my_action,
                label="test_step",
            )
        ]
        machine.navigate_to(ScreenState.MLBB_SHOP_DIAMONDS)
        assert action_called["n"] == 1

    def test_raises_when_no_path_exists(self):
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[ScreenState.MLBB_LOADING],
        )
        # No transitions registered → no path from LOADING to PAYMENT_SUCCESS
        machine._transitions = []
        with pytest.raises(NavigationError, match="No path"):
            machine.navigate_to(ScreenState.PAYMENT_SUCCESS)

    def test_multi_step_path(self):
        """
        Verify 2-step path: UNKNOWN → MLBB_MAIN_MENU → MLBB_SHOP.
        BFS should find the 2-edge path.
        """
        exe = _mock_executor()
        action_log = []

        def action_a(e):
            action_log.append("A")

        def action_b(e):
            action_log.append("B")

        machine = _make_machine(
            exe,
            detect_sequence=[
                ScreenState.UNKNOWN,       # initial
                ScreenState.MLBB_MAIN_MENU,  # after A
                ScreenState.MLBB_SHOP,     # after B
            ],
        )
        machine._transitions = [
            Transition(ScreenState.UNKNOWN, ScreenState.MLBB_MAIN_MENU, action_a, "a"),
            Transition(ScreenState.MLBB_MAIN_MENU, ScreenState.MLBB_SHOP, action_b, "b"),
        ]
        machine.navigate_to(ScreenState.MLBB_SHOP)
        assert action_log == ["A", "B"]


# ---------------------------------------------------------------------------
# Transition execution and timeout
# ---------------------------------------------------------------------------

class TestTransitionExecution:
    def test_timeout_raises_navigation_error(self):
        """If target state never appears, NavigationError raised after timeout."""
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[ScreenState.MLBB_SHOP] * 100,  # never transitions
            transition_timeout=0.1,
        )
        machine._transitions = [
            Transition(
                ScreenState.MLBB_SHOP,
                ScreenState.MLBB_SHOP_DIAMONDS,
                action=lambda e: None,
                label="stuck",
            )
        ]
        with pytest.raises(NavigationError, match="timed out"):
            machine.navigate_to(ScreenState.MLBB_SHOP_DIAMONDS)

    def test_action_exception_wraps_in_navigation_error(self):
        """Exceptions from the action callable are wrapped in NavigationError."""
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[ScreenState.MLBB_SHOP],
        )
        def bad_action(e):
            raise RuntimeError("action crashed")

        machine._transitions = [
            Transition(
                ScreenState.MLBB_SHOP,
                ScreenState.MLBB_SHOP_DIAMONDS,
                action=bad_action,
                label="bad",
            )
        ]
        with pytest.raises(NavigationError, match="action crashed"):
            machine.navigate_to(ScreenState.MLBB_SHOP_DIAMONDS)


# ---------------------------------------------------------------------------
# Google login / 2FA transitions
# ---------------------------------------------------------------------------

class TestGoogleLoginTransitions:
    def test_bfs_finds_path_from_google_login_to_mlbb_main_menu(self):
        """
        The full path GOOGLE_LOGIN → MLBB_LOADING → MLBB_MAIN_MENU must be
        discoverable via BFS using the registered default transitions.
        """
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[
                ScreenState.GOOGLE_LOGIN,
                ScreenState.MLBB_LOADING,
                ScreenState.MLBB_MAIN_MENU,
            ],
        )
        # find_element always "succeeds" so transition actions don't error
        exe.find_element.return_value = (100, 200)
        machine.navigate_to(ScreenState.MLBB_MAIN_MENU)

    def test_bfs_finds_path_from_google_2fa_to_mlbb_main_menu(self):
        """
        GOOGLE_2FA → GOOGLE_LOGIN → MLBB_LOADING → MLBB_MAIN_MENU
        must be reachable via the cancel recovery + login transitions.
        """
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[
                ScreenState.GOOGLE_2FA,
                ScreenState.GOOGLE_LOGIN,
                ScreenState.MLBB_LOADING,
                ScreenState.MLBB_MAIN_MENU,
            ],
        )
        exe.find_element.return_value = (100, 200)
        machine.navigate_to(ScreenState.MLBB_MAIN_MENU)

    def test_no_path_from_payment_success_to_google_login(self):
        """
        After payment succeeds there is no transition back to GOOGLE_LOGIN —
        these are terminal / one-directional states.
        """
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[ScreenState.PAYMENT_SUCCESS],
        )
        exe.find_element.return_value = (100, 200)
        with pytest.raises(NavigationError, match="No path"):
            machine.navigate_to(ScreenState.GOOGLE_LOGIN)


# ---------------------------------------------------------------------------
# Strict no-fallback behaviour
# ---------------------------------------------------------------------------

class TestStrictNoFallback:
    def test_find_and_tap_raises_navigation_error_when_element_not_found(self):
        """
        When all 3 stages (template, OCR, Appium) fail, the transition action must
        raise NavigationError — NOT silently tap a hard-coded coordinate.
        """
        exe = _mock_executor()
        machine = _make_machine(
            exe,
            detect_sequence=[ScreenState.MLBB_MAIN_MENU],
        )
        # Let the state machine use its real registered transitions but patch
        # find_element so it always raises (simulating all 3 stages failing)
        with patch.object(exe, "find_element", side_effect=RuntimeError("all stages failed")):
            with pytest.raises(NavigationError, match="find_and_tap"):
                machine.navigate_to(ScreenState.MLBB_SHOP, max_retries=1)

        # The executor must NOT have been asked to tap any coordinates
        exe.tap.assert_not_called()


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------

class TestRetry:
    def test_retries_on_navigation_error(self):
        """navigate_to retries up to max_retries times on NavigationError."""
        exe = _mock_executor()
        attempt_count = {"n": 0}

        def counting_action(e):
            attempt_count["n"] += 1

        machine = _make_machine(
            exe,
            detect_sequence=[ScreenState.MLBB_SHOP] * 100,
            transition_timeout=0.05,
        )
        machine._transitions = [
            Transition(
                ScreenState.MLBB_SHOP,
                ScreenState.MLBB_SHOP_DIAMONDS,
                action=counting_action,
                label="retry_test",
            )
        ]
        with pytest.raises(NavigationError):
            machine.navigate_to(ScreenState.MLBB_SHOP_DIAMONDS, max_retries=2)

        # action should have been called once per retry attempt
        assert attempt_count["n"] >= 2
