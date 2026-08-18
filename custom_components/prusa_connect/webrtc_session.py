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
from collections.abc import Callable
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
from .webrtc_protocol import MEDIA_MID

if TYPE_CHECKING:
    from .api import PrusaConnectAPI

_LOGGER = logging.getLogger(__name__)

# How long to wait for the camera stream to come up before giving up on a viewer.
# Covers negotiation *and* connection: a track object appears within a second of
# the camera's offer, but ICE can take several seconds more over a relay.
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
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the session. Nothing is opened until :meth:`start`.

        ``on_closed`` is called if the camera connection dies after the viewer
        has been answered, so the owner can drop a session that is no longer
        carrying anything.
        """
        self._api = api
        self._signaling_host = signaling_host
        self._webrtc_config_url = webrtc_config_url
        self._camera_token = camera_token
        self._access_token = access_token
        self._on_closed = on_closed

        self._signaling: PrusaCameraSignaling | None = None
        self._upstream: RTCPeerConnection | None = None
        self._downstream: RTCPeerConnection | None = None
        self._relay = MediaRelay()
        self._track_ready: asyncio.Future | None = None
        self._upstream_live: asyncio.Future | None = None
        self._answered = False
        self._closed = False

    async def start(self, offer_sdp: str) -> str:
        """Open the camera stream and answer the viewer's offer.

        The upstream session must be established first: the answer we give the
        viewer has to describe a track that already exists.
        """
        loop = asyncio.get_running_loop()
        self._track_ready = loop.create_future()
        self._upstream_live = loop.create_future()

        try:
            track = await self._connect_upstream()
            answer = await self._answer_downstream(offer_sdp, track)
        except Exception:
            await self.close()
            raise

        self._answered = True
        return answer

    async def _connect_upstream(self):  # noqa: ANN202 - aiortc track type
        """Negotiate with the camera and return its video track."""
        config = await self._api.get_webrtc_config(self._webrtc_config_url)

        pc = RTCPeerConnection(_to_aiortc(config["ice_servers"]))
        self._upstream = pc

        @pc.on("track")
        def _on_track(track) -> None:  # noqa: ANN001
            _LOGGER.debug("Camera track received (%s)", track.kind)
            _resolve(self._track_ready, track)

        @pc.on("connectionstatechange")
        async def _on_upstream_state() -> None:
            state = pc.connectionState
            _LOGGER.debug("Camera connection: %s", state)
            if state == "connected":
                _resolve(self._upstream_live, True)
            elif state in ("failed", "closed"):
                # Before the viewer is answered this is a start-up failure; after
                # it, the stream has died under a viewer who would otherwise sit
                # watching a frozen picture while the session stayed open.
                if not self._answered:
                    _resolve(self._upstream_live, False)
                elif not self._closed:
                    _LOGGER.info("Camera connection %s; closing the session", state)
                    asyncio.get_running_loop().create_task(self._async_abandon())

        async def on_offer(sdp: str) -> None:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            await pc.setLocalDescription(await pc.createAnswer())
            assert self._signaling is not None
            await self._signaling.send_answer(pc.localDescription.sdp)
            await self._async_trickle(pc.localDescription.sdp)

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

        deadline = asyncio.get_running_loop().time() + UPSTREAM_TIMEOUT
        track = await _before(
            self._track_ready, deadline, "the camera sent no video track"
        )

        # A track object exists as soon as the camera's offer is applied, long
        # before any media can flow. Answering the viewer here would hand the
        # frontend a stream that never produces a frame — a spinner forever, and
        # a session nobody tears down. Wait for the connection itself.
        #
        # Timing out and failing outright get the same explanation: a missing
        # relay strands the connection in "checking" rather than failing it, so
        # the timeout is the path that most needs the diagnosis.
        try:
            connected = await _before(self._upstream_live, deadline, "connecting")
        except SignalingError as err:
            raise SignalingError(self._connect_failure()) from err

        if not connected:
            raise SignalingError(self._connect_failure())

        return track

    async def _async_trickle(self, sdp: str) -> None:
        """Send our ICE candidates to the camera, one message each.

        aiortc finishes gathering before ``setLocalDescription`` returns, so the
        answer already lists every candidate — and the camera ignores them
        there. It offers ``a=ice-options:ice2,trickle`` and, as a capture of the
        working web client shows, expects each candidate as its own CANDIDATE
        message. Without them it never learns an address to send to, so ICE sits
        in ``checking`` until it times out: an offer arrives, an answer goes
        back, and nothing connects.

        The candidates stay in the answer as well. The web client omits them,
        but leaving them costs nothing and keeps the SDP self-describing for any
        peer that does read them.
        """
        if self._signaling is None:
            return

        # The answer has an m-section for video and another for the data
        # channel, and aiortc repeats the same candidate lines under each. They
        # describe one transport, so sending them twice just doubles the
        # signalling traffic for no gain.
        seen: set[str] = set()
        for line in sdp.replace("\r\n", "\n").split("\n"):
            candidate = line.strip()
            if not candidate.startswith("a=candidate:") or candidate in seen:
                continue
            seen.add(candidate)
            try:
                await self._signaling.send_candidate(candidate, MEDIA_MID)
            except Exception:  # noqa: BLE001 - one candidate is not the session
                _LOGGER.debug("Could not send candidate %r", candidate[:60])

    def _connect_failure(self) -> str:
        """Explain a failed camera connection, checking the likeliest cause.

        Prusa's TURN server allows only a handful of allocations per account and
        answers "486 Allocation Quota Reached" beyond that, holding the refusal
        for the ten-minute lifetime of the allocations already out. Both ends
        draw on that same pool, so the interesting question is which of us went
        without — the messages differ because the remedies differ, and because
        "did not connect" sends people looking at their own network.

        Measured on a working connection: the pair that carries the media runs
        to the *camera's* relay. Ours is the less important of the two.
        """
        ours = _sdp_of(self._upstream, "localDescription")
        theirs = _sdp_of(self._upstream, "remoteDescription")

        # What each side had to offer is the whole diagnosis, and it is gone the
        # moment the session closes. At warning level so it survives in the log
        # of an installation that is not running with debug turned on.
        _LOGGER.warning(
            "Camera stream did not connect. We offered %s; the camera offered %s",
            _candidate_types(ours) or "nothing",
            _candidate_types(theirs) or "nothing",
        )

        if "typ relay" not in theirs:
            return (
                "The camera could not get a relay on Prusa's TURN server, which "
                "limits how many are open at once, so there was no route to it. "
                "Wait a few minutes before trying again — opening the stream "
                "repeatedly keeps the limit full"
            )
        if "typ relay" not in ours:
            return (
                "Could not allocate a relay on Prusa's TURN server, so there was "
                "no route to the camera. This usually clears on its own after a "
                "few minutes"
            )
        return "The camera stream did not connect"

    async def _async_abandon(self) -> None:
        """Close a session whose camera connection died, and say so."""
        await self.close()
        if self._on_closed is not None:
            self._on_closed()

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


def _candidate_types(sdp: str) -> str:
    """Summarise an SDP's candidates as "host, srflx, relay".

    Which kinds each side gathered is what decides whether a connection was
    ever possible, and the addresses themselves are noise in a log.
    """
    kinds: list[str] = []
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if " typ " not in line or not line.strip().startswith("a=candidate:"):
            continue
        kind = line.split(" typ ", 1)[1].split()[0]
        if kind not in kinds:
            kinds.append(kind)
    return ", ".join(kinds)


def _sdp_of(pc, which: str) -> str:  # noqa: ANN001
    """The SDP on one side of a peer connection, or "" if there is not one yet."""
    description = getattr(pc, which, None) if pc is not None else None
    return getattr(description, "sdp", "") or ""


def _resolve(future: asyncio.Future | None, value: Any) -> None:
    """Complete a future once, ignoring later or duplicate answers."""
    if future is not None and not future.done():
        future.set_result(value)


async def _before(future: asyncio.Future, deadline: float, what: str) -> Any:
    """Await a future against a shared deadline.

    Both waits in start-up share one budget, so a slow negotiation cannot buy
    the connection a second full timeout and leave the viewer hanging twice as
    long as ``UPSTREAM_TIMEOUT`` promises.
    """
    remaining = deadline - asyncio.get_running_loop().time()
    try:
        return await asyncio.wait_for(future, max(remaining, 0.0))
    except TimeoutError as err:
        raise SignalingError(f"Timed out: {what}") from err


def _to_aiortc(ice_servers: list[dict[str, Any]]) -> RTCConfiguration:
    """Translate Connect's ICE configuration into aiortc's form.

    We must allocate a relay of our own, and it is worth being explicit about
    why, because the opposite was tried and it broke the stream outright.

    The reasoning for dropping it looked sound: the pair that carries the media
    runs to the *camera's* relay, sending to a relay is ordinary outbound
    traffic, and both ends draw on one small per-account quota, so ours seemed
    a waste that starved the camera. Measured back to back, sixty seconds
    apart, on the same camera:

        our config STUN only   -> the camera offered nothing      -> failed
        our config with TURN   -> the camera offered a relay       -> 343 frames

    The camera evidently gathers a relay only when its peer has one. Whatever
    the mechanism, ours is what makes the camera's appear, and without it there
    is no route at all.

    Only the ordering differs from what Connect sends, and only for STUN — the
    list handed to the camera must stay byte-identical to the browser's.
    """
    return RTCConfiguration(
        iceServers=[
            RTCIceServer(
                urls=_order_urls(
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


def _order_urls(urls: list[str]) -> list[str]:
    """Put a UDP-reachable STUN URL first.

    aioice uses only the *first* STUN server it is given, and speaks STUN over
    UDP only. Connect lists ``stun:stun.l.google.com:5349`` first — 5349 is the
    TLS port, so the binding request goes nowhere, no server-reflexive candidate
    is gathered, and the only address we can offer is a private one. A browser
    tries every URL and never notices the difference.

    Without a reflexive candidate the sole workable pair is relay-to-relay,
    which forces an allocation on Prusa's quota-limited TURN server for what
    should be an ordinary NAT traversal.

    TURN is left alone: aioice does speak TURN over TLS, and the ``turns:`` URL
    Connect puts first works.
    """
    if not all(str(url).startswith("stun:") for url in urls):
        return urls
    return sorted(urls, key=lambda url: ":5349" in str(url))
