"""The Prusa Connect integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PrusaConnectAPI
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TIMELAPSE,
    DEFAULT_TIMELAPSE,
)
from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator
from .timelapse import TimelapseRecorder

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
    timelapse: TimelapseRecorder | None = None


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

    if entry.options.get(CONF_TIMELAPSE, DEFAULT_TIMELAPSE):
        entry.runtime_data.timelapse = await _async_setup_timelapse(
            hass, api, printer_coordinator
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    from .services import async_setup_services

    await async_setup_services(hass)

    return True


async def _async_setup_timelapse(
    hass: HomeAssistant,
    api: PrusaConnectAPI,
    printer_coordinator: PrusaConnectPrinterCoordinator,
) -> TimelapseRecorder | None:
    """Start recording timelapses for every printer that has a camera."""
    cameras: dict[str, int] = {}
    for printer_uuid in printer_coordinator.data:
        try:
            found = await api.get_printer_cameras(printer_uuid)
        except Exception as err:  # noqa: BLE001 - one bad printer must not
            # stop the others being recorded.
            _LOGGER.warning(
                "Could not list cameras for printer %s: %s", printer_uuid, err
            )
            continue
        for camera in found:
            if camera.get("id") is not None:
                cameras[printer_uuid] = camera["id"]
                break

    if not cameras:
        _LOGGER.warning("Timelapse is enabled but no printer has a camera")
        return None

    recorder = TimelapseRecorder(hass, api, printer_coordinator, cameras)
    recorder.async_start()
    return recorder


async def _async_options_updated(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> None:
    """Apply an options change by reloading."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> bool:
    """Unload a Prusa Connect config entry."""
    recorder = entry.runtime_data.timelapse
    if recorder is not None:
        # Finishes rather than discards: a reload mid-print should still yield
        # the frames captured so far.
        await recorder.async_stop()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
