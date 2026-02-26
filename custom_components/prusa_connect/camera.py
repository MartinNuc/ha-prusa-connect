"""Camera platform for Prusa Connect — printer snapshots."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PrusaConnectConfigEntry
from .const import DATA_PRINTER_COORDINATOR, DOMAIN
from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

_LOGGER = logging.getLogger(__name__)


class PrusaConnectCamera(PrusaConnectEntity, Camera):
    """Camera entity showing the printer's latest snapshot."""

    _attr_translation_key = "camera"
    _attr_is_streaming = False

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        printer_uuid: str,
        camera_index: int = 0,
    ) -> None:
        """Initialize the camera entity."""
        PrusaConnectEntity.__init__(self, coordinator, printer_uuid)
        Camera.__init__(self)
        self._camera_index = camera_index
        suffix = f"_camera_{camera_index}" if camera_index > 0 else "_camera"
        self._attr_unique_id = f"{printer_uuid}{suffix}"
        self._attr_frame_interval = 30

    def _get_snapshot_url(self) -> str | None:
        """Get the snapshot URL from printer data."""
        printer = self._printer_data

        # Try cameras list first
        cameras = printer.get("cameras") or []
        if cameras and self._camera_index < len(cameras):
            cam = cameras[self._camera_index]
            return cam.get("snapshotUrl") or cam.get("imageUrl")

        # Fall back to top-level snapshot URL
        return printer.get("snapshotUrl") or printer.get("cameraUrl")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the current camera snapshot as bytes."""
        url = self._get_snapshot_url()
        if not url:
            return None

        try:
            # Prusa snapshot URLs are pre-signed, no auth needed
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    _LOGGER.debug(
                        "Failed to fetch snapshot for %s: HTTP %s",
                        self._printer_uuid,
                        resp.status,
                    )
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Error fetching snapshot for %s: %s", self._printer_uuid, err)

        return None

    @property
    def available(self) -> bool:
        """Return True if we have a snapshot URL."""
        if not super().available:
            return False
        return self._get_snapshot_url() is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Prusa Connect cameras."""
    data = hass.data[DOMAIN][entry.entry_id]
    printer_coordinator: PrusaConnectPrinterCoordinator = data[DATA_PRINTER_COORDINATOR]

    entities: list[PrusaConnectCamera] = []

    for printer_uuid, printer_data in printer_coordinator.data.items():
        cameras = printer_data.get("cameras") or []
        if cameras:
            for idx in range(len(cameras)):
                entities.append(
                    PrusaConnectCamera(printer_coordinator, printer_uuid, idx)
                )
        else:
            # Create a single camera entity using the top-level snapshot URL
            snapshot_url = printer_data.get("snapshotUrl") or printer_data.get(
                "cameraUrl"
            )
            if snapshot_url:
                entities.append(
                    PrusaConnectCamera(printer_coordinator, printer_uuid)
                )

    async_add_entities(entities)
