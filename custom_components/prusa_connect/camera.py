"""Camera platform for Prusa Connect — snapshots and live WebRTC video."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PrusaConnectAPI
from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity
from .signaling import SignalingError
from .webrtc_session import CameraStreamSession

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry

_LOGGER = logging.getLogger(__name__)

# Connect stores the latest frame pushed by the camera; the common trigger
# scheme uploads every 30 seconds, so polling faster gains nothing.
FRAME_INTERVAL = 30.0

# Cameras advertise their capabilities; only some can stream.
FEATURE_WEBRTC = "WebRtc"


class PrusaConnectCamera(PrusaConnectEntity, Camera):
    """A Prusa Connect camera.

    Always serves the snapshot Connect holds. Cameras that advertise WebRTC
    additionally stream live video, negotiated on demand: a session exists only
    while somebody is watching.
    """

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
        self._camera_token = camera.get("token")
        self._attr_name = camera.get("name") or "Camera"
        self._attr_unique_id = f"{printer_uuid}_camera_{self._camera_id}"
        self._attr_frame_interval = FRAME_INTERVAL

        self._supports_webrtc = (
            FEATURE_WEBRTC in (camera.get("features") or [])
            and bool(self._camera_token)
        )
        self._attr_is_streaming = self._supports_webrtc
        if self._supports_webrtc:
            self._attr_supported_features = CameraEntityFeature.STREAM

        self._sessions: dict[str, CameraStreamSession] = {}
        self._environment: dict[str, str] | None = None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the most recent snapshot as bytes."""
        return await self._api.get_camera_snapshot(self._camera_id)

    async def _async_environment(self) -> dict[str, str]:
        """Connect's runtime configuration, read once per entity."""
        if self._environment is None:
            self._environment = await self._api.get_environment()
        return self._environment

    async def async_handle_async_webrtc_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Answer a viewer's offer by opening a camera session behind it.

        The camera also expects to be answered, so its session is established
        first — the answer we return here has to describe a track that already
        exists.
        """
        if not self._supports_webrtc:
            raise HomeAssistantError("This camera does not support live streaming")

        environment = await self._async_environment()
        session = CameraStreamSession(
            self._api,
            environment["CAMERA_SIGNALING_SERVER"],
            environment["CAMERA_WEBRTC_CONFIG_URL"],
            self._camera_token,
            self._api.access_token,
        )
        self._sessions[session_id] = session

        try:
            answer_sdp = await session.start(offer_sdp)
        except SignalingError as err:
            self._sessions.pop(session_id, None)
            await session.close()
            raise HomeAssistantError(f"Could not start the camera stream: {err}") from err
        except Exception as err:
            self._sessions.pop(session_id, None)
            await session.close()
            _LOGGER.exception("Unexpected error starting camera stream")
            raise HomeAssistantError("Could not start the camera stream") from err

        send_message(WebRTCAnswer(answer_sdp))

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: Any
    ) -> None:
        """Pass a viewer's ICE candidate to its session."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        await session.add_viewer_candidate(
            getattr(candidate, "candidate", "") or "",
            getattr(candidate, "sdp_mid", None),
        )

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Tear a viewer's session down when they stop watching."""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            self.hass.async_create_task(session.close())

    async def async_will_remove_from_hass(self) -> None:
        """Close any sessions still open when the entity goes away."""
        sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            await session.close()
        await super().async_will_remove_from_hass()


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
