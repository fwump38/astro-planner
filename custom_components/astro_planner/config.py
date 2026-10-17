"""Parsed configuration and site resolution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .astro import LightDome, Observer, SkyOptions, dome_geometry
from .const import (
    CONF_AUTO_DRIVE,
    CONF_BORTLE,
    CONF_CAL_BEST_MINUTES,
    CONF_CAL_MIN_ALTITUDE,
    CONF_CUSTOM_TARGETS,
    CONF_DOME_SIGMA,
    CONF_DOME_WARNING,
    CONF_DRIVE_MINUTES,
    CONF_ENTITY,
    CONF_FOV,
    CONF_GOOD_SCORE,
    CONF_LATITUDE,
    CONF_LIGHT_DOMES,
    CONF_LONGITUDE,
    CONF_MAX_MOON,
    CONF_MIN_GC_ALTITUDE,
    CONF_NAME,
    CONF_NIGHTS,
    CONF_SITES,
    CONF_TARGET_MAX_MAGNITUDE,
    CONF_TARGET_MIN_ALTITUDE,
    CONF_WATCH_TARGETS,
    CONF_WEIGHTS,
    DEFAULT_BORTLE,
    DEFAULTS,
)
from .scoring import DEFAULT_WEIGHTS
from .targets import Target, all_targets


@dataclass
class Site:
    entity: str
    name: str
    bortle: float
    drive_minutes: int | None
    drive_estimated: bool
    observer: Observer | None
    # Where each input came from, so sensors can say whether a value was read from AstroWeather, configured, or assumed.
    coordinates_source: str | None = None  # configured | astroweather
    bortle_source: str = "default"  # configured | default
    drive_source: str | None = None  # configured | route | estimate

    @property
    def key(self) -> str:
        return self.entity


@dataclass
class PlannerConfig:
    home: Observer
    sites: list[Site]
    min_gc_altitude: float
    max_moon_illumination: float
    calendar_min_altitude: float
    calendar_best_minutes: float
    weights: dict[str, float]
    domes: list[LightDome]
    dome_sigma: float
    dome_warning_threshold: float
    nights: int
    good_score_threshold: float
    auto_drive_time: bool
    manual_drive: dict[str, int] = field(default_factory=dict)
    target_min_altitude: float = 30.0
    target_max_magnitude: float = 12.0
    fov_arcmin: float = 0.0  # field of view of the user's camera and scope (smaller side); 0 = don't filter by framing
    watch_targets: tuple[str, ...] = ()  # target ids that get their own good/dropped events
    targets: tuple[Target, ...] = ()  # every target the planner knows about (Messier, planets and the user's custom ones)

    def target(self, target_id: str) -> Target | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def plannable_targets(self) -> list[Target]:
        """Targets worth ranking: bright enough, and (with a field of view set) not too big to frame."""
        return [
            t
            for t in self.targets
            if (t.magnitude is None or t.magnitude <= self.target_max_magnitude) and t.framing(self.fov_arcmin) != "too_large"
        ]

    @property
    def located_sites(self) -> list[Site]:
        return [s for s in self.sites if s.observer]

    @property
    def reference(self) -> Observer:
        """The calendar's reference point: the centre of the sites (or home when there are none)."""
        located = self.located_sites
        if not located:
            return self.home
        n = len(located)
        return Observer(
            sum(s.observer.latitude for s in located) / n,
            sum(s.observer.longitude for s in located) / n,
            sum(s.observer.elevation for s in located) / n,
        )

    def calendar_options(self) -> SkyOptions:
        return SkyOptions(self.calendar_min_altitude, self.max_moon_illumination, 2, (), self.dome_sigma)

    def site_options(self, site: Site) -> SkyOptions:
        geoms = dome_geometry(site.observer, self.domes) if site.observer else ()
        return SkyOptions(self.min_gc_altitude, self.max_moon_illumination, site.bortle, geoms, self.dome_sigma)

    def signature(self) -> tuple:
        """Anything that changes computed nights; used to key caches."""
        return (
            self.min_gc_altitude, self.max_moon_illumination, self.calendar_min_altitude, self.dome_sigma,
            tuple(self.domes), tuple((s.entity, s.bortle, s.observer) for s in self.sites), self.home,
            self.target_min_altitude, self.target_max_magnitude, self.fov_arcmin, self.targets,
        )


def _sibling_observer(hass: HomeAssistant, entity_id: str) -> Observer | None:
    """AstroWeather exposes its coordinates as sibling `*_latitude`/`*_longitude`/`*_elevation` sensors on the same device."""
    reg = er.async_get(hass)
    entry = reg.async_get(entity_id)
    siblings = [e.entity_id for e in er.async_entries_for_device(reg, entry.device_id)] if entry and entry.device_id else []
    stem = entity_id.split(".", 1)[1]

    def find(suffix: str) -> float | None:
        candidates = [e for e in siblings if e.startswith("sensor.") and e.endswith(f"_{suffix}")] or [f"sensor.{stem}_{suffix}"]
        for cand in candidates:
            st = hass.states.get(cand)
            try:
                return float(st.state) if st else None
            except ValueError:
                continue
        return None

    lat, lon, elev = find("latitude"), find("longitude"), find("elevation")
    if lat is None or lon is None:
        return None
    return Observer(lat, lon, elev or 0.0)


def site_display_name(hass: HomeAssistant, entity_id: str, name: str | None) -> str:
    if name:
        return name
    st = hass.states.get(entity_id)
    if st:
        if loc := st.attributes.get("location_name"):
            return str(loc)
        friendly = st.attributes.get("friendly_name")
        if friendly:
            return str(friendly).removeprefix("AstroWeather ").removeprefix("Astroweather ").strip() or str(friendly)
    return entity_id.split(".", 1)[1].removeprefix("astroweather_").replace("_", " ")


def build_config(
    hass: HomeAssistant,
    options: dict[str, Any],
    drive_lookup: Callable[[Observer, Observer], int | None] | None = None,
    drive_lookup_source: Callable[[Observer, Observer], str | None] | None = None,
    bortle_lookup: Callable[[Observer], int | None] | None = None,
) -> PlannerConfig:
    o = {**DEFAULTS, **options}
    home = Observer(hass.config.latitude, hass.config.longitude, float(hass.config.elevation or 0))
    sites: list[Site] = []
    manual: dict[str, int] = {}
    for raw in o.get(CONF_SITES, []):
        entity = raw[CONF_ENTITY]
        if raw.get(CONF_LATITUDE) is not None and raw.get(CONF_LONGITUDE) is not None:
            observer: Observer | None = Observer(raw[CONF_LATITUDE], raw[CONF_LONGITUDE])
            coords_source: str | None = "configured"
        else:
            observer = _sibling_observer(hass, entity)
            coords_source = "astroweather" if observer else None
        drive = raw.get(CONF_DRIVE_MINUTES)
        estimated = False
        drive_source = None
        if drive:
            manual[entity] = int(drive)
            drive_source = "configured"
        elif o[CONF_AUTO_DRIVE] and drive_lookup and observer and (cached := drive_lookup(home, observer)):
            drive, estimated = cached, True
            drive_source = drive_lookup_source(home, observer) if drive_lookup_source else "estimate"
        if raw.get(CONF_BORTLE) is not None:
            bortle, bortle_source = float(raw[CONF_BORTLE]), "configured"
        elif bortle_lookup and observer and (est := bortle_lookup(observer)):
            bortle, bortle_source = float(est), "estimated"
        else:
            bortle, bortle_source = float(DEFAULT_BORTLE), "default"
        sites.append(
            Site(
                entity, site_display_name(hass, entity, raw.get(CONF_NAME)), bortle, drive or None, estimated, observer,
                coords_source, bortle_source, drive_source,
            )
        )
    return PlannerConfig(
        home=home,
        sites=sites,
        min_gc_altitude=float(o[CONF_MIN_GC_ALTITUDE]),
        max_moon_illumination=float(o[CONF_MAX_MOON]),
        calendar_min_altitude=float(o[CONF_CAL_MIN_ALTITUDE]),
        calendar_best_minutes=float(o[CONF_CAL_BEST_MINUTES]),
        weights={**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (o.get(CONF_WEIGHTS) or {}).items()}},
        domes=[LightDome(str(d["name"]), float(d["latitude"]), float(d["longitude"]), float(d.get("population", 0))) for d in o.get(CONF_LIGHT_DOMES, [])],
        dome_sigma=float(o[CONF_DOME_SIGMA]),
        dome_warning_threshold=float(o[CONF_DOME_WARNING]),
        nights=max(1, min(7, int(o[CONF_NIGHTS]))),
        good_score_threshold=float(o[CONF_GOOD_SCORE]),
        auto_drive_time=bool(o[CONF_AUTO_DRIVE]),
        manual_drive=manual,
        target_min_altitude=float(o[CONF_TARGET_MIN_ALTITUDE]),
        target_max_magnitude=float(o[CONF_TARGET_MAX_MAGNITUDE]),
        fov_arcmin=float(o[CONF_FOV]),
        watch_targets=tuple(o.get(CONF_WATCH_TARGETS) or ()),
        targets=tuple(all_targets(o.get(CONF_CUSTOM_TARGETS))),
    )
