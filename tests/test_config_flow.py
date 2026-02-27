"""Tests for the Prusa Connect config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.prusa_connect.auth import (
    AuthenticationError,
    InvalidCredentialsError,
    TotpInvalidError,
    TotpRequiredError,
)
from custom_components.prusa_connect.const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)

MOCK_TOKENS = {
    "access_token": "test-access-token",
    "refresh_token": "test-refresh-token",
}

MOCK_USER = {"id": 12345, "email": "user@example.com"}


async def test_full_user_flow(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test the complete user flow: email/pass -> entry created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with (
        patch(
            "custom_components.prusa_connect.config_flow.authenticate",
            return_value=MOCK_TOKENS,
        ),
        patch(
            "custom_components.prusa_connect.config_flow.PrusaConnectAPI",
        ) as mock_api_cls,
    ):
        mock_api_cls.return_value.get_user = AsyncMock(return_value=MOCK_USER)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret123"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"][CONF_ACCESS_TOKEN] == "test-access-token"
    assert result["data"][CONF_REFRESH_TOKEN] == "test-refresh-token"
    assert result["data"][CONF_USER_ID] == "12345"
    assert mock_setup_entry.called


async def test_user_flow_invalid_credentials(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test the user step with invalid credentials shows error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with patch(
        "custom_components.prusa_connect.config_flow.authenticate",
        side_effect=InvalidCredentialsError("bad creds"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert not mock_setup_entry.called


async def test_user_flow_cannot_connect(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test the user step with a connection error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with patch(
        "custom_components.prusa_connect.config_flow.authenticate",
        side_effect=AuthenticationError("cannot connect"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "pass"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test the user step with an unexpected exception."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with patch(
        "custom_components.prusa_connect.config_flow.authenticate",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "pass"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_totp_flow(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test the user step triggers TOTP and completes."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with patch(
        "custom_components.prusa_connect.config_flow.authenticate",
        side_effect=TotpRequiredError(
            {
                "code_verifier": "cv",
                "totp_url": "https://example.com",
                "totp_csrf": "csrf",
                "jar": None,
            }
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret123"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "totp"

    with (
        patch(
            "custom_components.prusa_connect.config_flow.authenticate_totp",
            return_value=MOCK_TOKENS,
        ),
        patch(
            "custom_components.prusa_connect.config_flow.PrusaConnectAPI",
        ) as mock_api_cls,
    ):
        mock_api_cls.return_value.get_user = AsyncMock(return_value=MOCK_USER)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"totp_code": "123456"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"


async def test_totp_flow_invalid_code(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test TOTP step with invalid code shows error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with patch(
        "custom_components.prusa_connect.config_flow.authenticate",
        side_effect=TotpRequiredError(
            {
                "code_verifier": "cv",
                "totp_url": "https://example.com",
                "totp_csrf": "csrf",
                "jar": None,
            }
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret123"},
        )

    with patch(
        "custom_components.prusa_connect.config_flow.authenticate_totp",
        side_effect=TotpInvalidError("bad code"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"totp_code": "000000"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "totp"
    assert result["errors"] == {"base": "invalid_totp"}


async def test_user_flow_already_configured(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that duplicate user_id aborts."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    with (
        patch(
            "custom_components.prusa_connect.config_flow.authenticate",
            return_value=MOCK_TOKENS,
        ),
        patch(
            "custom_components.prusa_connect.config_flow.PrusaConnectAPI",
        ) as mock_api_cls,
    ):
        mock_api_cls.return_value.get_user = AsyncMock(return_value=MOCK_USER)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret123"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test reauthentication flow updates tokens."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with (
        patch(
            "custom_components.prusa_connect.config_flow.authenticate",
            return_value=MOCK_TOKENS,
        ),
        patch(
            "custom_components.prusa_connect.config_flow.PrusaConnectAPI",
        ) as mock_api_cls,
    ):
        mock_api_cls.return_value.get_user = AsyncMock(return_value=MOCK_USER)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "newpass"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == "test-access-token"


async def test_reauth_flow_with_totp(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
) -> None:
    """Test reauthentication with TOTP flow."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.prusa_connect.config_flow.authenticate",
        side_effect=TotpRequiredError(
            {
                "code_verifier": "cv",
                "totp_url": "https://example.com",
                "totp_csrf": "csrf",
                "jar": None,
            }
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"email": "user@example.com", "password": "pass"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_totp"

    with (
        patch(
            "custom_components.prusa_connect.config_flow.authenticate_totp",
            return_value=MOCK_TOKENS,
        ),
        patch(
            "custom_components.prusa_connect.config_flow.PrusaConnectAPI",
        ) as mock_api_cls,
    ):
        mock_api_cls.return_value.get_user = AsyncMock(return_value=MOCK_USER)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"totp_code": "123456"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
