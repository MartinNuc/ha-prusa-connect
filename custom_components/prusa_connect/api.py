"""API client for Prusa Connect Mobile API."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed

from .auth import AuthenticationError, refresh_access_token
from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)


class PrusaConnectAPI:
    """Client for the Prusa Connect Mobile API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        token_update_callback: Any = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_update_callback = token_update_callback

    def update_tokens(
        self, access_token: str, refresh_token: str
    ) -> None:
        """Update stored tokens."""
        self._access_token = access_token
        self._refresh_token = refresh_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        data: dict | None = None,
        params: dict | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        """Make an authenticated API request.

        Automatically refreshes token on 401 and retries once.
        Unwraps hydra:member for collection responses.
        """
        url = f"{API_BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        async with self._session.request(
            method, url, headers=headers, json=json, data=data, params=params
        ) as resp:
            if resp.status == 401 and retry_on_401:
                # Try to refresh the token
                await self._refresh_token_and_persist()
                return await self._request(
                    method,
                    path,
                    json=json,
                    data=data,
                    params=params,
                    retry_on_401=False,
                )

            if resp.status == 401:
                raise ConfigEntryAuthFailed("Authentication failed after token refresh")

            if resp.status == 204:
                return None

            resp.raise_for_status()

            result = await resp.json()

            # Unwrap hydra:member for collection endpoints
            if isinstance(result, dict) and "hydra:member" in result:
                return result["hydra:member"]

            return result

    async def _refresh_token_and_persist(self) -> None:
        """Refresh the access token and persist the new tokens."""
        try:
            tokens = await refresh_access_token(self._session, self._refresh_token)
            self._access_token = tokens["access_token"]
            self._refresh_token = tokens["refresh_token"]

            if self._token_update_callback:
                await self._token_update_callback(tokens)
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Token refresh failed — re-authentication required"
            ) from err

    # --- User ---

    async def get_user(self) -> dict:
        """Get the authenticated user's profile."""
        return await self._request("GET", "/app/prusa/v1/user")

    # --- Printers ---

    async def get_printers(self) -> list[dict]:
        """Get all printers for the user."""
        return await self._request("GET", "/app/prusa/v1/printers")

    async def get_printer(self, uuid: str) -> dict:
        """Get detailed info for a specific printer including telemetry."""
        return await self._request("GET", f"/app/prusa/v1/printers/{uuid}")

    async def update_printer(self, uuid: str, data: dict) -> dict:
        """Update printer settings."""
        return await self._request("PATCH", f"/app/prusa/v1/printers/{uuid}", json=data)

    # --- Commands ---

    async def pause_print(self, uuid: str) -> None:
        """Pause current print on a printer."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={"command": "PAUSE"},
        )

    async def resume_print(self, uuid: str) -> None:
        """Resume a paused print."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={"command": "RESUME"},
        )

    async def stop_print(self, uuid: str) -> None:
        """Stop current print."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={"command": "STOP"},
        )

    async def set_ready(self, uuid: str) -> None:
        """Mark printer as ready (pick up finished print)."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={"command": "SET_READY"},
        )

    async def set_unready(self, uuid: str) -> None:
        """Cancel ready state."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={"command": "CANCEL_READY"},
        )

    async def start_print_cloud(
        self, uuid: str, file_hash: str, team_id: int
    ) -> None:
        """Start a print from cloud storage."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={
                "command": "START_PRINT",
                "source": "CLOUD",
                "fileHash": file_hash,
                "teamId": team_id,
            },
        )

    async def start_print_usb(self, uuid: str, path: str) -> None:
        """Start a print from USB storage."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={
                "command": "START_PRINT",
                "source": "USB",
                "path": path,
            },
        )

    async def start_print_url(self, uuid: str, url: str) -> None:
        """Start a print from a URL (e.g. Printables)."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={
                "command": "START_URL",
                "url": url,
            },
        )

    async def send_control_command(self, uuid: str, command: str) -> None:
        """Send a generic control command."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={"command": command},
        )

    async def respond_to_dialog(
        self, uuid: str, dialog_id: int, button: str
    ) -> None:
        """Respond to a printer dialog."""
        await self._request(
            "POST",
            f"/app/prusa/v1/printers/{uuid}/command",
            json={
                "command": "DIALOG_ACTION",
                "dialogId": dialog_id,
                "button": button,
            },
        )

    # --- Jobs ---

    async def get_jobs(self, **params: Any) -> list[dict]:
        """Get jobs, optionally filtered by query params."""
        return await self._request("GET", "/app/prusa/v1/jobs", params=params or None)

    async def get_job(self, job_id: int) -> dict:
        """Get a specific job by ID."""
        return await self._request("GET", f"/app/prusa/v1/jobs/{job_id}")

    async def get_queue(self, printer_uuid: str) -> list[dict]:
        """Get the print queue for a printer."""
        return await self._request(
            "GET",
            "/app/prusa/v1/jobs",
            params={"printerUuid": printer_uuid, "state": "QUEUED"},
        )

    async def delete_queued_job(self, job_id: int) -> None:
        """Delete a queued job."""
        await self._request("DELETE", f"/app/prusa/v1/jobs/{job_id}")

    # --- Cameras ---

    async def get_cameras(self) -> list[dict]:
        """Get all cameras."""
        return await self._request("GET", "/app/prusa/v1/cameras")

    async def get_camera(self, token: str) -> dict:
        """Get a specific camera by token."""
        return await self._request("GET", f"/app/prusa/v1/cameras/{token}")

    # --- Notifications ---

    async def get_notifications(self, **params: Any) -> list[dict]:
        """Get notifications."""
        return await self._request(
            "GET", "/app/prusa/v1/notifications", params=params or None
        )

    async def get_unseen_count(self) -> int:
        """Get the count of unseen notifications."""
        result = await self._request("GET", "/app/prusa/v1/notifications/unseen-count")
        if isinstance(result, dict):
            return result.get("count", 0)
        return 0

    async def mark_all_read(self) -> None:
        """Mark all notifications as read."""
        await self._request("POST", "/app/prusa/v1/notifications/mark-all-read")

    # --- Storage ---

    async def get_cloud_files(self, team_id: int) -> list[dict]:
        """Get cloud storage files for a team."""
        return await self._request("GET", f"/app/prusa/v1/teams/{team_id}/files")

    async def get_printer_files(self, uuid: str) -> list[dict]:
        """Get files on the printer's USB storage."""
        return await self._request("GET", f"/app/prusa/v1/printers/{uuid}/files")

    # --- Teams ---

    async def get_teams(self) -> list[dict]:
        """Get all teams the user belongs to."""
        return await self._request("GET", "/app/prusa/v1/teams")

    async def get_team(self, team_id: int) -> dict:
        """Get a specific team."""
        return await self._request("GET", f"/app/prusa/v1/teams/{team_id}")

    async def get_team_members(self, team_id: int) -> list[dict]:
        """Get members of a team."""
        return await self._request("GET", f"/app/prusa/v1/teams/{team_id}/members")
