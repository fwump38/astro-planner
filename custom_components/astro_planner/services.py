"""The `astro_planner.best_night` and `astro_planner.visible_targets` services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .runtime import PlannerEntry, PlannerRuntime
from .scoring import score_band
from .target_view import targets_payload
from .targets import KIND_LABELS

SERVICE_BEST_NIGHT = "best_night"
SERVICE_VISIBLE_TARGETS = "visible_targets"
MAX_DAYS = 365

SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("start"): cv.date,
        vol.Optional("days", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DAYS)),
        vol.Optional("count", default=3): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
        vol.Optional("min_grade", default="best"): vol.In(["best", "short", "any"]),
        vol.Optional("min_score", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("sort", default="score"): vol.In(["score", "date", "window"]),
        vol.Optional("site"): cv.entity_id,
        vol.Optional("include_sites", default=True): cv.boolean,
    }
)


TARGETS_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("start"): cv.date,
        vol.Optional("days", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=14)),
        vol.Optional("kinds"): vol.All(cv.ensure_list, [vol.In(sorted(KIND_LABELS))]),
        vol.Optional("min_minutes", default=60): vol.All(vol.Coerce(int), vol.Range(min=1, max=720)),
    }
)


@dataclass
class _Candidate:
    night: Any
    grade: str
    best: tuple | None
    ranked: list[tuple]
    score: int
    targets: list


def _compute(rt: PlannerRuntime, params: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    engine = rt.engine
    cfg = engine.config
    tonight = engine.tonight(now)
    first: date = max(params.get("start") or tonight, tonight)
    sites = [s for s in cfg.sites if s.observer and (not params.get("site") or s.entity == params["site"])]
    candidates: list[_Candidate] = []
    for i in range(params["days"]):
        d = first + timedelta(days=i)
        night = engine.ref_night(d)
        grade = night.grade(cfg.calendar_best_minutes)
        if (params["min_grade"] == "best" and grade != "best") or (params["min_grade"] == "short" and grade == "none"):
            continue
        ranked = []
        for site in sites:
            sc = engine.score(site, d, rt.planner.forecast_for(site.entity))
            if sc and sc.score is not None:
                ranked.append((site, sc))
        ranked.sort(key=lambda r: -(r[1].score or 0))
        best = ranked[0] if ranked else None
        # With no sites configured (or none with the core up) fall back on the reference window quality.
        score = best[1].score if best else 0
        if score < params["min_score"]:
            continue
        targets = engine.score_targets(best[0], d, rt.planner.forecast_for(best[0].entity))[:3] if best else []
        candidates.append(_Candidate(night, grade, best, ranked, score, targets))

    if params["sort"] == "score":
        candidates.sort(key=lambda c: (-c.score, c.night.date))
    elif params["sort"] == "window":
        candidates.sort(key=lambda c: (-c.night.gc_minutes, c.night.date))
    return [_describe(c, engine.tz, params["include_sites"]) for c in candidates[: params["count"]]]


def _site_dict(site, sc, tz) -> dict[str, Any]:
    n = sc.night
    return {
        "entity": site.entity,
        "name": site.name,
        "score": sc.score,
        "band": score_band(sc.score),
        "cloud": None if sc.cloud is None else round(sc.cloud),
        "astro_only": sc.astro_only,
        "bortle": site.bortle,
        "drive_minutes": site.drive_minutes,
        "window_start": n.gc_start.isoformat() if n.gc_start else None,
        "window_end": n.gc_end.isoformat() if n.gc_end else None,
        "milky_way": sc.milky_way,
        "light_dome_warning": sc.dome_warning,
    }


def _describe(c: _Candidate, tz, include_sites: bool) -> dict[str, Any]:
    n = c.night
    out: dict[str, Any] = {
        "date": n.date.isoformat(),
        "grade": c.grade,
        "color": {"best": "pink", "short": "blue"}.get(c.grade),
        "score": c.score if c.best else None,
        "window_start": n.gc_start.isoformat() if n.gc_start else None,
        "window_end": n.gc_end.isoformat() if n.gc_end else None,
        "window_minutes": n.gc_minutes,
        "orientation": n.orientation,
        "moon_illumination": round(n.moon_illumination * 100),
        "moon_rise": n.moon_rise.isoformat() if n.moon_rise else None,
        "moon_set": n.moon_set.isoformat() if n.moon_set else None,
        "best_site": _site_dict(*c.best, tz) if c.best else None,
        "best_targets": [
            {"id": t.night.target.id, "target": t.night.target.label, "score": t.score, "peak_altitude": round(t.night.peak_altitude), "minutes": t.night.minutes}
            for t in c.targets
        ],
    }
    if include_sites:
        out["sites"] = [_site_dict(s, sc, tz) for s, sc in c.ranked]
    return out


def _runtime(hass: HomeAssistant, entry_id: str | None) -> PlannerRuntime:
    entries: list[PlannerEntry] = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if not entries:
        raise ServiceValidationError("Astro Planner is not set up")
    return entries[0].runtime_data


def async_register_services(hass: HomeAssistant) -> None:
    async def best_night(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass, call.data.get("entry_id"))
        nights = await hass.async_add_executor_job(_compute, rt, dict(call.data), dt_util.utcnow())
        return {"nights": nights, "count": len(nights)}

    async def visible_targets(call: ServiceCall) -> ServiceResponse:
        rt = _runtime(hass, call.data.get("entry_id"))
        start = call.data.get("start") or rt.planner.data.tonight
        kinds = set(call.data["kinds"]) if call.data.get("kinds") else None
        return await hass.async_add_executor_job(targets_payload, rt.engine, start, call.data["days"], kinds, call.data["min_minutes"])

    hass.services.async_register(DOMAIN, SERVICE_BEST_NIGHT, best_night, schema=SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_VISIBLE_TARGETS, visible_targets, schema=TARGETS_SCHEMA, supports_response=SupportsResponse.ONLY)
