"""Two calendars: pink nights (a full-length core window) and blue nights (a short one)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .astro import NightSummary, compass_point
from .const import COLOR_BEST, COLOR_SHORT, GRADE_BEST, GRADE_SHORT, SEARCH_DAYS
from .coordinator import PlannerCoordinator, PlannerData
from .engine import Engine
from .entity import device_info
from .runtime import PlannerEntry
from .scoring import score_band


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


async def async_setup_entry(hass: HomeAssistant, entry: PlannerEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    planner = entry.runtime_data.planner
    async_add_entities([GradeCalendar(planner, entry.entry_id, GRADE_BEST), GradeCalendar(planner, entry.entry_id, GRADE_SHORT)])
