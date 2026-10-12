"""Milky Way Planner: Milky Way core calendars, sensors, events and a best-night service for astrophotographers."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .config import build_config
from .coordinator import LiveCoordinator, PlannerCoordinator
from .drive import DriveTimes
from .engine import Engine
from .runtime import PlannerEntry, PlannerRuntime
from .services import async_register_services
from .websocket_api import async_register_websocket

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.EVENT, Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    async_register_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PlannerEntry) -> bool:
    drive = DriveTimes(hass)
    await drive.async_load()
    config = build_config(hass, dict(entry.options), drive.cached)
    engine = Engine(config, dt_util.get_default_time_zone())
    planner = PlannerCoordinator(hass, entry, engine)
    live = LiveCoordinator(hass, entry, engine)
    await planner.async_config_entry_first_refresh()
    await live.async_config_entry_first_refresh()
    entry.runtime_data = PlannerRuntime(engine, planner, live, drive)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    planner.async_start_listening()
    entry.async_on_unload(planner.async_stop_listening)
    entry.async_on_unload(entry.add_update_listener(_reload))

    if config.auto_drive_time:
        entry.async_create_background_task(hass, _lookup_drive_times(planner, drive), "milky_way_planner drive times")
    return True


async def _lookup_drive_times(planner: PlannerCoordinator, drive: DriveTimes) -> None:
    config = planner.config
    changed = False
    for site in config.sites:
        if site.observer is None or site.entity in config.manual_drive or site.drive_minutes:
            continue
        minutes = await drive.async_get(config.home, site.observer)
        if minutes:
            site.drive_minutes, site.drive_estimated, changed = minutes, True, True
    if changed:
        planner.async_update_listeners()


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PlannerEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
