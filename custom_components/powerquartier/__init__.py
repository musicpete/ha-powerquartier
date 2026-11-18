"""Powerquartier energy community portal (Fanclub Burgenland Energieunabhängig).

Unofficial integration: logs in to the portal with the user's credentials,
imports hourly community/grid energy per profile into long-term statistics
(powerquartier:<profil>_gemeinschaft / _netz) and exposes the community
"Stromampel" forecast and tariff as sensors.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AmpelSlot,
    PowerquartierAuthError,
    PowerquartierClient,
    PowerquartierError,
    Profile,
    Tariff,
)
from .const import (
    AMPEL_INTERVAL_MIN,
    ATTR_DAYS,
    ATTR_PROFILE,
    CONF_DAYS_BACK,
    CONF_PROFILES,
    DEFAULT_DAYS_BACK,
    DOMAIN,
    IMPORT_TIMES,
    RETRY_AFTER_FAILURE_MIN,
    RETRY_MAX_PER_CYCLE,
    SERVICE_REFRESH,
    SIGNAL_IMPORT_UPDATED,
    STARTUP_DELAY_S,
)
from .importer import EnergyImporter

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class AmpelData:
    tariff: Tariff | None
    slots: list[AmpelSlot]


@dataclass
class PowerquartierRuntime:
    client: PowerquartierClient
    importer: EnergyImporter
    coordinator: DataUpdateCoordinator[AmpelData]


type PowerquartierConfigEntry = ConfigEntry[PowerquartierRuntime]


def account_key(email: str) -> str:
    """Config entry unique id: a hash, so the email address is not used as an id."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 -> 1.2: replace the email-based unique id and title."""
    if entry.version == 1 and entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry,
            unique_id=account_key(entry.data[CONF_EMAIL]),
            title="Powerquartier" if "@" in entry.title else entry.title,
            minor_version=2,
        )
    return True


def make_client(hass: HomeAssistant, email: str, password: str) -> PowerquartierClient:
    jar = aiohttp.CookieJar()
    session = async_create_clientsession(
        hass, cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=60)
    )
    return PowerquartierClient(session, jar, email, password)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def handle_refresh(call: ServiceCall) -> None:
        days = call.data[ATTR_DAYS]
        slug = call.data.get(ATTR_PROFILE)
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("Powerquartier is not set up")
        failed: list[str] = []
        for entry in entries:
            try:
                failed += await entry.runtime_data.importer.async_import(days, slug)
            except PowerquartierAuthError as err:
                entry.async_start_reauth(hass)
                raise HomeAssistantError(f"Powerquartier login failed: {err}") from err
        if failed:
            raise HomeAssistantError(
                "Powerquartier import failed for: " + ", ".join(failed)
                + " (details in the profile's 'letzter Datentag' sensor)"
            )

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_REFRESH,
        handle_refresh,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_DAYS, default=DEFAULT_DAYS_BACK): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=1000)
                ),
                vol.Optional(ATTR_PROFILE): cv.string,
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PowerquartierConfigEntry) -> bool:
    client = make_client(hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    profiles = [
        Profile(account_id=p["account_id"], name=p["name"])
        for p in entry.data[CONF_PROFILES]
    ]
    store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}_gaps")
    importer = EnergyImporter(hass, entry.entry_id, client, profiles, store)
    await importer.async_load()

    async def _update() -> AmpelData:
        try:
            tariff, slots = await client.async_get_tariff_and_ampel()
        except PowerquartierAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (PowerquartierError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err
        return AmpelData(tariff, slots)

    coordinator: DataUpdateCoordinator[AmpelData] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{DOMAIN}_ampel",
        update_method=_update,
        update_interval=timedelta(minutes=AMPEL_INTERVAL_MIN),
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = PowerquartierRuntime(client, importer, coordinator)

    days_back = entry.options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)

    retry = {"count": 0, "cancel": None}

    @callback
    def _schedule_import(*_args) -> None:
        entry.async_create_background_task(hass, _run_import(), f"{DOMAIN}_import")

    def _set_retry_at(when) -> None:
        for st in importer.status.values():
            st.retry_at = when
        async_dispatcher_send(hass, SIGNAL_IMPORT_UPDATED, entry.entry_id)

    async def _run_import(*_args) -> None:
        if retry["cancel"]:
            retry["cancel"]()
            retry["cancel"] = None
        try:
            failed = await importer.async_import(days_back)
        except PowerquartierAuthError:
            entry.async_start_reauth(hass)
            return
        except Exception:  # noqa: BLE001 - never let the scheduler die
            _LOGGER.exception("Powerquartier scheduled import failed")
            failed = ["*"]
        if failed and retry["count"] < RETRY_MAX_PER_CYCLE:
            # Portal hiccup: try again soon instead of waiting for the next slot.
            retry["count"] += 1
            delay = RETRY_AFTER_FAILURE_MIN * 60
            retry["cancel"] = async_call_later(hass, delay, _schedule_import)
            _set_retry_at(dt_util.utcnow() + timedelta(seconds=delay))
            _LOGGER.info(
                "Powerquartier import failed for %s, retry %s/%s in %s min",
                ", ".join(failed), retry["count"], RETRY_MAX_PER_CYCLE, RETRY_AFTER_FAILURE_MIN,
            )
        else:
            retry["count"] = 0
            _set_retry_at(None)

    @callback
    def _cancel_retry() -> None:
        if retry["cancel"]:
            retry["cancel"]()

    entry.async_on_unload(_cancel_retry)

    for hour, minute in IMPORT_TIMES:
        entry.async_on_unload(
            async_track_time_change(hass, _schedule_import, hour=hour, minute=minute, second=0)
        )
    entry.async_on_unload(async_call_later(hass, STARTUP_DELAY_S, _schedule_import))
    entry.async_on_unload(entry.add_update_listener(_reload_on_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload_on_update(hass: HomeAssistant, entry: PowerquartierConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PowerquartierConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
