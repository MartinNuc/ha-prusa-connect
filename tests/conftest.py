"""Test setup for the Prusa Connect integration.

These tests exercise the integration's own logic — endpoint construction,
response-envelope handling, the printer/job field mapping and the auth flow —
against fixtures captured from the live Prusa Connect API.

Home Assistant itself is stubbed rather than installed. The integration targets
HA 2025.3+ (for ``AddConfigEntryEntitiesCallback``), which requires Python 3.13,
so depending on ``pytest-homeassistant-custom-component`` would pin the whole
suite to one interpreter. Stubbing keeps these runnable anywhere and focuses
them on the layer where the bugs actually were: the API contract.

What this does NOT cover: entity registration, coordinator scheduling and other
Home Assistant wiring. Those need a real HA runtime.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from enum import IntFlag, StrEnum
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(Path(__file__).parent.parent))


def _module(name: str) -> types.ModuleType:
    """Register and return a stub module."""
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


class _Subscriptable:
    """Base tolerating ``Class[...]`` subscripting."""

    def __class_getitem__(cls, item):  # noqa: D105
        return cls


async def _noop_coroutine(*_args, **_kwargs) -> None:
    """Stand-in for HA base-class coroutines the integration calls via super()."""


def _attr_property(name: str, default=None) -> property:
    """Mirror HA's convention of exposing ``_attr_x`` as the property ``x``."""
    return property(lambda self: getattr(self, f"_attr_{name}", default))


def _dataclass_like(name: str, field: str) -> type:
    """A tiny value object mirroring HA's WebRTC message types."""

    def __init__(self, value) -> None:  # noqa: ANN001
        setattr(self, field, value)

    def __eq__(self, other) -> bool:  # noqa: ANN001
        return isinstance(other, type(self)) and getattr(self, field) == getattr(
            other, field
        )

    return type(name, (), {"__init__": __init__, "__eq__": __eq__})


@dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    """Stand-in for HA's entity description dataclasses."""

    key: str
    translation_key: str | None = None
    device_class: object | None = None
    state_class: object | None = None
    native_unit_of_measurement: str | None = None
    suggested_display_precision: int | None = None
    icon: str | None = None
    entity_category: str | None = None
    options: list | None = None


def _install_homeassistant_stubs() -> None:
    """Install the subset of Home Assistant the integration imports."""
    core = _module("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda fn: fn
    core.ServiceCall = type("ServiceCall", (), {})

    const = _module("homeassistant.const")
    const.PERCENTAGE = "%"
    const.CONF_EMAIL = "email"
    const.CONF_PASSWORD = "password"
    const.EntityCategory = StrEnum("EntityCategory", {"DIAGNOSTIC": "diagnostic"})
    const.UnitOfTemperature = StrEnum("UnitOfTemperature", {"CELSIUS": "°C"})
    const.UnitOfLength = StrEnum("UnitOfLength", {"MILLIMETERS": "mm"})
    const.UnitOfTime = StrEnum("UnitOfTime", {"SECONDS": "s"})
    const.Platform = StrEnum(
        "Platform",
        {
            "SENSOR": "sensor",
            "BINARY_SENSOR": "binary_sensor",
            "BUTTON": "button",
            "CAMERA": "camera",
            "IMAGE": "image",
        },
    )

    exceptions = _module("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    exceptions.ServiceValidationError = type(
        "ServiceValidationError", (Exception,), {}
    )

    ha = _module("homeassistant")
    ha.core, ha.const, ha.exceptions = core, const, exceptions

    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (_Subscriptable,), {})
    config_entries.ConfigFlow = type(
        "ConfigFlow", (), {"__init_subclass__": classmethod(lambda cls, **kw: None)}
    )
    config_entries.ConfigFlowResult = dict
    config_entries.OptionsFlow = type("OptionsFlow", (), {})
    config_entries.OptionsFlowWithReload = type("OptionsFlowWithReload", (), {})

    data_entry_flow = _module("homeassistant.data_entry_flow")
    data_entry_flow.AbortFlow = type("AbortFlow", (Exception,), {})

    helpers = _module("homeassistant.helpers")

    update_coordinator = _module("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = type(
        "DataUpdateCoordinator",
        (_Subscriptable,),
        {"__init__": lambda self, *a, **k: None},
    )
    update_coordinator.CoordinatorEntity = type(
        "CoordinatorEntity",
        (_Subscriptable,),
        {"__init__": lambda self, *a, **k: None, "available": True},
    )
    update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})

    device_registry = _module("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    device_registry.async_get = lambda hass: None

    entity_platform = _module("homeassistant.helpers.entity_platform")
    entity_platform.AddConfigEntryEntitiesCallback = object

    typing_mod = _module("homeassistant.helpers.typing")
    typing_mod.StateType = object

    aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None

    event = _module("homeassistant.helpers.event")

    def _track_time_interval(hass, action, interval, **kwargs):
        """Record the callback and hand back a canceller.

        Tests drive capture explicitly; what matters here is that the recorder
        registers and cancels the timer, not that real time passes.
        """
        return lambda: None

    event.async_track_time_interval = _track_time_interval

    util = _module("homeassistant.util")
    dt_util = _module("homeassistant.util.dt")
    dt_util.now = lambda: __import__("datetime").datetime(2026, 8, 14, 20, 30, 0)
    util.dt = dt_util

    helpers.update_coordinator = update_coordinator
    helpers.device_registry = device_registry
    helpers.entity_platform = entity_platform
    helpers.typing = typing_mod
    helpers.aiohttp_client = aiohttp_client
    helpers.event = event

    _module("homeassistant.components")

    sensor = _module("homeassistant.components.sensor")
    sensor.SensorDeviceClass = StrEnum(
        "SensorDeviceClass",
        {
            "TEMPERATURE": "temperature",
            "DURATION": "duration",
            "DISTANCE": "distance",
            "ENUM": "enum",
        },
    )
    sensor.SensorStateClass = StrEnum(
        "SensorStateClass", {"MEASUREMENT": "measurement"}
    )
    sensor.SensorEntityDescription = _EntityDescription
    sensor.SensorEntity = type("SensorEntity", (), {})

    binary_sensor = _module("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorDeviceClass = StrEnum(
        "BinarySensorDeviceClass",
        {"CONNECTIVITY": "connectivity", "RUNNING": "running", "PROBLEM": "problem"},
    )
    binary_sensor.BinarySensorEntityDescription = _EntityDescription
    binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})

    button = _module("homeassistant.components.button")
    button.ButtonEntityDescription = _EntityDescription
    button.ButtonEntity = type("ButtonEntity", (), {})

    camera = _module("homeassistant.components.camera")
    camera.Camera = type(
        "Camera",
        (),
        {
            "__init__": lambda self, *a, **k: None,
            "async_will_remove_from_hass": _noop_coroutine,
            # HA's Entity exposes _attr_* through properties; mirror the few
            # the camera entity sets so tests can read them as HA would.
            "unique_id": _attr_property("unique_id"),
            "name": _attr_property("name"),
            "is_streaming": _attr_property("is_streaming", False),
            "supported_features": _attr_property("supported_features", 0),
            "frame_interval": _attr_property("frame_interval", 0.0),
        },
    )
    camera.CameraEntityFeature = IntFlag("CameraEntityFeature", {"STREAM": 2})
    camera.WebRTCAnswer = _dataclass_like("WebRTCAnswer", "answer")
    camera.WebRTCCandidate = _dataclass_like("WebRTCCandidate", "candidate")
    camera.WebRTCSendMessage = object

    image = _module("homeassistant.components.image")
    image.ImageEntity = type("ImageEntity", (), {"__init__": lambda self, *a, **k: None})


def _install_aiortc_stub() -> None:
    """Stub aiortc so camera logic is testable without the media stack.

    Tests mostly substitute their own stream session, but the ICE configuration
    we hand aiortc is our own logic and worth asserting on, so the stubs keep
    their keyword arguments rather than discarding them.
    """
    if "aiortc" in sys.modules:
        return

    def _keep_kwargs(self, *_args, **kwargs) -> None:  # noqa: ANN001
        self.__dict__.update(kwargs)

    aiortc = _module("aiortc")
    for name in (
        "RTCConfiguration",
        "RTCIceServer",
        "RTCPeerConnection",
        "RTCSessionDescription",
    ):
        setattr(aiortc, name, type(name, (), {"__init__": _keep_kwargs}))

    media = _module("aiortc.contrib.media")
    media.MediaRelay = type("MediaRelay", (), {"__init__": lambda self, *a, **k: None})
    _module("aiortc.contrib").media = media

    sdp = _module("aiortc.sdp")
    sdp.candidate_from_sdp = lambda value: value


def _install_socketio_stub() -> None:
    """Stub python-socketio so signalling logic is testable without the dep.

    Tests substitute their own recording client for ``AsyncClient``; this only
    needs to exist so the import succeeds.
    """
    if "socketio" in sys.modules:
        return
    socketio = _module("socketio")
    socketio.AsyncClient = type("AsyncClient", (), {})


_install_homeassistant_stubs()
_install_socketio_stub()
_install_aiortc_stub()


def _load(name: str) -> dict:
    """Load a fixture captured from the live API."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def printers() -> list[dict]:
    """The /app/printers collection."""
    return _load("printers.json")["printers"]


@pytest.fixture
def printer(printers: list[dict]) -> dict:
    """A printer as the coordinator assembles it: list entry merged with detail."""
    return {**printers[0], **_load("printer_detail.json")}


@pytest.fixture
def job() -> dict:
    """The most recent job for the printer."""
    return _load("jobs.json")["jobs"][0]


@pytest.fixture
def supported_commands() -> list[dict]:
    """The printer's advertised command set."""
    return _load("supported_commands.json")["commands"]
