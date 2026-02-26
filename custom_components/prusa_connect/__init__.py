"""The Prusa Connect integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PrusaConnectAPI
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    DATA_API,
    DATA_JOB_COORDINATOR,
    DATA_PRINTER_COORDINATOR,
    DOMAIN,
)
from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator

_LOGGER = logging.getLogger(__name__)

type PrusaConnectConfigEntry = ConfigEntry

PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "button",
    "camera",
    "image",
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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

    # Set up coordinators
    printer_coordinator = PrusaConnectPrinterCoordinator(hass, api)
    job_coordinator = PrusaConnectJobCoordinator(hass, api)

    # Fetch initial data
    await printer_coordinator.async_config_entry_first_refresh()
    await job_coordinator.async_config_entry_first_refresh()

    # Store runtime data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_API: api,
        DATA_PRINTER_COORDINATOR: printer_coordinator,
        DATA_JOB_COORDINATOR: job_coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    await _async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Prusa Connect config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Set up Prusa Connect services (imported from services module)."""
    # Import here to avoid circular imports
    from .services import async_setup_services

    await async_setup_services(hass)
