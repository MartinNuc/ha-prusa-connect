"""DataUpdateCoordinators for Prusa Connect."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PrusaConnectAPI
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, FAST_SCAN_DURATION, FAST_SCAN_INTERVAL

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry

_LOGGER = logging.getLogger(__name__)

# Job states that mean a print is currently on the bed.
ACTIVE_JOB_STATES = {"PRINTING", "PAUSED"}


class PrusaConnectPrinterCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Coordinator for polling printer data.

    Data shape: dict keyed by printer UUID. Each value is the printer's list
    entry merged with its detail document, so entities can read either.
    """

    config_entry: PrusaConnectConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PrusaConnectConfigEntry,
        api: PrusaConnectAPI,
    ) -> None:
        """Initialize the printer coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_printers",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            always_update=False,
        )
        self.api = api
        self._fast_poll_until: float = 0

    def expect_change(self) -> None:
        """Speed up polling temporarily after a command."""
        self._fast_poll_until = time.monotonic() + FAST_SCAN_DURATION
        if time.monotonic() < self._fast_poll_until:
            self.update_interval = timedelta(seconds=FAST_SCAN_INTERVAL)

    async def _async_update_data(self) -> dict[str, dict]:
        """Fetch printer data from the API."""
        if (
            time.monotonic() >= self._fast_poll_until
            and self.update_interval != timedelta(seconds=DEFAULT_SCAN_INTERVAL)
        ):
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

        try:
            printers = await self.api.get_printers()

            tasks = [self.api.get_printer(p["uuid"]) for p in printers]
            details = await asyncio.gather(*tasks, return_exceptions=True)

            result: dict[str, dict] = {}
            for printer, detail in zip(printers, details):
                uuid = printer["uuid"]
                if isinstance(detail, ConfigEntryAuthFailed):
                    raise detail
                if isinstance(detail, Exception):
                    _LOGGER.warning(
                        "Failed to fetch detail for printer %s: %s", uuid, detail
                    )
                    result[uuid] = printer
                else:
                    # Detail wins, but the list entry carries fields the detail
                    # document omits (name, team, model marketing name).
                    result[uuid] = {**printer, **detail}

            return result

        except ConfigEntryAuthFailed:
            raise
        except ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            if isinstance(err, UpdateFailed):
                raise
            raise UpdateFailed(f"Unexpected error: {err}") from err


class PrusaConnectJobCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Coordinator for polling job data.

    Data shape: dict keyed by printer UUID holding that printer's current (or
    most recent) job.
    """

    config_entry: PrusaConnectConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PrusaConnectConfigEntry,
        api: PrusaConnectAPI,
    ) -> None:
        """Initialize the job coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_jobs",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
            always_update=False,
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, dict]:
        """Fetch job data from the API."""
        try:
            jobs = await self.api.get_jobs()

            result: dict[str, dict] = {}
            for job in jobs:
                printer_uuid = job.get("printer_uuid")
                if not printer_uuid:
                    continue

                existing = result.get(printer_uuid)
                if existing is None or _job_is_more_recent(job, existing):
                    result[printer_uuid] = job

            return result

        except ConfigEntryAuthFailed:
            raise
        except ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            if isinstance(err, UpdateFailed):
                raise
            raise UpdateFailed(f"Unexpected error: {err}") from err


def _job_is_more_recent(job_a: dict, job_b: dict) -> bool:
    """Check if job_a should replace job_b as the printer's current job."""
    a_active = job_a.get("state") in ACTIVE_JOB_STATES
    b_active = job_b.get("state") in ACTIVE_JOB_STATES
    if a_active != b_active:
        return a_active

    # Otherwise the one that started later wins.
    return (job_a.get("start") or 0) > (job_b.get("start") or 0)
