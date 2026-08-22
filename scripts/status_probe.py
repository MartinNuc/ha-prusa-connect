#!/usr/bin/env python3
"""Read the camera's reported video quality, and check whether setting it takes.

Measuring decoded frames to test the quality setter is slow and burns a TURN
allocation each time (Prusa's coturn quota is easy to exhaust). The camera's
`status` message already reports its own `video.quality` and resolution, so
this asks it directly over Socket.IO with no WebRTC at all.

    export PRUSA_ACCESS_TOKEN=...
    python scripts/status_probe.py        # just report
    python scripts/status_probe.py FHD    # report, set, report again

See docs/CAMERA_PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp  # noqa: E402
import socketio  # noqa: E402
from webrtc_poc import _protocol, fetch_camera, fetch_environment  # noqa: E402

VideoQuality = _protocol.VideoQuality
pb_decode = _protocol.pb_decode
pb_first = _protocol.pb_first

STATUS_WAIT = 10.0


def describe(status: bytes) -> str:
    """Summarise the fields of `status` that bear on stream quality."""
    fields = pb_decode(status)
    out = []

    image = pb_first(fields, 1)
    if image:
        img = pb_decode(image)
        current = pb_first(img, 1)
        if current:
            res = pb_decode(current)
            out.append(
                f"image.current={pb_first(res, 1)}x{pb_first(res, 2)}"
            )
        for supported in img.get(2, []):
            res = pb_decode(supported)
            for entry in res.get(1, []):
                dims = pb_decode(entry)
                out.append(
                    f"image.supported={pb_first(dims, 1)}x{pb_first(dims, 2)}"
                )
        out.append(f"image.quality={pb_first(img, 3)}")

    video = pb_first(fields, 11)
    if video is None:
        out.append("video=ABSENT")
    else:
        raw = pb_first(pb_decode(video), 1)
        name = VideoQuality(raw).name if raw in set(VideoQuality) else raw
        out.append(f"video.quality={name}")

    return "  ".join(str(x) for x in out)


async def main(quality: VideoQuality | None) -> int:
    token = os.environ.get("PRUSA_ACCESS_TOKEN")
    if not token:
        sys.exit("Set PRUSA_ACCESS_TOKEN.")

    async with aiohttp.ClientSession() as session:
        env = await fetch_environment(session)
        camera = await fetch_camera(session, token)

    camera_token = camera["token"]
    sio = socketio.AsyncClient()
    statuses: list[bytes] = []
    arrived = asyncio.Event()

    @sio.on(_protocol.EVENT_STATUS)
    async def _status(data=None) -> None:  # noqa: ANN001
        if isinstance(data, (bytes, bytearray)):
            statuses.append(bytes(data))
            arrived.set()

    await sio.connect(
        f"https://{env['CAMERA_SIGNALING_SERVER']}",
        auth={"token": camera_token},
        transports=["websocket"],
    )

    acked: asyncio.Future = asyncio.get_running_loop().create_future()
    await sio.emit(
        _protocol.EVENT_AUTH,
        _protocol.build_auth(camera_token, token),
        callback=lambda *a: acked.done() or acked.set_result(a),
    )
    await asyncio.wait_for(acked, 15)

    async def poll_status() -> bytes | None:
        arrived.clear()
        before = len(statuses)
        await sio.emit(
            _protocol.EVENT_TRIGGER,
            _protocol.build_command(camera_token, _protocol.CMD_GET_STATUS),
        )
        try:
            await asyncio.wait_for(arrived.wait(), STATUS_WAIT)
        except TimeoutError:
            return None
        return statuses[-1] if len(statuses) > before else None

    status = await poll_status()
    print("before:", describe(status) if status else "no status received")

    if quality is not None:
        print(f"sending Configuration video.quality={quality.name}")
        await sio.emit(
            _protocol.EVENT_CONFIGURATION,
            _protocol.build_video_configuration(camera_token, quality),
        )
        await asyncio.sleep(3)
        status = await poll_status()
        print("after: ", describe(status) if status else "no status received")

    await sio.disconnect()
    return 0


if __name__ == "__main__":
    tier = VideoQuality[sys.argv[1].upper()] if len(sys.argv) > 1 else None
    sys.exit(asyncio.run(main(tier)))
