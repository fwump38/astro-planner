"""Astro Planner: dark-sky site scoring, target visibility, calendars, sensors, events and services for astrophotographers."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .astro import Observer
from .bortle import BortleEstimates
from .cities import cache_path
from .config import build_config
from .const import DOMAIN
from .coordinator import LiveCoordinator, PlannerCoordinator
from .drive import DriveTimes
from .engine import Engine
from .runtime import PlannerEntry, PlannerRuntime
from .services import async_register_services
from .websocket_api import async_register_websocket

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.EVENT, Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    async_register_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PlannerEntry) -> bool:
    drive = DriveTimes(hass)
    await drive.async_load()
    bortle = BortleEstimates(hass)
    await bortle.async_load()
    config = build_config(hass, dict(entry.options), drive.cached, drive.cached_source, bortle.cached)
    engine = Engine(config, dt_util.get_default_time_zone())
    planner = PlannerCoordinator(hass, entry, engine)
    live = LiveCoordinator(hass, entry, engine)
    await planner.async_config_entry_first_refresh()
    await live.async_config_entry_first_refresh()
    entry.runtime_data = PlannerRuntime(engine, planner, live, drive, bortle)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    planner.async_start_listening()
    entry.async_on_unload(planner.async_stop_listening)
    entry.async_on_unload(entry.add_update_listener(_reload))

    if config.auto_drive_time:
        entry.async_create_background_task(hass, _lookup_drive_times(planner, drive), "astro_planner drive times")
    to_estimate = [s.observer for s in config.sites if s.observer and s.bortle_source == "default"]
    if to_estimate:
        entry.async_create_background_task(hass, _estimate_bortle(hass, entry, bortle, to_estimate), "astro_planner bortle estimates")
    return True


async def _estimate_bortle(hass: HomeAssistant, entry: ConfigEntry, bortle: BortleEstimates, sites: list[Observer]) -> None:
    # The estimates change every score at the site, so reload once they're in (they're cached, so this happens once).
    if await bortle.async_estimate(sites):
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def _lookup_drive_times(planner: PlannerCoordinator, drive: DriveTimes) -> None:
    config = planner.config
    changed = False
    for site in config.sites:
        if site.observer is None or site.entity in config.manual_drive or site.drive_minutes:
            continue
        minutes = await drive.async_get(config.home, site.observer)
        if minutes:
            site.drive_minutes, site.drive_estimated, changed = minutes, True, True
            site.drive_source = drive.cached_source(config.home, site.observer) or "estimate"
    if changed:
        planner.async_update_listeners()


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: PlannerEntry) -> None:
    """Delete the cached GeoNames dump along with the integration."""
    await hass.async_add_executor_job(lambda: cache_path(hass).unlink(missing_ok=True))


async def async_unload_entry(hass: HomeAssistant, entry: PlannerEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
