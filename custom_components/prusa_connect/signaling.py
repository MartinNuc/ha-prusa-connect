"""Socket.IO signalling client for Prusa Connect cameras.

Owns the conversation with ``camera-signaling.prusa3d.com``: authenticate, wake
the camera, ask it to stream, then shuttle SDP and ICE candidates.

Deliberately knows nothing about WebRTC itself. It hands offers and candidates
to callbacks and accepts answers and candidates back, so the media layer can be
chosen (or replaced) independently. See ``docs/CAMERA_PROTOCOL.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import socketio

from .webrtc_protocol import (
    CMD_GET_FEATURES,
    CMD_GET_STATUS,
    EVENT_AUTH,
    EVENT_CONFIGURATION,
    EVENT_FEATURES,
    EVENT_STATUS,
    EVENT_TRIGGER,
    EVENT_WEBRTC,
    MEDIA_MID,
    MessageType,
    VideoQuality,
    add_candidate_prefix,
    build_auth,
    build_command,
    build_ice_configuration,
    build_video_configuration,
    build_webrtc,
    parse_webrtc,
    strip_candidate_prefix,
)

_LOGGER = logging.getLogger(__name__)

# Commands sent before the server acknowledges authentication are dropped.
AUTH_TIMEOUT = 15.0

# The camera does not announce itself; it answers when asked. Waiting for that
# answer before requesting a stream is what the web app does, and requesting
# too early gets no reply.
CAMERA_READY_TIMEOUT = 20.0

# How long to wait for the camera to produce an SDP offer once asked.
OFFER_TIMEOUT = 20.0

OfferCallback = Callable[[str], Awaitable[None]]
CandidateCallback = Callable[[str, str], Awaitable[None]]


class SignalingError(Exception):
    """Signalling failed in a way that retrying will not fix immediately."""


class PrusaCameraSignaling:
    """One signalling session for one camera.

    Sessions are cheap and short-lived by design: the web app tears its own
    down as soon as the tab is hidden, so holding one open only while somebody
    is watching matches how the service expects to be used.
    """

    def __init__(
        self,
        signaling_host: str,
        camera_token: str,
        access_token: str,
        *,
        on_offer: OfferCallback,
        on_candidate: CandidateCallback,
    ) -> None:
        """Initialize the signalling client."""
        self._host = signaling_host
        self._camera_token = camera_token
        self._access_token = access_token
        self._on_offer = on_offer
        self._on_candidate = on_candidate

        self._sio = socketio.AsyncClient(logger=False, engineio_logger=False)
        self._session_id: str = ""
        self._camera_ready = asyncio.Event()
        self._offer_seen = asyncio.Event()
        self._closed = False

        self._register_handlers()

    @property
    def session_id(self) -> str:
        """The Socket.IO namespace session id used to route replies."""
        return self._session_id

    def _register_handlers(self) -> None:
        @self._sio.on(EVENT_STATUS)
        async def _status(_data: Any = None) -> None:
            self._camera_ready.set()

        @self._sio.on(EVENT_FEATURES)
        async def _features(_data: Any = None) -> None:
            self._camera_ready.set()

        @self._sio.on(EVENT_WEBRTC)
        async def _webrtc(data: Any = None) -> None:
            if not isinstance(data, (bytes, bytearray)):
                _LOGGER.debug("Ignoring non-binary webrtc payload: %r", data)
                return
            await self._handle_webrtc(parse_webrtc(bytes(data)))

        @self._sio.event
        async def connect_error(data: Any) -> None:
            _LOGGER.warning("Camera signalling connect error: %r", data)

    async def _handle_webrtc(self, message: dict) -> None:
        """Dispatch an inbound signalling message."""
        kind = message.get("message_type")
        sdp = message.get("sdp")

        if kind == MessageType.OFFER and sdp:
            if self._offer_seen.is_set():
                _LOGGER.debug("Ignoring duplicate offer")
                return
            self._offer_seen.set()
            await self._on_offer(sdp)

        elif kind == MessageType.CANDIDATE and sdp:
            await self._on_candidate(
                strip_candidate_prefix(sdp), message.get("mid") or MEDIA_MID
            )

    async def _send_webrtc(self, message_type: MessageType, **kwargs: Any) -> None:
        payload = build_webrtc(
            self._camera_token, self._session_id, message_type, **kwargs
        )
        await self._sio.emit(EVENT_WEBRTC, payload)

    async def connect(self) -> None:
        """Open the socket, authenticate, and wait for the camera to respond."""
        await self._sio.connect(
            f"https://{self._host}",
            auth={"token": self._camera_token},
            transports=["websocket"],
        )

        # NOT `self._sio.sid` — python-socketio exposes the *Engine.IO* id
        # there. The server routes replies by the *Socket.IO* namespace id, and
        # using the wrong one is silent: the request is accepted, then dropped.
        self._session_id = self._sio.namespaces.get("/") or self._sio.sid
        _LOGGER.debug("Camera signalling connected (session %s)", self._session_id)

        # Wait for the auth ack before doing anything else. Commands sent
        # before the server has processed authentication are dropped, and the
        # camera then never announces itself.
        await self._authenticate()

        for field in (CMD_GET_STATUS, CMD_GET_FEATURES):
            await self._sio.emit(
                EVENT_TRIGGER, build_command(self._camera_token, field)
            )

        try:
            await asyncio.wait_for(self._camera_ready.wait(), CAMERA_READY_TIMEOUT)
        except TimeoutError as err:
            raise SignalingError(
                "Camera did not respond on the signalling channel"
            ) from err

    async def _authenticate(self) -> None:
        """Send credentials and wait for the server to acknowledge them."""
        loop = asyncio.get_running_loop()
        acked: asyncio.Future = loop.create_future()

        def _on_ack(*args: Any) -> None:
            if not acked.done():
                acked.set_result(args)

        await self._sio.emit(
            EVENT_AUTH,
            build_auth(self._camera_token, self._access_token),
            callback=_on_ack,
        )

        try:
            await asyncio.wait_for(acked, AUTH_TIMEOUT)
        except TimeoutError as err:
            raise SignalingError(
                "Camera signalling server did not accept our credentials"
            ) from err

    async def set_video_quality(self, quality: VideoQuality) -> None:
        """Ask the camera for a stream resolution tier.

        Must be sent before ``request_stream``: the camera encodes at whatever
        quality was in force when it built its offer, so a later change only
        takes effect on the next session.

        Fire-and-forget — the camera does not acknowledge this, and a camera
        that ignores it simply streams at its current quality.
        """
        await self._sio.emit(
            EVENT_CONFIGURATION,
            build_video_configuration(self._camera_token, quality),
        )

    async def request_stream(
        self, ice_servers: list[dict], policy: str | None, ttl: int
    ) -> None:
        """Ask the camera to start streaming and wait for its offer."""
        await self._send_webrtc(
            MessageType.REQUEST,
            ice_configuration=build_ice_configuration(ice_servers, policy, ttl),
        )

        try:
            await asyncio.wait_for(self._offer_seen.wait(), OFFER_TIMEOUT)
        except TimeoutError as err:
            raise SignalingError(
                "Camera accepted the request but produced no offer"
            ) from err

    async def send_answer(self, sdp: str) -> None:
        """Send our SDP answer."""
        await self._send_webrtc(MessageType.ANSWER, sdp=sdp)

    async def send_candidate(self, candidate: str, mid: str = MEDIA_MID) -> None:
        """Send a local ICE candidate."""
        await self._send_webrtc(
            MessageType.CANDIDATE, sdp=add_candidate_prefix(candidate), mid=mid
        )

    async def close(self) -> None:
        """Tear the session down. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._sio.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not raise
            _LOGGER.debug("Error while closing camera signalling", exc_info=True)
