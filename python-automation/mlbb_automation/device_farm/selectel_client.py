"""
Selectel Mobile Farm API client.

API reference (based on Selectel's documented endpoints):
  GET  /devices                  — list devices
  POST /rent/start               — reserve a device
  POST /rent/stop                — release a device

Authentication: Bearer token via Authorization header.

Assumption: The farm returns an Appium/WebDriver URL and capabilities
in the reservation response. If the actual endpoint paths differ, update
the constants at the top of this file.

Usage:
    client = SelectelFarmClient(api_key="...", base_url="https://mf.selectel.ru/api/v1")
    reserved = client.acquire_device(platform_version="12")
    # ... run Appium session ...
    client.release_device(reserved)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests
from requests import Response, Session

from .base import DeviceFarmClient, DeviceInfo, ReservedDevice

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configurable endpoint paths (adjust if Selectel changes their API)
# --------------------------------------------------------------------------
_ENDPOINT_LIST_DEVICES = "/devices"
_ENDPOINT_RENT_START = "/rent/start"
_ENDPOINT_RENT_STOP = "/rent/stop"

# Default Appium capabilities injected for every session
_DEFAULT_CAPABILITIES: dict = {
    "platformName": "Android",
    "automationName": "UiAutomator2",
    "newCommandTimeout": 300,
    "noReset": False,
    "fullReset": False,
}


class SelectelFarmClient(DeviceFarmClient):
    """
    HTTP client for the Selectel Mobile Farm REST API.

    Args:
        api_key:  Selectel API key (Bearer token).
        base_url: Base URL of the farm API.
        timeout:  HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://mf.selectel.ru/api/v1",
        timeout: int = 30,
        appium_url_override: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # When set, this URL always overrides whatever the farm API returns.
        self._appium_url_override: Optional[str] = appium_url_override
        self._session: Session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # DeviceFarmClient interface
    # ------------------------------------------------------------------

    def list_devices(
        self,
        platform_version: Optional[str] = None,
        model: Optional[str] = None,
    ) -> list[DeviceInfo]:
        """Return available Android devices, optionally filtered."""
        resp = self._get(_ENDPOINT_LIST_DEVICES)
        raw_devices: list[dict] = resp.json() if isinstance(resp.json(), list) else resp.json().get("devices", [])

        devices: list[DeviceInfo] = []
        for raw in raw_devices:
            info = self._parse_device(raw)
            if info.platform.lower() != "android":
                continue
            if info.status.lower() not in ("available", "free", "online"):
                continue
            if platform_version and info.platform_version != platform_version:
                continue
            if model and model.lower() not in info.model.lower():
                continue
            devices.append(info)

        logger.info("list_devices returned %d available devices", len(devices))
        return devices

    def acquire_device_by_id(self, device_id: str) -> ReservedDevice:
        """
        Reserve a specific device by its ID.

        Args:
            device_id: Exact device ID as returned by list_devices().

        Returns:
            ReservedDevice ready for an Appium session.
        """
        # Fetch all devices (regardless of status) to resolve by ID
        all_devices = self._list_all_devices()
        target = next((d for d in all_devices if d.id == device_id), None)
        if target is None:
            raise RuntimeError(f"Device '{device_id}' not found in farm")

        return self._reserve_device(target)

    def _list_all_devices(self) -> list[DeviceInfo]:
        """Return all devices regardless of status (for lookup by ID)."""
        resp = self._get(_ENDPOINT_LIST_DEVICES)
        raw_devices: list[dict] = (
            resp.json() if isinstance(resp.json(), list) else resp.json().get("devices", [])
        )
        return [self._parse_device(raw) for raw in raw_devices]

    def acquire_device(
        self,
        platform_version: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ReservedDevice:
        """Reserve the first available device matching the filters."""
        devices = self.list_devices(platform_version=platform_version, model=model)
        if not devices:
            raise RuntimeError(
                f"No available Android devices found "
                f"(platform_version={platform_version!r}, model={model!r})"
            )

        target = devices[0]
        return self._reserve_device(target)

    def _reserve_device(self, target: DeviceInfo) -> ReservedDevice:
        """Issue the rent/start API call and build a ReservedDevice."""
        logger.info("Reserving device id=%s model=%s", target.id, target.model)

        payload = {"deviceId": target.id}
        resp = self._post(_ENDPOINT_RENT_START, json=payload)
        data: dict = resp.json()

        # Parse Appium URL and capabilities from the response.
        # Selectel typically returns something like:
        #   { "appiumUrl": "https://...", "capabilities": {...}, "sessionId": "..." }
        # Priority: config override > farm response > URL-based fallback
        farm_appium_url: str = (
            data.get("appiumUrl")
            or data.get("appium_url")
            or data.get("url")
            or f"{self._base_url}/wd/hub"  # fallback guess
        )
        appium_url: str = self._appium_url_override or farm_appium_url
        if self._appium_url_override:
            logger.info(
                "Using appium_url override from config (ignoring farm response): %s",
                appium_url,
            )

        raw_caps: dict = data.get("capabilities") or data.get("desiredCapabilities") or {}
        capabilities = {
            **_DEFAULT_CAPABILITIES,
            "deviceName": target.name,
            "platformVersion": target.platform_version,
            **({"udid": target.udid} if target.udid else {}),
            **raw_caps,
        }

        session_id: Optional[str] = data.get("sessionId") or data.get("id") or data.get("rentId")

        reserved = ReservedDevice(
            device_info=target,
            appium_url=appium_url,
            capabilities=capabilities,
            session_id=session_id,
        )
        logger.info(
            "Device reserved: id=%s, appium_url=%s, session_id=%s",
            target.id,
            appium_url,
            session_id,
        )
        return reserved

    def release_device(self, reserved: ReservedDevice) -> None:
        """Release the reserved device back to the farm."""
        payload: dict = {"deviceId": reserved.device_info.id}
        if reserved.session_id:
            payload["sessionId"] = reserved.session_id

        try:
            self._post(_ENDPOINT_RENT_STOP, json=payload)
            logger.info("Device released: id=%s", reserved.device_info.id)
        except Exception as exc:
            # Log but don't re-raise — device release should never crash the caller.
            logger.warning("Failed to release device id=%s: %s", reserved.device_info.id, exc)

    # ------------------------------------------------------------------
    # Internal HTTP helpers (with retry on transient errors)
    # ------------------------------------------------------------------

    #: HTTP status codes that are safe to retry (server-side transient errors)
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
    #: Number of automatic retries for transient HTTP errors
    _HTTP_RETRIES = 3
    #: Initial back-off in seconds (doubles on each attempt)
    _HTTP_RETRY_DELAY = 1.0

    def _get(self, path: str, **kwargs) -> Response:
        return self._request("GET", path, **kwargs)

    def _post(self, path: str, **kwargs) -> Response:
        return self._request("POST", path, **kwargs)

    def _request(self, method: str, path: str, **kwargs) -> Response:
        """
        Execute an HTTP request with automatic retry on transient errors.

        Retries on:
          - Connection/read timeouts (``requests.Timeout``)
          - Connection errors (``requests.ConnectionError``)
          - HTTP 429, 5xx responses (rate-limit or server errors)

        Args:
            method: HTTP method string ("GET" or "POST").
            path:   API path relative to base_url.

        Returns:
            The successful ``Response`` object.

        Raises:
            RuntimeError: After all retries are exhausted.
        """
        import time as _time

        url = self._base_url + path
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._HTTP_RETRIES + 1):
            try:
                resp = self._session.request(
                    method, url, timeout=self._timeout, **kwargs
                )
                if resp.status_code in self._RETRYABLE_STATUS and attempt < self._HTTP_RETRIES:
                    delay = self._HTTP_RETRY_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "Selectel API transient error, retrying: %s %s status=%s attempt=%d retry_in=%.1fs",
                        method, path, resp.status_code, attempt, delay,
                    )
                    _time.sleep(delay)
                    continue
                self._raise_for_status(resp)
                return resp
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                if attempt < self._HTTP_RETRIES:
                    delay = self._HTTP_RETRY_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "Selectel API network error, retrying: %s %s error=%s attempt=%d retry_in=%.1fs",
                        method, path, exc, attempt, delay,
                    )
                    _time.sleep(delay)
                else:
                    raise RuntimeError(
                        f"Selectel API {method} {path} failed after "
                        f"{self._HTTP_RETRIES} retries: {exc}"
                    ) from exc

        raise RuntimeError(
            f"Selectel API {method} {path} failed after {self._HTTP_RETRIES} retries"
        )

    @staticmethod
    def _raise_for_status(resp: Response) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = resp.text[:500]
            raise RuntimeError(
                f"Selectel API error {resp.status_code}: {body}"
            ) from exc

    @staticmethod
    def _parse_device(raw: dict) -> DeviceInfo:
        """Convert raw API response dict to DeviceInfo."""
        return DeviceInfo(
            id=str(raw.get("id") or raw.get("deviceId") or ""),
            name=raw.get("name") or raw.get("deviceName") or raw.get("model") or "unknown",
            platform=raw.get("platform") or raw.get("os") or "Android",
            platform_version=str(raw.get("platformVersion") or raw.get("os_version") or ""),
            model=raw.get("model") or raw.get("deviceModel") or raw.get("name") or "unknown",
            status=raw.get("status") or raw.get("state") or "unknown",
            udid=raw.get("udid") or raw.get("serial"),
            raw=raw,
        )


def create_client_from_settings(settings) -> SelectelFarmClient:
    """
    Convenience factory — creates a SelectelFarmClient from a Settings object.

    Propagates settings.appium_url as an override so any Appium URL returned
    by the farm is replaced with the value from configuration.
    """
    return SelectelFarmClient(
        api_key=settings.selectel_api_key,
        base_url=settings.selectel_api_url,
        timeout=settings.action_timeout_seconds,
        appium_url_override=settings.appium_url,  # None → no override
    )
