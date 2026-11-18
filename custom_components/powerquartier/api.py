"""Client for the Powerquartier (Exnaton) energy community portal.

Unofficial: the portal has no public API. Data is read from the same
server-rendered (Next.js RSC) responses the web app uses, so a portal
redesign can break this module. All parsing lives here so it can be fixed
in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://burgenland.powerquartier.at"
LOCALE = "de-AT"
PROFILE_COOKIE = "exnaton.selectedAccountId"
TIMEOUT = aiohttp.ClientTimeout(total=60)
RETRY_DELAYS_S = (2, 5, 15)  # transient errors: up to 3 retries per request

# Series aspects on the "Deine Daten" page and the statistic suffix used for them.
ASPECT_SUFFIX = {
    "communityConsumption": "gemeinschaft",
    "gridConsumption": "netz",
    "communityProduction": "einspeisung_gemeinschaft",
    "gridProduction": "einspeisung_netz",
}
ASPECT_LABEL = {
    "communityConsumption": "aus Gemeinschaft",
    "gridConsumption": "Reststrom (Netz)",
    "communityProduction": "Einspeisung an Gemeinschaft",
    "gridProduction": "Einspeisung ins Netz",
}

AVAILABILITY_MAP = {"community": "gemeinschaft", "default": "gemischt", "grid": "netz"}


class PowerquartierError(Exception):
    """Generic portal error."""


class PowerquartierAuthError(PowerquartierError):
    """Login failed or session rejected."""


class PowerquartierParseError(PowerquartierError):
    """The portal answered, but not in the expected format."""


class PowerquartierProfileRequired(PowerquartierError):
    """The portal redirected because no profile is selected yet."""


@dataclass
class Profile:
    """One portal profile (account), e.g. 'Profil-A'."""

    account_id: str
    name: str
    selected: bool = False
    community_id: str | None = None

    @property
    def slug(self) -> str:
        return profile_slug(self.name)


@dataclass
class DayEnergy:
    """15-minute energy values (kWh) for one local day, keyed by aspect."""

    start_utc: datetime
    series: dict[str, list[tuple[datetime, float, bool]]] = field(default_factory=dict)

    def total(self, aspect: str) -> float:
        return sum(v for _, v, _ in self.series.get(aspect, []))

    @property
    def is_empty(self) -> bool:
        return all(abs(self.total(a)) < 1e-9 for a in self.series) if self.series else True

    @property
    def preliminary(self) -> bool:
        return any(p for s in self.series.values() for _, _, p in s)


@dataclass
class PriceSpec:
    description: str
    applicable_to: str  # consumption | production
    price_class: str
    amount: float | None
    currency: str | None


@dataclass
class Tariff:
    name: str | None
    valid_begin: str | None
    specs: list[PriceSpec]

    def rate(self, applicable_to: str) -> float | None:
        for spec in self.specs:
            if spec.applicable_to == applicable_to and spec.amount is not None:
                return spec.amount
        return None


@dataclass
class AmpelSlot:
    start: datetime
    availability: str  # gemeinschaft | gemischt | netz
    forecast: bool
    preliminary: bool
    price: float | None  # EUR/kWh for community consumption
    consumption_kwh: float | None  # whole community, per 15 min
    production_kwh: float | None


def profile_slug(name: str) -> str:
    """'Prüfprofil-B' -> 'pruefprofil_b', 'Profil-C-consumer' -> 'profil_c'."""
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[-_ ]?(consumer|prosumer|producer)$", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "profil"


def _num(value: Any) -> float | None:
    """Decode a number from the RSC payload ('$-0', '$n123', 1.5, ...)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        v = value
        if v.startswith("$"):
            v = v[1:]
            if v.startswith("n"):
                v = v[1:]
            if v in ("undefined", "NaN", "Infinity", "-Infinity"):
                return None
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _scaled(value: Any) -> float | None:
    """Decode {'scale': 8, 'value': 10000000} -> 0.1."""
    if isinstance(value, dict) and "value" in value:
        v = _num(value.get("value"))
        scale = _num(value.get("scale")) or 0
        return None if v is None else v / (10**scale)
    return _num(value)


def rsc_chunks(text: str, marker: str) -> list[Any]:
    """Return every RSC line whose JSON payload contains `marker`, parsed."""
    out: list[Any] = []
    for line in text.split("\n"):
        if marker not in line:
            continue
        colon = line.find(":")
        if colon < 0:
            continue
        try:
            out.append(json.loads(line[colon + 1 :]))
        except ValueError:
            continue
    return out


def _props(chunk: Any) -> dict[str, Any] | None:
    """RSC element ['$', '$Lxx', key, props] -> props."""
    if isinstance(chunk, list) and len(chunk) >= 4 and isinstance(chunk[3], dict):
        return chunk[3]
    return None


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_profiles(text: str) -> list[Profile]:
    """Parse the profile list from a logged-in page.

    Two shapes exist: the user menu on normal pages
    ({"extendedAccounts": [... "isSelected": bool ...]}) and the
    "Select a profile" page ({"selectedAccountId": id, "accounts": [...]}).
    Only id, name and community are kept; the account objects also carry
    personal data (bank details, address) that is never stored or logged.
    """
    profiles: dict[str, Profile] = {}
    for marker in ('"extendedAccounts"', '"accounts"'):
        for chunk in rsc_chunks(text, marker):
            for d in _walk(chunk):
                if not isinstance(d, dict):
                    continue
                accounts = d.get("extendedAccounts")
                selected_id = None
                if not isinstance(accounts, list):
                    accounts = d.get("accounts")
                    selected_id = d.get("selectedAccountId")
                    if not isinstance(accounts, list):
                        continue
                for acc in accounts:
                    if not isinstance(acc, dict):
                        continue
                    acc_id, name = acc.get("id"), acc.get("name")
                    if not isinstance(acc_id, str) or not isinstance(name, str):
                        continue
                    community = acc.get("community")
                    if not (isinstance(community, str) and _UUID.fullmatch(community)):
                        community = None
                        for sub in _walk(acc):
                            c = sub.get("community")
                            if isinstance(c, str) and _UUID.fullmatch(c):
                                community = c
                                break
                    selected = (
                        bool(acc.get("isSelected"))
                        if "isSelected" in acc
                        else acc_id == selected_id
                    )
                    prev = profiles.get(acc_id)
                    profiles[acc_id] = Profile(
                        account_id=acc_id,
                        name=name,
                        selected=selected or bool(prev and prev.selected),
                        community_id=community or (prev.community_id if prev else None),
                    )
        if profiles:
            break
    if not profiles:
        raise PowerquartierParseError("No profiles found in page")
    return list(profiles.values())


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def parse_day(text: str, start_utc: datetime) -> DayEnergy:
    """Parse the 'Deine Daten' chart for one day (15-minute Wh values)."""
    day = DayEnergy(start_utc=start_utc)
    for chunk in rsc_chunks(text, '"dense"'):
        props = _props(chunk)
        if not props:
            continue
        dense = (props.get("charts") or {}).get("dense") or []
        for chart in dense:
            for s in chart.get("series") or []:
                aspect = s.get("aspect")
                if aspect not in ASPECT_SUFFIX:
                    continue
                if s.get("interval") not in (None, "15m"):
                    raise PowerquartierParseError(
                        f"Expected 15m interval, got {s.get('interval')}"
                    )
                pts: list[tuple[datetime, float, bool]] = []
                for d in s.get("rawData") or s.get("data") or []:
                    value = d.get("value") if isinstance(d, dict) else None
                    if not isinstance(value, list) or len(value) < 2:
                        continue
                    ts, wh = _num(value[0]), _num(value[1])
                    if ts is None:
                        continue
                    pts.append(
                        (
                            datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                            (wh or 0.0) / 1000.0,
                            bool((d.get("extras") or {}).get("dashed")),
                        )
                    )
                day.series[aspect] = pts
        break  # first chart block is the user's own data
    return day


def parse_tariff(text: str) -> Tariff | None:
    for chunk in rsc_chunks(text, '"ratePlan"'):
        for d in _walk(chunk):
            plan = d.get("ratePlan") if isinstance(d, dict) else None
            if not isinstance(plan, dict):
                continue
            specs = []
            for spec in plan.get("priceSpecs") or []:
                rate = ((spec.get("priceClassConfig") or {}).get("rate")) or {}
                specs.append(
                    PriceSpec(
                        description=str(spec.get("description")),
                        applicable_to=str(spec.get("applicableTo")),
                        price_class=str(spec.get("priceClass")),
                        amount=_num(rate.get("amount")),
                        currency=rate.get("currency"),
                    )
                )
            return Tariff(plan.get("name"), plan.get("validBegin"), specs)
    return None


def parse_ampel(text: str) -> list[AmpelSlot]:
    slots: list[AmpelSlot] = []
    for chunk in rsc_chunks(text, '"withForecast"'):
        props = _props(chunk)
        if not props or not isinstance(props.get("data"), list):
            continue
        for d in props["data"]:
            ts = d.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            cons, prod = _num(d.get("consumption")), _num(d.get("production"))
            slots.append(
                AmpelSlot(
                    start=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    availability=AVAILABILITY_MAP.get(
                        d.get("availability"), str(d.get("availability"))
                    ),
                    forecast=bool(d.get("forecast")),
                    preliminary=bool(d.get("preliminary")),
                    price=_scaled(d.get("communityConsumptionPrice")),
                    consumption_kwh=None if cons is None else cons / 1000.0,
                    production_kwh=None if prod is None else prod / 1000.0,
                )
            )
        break
    slots.sort(key=lambda s: s.start)
    return slots


def _iso_param(dt: datetime) -> str:
    """Portal expects JSON-quoted ISO strings with milliseconds and Z."""
    s = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )
    return quote(json.dumps(s))


class PowerquartierClient:
    """Session-holding client. One instance per config entry."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        jar: aiohttp.CookieJar,
        email: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        # `session` must use `jar` as its own cookie jar, so the portal session
        # and the selected-profile cookie never leak into HA's shared session.
        self._session = session
        self._jar = jar
        self._email = email
        self._password = password
        self._base = URL(base_url)
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._selected: Profile | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        return self._session

    # ---- auth -------------------------------------------------------------

    async def login(self) -> None:
        """Ory Kratos browser login flow (what the sign-in page does)."""
        sess = await self._sess()
        self._jar.clear()
        self._selected = None
        async with sess.get(
            self._base / "self-service/login/browser",
            headers={"Accept": "application/json"},
        ) as resp:
            if resp.status >= 400:
                raise PowerquartierError(f"Login flow init failed: HTTP {resp.status}")
            flow = await resp.json(content_type=None)
        ui = flow.get("ui") or {}
        action = ui.get("action")
        csrf = next(
            (
                n.get("attributes", {}).get("value")
                for n in ui.get("nodes") or []
                if n.get("attributes", {}).get("name") == "csrf_token"
            ),
            None,
        )
        if not action:
            raise PowerquartierParseError("Login flow has no action URL")
        # The password is only ever sent to the portal itself: refuse an
        # action URL on another host or without TLS.
        target = URL(action)
        if not target.is_absolute():
            target = self._base.join(target)
        if target.host != self._base.host or (
            self._base.scheme == "https" and target.scheme != "https"
        ):
            raise PowerquartierParseError("Login form points to an unexpected host")
        action = str(target)
        body = {
            "method": "password",
            "identifier": self._email,
            "password": self._password,
        }
        if csrf:
            body["csrf_token"] = csrf
        async with sess.post(
            action, json=body, headers={"Accept": "application/json"}
        ) as resp:
            status = resp.status
            data = await resp.json(content_type=None)
        if status in (400, 401, 403):
            msgs = [
                m.get("text")
                for m in ((data.get("ui") or {}).get("messages") or [])
                if isinstance(m, dict)
            ]
            raise PowerquartierAuthError("; ".join(filter(None, msgs)) or "Login rejected")
        if status not in (200, 422):  # 422 = browser redirect requested; session is set
            raise PowerquartierError(f"Login failed: HTTP {status}")
        if not await self._whoami():
            raise PowerquartierAuthError("Login did not produce a session")
        self._logged_in = True

    async def _whoami(self) -> bool:
        sess = await self._sess()
        async with sess.get(
            self._base / "sessions/whoami", headers={"Accept": "application/json"}
        ) as resp:
            if resp.status != 200:
                return False
            data = await resp.json(content_type=None)
            return bool(data.get("active"))

    async def _ensure_login(self) -> None:
        if not self._logged_in or not await self._whoami():
            await self.login()

    # ---- page fetch -------------------------------------------------------

    async def _get_with_retry(
        self, sess: aiohttp.ClientSession, path_query: str
    ) -> tuple[aiohttp.ClientResponse, str, str]:
        """GET with retries on timeouts, connection errors and HTTP 5xx."""
        delays = RETRY_DELAYS_S
        for attempt in range(len(delays) + 1):
            try:
                async with sess.get(
                    str(self._base) + path_query, headers={"RSC": "1"}
                ) as resp:
                    text = await resp.text()
                    if resp.status < 500 or attempt == len(delays):
                        return resp, resp.url.path, text
                    reason = f"HTTP {resp.status}"
            except (aiohttp.ClientError, TimeoutError) as err:
                if attempt == len(delays):
                    raise
                reason = type(err).__name__
            _LOGGER.debug(
                "Powerquartier %s: %s, retry %s in %ss",
                path_query.split("?", 1)[0], reason, attempt + 1, delays[attempt],
            )
            await asyncio.sleep(delays[attempt])
        raise PowerquartierError("unreachable")

    async def _rsc(self, path_query: str) -> str:
        """GET a page as RSC payload; re-login once if bounced to sign-in."""
        sess = await self._sess()
        for attempt in range(2):
            # Redirects are followed (the portal adds ?_rsc=... to RSC
            # requests); where we end up tells what happened.
            resp, final_path, text = await self._get_with_retry(sess, path_query)
            requested_path = path_query.split("?", 1)[0]
            # Only landing on the sign-in page means the session is gone; the
            # selection page just means no profile is chosen yet.
            bounced = "/auth/signin" in final_path or (
                "NEXT_REDIRECT" in text and "/auth/signin" in text
            )
            if (
                not bounced
                and "/auth/select" in final_path
                and "/auth/select" not in requested_path
            ):
                raise PowerquartierProfileRequired(final_path)
            if resp.status == 401 or bounced:
                if attempt == 0:
                    keep = self._selected
                    self._logged_in = False
                    await self.login()
                    if keep is not None:  # restore the profile for this request
                        self._jar.update_cookies(
                            {PROFILE_COOKIE: keep.account_id}, self._base
                        )
                        self._selected = keep
                    continue
                raise PowerquartierAuthError("Session rejected by portal")
            if resp.status >= 400:
                raise PowerquartierError(f"HTTP {resp.status} for {path_query}")
            return text
        raise PowerquartierError("unreachable")

    # ---- public API -------------------------------------------------------

    async def _list_profiles(self) -> list[Profile]:
        """Profiles from the account page, or from the selection page when
        the portal still wants a profile picked (fresh login)."""
        try:
            return parse_profiles(await self._rsc(f"/{LOCALE}/account"))
        except (PowerquartierParseError, PowerquartierProfileRequired):
            return parse_profiles(await self._rsc(f"/{LOCALE}/auth/select"))

    async def async_get_profiles(self) -> list[Profile]:
        async with self._lock:
            await self._ensure_login()
            return await self._list_profiles()

    async def _select(self, profile: Profile) -> None:
        """Switch profile via cookie, then verify the portal agrees."""
        self._jar.update_cookies({PROFILE_COOKIE: profile.account_id}, self._base)
        try:
            current = parse_profiles(await self._rsc(f"/{LOCALE}/account"))
        except (PowerquartierParseError, PowerquartierProfileRequired) as err:
            raise PowerquartierError(
                f"Profile switch to '{profile.name}' failed: portal still asks to select a profile"
            ) from err
        selected = [p for p in current if p.selected]
        if not selected or selected[0].account_id != profile.account_id:
            got = selected[0].name if selected else "none"
            raise PowerquartierError(
                f"Profile switch to '{profile.name}' failed (portal shows '{got}')"
            )
        self._selected = profile

    async def _ensure_selected(self) -> Profile:
        """Pages other than the selection page need some profile selected."""
        if self._selected is None:
            profiles = await self._list_profiles()
            await self._select(next((p for p in profiles if p.selected), profiles[0]))
        return self._selected

    async def async_get_days(
        self,
        profile: Profile,
        ranges_utc: list[tuple[datetime, datetime]],
        concurrency: int = 3,
    ) -> list[DayEnergy]:
        """Fetch 15-minute data for local days of one profile.

        ranges_utc: (local midnight, next local midnight) per day, as UTC.
        """
        async with self._lock:
            await self._ensure_login()
            await self._select(profile)
            sem = asyncio.Semaphore(concurrency)

            async def one(start: datetime, end: datetime) -> DayEnergy:
                stop = end - timedelta(milliseconds=1)
                q = (
                    f"/{LOCALE}/analyse/you?start={_iso_param(start)}"
                    f"&stop={_iso_param(stop)}&dpMode={quote(json.dumps('day'))}"
                )
                # A real day without data still comes with a chart (all zeros).
                # A page without any chart is a portal hiccup: retry, and never
                # import it as an empty day.
                async with sem:
                    for attempt in range(3):
                        text = await self._rsc(q)
                        if '"dense"' in text:
                            return parse_day(text, start)
                        await asyncio.sleep(RETRY_DELAYS_S[attempt])
                raise PowerquartierParseError(
                    f"No chart data for the day starting {start.isoformat()} after 3 tries"
                )

            return list(await asyncio.gather(*(one(a, b) for a, b in ranges_utc)))

    async def async_get_tariff_and_ampel(self) -> tuple[Tariff | None, list[AmpelSlot]]:
        async with self._lock:
            await self._ensure_login()
            await self._ensure_selected()
            account_page = await self._rsc(f"/{LOCALE}/account")
            tariff = parse_tariff(account_page)
            profiles = parse_profiles(account_page)
            community = next((p.community_id for p in profiles if p.community_id), None)
            q = f"/{LOCALE}" + (f"?communityId={community}" if community else "")
            ampel = parse_ampel(await self._rsc(q))
            return tariff, ampel
