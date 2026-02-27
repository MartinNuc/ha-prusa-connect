"""Diagnostics support for Prusa Connect."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import PrusaConnectConfigEntry


def _redact_printer(printer: dict) -> dict:
    """Redact sensitive data from a printer dict."""
    redacted = dict(printer)
    for key in ("serialNumber", "apiKey", "token"):
        if key in redacted:
            redacted[key] = "**REDACTED**"

    # Redact snapshot URLs (contain signed tokens)
    for key in ("snapshotUrl", "cameraUrl"):
        if key in redacted and redacted[key]:
            redacted[key] = "**REDACTED_URL**"

    # Redact camera URLs
    cameras = redacted.get("cameras")
    if cameras:
        redacted_cameras = []
        for cam in cameras:
            rc = dict(cam)
            for url_key in ("snapshotUrl", "imageUrl", "streamUrl"):
                if url_key in rc and rc[url_key]:
                    rc[url_key] = "**REDACTED_URL**"
            redacted_cameras.append(rc)
        redacted["cameras"] = redacted_cameras

    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PrusaConnectConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data

    printers = {}
    if data.printer_coordinator.data:
        for uuid, printer in data.printer_coordinator.data.items():
            printers[uuid] = _redact_printer(printer)

    jobs = {}
    if data.job_coordinator.data:
        for uuid, job in data.job_coordinator.data.items():
            redacted_job = dict(job)
            if "thumbnailUrl" in redacted_job and redacted_job["thumbnailUrl"]:
                redacted_job["thumbnailUrl"] = "**REDACTED_URL**"
            jobs[uuid] = redacted_job

    return {
        "printers": printers,
        "jobs": jobs,
    }
