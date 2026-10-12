"""Estimate a site's Bortle class from nearby cities (Walker's law), since AstroWeather doesn't provide one.

This is a rough stand-in for a real light-pollution atlas: it only knows about towns of 1,000+ people, ignores industry, roads
and terrain, and Walker's law is meant for 10-200 km. Override the value in the site's settings when you know better.
"""

from __future__ import annotations

import logging
import math

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .astro import Observer, distance_km
from .cities import CityLookupError, Place, async_find_places
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_STORE_VERSION = 1
SEARCH_RADIUS_KM = 300
MIN_DISTANCE_KM = 10  # Walker's law isn't valid closer in; a site inside a town is treated as 10 km from its centre
NATURAL_SQM = 22.0  # zenith sky brightness with no artificial light, mag/arcsec²

# Lower bound of zenith sky brightness (mag/arcsec²) for each class, darkest first.
BORTLE_SQM = ((1, 21.99), (2, 21.89), (3, 21.69), (4, 21.25), (5, 20.5), (6, 19.5), (7, 18.9), (8, 18.0))


def sky_brightness(site: Observer, places: list[Place]) -> float:
    """Zenith brightness in mag/arcsec². Walker's law: artificial/natural ratio = 0.01 * population * distance_km^-2.5, summed over places."""
    ratio = sum(0.01 * p.population * max(distance_km(site, p.latitude, p.longitude), MIN_DISTANCE_KM) ** -2.5 for p in places)
    return NATURAL_SQM - 2.5 * math.log10(1 + ratio)


def bortle_from_sqm(sqm: float) -> int:
    for bortle, floor in BORTLE_SQM:
        if sqm >= floor:
            return bortle
    return 9


def estimate_bortle(site: Observer, places: list[Place]) -> int:
    return bortle_from_sqm(sky_brightness(site, places))


def _key(o: Observer) -> str:
    return f"{round(o.latitude, 2)},{round(o.longitude, 2)}"


class BortleEstimates:
    """Computes and caches estimates, so the city lookup only runs for a site once."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, dict]] = Store(hass, _STORE_VERSION, f"{DOMAIN}.bortle_estimates")
        self._cache: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._cache = await self._store.async_load() or {}

    def cached(self, site: Observer) -> int | None:
        entry = self._cache.get(_key(site))
        return entry["bortle"] if entry else None

    async def async_estimate(self, sites: list[Observer]) -> bool:
        """Estimate every site that has no cached value. Returns True if anything new was stored."""
        missing = {_key(o): o for o in sites if _key(o) not in self._cache}
        if not missing:
            return False
        try:
            metros, found = await async_find_places(self._hass, list(missing.values()), SEARCH_RADIUS_KM)
        except CityLookupError as err:
            _LOGGER.debug("Couldn't estimate Bortle class (will retry on the next restart): %s", err)
            return False
        for key, obs in missing.items():
            self._cache[key] = {"bortle": estimate_bortle(obs, metros), "source": found.source}
        await self._store.async_save(self._cache)
        return True
