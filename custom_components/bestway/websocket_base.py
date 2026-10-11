"""Shared connection supervisor for the Gizwits and AWS IoT real-time
WebSocket clients.

Both clients hold a socket open for as long as the integration is loaded,
reconnecting on the same exponential backoff schedule whenever it drops or
an attempt fails, and both need the same "notify the disconnect callback at
most once per disconnected period" behaviour. The connection handshake,
message format and heartbeat payload differ too much between the two wire
protocols to share, so subclasses supply those as `_open()`,
`_listen_loop()` and `_heartbeat_loop()`; everything about *when* to
connect lives here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from logging import getLogger
from time import monotonic
from typing import Any

_LOGGER = getLogger(__name__)

# Reconnection delays (exponential backoff): 3s -> 6s -> 12s -> 24s -> 48s -> 60s max
RECONNECT_DELAYS = [3, 6, 12, 24, 48, 60]

# A connection must stay up this long before the backoff resets. Without
# this, a server that accepts the socket and then immediately drops it
# would be retried every RECONNECT_DELAYS[0] seconds forever.
STABLE_CONNECTION_SECONDS = 60

# Time to allow for TCP connect + TLS + opening handshake before giving up.
# Without this, a firewall that silently drops packets (rather than refusing
# the connection) leaves connect() hanging on the OS-level TCP timeout.
OPEN_TIMEOUT = 10


class BaseWebSocketClient:
    """Keeps one WebSocket connection alive until disconnect() is called.

    `run()` is a single long-lived loop: open, serve until the connection
    ends, back off, repeat. Reconnecting from that loop - rather than from
    inside the listen task of the connection that just died - means there
    is no recursion and no task ever has to cancel itself, so a long outage
    can't wedge the client into permanent polling.

    Subclasses implement:
    - `_open()`: connect and complete any handshake, leaving the socket in
      `self._websocket`; raise on failure.
    - `_listen_loop()`: consume messages until the connection ends.
    - `_heartbeat_loop()`: keep the connection alive; return or raise once
      it is no longer usable.
    - `_log_name`: how this connection is described in log messages.
    """

    def __init__(
        self,
        disconnect_callback: Callable[[], None] | None = None,
        connect_callback: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the shared connection/backoff state."""
        self._disconnect_callback = disconnect_callback
        self._connect_callback = connect_callback

        self._websocket: Any = None
        self._listen_task: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._connected = False
        self._reconnect_count = 0
        # Ensures disconnect_callback fires once per disconnected period,
        # rather than on every retry while backing off.
        self._notified_disconnect = False

    @property
    def _log_name(self) -> str:
        raise NotImplementedError

    async def _open(self) -> None:
        raise NotImplementedError

    async def _listen_loop(self) -> None:
        raise NotImplementedError

    async def _heartbeat_loop(self) -> None:
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        """Return True while a connection is open and ready for updates."""
        return self._connected

    async def run(self) -> None:
        """Connect, and keep reconnecting with backoff until disconnect().

        Never returns on its own; run it as a background task. Connection
        failures are logged and retried, never raised.
        """
        if self._run_task is not None and not self._run_task.done():
            _LOGGER.warning("%s is already running", self._log_name)
            return
        self._run_task = asyncio.current_task()

        try:
            while True:
                await self._connect_and_serve()
                await self._backoff()
        finally:
            self._connected = False
            await self._cancel_and_close()

    async def disconnect(self) -> None:
        """Stop reconnecting and close the connection. Safe to call repeatedly."""
        _LOGGER.debug("Disconnecting %s", self._log_name)
        task, self._run_task = self._run_task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._connected = False
        await self._cancel_and_close()

    async def _connect_and_serve(self) -> None:
        """Make one connection attempt and, if it succeeds, serve it until
        it drops."""
        _LOGGER.debug("Connecting %s", self._log_name)
        try:
            await self._open()
        except Exception as ex:
            await self._cancel_and_close()
            # Only the first failure of an outage is worth a warning; the
            # rest repeat every minute for as long as the outage lasts.
            log = _LOGGER.debug if self._notified_disconnect else _LOGGER.warning
            log("%s connection failed: %s", self._log_name, _describe(ex))
            self._notify_disconnected()
            return

        _LOGGER.info("%s connected", self._log_name)
        self._connected = True
        self._notify_connected()
        connected_at = monotonic()
        try:
            await self._serve()
        finally:
            self._connected = False
            await self._cancel_and_close()

        if monotonic() - connected_at >= STABLE_CONNECTION_SECONDS:
            self._reconnect_count = 0
        self._notify_disconnected()

    async def _serve(self) -> None:
        """Run the listen and heartbeat loops until either one ends.

        Either ending means the connection is no longer usable: the listen
        loop ends when the socket closes, the heartbeat loop when the
        connection stops answering.
        """
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        done, _ = await asyncio.wait(
            {self._listen_task, self._heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        reason = "closed by server"
        for task in done:
            if not task.cancelled() and (ex := task.exception()) is not None:
                reason = _describe(ex)
        _LOGGER.warning("%s connection lost: %s", self._log_name, reason)

    async def _backoff(self) -> None:
        """Sleep before the next connection attempt."""
        delay = self._next_reconnect_delay()
        self._reconnect_count += 1
        _LOGGER.info(
            "Reconnecting %s in %ds (attempt %d)",
            self._log_name,
            delay,
            self._reconnect_count,
        )
        await asyncio.sleep(delay)

    def _next_reconnect_delay(self) -> int:
        """Backoff delay for the current reconnect attempt.

        The index is clamped to the last entry, so the delay caps at
        RECONNECT_DELAYS[-1] (60s) rather than growing unbounded.
        """
        return RECONNECT_DELAYS[min(self._reconnect_count, len(RECONNECT_DELAYS) - 1)]

    async def _cancel_and_close(self) -> None:
        """Cancel the listen/heartbeat tasks and close the socket."""
        tasks = [
            task
            for task in (self._listen_task, self._heartbeat_task)
            if task is not None and task is not asyncio.current_task()
        ]
        self._listen_task = None
        self._heartbeat_task = None
        for task in tasks:
            task.cancel()
        # return_exceptions collects each task's own cancellation (or
        # failure) without swallowing a cancellation of the caller.
        await asyncio.gather(*tasks, return_exceptions=True)

        websocket, self._websocket = self._websocket, None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception as ex:
                _LOGGER.debug("Error closing %s: %s", self._log_name, ex)

    def _notify_connected(self) -> None:
        """Invoke connect callback and re-arm the disconnect notification."""
        self._notified_disconnect = False
        if self._connect_callback:
            try:
                self._connect_callback()
            except Exception as ex:
                _LOGGER.error("Error in connect callback: %s", ex)

    def _notify_disconnected(self) -> None:
        """Invoke disconnect callback at most once per disconnected period.

        Connection attempts repeat while backing off, so without this guard
        a persistently unreachable endpoint would fire the callback (and its
        log warning) on every attempt.
        """
        if self._notified_disconnect:
            return
        self._notified_disconnect = True
        if self._disconnect_callback:
            try:
                self._disconnect_callback()
            except Exception as ex:
                _LOGGER.error("Error in disconnect callback: %s", ex)


def _describe(ex: BaseException) -> str:
    """Exception text for a log line; some (e.g. TimeoutError) have none."""
    return str(ex) or type(ex).__name__
