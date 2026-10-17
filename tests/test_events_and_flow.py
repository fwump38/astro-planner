from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.astro_planner.const import DOMAIN
from custom_components.astro_planner.scoring import night_hours

from .test_integration import SITE, setup  # noqa: F401


async def test_forecast_events_fire_on_threshold_crossings(hass: HomeAssistant, setup):  # noqa: F811
    """A night turning good fires `forecast_good`; turning bad again fires `forecast_dropped`."""
    from custom_components.astro_planner.coordinator import PlannerCoordinator

    planner: PlannerCoordinator = setup.runtime_data.planner
    tonight = planner.data.tonight
    night = planner.engine.site_night(planner.config.sites[0], tonight)
    times = [s.time for s in night.visible] or night_hours(night)  # the core window, or the whole dark night out of season

    def rows(q):
        return [{"datetime": t.isoformat(), "cloudless_percentage": q, "transparency_percentage": q, "seeing_percentage": q, "calm_percentage": q} for t in times]

    state = {"rows": rows(5)}
    hass.services.async_remove("weather", "get_forecasts")

    async def fc(call):
        return {SITE: {"forecast": state["rows"]}}

    hass.services.async_register("weather", "get_forecasts", fc, supports_response=SupportsResponse.ONLY)
    await planner.async_refresh()  # seeds the baseline silently

    state["rows"] = rows(100)
    await planner.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("event.astro_planner_events").attributes["event_type"] == "forecast_good"
    assert hass.states.get("event.astro_planner_events").attributes["site"] == "Kanab"
    assert hass.states.get("event.astro_planner_events").attributes["best_target"]  # says what to shoot, not just "go"

    state["rows"] = rows(5)
    await planner.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("event.astro_planner_events").attributes["event_type"] == "forecast_dropped"


async def test_window_events_are_scheduled(hass: HomeAssistant, setup, freezer):  # noqa: F811
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    nxt = dt_util.parse_datetime(hass.states.get("sensor.milky_way_next_window_start").state)
    freezer.move_to(nxt + timedelta(seconds=2))
    async_fire_time_changed(hass, nxt + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert hass.states.get("event.astro_planner_events").attributes["event_type"] == "window_start"


async def test_config_flow_creates_entry_with_site_details(hass: HomeAssistant):
    hass.states.async_set(SITE, "sunny", {"location_name": "Kanab"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sites": [SITE], "review_sites": True})
    assert result["step_id"] == "site"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bortle": 2, "drive_minutes": 90})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["sites"] == [{"entity": SITE, "bortle": 2, "drive_minutes": 90}]


async def test_config_flow_preselects_astroweather_sites_and_skips_details(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    for name in ("kanab", "home"):
        reg.async_get_or_create("weather", "astroweather", name, suggested_object_id=f"astroweather_{name}")
    reg.async_get_or_create("weather", "other", "x", suggested_object_id="other_weather")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    key = next(k for k in result["data_schema"].schema if k == "sites")
    assert key.default() == ["weather.astroweather_home", "weather.astroweather_kanab"]
    review = next(k for k in result["data_schema"].schema if k == "review_sites")
    assert review.default() is False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sites": ["weather.astroweather_kanab"], "review_sites": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["sites"] == [{"entity": "weather.astroweather_kanab"}]


async def test_single_instance(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}, options={"sites": []}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT


async def test_works_without_any_sites(hass: HomeAssistant):
    """With no sites the integration plans for the Home Assistant location."""
    hass.config.latitude, hass.config.longitude = 37.05, -112.5
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}, options={"sites": []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.milky_way") is not None
    resp = await hass.services.async_call(DOMAIN, "best_night", {"days": 365, "count": 2, "sort": "date"}, blocking=True, return_response=True)
    assert resp["count"] == 2 and resp["nights"][0]["best_site"] is None


async def test_options_flow_updates_thresholds(hass: HomeAssistant, setup):  # noqa: F811
    result = await hass.config_entries.options.async_init(setup.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "sky"})
    data = {k: v for k, v in result["data_schema"]({"calendar_best_minutes": 120}).items()}
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["calendar_best_minutes"] == 120
    assert result["data"]["sites"][0]["entity"] == SITE  # other options are preserved


async def test_watched_target_fires_its_own_events(hass: HomeAssistant, setup):  # noqa: F811
    planner = setup.runtime_data.planner
    ranked = planner.data.targets[SITE][planner.data.tonight]
    if not ranked:
        pytest.skip("nothing is up at this site tonight")
    watched = ranked[0].night.target
    hass.config_entries.async_update_entry(setup, options={**setup.options, "watch_targets": [watched.id]})
    await hass.config_entries.async_reload(setup.entry_id)
    await hass.async_block_till_done()
    planner = setup.runtime_data.planner
    tn = next(t.night for t in planner.data.targets[SITE][planner.data.tonight] if t.night.target.id == watched.id)

    def rows(q):
        return [{"datetime": t.isoformat(), "cloudless_percentage": q, "transparency_percentage": q, "seeing_percentage": q, "calm_percentage": q} for t in tn.times]

    state = {"rows": rows(5)}
    hass.services.async_remove("weather", "get_forecasts")

    async def fc(call):
        return {SITE: {"forecast": state["rows"]}}

    hass.services.async_register("weather", "get_forecasts", fc, supports_response=SupportsResponse.ONLY)
    await planner.async_refresh()
    state["rows"] = rows(100)
    await planner.async_refresh()
    await hass.async_block_till_done()
    # With several events in one update the entity keeps the last; the target event comes after the site ones.
    attrs = hass.states.get("event.astro_planner_events").attributes
    assert attrs["event_type"] == "target_good" and attrs["target_id"] == watched.id and attrs["target"] == watched.label
    state["rows"] = rows(5)
    await planner.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("event.astro_planner_events").attributes["event_type"] in ("target_dropped", "forecast_dropped")


async def test_options_flow_targets_and_custom_targets(hass: HomeAssistant, setup):  # noqa: F811
    result = await hass.config_entries.options.async_init(setup.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "targets"})
    assert result["step_id"] == "targets"
    values = {"target_min_altitude": 40, "target_max_magnitude": 9, "fov_arcmin": 90, "watch_targets": ["milky_way", "m31"]}
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["fov_arcmin"] == 90 and result["data"]["watch_targets"] == ["milky_way", "m31"]

    result = await hass.config_entries.options.async_init(setup.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "custom_targets"})
    bad = await hass.config_entries.options.async_configure(result["flow_id"], {"custom_targets": [{"name": "Oops", "ra": "99", "dec": "0"}]})
    assert bad["type"] is FlowResultType.FORM and bad["errors"] == {"base": "invalid_targets"}
    good = await hass.config_entries.options.async_configure(result["flow_id"], {"custom_targets": [{"name": "NGC 7000", "ra": "20:59:17", "dec": "+44:31:44"}]})
    assert good["type"] is FlowResultType.CREATE_ENTRY and good["data"]["custom_targets"][0]["name"] == "NGC 7000"

