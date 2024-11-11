"""Provides the DataUpdateCoordinator."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from bleak import BleakError
from cometblue import AsyncCometBlue, InvalidByteValueError

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_ALL_TEMPERATURES

SCAN_INTERVAL = timedelta(minutes=5)
RETRY_COUNT = 3
LOGGER = logging.getLogger(__name__)


class DeviceUnavailable(HomeAssistantError):
    """Raised if device can't be found."""


class CometBlueDataUpdateCoordinator(DataUpdateCoordinator[dict[str, bytes]]):
    """Class to manage fetching data."""

    failed_update_count: int = 0

    def __init__(
        self,
        hass: HomeAssistant,
        cometblue: AsyncCometBlue,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize global data updater."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=f"Comet Blue {cometblue.client.address}",
            update_interval=SCAN_INTERVAL,
        )
        self.device: AsyncCometBlue = cometblue
        self.address = cometblue.client.address
        self.data: dict[str, Any] = {}
        self.device_info = device_info

    async def send_command(
        self, function: str, payload: dict[str, Any], caller_entity_id: str
    ) -> dict[str, Any] | None:
        """Send command to device."""

        LOGGER.debug("Updating device with '%s' from '%s'", caller_entity_id, payload)
        retry_count = 0
        while retry_count < RETRY_COUNT:
            try:
                async with self.device:
                    if not self.device.connected:
                        raise ConfigEntryNotReady(
                            f"Failed to connect to '{self.device.device.address}'"
                        )
                    return await getattr(self.device, function)(**payload)
            except (InvalidByteValueError, TimeoutError, BleakError) as ex:  # noqa: PERF203
                retry_count += 1
                if retry_count >= RETRY_COUNT:
                    raise HomeAssistantError(
                        f"Error sending command '{payload}' to '{caller_entity_id}': {ex}"
                    ) from ex
                LOGGER.info(
                    "Retrying command '%s' to '%s' after %s (%s)",
                    payload,
                    caller_entity_id,
                    type(ex).__name__,
                    ex,
                )
            except ValueError as ex:
                raise ServiceValidationError(
                    f"Invalid payload '{payload}' for '{caller_entity_id}': {ex}"
                ) from ex

    async def _async_update_data(self) -> dict[str, bytes]:
        """Poll the device."""
        data: dict = {}

        retry_count = 0
        retrieved_temperatures = {}
        battery = 0
        holiday = {}

        while retry_count < RETRY_COUNT:
            try:
                async with self.device:
                    if not self.device.connected:
                        raise ConfigEntryNotReady(
                            f"Failed to connect to '{self.device.device.address}'"
                        )
                    # temperatures are required and must trigger a retry if not available
                    retrieved_temperatures = await self.device.get_temperature_async()
                    # battery and holiday are optional and should not trigger a retry
                    try:
                        battery = await self.device.get_battery_async()
                        holiday = await self.device.get_holiday_async(1)
                    except InvalidByteValueError as ex:
                        LOGGER.warning(
                            "Failed to retrieve optional data: %s (%s)",
                            type(ex).__name__,
                            ex,
                        )
            except (InvalidByteValueError, TimeoutError, BleakError) as ex:  # noqa: PERF203
                retry_count += 1
                if retry_count >= RETRY_COUNT:
                    raise UpdateFailed(f"Error retrieving data: {ex}") from ex
                LOGGER.info(
                    "Retrying after %s (%s)",
                    type(ex).__name__,
                    ex,
                )
            except Exception as ex:
                raise UpdateFailed(f"({type(ex).__name__}) {ex}") from ex

        # If one value was not retrieved correctly, keep the old value
        data = {
            "battery": battery or self.data.get("battery"),
            "holiday": holiday or self.data.get("holiday"),
            **{
                k: retrieved_temperatures.get(k) or self.data.get(k)
                for k in CONF_ALL_TEMPERATURES
            },
        }
        LOGGER.debug("Received data: %s", data)
        return data


class CometBlueBluetoothEntity(CoordinatorEntity[CometBlueDataUpdateCoordinator]):
    """Coordinator entity for CometBlue."""

    coordinator: CometBlueDataUpdateCoordinator
    _attr_has_entity_name = True

    def __init__(self, coordinator: CometBlueDataUpdateCoordinator) -> None:
        """Initialize coordinator entity."""
        super().__init__(coordinator)
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.failed_update_count < RETRY_COUNT
            and bluetooth.async_address_present(
                self.hass, self.coordinator.address, True
            )
        )

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()
