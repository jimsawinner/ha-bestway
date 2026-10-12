"""Estimated power draw of a spa, from which components are running.

The devices report no electrical measurements, so this is a nameplate
estimate: each running component contributes a fixed wattage. It is
accurate enough to put spa running costs on the Home Assistant energy
dashboard, not to replace a real meter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_BUBBLES_WATTS,
    CONF_FILTER_WATTS,
    CONF_HEATER_WATTS,
    CONF_JETS_WATTS,
)
from .model import BubblesLevel, DeviceStatus, HeaterState


@dataclass(frozen=True)
class SpaWattages:
    """Assumed draw of each spa component while it runs, in watts."""

    heater: int = 2050
    bubbles: int = 800
    filter: int = 50
    # No figure is known for the Hydrojet jet pump yet, so jets add nothing
    # rather than a guess.
    jets: int = 0


DEFAULT_WATTAGES = SpaWattages()


def wattages_from_options(options: Mapping[str, Any]) -> SpaWattages:
    """The wattages configured in the config entry options, falling back to
    the defaults for any not set.

    Values arrive as floats from the options form's number selectors.
    """

    def watts(key: str, default: int) -> int:
        return int(options.get(key, default))

    return SpaWattages(
        heater=watts(CONF_HEATER_WATTS, DEFAULT_WATTAGES.heater),
        bubbles=watts(CONF_BUBBLES_WATTS, DEFAULT_WATTAGES.bubbles),
        filter=watts(CONF_FILTER_WATTS, DEFAULT_WATTAGES.filter),
        jets=watts(CONF_JETS_WATTS, DEFAULT_WATTAGES.jets),
    )


def estimate_power_watts(
    status: DeviceStatus, wattages: SpaWattages = DEFAULT_WATTAGES
) -> int | None:
    """Estimated current draw in watts, or None if the spa's state is unknown.

    Only HeaterState.HEATING counts the heater: once the water reaches its
    target the element cycles off while the heater stays "on". A component
    whose state is unknown (None) counts as not running.
    """
    if status.power is None:
        return None
    if not status.power:
        return 0

    watts = 0
    if status.heater == HeaterState.HEATING:
        watts += wattages.heater
    if status.filtering:
        watts += wattages.filter
    if status.bubbles not in (None, BubblesLevel.OFF):
        watts += wattages.bubbles
    if status.jets:
        watts += wattages.jets
    return watts
