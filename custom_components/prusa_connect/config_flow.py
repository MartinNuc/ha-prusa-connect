"""Config flow for Prusa Connect integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import PrusaConnectAPI
from .auth import (
    AuthenticationError,
    InvalidCredentialsError,
    TotpInvalidError,
    TotpRequiredError,
    authenticate,
    authenticate_totp,
)
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_USER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


class PrusaConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Prusa Connect."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._totp_session_data: dict | None = None
        self._email: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            try:
                async with aiohttp.ClientSession() as session:
                    tokens = await authenticate(
                        session,
                        user_input[CONF_EMAIL],
                        user_input[CONF_PASSWORD],
                    )
                return await self._async_finish_login(tokens)

            except TotpRequiredError as err:
                self._totp_session_data = err.session_data
                return await self.async_step_totp()

            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"

            except AuthenticationError:
                errors["base"] = "cannot_connect"

            except Exception:
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_totp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the TOTP step for two-factor authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                async with aiohttp.ClientSession() as session:
                    tokens = await authenticate_totp(
                        session,
                        user_input["totp_code"],
                        self._totp_session_data,
                    )
                return await self._async_finish_login(tokens)

            except TotpInvalidError:
                errors["base"] = "invalid_totp"

            except AuthenticationError:
                errors["base"] = "cannot_connect"

            except Exception:
                _LOGGER.exception("Unexpected error during TOTP authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="totp",
            data_schema=vol.Schema(
                {
                    vol.Required("totp_code"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth when tokens expire irrecoverably."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation with credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                async with aiohttp.ClientSession() as session:
                    tokens = await authenticate(
                        session,
                        user_input[CONF_EMAIL],
                        user_input[CONF_PASSWORD],
                    )

                # Get user info to verify
                async with aiohttp.ClientSession() as session:
                    api = PrusaConnectAPI(
                        session,
                        tokens["access_token"],
                        tokens["refresh_token"],
                    )
                    user = await api.get_user()

                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                        CONF_USER_ID: user.get("id"),
                    },
                )

            except TotpRequiredError:
                # For reauth with TOTP, we'd need the full flow
                # For simplicity, just show an error
                errors["base"] = "totp_not_supported_reauth"

            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"

            except AuthenticationError:
                errors["base"] = "cannot_connect"

            except Exception:
                _LOGGER.exception("Unexpected error during reauthentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _async_finish_login(self, tokens: dict) -> ConfigFlowResult:
        """Fetch user info and create the config entry."""
        async with aiohttp.ClientSession() as session:
            api = PrusaConnectAPI(
                session,
                tokens["access_token"],
                tokens["refresh_token"],
            )
            user = await api.get_user()

        user_id = str(user.get("id", ""))
        await self.async_set_unique_id(user_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=user.get("email", self._email or "Prusa Connect"),
            data={
                CONF_ACCESS_TOKEN: tokens["access_token"],
                CONF_REFRESH_TOKEN: tokens["refresh_token"],
                CONF_USER_ID: user_id,
            },
        )
