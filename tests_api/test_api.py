"""Client tests against the fake portal (no Home Assistant needed)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.powerquartier.api import (
    PowerquartierAuthError,
    PowerquartierClient,
    PowerquartierError,
    Profile,
    profile_slug,
)

from .fake_portal import ACCOUNTS, EMAIL, PASSWORD, FakePortal

VIENNA = ZoneInfo("Europe/Vienna")


@pytest.fixture
async def portal():
    fp = FakePortal()
    server = TestServer(fp.app)
    await server.start_server()
    fp.base = str(server.make_url("")).rstrip("/")
    yield fp
    await server.close()


async def _client(portal, password=PASSWORD):
    # Test server runs on 127.0.0.1: aiohttp only stores IP cookies with unsafe=True.
    jar = aiohttp.CookieJar(unsafe=True)
    session = aiohttp.ClientSession(cookie_jar=jar)
    return PowerquartierClient(session, jar, EMAIL, password, base_url=portal.base), session


def _ranges(first: str, n: int):
    d0 = datetime.fromisoformat(first).date()
    out = []
    for i in range(n):
        d = d0 + timedelta(days=i)
        a = datetime(d.year, d.month, d.day, tzinfo=VIENNA).astimezone(timezone.utc)
        e = d + timedelta(days=1)
        b = datetime(e.year, e.month, e.day, tzinfo=VIENNA).astimezone(timezone.utc)
        out.append((a, b))
    return out


def test_slug():
    assert profile_slug("Profil-A") == "profil_a"
    assert profile_slug("Prüfprofil-B") == "pruefprofil_b"
    assert profile_slug("Profil-C-consumer") == "profil_c"


async def test_login_and_profiles(portal):
    client, session = await _client(portal)
    try:
        profiles = await client.async_get_profiles()
        assert [p.name for p in profiles] == [a["name"] for a in ACCOUNTS]
        assert all(p.community_id for p in profiles)
        assert portal.rsc_redirects > 0  # the ?_rsc redirect was followed
    finally:
        await session.close()


async def test_wrong_password(portal):
    client, session = await _client(portal, password="wrong-test-password")  # pragma: allowlist secret
    try:
        with pytest.raises(PowerquartierAuthError, match="invalid"):
            await client.async_get_profiles()
    finally:
        await session.close()


async def test_days_per_profile_and_dst(portal):
    client, session = await _client(portal)
    try:
        profiles = {p.name: p for p in await client.async_get_profiles()}
        # 2026-03-29 has 23 h (DST start) in Vienna
        days = await client.async_get_days(profiles["Profil-A"], _ranges("2026-03-28", 3))
        assert [len(d.series["communityConsumption"]) for d in days] == [96, 92, 96]
        assert round(days[0].total("communityConsumption"), 3) == 9.6  # 96 * 100 Wh
        assert round(days[0].total("gridConsumption"), 3) == 4.8
        assert days[0].preliminary
        days = await client.async_get_days(profiles["Prüfprofil-B"], _ranges("2026-03-28", 1))
        assert round(days[0].total("communityConsumption"), 3) == 1.92
        # every request was made with the right profile cookie
        names = {a["id"]: a["name"] for a in ACCOUNTS}
        assert [names[s] for s, _, _ in portal.day_requests] == ["Profil-A"] * 3 + ["Prüfprofil-B"]
    finally:
        await session.close()


async def test_profile_switch_verification(portal):
    client, session = await _client(portal)
    try:
        await client.async_get_profiles()
        ghost = Profile(account_id="ffffffff-0000-0000-0000-000000000000", name="Ghost")
        with pytest.raises(PowerquartierError, match="Profile switch"):
            await client.async_get_days(ghost, _ranges("2026-11-01", 1))
        assert portal.day_requests == []  # never fetched data under a wrong profile
    finally:
        await session.close()


async def test_relogin_after_session_expiry(portal):
    client, session = await _client(portal)
    try:
        profiles = {p.name: p for p in await client.async_get_profiles()}
        portal.expire_next = True
        days = await client.async_get_days(profiles["Profil-A"], _ranges("2026-11-01", 1))
        assert round(days[0].total("communityConsumption"), 3) == 9.6
        assert len(portal.sessions) == 1  # logged in again
    finally:
        await session.close()


async def test_tariff_and_ampel(portal):
    client, session = await _client(portal)
    try:
        tariff, slots = await client.async_get_tariff_and_ampel()
        assert tariff.name == "Beispieltarif"
        assert tariff.rate("consumption") == 0.12
        assert tariff.rate("production") == 0.08
        assert len(slots) == 208
        assert {s.availability for s in slots} == {"gemeinschaft", "gemischt", "netz"}
        assert slots[0].price == pytest.approx(0.1)
        assert slots[0].production_kwh == 0.0  # "$-0"
    finally:
        await session.close()


async def test_chartless_page_is_retried_not_imported_as_zero(portal, monkeypatch):
    import custom_components.powerquartier.api as api_mod

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(api_mod.asyncio, "sleep", no_sleep)
    client, session = await _client(portal)
    try:
        prof = {p.name: p for p in await client.async_get_profiles()}["Profil-A"]
        ranges = _ranges("2025-10-26", 2)  # 25-hour day + the day after
        portal.flaky[ranges[1][0].date().isoformat()] = 2  # fails twice, then works
        days = await client.async_get_days(prof, ranges)
        assert [len(d.series["communityConsumption"]) for d in days] == [100, 96]
        assert round(days[1].total("communityConsumption"), 3) == 9.6
        portal.flaky[ranges[1][0].date().isoformat()] = 5  # never recovers
        with pytest.raises(PowerquartierError, match="No chart data"):
            await client.async_get_days(prof, ranges)
    finally:
        await session.close()


async def test_transient_5xx_is_retried(portal, monkeypatch):
    import custom_components.powerquartier.api as api_mod

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(api_mod.asyncio, "sleep", no_sleep)
    client, session = await _client(portal)
    try:
        prof = {p.name: p for p in await client.async_get_profiles()}["Profil-A"]
        portal.fail_5xx = 3  # three 503s, the 4th try works
        days = await client.async_get_days(prof, _ranges("2026-11-01", 1))
        assert round(days[0].total("communityConsumption"), 3) == 9.6
        portal.fail_5xx = 10  # persistent outage -> error, not silent zero
        with pytest.raises(PowerquartierError):
            await client.async_get_days(prof, _ranges("2026-11-02", 1))
    finally:
        await session.close()


def test_translations_have_no_tags():
    """HA's frontend parses '<x>' in translations as markup (UNCLOSED_TAG)."""
    import json
    import pathlib

    base = pathlib.Path(__file__).parents[1] / "custom_components" / "powerquartier"
    files = [base / "strings.json", *sorted((base / "translations").glob("*.json"))]
    for f in files:
        text = f.read_text(encoding="utf-8")
        json.loads(text)
        assert "<" not in text and ">" not in text.replace("->", "").replace("→", ""), f


async def test_password_only_sent_to_portal_host(portal):
    """A login form pointing elsewhere must not receive the credentials."""
    portal.action_override = "https://attacker.example/self-service/login?flow=flow1"
    client, session = await _client(portal)
    async with session:
        with pytest.raises(PowerquartierError, match="unexpected host"):
            await client.login()
