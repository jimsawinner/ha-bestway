"""Tests for the Gizwits WebSocket module."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets.exceptions

from custom_components.bestway.bestway.websocket import (
    GizwitsWebSocket,
    GizwitsWebSocketException,
)


@pytest.mark.asyncio
async def test_websocket_device_update():
    """Test device update message handling."""
    updates_received = []

    def update_callback(device_id, attrs):
        updates_received.append((device_id, attrs))

    ws = GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=update_callback,
    )

    # Test the message handler directly
    device_update_msg = {
        "cmd": "s2c_noti",
        "data": {
            "did": "device_abc123",
            "attrs": {
                "power": 1,
                "temp_now": 36,
                "temp_set": 38,
                "heat_power": 1,
                "filter_power": 1,
            },
        },
    }

    ws._handle_device_update(device_update_msg)

    # Verify callback was invoked
    assert len(updates_received) == 1
    assert updates_received[0][0] == "device_abc123"
    assert updates_received[0][1]["power"] == 1
    assert updates_received[0][1]["temp_now"] == 36


@pytest.mark.asyncio
async def test_websocket_device_update_empty_attrs():
    """Test device update with empty attributes."""
    update_callback = MagicMock()

    ws = GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=update_callback,
    )

    # Device update with empty attrs
    device_update_msg = {"cmd": "s2c_noti", "data": {"did": "device_123", "attrs": {}}}

    ws._handle_device_update(device_update_msg)

    # Callback should not be invoked for empty attrs
    update_callback.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_device_update_missing_device_id():
    """Test device update without device ID."""
    update_callback = MagicMock()

    ws = GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=update_callback,
    )

    # Device update without did
    device_update_msg = {"cmd": "s2c_noti", "data": {"attrs": {"power": 1}}}

    ws._handle_device_update(device_update_msg)

    # Callback should not be invoked
    update_callback.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_callback_exception_handling():
    """Test that exceptions in callback don't crash WebSocket."""

    def failing_callback(device_id, attrs):
        raise RuntimeError("Callback error")

    ws = GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=failing_callback,
    )

    # Should not raise exception
    device_update_msg = {
        "cmd": "s2c_noti",
        "data": {"did": "device_123", "attrs": {"power": 1}},
    }

    ws._handle_device_update(device_update_msg)
    # Test passes if no exception raised


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_ping():
    """Test heartbeat loop sends ping messages."""
    update_callback = MagicMock()
    ws = GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=update_callback,
    )

    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    ws._websocket = mock_ws

    # Test heartbeat sends ping
    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        # Run one iteration of heartbeat loop
        mock_sleep.side_effect = [
            None,
            asyncio.CancelledError(),
        ]  # First sleep succeeds, second cancels

        try:
            await ws._heartbeat_loop()
        except asyncio.CancelledError:
            pass

        # Verify ping was sent
        assert mock_ws.send.called
        ping_msg = json.loads(mock_ws.send.call_args[0][0])
        assert ping_msg["cmd"] == "ping"


@pytest.mark.asyncio
async def test_listen_loop_processes_messages():
    """Test listen loop processes incoming messages."""
    updates_received = []

    def update_callback(device_id, attrs):
        updates_received.append((device_id, attrs))

    ws = GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=update_callback,
    )

    # Mock WebSocket with messages
    mock_ws = MagicMock()
    ws._websocket = mock_ws

    # Create async iterator that yields messages then stops
    messages = [
        json.dumps(
            {"cmd": "s2c_noti", "data": {"did": "device1", "attrs": {"power": 1}}}
        ),
        json.dumps({"cmd": "pong"}),
        json.dumps(
            {"cmd": "s2c_online_status", "data": {"did": "device1", "is_online": True}}
        ),
    ]

    async def mock_async_iter():
        for msg in messages:
            yield msg

    mock_ws.__aiter__ = lambda self: mock_async_iter()

    # Run listen loop (will process messages then exit)
    await ws._listen_loop()

    # Verify device update was processed
    assert len(updates_received) == 1
    assert updates_received[0][0] == "device1"
    assert updates_received[0][1]["power"] == 1


@pytest.mark.asyncio
async def test_listen_loop_handles_json_decode_error():
    """Test listen loop handles malformed JSON gracefully."""
    update_callback = MagicMock()
    ws = GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=update_callback,
    )

    mock_ws = MagicMock()
    ws._websocket = mock_ws

    # Send malformed JSON
    async def mock_async_iter():
        yield "{ invalid json }"

    mock_ws.__aiter__ = lambda self: mock_async_iter()

    # Should not raise exception
    await ws._listen_loop()

    # Callback should not have been called
    update_callback.assert_not_called()


def _gizwits_ws(**kwargs):
    return GizwitsWebSocket(
        uid="test_uid",
        token="test_token",
        ws_host="m2m.gizwits.com",
        ws_port=8880,
        update_callback=kwargs.pop("update_callback", MagicMock()),
        **kwargs,
    )


def _mock_socket(login_response, messages=()):
    """A socket that answers the login, yields `messages`, then drops."""
    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.recv = AsyncMock(return_value=json.dumps(login_response))

    async def drop_after_messages():
        for message in messages:
            yield message
        raise websockets.exceptions.ConnectionClosedError(None, None)

    mock_ws.__aiter__ = lambda self: drop_after_messages()
    return mock_ws


LOGIN_OK = {"cmd": "login_res", "data": {"success": True}}


@pytest.mark.asyncio
async def test_open_connects_and_logs_in():
    """_open() connects to the bindings-derived URL and sends login_req."""
    ws = _gizwits_ws()
    mock_ws = _mock_socket(LOGIN_OK)

    with (
        patch(
            "custom_components.bestway.bestway.websocket.websockets.connect",
            new=AsyncMock(return_value=mock_ws),
        ) as mock_connect,
        patch("homeassistant.util.ssl.get_default_context"),
    ):
        await ws._open()

    assert mock_connect.call_args.args[0] == "wss://m2m.gizwits.com:8880/ws/app/v1"
    # A firewall that silently drops packets must fail fast, not hang on
    # the OS-level TCP timeout.
    assert mock_connect.call_args.kwargs.get("open_timeout") is not None
    login_msg = json.loads(mock_ws.send.call_args.args[0])
    assert login_msg["cmd"] == "login_req"
    assert login_msg["data"]["uid"] == "test_uid"
    assert login_msg["data"]["token"] == "test_token"
    assert login_msg["data"]["auto_subscribe"] is True


@pytest.mark.asyncio
async def test_open_raises_on_login_failure():
    """A rejected login is a failed connection attempt."""
    ws = _gizwits_ws()
    mock_ws = _mock_socket(
        {"cmd": "login_res", "data": {"success": False, "msg": "Invalid token"}}
    )

    with (
        patch(
            "custom_components.bestway.bestway.websocket.websockets.connect",
            new=AsyncMock(return_value=mock_ws),
        ),
        patch("homeassistant.util.ssl.get_default_context"),
        pytest.raises(GizwitsWebSocketException, match="Invalid token"),
    ):
        await ws._open()


@pytest.mark.asyncio
async def test_reconnects_through_an_outage():
    """After a drop, the client keeps retrying until the server is back.

    Regression: reconnects used to run inside the listen task of the dead
    connection, which then tried to cancel and await itself; the second
    failed attempt in a row killed the retry chain with a RuntimeError,
    leaving the integration on 30-second polling until a restart.
    """
    update_callback = MagicMock()
    connect_callback = MagicMock()
    disconnect_callback = MagicMock()
    ws = _gizwits_ws(
        update_callback=update_callback,
        connect_callback=connect_callback,
        disconnect_callback=disconnect_callback,
    )
    update = json.dumps(
        {"cmd": "s2c_noti", "data": {"did": "device1", "attrs": {"power": 1}}}
    )
    # Connect, drop, fail five times, connect again.
    outcomes = [_mock_socket(LOGIN_OK)]
    outcomes += [OSError("Network unreachable")] * 5
    outcomes += [_mock_socket(LOGIN_OK, messages=[update])]
    reconnected = asyncio.Event()
    update_callback.side_effect = lambda *_: reconnected.set()

    with (
        patch(
            "custom_components.bestway.bestway.websocket.websockets.connect",
            new=AsyncMock(side_effect=outcomes),
        ) as mock_connect,
        patch("homeassistant.util.ssl.get_default_context"),
        patch("custom_components.bestway.websocket_base.RECONNECT_DELAYS", [0]),
    ):
        task = asyncio.create_task(ws.run())
        await asyncio.wait_for(reconnected.wait(), timeout=2)
        await ws.disconnect()

    assert task.done()
    assert mock_connect.call_count == 7
    update_callback.assert_called_once_with("device1", {"power": 1})
    assert connect_callback.call_count == 2
    # Once for the whole outage, not once per failed retry.
    disconnect_callback.assert_called_once()
