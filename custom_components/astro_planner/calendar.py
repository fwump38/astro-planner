"""Calendars: pink/blue Milky Way nights, moonless nights and meteor shower peaks."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .astro import NightSummary, compass_point
from .const import COLOR_BEST, COLOR_SHORT, GRADE_BEST, GRADE_SHORT, MOONLESS_MIN_MINUTES, SEARCH_DAYS
from .coordinator import PlannerCoordinator, PlannerData
from .engine import Engine
from .entity import device_info
from .runtime import PlannerEntry
from .scoring import score_band
from .showers import MIN_RADIANT_ALTITUDE, SHOWERS, is_active, outlook


def format_duration(minutes: float) -> str:
    h, m = divmod(round(minutes), 60)
    return f"{h}h {m:02d}m" if h and m else f"{h}h" if h else f"{m}m"


def describe_night(night: NightSummary, engine: Engine, data: PlannerData | None) -> str:
    """Plain-text details shown in the calendar event."""
    tz = engine.tz

    def clock(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%H:%M")

    lines = []
    if night.gc_start and night.gc_end:
        az = ""
        if night.gc_azimuth_start is not None:
            a, b = compass_point(night.gc_azimuth_start), compass_point(night.gc_azimuth_end)
            az = a if a == b else f"{a} to {b}"
        lines.append(f"Core up {format_duration(night.gc_minutes)}, {clock(night.gc_start)}-{clock(night.gc_end)}")
        lines.append(f"Shape: {night.orientation}" + (f", {az}" if az else ""))
    moon = f"Moon {round(night.moon_illumination * 100)}% lit"
    if night.moon_set:
        moon += f", sets {clock(night.moon_set)}"
    if night.moon_rise:
        moon += f", rises {clock(night.moon_rise)}"
    lines.append(moon)
    best = data.best_site(night.date) if data else None
    if best:
        site, sc = best
        outlook = "astronomy only (beyond forecast)" if sc.astro_only else f"{round(sc.cloud)}% cloud" if sc.cloud is not None else "forecast scored"
        lines.append(f"Best site: {site.name}, score {sc.score} ({score_band(sc.score)}), {outlook}")
    return "\n".join(lines)


class GradeCalendar(CoordinatorEntity[PlannerCoordinator], CalendarEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PlannerCoordinator, entry_id: str, grade: str) -> None:
        super().__init__(coordinator)
        self._grade = grade
        self._engine: Engine = coordinator.engine
        self._attr_translation_key = f"{grade}_nights"
        self._attr_unique_id = f"{entry_id}_calendar_{grade}"
        self._attr_initial_color = COLOR_BEST if grade == GRADE_BEST else COLOR_SHORT
        self._attr_device_info = device_info(entry_id, "milky_way", "Milky Way")
        self._event: CalendarEvent | None = None
        self._summary = "Milky Way core" if grade == GRADE_BEST else "Milky Way core (short window)"

    def _to_event(self, night: NightSummary, data: PlannerData | None) -> CalendarEvent:
        return CalendarEvent(
            start=night.gc_start,
            end=night.gc_end,
            summary=self._summary,
            description=describe_night(night, self._engine, data),
            location=self._engine.config.sites[0].name if len(self._engine.config.sites) == 1 else None,
            uid=f"milky-way-{self._grade}-{night.date.isoformat()}",
        )

    def _matches(self, night: NightSummary) -> bool:
        return bool(night.gc_start and night.gc_end and night.grade(self._engine.config.calendar_best_minutes) == self._grade)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_update_event()

    async def _async_update_event(self) -> None:
        now = dt_util.utcnow()

        def find() -> NightSummary | None:
            start = self._engine.local_date(now) - timedelta(days=1)
            for i in range(SEARCH_DAYS):
                n = self._engine.ref_night(start + timedelta(days=i))
                if self._matches(n) and n.gc_end > now:
                    return n
            return None

        night = await self.hass.async_add_executor_job(find)
        self._event = self._to_event(night, self.coordinator.data) if night else None

    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._refresh())

    async def _refresh(self) -> None:
        await self._async_update_event()
        self.async_write_ha_state()

    @property
    def event(self) -> CalendarEvent | None:
        return self._event

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        start_date, end_date = dt_util.as_utc(start_date), dt_util.as_utc(end_date)
        if end_date - start_date > timedelta(days=SEARCH_DAYS):
            end_date = start_date + timedelta(days=SEARCH_DAYS)
        first = self._engine.local_date(start_date) - timedelta(days=1)
        last = self._engine.local_date(end_date)
        data = self.coordinator.data

        def build() -> list[CalendarEvent]:
            events: list[CalendarEvent] = []
            d: date = first
            while d <= last:
                n = self._engine.ref_night(d)
                if self._matches(n) and n.gc_end > start_date and n.gc_start < end_date:
                    events.append(self._to_event(n, data))
                d += timedelta(days=1)
            return events

        return await hass.async_add_executor_job(build)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"grade": self._grade, "min_window_minutes": self._engine.config.calendar_best_minutes}


class _NightCalendar(CoordinatorEntity[PlannerCoordinator], CalendarEntity):
    """A calendar with at most one event per night, computed on demand from the home location's sky."""

    _attr_has_entity_name = True
    _lookahead_days = 60

    def __init__(self, coordinator: PlannerCoordinator, entry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._engine: Engine = coordinator.engine
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_calendar_{key}"
        self._attr_device_info = device_info(entry_id, "planner", "Astro Planner")
        self._event: CalendarEvent | None = None

    def _events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        raise NotImplementedError

    def _clock(self, dt: datetime) -> str:
        return dt.astimezone(self._engine.tz).strftime("%H:%M")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_update_event()

    async def _async_update_event(self) -> None:
        now = dt_util.utcnow()
        events = await self.hass.async_add_executor_job(self._events_between, now, now + timedelta(days=self._lookahead_days))
        self._event = next((e for e in events if e.end > now), None)

    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._refresh())

    async def _refresh(self) -> None:
        await self._async_update_event()
        self.async_write_ha_state()

    @property
    def event(self) -> CalendarEvent | None:
        return self._event

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        start_date, end_date = dt_util.as_utc(start_date), dt_util.as_utc(end_date)
        if end_date - start_date > timedelta(days=SEARCH_DAYS):
            end_date = start_date + timedelta(days=SEARCH_DAYS)
        return await hass.async_add_executor_job(self._events_between, start_date, end_date)


class MoonlessCalendar(_NightCalendar):
    """Nights with a long moon-free stretch of astronomical darkness at the Home Assistant location."""

    _attr_initial_color = "#37474f"

    def __init__(self, coordinator: PlannerCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "moonless_nights")

    def _events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        d = self._engine.local_date(start) - timedelta(days=1)
        last = self._engine.local_date(end)
        while d <= last:
            night = self._engine.home_dark(d)
            window = night.moon_free_window
            if window and (window[1] - window[0]).total_seconds() / 60 >= MOONLESS_MIN_MINUTES and window[1] > start and window[0] < end:
                minutes = (window[1] - window[0]).total_seconds() / 60
                lines = [
                    f"Dark and moonless for {format_duration(minutes)}, {self._clock(window[0])}-{self._clock(window[1])}",
                    f"Moon {round(night.moon_illumination * 100)}% lit",
                ]
                events.append(CalendarEvent(start=window[0], end=window[1], summary="Moonless sky", description="\n".join(lines), uid=f"moonless-{d.isoformat()}"))
            d += timedelta(days=1)
        return events

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"min_window_minutes": MOONLESS_MIN_MINUTES}


class MeteorShowerCalendar(_NightCalendar):
    """The peak night of each annual meteor shower, with the radiant height and moon for this location."""

    _attr_initial_color = "#ff9800"
    _lookahead_days = 400

    def __init__(self, coordinator: PlannerCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "meteor_showers")

    def _events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for year in range(self._engine.local_date(start).year - 1, self._engine.local_date(end).year + 2):
            for shower in SHOWERS:
                peak = shower.peak_date(year)
                night = self._engine.home_dark(peak)
                if night.dusk and night.dawn:
                    ev_start, ev_end = night.dusk, night.dawn
                else:  # no true darkness at this latitude in summer: use the local night instead
                    ev_start = dt_util.start_of_local_day(peak) + timedelta(hours=21)
                    ev_end = ev_start + timedelta(hours=8)
                if ev_end <= start or ev_start >= end:
                    continue
                o = outlook(shower, peak, night)
                lines = [
                    f"Up to {shower.zhr} meteors an hour under ideal skies (ZHR); active {shower.active[0][0]}/{shower.active[0][1]} to {shower.active[1][0]}/{shower.active[1][1]}",
                    f"Radiant climbs to {round(o.radiant_peak_altitude)}° and is above {MIN_RADIANT_ALTITUDE:g}° for {format_duration(o.radiant_minutes)}",
                    f"Moon {round(o.moon_illumination * 100)}% lit, {format_duration(o.moon_free_minutes)} of moon-free dark",
                    f"Outlook here: {o.rating}/100",
                ]
                events.append(
                    CalendarEvent(start=ev_start, end=ev_end, summary=f"{shower.name} peak", description="\n".join(lines), uid=f"shower-{shower.id}-{peak.isoformat()}")
                )
        return sorted(events, key=lambda e: str(e.start))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        today = self.coordinator.data.tonight if self.coordinator.data else None
        return {"active_now": [s.name for s in SHOWERS if today and is_active(s, today)]}


async def async_setup_entry(hass: HomeAssistant, entry: PlannerEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    planner = entry.runtime_data.planner
    async_add_entities(
        [
            GradeCalendar(planner, entry.entry_id, GRADE_BEST),
            GradeCalendar(planner, entry.entry_id, GRADE_SHORT),
            MoonlessCalendar(planner, entry.entry_id),
            MeteorShowerCalendar(planner, entry.entry_id),
        ]
    )
