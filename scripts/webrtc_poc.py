#!/usr/bin/env python3
"""Live check of the camera WebRTC path, using the integration's own modules.

This exists to prove the real code works against the live service — it imports
``signaling`` and ``webrtc_protocol`` rather than reimplementing them, so a
successful run validates what actually ships, not a parallel copy.

It also reports the negotiated codec, which decides whether the media path can
forward RTP untouched or has to transcode.

Usage:
    pip install aiohttp aiortc "python-socketio[asyncio_client]"
    export PRUSA_ACCESS_TOKEN=...
    python scripts/webrtc_poc.py

See docs/CAMERA_PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

def _load_integration_modules() -> tuple:
    """Import the integration's protocol modules without importing Home Assistant.

    ``custom_components/prusa_connect/__init__.py`` pulls in HA, which is not
    available outside a running instance. The modules used here have no HA
    imports, so bind them under a synthetic package: relative imports resolve
    normally and the real package ``__init__`` never runs.
    """
    import importlib
    import types

    root = Path(__file__).resolve().parent.parent / "custom_components" / "prusa_connect"
    package = types.ModuleType("_prusa")
    package.__path__ = [str(root)]
    sys.modules["_prusa"] = package

    const = importlib.import_module("_prusa.const")
    protocol = importlib.import_module("_prusa.webrtc_protocol")
    signaling_mod = importlib.import_module("_prusa.signaling")
    return const, protocol, signaling_mod


_const, _protocol, _signaling = _load_integration_modules()

ENVIRONMENT_DEFAULTS = _const.ENVIRONMENT_DEFAULTS
ENVIRONMENT_URL = _const.ENVIRONMENT_URL
has_turn_server = _protocol.has_turn_server
trim_ice_servers = _protocol.trim_ice_servers
PrusaCameraSignaling = _signaling.PrusaCameraSignaling
SignalingError = _signaling.SignalingError

logging.basicConfig(
    level=os.environ.get("POC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
# aiortc logs a decode failure per packet if anything pulls frames; we do not.
logging.getLogger("aiortc").setLevel(logging.ERROR)
_LOG = logging.getLogger("poc")

CAMERAS_URL = "https://connect.prusa3d.com/app/cameras"
OBSERVE_SECONDS = 20


async def fetch_environment(session: aiohttp.ClientSession) -> dict[str, str]:
    """Read Connect's runtime config, falling back to known defaults."""
    try:
        async with session.get(ENVIRONMENT_URL) as resp:
            resp.raise_for_status()
            text = await resp.text()
    except aiohttp.ClientError:
        return dict(ENVIRONMENT_DEFAULTS)

    env = {}
    for line in text.splitlines():
        if line.startswith("window.") and "=" in line:
            key, _, value = line[len("window.") :].partition("=")
            env[key.strip()] = value.strip().rstrip(";").strip().strip("'\"")
    return {**ENVIRONMENT_DEFAULTS, **{k: v for k, v in env.items() if v}}


async def fetch_camera(session: aiohttp.ClientSession, token: str) -> dict:
    """Pick a registered camera that advertises WebRTC."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with session.get(CAMERAS_URL, headers=headers) as resp:
        resp.raise_for_status()
        cameras = (await resp.json()).get("cameras", [])

    usable = [c for c in cameras if c.get("registered") and c.get("token")]
    if not usable:
        sys.exit("No registered camera with a token on this account.")

    wanted = os.environ.get("PRUSA_CAMERA_ID")
    if wanted:
        usable = [c for c in usable if str(c.get("id")) == wanted] or usable

    camera = usable[0]
    _LOG.info(
        "Camera %s (%s) model=%s fw=%s",
        camera.get("id"),
        camera.get("name"),
        camera["config"].get("model"),
        camera["config"].get("firmware"),
    )
    if "WebRtc" not in camera.get("features", []):
        _LOG.warning("Camera does not advertise the WebRtc feature")
    return camera


async def fetch_ice(session: aiohttp.ClientSession, url: str, token: str) -> dict:
    """Fetch and trim the ICE configuration."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    config = payload.get("configuration", payload)
    servers = config.get("iceServers") or []

    if not has_turn_server(servers):
        sys.exit(
            "No TURN server returned. The token lacks the 'connect' scope — "
            "reauthenticate the integration."
        )

    return {
        "ice_servers": trim_ice_servers(servers),
        "policy": config.get("iceTransportPolicy"),
        "ttl": int(payload.get("ttl") or 0),
    }


def to_aiortc(ice_servers: list[dict]) -> RTCConfiguration:
    """Translate the ICE config into aiortc's form."""
    return RTCConfiguration(
        iceServers=[
            RTCIceServer(
                urls=s["urls"] if isinstance(s["urls"], list) else [s["urls"]],
                username=s.get("username"),
                credential=s.get("credential"),
            )
            for s in ice_servers
        ]
    )


def codecs_from_sdp(sdp: str) -> list[str]:
    """Pull codec names out of an SDP offer."""
    return sorted({m.group(1) for m in re.finditer(r"a=rtpmap:\d+ ([A-Za-z0-9]+)/", sdp)})


async def run() -> int:
    """Establish one session and report what came back."""
    token = os.environ.get("PRUSA_ACCESS_TOKEN")
    if not token:
        sys.exit("Set PRUSA_ACCESS_TOKEN.")

    async with aiohttp.ClientSession() as session:
        env = await fetch_environment(session)
        camera = await fetch_camera(session, token)
        ice = await fetch_ice(session, env["CAMERA_WEBRTC_CONFIG_URL"], token)

    _LOG.info(
        "ICE: %d server(s), ttl=%ss, TURN present",
        len(ice["ice_servers"]),
        ice["ttl"],
    )

    pc = RTCPeerConnection(to_aiortc(ice["ice_servers"]))
    state: dict = {"track": None, "codecs": []}
    connected = asyncio.Event()

    @pc.on("track")
    def _on_track(track) -> None:  # noqa: ANN001
        _LOG.info("Track: kind=%s id=%s", track.kind, track.id)
        state["track"] = track

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        _LOG.info("Connection state: %s", pc.connectionState)
        if pc.connectionState == "connected":
            connected.set()

    async def on_offer(sdp: str) -> None:
        state["codecs"] = codecs_from_sdp(sdp)
        _LOG.info("Offer received (%d bytes), codecs: %s",
                  len(sdp), ", ".join(state["codecs"]) or "none")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await signaling.send_answer(pc.localDescription.sdp)
        _LOG.info("Answer sent")

    async def on_candidate(candidate: str, mid: str) -> None:
        try:
            parsed = candidate_from_sdp(candidate)
            parsed.sdpMid = mid
            parsed.sdpMLineIndex = 0
            await pc.addIceCandidate(parsed)
        except Exception as err:  # noqa: BLE001 - a bad candidate is not fatal
            _LOG.debug("Ignoring candidate %r: %s", candidate[:60], err)

    signaling = PrusaCameraSignaling(
        env["CAMERA_SIGNALING_SERVER"],
        camera["token"],
        token,
        on_offer=on_offer,
        on_candidate=on_candidate,
    )

    try:
        await signaling.connect()
        _LOG.info("Camera is live (session %s)", signaling.session_id)
        await signaling.request_stream(
            ice["ice_servers"], ice["policy"], ice["ttl"]
        )
        await asyncio.wait_for(connected.wait(), 30)
    except (SignalingError, TimeoutError) as err:
        _LOG.error("Failed: %s", err)
        await signaling.close()
        await pc.close()
        return 1

    # Read RTP counters rather than pulling frames: decoding is exactly the cost
    # the real integration must avoid, so the check should not depend on it.
    await asyncio.sleep(OBSERVE_SECONDS)
    packets = bytes_received = 0
    for report in (await pc.getStats()).values():
        if getattr(report, "type", None) == "inbound-rtp":
            packets += getattr(report, "packetsReceived", 0) or 0
            bytes_received += getattr(report, "bytesReceived", 0) or 0

    _LOG.info("--- result ---")
    _LOG.info("connection      : %s", pc.connectionState)
    _LOG.info("track           : %s", state["track"].kind if state["track"] else "none")
    _LOG.info("codecs          : %s", ", ".join(state["codecs"]) or "unknown")
    _LOG.info("packets         : %d in %ss", packets, OBSERVE_SECONDS)
    if bytes_received:
        _LOG.info("bitrate         : %.0f kbit/s",
                  bytes_received * 8 / OBSERVE_SECONDS / 1000)

    ok = packets > 0
    _LOG.info("SUCCESS - media is flowing." if ok else "FAILED - no media.")

    await signaling.close()
    await pc.close()
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)
