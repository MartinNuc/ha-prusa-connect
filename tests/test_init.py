"""Tests for Prusa Connect integration init (setup & unload)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect.const import DOMAIN


async def test_load_unload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test successful setup and unload of config entry."""
    entry = init_integration

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not None
    assert entry.runtime_data.api is not None
    assert entry.runtime_data.printer_coordinator is not None
    assert entry.runtime_data.job_coordinator is not None

    # Unload
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_entry_api_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
) -> None:
    """Test that API error during setup results in SETUP_RETRY."""
    mock_config_entry.add_to_hass(hass)
    mock_api.get_printers.side_effect = ClientError("cannot reach server")

    with (
        patch(
            "custom_components.prusa_connect.PrusaConnectAPI",
            return_value=mock_api,
        ),
        patch(
            "custom_components.prusa_connect.async_get_clientsession",
        ),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
