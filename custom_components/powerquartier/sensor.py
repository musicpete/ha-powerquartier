"""Sensors: Stromampel, community price, tariff, per-profile import status."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AmpelData, PowerquartierConfigEntry
from .api import AmpelSlot, Profile
from .const import DOMAIN, SIGNAL_IMPORT_UPDATED

AMPEL_STATES = ["gemeinschaft", "gemischt", "netz"]
SLOT = timedelta(minutes=15)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerquartierConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Powerquartier",
        manufacturer="Exnaton / Burgenland Energie",
        model="Energiegemeinschaft-Portal",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://burgenland.powerquartier.at",
    )
    entities: list[SensorEntity] = [
        AmpelSensor(rt.coordinator, entry, device),
        NextCommunitySensor(rt.coordinator, entry, device),
        CommunityPriceSensor(rt.coordinator, entry, device),
        TariffSensor(rt.coordinator, entry, device, "consumption", "bezugstarif"),
        TariffSensor(rt.coordinator, entry, device, "production", "einspeisetarif"),
    ]
    entities += [ProfileStatusSensor(entry, device, p) for p in rt.importer.profiles]
    async_add_entities(entities)


def _current(slots: list[AmpelSlot], now: datetime) -> AmpelSlot | None:
    for s in slots:
        if s.start <= now < s.start + SLOT:
            return s
    return None


class _Base(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, device, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device
        # Stable entity IDs independent of the HA system language
        self.entity_id = f"sensor.{DOMAIN}_{key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _tick(_now) -> None:
            self.async_write_ha_state()

        # Slots are 15 minutes; recompute the current slot on each boundary.
        self.async_on_remove(
            async_track_time_change(self.hass, _tick, minute=[0, 15, 30, 45], second=5)
        )

    @property
    def data(self) -> AmpelData:
        return self.coordinator.data


class AmpelSensor(_Base):
    """Current community availability: gemeinschaft / gemischt / netz."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = AMPEL_STATES
    _attr_icon = "mdi:traffic-light"
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, coordinator, entry, device) -> None:
        super().__init__(coordinator, entry, device, "stromampel")

    @property
    def native_value(self) -> str | None:
        slot = _current(self.data.slots, dt_util.utcnow())
        return slot.availability if slot and slot.availability in AMPEL_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        upcoming = [s for s in self.data.slots if s.start + SLOT > now]
        next_24h = upcoming[:96]
        share = (
            round(100 * sum(s.availability == "gemeinschaft" for s in next_24h) / len(next_24h))
            if next_24h
            else None
        )
        return {
            "anteil_gemeinschaft_naechste_24h_pct": share,
            "prognose_bis": (
                dt_util.as_local(upcoming[-1].start + SLOT).isoformat() if upcoming else None
            ),
            "forecast": [
                {
                    "start": dt_util.as_local(s.start).isoformat(),
                    "status": s.availability,
                    "preis": s.price,
                    "prognose": s.forecast,
                    "erzeugung_kwh": s.production_kwh,
                    "verbrauch_kwh": s.consumption_kwh,
                }
                for s in upcoming
            ],
        }


class NextCommunitySensor(_Base):
    """Start of the next 15-min slot with community power (timestamp)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator, entry, device) -> None:
        super().__init__(coordinator, entry, device, "naechstes_gemeinschaftsfenster")

    @property
    def native_value(self) -> datetime | None:
        now = dt_util.utcnow()
        for s in self.data.slots:
            if s.start + SLOT > now and s.availability == "gemeinschaft":
                return max(s.start, now.replace(second=0, microsecond=0))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        start = self.native_value
        if start is None:
            return {}
        end = None
        started = False
        for s in self.data.slots:
            if s.start + SLOT <= now:
                continue
            if s.availability == "gemeinschaft":
                started = True
            elif started:
                end = s.start
                break
        return {"ende": dt_util.as_local(end).isoformat() if end else None}


class CommunityPriceSensor(_Base):
    """Price of community power in the current slot (EUR/kWh)."""

    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_icon = "mdi:cash"
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator, entry, device) -> None:
        super().__init__(coordinator, entry, device, "gemeinschaftspreis")

    @property
    def native_value(self) -> float | None:
        slot = _current(self.data.slots, dt_util.utcnow())
        if slot and slot.price is not None:
            return slot.price
        tariff = self.data.tariff
        return tariff.rate("consumption") if tariff else None


class TariffSensor(_Base):
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_icon = "mdi:file-document-outline"
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator, entry, device, applicable_to: str, key: str) -> None:
        super().__init__(coordinator, entry, device, key)
        self._applicable_to = applicable_to

    @property
    def native_value(self) -> float | None:
        t = self.data.tariff
        return t.rate(self._applicable_to) if t else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self.data.tariff
        if not t:
            return {}
        spec = next((s for s in t.specs if s.applicable_to == self._applicable_to), None)
        return {
            "tarif": t.name,
            "gueltig_ab": t.valid_begin,
            "beschreibung": spec.description if spec else None,
            "preisklasse": spec.price_class if spec else None,
            "hinweis": "Nettopreis laut Portal",
        }


class ProfileStatusSensor(SensorEntity):
    """Last imported day per profile, with that day's kWh and import status."""

    _unrecorded_attributes = frozenset({"kwh_pro_monat", "kwh_pro_jahr"})
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:database-clock"
    _attr_should_poll = False

    def __init__(self, entry: PowerquartierConfigEntry, device: DeviceInfo, profile: Profile) -> None:
        self._entry = entry
        self._profile = profile
        self._attr_unique_id = f"{entry.entry_id}_{profile.slug}_letzter_datentag"
        self._attr_translation_key = "letzter_datentag"
        self._attr_translation_placeholders = {"profile": profile.name}
        self._attr_device_info = device
        self.entity_id = f"sensor.{DOMAIN}_{profile.slug}_letzter_datentag"

    async def async_added_to_hass(self) -> None:
        @callback
        def _updated(entry_id: str) -> None:
            if entry_id == self._entry.entry_id:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_IMPORT_UPDATED, _updated)
        )

    @property
    def _status(self):
        return self._entry.runtime_data.importer.status[self._profile.slug]

    @property
    def native_value(self):
        return self._status.last_day

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._status
        kwh = st.last_day_kwh
        community, grid = kwh.get("gemeinschaft"), kwh.get("netz")
        share = (
            round(100 * community / (community + grid), 1)
            if community is not None and grid is not None and (community + grid) > 0
            else None
        )
        return {
            "kwh_gemeinschaft": community,
            "kwh_netz": grid,
            "anteil_gemeinschaft_pct": share,
            "vorlaeufig_ab": st.preliminary_from.isoformat() if st.preliminary_from else None,
            "letzter_lauf": st.last_run.isoformat() if st.last_run else None,
            "geschriebene_stunden": st.hours_written,
            "fehler": st.last_error,
            "kwh_pro_jahr": st.kwh_per_year,
            "kwh_pro_monat": st.kwh_per_month,
            "fehlende_tage": [d.isoformat() for d in st.gaps],
            "naechster_versuch": st.retry_at.isoformat() if st.retry_at else None,
            "statistik_ids": [
                f"{DOMAIN}:{self._profile.slug}_gemeinschaft",
                f"{DOMAIN}:{self._profile.slug}_netz",
            ],
        }
