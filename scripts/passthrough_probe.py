#!/usr/bin/env python3
"""Check whether encoded H.264 frames can be taken without decoding them.

The media path has one expensive failure mode: relaying through aiortc's
``MediaRelay`` re-encodes 1080p H.264 for every viewer, which a Home Assistant
host should not be doing. This probe tests the alternative — intercepting
frames while they are still encoded — and proves the result is real H.264 by
parsing it back.

It patches ``aiortc.rtcrtpreceiver.get_decoder``, a module-level seam, rather
than reaching into the receiver's private attributes. The stand-in returns no
decoded frames, so nothing is ever handed to a decoder.

Usage:
    export PRUSA_ACCESS_TOKEN=...
    python scripts/passthrough_probe.py [output.h264]
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import aiortc.rtcrtpreceiver as rtcrtpreceiver

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webrtc_poc import (  # noqa: E402
    fetch_camera,
    fetch_environment,
    fetch_ice,
    to_aiortc,
)

import aiohttp  # noqa: E402
from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.sdp import candidate_from_sdp  # noqa: E402

from webrtc_poc import PrusaCameraSignaling, SignalingError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logging.getLogger("aiortc").setLevel(logging.ERROR)
_LOG = logging.getLogger("probe")

CAPTURE_SECONDS = 10


class _CapturingDecoder:
    """Stands in for a real decoder and keeps the encoded frames instead."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def decode(self, encoded_frame):  # noqa: ANN001
        """Take the frame and return nothing to decode."""
        self._sink.append(bytes(encoded_frame.data))
        return []


def install_capture(sink: list) -> None:
    """Replace the decoder factory the receiver's worker thread calls."""
    rtcrtpreceiver.get_decoder = lambda codec: _CapturingDecoder(sink)


async def run(output: Path) -> int:
    """Capture encoded frames from one session and verify them."""
    token = os.environ.get("PRUSA_ACCESS_TOKEN")
    if not token:
        sys.exit("Set PRUSA_ACCESS_TOKEN.")

    frames: list[bytes] = []
    install_capture(frames)

    async with aiohttp.ClientSession() as session:
        env = await fetch_environment(session)
        camera = await fetch_camera(session, token)
        ice = await fetch_ice(session, env["CAMERA_WEBRTC_CONFIG_URL"], token)

    pc = RTCPeerConnection(to_aiortc(ice["ice_servers"]))
    connected = asyncio.Event()

    @pc.on("connectionstatechange")
    async def _state() -> None:
        if pc.connectionState == "connected":
            connected.set()

    async def on_offer(sdp: str) -> None:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        await signaling.send_answer(pc.localDescription.sdp)

    async def on_candidate(candidate: str, mid: str) -> None:
        try:
            parsed = candidate_from_sdp(candidate)
            parsed.sdpMid = mid
            parsed.sdpMLineIndex = 0
            await pc.addIceCandidate(parsed)
        except Exception:  # noqa: BLE001 - a bad candidate is not fatal
            pass

    signaling = PrusaCameraSignaling(
        env["CAMERA_SIGNALING_SERVER"],
        camera["token"],
        token,
        on_offer=on_offer,
        on_candidate=on_candidate,
    )

    try:
        await signaling.connect()
        await signaling.request_stream(ice["ice_servers"], ice["policy"], ice["ttl"])
        await asyncio.wait_for(connected.wait(), 30)
    except (SignalingError, TimeoutError) as err:
        _LOG.error("Could not establish the stream: %s", err)
        await signaling.close()
        await pc.close()
        return 1

    _LOG.info("Connected. Capturing encoded frames for %ss...", CAPTURE_SECONDS)
    await asyncio.sleep(CAPTURE_SECONDS)

    await signaling.close()
    await pc.close()

    if not frames:
        _LOG.error("No encoded frames captured.")
        return 1

    payload = b"".join(frames)
    output.write_bytes(payload)
    _LOG.info(
        "Captured %d frames, %.1f KiB (%.1f fps)",
        len(frames),
        len(payload) / 1024,
        len(frames) / CAPTURE_SECONDS,
    )
    _LOG.info("Wrote %s", output)

    return verify(output, len(frames))


def verify(path: Path, expected_frames: int) -> int:
    """Parse the captured bytes back as H.264 to prove they are intact."""
    try:
        import av
    except ImportError:
        _LOG.warning("PyAV not available; skipping verification")
        return 0

    try:
        with av.open(str(path), format="h264") as container:
            stream = container.streams.video[0]
            decoded = sum(1 for _ in container.decode(stream))
    except Exception as err:  # noqa: BLE001 - a parse failure is the answer
        _LOG.error("Captured data did not parse as H.264: %s", err)
        return 1

    _LOG.info("--- verification ---")
    _LOG.info("codec           : %s", stream.codec_context.name)
    _LOG.info("resolution      : %sx%s", stream.codec_context.width,
              stream.codec_context.height)
    _LOG.info("frames decoded  : %d of %d captured", decoded, expected_frames)

    if decoded:
        _LOG.info("SUCCESS - encoded frames are intact without any decode in the path.")
        return 0

    _LOG.error("Parsed but decoded nothing.")
    return 1


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/prusa_capture.h264")
    try:
        sys.exit(asyncio.run(run(target)))
    except KeyboardInterrupt:
        sys.exit(130)
