"""Home Assistant sensor descriptions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfPower,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import BestwayUpdateCoordinator
from .bestway.model import BestwayDevice, BestwayDeviceType
from .const import BACKEND_GIZWITS, DOMAIN, Icon
from .entity import BestwayEntity

from datetime import datetime, timezone

import logging

_LOGGER = logging.getLogger(__name__)

ESTIMATED_HEATER_WATTS = 2050
ESTIMATED_BUBBLES_WATTS = 800
ESTIMATED_FILTER_WATTS = 50
ESTIMATED_JETS_WATTS = 0  # set later if you want Hydrojet jet estimate


@dataclass
class DeviceSensorDescription:
    """An entity description with a function that describes how to derive a value."""

    entity_description: SensorEntityDescription
    value_fn: Callable[[BestwayDevice], StateType]

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    coordinator: BestwayUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[BestwayEntity] = []

    for device_id, device_info in coordinator.api.devices.items():
        _LOGGER.warning(
            "Bestway sensor setup: device_id=%s device_type=%s",
            device_id,
            device_info.device_type,
        )

        name_prefix = "Bestway"
        if device_info.device_type in [
            BestwayDeviceType.AIRJET_SPA,
            BestwayDeviceType.HYDROJET_SPA,
            BestwayDeviceType.HYDROJET_PRO_SPA,
            BestwayDeviceType.AIRJET_V02,
            BestwayDeviceType.ULTRAFIT_AIRJET_V02,
            BestwayDeviceType.HYDROJET_V02,
            BestwayDeviceType.HYDROJET_PRO_V02,
        ]:
            name_prefix = "Spa"
            entities.extend(
                [
                    EstimatedPowerSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        name=f"{name_prefix} Estimated Power",
                    ),
                    EstimatedEnergySensor(
                        coordinator,
                        config_entry,
                        device_id,
                        name=f"{name_prefix} Estimated Energy",
                    ),
                ]
            )
        elif device_info.device_type == BestwayDeviceType.POOL_FILTER:
            name_prefix = "Pool Filter"

        # Version sensors - different for V01 vs V02
        if device_info.backend == BACKEND_GIZWITS:
            # V01 Gizwits devices: Show MCU and WiFi versions from device object
            entities.extend(
                [
                    DeviceSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        sensor_description=DeviceSensorDescription(
                            SensorEntityDescription(
                                key="protocol_version",
                                name=f"{name_prefix} Protocol Version",
                                icon=Icon.PROTOCOL,
                                entity_category=EntityCategory.DIAGNOSTIC,
                            ),
                            lambda device: device.protocol_version,
                        ),
                    ),
                    DeviceSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        sensor_description=DeviceSensorDescription(
                            SensorEntityDescription(
                                key="mcu_soft_version",
                                name=f"{name_prefix} MCU Software Version",
                                icon=Icon.SOFTWARE,
                                entity_category=EntityCategory.DIAGNOSTIC,
                            ),
                            lambda device: device.mcu_soft_version,
                        ),
                    ),
                    DeviceSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        sensor_description=DeviceSensorDescription(
                            SensorEntityDescription(
                                key="mcu_hard_version",
                                name=f"{name_prefix} MCU Hardware Version",
                                icon=Icon.HARDWARE,
                                entity_category=EntityCategory.DIAGNOSTIC,
                            ),
                            lambda device: device.mcu_hard_version,
                        ),
                    ),
                    DeviceSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        sensor_description=DeviceSensorDescription(
                            SensorEntityDescription(
                                key="wifi_soft_version",
                                name=f"{name_prefix} Wi-Fi Software Version",
                                icon=Icon.SOFTWARE,
                                entity_category=EntityCategory.DIAGNOSTIC,
                            ),
                            lambda device: device.wifi_soft_version,
                        ),
                    ),
                    DeviceSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        sensor_description=DeviceSensorDescription(
                            SensorEntityDescription(
                                key="wifi_hard_version",
                                name=f"{name_prefix} Wi-Fi Hardware Version",
                                icon=Icon.HARDWARE,
                                entity_category=EntityCategory.DIAGNOSTIC,
                            ),
                            lambda device: device.wifi_hard_version,
                        ),
                    ),
                ]
            )
        else:
            # V02 AWS IoT devices: Show WiFi, TRD, OTA from shadow state
            entities.extend(
                [
                    StateSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        SensorEntityDescription(
                            key="wifi_version",
                            name=f"{name_prefix} WiFi Version",
                            icon=Icon.SOFTWARE,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        "wifi_version",
                    ),
                    StateSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        SensorEntityDescription(
                            key="trd_version",
                            name=f"{name_prefix} TRD Version",
                            icon=Icon.SOFTWARE,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        "trd_version",
                    ),
                    StateSensor(
                        coordinator,
                        config_entry,
                        device_id,
                        SensorEntityDescription(
                            key="ota_status",
                            name=f"{name_prefix} OTA Status",
                            icon=Icon.PROTOCOL,
                            entity_category=EntityCategory.DIAGNOSTIC,
                        ),
                        "ota_status",
                    ),
                ]
            )

    async_add_entities(entities)

def calculate_estimated_power_watts(attrs: dict) -> int:
    """Calculate estimated power draw from Bestway attrs."""
    watts = 0

    heater_active = False

    if "heat_power" in attrs:
        heater_active = bool(attrs.get("heat_power")) and not bool(
            attrs.get("heat_temp_reach")
        )
    elif "heat" in attrs:
        heater_active = (
            int(attrs.get("heat") or 0) > 0 and int(attrs.get("heat") or 0) != 4
        )

    if heater_active:
        watts += ESTIMATED_HEATER_WATTS

    filter_active = False

    if "filter_power" in attrs:
        filter_active = bool(attrs.get("filter_power"))
    elif "filter" in attrs:
        filter_active = int(attrs.get("filter") or 0) == 2

    if filter_active:
        watts += ESTIMATED_FILTER_WATTS

    bubbles_active = False

    if "wave_power" in attrs:
        bubbles_active = bool(attrs.get("wave_power"))
    elif "wave" in attrs:
        bubbles_active = int(attrs.get("wave") or 0) > 0

    if bubbles_active:
        watts += ESTIMATED_BUBBLES_WATTS

    if bool(attrs.get("jet")):
        watts += ESTIMATED_JETS_WATTS

    return watts

class DeviceSensor(BestwayEntity, SensorEntity):
    """A sensor based on device metadata."""

    sensor_description: DeviceSensorDescription

    def __init__(
        self,
        coordinator: BestwayUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        sensor_description: DeviceSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self.sensor_description = sensor_description
        self.entity_description = sensor_description.entity_description
        self._attr_unique_id = f"{device_id}_{self.entity_description.key}"

    @property
    def native_value(self) -> StateType:
        """Return the relevant property."""
        if (device := self.bestway_device) is not None:
            return self.sensor_description.value_fn(device)
        return None


class StateSensor(BestwayEntity, SensorEntity):
    """A sensor based on device state attributes (for V02 devices)."""

    def __init__(
        self,
        coordinator: BestwayUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        entity_description: SensorEntityDescription,
        state_key: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = entity_description
        self._state_key = state_key
        self._attr_unique_id = f"{device_id}_{entity_description.key}"

    @property
    def native_value(self) -> StateType:
        """Return value from state attrs."""
        if self.status is not None:
            return self.status.attrs.get(self._state_key)
        return None


class EstimatedPowerSensor(BestwayEntity, SensorEntity):
    """Estimated instantaneous power consumption for a spa."""

    entity_description = SensorEntityDescription(
        key="estimated_power",
        name="Estimated Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
    )

    def __init__(
        self,
        coordinator: BestwayUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        name: str,
    ) -> None:
        """Initialize the estimated power sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self._attr_name = name
        self._attr_unique_id = f"{device_id}_estimated_power"
        self._attr_has_entity_name = True

    @property
    def native_value(self) -> int | None:
        """Return estimated current power draw in watts."""
        if self.status is None:
            return None

        return calculate_estimated_power_watts(self.status.attrs)

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Return the assumptions used by this estimated sensor."""
        return {
            "calculation": "estimated",
            "heater_watts": ESTIMATED_HEATER_WATTS,
            "bubbles_watts": ESTIMATED_BUBBLES_WATTS,
            "filter_watts": ESTIMATED_FILTER_WATTS,
            "jets_watts": ESTIMATED_JETS_WATTS,
        }

class EstimatedEnergySensor(BestwayEntity, RestoreEntity, SensorEntity):
    """Estimated cumulative energy consumption for a spa."""

    entity_description = SensorEntityDescription(
        key="estimated_energy",
        name="Estimated Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
    )

    def __init__(
        self,
        coordinator: BestwayUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        name: str,
    ) -> None:
        """Initialize the estimated energy sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self._attr_name = name
        self._attr_unique_id = f"{device_id}_estimated_energy"
        self._attr_has_entity_name = True
        self._energy_kwh: float = 0.0
        self._last_update: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Restore previous energy total after Home Assistant restart."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        if (
            last_state is not None
            and last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        ):
            try:
                self._energy_kwh = float(last_state.state)
            except ValueError:
                self._energy_kwh = 0.0

        self._last_update = datetime.now(timezone.utc)

    @property
    def native_value(self) -> float | None:
        """Return estimated cumulative energy in kWh."""
        if self.status is None:
            return round(self._energy_kwh, 3)

        now = datetime.now(timezone.utc)

        if self._last_update is None:
            self._last_update = now
            return round(self._energy_kwh, 3)

        elapsed_hours = (now - self._last_update).total_seconds() / 3600

        if 0 < elapsed_hours < 1:
            watts = calculate_estimated_power_watts(self.status.attrs)
            self._energy_kwh += watts * elapsed_hours / 1000

        self._last_update = now

        return round(self._energy_kwh, 3)

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Return the assumptions used by this estimated sensor."""
        return {
            "calculation": "estimated",
            "source": "estimated_power",
            "heater_watts": ESTIMATED_HEATER_WATTS,
            "bubbles_watts": ESTIMATED_BUBBLES_WATTS,
            "filter_watts": ESTIMATED_FILTER_WATTS,
            "jets_watts": ESTIMATED_JETS_WATTS,
        }