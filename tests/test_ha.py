"""Home Assistant level tests: config flow, statistics import, sensors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.powerquartier.api import (
    AmpelSlot,
    DayEnergy,
    PowerquartierAuthError,
    PriceSpec,
    Profile,
    Tariff,
)
from custom_components.powerquartier.const import CONF_PROFILES, DOMAIN
from custom_components.powerquartier.importer import EnergyImporter, _metadata

PROFILES = [
    Profile("id-c", "Profil-C-consumer"),
    Profile("id-b", "Prüfprofil-B"),
    Profile("id-a", "Profil-A"),
]


class FakeClient:
    def __init__(self, empty_days=(), password_ok=True, fail_times=0):
        self.empty_days = set(empty_days)
        self.password_ok = password_ok
        self.fail_times = fail_times
        self.calls = []
        self.requested_days = []

    async def async_get_profiles(self):
        if not self.password_ok:
            raise PowerquartierAuthError("bad")
        return [Profile(p.account_id, p.name, selected=i == 0) for i, p in enumerate(PROFILES)]

    async def async_get_days(self, profile, ranges):
        self.calls.append(profile.name)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("portal hiccup")
        self.requested_days += [a for a, _ in ranges]
        out = []
        for start, end in ranges:
            e = DayEnergy(start_utc=start)
            empty = start.astimezone(timezone(timedelta(hours=1))).date().isoformat() in self.empty_days
            for aspect, wh in (("communityConsumption", 100.0), ("gridConsumption", 50.0)):
                pts, t = [], start
                while t < end:
                    pts.append((t, 0.0 if empty else wh / 1000, True))
                    t += timedelta(minutes=15)
                e.series[aspect] = pts
            out.append(e)
        return out

    async def async_get_tariff_and_ampel(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        now -= timedelta(minutes=now.minute % 15)
        slots = [
            AmpelSlot(now + timedelta(minutes=15 * i),
                      "gemeinschaft" if 0 <= i < 4 else "netz", i >= 0, False, 0.1, 1.0, 1.0)
            for i in range(-4, 192)
        ]
        tariff = Tariff("Beispieltarif", "2025-01-01",
                        [PriceSpec("Bezugstarif", "consumption", "fixedRate", 0.12, "EUR"),
                         PriceSpec("Einspeisetarif", "production", "fixedRate", 0.08, "EUR")])
        return tariff, slots


async def _rows(hass, stat_id, start, end, period="hour"):
    from homeassistant.components.recorder import get_instance

    res = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {stat_id}, period, None, {"sum", "change"}
    )
    return res.get(stat_id, [])


@pytest.fixture
async def vienna(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/Vienna")


async def test_import_continues_daily_history(recorder_mock, enable_custom_integrations, hass, vienna, freezer):
    freezer.move_to("2026-11-18 10:00:00+01:00")
    profile = PROFILES[2]
    stat_id = "powerquartier:profil_a_gemeinschaft"
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Vienna")
    # Existing data as imported on 18 Nov: one daily row per local midnight, 10 kWh/day
    base, rows = 1000.0, []
    d = datetime(2026, 10, 1, tzinfo=tz)
    while d <= datetime(2026, 11, 16, tzinfo=tz):
        base += 10
        rows.append({"start": d.astimezone(timezone.utc), "sum": base})
        d = (d + timedelta(days=1, hours=2)).replace(hour=0)
    async_add_external_statistics(hass, _metadata(profile, "communityConsumption"), rows)
    await async_wait_recording_done(hass)

    client = FakeClient(empty_days={"2026-11-17"})
    importer = EnergyImporter(hass, "e1", client, [profile])
    await importer.async_import(days_back=5)  # 13..17 Nov
    await async_wait_recording_done(hass)

    win = datetime(2026, 11, 13, tzinfo=tz).astimezone(timezone.utc)
    got = await _rows(hass, stat_id, win - timedelta(hours=1), win + timedelta(days=6))
    by_start = {datetime.fromtimestamp(r["start"], timezone.utc): r["sum"] for r in got}
    # base = sum through 12 Nov = 1000 + 43*10
    assert by_start[win] == pytest.approx(1430.4)  # old daily row overwritten
    end16 = datetime(2026, 11, 16, 23, tzinfo=tz).astimezone(timezone.utc)
    assert by_start[end16] == pytest.approx(1430 + 4 * 9.6)
    mid17 = datetime(2026, 11, 17, tzinfo=tz).astimezone(timezone.utc)
    assert by_start[mid17] == pytest.approx(1430 + 4 * 9.6)  # empty day: carry-over
    assert len([s for s in by_start if win <= s < mid17]) == 96  # hourly now

    days = await _rows(hass, stat_id, win, mid17, period="day")
    assert [round(r["change"], 3) for r in days] == [9.6] * 4 + [0.0]

    st = importer.status["profil_a"]
    assert st.last_day.isoformat() == "2026-11-16"
    assert st.last_day_kwh == {"gemeinschaft": 9.6, "netz": 4.8}
    assert st.last_error is None

    # Second run over the same window is idempotent
    await importer.async_import(days_back=5)
    await async_wait_recording_done(hass)
    got2 = await _rows(hass, stat_id, win - timedelta(hours=1), win + timedelta(days=6))
    assert [r["sum"] for r in got2] == [r["sum"] for r in got]


async def test_config_flow(recorder_mock, enable_custom_integrations, hass):
    with patch("custom_components.powerquartier.config_flow.make_client", return_value=FakeClient(password_ok=False)):
        res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "x"})
        assert res["errors"] == {"base": "invalid_auth"}
    with patch("custom_components.powerquartier.config_flow.make_client", return_value=FakeClient()), \
         patch("custom_components.powerquartier.make_client", return_value=FakeClient()):
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "y"})
        assert res["step_id"] == "profiles"
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {CONF_PROFILES: ["id-a", "id-b"]})
        assert res["type"] is FlowResultType.CREATE_ENTRY
        assert [p["name"] for p in res["data"][CONF_PROFILES]] == ["Prüfprofil-B", "Profil-A"]
        await hass.async_block_till_done()


async def test_sensors_and_service(recorder_mock, enable_custom_integrations, hass, vienna):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "y",
              CONF_PROFILES: [{"account_id": "id-a", "name": "Profil-A"}]},
    )
    entry.add_to_hass(hass)
    fake = FakeClient()
    with patch("custom_components.powerquartier.make_client", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    assert sorted(states) == [
        "sensor.powerquartier_bezugstarif", "sensor.powerquartier_einspeisetarif",
        "sensor.powerquartier_gemeinschaftspreis", "sensor.powerquartier_naechstes_gemeinschaftsfenster",
        "sensor.powerquartier_profil_a_letzter_datentag", "sensor.powerquartier_stromampel"]
    ampel = next(s for e, s in states.items() if "stromampel" in e)
    assert ampel.state == "gemeinschaft"
    # whole upcoming horizon (2 days in the fake), share still over 24 h
    assert len(ampel.attributes["forecast"]) == 192
    assert ampel.attributes["anteil_gemeinschaft_naechste_24h_pct"] == 4
    assert ampel.attributes["forecast"][0]["erzeugung_kwh"] == 1.0
    assert ampel.attributes["prognose_bis"]
    assert ampel.attributes["forecast"][0]["vorlaeufig"] is False
    tarif = next(s for e, s in states.items() if "bezugstarif" in e)
    assert float(tarif.state) == 0.12
    nxt = next(s for e, s in states.items() if "gemeinschaftsfenster" in e)
    assert nxt.state not in ("unknown", "unavailable")

    await hass.services.async_call(DOMAIN, "refresh", {"days": 3}, blocking=True)
    await hass.async_block_till_done()
    assert fake.calls == ["Profil-A"]
    status = hass.states.get("sensor.powerquartier_profil_a_letzter_datentag")
    assert status.attributes["kwh_gemeinschaft"] == 9.6
    assert status.attributes["fehler"] is None


async def test_gap_is_remembered_and_refilled(recorder_mock, enable_custom_integrations, hass, vienna, freezer):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Vienna")
    freezer.move_to("2026-11-18 10:00:00+01:00")
    profile = PROFILES[2]
    stat_id = "powerquartier:profil_a_gemeinschaft"
    client = FakeClient(empty_days={"2026-11-10"})  # portal has no data that day (yet)
    importer = EnergyImporter(hass, "e1", client, [profile])
    await importer.async_import(days_back=10)  # 8..17 Nov, 17 Nov = not delivered yet
    await async_wait_recording_done(hass)
    st = importer.status["profil_a"]
    assert [d.isoformat() for d in st.gaps] == ["2026-11-10"]  # 17 Nov is too recent to be a gap

    # Two weeks later the gap is outside the 10-day window, and the portal now has it.
    freezer.move_to("2026-12-02 10:00:00+01:00")
    client.empty_days.clear()
    client.requested_days.clear()
    await importer.async_import(days_back=10)
    await async_wait_recording_done(hass)
    assert st.gaps == []
    first_requested = min(client.requested_days).astimezone(tz).date().isoformat()
    assert first_requested == "2026-11-10"  # re-import restarted at the gap

    win = datetime(2026, 11, 8, tzinfo=tz).astimezone(timezone.utc)
    days = await _rows(hass, stat_id, win, datetime(2026, 12, 2, tzinfo=tz).astimezone(timezone.utc), "day")
    assert [round(r["change"], 3) for r in days] == [9.6] * 24  # 8 Nov .. 1 Dec, no hole
    assert days[-1]["sum"] == pytest.approx(9.6 * 24)


async def test_failed_run_is_retried(recorder_mock, enable_custom_integrations, hass, vienna, freezer):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from homeassistant.util import dt as dt_util

    await async_wait_recording_done(hass)
    freezer.move_to("2026-11-18 10:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "y",
              CONF_PROFILES: [{"account_id": "id-a", "name": "Profil-A"}]},
        options={"days_back": 3},
    )
    entry.add_to_hass(hass)
    fake = FakeClient(fail_times=1)
    with patch("custom_components.powerquartier.make_client", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=130))  # startup import
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        state = hass.states.get("sensor.powerquartier_profil_a_letzter_datentag")
        assert "portal hiccup" in state.attributes["fehler"]
        assert state.attributes["naechster_versuch"] is not None
        freezer.tick(timedelta(minutes=31))  # retry fires
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
        state = hass.states.get("sensor.powerquartier_profil_a_letzter_datentag")
        assert state.attributes["fehler"] is None
        assert state.attributes["naechster_versuch"] is None
        assert state.state == "2026-11-17"
        assert fake.calls == ["Profil-A", "Profil-A"]
        await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_replaces_email_based_id(recorder_mock, enable_custom_integrations, hass):
    from custom_components.powerquartier import account_key

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        unique_id="user@example.com",
        title="Powerquartier (user@example.com)",
        data={CONF_EMAIL: "User@Example.com", CONF_PASSWORD: "test-password",  # pragma: allowlist secret
              CONF_PROFILES: [{"account_id": "id-a", "name": "Profil-A"}]},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.powerquartier.make_client", side_effect=Exception("no network")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.unique_id == account_key("user@example.com")
    assert "@" not in entry.unique_id and entry.title == "Powerquartier"
    assert entry.minor_version == 2


async def test_refresh_action_is_admin_only(recorder_mock, enable_custom_integrations, hass, hass_read_only_user):
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "refresh", {}, blocking=True, context=Context(user_id=hass_read_only_user.id)
        )


async def test_totals_come_from_whole_history(recorder_mock, enable_custom_integrations, hass, vienna, freezer):
    """Yearly/monthly totals cover everything stored, not just the import window."""
    freezer.move_to("2026-11-18 10:00:00+01:00")
    profile = PROFILES[2]
    client = FakeClient()
    importer = EnergyImporter(hass, "e1", client, [profile])
    await importer.async_import(days_back=30)  # 19 Oct .. 17 Nov
    await async_wait_recording_done(hass)
    await importer.async_import(days_back=3)  # a short run must not shrink the totals
    await async_wait_recording_done(hass)
    importer.async_shutdown()
    await importer.async_refresh_totals()
    st = importer.status["profil_a"]
    # 19-31 Oct = 13 days, one of them 25 h long (end of DST on 25/26 Oct).
    assert st.kwh_per_month["2026-10"] == {"gemeinschaft": pytest.approx(12 * 9.6 + 10.0), "netz": pytest.approx(12 * 4.8 + 5.0)}
    assert st.kwh_per_month["2026-11"]["gemeinschaft"] == pytest.approx(9.6 * 17)
    assert st.kwh_per_year["2026"]["gemeinschaft"] == pytest.approx(12 * 9.6 + 10.0 + 9.6 * 17)


async def test_last_day_survives_restart(hass):
    """The last imported day is restored from storage, not unknown after a restart."""

    class FakeStore:
        def __init__(self, data):
            self.data = data

        async def async_load(self):
            return self.data

        def async_delay_save(self, func, _delay):
            self.data = func()

    profile = Profile("id-a", "Profil-A")
    slug = profile.slug
    store = FakeStore({"gaps": {}, "last": {slug: {"day": "2000-01-31", "kwh": {"netz": 1.5}, "preliminary_from": None}}})
    importer = EnergyImporter(hass, "e1", None, [profile], store)
    await importer.async_load()
    st = importer.status[slug]
    assert st.last_day.isoformat() == "2000-01-31"
    assert st.last_day_kwh == {"netz": 1.5}
    importer._save()
    assert store.data["last"][slug]["day"] == "2000-01-31"
