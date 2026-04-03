"""
ADB connector for Selectel Mobile Farm.

Selectel devices are accessed via ADB over TCP:
    adb connect adb.mobfarm.selectel.ru:<port>

The public ADB key (QAAAA... format) must be registered in Selectel:
    Control Panel → Account → Access → ADB Keys

Usage:
    connector = AdbConnector()
    connector.ensure_key()
    print(connector.get_public_key())          # paste into Selectel panel
    serial = connector.connect("adb.mobfarm.selectel.ru", 4022)
    # ... do work via Appium with udid=serial ...
    connector.disconnect(serial)
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..logging.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_KEY_PATH = Path.home() / ".android" / "adbkey"
_CONNECT_TIMEOUT = 15  # seconds to wait for adb connect
_CONNECT_POLL_INTERVAL = 0.5


class AdbError(RuntimeError):
    """Raised when an ADB command fails unexpectedly."""


class AdbConnector:
    """
    Thin wrapper around the ``adb`` binary for managing device connections
    to Selectel Mobile Farm.

    Args:
        key_path: Path to the ADB private key (default: ``~/.android/adbkey``).
                  The public key is stored at ``<key_path>.pub``.
        adb_bin:  Path to the ``adb`` binary.  Resolved from ``$PATH`` by default.
    """

    def __init__(
        self,
        key_path: Optional[Path] = None,
        adb_bin: Optional[str] = None,
    ) -> None:
        self._key_path: Path = Path(key_path) if key_path else _DEFAULT_KEY_PATH
        self._adb_bin: str = adb_bin or self._find_adb()

    # ------------------------------------------------------------------
    # ADB binary discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_adb() -> str:
        """Locate the adb binary; raise AdbError if not found."""
        found = shutil.which("adb")
        if found:
            return found
        raise AdbError(
            "adb binary not found in PATH. "
            "Install android-tools via Nix or add adb to your PATH."
        )

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    def ensure_key(self) -> None:
        """
        Ensure ``~/.android/adbkey`` and ``~/.android/adbkey.pub`` exist.

        Generates a new RSA key pair if the private key is absent.
        The ``.pub`` file is also generated if missing (e.g. after manual import).
        """
        self._key_path.parent.mkdir(parents=True, exist_ok=True)

        if not self._key_path.exists():
            logger.info("Generating ADB key pair at %s", self._key_path)
            self._run(["keygen", str(self._key_path)])
            logger.info("ADB key pair generated")
        else:
            logger.debug("ADB private key already exists at %s", self._key_path)

        pub_path = Path(str(self._key_path) + ".pub")
        if not pub_path.exists():
            logger.info("Generating ADB public key at %s", pub_path)
            self._run(["pubkey", str(self._key_path)])

    def get_public_key(self) -> str:
        """
        Return the ADB RSA public key string (QAAAA... format).

        The key must be registered in the Selectel control panel before ADB
        connections will be accepted:
            Control Panel → Account → Access → ADB Keys

        Raises:
            AdbError: If the public key file does not exist.
                      Call ``ensure_key()`` first.
        """
        pub_path = Path(str(self._key_path) + ".pub")
        if not pub_path.exists():
            raise AdbError(
                f"ADB public key not found at {pub_path}. "
                "Run ensure_key() to generate it."
            )
        content = pub_path.read_text(encoding="utf-8").strip()
        if not content:
            raise AdbError(f"ADB public key file is empty: {pub_path}")
        return content

    def is_key_valid(self) -> bool:
        """Return True if both key files exist and the public key starts with QAAAA."""
        pub_path = Path(str(self._key_path) + ".pub")
        if not self._key_path.exists() or not pub_path.exists():
            return False
        try:
            content = pub_path.read_text(encoding="utf-8").strip()
            return content.startswith("QAAAA")
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Device connection
    # ------------------------------------------------------------------

    def connect(self, host: str, port: int, timeout: int = _CONNECT_TIMEOUT) -> str:
        """
        Connect to a device at ``host:port`` via ADB over TCP.

        Retries until the device appears in ``adb devices`` or timeout is reached.

        Args:
            host:    Hostname or IP, e.g. ``adb.mobfarm.selectel.ru``.
            port:    TCP port from the Selectel rent/start response.
            timeout: Maximum seconds to wait for the connection.

        Returns:
            Device serial string, e.g. ``adb.mobfarm.selectel.ru:4022``.

        Raises:
            AdbError: If connection fails within the timeout.
        """
        serial = f"{host}:{port}"
        logger.info("ADB connecting to %s", serial)

        result = self._run(["connect", serial], capture=True)
        output = (result.stdout or "").strip()
        logger.debug("adb connect output: %s", output)

        if "cannot connect" in output.lower() or "failed" in output.lower():
            raise AdbError(f"adb connect {serial} failed: {output}")

        # Wait until the device is listed as connected
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._is_device_connected(serial):
                logger.info("ADB device connected: %s", serial)
                return serial
            time.sleep(_CONNECT_POLL_INTERVAL)

        # Last-chance check
        if self._is_device_connected(serial):
            return serial

        raise AdbError(
            f"adb connect {serial}: device not ready after {timeout}s. "
            f"Last adb devices output: {self._list_devices_raw()}"
        )

    def disconnect(self, serial: str) -> None:
        """
        Disconnect from the device with the given serial.

        Args:
            serial: Device serial returned by ``connect()``.
        """
        logger.info("ADB disconnecting from %s", serial)
        try:
            self._run(["disconnect", serial])
        except Exception as exc:
            logger.warning("adb disconnect %s failed (non-fatal): %s", serial, exc)

    def list_connected(self) -> list[str]:
        """
        Return serials of all currently connected (online) devices.

        Parses ``adb devices`` and filters to lines with ``device`` state
        (excludes ``offline``, ``unauthorized``, etc.).
        """
        raw = self._list_devices_raw()
        serials: list[str] = []
        for line in raw.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_device_connected(self, serial: str) -> bool:
        """Return True if serial appears in ``adb devices`` as 'device'."""
        return serial in self.list_connected()

    def _list_devices_raw(self) -> str:
        """Run ``adb devices`` and return the raw stdout."""
        result = self._run(["devices"], capture=True)
        return result.stdout or ""

    def _run(
        self,
        args: list[str],
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """
        Execute an adb subcommand.

        Args:
            args:    Arguments after ``adb`` (e.g. ``["connect", "host:port"]``).
            capture: If True, capture stdout/stderr and return them.
            check:   If True, raise AdbError on non-zero exit code.

        Returns:
            ``subprocess.CompletedProcess`` instance.

        Raises:
            AdbError: On non-zero exit when ``check=True``.
        """
        cmd = [self._adb_bin] + args
        logger.debug("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb command timed out: {' '.join(cmd)}") from exc
        except FileNotFoundError as exc:
            raise AdbError(
                f"adb binary not found: {self._adb_bin}. "
                "Install android-tools via Nix."
            ) from exc

        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            raise AdbError(
                f"adb {args[0]} failed (exit {result.returncode}): "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        return result

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"AdbConnector(key_path={self._key_path!r}, adb_bin={self._adb_bin!r})"
