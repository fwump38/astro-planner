"""Observing targets (Messier objects, planets, custom RA/Dec entries) and when they are well placed.

Pure Python, no Home Assistant imports. Fixed objects use J2000 coordinates and ignore precession (under half a
degree), which is far below what matters for "is it up tonight".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import astronomy as ae

from .astro import (
    _MOON_UP_ALT,
    DomeGeometry,
    NightSummary,
    Observer,
    compass_point,
    dome_impact,
    night_bounds,
    to_time,
)
from .messier_data import MESSIER

MILKY_WAY_ID = "milky_way"

KIND_LABELS = {
    "milky_way": "Milky Way",
    "galaxy": "Galaxy",
    "globular_cluster": "Globular cluster",
    "open_cluster": "Open cluster",
    "planetary_nebula": "Planetary nebula",
    "nebula": "Nebula",
    "supernova_remnant": "Supernova remnant",
    "star_cloud": "Star cloud",
    "double_star": "Double star",
    "asterism": "Asterism",
    "planet": "Planet",
    "custom": "Custom",
}

CONSTELLATIONS = {
    "And": "Andromeda", "Aqr": "Aquarius", "Aur": "Auriga", "CMa": "Canis Major", "CVn": "Canes Venatici", "Cnc": "Cancer",
    "Cap": "Capricornus", "Cas": "Cassiopeia", "Cet": "Cetus", "Com": "Coma Berenices", "Cyg": "Cygnus", "Dra": "Draco",
    "Gem": "Gemini", "Her": "Hercules", "Hya": "Hydra", "Leo": "Leo", "Lep": "Lepus", "Lyr": "Lyra", "Mon": "Monoceros",
    "Oph": "Ophiuchus", "Ori": "Orion", "Peg": "Pegasus", "Per": "Perseus", "Psc": "Pisces", "Pup": "Puppis", "Sge": "Sagitta",
    "Sgr": "Sagittarius", "Sco": "Scorpius", "Sct": "Scutum", "Ser": "Serpens", "Tau": "Taurus", "Tri": "Triangulum",
    "UMa": "Ursa Major", "Vir": "Virgo", "Vul": "Vulpecula",
}

# How much a target is hurt by light pollution: planets barely, clusters somewhat, faint galaxies and nebulae fully.
LIGHT_SENSITIVITY = {
    "planet": 0.15, "double_star": 0.15, "open_cluster": 0.5, "globular_cluster": 0.6, "asterism": 0.5, "star_cloud": 0.9,
}

_PLANETS = (
    ("mercury", "Mercury", ae.Body.Mercury),
    ("venus", "Venus", ae.Body.Venus),
    ("mars", "Mars", ae.Body.Mars),
    ("jupiter", "Jupiter", ae.Body.Jupiter),
    ("saturn", "Saturn", ae.Body.Saturn),
    ("uranus", "Uranus", ae.Body.Uranus),
    ("neptune", "Neptune", ae.Body.Neptune),
)
_PLANET_MAG = {"mercury": 0.0, "venus": -4.0, "mars": 0.5, "jupiter": -2.3, "saturn": 0.8, "uranus": 5.7, "neptune": 7.8}


@dataclass(frozen=True)
class Target:
    id: str
    designation: str  # "M31", "Jupiter", "NGC 7000"
    name: str  # "Andromeda Galaxy"; a descriptive fallback when the object has no common name
    kind: str
    ra: float | None = None  # hours, J2000 (None for planets)
    dec: float | None = None  # degrees
    magnitude: float | None = None
    size: float | None = None  # arcmin, major axis
    constellation: str | None = None
    body: ae.Body | None = field(default=None, compare=False, hash=False)

    @property
    def label(self) -> str:
        """"M1 – Crab Nebula" (or just the designation when it has no separate name)."""
        return self.designation if self.name == self.designation else f"{self.designation} – {self.name}"

    @property
    def sensitivity(self) -> float:
        return LIGHT_SENSITIVITY.get(self.kind, 1.0)

    def best_date(self, year: int) -> date | None:
        """Roughly when the object crosses the meridian at local midnight (its best time of year), within a few days."""
        if self.ra is None:
            return None
        doy = (self.ra - 12.0) % 24.0 / 24.0 * 365.2422 + 79.7  # the sun's RA is `ra - 12h` on that day
        return date(year, 1, 1) + timedelta(days=round(doy) % 365)

    def framing(self, fov_arcmin: float) -> str | None:
        """How the object fits a field of view: "fits", "too_large" or "too_small" (None when no field of view is set)."""
        if fov_arcmin <= 0 or not self.size:
            return None
        if self.size > fov_arcmin:
            return "too_large"
        return "too_small" if self.size < fov_arcmin * 0.08 else "fits"


def messier_targets() -> list[Target]:
    out = []
    for num, name, kind, ra, dec, mag, size, con in MESSIER:
        fallback = f"{KIND_LABELS[kind]} in {CONSTELLATIONS.get(con, con)}"
        out.append(Target(f"m{num}", f"M{num}", name or fallback, kind, ra, dec, mag, size, con))
    return out


def planet_targets() -> list[Target]:
    return [Target(pid, name, name, "planet", None, None, _PLANET_MAG[pid], None, None, body) for pid, name, body in _PLANETS]


def parse_ra(value: Any) -> float | None:
    """RA as decimal hours or "HH:MM:SS" / "HHhMMmSSs"."""
    return _parse_sexagesimal(value, 24.0)


def parse_dec(value: Any) -> float | None:
    return _parse_sexagesimal(value, 90.0)


def _parse_sexagesimal(value: Any, limit: float) -> float | None:
    try:
        if isinstance(value, (int, float)):
            out = float(value)
        else:
            text = str(value).strip().lower().replace("h", ":").replace("d", ":").replace("°", ":").replace("m", ":").replace("s", "").replace("'", ":").replace('"', "")
            sign = -1.0 if text.startswith("-") else 1.0
            parts = [float(p) for p in text.lstrip("+-").split(":") if p.strip()]
            if not parts:
                return None
            out = sign * sum(p / 60**i for i, p in enumerate(parts))
    except ValueError:
        return None
    return out if abs(out) <= limit and not (limit == 24.0 and out < 0) else None


def custom_targets(raw: list[dict[str, Any]] | None) -> list[Target]:
    """Targets from the options' custom list (`name`, `ra`, `dec`, optional `magnitude`/`size`); invalid rows are skipped."""
    out: list[Target] = []
    for i, row in enumerate(raw or []):
        ra, dec = parse_ra(row.get("ra")), parse_dec(row.get("dec"))
        name = str(row.get("name") or "").strip()
        if ra is None or dec is None or not name:
            continue
        mag = row.get("magnitude")
        size = row.get("size")
        out.append(
            Target(
                f"custom_{i + 1}", name, name, "custom", ra, dec,
                float(mag) if mag not in (None, "") else None, float(size) if size not in (None, "") else None,
            )
        )
    return out


def all_targets(custom: list[dict[str, Any]] | None = None) -> list[Target]:
    return [*messier_targets(), *planet_targets(), *custom_targets(custom)]


# --- dark nights ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    time: datetime
    lst: float  # local sidereal time, degrees
    moon_altitude: float
    moon_ra: float  # degrees
    moon_dec: float


@dataclass
class DarkNight:
    """The astronomically dark part of the night that begins on `date`, sampled every `step_minutes`."""

    date: date
    observer: Observer
    dusk: datetime | None
    dawn: datetime | None
    step_minutes: int
    slots: list[Slot]
    moon_illumination: float  # 0-1 at local midnight
    moon_free_minutes: int  # dark minutes with the moon down or dim (under `max_moon_illumination`)
    planets: dict[str, tuple[float, float]]  # planet id -> (RA degrees, Dec degrees) around midnight
    moon_free_window: tuple[datetime, datetime] | None = None  # the longest unbroken moon-free stretch of the dark

    @property
    def astro_minutes(self) -> int:
        return len(self.slots) * self.step_minutes


def compute_dark_night(d: date, observer: Observer, max_moon_illumination: float = 10.0, step_minutes: int = 10) -> DarkNight:
    obs = observer.ae()
    dusk, dawn, midnight = night_bounds(d, observer)
    slots: list[Slot] = []
    if dusk and dawn:
        t = dusk
        step = timedelta(minutes=step_minutes)
        while t <= dawn:
            at = to_time(t)
            moon = ae.Equator(ae.Body.Moon, at, obs, True, True)
            lst = (ae.SiderealTime(at) * 15.0 + observer.longitude) % 360.0
            moon_alt = _altitude(lst, moon.ra * 15.0, moon.dec, observer.latitude)
            slots.append(Slot(t, lst, moon_alt, moon.ra * 15.0, moon.dec))
            t += step
    mt = to_time(midnight)
    illum = ae.Illumination(ae.Body.Moon, mt).phase_fraction
    moon_ok_always = illum * 100 <= max_moon_illumination
    free = sum(1 for s in slots if moon_ok_always or s.moon_altitude < _MOON_UP_ALT) * step_minutes
    planets = {}
    for pid, _name, body in _PLANETS:
        eq = ae.Equator(body, mt, obs, True, True)
        planets[pid] = (eq.ra * 15.0, eq.dec)
    return DarkNight(d, observer, dusk, dawn, step_minutes, slots, illum, free, planets, _longest_moon_free(slots, moon_ok_always, step_minutes))


def _longest_moon_free(slots: list[Slot], moon_dim: bool, step_minutes: int) -> tuple[datetime, datetime] | None:
    best: tuple[int, int] | None = None  # first and last slot of the longest run
    run_start = None
    for i, slot in enumerate([*slots, None]):
        free = slot is not None and (moon_dim or slot.moon_altitude < _MOON_UP_ALT)
        if free and run_start is None:
            run_start = i
        elif not free and run_start is not None:
            if best is None or i - run_start > best[1] - best[0] + 1:
                best = (run_start, i - 1)
            run_start = None
    if best is None:
        return None
    return slots[best[0]].time, slots[best[1]].time + timedelta(minutes=step_minutes)


# --- a target's night -------------------------------------------------------------------------


@dataclass
class TargetNight:
    target: Target
    date: date
    minutes: int  # dark minutes spent above the minimum altitude
    peak_altitude: float
    peak_time: datetime | None
    peak_azimuth: float | None
    start: datetime | None  # first and last observable moment
    end: datetime | None
    moon_penalty: float  # 0-1, how much the moon spoils the observable part of the night
    moon_separation: float | None  # degrees from the moon at the peak
    dome_penalty: float  # 0-1, city glow toward the target at its peak
    times: tuple[datetime, ...] = ()  # the observable slots, for weighting the weather forecast

    @property
    def observable(self) -> bool:
        return self.minutes > 0

    @property
    def direction(self) -> str | None:
        return compass_point(self.peak_azimuth) if self.peak_azimuth is not None else None


def _altitude(lst_deg: float, ra_deg: float, dec_deg: float, lat_deg: float) -> float:
    lat, dec = math.radians(lat_deg), math.radians(dec_deg)
    h = math.radians(lst_deg - ra_deg)
    return math.degrees(math.asin(max(-1.0, min(1.0, math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(h)))))


def _azimuth(lst_deg: float, ra_deg: float, dec_deg: float, lat_deg: float) -> float:
    lat, dec = math.radians(lat_deg), math.radians(dec_deg)
    h = math.radians(lst_deg - ra_deg)
    a = math.atan2(math.sin(h), math.cos(h) * math.sin(lat) - math.tan(dec) * math.cos(lat))
    return (math.degrees(a) + 180.0) % 360.0


def _separation(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    d1, d2 = math.radians(dec1), math.radians(dec2)
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(math.radians(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def moon_penalty_at(altitude: float, illumination: float, separation: float) -> float:
    """0-1 damage the moon does to a target: nothing when it's down, worst when it's bright and close."""
    if altitude < _MOON_UP_ALT:
        return 0.0
    return illumination * (1.0 - 0.6 * min(separation, 120.0) / 120.0)


def target_night(
    target: Target,
    night: DarkNight,
    min_altitude: float,
    domes: tuple[DomeGeometry, ...] = (),
    dome_sigma: float = 25.0,
) -> TargetNight:
    if target.body is not None:
        ra, dec = night.planets[target.id]
    else:
        ra, dec = (target.ra or 0.0) * 15.0, target.dec or 0.0
    lat = night.observer.latitude
    empty = TargetNight(target, night.date, 0, -90.0, None, None, None, None, 0.0, None, 0.0)
    if 90.0 - abs(lat - dec) < min_altitude:  # never gets high enough from here
        empty.peak_altitude = 90.0 - abs(lat - dec)
        return empty
    above: list[tuple[Slot, float]] = []
    best_alt, best_slot = -90.0, None
    for s in night.slots:
        alt = _altitude(s.lst, ra, dec, lat)
        if alt > best_alt:
            best_alt, best_slot = alt, s
        if alt >= min_altitude:
            above.append((s, alt))
    if not above or best_slot is None:
        empty.peak_altitude = best_alt
        return empty
    illum = night.moon_illumination
    penalties = [moon_penalty_at(s.moon_altitude, illum, _separation(ra, dec, s.moon_ra, s.moon_dec)) for s, _ in above]
    peak_slot, peak_alt = max(above, key=lambda a: a[1])
    az = _azimuth(peak_slot.lst, ra, dec, lat)
    dome = dome_impact(domes, az, peak_alt, dome_sigma)[0] if domes else 0.0
    return TargetNight(
        target, night.date, len(above) * night.step_minutes, peak_alt, peak_slot.time, az,
        above[0][0].time, above[-1][0].time + timedelta(minutes=night.step_minutes),
        sum(penalties) / len(penalties), _separation(ra, dec, peak_slot.moon_ra, peak_slot.moon_dec), dome,
        tuple(s.time for s, _ in above),
    )


def target_nights(
    targets: list[Target],
    night: DarkNight,
    min_altitude: float,
    domes: tuple[DomeGeometry, ...] = (),
    dome_sigma: float = 25.0,
) -> list[TargetNight]:
    return [target_night(t, night, min_altitude, domes, dome_sigma) for t in targets]



# --- the Milky Way core, as a target ----------------------------------------------------------

MILKY_WAY = Target(MILKY_WAY_ID, "Milky Way", "Galactic core", "milky_way", 17.7611, -29.0078, None, None, "Sgr")


def milky_way_night(n: NightSummary) -> TargetNight:
    """The core's night as a TargetNight, from the Milky Way calculations (which already account for the moon and city glow)."""
    return TargetNight(
        MILKY_WAY, n.date, n.gc_minutes, n.gc_peak_altitude if n.gc_peak_altitude is not None else -90.0, None,
        n.gc_azimuth_start, n.gc_start, n.gc_end, 0.0, None, n.dome_penalty, tuple(s.time for s in n.visible),
    )
