"""Event entity: fires when the core window opens/closes and when a night's (or a watched target's) forecast becomes good or stops being."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.event import EventEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import (
    EVENT_FORECAST_DROPPED,
    EVENT_FORECAST_GOOD,
    EVENT_TARGET_DROPPED,
    EVENT_TARGET_GOOD,
    EVENT_WINDOW_END,
    EVENT_WINDOW_START,
    SEARCH_DAYS,
)
from .coordinator import PlannerCoordinator, PlannerData
from .entity import device_info
from .runtime import PlannerEntry

_HYSTERESIS = 10  # a night must fall this far below the threshold before it counts as "dropped"


async def async_setup_entry(hass: HomeAssistant, entry: PlannerEntry, async_add_entities) -> None:
    async_add_entities([PlannerEvents(entry.runtime_data.planner, entry.entry_id)])


class PlannerEvents(EventEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "events"
    _attr_event_types = [EVENT_WINDOW_START, EVENT_WINDOW_END, EVENT_FORECAST_GOOD, EVENT_FORECAST_DROPPED, EVENT_TARGET_GOOD, EVENT_TARGET_DROPPED]  # noqa: RUF012
    _attr_should_poll = False

    def __init__(self, planner: PlannerCoordinator, entry_id: str) -> None:
        self._planner = planner
        self._engine = planner.engine
        self._attr_unique_id = f"{entry_id}_events"
        self._attr_device_info = device_info(entry_id, "planner", "Astro Planner")
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._good: set[tuple[str, date]] | None = None  # (site entity, night) currently at/above threshold; None until seeded
        self._good_targets: set[tuple[str, str, date]] | None = None  # (site entity, target id, night) of watched targets, likewise

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._planner.async_add_listener(self._on_planner_update))
        self.async_on_remove(self._cancel_timer)
        self._on_planner_update()
        await self._schedule_next()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    # -- window start / end -------------------------------------------------------------------

    async def _schedule_next(self) -> None:
        """Wake at the next window boundary (the start if we're before a window, the end if inside one)."""
        self._cancel_timer()
        now = dt_util.utcnow()

        def find() -> tuple[datetime, str] | None:
            n = self._engine.next_window(now, SEARCH_DAYS, "short")
            if not n or not n.gc_start or not n.gc_end:
                return None
            return (n.gc_start, EVENT_WINDOW_START) if n.gc_start > now else (n.gc_end, EVENT_WINDOW_END)

        nxt = await self.hass.async_add_executor_job(find)
        if nxt is None:
            return
        when, kind = nxt

        @callback
        def fire(_now: datetime) -> None:
            self._trigger_event(kind, {"at": when.isoformat()})
            self.async_write_ha_state()
            self.hass.async_create_task(self._schedule_next())

        self._unsub_timer = async_track_point_in_utc_time(self.hass, fire, when + timedelta(seconds=1) if kind == EVENT_WINDOW_END else when)

    # -- forecast threshold crossings ---------------------------------------------------------

    @callback
    def _on_planner_update(self) -> None:
        data: PlannerData | None = self._planner.data
        if data is None:
            return
        fired = self._check_sites(data)
        fired = self._check_targets(data) or fired
        if fired and self.hass:
            self.async_write_ha_state()

    def _check_sites(self, data: PlannerData) -> bool:
        threshold = self._engine.config.good_score_threshold
        current: set[tuple[str, date]] = set()
        scores: dict[tuple[str, date], int] = {}
        for entity, by_date in data.scores.items():
            for d, sc in by_date.items():
                if sc and sc.score is not None and not sc.astro_only:
                    scores[(entity, d)] = sc.score
                    if sc.score >= threshold:
                        current.add((entity, d))
        previous = self._good
        if previous is None:
            self._good = current
            return False
        names = {s.entity: s.name for s in self._engine.config.sites}

        def extras(entity: str, d: date) -> dict[str, object]:
            """What's best to shoot at that site that night, so a notification can say more than "go"."""
            ranked = data.targets.get(entity, {}).get(d)
            return {"best_target": ranked[0].night.target.label, "best_target_id": ranked[0].night.target.id} if ranked else {}

        fired = False
        for key in sorted(current - previous, key=lambda k: (k[1], k[0])):
            entity, d = key
            self._trigger_event(
                EVENT_FORECAST_GOOD,
                {"night": d.isoformat(), "site": names.get(entity, entity), "entity": entity, "score": scores[key], "threshold": threshold, **extras(entity, d)},
            )
            fired = True
        keep = set(current)
        for key in previous - current:
            entity, d = key
            if d < data.tonight:
                continue  # the night has simply passed
            score = scores.get(key)
            if score is not None and score > threshold - _HYSTERESIS:
                keep.add(key)  # wobbling just under the threshold: not a real drop
                continue
            self._trigger_event(EVENT_FORECAST_DROPPED, {"night": d.isoformat(), "site": names.get(entity, entity), "entity": entity, "score": score, "threshold": threshold})
            fired = True
        self._good = keep
        return fired

    def _check_targets(self, data: PlannerData) -> bool:
        """Same threshold crossings, for each target on the watch list (the core included) at each site."""
        watched = set(self._engine.config.watch_targets)
        if not watched:
            self._good_targets = set()
            return False
        threshold = self._engine.config.good_score_threshold
        current: set[tuple[str, str, date]] = set()
        scores: dict[tuple[str, str, date], int] = {}
        labels: dict[str, str] = {}
        for entity, by_date in data.targets.items():
            for d, ranked in by_date.items():
                for ts in ranked:
                    tid = ts.night.target.id
                    if tid not in watched or ts.astro_only:
                        continue
                    labels[tid] = ts.night.target.label
                    scores[(entity, tid, d)] = ts.score
                    if ts.score >= threshold:
                        current.add((entity, tid, d))
        previous = self._good_targets
        if previous is None:
            self._good_targets = current
            return False
        names = {s.entity: s.name for s in self._engine.config.sites}
        fired = False

        def payload(key: tuple[str, str, date], score: int | None) -> dict[str, object]:
            entity, tid, d = key
            return {"night": d.isoformat(), "site": names.get(entity, entity), "entity": entity, "target": labels.get(tid, tid), "target_id": tid, "score": score, "threshold": threshold}

        for key in sorted(current - previous, key=lambda k: (k[2], k[0], k[1])):
            self._trigger_event(EVENT_TARGET_GOOD, payload(key, scores[key]))
            fired = True
        keep = set(current)
        for key in previous - current:
            if key[2] < data.tonight:
                continue
            score = scores.get(key)
            if score is not None and score > threshold - _HYSTERESIS:
                keep.add(key)
                continue
            self._trigger_event(EVENT_TARGET_DROPPED, payload(key, score))
            fired = True
        self._good_targets = keep
        return fired
