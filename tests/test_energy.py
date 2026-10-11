"""Tests for the estimated power and energy sensors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorExtraStoredData

from custom_components.bestway.energy import (
    DEFAULT_WATTAGES,
    SpaWattages,
    estimate_power_watts,
)
from custom_components.bestway.model import (
    BestwayApiResults,
    BubblesLevel,
    DeviceStatus,
    HeaterState,
)
from custom_components.bestway.sensor import (
    _ESTIMATED_ENERGY_SENSOR,
    _ESTIMATED_POWER_SENSOR,
    MAX_ENERGY_SAMPLE_GAP_S,
    EstimatedEnergySensor,
    EstimatedPowerSensor,
)

HEATER = DEFAULT_WATTAGES.heater
FILTER = DEFAULT_WATTAGES.filter
BUBBLES = DEFAULT_WATTAGES.bubbles

_DEVICE_ID = "spa1"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DeviceStatus(), None),
        (DeviceStatus(power=False, heater=HeaterState.HEATING, filtering=True), 0),
        (DeviceStatus(power=True), 0),
        (DeviceStatus(power=True, filtering=True), FILTER),
        (
            DeviceStatus(power=True, heater=HeaterState.HEATING, filtering=True),
            HEATER + FILTER,
        ),
        # The element cycles off once the water is up to temperature.
        (
            DeviceStatus(
                power=True, heater=HeaterState.TARGET_REACHED, filtering=True
            ),
            FILTER,
        ),
        (DeviceStatus(power=True, bubbles=BubblesLevel.OFF), 0),
        (DeviceStatus(power=True, bubbles=BubblesLevel.MEDIUM), BUBBLES),
        (DeviceStatus(power=True, bubbles=BubblesLevel.MAX), BUBBLES),
        (
            DeviceStatus(
                power=True,
                heater=HeaterState.HEATING,
                filtering=True,
                bubbles=BubblesLevel.MAX,
                jets=True,
            ),
            HEATER + FILTER + BUBBLES + DEFAULT_WATTAGES.jets,
        ),
    ],
)
def test_estimate_power_watts(status: DeviceStatus, expected: int | None) -> None:
    """Each running component adds its wattage; unknown power is unknown."""
    assert estimate_power_watts(status) == expected


def test_estimate_power_watts_custom_wattages() -> None:
    """Wattages can be overridden."""
    status = DeviceStatus(power=True, jets=True, filtering=True)
    assert estimate_power_watts(status, SpaWattages(jets=1000, filter=60)) == 1060


def _coordinator(status: DeviceStatus) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = BestwayApiResults(devices={_DEVICE_ID: status})
    coordinator.last_update_success = True
    return coordinator


def _energy_sensor(coordinator: MagicMock) -> EstimatedEnergySensor:
    return EstimatedEnergySensor(
        coordinator, MagicMock(), _DEVICE_ID, _ESTIMATED_ENERGY_SENSOR
    )


class _Clock:
    """Stands in for time.monotonic() in the sensor module."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    clock = _Clock()
    with patch("custom_components.bestway.sensor.monotonic", new=clock):
        yield clock


def test_power_sensor_reports_estimate() -> None:
    """The power sensor reads the estimate and exposes its assumptions."""
    status = DeviceStatus(power=True, heater=HeaterState.HEATING, filtering=True)
    sensor = EstimatedPowerSensor(
        _coordinator(status), MagicMock(), _DEVICE_ID, _ESTIMATED_POWER_SENSOR
    )

    assert sensor.native_value == HEATER + FILTER
    assert sensor.extra_state_attributes["heater_watts"] == HEATER


def test_energy_charges_each_interval_at_its_starting_power(clock) -> None:
    """An interval is charged at the state the spa was in during it.

    Heating for an hour, then switching the heater off: the hour counts
    at heater + filter, and nothing after the switch-off counts at the
    heater rate.
    """
    heating = DeviceStatus(power=True, heater=HeaterState.HEATING, filtering=True)
    coordinator = _coordinator(heating)
    sensor = _energy_sensor(coordinator)
    sensor._sample()

    for _ in range(120):  # an hour of 30-second polls
        clock.now += 30
        sensor._sample()

    coordinator.data = BestwayApiResults(
        devices={_DEVICE_ID: DeviceStatus(power=True, filtering=True)}
    )
    clock.now += 30
    sensor._sample()
    assert sensor.native_value == pytest.approx(
        (HEATER + FILTER) * 1.0 / 1000 + (HEATER + FILTER) * 30 / 3_600_000, abs=1e-3
    )

    total_after_heating = sensor._energy_kwh
    clock.now += 600
    sensor._sample()
    # Ten minutes at filter-only power was added, not heater power.
    assert sensor._energy_kwh - total_after_heating == pytest.approx(FILTER / 6000)


def test_energy_skips_long_gaps(clock) -> None:
    """A gap between updates longer than the limit isn't counted."""
    heating = DeviceStatus(power=True, heater=HeaterState.HEATING)
    sensor = _energy_sensor(_coordinator(heating))
    sensor._sample()

    clock.now += MAX_ENERGY_SAMPLE_GAP_S + 1
    sensor._sample()

    assert sensor.native_value == 0


def test_energy_not_counted_while_updates_fail(clock) -> None:
    """While the coordinator is failing, the last state is not assumed."""
    coordinator = _coordinator(DeviceStatus(power=True, heater=HeaterState.HEATING))
    sensor = _energy_sensor(coordinator)
    coordinator.last_update_success = False
    sensor._sample()

    clock.now += 60
    sensor._sample()

    assert sensor.native_value == 0


@pytest.mark.parametrize(
    ("restored", "expected"),
    [(12.5, 12.5), ("not a number", 0.0), (None, 0.0)],
)
async def test_energy_total_restored_after_restart(
    clock, restored: object, expected: float
) -> None:
    """The running total continues from the last recorded value."""
    sensor = _energy_sensor(_coordinator(DeviceStatus(power=True)))
    last = (
        None
        if restored is None
        else SensorExtraStoredData(native_value=restored, native_unit_of_measurement="kWh")
    )

    with (
        patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ),
        patch.object(
            EstimatedEnergySensor,
            "async_get_last_sensor_data",
            new=AsyncMock(return_value=last),
        ),
    ):
        await sensor.async_added_to_hass()

    assert sensor.native_value == expected
