"""Tests for polling cadence: the configurable interval and the extra polls
that confirm a command on backends whose writes take a while to land."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.bestway.const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.bestway.coordinator import BestwayUpdateCoordinator
from custom_components.bestway.entity import OPTIMISTIC_TIMEOUT_S, BestwayEntity
from custom_components.bestway.smartspa.api import SmartSpaApi


def _coordinator(
    hass: HomeAssistant, settle_s: float = 0.0, options: dict | None = None
) -> BestwayUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options or {})
    api = MagicMock()
    api.command_settle_s = settle_s
    coordinator = BestwayUpdateCoordinator(hass, entry, api)
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    return coordinator


async def _advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def test_smartspa_declares_settle_time() -> None:
    """SmartSpa writes land after ~5-10 s; the default is to need no wait."""
    assert SmartSpaApi.command_settle_s == 10.0


async def test_poll_interval_defaults(hass: HomeAssistant) -> None:
    """Without the option set, the coordinator polls every 30 seconds."""
    coordinator = _coordinator(hass)
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_POLL_INTERVAL)


async def test_poll_interval_from_options(hass: HomeAssistant) -> None:
    """The configured interval is used, and restored after a WebSocket drop."""
    coordinator = _coordinator(hass, options={CONF_POLL_INTERVAL: 15.0})
    assert coordinator.update_interval == timedelta(seconds=15)

    coordinator.set_websocket_active()
    assert coordinator.update_interval == timedelta(seconds=300)
    coordinator.handle_websocket_disconnect()
    assert coordinator.update_interval == timedelta(seconds=15)


async def test_command_refreshes_once_when_writes_land_immediately(
    hass: HomeAssistant,
) -> None:
    """A backend with no settle time just refreshes once."""
    coordinator = _coordinator(hass, settle_s=0.0)

    await coordinator.async_refresh_after_command()
    await _advance(hass, 60)

    assert coordinator.async_request_refresh.await_count == 1


async def test_command_repolls_across_settle_window(hass: HomeAssistant) -> None:
    """With a settle time, the coordinator re-polls at one and two settle
    periods, so the result shows without waiting for the next poll."""
    coordinator = _coordinator(hass, settle_s=10.0)

    await coordinator.async_refresh_after_command()
    assert coordinator.async_request_refresh.await_count == 1

    await _advance(hass, 10.5)
    assert coordinator.async_request_refresh.await_count == 2

    await _advance(hass, 20.5)
    assert coordinator.async_request_refresh.await_count == 3

    await _advance(hass, 60)
    assert coordinator.async_request_refresh.await_count == 3


async def test_newer_command_supersedes_pending_repolls(hass: HomeAssistant) -> None:
    """A second command restarts the confirmation polls rather than
    stacking another set on top."""
    coordinator = _coordinator(hass, settle_s=10.0)

    await coordinator.async_refresh_after_command()
    await coordinator.async_refresh_after_command()
    await _advance(hass, 25)

    # Two immediate refreshes, then one pair of confirmation polls.
    assert coordinator.async_request_refresh.await_count == 4


async def test_shutdown_cancels_pending_repolls(hass: HomeAssistant) -> None:
    """Unloading mid-confirmation leaves no timers behind."""
    coordinator = _coordinator(hass, settle_s=10.0)

    await coordinator.async_refresh_after_command()
    await coordinator.async_shutdown()
    await _advance(hass, 25)

    assert coordinator.async_request_refresh.await_count == 1


@pytest.mark.parametrize(
    ("settle_s", "expected"),
    [(0.0, OPTIMISTIC_TIMEOUT_S), (10.0, OPTIMISTIC_TIMEOUT_S + 20.0)],
)
async def test_optimistic_value_spans_settle_window(
    hass: HomeAssistant, settle_s: float, expected: float
) -> None:
    """An entity's optimistic value outlasts the confirmation polls, so it
    doesn't flash back to the old state while a write is still landing."""
    coordinator = _coordinator(hass, settle_s=settle_s)
    entity = BestwayEntity(coordinator, MagicMock(), "device")

    optimistic = entity._new_optimistic()
    optimistic.set(True)

    with patch(
        "custom_components.bestway.entity.monotonic",
        return_value=optimistic._set_at + expected - 0.1,
    ):
        optimistic.confirm(False)
    assert optimistic.value is True

    with patch(
        "custom_components.bestway.entity.monotonic",
        return_value=optimistic._set_at + expected,
    ):
        optimistic.confirm(False)
    assert optimistic.value is None
