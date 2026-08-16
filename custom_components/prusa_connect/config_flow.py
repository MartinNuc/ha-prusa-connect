"""Config flow for the Prusa Connect integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow

from .auth import (
    AuthenticationError,
    InvalidCredentialsError,
    TotpInvalidError,
    TotpRequiredError,
    authenticate,
    authenticate_totp,
    decode_id_token,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TIMELAPSE,
    CONF_USER_ID,
    DEFAULT_TIMELAPSE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class PrusaConnectOptionsFlow(OptionsFlow):
    """Options for an existing Prusa Connect entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user turn timelapse recording on or off."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TIMELAPSE,
                        default=self.config_entry.options.get(
                            CONF_TIMELAPSE, DEFAULT_TIMELAPSE
                        ),
                    ): bool,
                }
            ),
        )


class PrusaConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Prusa Connect."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return PrusaConnectOptionsFlow()

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._totp_session_data: dict | None = None
        self._email: str | None = None

    def _account_from_tokens(self, tokens: dict) -> tuple[str, str]:
        """Return (user_id, label) for the signed-in account.

        Connect exposes no user endpoint, so identity comes from the id_token.

        The label becomes the config entry's title, which Home Assistant reuses
        verbatim where nothing else identifies us: the reauthentication repair
        reads "Authentication expired for {title}" and is filed under Home
        Assistant Core, not this integration. A bare email address leaves the
        user guessing which of their accounts expired, so the title carries the
        brand as well.
        """
        claims = decode_id_token(tokens["id_token"])
        user = claims.get("user") or {}
        user_id = str(user.get("id") or claims.get("sub") or "")
        account = user.get("email") or self._email
        label = f"Prusa Connect ({account})" if account else "Prusa Connect"
        return user_id, label

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

            except AbortFlow:
                raise

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

            except AbortFlow:
                raise

            except Exception:
                _LOGGER.exception("Unexpected error during TOTP authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="totp",
            data_schema=vol.Schema({vol.Required("totp_code"): str}),
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
            self._email = user_input[CONF_EMAIL]
            try:
                async with aiohttp.ClientSession() as session:
                    tokens = await authenticate(
                        session,
                        user_input[CONF_EMAIL],
                        user_input[CONF_PASSWORD],
                    )
                return await self._async_finish_reauth(tokens)

            except TotpRequiredError as err:
                self._totp_session_data = err.session_data
                return await self.async_step_reauth_totp()

            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"

            except AuthenticationError:
                errors["base"] = "cannot_connect"

            except AbortFlow:
                raise

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

    async def async_step_reauth_totp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle TOTP during reauthentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                async with aiohttp.ClientSession() as session:
                    tokens = await authenticate_totp(
                        session,
                        user_input["totp_code"],
                        self._totp_session_data,
                    )
                return await self._async_finish_reauth(tokens)

            except TotpInvalidError:
                errors["base"] = "invalid_totp"

            except AuthenticationError:
                errors["base"] = "cannot_connect"

            except AbortFlow:
                raise

            except Exception:
                _LOGGER.exception("Unexpected error during TOTP reauthentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_totp",
            data_schema=vol.Schema({vol.Required("totp_code"): str}),
            errors=errors,
        )

    async def _async_finish_login(self, tokens: dict) -> ConfigFlowResult:
        """Create the config entry for the signed-in account."""
        user_id, label = self._account_from_tokens(tokens)

        await self.async_set_unique_id(user_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=label,
            data={
                CONF_ACCESS_TOKEN: tokens["access_token"],
                CONF_REFRESH_TOKEN: tokens["refresh_token"],
                CONF_USER_ID: user_id,
            },
        )

    async def _async_finish_reauth(self, tokens: dict) -> ConfigFlowResult:
        """Update the existing config entry with new tokens."""
        user_id, _label = self._account_from_tokens(tokens)

        await self.async_set_unique_id(user_id)
        self._abort_if_unique_id_mismatch()

        return self.async_update_reload_and_abort(
            self._get_reauth_entry(),
            data_updates={
                CONF_ACCESS_TOKEN: tokens["access_token"],
                CONF_REFRESH_TOKEN: tokens["refresh_token"],
                CONF_USER_ID: user_id,
            },
        )
