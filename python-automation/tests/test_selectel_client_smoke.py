"""
Smoke tests for SelectelFarmClient using mocked HTTP responses.

Covers the list→rent/start→rent/stop contract and appium_url resolution
without requiring a real Selectel account or network access.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mlbb_automation.device_farm.base import DeviceInfo, ReservedDevice
from mlbb_automation.device_farm.selectel_client import SelectelFarmClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(data: Any, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(data)
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


DEVICE_RAW = {
    "id": "dev-001",
    "name": "Pixel 7",
    "model": "Pixel 7",
    "platform": "Android",
    "platformVersion": "13",
    "status": "available",
    "udid": "emulator-5554",
}

RENT_START_RESPONSE = {
    "appiumUrl": "https://farm.example.com/wd/hub",
    "capabilities": {"automationName": "UiAutomator2"},
    "sessionId": "rent-xyz",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListDevices:
    def _client(self, list_response) -> SelectelFarmClient:
        client = SelectelFarmClient(api_key="test-key")
        client._session = MagicMock()
        client._session.get.return_value = _mock_response(list_response)
        return client

    def test_returns_available_android_devices(self):
        client = self._client([DEVICE_RAW])
        devices = client.list_devices()
        assert len(devices) == 1
        assert devices[0].id == "dev-001"
        assert devices[0].model == "Pixel 7"
        assert devices[0].platform_version == "13"

    def test_filters_out_non_android(self):
        ios_device = {**DEVICE_RAW, "id": "ios-001", "platform": "iOS"}
        client = self._client([DEVICE_RAW, ios_device])
        devices = client.list_devices()
        assert len(devices) == 1
        assert devices[0].id == "dev-001"

    def test_filters_out_busy_devices(self):
        busy = {**DEVICE_RAW, "id": "busy-001", "status": "busy"}
        client = self._client([busy])
        devices = client.list_devices()
        assert devices == []

    def test_filters_by_platform_version(self):
        other = {**DEVICE_RAW, "id": "dev-002", "platformVersion": "12"}
        client = self._client([DEVICE_RAW, other])
        devices = client.list_devices(platform_version="13")
        assert len(devices) == 1
        assert devices[0].id == "dev-001"

    def test_filters_by_model_substring(self):
        s21 = {**DEVICE_RAW, "id": "s21-001", "model": "Samsung Galaxy S21"}
        client = self._client([DEVICE_RAW, s21])
        devices = client.list_devices(model="Samsung")
        assert len(devices) == 1
        assert devices[0].id == "s21-001"

    def test_accepts_devices_key_in_response(self):
        client = self._client({"devices": [DEVICE_RAW]})
        devices = client.list_devices()
        assert len(devices) == 1


class TestAcquireDevice:
    def _client(self) -> SelectelFarmClient:
        client = SelectelFarmClient(api_key="test-key")
        session = MagicMock()
        session.get.return_value = _mock_response([DEVICE_RAW])
        session.post.return_value = _mock_response(RENT_START_RESPONSE)
        client._session = session
        return client

    def test_acquire_device_returns_reserved_device(self):
        client = self._client()
        reserved = client.acquire_device()
        assert isinstance(reserved, ReservedDevice)
        assert reserved.device_info.id == "dev-001"
        assert reserved.appium_url == "https://farm.example.com/wd/hub"
        assert reserved.session_id == "rent-xyz"

    def test_acquire_device_raises_if_no_devices(self):
        client = SelectelFarmClient(api_key="test-key")
        client._session = MagicMock()
        client._session.get.return_value = _mock_response([])
        with pytest.raises(RuntimeError, match="No available Android devices"):
            client.acquire_device()

    def test_appium_url_override_takes_precedence(self):
        client = SelectelFarmClient(
            api_key="test-key",
            appium_url_override="https://my-custom-appium/wd/hub",
        )
        session = MagicMock()
        session.get.return_value = _mock_response([DEVICE_RAW])
        session.post.return_value = _mock_response(RENT_START_RESPONSE)
        client._session = session

        reserved = client.acquire_device()
        # Override must win over the farm-returned appiumUrl
        assert reserved.appium_url == "https://my-custom-appium/wd/hub"

    def test_capabilities_merged_with_defaults(self):
        client = self._client()
        reserved = client.acquire_device()
        caps = reserved.capabilities
        # Default caps include automationName
        assert "automationName" in caps
        # Farm extras are merged in
        assert caps["automationName"] in ("UiAutomator2", "uiautomator2")
        # Device name and version populated
        assert "deviceName" in caps
        assert "platformVersion" in caps


class TestAcquireDeviceById:
    def _client(self) -> SelectelFarmClient:
        client = SelectelFarmClient(api_key="test-key")
        session = MagicMock()
        session.get.return_value = _mock_response([DEVICE_RAW])
        session.post.return_value = _mock_response(RENT_START_RESPONSE)
        client._session = session
        return client

    def test_acquire_by_existing_id(self):
        client = self._client()
        reserved = client.acquire_device_by_id("dev-001")
        assert reserved.device_info.id == "dev-001"

    def test_acquire_by_missing_id_raises(self):
        client = self._client()
        with pytest.raises(RuntimeError, match="not found in farm"):
            client.acquire_device_by_id("nonexistent-id")


class TestReleaseDevice:
    def test_release_calls_rent_stop(self):
        client = SelectelFarmClient(api_key="test-key")
        session = MagicMock()
        session.post.return_value = _mock_response({"status": "ok"})
        client._session = session

        reserved = ReservedDevice(
            device_info=DeviceInfo(
                id="dev-001", name="Pixel 7", platform="Android",
                platform_version="13", model="Pixel 7", status="rented",
            ),
            appium_url="https://farm.example.com/wd/hub",
            capabilities={},
            session_id="rent-xyz",
        )
        client.release_device(reserved)
        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert "rent/stop" in call_kwargs[0][0] or "stop" in str(call_kwargs)

    def test_release_does_not_raise_on_http_error(self):
        client = SelectelFarmClient(api_key="test-key")
        session = MagicMock()
        error_resp = _mock_response({"error": "gone"}, status_code=404)
        error_resp.raise_for_status.side_effect = Exception("404")
        session.post.return_value = error_resp
        client._session = session

        reserved = ReservedDevice(
            device_info=DeviceInfo(
                id="dev-001", name="Pixel 7", platform="Android",
                platform_version="13", model="Pixel 7", status="rented",
            ),
            appium_url="https://farm.example.com/wd/hub",
            capabilities={},
            session_id=None,
        )
        # Should not raise — release failures are swallowed and logged
        client.release_device(reserved)
