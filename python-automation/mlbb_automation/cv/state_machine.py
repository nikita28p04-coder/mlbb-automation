"""
State machine for navigating between MLBB/Google screens.

Defines a directed graph of allowed screen transitions, each with an action
that moves from one state to the next (e.g. tap a button, swipe, etc.).

navigate_to(target) finds the shortest path using BFS and executes each edge
with retry logic and a per-transition timeout of 45 seconds.

Usage:
    executor = AppiumExecutor(reserved_device, settings)
    machine = StateMachine(executor, detector, run_logger=run_logger)
    machine.navigate_to(ScreenState.MLBB_SHOP_DIAMONDS)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from PIL import Image

from .screen_detector import ScreenDetector, ScreenState
from ..logging.logger import RunLogger, get_logger

logger = get_logger(__name__)

MAX_NAVIGATE_RETRIES = 3
TRANSITION_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 1.5


@dataclass
class Transition:
    """
    A directed edge in the state graph.

    source:  State we must be in to fire this transition.
    target:  State we expect to reach after the action.
    action:  Callable(executor) that performs the UI action.
    label:   Human-readable description for logs.
    """

    source: ScreenState
    target: ScreenState
    action: Callable
    label: str = ""


class NavigationError(Exception):
    """Raised when navigate_to cannot reach the target state."""


class StateMachine:
    """
    Screen-state machine with BFS path planning and retry-guarded transitions.

    Args:
        executor:    AppiumExecutor (passed through to transition actions).
        detector:    ScreenDetector for current-state classification.
        run_logger:  Optional RunLogger for step-level audit trail.
        poll_interval: Seconds between state polls during a transition wait.
        transition_timeout: Max seconds to wait per transition.
    """

    def __init__(
        self,
        executor,
        detector: ScreenDetector,
        run_logger: Optional[RunLogger] = None,
        poll_interval: float = POLL_INTERVAL_S,
        transition_timeout: float = TRANSITION_TIMEOUT_S,
    ) -> None:
        self._executor = executor
        self._detector = detector
        self._run_logger = run_logger
        self._poll_interval = poll_interval
        self._transition_timeout = transition_timeout
        self._transitions: List[Transition] = []
        self._register_transitions()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def navigate_to(
        self,
        target: ScreenState,
        max_retries: int = MAX_NAVIGATE_RETRIES,
    ) -> None:
        """
        Navigate from the current screen to `target`.

        Retries up to max_retries times on transient failures.

        Args:
            target:      The desired ScreenState to reach.
            max_retries: Maximum number of full-path retry attempts.

        Raises:
            NavigationError: If target cannot be reached after all retries.
        """
        for attempt in range(1, max_retries + 1):
            try:
                self._navigate_once(target)
                return
            except NavigationError as exc:
                if attempt == max_retries:
                    raise
                logger.warning(
                    "navigation_retry",
                    target=target.name,
                    attempt=attempt,
                    error=str(exc),
                )
                time.sleep(2.0)

    def current_state(self) -> ScreenState:
        """Take a screenshot and classify the current screen."""
        img = self._screenshot()
        return self._detector.detect(img)

    def add_transition(self, transition: Transition) -> None:
        """Register a custom transition edge (e.g. for test injection)."""
        self._transitions.append(transition)

    # ------------------------------------------------------------------
    # Navigation internals
    # ------------------------------------------------------------------

    def _navigate_once(self, target: ScreenState) -> None:
        current = self.current_state()
        if current == target:
            logger.info("navigation_already_at_target", target=target.name)
            return

        path = self._bfs(current, target)
        if path is None:
            raise NavigationError(
                f"No path from {current.name} to {target.name} in state graph"
            )

        logger.info(
            "navigation_path",
            source=current.name,
            target=target.name,
            steps=[t.label or f"{t.source.name}→{t.target.name}" for t in path],
        )

        for transition in path:
            self._execute_transition(transition)

    def _execute_transition(self, transition: Transition) -> None:
        """Fire a transition action and wait for the target state to appear."""
        label = transition.label or f"{transition.source.name}→{transition.target.name}"
        logger.info("transition_start", label=label)

        if self._run_logger:
            self._run_logger.log_step(
                f"nav_{label}",
                status="started",
                source=transition.source.name,
                target=transition.target.name,
            )

        # Execute the UI action
        try:
            transition.action(self._executor)
        except Exception as exc:
            raise NavigationError(
                f"Transition action '{label}' raised: {exc}"
            ) from exc

        # Wait for the target state to manifest
        deadline = time.monotonic() + self._transition_timeout
        while time.monotonic() < deadline:
            time.sleep(self._poll_interval)
            img = self._screenshot()
            state = self._detector.detect(img)
            if state == transition.target:
                logger.info("transition_complete", label=label, state=state.name)
                if self._run_logger:
                    self._run_logger.log_step(
                        f"nav_{label}",
                        status="ok",
                        reached=state.name,
                    )
                return
            logger.debug(
                "transition_waiting",
                label=label,
                current=state.name,
                expected=transition.target.name,
                remaining_s=round(deadline - time.monotonic(), 1),
            )

        img = self._screenshot()
        actual = self._detector.detect(img)
        raise NavigationError(
            f"Transition '{label}' timed out after {self._transition_timeout}s; "
            f"expected {transition.target.name}, got {actual.name}"
        )

    def _bfs(
        self, source: ScreenState, target: ScreenState
    ) -> Optional[List[Transition]]:
        """Return the shortest path (list of Transition) via BFS, or None."""
        if source == target:
            return []

        edge_map: Dict[ScreenState, List[Transition]] = {}
        for t in self._transitions:
            edge_map.setdefault(t.source, []).append(t)

        queue: deque = deque([(source, [])])
        visited: Set[ScreenState] = {source}

        while queue:
            state, path = queue.popleft()
            for transition in edge_map.get(state, []):
                if transition.target in visited:
                    continue
                new_path = path + [transition]
                if transition.target == target:
                    return new_path
                visited.add(transition.target)
                queue.append((transition.target, new_path))

        return None

    def _screenshot(self) -> Image.Image:
        return self._executor.screenshot()

    # ------------------------------------------------------------------
    # Transition registration
    # ------------------------------------------------------------------

    def _register_transitions(self) -> None:
        """Register all known screen transitions.

        Actions receive the executor as their only argument.
        Coordinates are approximate 1080×1920 reference values;
        adjust via config or template matching in real runs.
        """
        exe = self._executor
        T = Transition

        def _tap(x: int, y: int, label: str = "") -> Callable:
            def action(e):
                e.tap(x, y)
            action.__name__ = label or f"tap_{x}_{y}"
            return action

        def _find_and_tap_text(text: str, fallback_xy: Tuple[int, int]) -> Callable:
            """Try OCR text search first, fall back to coordinates."""
            def action(e):
                from .ocr import OcrEngine
                ocr = OcrEngine()
                img = e.screenshot()
                result = ocr.find_text(img, text, min_confidence=0.5)
                if result:
                    e.tap(result.cx, result.cy)
                else:
                    e.tap(*fallback_xy)
            action.__name__ = f"find_and_tap_{text}"
            return action

        self._transitions = [
            # MLBB loading → main menu (just wait, loading finishes on its own)
            T(
                ScreenState.MLBB_LOADING,
                ScreenState.MLBB_MAIN_MENU,
                action=lambda e: time.sleep(3),
                label="wait_mlbb_loaded",
            ),
            # Main menu → shop (tap shop icon, approximate position)
            T(
                ScreenState.MLBB_MAIN_MENU,
                ScreenState.MLBB_SHOP,
                action=_find_and_tap_text("Shop", fallback_xy=(540, 1750)),
                label="open_shop",
            ),
            # Shop → diamonds section
            T(
                ScreenState.MLBB_SHOP,
                ScreenState.MLBB_SHOP_DIAMONDS,
                action=_find_and_tap_text("Diamonds", fallback_xy=(180, 400)),
                label="open_diamonds",
            ),
            # Diamonds → payment selection (tap smallest pack then Buy)
            T(
                ScreenState.MLBB_SHOP_DIAMONDS,
                ScreenState.MLBB_PAYMENT,
                action=_find_and_tap_text("Buy", fallback_xy=(540, 1700)),
                label="tap_buy",
            ),
            # Payment selection → Google Pay sheet
            T(
                ScreenState.MLBB_PAYMENT,
                ScreenState.GOOGLE_PAY_SHEET,
                action=_find_and_tap_text("Google Pay", fallback_xy=(540, 900)),
                label="select_google_pay",
            ),
            # Google Pay sheet → payment success (tap Pay button)
            T(
                ScreenState.GOOGLE_PAY_SHEET,
                ScreenState.PAYMENT_SUCCESS,
                action=_find_and_tap_text("Pay", fallback_xy=(540, 1700)),
                label="confirm_payment",
            ),
            # Google login → account added (full login handled by scenario steps)
            T(
                ScreenState.GOOGLE_LOGIN,
                ScreenState.GOOGLE_ACCOUNT_ADDED,
                action=lambda e: logger.warning(
                    "google_login_transition_manual",
                    msg="Google login must be handled by the google_account scenario step",
                ),
                label="google_login_placeholder",
            ),
            # UNKNOWN → try going home and re-detect
            T(
                ScreenState.UNKNOWN,
                ScreenState.MLBB_MAIN_MENU,
                action=lambda e: (e.press_key(3), time.sleep(2)),  # Home key
                label="recover_to_main_menu",
            ),
        ]
