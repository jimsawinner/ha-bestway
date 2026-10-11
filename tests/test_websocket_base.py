"""Tests for the reconnect supervisor shared by both WebSocket clients."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bestway.websocket_base import (
    RECONNECT_DELAYS,
    STABLE_CONNECTION_SECONDS,
    BaseWebSocketClient,
)


class FakeClient(BaseWebSocketClient):
    """Client whose connection attempts follow a script.

    Each entry in `script` is one attempt: an exception fails it, a
    float connects and then drops after that many seconds.
    """

    def __init__(self, script, **kwargs):
        super().__init__(**kwargs)
        self.script = list(script)
        self.attempts = 0
        self.sockets = []
        self.heartbeat = AsyncMock(side_effect=lambda: asyncio.Event().wait())
        self.uptime = 0.0
        self.exhausted = asyncio.Event()

    @property
    def _log_name(self):
        return "Fake WebSocket"

    async def _open(self):
        self.attempts += 1
        if not self.script:
            self.exhausted.set()
            await asyncio.Event().wait()
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.uptime = outcome
        self._websocket = MagicMock(close=AsyncMock())
        self.sockets.append(self._websocket)

    async def _listen_loop(self):
        pass  # The socket closes as soon as it opens.

    async def _heartbeat_loop(self):
        await self.heartbeat()


async def _run_script(client):
    """Run the client until its script is used up, then stop it."""
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    with patch(
        "custom_components.bestway.websocket_base.asyncio.sleep", new=fake_sleep
    ):
        # Uptime is measured with monotonic(); fake it from the script.
        clock = [0.0]

        def fake_monotonic():
            return clock[0]

        original_listen = client._listen_loop

        async def listen_and_age():
            await original_listen()
            clock[0] += client.uptime

        client._listen_loop = listen_and_age
        with patch(
            "custom_components.bestway.websocket_base.monotonic", new=fake_monotonic
        ):
            task = asyncio.create_task(client.run())
            await asyncio.wait_for(client.exhausted.wait(), timeout=2)
            await client.disconnect()
    assert task.done()
    return sleeps


@pytest.mark.asyncio
async def test_backoff_grows_and_caps():
    """Consecutive failures back off exponentially up to the cap."""
    client = FakeClient([OSError("down")] * 8)

    sleeps = await _run_script(client)

    assert sleeps == RECONNECT_DELAYS + [RECONNECT_DELAYS[-1]] * 2


@pytest.mark.asyncio
async def test_backoff_resets_only_after_a_stable_connection():
    """A connection that drops straight away doesn't reset the backoff.

    Otherwise a server that accepts and immediately drops the socket
    would be retried every few seconds forever.
    """
    client = FakeClient(
        [
            OSError("down"),
            1.0,  # brief connection: backoff keeps growing
            OSError("down"),
            float(STABLE_CONNECTION_SECONDS),  # stable: backoff resets
            OSError("down"),
        ]
    )

    sleeps = await _run_script(client)

    assert sleeps == [3, 6, 12, 3, 6]


@pytest.mark.asyncio
async def test_callbacks_once_per_state_change():
    """connect fires on every connection; disconnect once per outage."""
    connect_callback = MagicMock()
    disconnect_callback = MagicMock()
    client = FakeClient(
        [OSError("down"), OSError("down"), 1.0, OSError("down"), 1.0],
        connect_callback=connect_callback,
        disconnect_callback=disconnect_callback,
    )

    await _run_script(client)

    assert connect_callback.call_count == 2
    # Initial outage, drop after first connection, drop after second.
    assert disconnect_callback.call_count == 3


@pytest.mark.asyncio
async def test_heartbeat_failure_ends_connection():
    """A heartbeat that fails ends the connection and triggers a reconnect,
    even though the listen loop never saw the socket close."""
    client = FakeClient([1.0, 1.0])
    client.heartbeat.side_effect = TimeoutError()

    async def listen_forever():
        await asyncio.Event().wait()

    client._listen_loop = listen_forever

    await _run_script(client)

    assert client.attempts == 3
    for socket in client.sockets:
        socket.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_closes_socket_and_stops_retrying():
    """disconnect() closes the live socket and no further attempts are made."""
    client = FakeClient([])
    client._websocket = socket = MagicMock(close=AsyncMock())

    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.exhausted.wait(), timeout=2)
    await client.disconnect()

    assert task.done()
    socket.close.assert_awaited_once()
    assert not client.is_connected
    assert client.attempts == 1
    # Safe to call again.
    await client.disconnect()


@pytest.mark.asyncio
async def test_is_connected_tracks_connection():
    """is_connected is True only while a connection is being served."""
    client = FakeClient([60.0])
    observed = []

    async def heartbeat():
        observed.append(client.is_connected)
        await asyncio.Event().wait()

    client.heartbeat.side_effect = heartbeat

    await _run_script(client)

    assert observed == [True]
    assert not client.is_connected


@pytest.mark.asyncio
async def test_run_twice_is_ignored(caplog):
    """A second run() while the first is active doesn't start a second loop."""
    client = FakeClient([])

    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.exhausted.wait(), timeout=2)
    with caplog.at_level(logging.WARNING):
        await client.run()
    await client.disconnect()

    assert task.done()
    assert client.attempts == 1
    assert "already running" in caplog.text


@pytest.mark.asyncio
async def test_only_first_failure_of_an_outage_warns(caplog):
    """Retries during a long outage log at debug, not one warning a minute."""
    client = FakeClient([OSError("down")] * 5)

    with caplog.at_level(logging.DEBUG):
        await _run_script(client)

    failures = [r for r in caplog.records if "connection failed" in r.message]
    assert len(failures) == 5
    assert [r.levelno for r in failures] == [logging.WARNING] + [logging.DEBUG] * 4
