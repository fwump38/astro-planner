"""End-to-end: set the integration up in a test Home Assistant and exercise the entities, calendars, events and service."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.milky_way_planner.const import DOMAIN

from .conftest import FORECAST

SITE = "weather.astroweather_kanab"


@pytest.fixture
async def setup(hass: HomeAssistant):
    await hass.config.async_set_time_zone("America/Denver")
    hass.config.latitude, hass.config.longitude, hass.config.elevation = 37.05, -112.5, 1500
    hass.states.async_set(SITE, "sunny", {"friendly_name": "AstroWeather Kanab", "location_name": "Kanab"})
    # AstroWeather's coordinate sensors live next to the weather entity.
    hass.states.async_set("sensor.astroweather_kanab_latitude", "37.047")
    hass.states.async_set("sensor.astroweather_kanab_longitude", "-112.527")
    hass.states.async_set("sensor.astroweather_kanab_elevation", "1500")

    async def get_forecasts(call):
        return {SITE: {"forecast": FORECAST}}

    from homeassistant.core import SupportsResponse

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, unique_id=DOMAIN,
        options={"sites": [{"entity": SITE, "bortle": 2, "drive_minutes": 120}], "auto_drive_time": False, "good_score_threshold": 50},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_exist(hass, setup):
    for eid in (
        "sensor.milky_way", "binary_sensor.milky_way_visible", "sensor.galactic_core_elevation", "sensor.galactic_core_azimuth",
        "sensor.galactic_core_next_rising", "sensor.galactic_core_next_setting", "sensor.moon_next_rising", "sensor.moon_next_setting",
        "calendar.milky_way_best_nights", "calendar.milky_way_short_window_nights", "event.milky_way_events", "sensor.milky_way_next_good_night",
        "sensor.kanab_milky_way_score",
    ):
        assert hass.states.get(eid) is not None, eid
    assert hass.states.get("sensor.milky_way").state in ("visible", "twilight", "moonlit", "below_horizon")
    assert hass.states.get("sensor.kanab_milky_way_score").attributes["drive_minutes"] == 120
    src = hass.states.get("sensor.kanab_milky_way_score").attributes["source"]
    assert src["bortle"] == "configured" and src["drive_time"] == "configured" and src["sky"] == "computed"
    assert hass.states.get("sensor.milky_way").attributes["source"] == "computed"


async def test_calendars_list_pink_and_blue(hass, setup):
    start = dt_util.utcnow()
    for cal, grade in (("calendar.milky_way_best_nights", "best"), ("calendar.milky_way_short_window_nights", "short")):
        events = await hass.services.async_call(
            "calendar", "get_events", {"entity_id": cal, "start_date_time": start.isoformat(), "end_date_time": (start + timedelta(days=370)).isoformat()},
            blocking=True, return_response=True,
        )
        evs = events[cal]["events"]
        assert evs, grade
        assert all(e["summary"].startswith("Milky Way core") for e in evs)
        assert all("Core up" in e["description"] for e in evs)
    best = hass.states.get("calendar.milky_way_best_nights")
    assert best.attributes["message"] == "Milky Way core"


async def test_best_night_service(hass, setup):
    resp = await hass.services.async_call(DOMAIN, "best_night", {"days": 200, "count": 3, "sort": "date"}, blocking=True, return_response=True)
    nights = resp["nights"]
    assert 1 <= len(nights) <= 3
    assert [n["date"] for n in nights] == sorted(n["date"] for n in nights)
    assert all(n["grade"] == "best" and n["best_site"]["entity"] == SITE for n in nights)
    resp = await hass.services.async_call(DOMAIN, "best_night", {"days": 200, "count": 5, "min_grade": "short", "sort": "score"}, blocking=True, return_response=True)
    scores = [n["score"] for n in resp["nights"]]
    assert scores == sorted(scores, reverse=True)


async def test_websocket_overview_calendar_and_sky_hours(hass, hass_ws_client, setup):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "milky_way_planner/info"})
    assert (await client.receive_json())["result"]["api_version"] == 2

    await client.send_json_auto_id({"type": "milky_way_planner/overview"})
    res = (await client.receive_json())["result"]
    site = res["sites"][0]
    assert site["entity"] == SITE and site["bortle"] == 2 and site["source"]["bortle"] == "configured"
    assert res["settings"]["nights"] >= 1 and len(res["scores"][SITE]) == res["settings"]["nights"]
    row = res["scores"][SITE][0]
    assert {"date", "score", "astro_only", "cloud", "dome_warning", "night"} <= row.keys()

    await client.send_json_auto_id({"type": "milky_way_planner/calendar", "start": "2026-10-10", "days": 2})
    nights = (await client.receive_json())["result"]["nights"]
    assert len(nights) == 2 and nights[0]["samples"] and nights[0]["gc_minutes"] > 0

    await client.send_json_auto_id({"type": "milky_way_planner/sky_hours", "entity": SITE, "start": "2026-10-10T00:00:00+00:00", "hours": 5})
    hours = (await client.receive_json())["result"]["hours"]
    assert len(hours) == 5 and {"sun", "moon", "lm", "alt", "vis", "dp"} <= hours[0].keys()

    await client.send_json_auto_id({"type": "milky_way_planner/sky_hours", "entity": "weather.nope", "start": "2026-10-10T00:00:00+00:00", "hours": 1})
    assert (await client.receive_json())["error"]["code"] == "unknown_site"


async def test_window_sensors_consistent(hass, setup):
    start = dt_util.parse_datetime(hass.states.get("sensor.milky_way_next_window_start").state)
    end = dt_util.parse_datetime(hass.states.get("sensor.milky_way_next_window_end").state)
    length = float(hass.states.get("sensor.milky_way_next_window_length").state)  # hours
    assert end > start
    assert (end - start).total_seconds() / 3600 == pytest.approx(length, abs=0.02)
    assert hass.states.get("sensor.milky_way_tonight").state in ("best", "short", "none")
    assert hass.states.get("sensor.moon_next_rising").state != "unknown"
