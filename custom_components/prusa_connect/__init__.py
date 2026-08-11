"""The Prusa Connect integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PrusaConnectAPI
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN
from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.IMAGE,
]


@dataclass
class PrusaConnectData:
    """Runtime data for a Prusa Connect config entry."""

    api: PrusaConnectAPI
    printer_coordinator: PrusaConnectPrinterCoordinator
    job_coordinator: PrusaConnectJobCoordinator


type PrusaConnectConfigEntry = ConfigEntry[PrusaConnectData]


async def async_setup_entry(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> bool:
    """Set up Prusa Connect from a config entry."""
    session = async_get_clientsession(hass)

    async def _async_update_tokens(tokens: dict) -> None:
        """Persist updated tokens to the config entry."""
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: tokens["access_token"],
                CONF_REFRESH_TOKEN: tokens["refresh_token"],
            },
        )

    api = PrusaConnectAPI(
        session=session,
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        token_update_callback=_async_update_tokens,
    )

    printer_coordinator = PrusaConnectPrinterCoordinator(hass, entry, api)
    job_coordinator = PrusaConnectJobCoordinator(hass, entry, api)

    await printer_coordinator.async_config_entry_first_refresh()
    await job_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PrusaConnectData(
        api=api,
        printer_coordinator=printer_coordinator,
        job_coordinator=job_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    from .services import async_setup_services

    await async_setup_services(hass)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> bool:
    """Unload a Prusa Connect config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
