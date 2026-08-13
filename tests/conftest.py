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
from enum import StrEnum
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

    helpers.update_coordinator = update_coordinator
    helpers.device_registry = device_registry
    helpers.entity_platform = entity_platform
    helpers.typing = typing_mod
    helpers.aiohttp_client = aiohttp_client

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
    camera.Camera = type("Camera", (), {"__init__": lambda self, *a, **k: None})

    image = _module("homeassistant.components.image")
    image.ImageEntity = type("ImageEntity", (), {"__init__": lambda self, *a, **k: None})


_install_homeassistant_stubs()


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
