"""Websocket API used by the Astro Planner card to offload its astronomy to the integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .astro import NightSummary, Observer, compute_night, sample_sky
from .cities import CityLookupError, async_find_domes
from .config import Site
from .const import API_VERSION, CONF_LIGHT_DOMES, CONF_LIGHT_DOMES_INFO, DOMAIN
from .showers import SHOWERS, is_active, outlook, upcoming
from .target_view import targets_payload

MAX_DAYS = 62
MAX_TARGET_DAYS = 31


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize_night(n: NightSummary, include_samples: bool) -> dict[str, Any]:
    w = n.worst_dome
    out: dict[str, Any] = {
        "date": n.date.isoformat(),
        "dusk": _iso(n.dusk),
        "dawn": _iso(n.dawn),
        "step_minutes": n.step_minutes,
        "moon_phase": n.moon_phase,
        "moon_illumination": n.moon_illumination,
        "moon_rise": _iso(n.moon_rise),
        "moon_set": _iso(n.moon_set),
        "dark_minutes": n.dark_minutes,
        "gc_minutes": n.gc_minutes,
        "gc_start": _iso(n.gc_start),
        "gc_end": _iso(n.gc_end),
        "gc_peak_altitude": n.gc_peak_altitude,
        "gc_tilt": n.gc_tilt,
        "orientation": n.orientation,
        "gc_azimuth_start": n.gc_azimuth_start,
        "gc_azimuth_end": n.gc_azimuth_end,
        "dome_penalty": n.dome_penalty,
        "worst_dome": None
        if w is None
        else {
            "name": w.dome.name,
            "latitude": w.dome.latitude,
            "longitude": w.dome.longitude,
            "population": w.dome.population,
            "distance_km": w.distance_km,
            "bearing": w.bearing,
            "glow": w.glow,
        },
    }
    if include_samples:
        out["samples"] = [
            {
                "t": int(s.time.timestamp() * 1000),
                "sun": round(s.sun_altitude, 3),
                "moon": round(s.moon_altitude, 3),
                "ill": round(s.moon_illumination, 5),
                "alt": round(s.gc.altitude, 3),
                "az": round(s.gc.azimuth, 3),
                "tilt": round(s.gc.tilt, 3),
                "dark": s.dark,
                "vis": s.gc_visible,
                "lm": round(s.limiting_magnitude, 3),
                "dp": round(s.dome_penalty, 5),
            }
            for s in n.samples
        ]
    return out


def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_info)
    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_calendar)
    websocket_api.async_register_command(hass, ws_sky_hours)
    websocket_api.async_register_command(hass, ws_targets)
    websocket_api.async_register_command(hass, ws_showers)
    websocket_api.async_register_command(hass, ws_light_domes)
    websocket_api.async_register_command(hass, ws_find_light_domes)


@websocket_api.websocket_command({vol.Required("type"): "astro_planner/info"})
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"api_version": API_VERSION})


_OBSERVER = vol.Schema(
    {
        vol.Required("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90, max=90)),
        vol.Required("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
        vol.Optional("elevation", default=0.0): vol.Coerce(float),
    }
)


def _runtime(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]):
    """The loaded config entry's runtime data, or None after replying with an error."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    connection.send_error(msg["id"], "not_loaded", "Astro Planner isn't set up")
    return None


def _site_json(site: Site) -> dict[str, Any]:
    o = site.observer
    return {
        "entity": site.entity,
        "name": site.name,
        "bortle": site.bortle,
        "drive_minutes": site.drive_minutes,
        "drive_estimated": site.drive_estimated,
        "latitude": o.latitude if o else None,
        "longitude": o.longitude if o else None,
        "elevation": o.elevation if o else None,
        "source": {"coordinates": site.coordinates_source, "bortle": site.bortle_source, "drive_time": site.drive_source},
    }


def _observer_json(o: Observer) -> dict[str, float]:
    return {"latitude": o.latitude, "longitude": o.longitude, "elevation": o.elevation}


@websocket_api.websocket_command({vol.Required("type"): "astro_planner/overview"})
@websocket_api.async_response
async def ws_overview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Everything the card's Tonight and calendar-badge views show: the sites, their settings and each site's scored nights."""
    rt = _runtime(hass, connection, msg)
    if rt is None:
        return
    cfg = rt.engine.config
    data = rt.planner.data
    scores: dict[str, list[dict[str, Any]]] = {}
    for site in cfg.sites:
        rows = []
        for d in data.dates:
            sc = data.scores.get(site.entity, {}).get(d)
            if sc is None:
                continue
            rows.append(
                {
                    "date": d.isoformat(),
                    "score": sc.score,
                    "astro_only": sc.astro_only,
                    "weather": sc.weather,
                    "cloud": sc.cloud,
                    "dome_warning": sc.dome_warning,
                    "milky_way": sc.milky_way,
                    "sky": sc.sky,
                    "targets": [
                        {
                            "id": t.night.target.id,
                            "label": t.night.target.label,
                            "kind": t.night.target.kind,
                            "score": t.score,
                            "minutes": t.night.minutes,
                            "peak_altitude": round(t.night.peak_altitude),
                            "direction": t.night.direction,
                        }
                        for t in data.targets.get(site.entity, {}).get(d, [])[:5]
                    ],
                    "night": serialize_night(sc.night, False),
                }
            )
        scores[site.entity] = rows
    connection.send_result(
        msg["id"],
        {
            "api_version": API_VERSION,
            "tonight": data.tonight.isoformat(),
            "reference": _observer_json(cfg.reference),
            "settings": {
                "nights": cfg.nights,
                "min_gc_altitude": cfg.min_gc_altitude,
                "max_moon_illumination": cfg.max_moon_illumination,
                "calendar_min_altitude": cfg.calendar_min_altitude,
                "calendar_best_minutes": cfg.calendar_best_minutes,
                "dome_warning_threshold": cfg.dome_warning_threshold,
                "target_min_altitude": cfg.target_min_altitude,
                "target_max_magnitude": cfg.target_max_magnitude,
                "fov_arcmin": cfg.fov_arcmin,
            },
            "sites": [_site_json(s) for s in cfg.sites],
            "scores": scores,
            "forecast_end": {e: end.isoformat() for e, end in data.forecast_end.items() if end},
        },
    )


def _calendar(rt, start: date, days: int) -> list[dict[str, Any]]:
    cfg = rt.engine.config
    ref = cfg.reference
    opts = cfg.calendar_options()
    return [serialize_night(compute_night(start + timedelta(days=i), ref, opts, 10), True) for i in range(days)]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "astro_planner/targets",
        vol.Optional("start"): vol.Coerce(date.fromisoformat),
        vol.Optional("days", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_TARGET_DAYS)),
    }
)
@websocket_api.async_response
async def ws_targets(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Messier objects, planets and custom targets that are up after dark at the Home Assistant location."""
    rt = _runtime(hass, connection, msg)
    if rt is None:
        return
    start = msg.get("start") or rt.planner.data.tonight
    payload = await hass.async_add_executor_job(targets_payload, rt.engine, start, msg["days"])
    connection.send_result(msg["id"], {"api_version": API_VERSION, **payload})


@websocket_api.websocket_command({vol.Required("type"): "astro_planner/showers", vol.Optional("count", default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=len(SHOWERS)))})
@websocket_api.async_response
async def ws_showers(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The next meteor showers to peak, each rated for its peak night here."""
    rt = _runtime(hass, connection, msg)
    if rt is None:
        return
    today = rt.planner.data.tonight

    def build() -> list[dict[str, Any]]:
        out = []
        for shower, peak in upcoming(today, msg["count"]):
            o = outlook(shower, peak, rt.engine.home_dark(peak))
            out.append(
                {
                    "id": shower.id,
                    "name": shower.name,
                    "peak": peak.isoformat(),
                    "days_until": (peak - today).days,
                    "active": is_active(shower, today),
                    "zhr": shower.zhr,
                    "radiant_peak_altitude": round(o.radiant_peak_altitude),
                    "moon_illumination": round(o.moon_illumination * 100),
                    "moon_free_minutes": o.moon_free_minutes,
                    "rating": o.rating,
                }
            )
        return out

    connection.send_result(msg["id"], {"api_version": API_VERSION, "showers": await hass.async_add_executor_job(build)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "astro_planner/calendar",
        vol.Required("start"): vol.Coerce(date.fromisoformat),
        vol.Required("days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DAYS)),
    }
)
@websocket_api.async_response
async def ws_calendar(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Nights at the centre of the sites (the calendar's astronomy), with the samples behind the timeline bars."""
    rt = _runtime(hass, connection, msg)
    if rt is None:
        return
    nights = await hass.async_add_executor_job(_calendar, rt, msg["start"], msg["days"])
    connection.send_result(msg["id"], {"api_version": API_VERSION, "nights": nights})


def _sky_hours(rt, site: Site, start: datetime, hours: int) -> list[dict[str, Any]]:
    opts = rt.engine.config.site_options(site)
    obs = site.observer.ae()
    out = []
    for i in range(hours):
        s = sample_sky(start + timedelta(hours=i), site.observer, opts, obs)
        out.append(
            {
                "t": int(s.time.timestamp() * 1000),
                "sun": round(s.sun_altitude, 3),
                "moon": round(s.moon_altitude, 3),
                "lm": round(s.limiting_magnitude, 3),
                "alt": round(s.gc.altitude, 3),
                "vis": s.gc_visible,
                "dp": round(s.dome_penalty, 5),
            }
        )
    return out


@websocket_api.websocket_command(
    {
        vol.Required("type"): "astro_planner/sky_hours",
        vol.Required("entity"): str,
        vol.Required("start"): vol.Coerce(datetime.fromisoformat),
        vol.Required("hours"): vol.All(vol.Coerce(int), vol.Range(min=1, max=96)),
    }
)
@websocket_api.async_response
async def ws_sky_hours(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Hourly sky conditions at a site (sun, moon, limiting magnitude, core altitude, dome interference) for the sky chart."""
    rt = _runtime(hass, connection, msg)
    if rt is None:
        return
    site = next((s for s in rt.engine.config.sites if s.entity == msg["entity"] and s.observer), None)
    if site is None:
        connection.send_error(msg["id"], "unknown_site", f"{msg['entity']} isn't a located site")
        return
    hours = await hass.async_add_executor_job(_sky_hours, rt, site, msg["start"], msg["hours"])
    connection.send_result(msg["id"], {"api_version": API_VERSION, "hours": hours})


# -- light domes: stored lookup results and on-demand lookup, for the card ---------------------


def _entry_options(hass: HomeAssistant) -> dict[str, Any] | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        return dict(entry.options)
    return None


@websocket_api.websocket_command({vol.Required("type"): "astro_planner/light_domes"})
@websocket_api.async_response
async def ws_light_domes(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The light domes stored in the integration (from its "Find nearby cities" lookup or edited by hand)."""
    options = _entry_options(hass) or {}
    connection.send_result(
        msg["id"],
        {"api_version": API_VERSION, "domes": options.get(CONF_LIGHT_DOMES, []), "info": options.get(CONF_LIGHT_DOMES_INFO)},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "astro_planner/find_light_domes",
        vol.Required("points"): vol.All([_OBSERVER], vol.Length(min=1, max=30)),
        vol.Optional("radius_km", default=250): vol.All(vol.Coerce(float), vol.Range(min=10, max=600)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_find_light_domes(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Look up cities around some points (GeoNames, then Wikidata) without storing anything."""
    points = [Observer(p["latitude"], p["longitude"], p.get("elevation", 0.0)) for p in msg["points"]]
    try:
        result = await async_find_domes(hass, points, msg["radius_km"])
    except CityLookupError as err:
        connection.send_error(msg["id"], "lookup_failed", "; ".join(err.failures))
        return
    connection.send_result(
        msg["id"],
        {
            "api_version": API_VERSION,
            "source": result.source,
            "places_considered": result.places_considered,
            "domes": [{"name": d.name, "latitude": d.latitude, "longitude": d.longitude, "population": d.population} for d in result.domes],
        },
    )
