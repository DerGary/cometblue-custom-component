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
        try:
            async with self.device:
                if not self.device.connected:
                    raise ConfigEntryNotReady(
                        f"Failed to connect to '{self.device.device.address}'"
                    )
                result = await getattr(self.device, function)(**payload)
        except ValueError as err:
            raise ServiceValidationError(
                f"Invalid payload '{payload}' for '{caller_entity_id}': {err}"
            ) from err
        except BleakError as err:
            raise HomeAssistantError(
                f"Error sending command '{payload}' to '{caller_entity_id}': {err}"
            ) from err
        except TimeoutError as err:
            raise HomeAssistantError(
                f"Error sending command '{payload}' to '{caller_entity_id}': {err}"
            ) from err

    async def _async_update_data(self) -> dict[str, bytes]:
        """Poll the device."""
        data: dict = {}

        try:
            async with self.device:
                if not self.device.connected:
                    raise ConfigEntryNotReady(
                        f"Failed to connect to '{self.device.device.address}'"
                    )
                retrieved_temperatures = await self.device.get_temperature_async()
                data = {
                    "battery": await self.device.get_battery_async(),
                    "holiday": await self.device.get_holiday_async(1),
                    # If one value was not retrieved correctly, keep the old value
                    **{
                        k: retrieved_temperatures.get(k) or self.data.get(k)
                        for k
                        in CONF_ALL_TEMPERATURES
                    },
                }
                # Reset failed update count if all values were retrieved correctly
                self.failed_update_count = 0
        except InvalidByteValueError as ex:
            # allow invalid bytes until RETRY_COUNT is reached
            if self.failed_update_count < RETRY_COUNT and self.data:
                self.failed_update_count += 1
                return self.data
            raise UpdateFailed(f"Error in device response: {ex}") from ex
        except Exception as ex:
            self.failed_update_count += 1
            raise UpdateFailed(f"({type(ex).__name__}) {ex}") from ex
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
