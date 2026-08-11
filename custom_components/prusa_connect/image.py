"""Image platform for Prusa Connect — print preview thumbnails."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PrusaConnectAPI
from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry


class PrusaConnectPrintPreview(PrusaConnectEntity, ImageEntity):
    """Image entity showing the preview of the current print job.

    Connect serves previews from an authenticated endpoint, so the bytes are
    fetched with the access token rather than exposed as a plain URL.
    """

    _attr_content_type = "image/png"
    _attr_translation_key = "print_preview"

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        job_coordinator: PrusaConnectJobCoordinator,
        api: PrusaConnectAPI,
        printer_uuid: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the print preview image entity."""
        PrusaConnectEntity.__init__(self, coordinator, printer_uuid)
        ImageEntity.__init__(self, hass)
        self._job_coordinator = job_coordinator
        self._api = api
        self._attr_unique_id = f"{printer_uuid}_print_preview"
        self._cached_url: str | None = None
        self._cached_bytes: bytes | None = None

    async def async_added_to_hass(self) -> None:
        """Register listeners for both coordinators."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._job_coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from either coordinator."""
        self.async_write_ha_state()

    @property
    def _job(self) -> dict | None:
        """Return the current job for this printer."""
        if not self._job_coordinator.data:
            return None
        return self._job_coordinator.data.get(self._printer_uuid)

    @property
    def _preview_url(self) -> str | None:
        """Return the API-relative preview path for the current job."""
        file = (self._job or {}).get("file") or {}
        return file.get("preview_url")

    @property
    def image_last_updated(self) -> datetime | None:
        """Track when the preview changes (new job = new image)."""
        job = self._job
        if not job or not job.get("start"):
            return None
        try:
            return datetime.fromtimestamp(float(job["start"]), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

    async def async_image(self) -> bytes | None:
        """Return the preview image bytes."""
        url = self._preview_url
        if not url:
            return None
        if url != self._cached_url:
            self._cached_bytes = await self._api.get_bytes(url)
            self._cached_url = url
        return self._cached_bytes

    @property
    def available(self) -> bool:
        """Only available when the current job has a preview."""
        if not super().available:
            return False
        return self._preview_url is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect print preview images."""
    data = entry.runtime_data

    async_add_entities(
        PrusaConnectPrintPreview(
            data.printer_coordinator,
            data.job_coordinator,
            data.api,
            printer_uuid,
            hass,
        )
        for printer_uuid in data.printer_coordinator.data
    )
