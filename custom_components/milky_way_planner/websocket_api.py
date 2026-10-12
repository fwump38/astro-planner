"""Websocket API used by the Milky Way Planner card to offload its astronomy to the integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .astro import LightDome, NightSummary, Observer, SkyOptions, compute_night, dome_geometry
from .cities import CityLookupError, async_find_domes
from .const import API_VERSION, CONF_LIGHT_DOMES, CONF_LIGHT_DOMES_INFO, DOMAIN

MAX_DAYS = 62


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


def _compute(params: dict[str, Any]) -> list[dict[str, Any]]:
    obs_p = params["observer"]
    observer = Observer(obs_p["latitude"], obs_p["longitude"], obs_p.get("elevation", 0.0))
    domes = [LightDome(d["name"], d["latitude"], d["longitude"], d.get("population", 0)) for d in params.get("domes", [])]
    opts = SkyOptions(params["min_gc_altitude"], params["max_moon_illumination"], params["bortle"], dome_geometry(observer, domes), params["dome_sigma"])
    start: date = params["start"]
    return [
        serialize_night(compute_night(start + timedelta(days=i), observer, opts, params["step_minutes"]), params["include_samples"])
        for i in range(params["days"])
    ]


def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_info)
    websocket_api.async_register_command(hass, ws_nights)
    websocket_api.async_register_command(hass, ws_light_domes)
    websocket_api.async_register_command(hass, ws_find_light_domes)


@websocket_api.websocket_command({vol.Required("type"): "milky_way_planner/info"})
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
_DOME = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("latitude"): vol.Coerce(float),
        vol.Required("longitude"): vol.Coerce(float),
        vol.Optional("population", default=0): vol.Coerce(float),
    }
)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "milky_way_planner/nights",
        vol.Required("start"): vol.Coerce(date.fromisoformat),
        vol.Required("days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DAYS)),
        vol.Required("observer"): _OBSERVER,
        vol.Optional("bortle", default=3): vol.Coerce(float),
        vol.Optional("min_gc_altitude", default=10): vol.Coerce(float),
        vol.Optional("max_moon_illumination", default=10): vol.Coerce(float),
        vol.Optional("dome_sigma", default=25): vol.Coerce(float),
        vol.Optional("domes", default=[]): [_DOME],
        vol.Optional("step_minutes", default=10): vol.All(vol.Coerce(int), vol.Range(min=2, max=30)),
        vol.Optional("include_samples", default=True): bool,
    }
)
@websocket_api.async_response
async def ws_nights(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    nights = await hass.async_add_executor_job(_compute, msg)
    connection.send_result(msg["id"], {"api_version": API_VERSION, "nights": nights})


# -- light domes: stored lookup results and on-demand lookup, for the card ---------------------


def _entry_options(hass: HomeAssistant) -> dict[str, Any] | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        return dict(entry.options)
    return None


@websocket_api.websocket_command({vol.Required("type"): "milky_way_planner/light_domes"})
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
        vol.Required("type"): "milky_way_planner/find_light_domes",
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
