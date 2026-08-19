"""Service handlers for Prusa Connect."""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .api import PrusaConnectAPI
from .const import DOMAIN
from .coordinator import PrusaConnectPrinterCoordinator

_LOGGER = logging.getLogger(__name__)


_turn_instrumented = False


def _instrument_turn_data() -> None:
    """TEMPORARY: count relay traffic in each direction.

    aioice logs the STUN it exchanges with the TURN server — allocate, bind,
    refresh — but not the data that flows through a bound channel, which is
    where the connectivity checks go. So "we send and nothing comes back" has
    been an inference. This counts both directions so it becomes an
    observation.
    """
    global _turn_instrumented  # noqa: PLW0603
    if _turn_instrumented:
        return
    try:
        import aioice.turn as turn  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return

    sent = {"n": 0, "bytes": 0}
    received = {"n": 0, "bytes": 0}

    original_send = turn.TurnClientMixin.send_data

    async def _counting_send(self, data, addr):  # noqa: ANN001
        sent["n"] += 1
        sent["bytes"] += len(data)
        if sent["n"] in (1, 5, 20):
            _LOGGER.warning(
                "TURN data: sent %d packets (%d bytes) to %s", sent["n"], sent["bytes"], addr
            )
        return await original_send(self, data, addr)

    original_recv = turn.TurnClientMixin.datagram_received

    def _counting_recv(self, data, addr):  # noqa: ANN001
        received["n"] += 1
        received["bytes"] += len(data)
        if received["n"] in (1, 5, 20):
            _LOGGER.warning(
                "TURN data: RECEIVED %d packets (%d bytes) from %s",
                received["n"], received["bytes"], addr,
            )
        return original_recv(self, data, addr)

    turn.TurnClientMixin.send_data = _counting_send
    turn.TurnClientMixin.datagram_received = _counting_recv
    _turn_instrumented = True
    _LOGGER.warning("TURN data counters installed")


async def _async_relay_reachability(hass: HomeAssistant, call: ServiceCall) -> None:
    """TEMPORARY: can this host's TURN relay carry traffic to a known-good peer?

    Sidesteps the camera entirely. Allocates a relay and sends a STUN binding
    request through it to a public STUN server, which answers anything. A reply
    proves the relay path works end to end from Home Assistant; no reply proves
    it does not, and every camera-side theory becomes irrelevant.
    """
    import os  # noqa: PLC0415
    import struct  # noqa: PLC0415

    from aioice import turn  # noqa: PLC0415

    peer = ("74.125.250.129", 19302)  # stun.l.google.com

    data = hass.data.get(DOMAIN, {})
    entry_api = next(
        (c._api for c in data.get("cameras", [])), None  # noqa: SLF001
    )
    if entry_api is None:
        _LOGGER.warning("Relay check: no camera entity to borrow credentials from")
        return

    camera = data["cameras"][0]
    environment = await camera._async_environment()  # noqa: SLF001
    ice = await entry_api.get_webrtc_config(environment["CAMERA_WEBRTC_CONFIG_URL"])
    server = next(
        s
        for s in ice["ice_servers"]
        if any(
            "turn" in u
            for u in (s["urls"] if isinstance(s["urls"], list) else [s["urls"]])
        )
    )

    got: asyncio.Future = asyncio.get_running_loop().create_future()

    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, payload: bytes, addr) -> None:  # noqa: ANN001
            if not got.done():
                got.set_result((len(payload), addr))

    transport, _proto = await turn.create_turn_endpoint(
        _Proto,
        server_addr=("coturn.prusa3d.com", 5349),
        username=server.get("username"),
        password=server.get("credential"),
        ssl=True,
        transport="tcp",
    )
    _LOGGER.warning(
        "Relay check: allocated %s", transport.get_extra_info("sockname")
    )

    request = struct.pack(">HHI12s", 0x0001, 0, 0x2112A442, os.urandom(12))
    for _ in range(4):
        transport.sendto(request, peer)
        try:
            size, addr = await asyncio.wait_for(asyncio.shield(got), 3)
            _LOGGER.warning(
                "Relay check: REPLY %d bytes from %s - relay path WORKS", size, addr
            )
            transport.close()
            return
        except (TimeoutError, asyncio.TimeoutError):
            continue
    _LOGGER.warning("Relay check: NO REPLY - relay path is BROKEN from Home Assistant")
    transport.close()


async def _async_diagnose_stream(hass: HomeAssistant, call: ServiceCall) -> None:
    """TEMPORARY: open a camera stream from inside Home Assistant and report.

    The camera stream connects from a container with one network interface and
    fails from Home Assistant, which runs with host networking and gathers
    eleven candidates. Every experiment so far has cost a deploy, a restart and
    a click from the user; this runs the same attempt on demand, in Home
    Assistant's own network namespace, and writes what happened to the log.

    Removed with the rest of the camera diagnostics.
    """
    from .camera import PrusaConnectCamera  # noqa: PLC0415

    entity_id = call.data.get("entity_id")
    cameras = [
        entity
        for entity in hass.data.get(DOMAIN, {}).get("cameras", [])
        if isinstance(entity, PrusaConnectCamera)
        and (entity_id is None or entity.entity_id == entity_id)
    ]
    camera = cameras[0] if cameras else None
    if camera is None:
        _LOGGER.warning("Stream diagnosis: no Prusa camera entity found")
        return

    from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: PLC0415

    viewer = RTCPeerConnection()
    viewer.addTransceiver("video", direction="recvonly")
    frames = {"n": 0}

    @viewer.on("track")
    def _on_track(track) -> None:  # noqa: ANN001
        async def drain() -> None:
            while True:
                try:
                    await track.recv()
                except Exception:  # noqa: BLE001
                    return
                frames["n"] += 1

        asyncio.ensure_future(drain())

    await viewer.setLocalDescription(await viewer.createOffer())

    sent: list = []

    def _send(message) -> None:  # noqa: ANN001
        sent.append(message)

    from . import webrtc_session as _ws  # noqa: PLC0415

    types = call.data.get("types")
    _ws.DIAGNOSTIC_CANDIDATE_TYPES = set(types.split(",")) if types else None
    import platform  # noqa: PLC0415

    try:
        import importlib.metadata as _md  # noqa: PLC0415

        versions = ", ".join(
            f"{name} {_md.version(name)}" for name in ("aiortc", "aioice", "pylibsrtp")
        )
    except Exception as err:  # noqa: BLE001
        versions = f"<unknown: {err}>"
    _LOGGER.warning(
        "Stream diagnosis: python %s, %s", platform.python_version(), versions
    )
    _instrument_turn_data()
    _LOGGER.warning("Stream diagnosis: starting (types=%s)", types or "all")
    try:
        await camera.async_handle_async_webrtc_offer(
            viewer.localDescription.sdp, "diagnostic", _send
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Stream diagnosis: FAILED - %s", err)
        await viewer.close()
        _ws.DIAGNOSTIC_CANDIDATE_TYPES = None
        return

    if sent:
        await viewer.setRemoteDescription(
            RTCSessionDescription(sdp=sent[0].answer, type="answer")
        )
    await asyncio.sleep(10)
    _LOGGER.warning(
        "Stream diagnosis: viewer=%s frames=%d",
        viewer.connectionState,
        frames["n"],
    )
    camera.close_webrtc_session("diagnostic")
    await viewer.close()
    _ws.DIAGNOSTIC_CANDIDATE_TYPES = None


def _resolve_printer(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[PrusaConnectAPI, PrusaConnectPrinterCoordinator, str]:
    """Resolve a service call's device target to (api, coordinator, uuid)."""
    device_ids = call.data.get("device_id", [])
    if isinstance(device_ids, str):
        device_ids = [device_ids]
    if not device_ids:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_device",
        )

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_ids[0])
    if not device:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
        )

    printer_uuid = None
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN:
            printer_uuid = identifier[1]
            break

    if not printer_uuid:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_prusa_device",
        )

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN:
            data = entry.runtime_data
            return data.api, data.printer_coordinator, printer_uuid

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="entry_not_found",
    )


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up Prusa Connect services."""

    if hass.services.has_service(DOMAIN, "pause_print"):
        return

    # TEMPORARY, paired with the camera diagnostics.
    async def _diagnose(call: ServiceCall) -> None:
        await _async_diagnose_stream(hass, call)

    hass.services.async_register(DOMAIN, "diagnose_stream", _diagnose)

    async def _relay_check(call: ServiceCall) -> None:
        await _async_relay_reachability(hass, call)

    hass.services.async_register(DOMAIN, "relay_check", _relay_check)

    def _make_simple_handler(method_name: str):
        """Build a handler for a command that takes no arguments."""

        async def _handler(call: ServiceCall) -> None:
            api, coordinator, uuid = _resolve_printer(hass, call)
            await getattr(api, method_name)(uuid)
            coordinator.expect_change()
            await coordinator.async_request_refresh()

        return _handler

    for name, method in (
        ("pause_print", "pause_print"),
        ("resume_print", "resume_print"),
        ("stop_print", "stop_print"),
        ("set_ready", "set_ready"),
        ("cancel_ready", "set_unready"),
    ):
        hass.services.async_register(DOMAIN, name, _make_simple_handler(method))

    async def _handle_start_print(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.start_print(uuid, call.data["path"])
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "start_print",
        _handle_start_print,
        schema=vol.Schema(
            {vol.Required("path"): str},
            extra=vol.ALLOW_EXTRA,
        ),
    )

    async def _handle_respond_dialog(call: ServiceCall) -> None:
        api, coordinator, uuid = _resolve_printer(hass, call)
        await api.respond_to_dialog(
            uuid, int(call.data["dialog_id"]), call.data["button"]
        )
        coordinator.expect_change()
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "respond_to_dialog",
        _handle_respond_dialog,
        schema=vol.Schema(
            {
                vol.Required("dialog_id"): vol.Coerce(int),
                vol.Required("button"): str,
            },
            extra=vol.ALLOW_EXTRA,
        ),
    )
