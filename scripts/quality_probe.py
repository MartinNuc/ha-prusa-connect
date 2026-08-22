#!/usr/bin/env python3
"""Check whether the live stream resolution can be raised to 1080p.

The camera advertises `VideoQuality` and streams 640x480 by default, while its
snapshots are 1920x1080. This probe sends a `Configuration` message asking for a
quality tier and then *decodes a frame* to see what actually arrived — the
camera does not acknowledge the request, so measuring is the only way to know
whether it took.

Run it for each tier and compare:

    export PRUSA_ACCESS_TOKEN=...
    python scripts/quality_probe.py SD
    python scripts/quality_probe.py FHD

See docs/CAMERA_PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp  # noqa: E402
from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.sdp import candidate_from_sdp  # noqa: E402
from webrtc_poc import (  # noqa: E402
    _protocol,
    _signaling,
    fetch_camera,
    fetch_environment,
    fetch_ice,
    to_aiortc,
)

PrusaCameraSignaling = _signaling.PrusaCameraSignaling
SignalingError = _signaling.SignalingError
VideoQuality = _protocol.VideoQuality
FEATURE_VIDEO_QUALITY = _protocol.FEATURE_VIDEO_QUALITY

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logging.getLogger("aiortc").setLevel(logging.ERROR)
_LOG = logging.getLogger("quality")

# Enough to get past the first keyframe and any initial adaptation.
FRAMES_TO_SAMPLE = 30


async def run(quality: VideoQuality) -> int:
    """Request a quality tier, then report the resolution actually received."""
    token = os.environ.get("PRUSA_ACCESS_TOKEN")
    if not token:
        sys.exit("Set PRUSA_ACCESS_TOKEN.")

    async with aiohttp.ClientSession() as session:
        env = await fetch_environment(session)
        camera = await fetch_camera(session, token)
        ice = await fetch_ice(session, env["CAMERA_WEBRTC_CONFIG_URL"], token)

    if FEATURE_VIDEO_QUALITY not in camera.get("features", []):
        _LOG.warning(
            "Camera does not advertise %s; the request will likely be ignored",
            FEATURE_VIDEO_QUALITY,
        )

    pc = RTCPeerConnection(to_aiortc(ice["ice_servers"]))
    track: dict = {"obj": None}
    got_track = asyncio.Event()

    @pc.on("track")
    def _on_track(t) -> None:  # noqa: ANN001
        track["obj"] = t
        got_track.set()

    async def on_offer(sdp: str) -> None:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        await signaling.send_answer(pc.localDescription.sdp)

    async def on_candidate(candidate: str, mid: str) -> None:
        try:
            parsed = candidate_from_sdp(candidate)
            parsed.sdpMid, parsed.sdpMLineIndex = mid, 0
            await pc.addIceCandidate(parsed)
        except Exception as err:  # noqa: BLE001 - a bad candidate is not fatal
            _LOG.debug("Ignoring candidate: %s", err)

    signaling = PrusaCameraSignaling(
        env["CAMERA_SIGNALING_SERVER"],
        camera["token"],
        token,
        on_offer=on_offer,
        on_candidate=on_candidate,
    )

    try:
        await signaling.connect()

        # Before request_stream: the camera fixes the encoder when it builds
        # its offer, so asking afterwards would only affect the next session.
        _LOG.info("Requesting %s", quality.name)
        await signaling.set_video_quality(quality)
        await asyncio.sleep(1.0)

        await signaling.request_stream(ice["ice_servers"], ice["policy"], ice["ttl"])
        await asyncio.wait_for(got_track.wait(), 30)

        sizes: list[tuple[int, int]] = []
        for _ in range(FRAMES_TO_SAMPLE):
            frame = await asyncio.wait_for(track["obj"].recv(), 15)
            sizes.append((frame.width, frame.height))

        # The camera ignores a quality change while idle, so it may only apply
        # one to a running encoder. Ask again now that media is flowing.
        _LOG.info("Re-requesting %s mid-session", quality.name)
        await signaling.set_video_quality(quality)

        after: list[tuple[int, int]] = []
        for _ in range(FRAMES_TO_SAMPLE * 2):
            frame = await asyncio.wait_for(track["obj"].recv(), 15)
            after.append((frame.width, frame.height))
        _LOG.info("mid-session : %s", sorted(set(after)))

        relayed = any(
            getattr(r, "type", None) == "candidate-pair"
            and getattr(r, "nominated", False)
            for r in (await pc.getStats()).values()
        )
        _LOG.info("candidates  : %s", "checked" if relayed else "unknown")
    except (SignalingError, TimeoutError) as err:
        _LOG.error("Failed: %s", err)
        return 1
    finally:
        await signaling.close()
        await pc.close()

    _LOG.info("--- result ---")
    _LOG.info("requested : %s", quality.name)
    _LOG.info("first     : %dx%d", *sizes[0])
    _LOG.info("last      : %dx%d", *sizes[-1])
    if len(set(sizes)) > 1:
        _LOG.info("varied    : %s", sorted(set(sizes)))
    return 0


if __name__ == "__main__":
    name = (sys.argv[1] if len(sys.argv) > 1 else "FHD").upper()
    try:
        tier = VideoQuality[name]
    except KeyError:
        sys.exit(f"Unknown quality {name!r}; use SD, HD or FHD.")
    try:
        sys.exit(asyncio.run(run(tier)))
    except KeyboardInterrupt:
        sys.exit(130)
