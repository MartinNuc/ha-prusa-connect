#!/usr/bin/env python3
"""End-to-end check of the camera bridge, with a local peer standing in for HA.

``CameraStreamSession`` is the piece the camera entity depends on: it answers a
viewer's offer while simultaneously answering the camera's. Home Assistant's
frontend cannot be driven from here, so this substitutes a plain aiortc peer
that offers exactly as a browser would. Everything between that offer and the
camera is the real code path.

Usage:
    export PRUSA_ACCESS_TOKEN=...
    python scripts/bridge_probe.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webrtc_poc import (  # noqa: E402
    fetch_camera,
    fetch_environment,
    fetch_ice,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logging.getLogger("aiortc").setLevel(logging.ERROR)
_LOG = logging.getLogger("bridge")

OBSERVE_SECONDS = 15


def _load_session_class():  # noqa: ANN202
    """Import the integration's session without importing Home Assistant."""
    import importlib
    import types

    root = Path(__file__).resolve().parent.parent / "custom_components" / "prusa_connect"
    package = types.ModuleType("_prusa")
    package.__path__ = [str(root)]
    sys.modules["_prusa"] = package
    return importlib.import_module("_prusa.webrtc_session").CameraStreamSession


class _ApiShim:
    """Supplies the one method the session needs, without Home Assistant."""

    def __init__(self, config: dict) -> None:
        self._config = config

    async def get_webrtc_config(self, _url: str) -> dict:
        return self._config


async def run() -> int:
    """Bridge one session and confirm video reaches the stand-in viewer."""
    token = os.environ.get("PRUSA_ACCESS_TOKEN")
    if not token:
        sys.exit("Set PRUSA_ACCESS_TOKEN.")

    CameraStreamSession = _load_session_class()

    async with aiohttp.ClientSession() as http:
        env = await fetch_environment(http)
        camera = await fetch_camera(http, token)
        ice = await fetch_ice(http, env["CAMERA_WEBRTC_CONFIG_URL"], token)

    session = CameraStreamSession(
        _ApiShim(ice),
        env["CAMERA_SIGNALING_SERVER"],
        env["CAMERA_WEBRTC_CONFIG_URL"],
        camera["token"],
        token,
    )

    # Stand in for the Home Assistant frontend: offer, receive-only video.
    viewer = RTCPeerConnection()
    viewer.addTransceiver("video", direction="recvonly")

    frames = {"count": 0}
    got_track = asyncio.Event()

    @viewer.on("track")
    def _on_track(track) -> None:  # noqa: ANN001
        _LOG.info("Viewer received a %s track", track.kind)
        got_track.set()

        async def drain() -> None:
            while True:
                try:
                    await track.recv()
                except Exception:  # noqa: BLE001 - ends with the session
                    return
                frames["count"] += 1

        asyncio.ensure_future(drain())

    await viewer.setLocalDescription(await viewer.createOffer())
    _LOG.info("Viewer offer created; bridging to the camera...")

    try:
        answer_sdp = await session.start(viewer.localDescription.sdp)
    except Exception as err:  # noqa: BLE001 - report and bail
        _LOG.error("Bridge failed: %s", err)
        await session.close()
        await viewer.close()
        return 1

    _LOG.info("Bridge answered (%d bytes of SDP)", len(answer_sdp))
    await viewer.setRemoteDescription(
        RTCSessionDescription(sdp=answer_sdp, type="answer")
    )

    try:
        await asyncio.wait_for(got_track.wait(), 30)
    except TimeoutError:
        _LOG.error("Viewer never received a track")
        await session.close()
        await viewer.close()
        return 1

    await asyncio.sleep(OBSERVE_SECONDS)

    _LOG.info("--- result ---")
    _LOG.info("viewer connection : %s", viewer.connectionState)
    _LOG.info("frames at viewer  : %d in %ss", frames["count"], OBSERVE_SECONDS)

    ok = frames["count"] > 0
    _LOG.info(
        "SUCCESS - video reached the viewer through the bridge."
        if ok
        else "FAILED - no video reached the viewer."
    )

    await session.close()
    await viewer.close()
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)
