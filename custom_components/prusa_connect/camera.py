"""Camera platform for Prusa Connect — printer snapshots."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PrusaConnectAPI
from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry

_LOGGER = logging.getLogger(__name__)

# Connect stores the latest frame pushed by the camera; the common trigger
# scheme uploads every 30 seconds, so polling faster gains nothing.
FRAME_INTERVAL = 30.0


class PrusaConnectCamera(PrusaConnectEntity, Camera):
    """Camera entity serving the latest snapshot stored in Connect."""

    _attr_is_streaming = False

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        api: PrusaConnectAPI,
        printer_uuid: str,
        camera: dict,
    ) -> None:
        """Initialize the camera entity."""
        PrusaConnectEntity.__init__(self, coordinator, printer_uuid)
        Camera.__init__(self)
        self._api = api
        self._camera_id = camera["id"]
        self._attr_name = camera.get("name") or "Camera"
        self._attr_unique_id = f"{printer_uuid}_camera_{self._camera_id}"
        self._attr_frame_interval = FRAME_INTERVAL

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the most recent snapshot as bytes."""
        return await self._api.get_camera_snapshot(self._camera_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect cameras."""
    data = entry.runtime_data
    printer_coordinator = data.printer_coordinator

    entities: list[PrusaConnectCamera] = []

    for printer_uuid in printer_coordinator.data:
        try:
            cameras = await data.api.get_printer_cameras(printer_uuid)
        except Exception as err:  # noqa: BLE001 - one bad printer must not
            # prevent the remaining platforms from loading.
            _LOGGER.warning(
                "Could not list cameras for printer %s: %s", printer_uuid, err
            )
            continue

        entities.extend(
            PrusaConnectCamera(printer_coordinator, data.api, printer_uuid, camera)
            for camera in cameras
            if camera.get("id") is not None
        )

    async_add_entities(entities)
