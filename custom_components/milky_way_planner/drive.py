"""Driving time from the Home Assistant location to each site (OSRM, with a straight-line fallback)."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .astro import Observer, distance_km
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_STORE_VERSION = 1


def _round5(minutes: float) -> int:
    return int(round(minutes / 5) * 5)


def straight_line_estimate(a: Observer, b: Observer) -> int:
    """Roads wind (x1.35) and average ~70 km/h on rural roads."""
    return _round5(distance_km(a, b.latitude, b.longitude) * 1.35 / 70 * 60)


def _pair_key(a: Observer, b: Observer) -> str:
    # Rounded to 0.01 degrees (~1 km): plenty for a multi-hour drive, and avoids sending an exact home address to a third party.
    return f"{round(a.latitude, 2)},{round(a.longitude, 2)}>{round(b.latitude, 2)},{round(b.longitude, 2)}"


class DriveTimes:
    """Looks up and caches drive times. Routes are cached for good; estimates are retried on the next restart."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, dict]] = Store(hass, _STORE_VERSION, f"{DOMAIN}.drive_times")
        self._cache: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._cache = await self._store.async_load() or {}

    def cached(self, a: Observer, b: Observer) -> int | None:
        entry = self._cache.get(_pair_key(a, b))
        return entry["minutes"] if entry else None

    def cached_source(self, a: Observer, b: Observer) -> str | None:
        entry = self._cache.get(_pair_key(a, b))
        return entry["source"] if entry else None

    async def async_get(self, a: Observer, b: Observer) -> int:
        key = _pair_key(a, b)
        async with self._lock:
            if (entry := self._cache.get(key)) and entry["source"] == "route":
                return entry["minutes"]
            if distance_km(a, b.latitude, b.longitude) < 1:
                return 0
            minutes, source = None, "estimate"
            url = f"{OSRM_URL}/{round(a.longitude, 2)},{round(a.latitude, 2)};{round(b.longitude, 2)},{round(b.latitude, 2)}?overview=false"
            try:
                async with asyncio.timeout(15):
                    resp = await async_get_clientsession(self._hass).get(url)
                    data = await resp.json()
                if data.get("code") == "Ok" and data.get("routes"):
                    minutes, source = _round5(data["routes"][0]["duration"] / 60), "route"
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.debug("Routing lookup failed (%s); using a straight-line estimate", err)
            if minutes is None:
                minutes = straight_line_estimate(a, b)
            self._cache[key] = {"minutes": minutes, "source": source}
            self._store.async_delay_save(lambda: self._cache, 5)
            return minutes
