"""Tests for AWS IoT WebSocket client."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response

from custom_components.bestway.aws_iot.websocket import AwsIotWebSocket


@pytest.fixture
def mock_callback():
    """Create mock coordinator callback."""
    return MagicMock()


@pytest.fixture
def aws_websocket(mock_callback):
    """Create AwsIotWebSocket instance."""
    return AwsIotWebSocket(
        device_id="test_device_123",
        service_region="eu-central-1",
        token="test_token",
        update_callback=mock_callback,
    )


@pytest.mark.asyncio
async def test_handle_message_calls_callback_with_normalized_attrs(
    aws_websocket, mock_callback
):
    """Test shadow delta message triggers callback with normalized fields."""
    # Shadow delta message from AWS IoT
    message = {
        "state": {
            "reported": {
                "power_state": 1,
                "heater_state": 3,
                "temperature_setting": 37,
            }
        }
    }

    with patch(
        "custom_components.bestway.aws_iot.websocket.v01_attrs_from_shadow"
    ) as mock_normalize:
        mock_normalize.return_value = {"power": True, "heat": 3, "Tset": 37}

        await aws_websocket._handle_message(message)

        # Verify callback was called with device_id and normalized attrs
        mock_callback.assert_called_once()
        call_args = mock_callback.call_args[0]
        assert call_args[0] == "test_device_123"  # device_id
        assert call_args[1] == {"power": True, "heat": 3, "Tset": 37}  # normalized


@pytest.mark.asyncio
async def test_handle_message_no_callback_error(aws_websocket):
    """Test message handling with missing callback doesn't crash."""
    aws_websocket._update_callback = None
    message = {"state": {"reported": {"power_state": 1}}}

    # Should not raise exception
    with patch("custom_components.bestway.aws_iot.websocket.v01_attrs_from_shadow"):
        await aws_websocket._handle_message(message)


@pytest.mark.asyncio
async def test_handle_message_delta_shape(aws_websocket, mock_callback):
    """Delta-shape shadow messages (no nested reported/desired) reach the callback.

    AWS IoT also publishes update-delta notifications where the changed
    fields live directly under "state". Before the fix these were silently
    dropped.
    """
    message = {"state": {"power_state": 1, "filter_state": 1}, "version": 42}

    with patch(
        "custom_components.bestway.aws_iot.websocket.v01_attrs_from_shadow"
    ) as mock_normalize:
        mock_normalize.return_value = {"power": True, "filter": 1}
        await aws_websocket._handle_message(message)

    mock_normalize.assert_called_once_with({"power_state": 1, "filter_state": 1})
    mock_callback.assert_called_once()
    assert mock_callback.call_args[0][1] == {"power": True, "filter": 1}


@pytest.mark.asyncio
async def test_handle_message_documents_shape(aws_websocket, mock_callback):
    """Documents-shape shadow snapshots (current.state.reported) reach the callback."""
    message = {
        "current": {
            "state": {
                "reported": {"power_state": 1},
                "desired": {"heater_state": 1},
            }
        }
    }

    with patch(
        "custom_components.bestway.aws_iot.websocket.v01_attrs_from_shadow"
    ) as mock_normalize:
        mock_normalize.return_value = {"power": True, "heat": 1}
        await aws_websocket._handle_message(message)

    # desired wins over reported on key conflicts; here they're disjoint
    mock_normalize.assert_called_once_with({"power_state": 1, "heater_state": 1})
    mock_callback.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_desired_overrides_reported(aws_websocket, mock_callback):
    """When reported and desired disagree, desired wins (matches Bestway app)."""
    message = {
        "state": {
            "reported": {"filter_state": 0},
            "desired": {"filter_state": 1},
        }
    }

    with patch(
        "custom_components.bestway.aws_iot.websocket.v01_attrs_from_shadow"
    ) as mock_normalize:
        mock_normalize.return_value = {"filter": 1}
        await aws_websocket._handle_message(message)

    mock_normalize.assert_called_once_with({"filter_state": 1})


@pytest.mark.asyncio
async def test_region_fallback_to_eu(aws_websocket):
    """Test unknown region falls back to EU endpoint."""
    aws_websocket._service_region = "unknown-region-99"

    url = aws_websocket.ws_url

    # Should use EU endpoint as fallback
    assert "eu-central-1" in url


@pytest.mark.asyncio
async def test_listen_loop_processes_shadow_updates(aws_websocket):
    """Test listen loop processes incoming shadow delta messages."""
    updates_received = []

    def capture_callback(device_id, attrs):
        updates_received.append((device_id, attrs))

    aws_websocket._update_callback = capture_callback

    # Mock WebSocket to yield messages
    mock_ws = MagicMock()
    message1 = json.dumps({"state": {"reported": {"power_state": 1}}})
    message2 = json.dumps({"state": {"reported": {"heater_state": 3}}})

    async def message_generator():
        yield message1
        yield message2

    mock_ws.__aiter__ = lambda self: message_generator()
    aws_websocket._websocket = mock_ws

    with patch(
        "custom_components.bestway.aws_iot.websocket.v01_attrs_from_shadow"
    ) as mock_normalize:
        mock_normalize.side_effect = [
            {"power": True},
            {"heat": 3},
        ]

        await aws_websocket._listen_loop()

        # Verify both messages processed
        assert len(updates_received) == 2
        assert updates_received[0] == ("test_device_123", {"power": True})
        assert updates_received[1] == ("test_device_123", {"heat": 3})


def _rejected(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "Rejected", Headers()))


def _dropping_socket(messages=()):
    """A socket that yields `messages`, then drops."""
    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    mock_ws.close = AsyncMock()

    async def drop_after_messages():
        for message in messages:
            yield message
        raise ConnectionClosedError(None, None)

    mock_ws.__aiter__ = lambda self: drop_after_messages()
    return mock_ws


@pytest.mark.asyncio
async def test_open_sends_auth_header(aws_websocket):
    """_open() authenticates with the token in the Authorization header."""
    with (
        patch(
            "custom_components.bestway.aws_iot.websocket.websockets.connect",
            new=AsyncMock(),
        ) as mock_connect,
        patch("homeassistant.util.ssl.get_default_context"),
    ):
        await aws_websocket._open()

    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["additional_headers"]["Authorization"] == "test_token"
    # A firewall that silently drops packets must fail fast, not hang on
    # the OS-level TCP timeout.
    assert call_kwargs.get("open_timeout") is not None


@pytest.mark.asyncio
async def test_open_refreshes_token_on_http_400(aws_websocket):
    """An expired token (HTTP 400) is refreshed and retried immediately."""
    aws_websocket._token_refresh_callback = AsyncMock(return_value="new_token_789")

    with (
        patch(
            "custom_components.bestway.aws_iot.websocket.websockets.connect",
            new=AsyncMock(side_effect=[_rejected(400), MagicMock()]),
        ) as mock_connect,
        patch("homeassistant.util.ssl.get_default_context"),
    ):
        await aws_websocket._open()

    aws_websocket._token_refresh_callback.assert_awaited_once()
    assert mock_connect.call_count == 2
    retry_headers = mock_connect.call_args.kwargs["additional_headers"]
    assert retry_headers["Authorization"] == "new_token_789"


@pytest.mark.asyncio
async def test_open_does_not_refresh_token_on_other_errors(aws_websocket):
    """Only HTTP 400 means an expired token; other rejections just fail."""
    aws_websocket._token_refresh_callback = AsyncMock()

    with (
        patch(
            "custom_components.bestway.aws_iot.websocket.websockets.connect",
            new=AsyncMock(side_effect=_rejected(503)),
        ),
        patch("homeassistant.util.ssl.get_default_context"),
        pytest.raises(InvalidStatus),
    ):
        await aws_websocket._open()

    aws_websocket._token_refresh_callback.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_sends_json_heartbeat_and_checks_pong(aws_websocket):
    """Each heartbeat sends the JSON keepalive and waits for a pong."""
    pong = asyncio.get_running_loop().create_future()
    pong.set_result(0.01)
    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    mock_ws.ping = AsyncMock(return_value=pong)
    aws_websocket._websocket = mock_ws

    with (
        patch(
            "custom_components.bestway.aws_iot.websocket.asyncio.sleep",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await aws_websocket._heartbeat_loop()

    heartbeat_msg = json.loads(mock_ws.send.call_args.args[0])
    assert heartbeat_msg["action"] == "heartbeat"
    assert heartbeat_msg["req_event"] == "heartbeat_req"
    assert "seq_id" in heartbeat_msg
    mock_ws.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_raises_when_pong_never_arrives(aws_websocket):
    """A half-open connection is detected by the missing pong."""
    mock_ws = MagicMock()
    mock_ws.send = AsyncMock()
    mock_ws.ping = AsyncMock(
        return_value=asyncio.get_running_loop().create_future()  # never resolves
    )
    aws_websocket._websocket = mock_ws

    with (
        patch(
            "custom_components.bestway.aws_iot.websocket.asyncio.sleep", new=AsyncMock()
        ),
        patch("custom_components.bestway.aws_iot.websocket.PONG_TIMEOUT", 0.01),
        pytest.raises(TimeoutError),
    ):
        await aws_websocket._heartbeat_loop()


@pytest.mark.asyncio
async def test_reconnects_after_drop_and_failed_attempts(mock_callback):
    """After a drop, the client reconnects, retrying through failures.

    Regression: connect() bailed out with "already running" when called
    after a drop, and the backoff path gave up after any failed attempt,
    so a single drop (API Gateway closes every socket after 2 hours) left
    the device on 30-second polling until Home Assistant restarted.
    """
    connect_callback = MagicMock()
    disconnect_callback = MagicMock()
    ws = AwsIotWebSocket(
        device_id="test_device_123",
        service_region="eu-central-1",
        token="test_token",
        update_callback=mock_callback,
        disconnect_callback=disconnect_callback,
        connect_callback=connect_callback,
    )
    update = json.dumps({"state": {"reported": {"power_state": 1}}})
    outcomes = [_dropping_socket()]
    outcomes += [OSError("Network unreachable")] * 3
    outcomes += [_dropping_socket(messages=[update])]
    reconnected = asyncio.Event()
    mock_callback.side_effect = lambda *_: reconnected.set()

    with (
        patch(
            "custom_components.bestway.aws_iot.websocket.websockets.connect",
            new=AsyncMock(side_effect=outcomes),
        ) as mock_connect,
        patch("homeassistant.util.ssl.get_default_context"),
        patch("custom_components.bestway.websocket_base.RECONNECT_DELAYS", [0]),
    ):
        task = asyncio.create_task(ws.run())
        await asyncio.wait_for(reconnected.wait(), timeout=2)
        await ws.disconnect()

    assert task.done()
    assert mock_connect.call_count == 5
    mock_callback.assert_called_once()
    assert mock_callback.call_args.args[0] == "test_device_123"
    assert connect_callback.call_count == 2
    # Once for the whole outage, not once per failed retry.
    disconnect_callback.assert_called_once()
