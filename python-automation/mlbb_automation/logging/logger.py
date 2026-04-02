"""
Structured JSON logging and run artifact management.

Usage:
    logger = get_logger("module_name")
    logger.info("action_complete", action="tap", x=100, y=200, result="ok")

    run_logger = RunLogger(run_id="run_20240101_120000", log_dir=Path("./run_artifacts"))
    run_logger.log_step("google_account", status="started")
    run_logger.save_screenshot(image, label="after_login")
    run_logger.finalize(success=True)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from PIL import Image


def _configure_structlog(log_level: str = "INFO") -> None:
    """Configure structlog once at startup."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


_configured = False


def get_logger(name: str, log_level: str = "INFO") -> structlog.stdlib.BoundLogger:
    """
    Return a named structlog logger. Configures structlog on first call.

    Args:
        name:      Logger name, typically __name__ of the calling module.
        log_level: Minimum log level (DEBUG/INFO/WARNING/ERROR).
    """
    global _configured
    if not _configured:
        _configure_structlog(log_level)
        _configured = True
    return structlog.get_logger(name)


class RunLogger:
    """
    Manages artifacts for a single automation run:
      - Structured JSON event log
      - Screenshots directory
      - Final JSON summary report
    """

    def __init__(self, run_id: str, log_dir: Path, log_level: str = "INFO") -> None:
        self.run_id = run_id
        self.run_dir = log_dir / run_id
        self.screenshots_dir = self.run_dir / "screenshots"

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self._log_path = self.run_dir / "events.jsonl"
        self._events: list[dict[str, Any]] = []
        self._screenshot_count = 0

        self.logger = get_logger("run_logger", log_level).bind(run_id=run_id)
        self.logger.info("run_started", run_dir=str(self.run_dir))

    # ------------------------------------------------------------------
    # Event logging
    # ------------------------------------------------------------------

    def log_step(
        self,
        step: str,
        status: str,
        *,
        device_id: Optional[str] = None,
        **extra: Any,
    ) -> None:
        """Record a high-level scenario step event."""
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "step",
            "step": step,
            "status": status,
            "device_id": device_id,
            **extra,
        }
        self._append_event(event)
        self.logger.info("step", step=step, status=status, **extra)

    def log_action(
        self,
        action: str,
        *,
        device_id: Optional[str] = None,
        result: str = "ok",
        **params: Any,
    ) -> None:
        """Record a low-level device action (tap, type, swipe, etc.)."""
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "action",
            "action": action,
            "device_id": device_id,
            "result": result,
            **params,
        }
        self._append_event(event)
        self.logger.debug("action", action=action, result=result, **params)

    def log_error(
        self,
        message: str,
        *,
        step: Optional[str] = None,
        exc: Optional[BaseException] = None,
        screenshot: Optional[Image.Image] = None,
        device_id: Optional[str] = None,
    ) -> None:
        """Record an error, optionally saving a screenshot."""
        screenshot_path: Optional[str] = None
        if screenshot is not None:
            screenshot_path = self._save_screenshot_internal(
                screenshot, label=f"error_{step or 'unknown'}"
            )

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "error",
            "message": message,
            "step": step,
            "device_id": device_id,
            "screenshot": screenshot_path,
            "exception": repr(exc) if exc else None,
        }
        self._append_event(event)
        self.logger.error("error", message=message, step=step, exc=repr(exc) if exc else None)

    def save_screenshot(
        self,
        image: Image.Image,
        label: str = "screenshot",
        device_id: Optional[str] = None,
    ) -> str:
        """
        Save a screenshot to the run's screenshots directory.

        Returns:
            Relative path string to the saved file.
        """
        path = self._save_screenshot_internal(image, label)
        self.log_action("screenshot", device_id=device_id, path=path)
        return path

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def finalize(
        self,
        success: bool,
        summary: Optional[dict[str, Any]] = None,
    ) -> Path:
        """
        Write the final JSON report and return its path.

        Args:
            success: Whether the overall run succeeded.
            summary: Extra data to include in the report (e.g. payment result).
        """
        report = {
            "run_id": self.run_id,
            "success": success,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "total_events": len(self._events),
            "screenshots_saved": self._screenshot_count,
            "summary": summary or {},
        }
        report_path = self.run_dir / "report.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        status = "success" if success else "failure"
        self.logger.info("run_finished", status=status, report=str(report_path))
        return report_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_event(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _save_screenshot_internal(self, image: Image.Image, label: str) -> str:
        self._screenshot_count += 1
        ts = datetime.now(timezone.utc).strftime("%H%M%S_%f")[:10]
        filename = f"{self._screenshot_count:03d}_{ts}_{label}.png"
        path = self.screenshots_dir / filename
        image.save(path)
        return str(path.relative_to(self.run_dir))


def make_run_id() -> str:
    """Generate a unique run ID based on current UTC timestamp."""
    return datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")
