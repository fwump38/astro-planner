"""Coordinators: slow (weather + scores) and live (sky position and next events)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import astronomy as ae
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .astro import GalacticCenter, NightSummary, body_altitude, galactic_center_position, next_crossing, to_time
from .config import PlannerConfig, Site
from .const import FORECAST_REFRESH_MINUTES, LIVE_REFRESH_SECONDS, SEARCH_DAYS
from .engine import Engine
from .scoring import EMPTY_FORECAST, Forecast, NightScore

_LOGGER = logging.getLogger(__name__)
MOON_UP_ALT = -0.5


@dataclass
class PlannerData:
    tonight: date
    dates: list[date]
    ref: dict[date, NightSummary]
    scores: dict[str, dict[date, NightScore | None]]
    forecast_end: dict[str, datetime | None]
    best: dict[date, tuple[Site, NightScore]] = field(default_factory=dict)

    def best_site(self, d: date) -> tuple[Site, NightScore] | None:
        return self.best.get(d)


class PlannerCoordinator(DataUpdateCoordinator[PlannerData]):
    """Fetches each site's hourly forecast and scores the coming nights."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, engine: Engine) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name="Milky Way Planner", update_interval=timedelta(minutes=FORECAST_REFRESH_MINUTES)
        )
        self.engine = engine
        self._forecasts: dict[str, Forecast] = {}
        self._unsub_states = None

    @property
    def config(self) -> PlannerConfig:
        return self.engine.config

    def forecast_for(self, entity: str) -> Forecast:
        return self._forecasts.get(entity, EMPTY_FORECAST)

    @callback
    def async_start_listening(self) -> None:
        """Refresh when a site's weather entity updates (the coordinator debounces bursts)."""
        entities = [s.entity for s in self.config.sites]
        if entities:
            self._unsub_states = async_track_state_change_event(self.hass, entities, lambda _e: self.hass.async_create_task(self.async_request_refresh()))

    @callback
    def async_stop_listening(self) -> None:
        if self._unsub_states:
            self._unsub_states()
            self._unsub_states = None

    async def _fetch_forecast(self, entity: str) -> None:
        try:
            resp: dict[str, Any] | None = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": entity, "type": "hourly"}, blocking=True, return_response=True
            )
        except HomeAssistantError as err:
            _LOGGER.debug("No hourly forecast from %s: %s", entity, err)
            return
        rows = (resp or {}).get(entity, {}).get("forecast")
        if rows:
            self._forecasts[entity] = Forecast.from_rows(rows)

    async def _async_update_data(self) -> PlannerData:
        for site in self.config.sites:
            if self.hass.states.get(site.entity):
                await self._fetch_forecast(site.entity)
        return await self.hass.async_add_executor_job(self._compute, dt_util.utcnow())

    def _compute(self, now: datetime) -> PlannerData:
        engine = self.engine
        tonight = engine.tonight(now)
        dates = [tonight + timedelta(days=i) for i in range(self.config.nights)]
        ref = {d: engine.ref_night(d) for d in dates}
        scores: dict[str, dict[date, NightScore | None]] = {}
        best: dict[date, tuple[Site, NightScore]] = {}
        for site in self.config.sites:
            fc = self.forecast_for(site.entity)
            scores[site.entity] = {d: engine.score(site, d, fc) for d in dates}
            for d, sc in scores[site.entity].items():
                if sc and sc.score is not None and (d not in best or sc.score > (best[d][1].score or 0)):
                    best[d] = (site, sc)
        return PlannerData(tonight, dates, ref, scores, {e: f.end for e, f in self._forecasts.items()}, best)


@dataclass
class LiveState:
    now: datetime
    gc: GalacticCenter
    sun_altitude: float
    moon_altitude: float
    moon_illumination: float  # 0-1
    moon_phase: float
    status: str  # visible | twilight | moonlit | below_horizon
    gc_up: bool
    visible: bool
    gc_next_rising: datetime | None
    gc_next_setting: datetime | None
    moon_next_rising: datetime | None
    moon_next_setting: datetime | None
    window: NightSummary | None  # the next (or current) night with a core window
    next_best: NightSummary | None  # the next (or current) full-length ("pink") night
    tonight: NightSummary | None


class LiveCoordinator(DataUpdateCoordinator[LiveState]):
    """Sun-style live values: position of the core and moon plus the next rise/set and window times."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, engine: Engine) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="Milky Way live sky", update_interval=timedelta(seconds=LIVE_REFRESH_SECONDS))
        self.engine = engine
        self._crossings: dict[str, datetime | None] = {}
        self._crossings_valid_until = dt_util.utc_from_timestamp(0)

    async def _async_update_data(self) -> LiveState:
        return await self.hass.async_add_executor_job(self._compute, dt_util.utcnow())

    def _refresh_crossings(self, now: datetime) -> None:
        if now < self._crossings_valid_until:
            return
        cfg = self.engine.config
        obs = cfg.reference.ae()
        gc_alt = lambda dt: galactic_center_position(to_time(dt), obs).altitude
        moon_alt = lambda dt: body_altitude(ae.Body.Moon, to_time(dt), obs)
        thr = cfg.calendar_min_altitude
        self._crossings = {
            "gc_rise": next_crossing(gc_alt, now, True, thr),
            "gc_set": next_crossing(gc_alt, now, False, thr),
            "moon_rise": next_crossing(moon_alt, now, True, MOON_UP_ALT),
            "moon_set": next_crossing(moon_alt, now, False, MOON_UP_ALT),
        }
        times = [t for t in self._crossings.values() if t]
        # Recompute once the earliest cached time has passed; with no crossing in range, look again in an hour.
        self._crossings_valid_until = min(times) if times else now + timedelta(hours=1)

    def _compute(self, now: datetime) -> LiveState:
        engine = self.engine
        cfg = engine.config
        obs = cfg.reference.ae()
        t = to_time(now)
        gc = galactic_center_position(t, obs)
        sun_alt = body_altitude(ae.Body.Sun, t, obs)
        moon_alt = body_altitude(ae.Body.Moon, t, obs)
        moon_ill = ae.Illumination(ae.Body.Moon, t).phase_fraction
        moon_ok = moon_alt < MOON_UP_ALT or moon_ill * 100 <= cfg.max_moon_illumination
        gc_up = gc.altitude >= cfg.calendar_min_altitude
        if not gc_up:
            status = "below_horizon"
        elif sun_alt >= -18:
            status = "twilight"
        elif not moon_ok:
            status = "moonlit"
        else:
            status = "visible"
        self._refresh_crossings(now)
        c = self._crossings
        return LiveState(
            now=now, gc=gc, sun_altitude=sun_alt, moon_altitude=moon_alt, moon_illumination=moon_ill, moon_phase=ae.MoonPhase(t),
            status=status, gc_up=gc_up, visible=status == "visible",
            gc_next_rising=c["gc_rise"], gc_next_setting=c["gc_set"], moon_next_rising=c["moon_rise"], moon_next_setting=c["moon_set"],
            window=engine.next_window(now, SEARCH_DAYS, "short"),
            next_best=engine.next_window(now, SEARCH_DAYS, "best"),
            tonight=engine.ref_night(engine.tonight(now)),
        )
