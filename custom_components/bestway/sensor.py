"""Home Assistant sensor descriptions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from time import monotonic
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import DOMAIN, Icon
from .coordinator import BestwayUpdateCoordinator
from .energy import DEFAULT_WATTAGES, estimate_power_watts
from .entity import BestwayEntity
from .features import VersionSensorSet, features_for
from .model import BestwayDevice, DeviceStatus


@dataclass(frozen=True, kw_only=True)
class BestwayDeviceSensorEntityDescription(SensorEntityDescription):
    """Sensor entity description for a value read from device metadata
    (`BestwayDevice`), not from polled/pushed device status.

    `name` carries a `{prefix}` placeholder filled in per-device from
    `DeviceFeatures.name_prefix` (e.g. "Spa", "Pool Filter") - it varies by
    device type, so it can't be baked into a fixed module-level constant
    the way switch.py's descriptions are.
    """

    value_fn: Callable[[BestwayDevice], StateType]


@dataclass(frozen=True, kw_only=True)
class BestwayStateSensorEntityDescription(SensorEntityDescription):
    """Sensor entity description for a value read from typed device status.

    `name` carries the same `{prefix}` placeholder as
    `BestwayDeviceSensorEntityDescription`.
    """

    value_fn: Callable[[DeviceStatus], StateType]


# V01 Gizwits devices: MCU and WiFi versions from the device object.
_GIZWITS_VERSION_SENSORS: tuple[BestwayDeviceSensorEntityDescription, ...] = (
    BestwayDeviceSensorEntityDescription(
        key="protocol_version",
        name="{prefix} Protocol Version",
        icon=Icon.PROTOCOL,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.protocol_version,
    ),
    BestwayDeviceSensorEntityDescription(
        key="mcu_soft_version",
        name="{prefix} MCU Software Version",
        icon=Icon.SOFTWARE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.mcu_soft_version,
    ),
    BestwayDeviceSensorEntityDescription(
        key="mcu_hard_version",
        name="{prefix} MCU Hardware Version",
        icon=Icon.HARDWARE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.mcu_hard_version,
    ),
    BestwayDeviceSensorEntityDescription(
        key="wifi_soft_version",
        name="{prefix} Wi-Fi Software Version",
        icon=Icon.SOFTWARE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.wifi_soft_version,
    ),
    BestwayDeviceSensorEntityDescription(
        key="wifi_hard_version",
        name="{prefix} Wi-Fi Hardware Version",
        icon=Icon.HARDWARE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.wifi_hard_version,
    ),
)

# V02 devices (AWS IoT, SmartSpa): WiFi, TRD, OTA versions from shadow state.
_SHADOW_VERSION_SENSORS: tuple[BestwayStateSensorEntityDescription, ...] = (
    BestwayStateSensorEntityDescription(
        key="wifi_version",
        name="{prefix} WiFi Version",
        icon=Icon.SOFTWARE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda status: status.wifi_version,
    ),
    BestwayStateSensorEntityDescription(
        key="trd_version",
        name="{prefix} TRD Version",
        icon=Icon.SOFTWARE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda status: status.trd_version,
    ),
    BestwayStateSensorEntityDescription(
        key="ota_status",
        name="{prefix} OTA Status",
        icon=Icon.PROTOCOL,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda status: status.ota_status,
    ),
)


_ESTIMATED_POWER_SENSOR = BestwayStateSensorEntityDescription(
    key="estimated_power",
    name="{prefix} Estimated Power",
    native_unit_of_measurement=UnitOfPower.WATT,
    device_class=SensorDeviceClass.POWER,
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=estimate_power_watts,
)

_ESTIMATED_ENERGY_SENSOR = SensorEntityDescription(
    key="estimated_energy",
    name="{prefix} Estimated Energy",
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    suggested_display_precision=2,
)

# Gaps between coordinator updates longer than this aren't counted toward
# estimated energy: the last known state is too stale to assume it held
# throughout (e.g. Home Assistant was stopped, or the cloud API was down).
MAX_ENERGY_SAMPLE_GAP_S = 15 * 60


def _named[
    T: (BestwayDeviceSensorEntityDescription, BestwayStateSensorEntityDescription)
](description: T, name_prefix: str) -> T:
    """Fill in a description's `{prefix}` name placeholder for one device."""
    return replace(description, name=str(description.name).format(prefix=name_prefix))


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    coordinator: BestwayUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[BestwayEntity] = []

    for device_id, device_info in coordinator.api.devices.items():
        features = features_for(device_info, config_entry.options)
        name_prefix = features.name_prefix

        if features.version_sensors == VersionSensorSet.GIZWITS:
            entities.extend(
                DeviceSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    _named(description, name_prefix),
                )
                for description in _GIZWITS_VERSION_SENSORS
            )
        else:
            entities.extend(
                StateSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    _named(description, name_prefix),
                )
                for description in _SHADOW_VERSION_SENSORS
            )

        if features.energy_estimate:
            entities.append(
                EstimatedPowerSensor(
                    coordinator,
                    config_entry,
                    device_id,
                    _named(_ESTIMATED_POWER_SENSOR, name_prefix),
                )
            )
            entities.append(
                EstimatedEnergySensor(
                    coordinator,
                    config_entry,
                    device_id,
                    replace(
                        _ESTIMATED_ENERGY_SENSOR,
                        name=str(_ESTIMATED_ENERGY_SENSOR.name).format(
                            prefix=name_prefix
                        ),
                    ),
                )
            )

    async_add_entities(entities)


class DeviceSensor(BestwayEntity, SensorEntity):
    """A sensor based on device metadata."""

    entity_description: BestwayDeviceSensorEntityDescription

    def __init__(
        self,
        coordinator: BestwayUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        description: BestwayDeviceSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> StateType:
        """Return the relevant property."""
        if (device := self.bestway_device) is not None:
            return self.entity_description.value_fn(device)
        return None


class StateSensor(BestwayEntity, SensorEntity):
    """A sensor based on typed device status fields (for V02 devices)."""

    entity_description: BestwayStateSensorEntityDescription

    def __init__(
        self,
        coordinator: BestwayUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        description: BestwayStateSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> StateType:
        """Return value from typed device status."""
        if self.status is not None:
            return self.entity_description.value_fn(self.status)
        return None


def _wattage_attributes() -> dict[str, Any]:
    """The per-component wattages an estimate assumes, so users can see
    what the figure is based on."""
    return {f"{name}_watts": watts for name, watts in asdict(DEFAULT_WATTAGES).items()}


class EstimatedPowerSensor(StateSensor):
    """Estimated current power draw, from which components are running."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the wattages the estimate assumes."""
        return _wattage_attributes()


class EstimatedEnergySensor(BestwayEntity, RestoreSensor):
    """Estimated cumulative energy use, for the Home Assistant energy dashboard.

    Each coordinator update closes the interval since the previous one,
    charging it at the power estimated at the *start* of that interval -
    the state the spa was actually in while the time passed. The running
    total is restored across restarts.
    """

    def __init__(
        self,
        coordinator: BestwayUpdateCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._energy_kwh = 0.0
        self._interval_watts: int | None = None
        self._interval_start: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the running total and start the first interval."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float | Decimal):
            self._energy_kwh = float(last.native_value)
        self._sample()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Account for the interval that just ended, then update state."""
        self._sample()
        super()._handle_coordinator_update()

    def _sample(self) -> None:
        now = monotonic()
        if self._interval_watts and self._interval_start is not None:
            elapsed_s = now - self._interval_start
            if elapsed_s <= MAX_ENERGY_SAMPLE_GAP_S:
                self._energy_kwh += self._interval_watts * elapsed_s / 3_600_000

        self._interval_start = now
        status = self.status if self.coordinator.last_update_success else None
        self._interval_watts = None if status is None else estimate_power_watts(status)

    @property
    def native_value(self) -> float:
        """Return the estimated energy used so far, in kWh."""
        return round(self._energy_kwh, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the wattages the estimate assumes."""
        return _wattage_attributes()
