"""
Selectel Mobile Farm API client.

API reference (Selectel docs: https://docs.selectel.ru/api/mobile-farm/):
  GET  /devices                  — list devices
  POST /rent/start               — reserve a device
  POST /rent/stop                — release a device

Authentication (per https://docs.selectel.ru/api/authorization/):
  Mobile Farm supports ONLY IAM tokens — NOT static API keys.
  The IAM token is passed in the X-Auth-Token header.

  IAM token is obtained by POSTing service-user credentials to:
    https://cloud.api.selcloud.ru/identity/v3/auth/tokens
  Token TTL: 24 hours. This client auto-refreshes when the token expires.

Required credentials (set via env or config.yaml):
  MLBB_SELECTEL_USERNAME    — service user name (e.g. "user-abc123")
  MLBB_SELECTEL_ACCOUNT_ID  — numeric Selectel account ID
  MLBB_SELECTEL_PASSWORD    — service user password

Usage:
    client = SelectelFarmClient(
        username="user-abc123",
        account_id="12345678",
        password="secret",
        base_url="https://mf.selectel.ru/api/v1",
    )
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

# ---------------------------------------------------------------------------
# IAM token helpers
# ---------------------------------------------------------------------------

_KEYSTONE_URL = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
_TOKEN_TTL_SECONDS = 23 * 3600  # refresh 1 hour before 24h expiry


class _IamTokenProvider:
    """
    Fetches and caches a Selectel IAM token for account scope.

    The token is obtained from the Keystone endpoint and passed as
    X-Auth-Token in every Mobile Farm API request.
    """

    def __init__(self, username: str, account_id: str, password: str) -> None:
        self._username = username
        self._account_id = account_id
        self._password = password
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def get(self) -> str:
        """Return a valid IAM token, refreshing if necessary."""
        if self._token and time.time() < self._expires_at:
            return self._token
        self._refresh()
        assert self._token is not None
        return self._token

    def _refresh(self) -> None:
        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self._username,
                            "domain": {"name": self._account_id},
                            "password": self._password,
                        }
                    },
                },
                "scope": {"domain": {"name": self._account_id}},
            }
        }
        try:
            resp = requests.post(
                _KEYSTONE_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to obtain Selectel IAM token from {_KEYSTONE_URL}: {exc}"
            ) from exc

        token = resp.headers.get("X-Subject-Token")
        if not token:
            raise RuntimeError(
                "Selectel IAM token response did not contain X-Subject-Token header. "
                f"Status: {resp.status_code}, body: {resp.text[:300]}"
            )
        self._token = token
        self._expires_at = time.time() + _TOKEN_TTL_SECONDS
        logger.info(
            "Selectel IAM token refreshed (expires in ~23h). "
            "account_id=%s username=%s",
            self._account_id,
            self._username,
        )


def _mask_proxy(proxy_url: str) -> str:
    """Return proxy URL with password replaced by '***' for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(proxy_url)
        if parsed.password:
            masked = parsed._replace(
                netloc=f"{parsed.username}:***@{parsed.hostname}"
                + (f":{parsed.port}" if parsed.port else "")
            )
            return urlunparse(masked)
    except Exception:
        pass
    return proxy_url


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

    Authentication uses IAM tokens (X-Auth-Token header) as required by
    Selectel documentation. The token is automatically fetched and refreshed
    using service-user credentials against the Keystone endpoint.

    Devices are connected via ADB over TCP (not via a farm-hosted Appium URL).
    After reserving a device the caller receives ``ReservedDevice.adb_host``
    and ``ReservedDevice.adb_port``; ``AppiumExecutor`` uses those to run
    ``adb connect`` before starting the Appium session.

    Args:
        username:          Selectel service user name.
        account_id:        Selectel numeric account ID.
        password:          Selectel service user password.
        base_url:          Base URL of the Mobile Farm API.
        timeout:           HTTP request timeout in seconds.
        appium_url_override: If set, overrides the Appium URL used for sessions.
        proxy_url:         Optional HTTP/SOCKS5 proxy URL.
        adb_host:          Default ADB relay hostname (used when the farm API
                           response does not include an explicit adbHost field).
        local_appium_url:  URL of the locally running Appium server.
    """

    def __init__(
        self,
        username: str,
        account_id: str,
        password: str,
        base_url: str = "https://mf.selectel.ru/api/v1",
        timeout: int = 30,
        appium_url_override: Optional[str] = None,
        proxy_url: Optional[str] = None,
        adb_host: str = "adb.mobfarm.selectel.ru",
        local_appium_url: str = "http://localhost:4723",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._appium_url_override: Optional[str] = appium_url_override
        self._adb_host: str = adb_host
        self._local_appium_url: str = local_appium_url
        self._iam = _IamTokenProvider(username, account_id, password)
        self._session: Session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        if proxy_url:
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})
            logger.info("Selectel client using proxy: %s", _mask_proxy(proxy_url))

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

        # --- Appium URL ---
        # Selectel Farm uses ADB over TCP (not a farm-hosted Appium server).
        # Priority:
        #   1. Explicit override from config (appium_url / MLBB_APPIUM_URL)
        #   2. Local Appium URL from config (local_appium_url, default localhost:4723)
        # The farm-returned appiumUrl is intentionally ignored: it is either absent
        # or not accessible from outside Russia.
        appium_url: str = self._appium_url_override or self._local_appium_url
        if self._appium_url_override:
            logger.info(
                "Using appium_url override from config: %s", appium_url,
            )

        # --- ADB connection info ---
        # The farm returns the TCP port for adb connect.
        # Key names vary across Selectel API versions; try all known names.
        adb_port_raw: Optional[int] = (
            data.get("adbPort")
            or data.get("adb_port")
            or data.get("port")
        )
        adb_port: Optional[int] = int(adb_port_raw) if adb_port_raw else None

        adb_host: Optional[str] = (
            data.get("adbHost")
            or data.get("adb_host")
            or (self._adb_host if adb_port else None)
        )
        if adb_host and adb_port:
            logger.info("ADB endpoint from farm: %s:%s", adb_host, adb_port)
        else:
            logger.warning(
                "Farm response missing ADB port info — "
                "ADB connect will be skipped. Response keys: %s",
                list(data.keys()),
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
            adb_host=adb_host,
            adb_port=adb_port,
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
                # Inject a fresh (or cached) IAM token on every request.
                # The provider refreshes automatically when within 1h of expiry.
                iam_token = self._iam.get()
                self._session.headers["X-Auth-Token"] = iam_token
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

    Uses IAM token auth (X-Auth-Token) as required by Selectel's Mobile Farm API.
    Credentials are taken from settings: selectel_username / selectel_account_id /
    selectel_password.
    """
    return SelectelFarmClient(
        username=settings.selectel_username,
        account_id=settings.selectel_account_id,
        password=settings.selectel_password,
        base_url=settings.selectel_api_url,
        timeout=settings.action_timeout_seconds,
        appium_url_override=settings.appium_url,
        proxy_url=settings.proxy_url,
        adb_host=settings.adb_host,
        local_appium_url=settings.local_appium_url,
    )
