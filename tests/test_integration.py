"""End-to-end: set the integration up in a test Home Assistant and exercise the entities, calendars, events and service."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.astro_planner.const import DOMAIN

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
        "calendar.milky_way_best_nights", "calendar.milky_way_short_window_nights", "event.astro_planner_events", "sensor.milky_way_next_good_night",
        "sensor.kanab_sky_score",
    ):
        assert hass.states.get(eid) is not None, eid
    assert hass.states.get("sensor.milky_way").state in ("visible", "twilight", "moonlit", "below_horizon")
    assert hass.states.get("sensor.kanab_sky_score").attributes["drive_minutes"] == 120
    src = hass.states.get("sensor.kanab_sky_score").attributes["source"]
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
    await client.send_json_auto_id({"type": "astro_planner/info"})
    assert (await client.receive_json())["result"]["api_version"] == 3

    await client.send_json_auto_id({"type": "astro_planner/overview"})
    res = (await client.receive_json())["result"]
    site = res["sites"][0]
    assert site["entity"] == SITE and site["bortle"] == 2 and site["source"]["bortle"] == "configured"
    assert res["settings"]["nights"] >= 1 and len(res["scores"][SITE]) == res["settings"]["nights"]
    row = res["scores"][SITE][0]
    assert {"date", "score", "astro_only", "cloud", "dome_warning", "night"} <= row.keys()

    await client.send_json_auto_id({"type": "astro_planner/calendar", "start": "2026-10-10", "days": 2})
    nights = (await client.receive_json())["result"]["nights"]
    assert len(nights) == 2 and nights[0]["samples"] and nights[0]["gc_minutes"] > 0

    await client.send_json_auto_id({"type": "astro_planner/sky_hours", "entity": SITE, "start": "2026-10-10T00:00:00+00:00", "hours": 5})
    hours = (await client.receive_json())["result"]["hours"]
    assert len(hours) == 5 and {"sun", "moon", "lm", "alt", "vis", "dp"} <= hours[0].keys()

    await client.send_json_auto_id({"type": "astro_planner/sky_hours", "entity": "weather.nope", "start": "2026-10-10T00:00:00+00:00", "hours": 1})
    assert (await client.receive_json())["error"]["code"] == "unknown_site"


async def test_window_sensors_consistent(hass, setup):
    start = dt_util.parse_datetime(hass.states.get("sensor.milky_way_next_window_start").state)
    end = dt_util.parse_datetime(hass.states.get("sensor.milky_way_next_window_end").state)
    length = float(hass.states.get("sensor.milky_way_next_window_length").state)  # hours
    assert end > start
    assert (end - start).total_seconds() / 3600 == pytest.approx(length, abs=0.02)
    assert hass.states.get("sensor.milky_way_tonight").state in ("best", "short", "none")
    assert hass.states.get("sensor.moon_next_rising").state != "unknown"


async def test_planner_entities_work_in_any_season(hass, setup):
    for eid in (
        "sensor.astro_planner_best_site_tonight", "sensor.astro_planner_best_target_tonight", "sensor.astro_planner_dark_hours_tonight",
        "sensor.astro_planner_moon_free_dark_hours_tonight", "sensor.astro_planner_messier_objects_up_tonight", "sensor.astro_planner_next_meteor_shower",
        "calendar.astro_planner_moonless_nights", "calendar.astro_planner_meteor_showers",
    ):
        assert hass.states.get(eid) is not None, eid
    score = hass.states.get("sensor.kanab_sky_score")
    assert score.state not in ("unknown", "unavailable") and 0 <= int(score.state) <= 100  # a number even when the core isn't up
    assert "milky_way" in score.attributes and score.attributes["nights"][0]["sky"] > 0
    assert hass.states.get("sensor.astro_planner_best_site_tonight").state == "Kanab"
    best = hass.states.get("sensor.astro_planner_best_target_tonight")
    known = {t.label for t in setup.runtime_data.engine.config.targets} | {"Milky Way – Galactic core"}
    assert best.state in known
    assert best.attributes["site"] == "Kanab" and best.attributes["alternatives"]
    dark = float(hass.states.get("sensor.astro_planner_dark_hours_tonight").state)
    free = float(hass.states.get("sensor.astro_planner_moon_free_dark_hours_tonight").state)
    assert 3 < dark < 15 and 0 <= free <= dark + 0.01
    assert int(hass.states.get("sensor.astro_planner_messier_objects_up_tonight").state) > 5
    shower = hass.states.get("sensor.astro_planner_next_meteor_shower")
    assert shower.attributes["zhr"] > 0 and shower.attributes["days_until_peak"] >= 0


async def test_new_calendars_list_events(hass, setup):
    start = dt_util.utcnow()
    for cal, summary_start in (("calendar.astro_planner_moonless_nights", "Moonless"), ("calendar.astro_planner_meteor_showers", None)):
        resp = await hass.services.async_call(
            "calendar", "get_events", {"entity_id": cal, "start_date_time": start.isoformat(), "end_date_time": (start + timedelta(days=366)).isoformat()},
            blocking=True, return_response=True,
        )
        evs = resp[cal]["events"]
        assert evs, cal
        if summary_start:
            assert all(e["summary"].startswith(summary_start) for e in evs)
            assert len(evs) >= 20  # roughly one new moon's worth of nights every month
        else:
            names = {e["summary"] for e in evs}
            assert "Perseids peak" in names and "Geminids peak" in names and len(evs) >= 12
            assert all("ZHR" in e["description"] and "Moon" in e["description"] for e in evs)


async def test_best_night_any_grade_works_out_of_season(hass, setup):
    resp = await hass.services.async_call(DOMAIN, "best_night", {"days": 60, "count": 5, "min_grade": "any", "sort": "score"}, blocking=True, return_response=True)
    nights = resp["nights"]
    assert len(nights) == 5
    scores = [n["score"] for n in nights]
    assert scores == sorted(scores, reverse=True) and all(s is not None for s in scores)
    assert nights[0]["best_targets"] and nights[0]["best_site"]["entity"] == SITE


async def test_visible_targets_service_and_websocket(hass, hass_ws_client, setup):
    resp = await hass.services.async_call(DOMAIN, "visible_targets", {"days": 2, "min_minutes": 90, "kinds": ["galaxy", "planet"]}, blocking=True, return_response=True)
    assert resp["targets"] and {t["kind"] for t in resp["targets"]} <= {"galaxy", "planet"}
    t = resp["targets"][0]
    assert t["label"] and t["nights"] and {"minutes", "peak_altitude", "direction", "start", "end"} <= t["nights"][0].keys()
    assert all(n["minutes"] >= 90 for tt in resp["targets"] for n in tt["nights"])
    assert resp["settings"]["min_altitude"] == 30 and len(resp["dark"]) == 2

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "astro_planner/targets", "days": 1})
    res = (await client.receive_json())["result"]
    by = {t["id"]: t for t in res["targets"]}
    assert res["api_version"] == 3 and len(by) > 10
    m = next(t for t in res["targets"] if t["designation"].startswith("M"))
    assert " – " in m["label"] and m["best_date"] and m["kind_label"]
    assert res["total"] > 110  # Messier plus the planets

    await client.send_json_auto_id({"type": "astro_planner/showers", "count": 3})
    showers = (await client.receive_json())["result"]["showers"]
    assert len(showers) == 3 and [s["peak"] for s in showers] == sorted(s["peak"] for s in showers)

    await client.send_json_auto_id({"type": "astro_planner/overview"})
    row = (await client.receive_json())["result"]["scores"][SITE][0]
    assert row["score"] is not None and "milky_way" in row and "targets" in row
