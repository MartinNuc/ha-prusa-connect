"""Bridges one Home Assistant viewer to one Prusa Connect camera stream.

Both ends of this expect to *answer*: Home Assistant's frontend sends us an
offer, and so does the camera. SDP therefore cannot be relayed between them —
the session has to be terminated on both sides and the media forwarded across.

Sessions are created when somebody opens the camera and torn down when they
close it. That matches how Connect itself behaves (the web app drops its
session as soon as the tab is hidden) and keeps traffic off Prusa's TURN relay
when nobody is watching.

On transcoding: ``MediaRelay`` decodes and re-encodes. The stream is 640x480 at
roughly 12 fps, so that cost is small, and it uses only aiortc's public API.
Passthrough is possible and measurably cheaper — see ``docs/CAMERA_PROTOCOL.md``
— but the obvious implementation patches aiortc's module-level codec factories,
which would affect every other aiortc user in the same process. That is not an
acceptable trade inside Home Assistant, so it needs a properly scoped
implementation before it is worth adopting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp

from .signaling import PrusaCameraSignaling, SignalingError

if TYPE_CHECKING:
    from .api import PrusaConnectAPI

_LOGGER = logging.getLogger(__name__)

# How long to wait for the upstream camera track before giving up on a viewer.
UPSTREAM_TIMEOUT = 30.0


class CameraStreamSession:
    """One viewer's stream: camera -> us -> Home Assistant."""

    def __init__(
        self,
        api: PrusaConnectAPI,
        signaling_host: str,
        webrtc_config_url: str,
        camera_token: str,
        access_token: str,
    ) -> None:
        """Initialize the session. Nothing is opened until :meth:`start`."""
        self._api = api
        self._signaling_host = signaling_host
        self._webrtc_config_url = webrtc_config_url
        self._camera_token = camera_token
        self._access_token = access_token

        self._signaling: PrusaCameraSignaling | None = None
        self._upstream: RTCPeerConnection | None = None
        self._downstream: RTCPeerConnection | None = None
        self._relay = MediaRelay()
        self._track_ready: asyncio.Future | None = None
        self._closed = False

    async def start(self, offer_sdp: str) -> str:
        """Open the camera stream and answer the viewer's offer.

        The upstream session must be established first: the answer we give the
        viewer has to describe a track that already exists.
        """
        loop = asyncio.get_running_loop()
        self._track_ready = loop.create_future()

        try:
            track = await self._connect_upstream()
            return await self._answer_downstream(offer_sdp, track)
        except Exception:
            await self.close()
            raise

    async def _connect_upstream(self):  # noqa: ANN202 - aiortc track type
        """Negotiate with the camera and return its video track."""
        config = await self._api.get_webrtc_config(self._webrtc_config_url)

        pc = RTCPeerConnection(_to_aiortc(config["ice_servers"]))
        self._upstream = pc

        @pc.on("track")
        def _on_track(track) -> None:  # noqa: ANN001
            _LOGGER.debug("Camera track received (%s)", track.kind)
            if self._track_ready is not None and not self._track_ready.done():
                self._track_ready.set_result(track)

        async def on_offer(sdp: str) -> None:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            await pc.setLocalDescription(await pc.createAnswer())
            assert self._signaling is not None
            await self._signaling.send_answer(pc.localDescription.sdp)

        async def on_candidate(candidate: str, mid: str) -> None:
            try:
                parsed = candidate_from_sdp(candidate)
                parsed.sdpMid = mid
                parsed.sdpMLineIndex = 0
                await pc.addIceCandidate(parsed)
            except Exception:  # noqa: BLE001 - one bad candidate is not fatal
                _LOGGER.debug("Discarding camera candidate %r", candidate[:60])

        self._signaling = PrusaCameraSignaling(
            self._signaling_host,
            self._camera_token,
            self._access_token,
            on_offer=on_offer,
            on_candidate=on_candidate,
        )

        await self._signaling.connect()
        await self._signaling.request_stream(
            config["ice_servers"], config["policy"], config["ttl"]
        )

        try:
            return await asyncio.wait_for(self._track_ready, UPSTREAM_TIMEOUT)
        except TimeoutError as err:
            raise SignalingError("Camera produced no video track") from err

    async def _answer_downstream(self, offer_sdp: str, track) -> str:  # noqa: ANN001
        """Answer the viewer, attaching the relayed camera track."""
        pc = RTCPeerConnection()
        self._downstream = pc

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            _LOGGER.debug("Viewer connection: %s", pc.connectionState)

        pc.addTrack(self._relay.subscribe(track))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())

        # aiortc completes ICE gathering before setLocalDescription returns, so
        # the answer already carries every candidate and nothing needs trickling.
        return pc.localDescription.sdp

    async def add_viewer_candidate(self, candidate: str, mid: str | None) -> None:
        """Add an ICE candidate sent by the viewer."""
        if self._downstream is None or not candidate:
            return
        try:
            parsed = candidate_from_sdp(candidate)
            parsed.sdpMid = mid or "0"
            parsed.sdpMLineIndex = 0
            await self._downstream.addIceCandidate(parsed)
        except Exception:  # noqa: BLE001 - one bad candidate is not fatal
            _LOGGER.debug("Discarding viewer candidate %r", candidate[:60])

    async def close(self) -> None:
        """Tear everything down. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True

        if self._track_ready is not None and not self._track_ready.done():
            self._track_ready.cancel()

        for name, closeable in (
            ("signalling", self._signaling),
            ("viewer connection", self._downstream),
            ("camera connection", self._upstream),
        ):
            if closeable is None:
                continue
            try:
                await closeable.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                _LOGGER.debug("Error closing %s", name, exc_info=True)

        self._signaling = None
        self._downstream = None
        self._upstream = None


def _to_aiortc(ice_servers: list[dict[str, Any]]) -> RTCConfiguration:
    """Translate Connect's ICE configuration into aiortc's form."""
    return RTCConfiguration(
        iceServers=[
            RTCIceServer(
                urls=(
                    server["urls"]
                    if isinstance(server["urls"], list)
                    else [server["urls"]]
                ),
                username=server.get("username"),
                credential=server.get("credential"),
            )
            for server in ice_servers
        ]
    )
