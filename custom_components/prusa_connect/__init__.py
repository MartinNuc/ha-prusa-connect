"""The Prusa Connect integration."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import logging.handlers

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

# TEMPORARY diagnostic. Home Assistant here writes no log file and the add-on
# is refused the supervisor's core-log endpoint, so a stream failure inside HA
# leaves no trace beyond one warning — while the same code called directly
# connects and streams. This writes what HA's own attempt does, ICE included,
# somewhere readable. Remove once the camera stream is understood.
_DEBUG_LOG = "prusa_connect_debug.log"
_debug_attached = False


def _attach_debug_log(hass: HomeAssistant) -> None:
    """Mirror our logging, and aioice's, into a file under config/."""
    global _debug_attached  # noqa: PLW0603 - one handler per process
    if _debug_attached:
        return
    try:
        handler = logging.handlers.RotatingFileHandler(
            hass.config.path(_DEBUG_LOG), maxBytes=4_000_000, backupCount=1
        )
    except OSError as err:
        _LOGGER.debug("Could not open the debug log: %s", err)
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    for name, level in (
        ("custom_components.prusa_connect", logging.DEBUG),
        # The whole of aioice, not just ice: a relay-to-relay pair failing
        # between two addresses on the same TURN server is a TURN-layer
        # problem, and aioice.turn is where allocations and channel binds are
        # reported.
        # DEBUG on aioice.turn logs every packet in and out of the relay, which
        # is the only way to see whether our connectivity checks are actually
        # sent and whether the camera answers them.
        ("aioice", logging.DEBUG),
        ("aiortc", logging.INFO),
    ):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > level:
            logger.setLevel(level)
    _log_stun_errors()
    _debug_attached = True
    _LOGGER.info("Writing a diagnostic log to %s", hass.config.path(_DEBUG_LOG))


def _log_stun_errors() -> None:
    """Surface STUN/TURN ERROR-CODE, which aioice never logs.

    TEMPORARY, and paired with the debug log above. A TURN allocation that is
    refused shows up in aioice only as "Class.ERROR" with no reason, so a
    rejected credential and an exhausted quota are indistinguishable — the two
    have been confused for a day. This wraps the parser to log the code and
    changes nothing else; it is removed with the rest of the diagnostics.
    """
    try:
        import aioice.stun as stun
    except ImportError:  # pragma: no cover
        return
    if getattr(stun.parse_message, "_prusa_wrapped", False):
        return

    original = stun.parse_message

    def _logging_parse(data, integrity_key=None):  # noqa: ANN001, ANN202
        message = original(data, integrity_key)
        error = message.attributes.get("ERROR-CODE")
        if error:
            _LOGGER.warning("STUN/TURN refused: %s (%s)", error, message.message_method)
        return message

    _logging_parse._prusa_wrapped = True
    stun.parse_message = _logging_parse

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
    # The options this entry was set up with, so an update listener can tell an
    # options change from a token being written back to the same entry.
    options: dict = field(default_factory=dict)


type PrusaConnectConfigEntry = ConfigEntry[PrusaConnectData]


async def async_setup_entry(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> bool:
    """Set up Prusa Connect from a config entry."""
    _attach_debug_log(hass)
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
        options=dict(entry.options),
    )

    if entry.options.get(CONF_TIMELAPSE, DEFAULT_TIMELAPSE):
        entry.runtime_data.timelapse = await _async_setup_timelapse(
            hass, api, printer_coordinator, job_coordinator
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
    job_coordinator: PrusaConnectJobCoordinator,
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

    recorder = TimelapseRecorder(
        hass, api, printer_coordinator, cameras, job_coordinator
    )
    recorder.async_start()
    return recorder


async def _async_options_updated(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> None:
    """Apply an options change by reloading — and only an options change.

    This listener fires on *any* update to the config entry, and the access
    token is written back to that same entry every time it is refreshed. So
    reloading unconditionally restarted the whole integration on a schedule set
    by token expiry: observed at almost exactly two-hour intervals, each one
    ending the timelapse recording in progress and starting a fresh one. A
    seven-hour print came out as a 12-second video and some fragments.
    """
    data = getattr(entry, "runtime_data", None)
    if data is not None and data.options == dict(entry.options):
        return
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
