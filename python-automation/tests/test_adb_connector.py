"""
Tests for AdbConnector.

All subprocess calls are mocked — no real adb binary or network required.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from mlbb_automation.device_farm.adb_connector import AdbConnector, AdbError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    r = subprocess.CompletedProcess(args=[], returncode=returncode)
    r.stdout = stdout
    r.stderr = ""
    return r


# ---------------------------------------------------------------------------
# ADB binary discovery
# ---------------------------------------------------------------------------


def test_find_adb_raises_when_not_in_path():
    with patch("shutil.which", return_value=None):
        with pytest.raises(AdbError, match="adb binary not found"):
            AdbConnector._find_adb()


def test_find_adb_returns_path_when_found():
    with patch("shutil.which", return_value="/usr/bin/adb"):
        result = AdbConnector._find_adb()
    assert result == "/usr/bin/adb"


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def test_ensure_key_creates_files(tmp_path):
    key_path = tmp_path / ".android" / "adbkey"

    with patch("shutil.which", return_value="/usr/bin/adb"):
        connector = AdbConnector(key_path=key_path, adb_bin="/usr/bin/adb")

    with patch.object(connector, "_run") as mock_run:
        mock_run.return_value = _make_completed()

        connector.ensure_key()

        # Should have called keygen because key doesn't exist
        calls = mock_run.call_args_list
        assert any("keygen" in str(c) for c in calls), (
            f"Expected keygen call, got: {calls}"
        )
        # Parent directory should be created
        assert key_path.parent.exists()


def test_ensure_key_skips_generation_if_exists(tmp_path):
    key_path = tmp_path / "adbkey"
    key_path.write_text("fake_private_key")

    connector = AdbConnector(key_path=key_path, adb_bin="/usr/bin/adb")

    with patch.object(connector, "_run") as mock_run:
        mock_run.return_value = _make_completed()

        connector.ensure_key()

        # keygen should NOT be called because key already exists
        keygen_calls = [c for c in mock_run.call_args_list if "keygen" in str(c)]
        assert not keygen_calls, f"Unexpected keygen call: {keygen_calls}"


def test_get_public_key_returns_content(tmp_path):
    key_path = tmp_path / "adbkey"
    pub_path = tmp_path / "adbkey.pub"

    pub_key_content = "QAAAAK9+RVz/abcdefghij1234567890== replit@mlbb-automation"
    pub_path.write_text(pub_key_content + "\n")

    connector = AdbConnector(key_path=key_path, adb_bin="/usr/bin/adb")
    result = connector.get_public_key()

    assert result == pub_key_content


def test_get_public_key_raises_if_missing(tmp_path):
    key_path = tmp_path / "adbkey"
    connector = AdbConnector(key_path=key_path, adb_bin="/usr/bin/adb")

    with pytest.raises(AdbError, match="not found"):
        connector.get_public_key()


def test_get_public_key_raises_if_empty(tmp_path):
    key_path = tmp_path / "adbkey"
    pub_path = tmp_path / "adbkey.pub"
    pub_path.write_text("  \n")

    connector = AdbConnector(key_path=key_path, adb_bin="/usr/bin/adb")

    with pytest.raises(AdbError, match="empty"):
        connector.get_public_key()


def test_is_key_valid_true(tmp_path):
    key_path = tmp_path / "adbkey"
    pub_path = tmp_path / "adbkey.pub"
    key_path.write_text("private_key_data")
    pub_path.write_text("QAAAAK9+RVz/test== user@host")

    connector = AdbConnector(key_path=key_path, adb_bin="/usr/bin/adb")
    assert connector.is_key_valid() is True


def test_is_key_valid_false_if_no_files(tmp_path):
    key_path = tmp_path / "adbkey"
    connector = AdbConnector(key_path=key_path, adb_bin="/usr/bin/adb")
    assert connector.is_key_valid() is False


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_returns_serial_on_success(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    def mock_run(args, **kwargs):
        if args[0] == "connect":
            return _make_completed("connected to adb.mobfarm.selectel.ru:4022")
        if args[0] == "devices":
            return _make_completed(
                "List of devices attached\n"
                "adb.mobfarm.selectel.ru:4022\tdevice\n"
            )
        return _make_completed()

    with patch.object(connector, "_run", side_effect=mock_run):
        serial = connector.connect("adb.mobfarm.selectel.ru", 4022)

    assert serial == "adb.mobfarm.selectel.ru:4022"


def test_connect_raises_on_cannot_connect(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    with patch.object(connector, "_run") as mock_run:
        mock_run.return_value = _make_completed(
            "cannot connect to adb.mobfarm.selectel.ru:9999: Connection refused"
        )
        with pytest.raises(AdbError, match="cannot connect|failed"):
            connector.connect("adb.mobfarm.selectel.ru", 9999, timeout=0)


def test_connect_raises_on_timeout(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    def mock_run(args, **kwargs):
        if args[0] == "connect":
            return _make_completed("connecting to adb.mobfarm.selectel.ru:4022")
        if args[0] == "devices":
            return _make_completed("List of devices attached\n")
        return _make_completed()

    with patch.object(connector, "_run", side_effect=mock_run):
        with pytest.raises(AdbError, match="not ready after"):
            connector.connect("adb.mobfarm.selectel.ru", 4022, timeout=0)


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


def test_disconnect_calls_adb(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    with patch.object(connector, "_run") as mock_run:
        mock_run.return_value = _make_completed("disconnected adb.mobfarm.selectel.ru:4022")
        connector.disconnect("adb.mobfarm.selectel.ru:4022")

    mock_run.assert_called_once_with(["disconnect", "adb.mobfarm.selectel.ru:4022"])


def test_disconnect_does_not_raise_on_failure(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    with patch.object(connector, "_run", side_effect=AdbError("some failure")):
        connector.disconnect("adb.mobfarm.selectel.ru:4022")  # must not raise


# ---------------------------------------------------------------------------
# list_connected()
# ---------------------------------------------------------------------------


def test_list_connected_parses_output(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    raw = textwrap.dedent("""\
        List of devices attached
        adb.mobfarm.selectel.ru:4022\tdevice
        192.168.1.10:5555\toffline
        emulator-5554\tdevice
    """)

    with patch.object(connector, "_run", return_value=_make_completed(raw)):
        devices = connector.list_connected()

    # Only lines with "device" state should be included
    assert "adb.mobfarm.selectel.ru:4022" in devices
    assert "emulator-5554" in devices
    assert "192.168.1.10:5555" not in devices  # offline


def test_list_connected_returns_empty_when_none(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    with patch.object(connector, "_run", return_value=_make_completed("List of devices attached\n")):
        devices = connector.list_connected()

    assert devices == []


# ---------------------------------------------------------------------------
# _run() error handling
# ---------------------------------------------------------------------------


def test_run_raises_adb_error_on_nonzero_exit(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/usr/bin/adb")

    with patch("subprocess.run") as mock_subproc:
        cp = subprocess.CompletedProcess(args=[], returncode=1)
        cp.stdout = ""
        cp.stderr = "error: something went wrong"
        mock_subproc.return_value = cp

        with pytest.raises(AdbError):
            connector._run(["devices"], capture=True, check=True)


def test_run_raises_adb_error_on_file_not_found(tmp_path):
    connector = AdbConnector(key_path=tmp_path / "adbkey", adb_bin="/nonexistent/adb")

    with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
        with pytest.raises(AdbError, match="binary not found"):
            connector._run(["devices"])
