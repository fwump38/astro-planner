"""Find the cities whose glow matters (light domes): GeoNames first, Wikidata as the fallback.

GeoNames publishes daily dumps of every populated place, free and with no account (CC BY 4.0). Browsers can't fetch them (no CORS headers),
which is why the card asks the integration to do this lookup. Nearby places are merged into metro areas so a city and its suburbs count as one dome.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .astro import LightDome, Observer, distance_km, glow_index
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_AGENT = "milky-way-planner (Home Assistant integration; https://github.com/fwump38/milky-way-planner)"
GEONAMES_URL = "https://download.geonames.org/export/dump/cities1000.zip"
GEONAMES_MEMBER = "cities1000.txt"
WIKIDATA_URL = "https://query.wikidata.org/sparql"
CACHE_MAX_AGE = 30 * 24 * 3600
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024
DOWNLOAD_TIMEOUT = 180
QUERY_TIMEOUT = 40

MIN_POPULATION = 1000
GLOW_CUTOFF = 0.005  # places whose glow index at every point is below this are too small or far to matter
PER_POINT_LIMIT = 12  # strongest sources kept per point, so suburbs near one site can't crowd out another site's big city
MAX_DOMES = 60
METRO_RADIUS_KM = 20  # places this close to a larger one are folded into its metro area


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    population: int


@dataclass
class LookupResult:
    domes: list[LightDome]
    source: str  # "geonames" | "wikidata"
    places_considered: int
    failures: list[str] = field(default_factory=list)


class CityLookupError(Exception):
    def __init__(self, failures: list[str]) -> None:
        super().__init__("; ".join(failures))
        self.failures = failures


# --- pure helpers ----------------------------------------------------------------------------


def bounding_box(points: list[Observer], radius_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) covering `radius_km` around every point."""
    south, north, west, east = 90.0, -90.0, 180.0, -180.0
    for p in points:
        d_lat = radius_km / 111
        d_lon = radius_km / (111 * max(0.1, math.cos(math.radians(p.latitude))))
        south, north = min(south, p.latitude - d_lat), max(north, p.latitude + d_lat)
        west, east = min(west, p.longitude - d_lon), max(east, p.longitude + d_lon)
    return max(-90.0, south), max(-180.0, west), min(90.0, north), min(180.0, east)


def parse_geonames(lines, points: list[Observer], radius_km: float) -> list[Place]:
    """Populated places (feature class P) with a population, within `radius_km` of any point.

    Columns (tab separated): 1 name, 4 latitude, 5 longitude, 6 feature class, 14 population.
    """
    south, west, north, east = bounding_box(points, radius_km)
    places: list[Place] = []
    for line in lines:
        cols = line.rstrip("\n").split("\t")
        if len(cols) < 15 or cols[6] != "P":
            continue
        try:
            lat, lon, pop = float(cols[4]), float(cols[5]), int(cols[14] or 0)
        except ValueError:
            continue
        if pop < MIN_POPULATION or not (south <= lat <= north and west <= lon <= east):
            continue
        if any(distance_km(p, lat, lon) <= radius_km for p in points):
            places.append(Place(cols[1], lat, lon, pop))
    return places


def wikidata_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = (f"{v:.3f}" for v in bbox)
    return f"""SELECT DISTINCT ?name ?pop ?coord WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({west} {south})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({east} {north})"^^geo:wktLiteral .
  }}
  ?item wdt:P1082 ?pop .
  FILTER(?pop >= {MIN_POPULATION})
  ?item wdt:P31/wdt:P279* wd:Q486972 .
  MINUS {{ ?item wdt:P31/wdt:P279* wd:Q1907114 }}
  ?item rdfs:label ?name . FILTER(LANG(?name) = "en")
  FILTER(!REGEX(?name, "(urban|metropolitan|statistical|micropolitan) area|metro|agglomeration", "i"))
}}
ORDER BY DESC(?pop)
LIMIT 400"""


_WKT = re.compile(r"^Point\(\s*(-?[\d.]+(?:e-?\d+)?)\s+(-?[\d.]+(?:e-?\d+)?)\s*\)$", re.IGNORECASE)


def places_from_sparql(data: dict) -> list[Place]:
    places = []
    for b in data.get("results", {}).get("bindings", []):
        m = _WKT.match(b.get("coord", {}).get("value", ""))
        name = b.get("name", {}).get("value")
        try:
            pop = int(float(b.get("pop", {}).get("value", 0)))
        except ValueError:
            continue
        if m and name and pop > 0:
            places.append(Place(name, float(m.group(2)), float(m.group(1)), pop))
    return places


def merge_metros(places: list[Place], radius_km: float = METRO_RADIUS_KM) -> list[Place]:
    """Best-effort metro areas: fold smaller places within `radius_km` of a larger one into it.

    Greedy from the biggest place down, with no chaining (a place joins the nearest anchor that claims it, never a
    suburb of a suburb), so a long string of towns can't snowball into one giant dome. The merged place keeps the
    anchor's name, sums the populations and sits at the population-weighted centre.
    """
    cell = radius_km / 111  # degrees; a cell is at least `radius_km` tall, so neighbours are within +-1 cell in latitude
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    ordered = sorted(places, key=lambda p: -p.population)
    for i, p in enumerate(ordered):
        grid[(math.floor(p.latitude / cell), math.floor(p.longitude / cell))].append(i)
    taken = [False] * len(ordered)
    merged: list[Place] = []
    for i, anchor in enumerate(ordered):
        if taken[i]:
            continue
        taken[i] = True
        members = [anchor]
        a_obs = Observer(anchor.latitude, anchor.longitude)
        lon_reach = math.ceil(1 / max(0.2, math.cos(math.radians(anchor.latitude))))  # longitude cells narrow towards the poles
        ci, cj = math.floor(anchor.latitude / cell), math.floor(anchor.longitude / cell)
        for di in (-1, 0, 1):
            for dj in range(-lon_reach, lon_reach + 1):
                for k in grid.get((ci + di, cj + dj), ()):
                    if not taken[k] and distance_km(a_obs, ordered[k].latitude, ordered[k].longitude) <= radius_km:
                        taken[k] = True
                        members.append(ordered[k])
        if len(members) == 1:
            merged.append(anchor)
            continue
        total = sum(m.population for m in members)
        lat = sum(m.latitude * m.population for m in members) / total
        lon = sum(m.longitude * m.population for m in members) / total
        merged.append(Place(anchor.name, round(lat, 4), round(lon, 4), total))
    return merged


def domes_from_places(places: list[Place], points: list[Observer], radius_km: float) -> list[LightDome]:
    """Keep the places that matter for at least one point, strongest first (same selection as the card)."""
    unique: dict[str, LightDome] = {}
    best: dict[str, float] = {}
    for point in points:
        scored: list[tuple[float, str, LightDome]] = []
        for p in places:
            d = distance_km(point, p.latitude, p.longitude)
            if d > radius_km:
                continue
            glow = glow_index(p.population, d)
            if glow < GLOW_CUTOFF:
                continue
            dome = LightDome(p.name, round(p.latitude, 4), round(p.longitude, 4), p.population)
            scored.append((glow, f"{dome.name.lower()}|{dome.latitude:.2f}|{dome.longitude:.2f}", dome))
        scored.sort(key=lambda s: -s[0])
        for glow, key, dome in scored[:PER_POINT_LIMIT]:
            unique[key] = dome
            best[key] = max(best.get(key, 0.0), glow)
    return [unique[k] for k in sorted(unique, key=lambda k: -best[k])[:MAX_DOMES]]


# --- sources ---------------------------------------------------------------------------------


def cache_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}_{GEONAMES_MEMBER.replace('.txt', '')}.zip"))


def _read_cache(path: Path) -> bytes | None:
    try:
        if time.time() - path.stat().st_mtime < CACHE_MAX_AGE:
            return path.read_bytes()
    except OSError:
        pass
    return None


def _write_cache(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as err:
        _LOGGER.debug("Couldn't cache the GeoNames dump: %s", err)


def _filter_dump(blob: bytes, points: list[Observer], radius_km: float) -> list[Place]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z, z.open(GEONAMES_MEMBER) as f:
        # Read a margin beyond the radius: a big city just outside it must still absorb its suburbs inside it before the radius is applied.
        return parse_geonames(io.TextIOWrapper(f, encoding="utf-8"), points, radius_km + METRO_RADIUS_KM)


async def _geonames_places(hass: HomeAssistant, points: list[Observer], radius_km: float) -> list[Place]:
    path = cache_path(hass)
    blob = await hass.async_add_executor_job(_read_cache, path)
    if blob is None:
        session = async_get_clientsession(hass)
        buf = bytearray()
        async with asyncio.timeout(DOWNLOAD_TIMEOUT):
            resp = await session.get(GEONAMES_URL, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(1 << 16):
                buf += chunk
                if len(buf) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("GeoNames download is larger than expected")
        blob = bytes(buf)
        # Validate before caching so a truncated or error page can't poison the cache.
        places = await hass.async_add_executor_job(_filter_dump, blob, points, radius_km)
        await hass.async_add_executor_job(_write_cache, path, blob)
        return places
    return await hass.async_add_executor_job(_filter_dump, blob, points, radius_km)


async def _wikidata_places(hass: HomeAssistant, points: list[Observer], radius_km: float) -> list[Place]:
    session = async_get_clientsession(hass)
    params = {"format": "json", "query": wikidata_query(bounding_box(points, radius_km + METRO_RADIUS_KM))}
    async with asyncio.timeout(QUERY_TIMEOUT):
        resp = await session.get(WIKIDATA_URL, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
        resp.raise_for_status()
        data = json.loads(await resp.text())
    return places_from_sparql(data)


def _describe(name: str, err: BaseException) -> str:
    if isinstance(err, TimeoutError):
        return f"{name}: timed out"
    if isinstance(err, aiohttp.ClientResponseError):
        return f"{name}: HTTP {err.status}"
    if isinstance(err, aiohttp.ClientError):
        return f"{name}: network error"
    return f"{name}: {err}"


async def async_find_places(hass: HomeAssistant, points: list[Observer], radius_km: float) -> tuple[list[Place], LookupResult]:
    """Metro-merged places around `points`: GeoNames first, then Wikidata. Raises CityLookupError if both fail.

    The second value carries the source and counts (its `domes` is empty); `async_find_domes` fills it in.
    """
    failures: list[str] = []
    for source, fetch in (("geonames", _geonames_places), ("wikidata", _wikidata_places)):
        try:
            places = await fetch(hass, points, radius_km)
        except (aiohttp.ClientError, TimeoutError, ValueError, zipfile.BadZipFile, KeyError, OSError) as err:
            _LOGGER.debug("City lookup via %s failed: %s", source, err)
            failures.append(_describe(source, err))
            continue
        if not places:
            failures.append(f"{source}: no places found")
            continue
        metros = await hass.async_add_executor_job(merge_metros, places)
        return metros, LookupResult([], source, len(places), failures)
    raise CityLookupError(failures)


async def async_find_domes(hass: HomeAssistant, points: list[Observer], radius_km: float) -> LookupResult:
    """Look up the light domes around `points`: GeoNames first, then Wikidata. Raises CityLookupError if both fail."""
    if not points:
        return LookupResult([], "none", 0)
    metros, found = await async_find_places(hass, points, radius_km)
    found.domes = await hass.async_add_executor_job(domes_from_places, metros, points, radius_km)
    return found


def lookup_points(home: Observer, sites: list[Observer]) -> list[Observer]:
    """Home plus each located site (the card does the same), de-duplicated."""
    seen: dict[tuple[float, float], Observer] = {}
    for o in [home, *sites]:
        seen.setdefault((round(o.latitude, 3), round(o.longitude, 3)), o)
    return list(seen.values())
