"""Image platform for Prusa Connect — print preview thumbnails."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PrusaConnectConfigEntry
from .const import DATA_JOB_COORDINATOR, DATA_PRINTER_COORDINATOR, DOMAIN
from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity


class PrusaConnectPrintPreview(PrusaConnectEntity, ImageEntity):
    """Image entity showing the thumbnail of the current print job."""

    _attr_content_type = "image/png"
    _attr_translation_key = "print_preview"

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        job_coordinator: PrusaConnectJobCoordinator,
        printer_uuid: str,
    ) -> None:
        """Initialize the print preview image entity."""
        PrusaConnectEntity.__init__(self, coordinator, printer_uuid)
        ImageEntity.__init__(self, coordinator.hass)
        self._job_coordinator = job_coordinator
        self._attr_unique_id = f"{printer_uuid}_print_preview"

    @property
    def image_url(self) -> str | None:
        """Return the job's thumbnail URL from the API."""
        job = self._job_coordinator.data.get(self._printer_uuid)
        if job:
            return job.get("thumbnailUrl")
        return None

    @property
    def image_last_updated(self) -> datetime | None:
        """Track when the image changes (new job = new image)."""
        job = self._job_coordinator.data.get(self._printer_uuid)
        if job and job.get("startedAt"):
            try:
                return datetime.fromisoformat(job["startedAt"])
            except (ValueError, TypeError):
                pass
        return None

    @property
    def available(self) -> bool:
        """Only available when a job is active."""
        if not super().available:
            return False
        job = self._job_coordinator.data.get(self._printer_uuid)
        return job is not None and job.get("state") in ("PRINTING", "PAUSED")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Prusa Connect print preview images."""
    data = hass.data[DOMAIN][entry.entry_id]
    printer_coordinator: PrusaConnectPrinterCoordinator = data[DATA_PRINTER_COORDINATOR]
    job_coordinator: PrusaConnectJobCoordinator = data[DATA_JOB_COORDINATOR]

    entities: list[PrusaConnectPrintPreview] = []

    for printer_uuid in printer_coordinator.data:
        entities.append(
            PrusaConnectPrintPreview(
                printer_coordinator,
                job_coordinator,
                printer_uuid,
            )
        )

    async_add_entities(entities)
