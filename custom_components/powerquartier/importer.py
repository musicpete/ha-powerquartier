"""Import portal energy data into Home Assistant long-term statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import (
    ASPECT_LABEL,
    ASPECT_SUFFIX,
    DayEnergy,
    PowerquartierAuthError,
    PowerquartierClient,
    Profile,
)
from .const import DOMAIN, SIGNAL_IMPORT_UPDATED

try:  # HA 2025.10+
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover - older HA
    StatisticMeanType = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)


@dataclass
class ProfileStatus:
    """What the last import achieved for one profile (shown on sensors)."""

    last_day: date | None = None
    last_day_kwh: dict[str, float] = field(default_factory=dict)
    preliminary_from: date | None = None
    last_run: datetime | None = None
    last_error: str | None = None
    hours_written: int = 0
    gaps: list[date] = field(default_factory=list)
    retry_at: datetime | None = None


def statistic_id(slug: str, aspect: str) -> str:
    return f"{DOMAIN}:{slug}_{ASPECT_SUFFIX[aspect]}"


def _metadata(profile: Profile, aspect: str) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "source": DOMAIN,
        "statistic_id": statistic_id(profile.slug, aspect),
        "name": f"Powerquartier {profile.name} · {ASPECT_LABEL[aspect]}",
        "unit_of_measurement": "kWh",
        "has_sum": True,
        "unit_class": "energy",
    }
    if StatisticMeanType is not None:
        meta["mean_type"] = StatisticMeanType.NONE
    else:  # pragma: no cover
        meta["has_mean"] = False
    return meta


def local_day_ranges(first: date, last: date) -> list[tuple[date, datetime, datetime]]:
    """(day, local midnight as UTC, next local midnight as UTC) - DST-safe."""
    tz = dt_util.get_default_time_zone()
    out = []
    d = first
    while d <= last:
        start = datetime.combine(d, time(0), tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz).astimezone(
            timezone.utc
        )
        out.append((d, start, end))
        d += timedelta(days=1)
    return out


def build_hourly_rows(
    days: list[tuple[date, datetime, DayEnergy]],
    aspect: str,
    base_sum: float,
) -> list[dict[str, Any]]:
    """Cumulative hourly rows for one aspect.

    Empty days (missing or not yet delivered) get a single carry-over row at
    their local midnight, which also overwrites any older daily row there.
    """
    rows: dict[datetime, float] = {}
    for _day, start, energy in days:
        if energy.is_empty:
            rows.setdefault(start, 0.0)
            continue
        for ts, kwh, _prelim in energy.series.get(aspect, []):
            hour = ts.replace(minute=0, second=0, microsecond=0)
            rows[hour] = rows.get(hour, 0.0) + kwh
    total = base_sum
    out = []
    for hour in sorted(rows):
        total += rows[hour]
        out.append({"start": hour, "sum": round(total, 4)})
    return out


# Days younger than this are "not delivered yet", not gaps: the regular
# window re-fetches them anyway.
GAP_MIN_AGE_DAYS = 3
GAP_MAX_AGE_DAYS = 400
GAP_PROBES_PER_RUN = 10


class EnergyImporter:
    """Fetches recent days per profile and writes hourly statistics.

    Empty days that should have data ("gaps", e.g. a day the grid operator
    delivered late) are remembered and probed again on later runs, even after
    they have left the regular window. When a gap fills, the import restarts
    from that day so all later cumulative sums move with it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client: PowerquartierClient,
        profiles: list[Profile],
        store: Store | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.client = client
        self.profiles = profiles
        self.status: dict[str, ProfileStatus] = {p.slug: ProfileStatus() for p in profiles}
        self._store = store
        self._gaps: dict[str, set[date]] = {}

    async def async_load(self) -> None:
        data = (await self._store.async_load()) if self._store else None
        for slug, days in ((data or {}).get("gaps") or {}).items():
            self._gaps[slug] = {date.fromisoformat(d) for d in days}
        for slug, st in self.status.items():
            st.gaps = sorted(self._gaps.get(slug, set()))

    def _save(self) -> None:
        if self._store:
            self._store.async_delay_save(
                lambda: {
                    "gaps": {
                        slug: sorted(d.isoformat() for d in days)
                        for slug, days in self._gaps.items()
                    }
                },
                5,
            )

    async def _base_sum(self, stat_id: str, before: datetime) -> float:
        """Sum of the last row strictly before `before` (0 if none)."""
        for lookback in (timedelta(days=70), timedelta(days=4000)):
            result = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                before - lookback,
                before,
                {stat_id},
                "hour",
                None,
                {"sum"},
            )
            rows = result.get(stat_id) or []
            if rows:
                last = rows[-1]
                return float(last.get("sum") or 0.0)
        return 0.0

    async def _probe_gaps(self, profile: Profile, before: date, today: date) -> date | None:
        """Re-check remembered gaps older than the regular window.

        Returns the earliest gap that now has data (the import must restart
        there), or None. Gaps that are too old are dropped.
        """
        gaps = self._gaps.get(profile.slug, set())
        oldest_allowed = today - timedelta(days=GAP_MAX_AGE_DAYS)
        for d in [d for d in gaps if d < oldest_allowed]:
            gaps.discard(d)
        candidates = sorted((d for d in gaps if d < before), reverse=True)[:GAP_PROBES_PER_RUN]
        if not candidates:
            return None
        filled: list[date] = []
        for d in candidates:  # one request each; few and far between
            (_, a, b), = local_day_ranges(d, d)
            (energy,) = await self.client.async_get_days(profile, [(a, b)])
            if not energy.is_empty:
                filled.append(d)
        if filled:
            _LOGGER.info(
                "Powerquartier %s: gap(s) %s now have data, re-importing from %s",
                profile.name, ", ".join(x.isoformat() for x in sorted(filled)), min(filled),
            )
        return min(filled) if filled else None

    async def async_import(self, days_back: int, only_slug: str | None = None) -> list[str]:
        """Import per profile. Returns the slugs whose import failed."""
        today = dt_util.now().date()
        last = today - timedelta(days=1)
        failed: list[str] = []
        for profile in self.profiles:
            if only_slug and profile.slug != only_slug:
                continue
            st = self.status[profile.slug]
            st.last_run = dt_util.utcnow()
            first = today - timedelta(days=days_back)
            try:
                refill_from = await self._probe_gaps(profile, first, today)
                if refill_from is not None:
                    first = refill_from
                ranges = local_day_ranges(first, last)
                energies = await self.client.async_get_days(
                    profile, [(a, b) for _, a, b in ranges]
                )
                days = [(d, a, e) for (d, a, _), e in zip(ranges, energies)]
                await self._write(profile, days, today)
                st.last_error = None
            except PowerquartierAuthError as err:
                st.last_error = f"Login: {err}"
                raise
            except Exception as err:  # noqa: BLE001 - surfaced on the sensor
                st.last_error = f"{type(err).__name__}: {err}"
                failed.append(profile.slug)
                _LOGGER.warning("Powerquartier import for %s failed: %s", profile.name, err)
            finally:
                st.gaps = sorted(self._gaps.get(profile.slug, set()))
                self._save()
                async_dispatcher_send(self.hass, SIGNAL_IMPORT_UPDATED, self.entry_id)
        return failed

    def _update_gaps(
        self,
        profile: Profile,
        days: list[tuple[date, datetime, DayEnergy]],
        had_history: bool,
        today: date,
    ) -> None:
        gaps = self._gaps.setdefault(profile.slug, set())
        seen_data = had_history
        for d, _, e in days:
            if not e.is_empty:
                seen_data = True
                gaps.discard(d)
            elif seen_data and d <= today - timedelta(days=GAP_MIN_AGE_DAYS):
                gaps.add(d)  # empty although earlier days have data
            else:
                gaps.discard(d)  # before the first data, or not delivered yet

    async def _write(
        self, profile: Profile, days: list[tuple[date, datetime, DayEnergy]], today: date
    ) -> None:
        st = self.status[profile.slug]
        aspects = {a for _, _, e in days for a in e.series}
        window_start = days[0][1]
        written = 0
        had_history = False
        for aspect in sorted(aspects):
            has_data = any(abs(e.total(aspect)) > 1e-9 for _, _, e in days)
            stat_id = statistic_id(profile.slug, aspect)
            base = await self._base_sum(stat_id, window_start)
            had_history = had_history or base > 0
            if not has_data and base == 0.0:
                continue  # e.g. production series of a pure consumer
            rows = build_hourly_rows(days, aspect, base)
            if rows:
                async_add_external_statistics(self.hass, _metadata(profile, aspect), rows)
                written += len(rows)
        self._update_gaps(profile, days, had_history, today)
        filled = [(d, e) for d, _, e in days if not e.is_empty]
        if filled:
            d, e = filled[-1]
            st.last_day = d
            st.last_day_kwh = {
                ASPECT_SUFFIX[a]: round(e.total(a), 3) for a in e.series
            }
            prelim = [d for d, e in filled if e.preliminary]
            st.preliminary_from = prelim[0] if prelim else None
        st.hours_written = written
        _LOGGER.debug("Powerquartier %s: wrote %s rows", profile.name, written)
