"""AWS IoT WebSocket client for V02 device real-time updates.

Provides near-instant state synchronization by connecting to regional AWS API Gateway
endpoints and receiving device shadow delta updates from AWS IoT Core.

Key differences from Gizwits WebSocket:
- No login message (Authorization header authentication)
- Per-device connection (one WebSocket per device)
- Regional endpoints (eu-central-1, us-west-1, etc)
- Shadow delta message format (not s2c_noti)
- 30s heartbeat (not 180s)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import InvalidStatus

from ..translation import v01_attrs_from_shadow
from ..websocket_base import OPEN_TIMEOUT, BaseWebSocketClient

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30
PONG_TIMEOUT = 20

# Regional WebSocket endpoints (from official app ServiceConfig.java)
ENDPOINTS = {
    "eu-central-1": "wss://7lv67j5lbh.execute-api.eu-central-1.amazonaws.com/prod",
    "us-west-1": "wss://9i661wi8f9.execute-api.us-west-1.amazonaws.com/prod",
    "cn-north-1": "wss://fu9gsv4dxh.execute-api.cn-north-1.amazonaws.com.cn/prod",
}


class AwsIotWebSocketException(Exception):
    """Base exception for AWS IoT WebSocket operations."""


class AwsIotWebSocket(BaseWebSocketClient):
    """Per-device WebSocket client for AWS IoT shadow updates.

    Each device requires its own WebSocket connection to receive real-time
    state updates from AWS IoT device shadows. Reconnection with
    exponential backoff is handled by BaseWebSocketClient.run().
    """

    def __init__(
        self,
        device_id: str,
        service_region: str,
        token: str,
        update_callback: Callable[[str, dict[str, Any]], None],
        disconnect_callback: Callable[[], None] | None = None,
        token_refresh_callback: Callable[[], Awaitable[str]] | None = None,
        connect_callback: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the client for one device's shadow subscription.
        update_callback is called with (device_id, normalized_attrs) on each
        update; disconnect_callback also fires on a failed connection
        attempt; token_refresh_callback is used to recover from an HTTP 400.
        """
        super().__init__(disconnect_callback, connect_callback)
        self._device_id = device_id
        self._service_region = service_region
        self._token = token
        self._update_callback = update_callback
        self._token_refresh_callback = token_refresh_callback
        self._seq_id = int(time.time() * 1000)

    @property
    def ws_url(self) -> str:
        """Get region-specific WebSocket endpoint."""
        endpoint = ENDPOINTS.get(self._service_region, ENDPOINTS["eu-central-1"])
        if self._service_region not in ENDPOINTS:
            _LOGGER.warning(
                "Unknown region %s for device %s, using EU",
                self._service_region,
                self._device_id,
            )
        return endpoint

    @property
    def _log_name(self) -> str:
        return f"WebSocket for device {self._device_id}"

    async def _open(self) -> None:
        """Connect to the region-specific endpoint.

        API Gateway authenticates the Authorization header during the
        opening handshake, so there is no login message. An expired token
        is rejected with HTTP 400; that is retried once immediately with a
        fresh token rather than waiting out a backoff delay.
        """
        try:
            self._websocket = await self._connect_socket()
        except InvalidStatus as err:
            if err.response.status_code != 400 or self._token_refresh_callback is None:
                raise
            _LOGGER.info(
                "Token rejected for device %s (HTTP 400), refreshing",
                self._device_id,
            )
            self._token = await self._token_refresh_callback()
            self._websocket = await self._connect_socket()

    async def _connect_socket(self) -> Any:
        _LOGGER.info(
            "Connecting WebSocket for device %s (region: %s)",
            self._device_id,
            self._service_region,
        )
        from homeassistant.util import ssl as ssl_util

        return await websockets.connect(
            self.ws_url,
            additional_headers={"Authorization": self._token},
            ssl=ssl_util.get_default_context(),
            ping_interval=None,  # Manual heartbeat
            open_timeout=OPEN_TIMEOUT,
        )

    async def _listen_loop(self) -> None:
        """Process shadow update messages until the connection closes."""
        async for message in self._websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                _LOGGER.warning("Received malformed JSON message")
                continue
            try:
                await self._handle_message(data)
            except Exception as ex:
                _LOGGER.error("Error processing WebSocket message: %s", ex)

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Process shadow update message.

        Handles the three AWS IoT shadow notification shapes:
        - documents:  {current: {state: {reported: {...}, desired: {...}}}, ...}
        - update:     {state: {reported: {...}, desired: {...}}, ...}
        - delta:      {state: {<field>: <value>, ...}, ...}
        """
        state: dict[str, Any] | None = None

        # "documents" notification — full shadow snapshot
        current = data.get("current")
        if isinstance(current, dict):
            current_state = current.get("state")
            if isinstance(current_state, dict):
                reported = current_state.get("reported") or {}
                desired = current_state.get("desired") or {}
                if reported or desired:
                    state = {**reported, **desired}

        # "update" / "delta" notifications carry "state" at the top level
        if state is None:
            raw_state = data.get("state")
            if isinstance(raw_state, dict):
                reported = raw_state.get("reported")
                desired = raw_state.get("desired")
                if isinstance(reported, dict) or isinstance(desired, dict):
                    # update-style: merge desired over reported (desired wins)
                    state = {**(reported or {}), **(desired or {})}
                else:
                    # delta-style: state values are the deltas themselves
                    state = raw_state

        if not state:
            return

        _LOGGER.debug("Shadow update for %s: %s", self._device_id, state)

        normalized = v01_attrs_from_shadow(state)

        _LOGGER.debug("Normalized attrs for %s: %s", self._device_id, normalized)

        # Call coordinator callback with (device_id, normalized_attrs)
        if self._update_callback is not None:
            try:
                self._update_callback(self._device_id, normalized)
            except Exception as err:
                _LOGGER.error("Callback error: %s", err)

    async def _heartbeat_loop(self) -> None:
        """Send a heartbeat every 30 seconds and check the connection answers.

        API Gateway drops connections idle for 10 minutes, so the JSON
        heartbeat keeps it open. The protocol ping catches a half-open
        connection (e.g. after a NAT timeout or network change) that would
        otherwise never deliver another message nor raise an error; a
        missing pong raises TimeoutError, ending the connection.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            message = {
                "action": "heartbeat",
                "req_event": "heartbeat_req",
                "seq_id": self._seq_id,
                "req_count": 1,
                "req": None,
            }
            await self._websocket.send(json.dumps(message))
            self._seq_id += 1

            pong_waiter = await self._websocket.ping()
            async with asyncio.timeout(PONG_TIMEOUT):
                await pong_waiter
            _LOGGER.debug("Heartbeat acknowledged for device %s", self._device_id)
