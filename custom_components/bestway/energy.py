"""Estimated power draw of a spa, from which components are running.

The devices report no electrical measurements, so this is a nameplate
estimate: each running component contributes a fixed wattage. It is
accurate enough to put spa running costs on the Home Assistant energy
dashboard, not to replace a real meter.
"""

from __future__ import annotations

from dataclasses import dataclass

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
