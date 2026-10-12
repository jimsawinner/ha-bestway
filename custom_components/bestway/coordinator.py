"""Data update coordinator for the Bestway API."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from logging import getLogger
from time import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .backend import BackendApi
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .features import features_for
from .model import BestwayApiResults, BestwayDeviceType
from .smartspa.api import SmartSpaAuthException
from .websocket_base import BaseWebSocketClient

_LOGGER = getLogger(__name__)


class BestwayUpdateCoordinator(DataUpdateCoordinator[BestwayApiResults]):
    """Update coordinator that polls the device status for all devices in an account."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: BackendApi,
    ) -> None:
        """Initialize my coordinator."""
        # The interval polled at whenever no WebSocket is delivering updates.
        self._poll_interval = timedelta(
            seconds=int(
                config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            )
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Bestway API",
            update_interval=self._poll_interval,
        )
        self.api = api
        self._cancel_confirm_refreshes: list[Callable[[], None]] = []
        self._ws_last_update: dict[str, float] = {}  # Track WebSocket update times
        self._logged_inventory: frozenset[tuple[str, BestwayDeviceType]] = frozenset()
        # One entry per backend connection: a single Gizwits socket, or one
        # AWS IoT socket per device.
        self.websockets: list[BaseWebSocketClient] = []

    async def _async_update_data(self) -> BestwayApiResults:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        # 30s (was 10s): the SmartSpa backend may transparently re-login and
        # retry inside a poll cycle, and 10s was already reported as too tight
        # for slow paths to Bestway's cloud (upstream PR #137).
        try:
            async with asyncio.timeout(30):
                await self.api.refresh_bindings()
                self._log_device_inventory()
                return await self.api.fetch_data()
        except SmartSpaAuthException as err:
            # Re-login already failed inside the API; the stored credentials no
            # longer work. A re-auth config flow is out of scope for now, but HA
            # should at least surface this as an auth problem rather than a
            # generic update failure.
            raise ConfigEntryAuthFailed(str(err)) from err

    def _log_device_inventory(self) -> None:
        """Log each discovered device and the entity feature set it resolves to.

        `refresh_bindings()` runs every poll, so this only speaks up when the
        set of devices or their derived types changes. `device_type` is derived
        rather than reported (see `BestwayDevice.device_type`) and is the sole
        input to `features_for()`, so a device landing on an unexpected type is
        the first thing to check when entities are missing.
        """
        inventory = frozenset(
            (device_id, device.device_type)
            for device_id, device in self.api.devices.items()
        )
        if inventory == self._logged_inventory:
            return
        self._logged_inventory = inventory

        options = self.config_entry.options if self.config_entry else {}
        for device_id, device in self.api.devices.items():
            _LOGGER.info(
                "Device %s (%s): type=%s, protocol=v%d, backend=%s",
                device_id,
                device.alias,
                device.device_type.name,
                device.protocol_version,
                device.backend,
            )
            _LOGGER.debug(
                "Features for %s: %s", device_id, features_for(device, options)
            )

    def handle_websocket_update(self, device_id: str, attrs: dict[str, Any]) -> None:
        """Handle a real-time device update from a WebSocket.

        Delegates the merge-and-translate work to the backend, which owns
        the raw state cache these deltas are partial updates against, then
        triggers immediate entity updates. This provides sub-second update
        latency compared to 30-second polling.
        """
        _LOGGER.debug(
            "WebSocket update for device %s with %d attributes", device_id, len(attrs)
        )

        results = self.api.handle_partial_update(device_id, attrs)

        # Track last WebSocket update time for this device
        self._ws_last_update[device_id] = time()

        # Trigger immediate entity updates
        self.async_set_updated_data(results)

    async def async_refresh_after_command(self) -> None:
        """Refresh after a control write, and keep re-polling until the
        backend will have applied it.

        A backend whose writes take a while to land (command_settle_s) would
        otherwise only show the result at the next scheduled poll: the
        immediate refresh still reads the old state.
        """
        await self.async_request_refresh()

        settle = self.api.command_settle_s
        if settle <= 0:
            return
        # A newer command supersedes the confirmation polls of an older one.
        self._cancel_pending_confirm_refreshes()
        self._cancel_confirm_refreshes = [
            async_call_later(self.hass, delay, self._async_confirm_refresh)
            for delay in (settle, settle * 2)
        ]

    async def _async_confirm_refresh(self, _now: datetime) -> None:
        await self.async_request_refresh()

    def _cancel_pending_confirm_refreshes(self) -> None:
        for cancel in self._cancel_confirm_refreshes:
            cancel()
        self._cancel_confirm_refreshes = []

    async def async_shutdown(self) -> None:
        """Cancel pending confirmation polls along with the coordinator."""
        self._cancel_pending_confirm_refreshes()
        await super().async_shutdown()

    def handle_websocket_disconnect(self) -> None:
        """Handle WebSocket disconnection.

        Reverts to the configured polling interval as a fallback when the
        WebSocket connection is lost, so the integration keeps working
        without real-time updates.
        """
        _LOGGER.warning(
            "WebSocket disconnected, reverting to %d-second polling",
            self._poll_interval.total_seconds(),
        )
        self.update_interval = self._poll_interval

    def set_websocket_active(self) -> None:
        """Set polling interval for WebSocket-active mode.

        Reduces polling frequency to 5 minutes when WebSocket is providing
        real-time updates. Polling continues as a safety net to catch any
        missed updates or handle WebSocket connection issues.
        """
        _LOGGER.info("WebSocket active, reducing polling to 5-minute intervals")
        self.update_interval = timedelta(seconds=300)
