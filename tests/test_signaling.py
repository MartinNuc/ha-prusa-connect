"""Tests for the camera signalling session.

The protocol is undocumented and the service gives almost no feedback when we
get the conversation wrong — a malformed or mistimed message is accepted and
then silently dropped. Two failures found the hard way are pinned here: using
the Engine.IO session id instead of the Socket.IO one, and sending commands
before the server has acknowledged authentication. Both looked like "the camera
is offline" and neither produced an error.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.prusa_connect import signaling as signaling_module
from custom_components.prusa_connect.signaling import (
    PrusaCameraSignaling,
    SignalingError,
)
from custom_components.prusa_connect.webrtc_protocol import (
    EVENT_AUTH,
    EVENT_TRIGGER,
    EVENT_WEBRTC,
    MEDIA_MID,
    MessageType,
    build_webrtc,
    parse_webrtc,
)

CAMERA_TOKEN = "CAMERATOKEN000000000"
ACCESS_TOKEN = "jwt-access-token"
NAMESPACE_SID = "AHsvezUMJR3o--zZBoqh"
ENGINEIO_SID = "avxeIbeBhp7o3jTuBoqg"


class FakeSocketIO:
    """Recording stand-in for ``socketio.AsyncClient``.

    Mimics the two behaviours that matter: the Engine.IO and Socket.IO session
    ids differ, and the camera only speaks when spoken to.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self.handlers: dict[str, object] = {}
        self.emitted: list[tuple[str, bytes | None]] = []
        self.namespaces: dict[str, str] = {}
        self.sid = ENGINEIO_SID
        self.connected = False
        self.auth: dict | None = None
        self.answer_triggers = True

    def on(self, event):  # noqa: ANN001, D102
        def decorator(fn):
            self.handlers[event] = fn
            return fn

        return decorator

    def event(self, fn):  # noqa: ANN001, D102
        self.handlers[fn.__name__] = fn
        return fn

    async def connect(self, _url, auth=None, transports=None) -> None:  # noqa: ANN001
        self.connected = True
        self.auth = auth
        self.namespaces = {"/": NAMESPACE_SID}

    async def emit(self, event, data=None, callback=None) -> None:  # noqa: ANN001
        self.emitted.append((event, data))
        if callback is not None:
            callback(0)
        if event == EVENT_TRIGGER and self.answer_triggers:
            await self.fire("status", b"")

    async def disconnect(self) -> None:  # noqa: D102
        self.connected = False

    async def fire(self, event: str, data) -> None:  # noqa: ANN001
        """Deliver an inbound event to the registered handler."""
        handler = self.handlers.get(event)
        if handler is not None:
            await handler(data)

    def events_named(self, name: str) -> list[bytes]:
        """Payloads emitted under ``name``."""
        return [payload for event, payload in self.emitted if event == name]


@pytest.fixture
def fake_sio(monkeypatch: pytest.MonkeyPatch) -> FakeSocketIO:
    """Install a recording client and hand it back."""
    client = FakeSocketIO()
    monkeypatch.setattr(
        signaling_module.socketio, "AsyncClient", lambda *a, **k: client, raising=False
    )
    return client


def make_session(**kwargs) -> tuple[PrusaCameraSignaling, list, list]:
    """Build a session with recording callbacks."""
    offers: list[str] = []
    candidates: list[tuple[str, str]] = []

    async def on_offer(sdp: str) -> None:
        offers.append(sdp)

    async def on_candidate(candidate: str, mid: str) -> None:
        candidates.append((candidate, mid))

    session = PrusaCameraSignaling(
        "camera-signaling.prusa3d.com",
        CAMERA_TOKEN,
        ACCESS_TOKEN,
        on_offer=on_offer,
        on_candidate=on_candidate,
        **kwargs,
    )
    return session, offers, candidates


class TestConnect:
    """Opening a session."""

    @pytest.mark.asyncio
    async def test_uses_socketio_sid_not_engineio_sid(self, fake_sio) -> None:
        """The server routes replies by the Socket.IO id; the other is silently wrong."""
        session, _, _ = make_session()
        await session.connect()
        assert session.session_id == NAMESPACE_SID
        assert session.session_id != fake_sio.sid

    @pytest.mark.asyncio
    async def test_camera_token_is_the_handshake_auth(self, fake_sio) -> None:
        session, _, _ = make_session()
        await session.connect()
        assert fake_sio.auth == {"token": CAMERA_TOKEN}

    @pytest.mark.asyncio
    async def test_authenticates_before_sending_commands(self, fake_sio) -> None:
        """Commands sent before the auth ack are dropped and the camera goes quiet."""
        session, _, _ = make_session()
        await session.connect()
        order = [event for event, _ in fake_sio.emitted]
        assert order[0] == EVENT_AUTH
        assert order[1:3] == [EVENT_TRIGGER, EVENT_TRIGGER]

    @pytest.mark.asyncio
    async def test_requests_status_and_features(self, fake_sio) -> None:
        """The camera does not announce itself; it has to be asked."""
        session, _, _ = make_session()
        await session.connect()
        assert len(fake_sio.events_named(EVENT_TRIGGER)) == 2

    @pytest.mark.asyncio
    async def test_raises_when_camera_never_responds(self, fake_sio, monkeypatch) -> None:
        monkeypatch.setattr(signaling_module, "CAMERA_READY_TIMEOUT", 0.05)
        fake_sio.answer_triggers = False
        session, _, _ = make_session()
        with pytest.raises(SignalingError, match="did not respond"):
            await session.connect()

    @pytest.mark.asyncio
    async def test_raises_when_auth_is_not_acknowledged(self, fake_sio, monkeypatch) -> None:
        monkeypatch.setattr(signaling_module, "AUTH_TIMEOUT", 0.05)

        async def emit_without_ack(event, data=None, callback=None):  # noqa: ANN001
            fake_sio.emitted.append((event, data))

        monkeypatch.setattr(fake_sio, "emit", emit_without_ack)
        session, _, _ = make_session()
        with pytest.raises(SignalingError, match="credentials"):
            await session.connect()


class TestStreamRequest:
    """Asking the camera to stream."""

    @pytest.mark.asyncio
    async def test_request_carries_ice_configuration(self, fake_sio) -> None:
        session, offers, _ = make_session()
        await session.connect()

        async def offer_soon() -> None:
            await asyncio.sleep(0)
            await fake_sio.fire(
                EVENT_WEBRTC,
                build_webrtc(
                    CAMERA_TOKEN, NAMESPACE_SID, MessageType.OFFER, sdp="v=0\r\n"
                ),
            )

        task = asyncio.create_task(offer_soon())
        await session.request_stream([], "all", 300)
        await task

        sent = parse_webrtc(fake_sio.events_named(EVENT_WEBRTC)[0])
        assert sent["message_type"] == MessageType.REQUEST
        assert sent["request_id"] == NAMESPACE_SID
        assert offers == ["v=0\r\n"]

    @pytest.mark.asyncio
    async def test_raises_when_no_offer_arrives(self, fake_sio, monkeypatch) -> None:
        monkeypatch.setattr(signaling_module, "OFFER_TIMEOUT", 0.05)
        session, _, _ = make_session()
        await session.connect()
        with pytest.raises(SignalingError, match="no offer"):
            await session.request_stream([], "all", 300)


class TestInboundMessages:
    """Handling what the camera sends back."""

    @pytest.mark.asyncio
    async def test_candidate_prefix_is_stripped(self, fake_sio) -> None:
        session, _, candidates = make_session()
        await session.connect()
        await fake_sio.fire(
            EVENT_WEBRTC,
            build_webrtc(
                CAMERA_TOKEN,
                NAMESPACE_SID,
                MessageType.CANDIDATE,
                sdp="a=candidate:1 1 UDP 2122317823 203.0.113.114 36512 typ host",
            ),
        )
        assert candidates == [
            ("candidate:1 1 UDP 2122317823 203.0.113.114 36512 typ host", MEDIA_MID)
        ]

    @pytest.mark.asyncio
    async def test_duplicate_offer_is_ignored(self, fake_sio) -> None:
        """A second offer would renegotiate an already-running session."""
        session, offers, _ = make_session()
        await session.connect()
        offer = build_webrtc(
            CAMERA_TOKEN, NAMESPACE_SID, MessageType.OFFER, sdp="v=0\r\n"
        )
        await fake_sio.fire(EVENT_WEBRTC, offer)
        await fake_sio.fire(EVENT_WEBRTC, offer)
        assert len(offers) == 1

    @pytest.mark.asyncio
    async def test_non_binary_payload_is_ignored(self, fake_sio) -> None:
        session, offers, candidates = make_session()
        await session.connect()
        await fake_sio.fire(EVENT_WEBRTC, {"unexpected": "json"})
        assert not offers and not candidates


class TestTeardown:
    """Closing a session."""

    @pytest.mark.asyncio
    async def test_close_disconnects(self, fake_sio) -> None:
        session, _, _ = make_session()
        await session.connect()
        await session.close()
        assert fake_sio.connected is False

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, fake_sio) -> None:
        """Teardown runs from several paths; it must not raise on the second."""
        session, _, _ = make_session()
        await session.connect()
        await session.close()
        await session.close()

    @pytest.mark.asyncio
    async def test_close_survives_a_failing_disconnect(self, fake_sio, monkeypatch) -> None:
        session, _, _ = make_session()
        await session.connect()

        async def boom() -> None:
            raise RuntimeError("socket already gone")

        monkeypatch.setattr(fake_sio, "disconnect", boom)
        await session.close()
