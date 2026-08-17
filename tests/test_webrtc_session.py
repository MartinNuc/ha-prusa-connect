"""Start-up rules for a camera stream session.

The subtle one: aiortc creates a track object as soon as the camera's offer is
applied, seconds before any media can flow. Treating that as success answers the
viewer with a stream that may never carry a frame — the frontend shows a spinner
forever, and because nothing failed, nothing tears the session down. Each dead
session keeps a TURN allocation, and Prusa's relay starts refusing new ones
("486 Allocation Quota Reached") after a handful, which takes the camera out
entirely until the allocations expire. So the session must wait for the
connection, not the track.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.prusa_connect import webrtc_session as ws
from custom_components.prusa_connect.signaling import SignalingError

ICE_CONFIG = {"ice_servers": [], "policy": None, "ttl": 60}

RELAY_SDP = "v=0\r\na=candidate:1 1 udp 1 10.0.0.1 1 typ relay\r\n"

# Shaped like a real aiortc answer: gathering finishes before
# setLocalDescription returns, so every candidate is already in the SDP.
ANSWER_SDP = (
    "v=0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 102\r\n"
    "a=candidate:1 1 udp 2113937151 192.168.1.5 50470 typ host\r\n"
    "a=candidate:2 1 udp 1677729535 94.112.176.46 50470 typ srflx\r\n"
    "a=candidate:3 1 udp 50340095 34.159.146.76 52342 typ relay\r\n"
    "a=ice-ufrag:abcd\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "a=candidate:1 1 udp 2113937151 192.168.1.5 50470 typ host\r\n"
    "a=candidate:2 1 udp 1677729535 94.112.176.46 50470 typ srflx\r\n"
    "a=candidate:3 1 udp 50340095 34.159.146.76 52342 typ relay\r\n"
    "ANSWER\r\n"
)
HOST_ONLY_SDP = "v=0\r\na=candidate:1 1 udp 1 10.0.0.1 1 typ host\r\n"


class _Api:
    async def get_webrtc_config(self, _url):  # noqa: ANN001, ANN201
        return ICE_CONFIG


class _Description:
    def __init__(self, sdp: str) -> None:
        self.sdp = sdp


class _FakePeerConnection:
    """A peer connection whose state the test drives by hand."""

    instances: list["_FakePeerConnection"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.connectionState = "new"
        self.localDescription = _Description(RELAY_SDP)
        self.closed = False
        self._handlers: dict[str, list] = {}
        _FakePeerConnection.instances.append(self)

    def on(self, event):  # noqa: ANN001, ANN201
        def register(func):  # noqa: ANN001, ANN202
            self._handlers.setdefault(event, []).append(func)
            return func

        return register

    async def fire(self, event: str, *args) -> None:
        for handler in self._handlers.get(event, []):
            result = handler(*args)
            if asyncio.iscoroutine(result):
                await result

    async def enter(self, state: str) -> None:
        self.connectionState = state
        await self.fire("connectionstatechange")

    def addTrack(self, track):  # noqa: ANN001, ANN201, N802
        return track

    async def setRemoteDescription(self, _desc) -> None:  # noqa: N802
        return None

    # Default: the camera did get a relay, which is the healthy case.
    remoteDescription = _Description(  # noqa: N815
        "v=0\r\na=candidate:9 1 udp 1 34.159.146.76 52426 typ relay\r\n"
    )

    async def setLocalDescription(self, desc) -> None:  # noqa: ANN001, N802
        # aiortc finishes gathering here, so the stored SDP is what carries our
        # candidates — which is exactly what the relay diagnosis reads back.
        self.localDescription = desc

    async def createAnswer(self):  # noqa: ANN201, N802
        return _Description(ANSWER_SDP)

    async def close(self) -> None:
        self.closed = True


class _FakeSignaling:
    """Replays a camera offer on request; records that it was closed."""

    instances: list["_FakeSignaling"] = []

    def __init__(self, *_args, on_offer=None, on_candidate=None, **_kwargs) -> None:  # noqa: ANN001
        self.on_offer = on_offer
        self.on_candidate = on_candidate
        self.closed = False
        self.answers: list[str] = []
        self.candidates: list[tuple[str, str]] = []
        _FakeSignaling.instances.append(self)

    async def connect(self) -> None:
        return None

    async def request_stream(self, *_args) -> None:
        return None

    async def send_answer(self, sdp: str) -> None:
        self.answers.append(sdp)

    async def send_candidate(self, candidate: str, mid: str) -> None:
        self.candidates.append((candidate, mid))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    _FakePeerConnection.instances = []
    _FakeSignaling.instances = []
    monkeypatch.setattr(ws, "RTCPeerConnection", _FakePeerConnection)
    monkeypatch.setattr(ws, "PrusaCameraSignaling", _FakeSignaling)
    monkeypatch.setattr(ws, "MediaRelay", lambda: _Relay())
    monkeypatch.setattr(ws, "UPSTREAM_TIMEOUT", 0.2)


class _Relay:
    def subscribe(self, track):  # noqa: ANN001, ANN201
        return track


def make_session(on_closed=None) -> ws.CameraStreamSession:  # noqa: ANN001
    return ws.CameraStreamSession(
        _Api(), "signal.example", "https://cfg", "CAMERATOKEN000000000", "ACCESS",
        on_closed=on_closed,
    )


async def _drive(session, *, track: bool = True, connect: str | None = "connected"):
    """Run ``start`` while feeding the upstream connection its events."""
    task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
    await asyncio.sleep(0)

    upstream = _FakePeerConnection.instances[0]
    if track:
        await upstream.fire("track", _Track())
    if connect is not None:
        await upstream.enter(connect)
    return await task


class _Track:
    kind = "video"


class TestStartup:
    """What has to be true before a viewer is answered."""

    @pytest.mark.asyncio
    async def test_answers_once_the_camera_connects(self) -> None:
        session = make_session()
        answer = await _drive(session)
        assert "ANSWER" in answer

    @pytest.mark.asyncio
    async def test_a_track_alone_is_not_enough(self) -> None:
        """The regression: a track exists long before media can flow."""
        session = make_session()
        with pytest.raises(SignalingError, match="did not connect"):
            await _drive(session, connect=None)

    @pytest.mark.asyncio
    async def test_gives_up_when_no_track_arrives(self) -> None:
        session = make_session()
        with pytest.raises(SignalingError, match="no video track"):
            await _drive(session, track=False, connect=None)

    @pytest.mark.asyncio
    async def test_reports_a_failed_connection_rather_than_waiting(self) -> None:
        """Failure is known immediately; the viewer should not wait it out."""
        session = make_session()
        with pytest.raises(SignalingError):
            await _drive(session, connect="failed")

    @pytest.mark.asyncio
    async def test_a_failed_start_closes_everything(self) -> None:
        session = make_session()
        with pytest.raises(SignalingError):
            await _drive(session, connect=None)

        assert _FakeSignaling.instances[0].closed, "signalling left open"
        assert all(pc.closed for pc in _FakePeerConnection.instances)


class TestTrickle:
    """The camera reads candidates from CANDIDATE messages, not from the SDP.

    It offers ``a=ice-options:ice2,trickle``, and a capture of the working web
    client shows every candidate sent as its own message. Leaving them only in
    the answer means the camera never learns an address to send to: an offer
    arrives, an answer goes back, and ICE sits in checking until it times out.
    """

    @pytest.mark.asyncio
    async def test_every_candidate_is_sent_separately(self) -> None:
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        sent = [c for c, _mid in signaling.candidates]
        assert len(sent) == 3, sent
        assert any("typ host" in c for c in sent)
        assert any("typ srflx" in c for c in sent)
        assert any("typ relay" in c for c in sent)

        upstream = _FakePeerConnection.instances[0]
        await upstream.fire("track", _Track())
        await upstream.enter("connected")
        await task

    @pytest.mark.asyncio
    async def test_a_candidate_repeated_per_m_section_is_sent_once(self) -> None:
        """aiortc lists the same candidates under video and the data channel."""
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        sent = [c for c, _mid in signaling.candidates]
        assert len(sent) == len(set(sent)) == 3, sent

    @pytest.mark.asyncio
    async def test_candidates_use_the_mid_the_camera_expects(self) -> None:
        """Observed on the wire: every candidate carries mid "video-stream"."""
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        assert {mid for _c, mid in signaling.candidates} == {"video-stream"}

    @pytest.mark.asyncio
    async def test_the_answer_still_goes_first(self) -> None:
        """Candidates for an unanswered session have nothing to attach to."""
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        assert signaling.answers, "no answer was sent"
        assert "ANSWER" in signaling.answers[0]

    @pytest.mark.asyncio
    async def test_non_candidate_lines_are_not_sent(self) -> None:
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        for candidate, _mid in signaling.candidates:
            assert candidate.startswith("a=candidate:"), candidate

    @pytest.mark.asyncio
    async def test_one_rejected_candidate_does_not_kill_the_session(self) -> None:
        session = make_session()
        asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        signaling = _FakeSignaling.instances[0]

        calls = {"n": 0}

        async def flaky(candidate: str, mid: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("transient")
            signaling.candidates.append((candidate, mid))

        signaling.send_candidate = flaky
        await signaling.on_offer("v=0\r\nCAMERA OFFER\r\n")

        assert calls["n"] == 3, "gave up after the failure"
        assert len(signaling.candidates) == 2


class TestDiagnosis:
    """A failure the user can act on beats a generic timeout."""

    @pytest.mark.asyncio
    async def test_blames_the_relay_when_we_never_got_one(self) -> None:
        """No relay of our own means no route at all — say so."""
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        upstream = _FakePeerConnection.instances[0]
        upstream.localDescription = _Description(HOST_ONLY_SDP)
        await upstream.fire("track", _Track())
        await upstream.enter("failed")

        with pytest.raises(SignalingError, match="TURN"):
            await task

    @pytest.mark.asyncio
    async def test_blames_the_relay_on_a_timeout_too(self) -> None:
        """The observed case: no relay leaves ICE stuck, it never says "failed"."""
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        upstream = _FakePeerConnection.instances[0]
        upstream.localDescription = _Description(HOST_ONLY_SDP)
        await upstream.fire("track", _Track())

        with pytest.raises(SignalingError, match="TURN"):
            await task

    @pytest.mark.asyncio
    async def test_blames_the_cameras_relay_when_it_has_none(self) -> None:
        """The pair that carries media runs to the camera's relay, not ours.

        When the quota is full the camera goes without and offers only an
        unreachable address, which reads as a local network problem unless the
        message says otherwise.
        """
        session = make_session()
        task = asyncio.ensure_future(session.start("v=0\r\nOFFER\r\n"))
        await asyncio.sleep(0)
        upstream = _FakePeerConnection.instances[0]
        upstream.remoteDescription = _Description(
            "v=0\r\na=candidate:2 1 udp 1 185.87.61.1 58303 typ srflx\r\n"
        )
        await upstream.fire("track", _Track())
        await upstream.enter("failed")

        with pytest.raises(SignalingError, match="camera could not get a relay"):
            await task

    @pytest.mark.asyncio
    async def test_does_not_blame_the_relay_when_we_had_one(self) -> None:
        session = make_session()
        with pytest.raises(SignalingError) as caught:
            await _drive(session, connect="failed")
        assert "TURN" not in str(caught.value)


class TestIceServerOrdering:
    """aioice uses only the first STUN server, so which one is first matters."""

    def test_a_udp_stun_url_goes_first(self) -> None:
        """Measured: :5349 yields no srflx, :3478 does.

        Without a reflexive candidate the only workable pair is relay-to-relay,
        which spends an allocation on a quota-limited server for what should be
        ordinary NAT traversal.
        """
        assert ws._order_urls(
            ["stun:stun.l.google.com:5349", "stun:stun1.l.google.com:3478"]
        ) == ["stun:stun1.l.google.com:3478", "stun:stun.l.google.com:5349"]

    def test_an_already_usable_order_is_kept(self) -> None:
        urls = ["stun:stun1.l.google.com:3478", "stun:stun.l.google.com:5349"]
        assert ws._order_urls(urls) == urls

    def test_turn_is_left_exactly_as_connect_sent_it(self) -> None:
        """aioice does speak TURN over TLS, and turns: first is what works."""
        urls = [
            "turns:coturn.prusa3d.com:5349",
            "turn:coturn.prusa3d.com:3478?transport=udp",
        ]
        assert ws._order_urls(urls) == urls

    def test_the_config_we_build_reorders_stun_only(self) -> None:
        config = ws._to_aiortc(
            [
                {
                    "urls": ["turns:coturn.prusa3d.com:5349", "turn:x:3478"],
                    "username": "u",
                    "credential": "c",
                },
                {"urls": ["stun:a:5349", "stun:b:3478"]},
            ]
        )
        assert config.iceServers[0].urls[0] == "turns:coturn.prusa3d.com:5349"
        assert config.iceServers[1].urls[0] == "stun:b:3478"
        assert config.iceServers[0].credential == "c"


class TestLateFailure:
    """A stream that dies after the viewer is answered."""

    @pytest.mark.asyncio
    async def test_closes_itself_and_reports_it(self) -> None:
        closed: list[bool] = []
        session = make_session(on_closed=lambda: closed.append(True))
        await _drive(session)

        await _FakePeerConnection.instances[0].enter("failed")
        await asyncio.sleep(0)

        assert closed == [True]
        assert _FakeSignaling.instances[0].closed

    @pytest.mark.asyncio
    async def test_a_live_stream_is_left_alone(self) -> None:
        closed: list[bool] = []
        session = make_session(on_closed=lambda: closed.append(True))
        await _drive(session)

        await asyncio.sleep(0)
        assert closed == []
        assert not _FakeSignaling.instances[0].closed
