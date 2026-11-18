"""Config flow for Powerquartier."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import account_key, make_client
from .api import PowerquartierAuthError, PowerquartierError, Profile
from .const import CONF_DAYS_BACK, CONF_PROFILES, DEFAULT_DAYS_BACK, DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


class PowerquartierConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in, discover profiles, let the user pick which to import."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._profiles: list[Profile] = []

    async def _try_login(self, email: str, password: str) -> tuple[list[Profile], str | None]:
        client = make_client(self.hass, email, password)
        try:
            return await client.async_get_profiles(), None
        except PowerquartierAuthError:
            return [], "invalid_auth"
        except (PowerquartierError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Powerquartier login test failed: %s", err)
            return [], "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during Powerquartier login")
            return [], "unknown"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            profiles, error = await self._try_login(email, user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            else:
                # Set only after a successful login, and let a new dialog take
                # over from an abandoned one instead of being blocked by it.
                await self.async_set_unique_id(account_key(email), raise_on_progress=False)
                self._abort_if_unique_id_configured()
                self._email, self._password, self._profiles = (
                    email,
                    user_input[CONF_PASSWORD],
                    profiles,
                )
                return await self.async_step_profiles()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_profiles(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = {p.account_id: f"{p.name} → {p.slug}" for p in self._profiles}
        if user_input is not None:
            chosen = [p for p in self._profiles if p.account_id in user_input[CONF_PROFILES]]
            if chosen:
                return self.async_create_entry(
                    title="Powerquartier",
                    data={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        CONF_PROFILES: [
                            {"account_id": p.account_id, "name": p.name} for p in chosen
                        ],
                    },
                )
        return self.async_show_form(
            step_id="profiles",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROFILES, default=list(options)): cv.multi_select(
                        options
                    )
                }
            ),
            description_placeholders={"count": str(len(options))},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            _, error = await self._try_login(self._email or "", user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={"email": self._email or ""},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PowerquartierOptionsFlow()


class PowerquartierOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_DAYS_BACK: int(user_input[CONF_DAYS_BACK])}
            )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DAYS_BACK,
                        default=self.config_entry.options.get(
                            CONF_DAYS_BACK, DEFAULT_DAYS_BACK
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=3, max=90, step=1, mode=NumberSelectorMode.BOX
                        )
                    )
                }
            ),
        )
