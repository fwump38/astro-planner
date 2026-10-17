"""Pure astronomy for sky planning (no Home Assistant imports).

A faithful port of the Astro Planner card's TypeScript (`src/astro`), so the card and
this integration agree to the minute. Built on astronomy-engine.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import astronomy as ae

_J2000_TS = 946728000.0  # 2000-01-01T12:00:00Z
_GAL_EQJ = ae.Rotation_GAL_EQJ()
_CORE = ae.Spherical(0, 0, 1)
_BAND_A = ae.Spherical(0, 5, 1)
_BAND_B = ae.Spherical(0, -5, 1)
_BORTLE_NELM = [7.8, 7.8, 7.3, 6.8, 6.3, 5.8, 5.3, 4.8, 4.3, 4.0]
_EARTH_RADIUS_KM = 6371.0
_MOON_UP_ALT = -0.5


def to_time(dt: datetime) -> ae.Time:
    return ae.Time((dt.timestamp() - _J2000_TS) / 86400.0)


def to_datetime(t: ae.Time) -> datetime:
    return datetime.fromtimestamp(t.ut * 86400.0 + _J2000_TS, tz=UTC)


@dataclass(frozen=True)
class Observer:
    latitude: float
    longitude: float
    elevation: float = 0.0

    def ae(self) -> ae.Observer:
        return ae.Observer(self.latitude, self.longitude, self.elevation)


@dataclass(frozen=True)
class LightDome:
    name: str
    latitude: float
    longitude: float
    population: float


@dataclass(frozen=True)
class DomeGeometry:
    dome: LightDome
    distance_km: float
    bearing: float
    glow: float


@dataclass(frozen=True)
class SkyOptions:
    min_gc_altitude: float = 10.0
    max_moon_illumination: float = 10.0
    bortle: float = 3
    domes: tuple[DomeGeometry, ...] = ()
    dome_sigma: float = 25.0


@dataclass(frozen=True)
class GalacticCenter:
    altitude: float
    azimuth: float
    tilt: float

    @property
    def orientation(self) -> str:
        return classify_orientation(self.tilt)


@dataclass
class SkySample:
    time: datetime
    sun_altitude: float
    moon_altitude: float
    moon_illumination: float  # 0-1
    gc: GalacticCenter
    dark: bool
    gc_visible: bool
    limiting_magnitude: float
    dome_penalty: float
    worst_dome: DomeGeometry | None


@dataclass
class NightSummary:
    date: date
    dusk: datetime | None
    dawn: datetime | None
    samples: list[SkySample]
    step_minutes: int
    moon_phase: float  # degrees, 0 = new, 180 = full
    moon_illumination: float  # 0-1 at local midnight
    moon_rise: datetime | None
    moon_set: datetime | None
    dark_minutes: int
    gc_minutes: int
    gc_start: datetime | None
    gc_end: datetime | None
    gc_peak_altitude: float | None
    gc_tilt: float | None
    orientation: str | None
    gc_azimuth_start: float | None
    gc_azimuth_end: float | None
    dome_penalty: float
    worst_dome: DomeGeometry | None = None
    visible: list[SkySample] = field(default_factory=list)
    astro_minutes: int = 0  # dusk to dawn (sun below -18°), whatever the moon is doing
    ambient_dome_penalty: float = 0.0  # 0-1 average glow of the light domes across the whole sky, not just toward the core

    def grade(self, best_minutes: float) -> str:
        return classify_night(self.gc_minutes, best_minutes)


# --- galactic center -------------------------------------------------------------------------


def galactic_center_position(t: ae.Time, observer: ae.Observer) -> GalacticCenter:
    rot = ae.CombineRotation(_GAL_EQJ, ae.Rotation_EQJ_HOR(t, observer))

    def to_hor(s: ae.Spherical) -> ae.Spherical:
        return ae.HorizonFromVector(ae.RotateVector(rot, ae.VectorFromSphere(s, t)), ae.Refraction.Normal)

    core, a, b = to_hor(_CORE), to_hor(_BAND_A), to_hor(_BAND_B)
    return GalacticCenter(core.lat, core.lon, _band_tilt(a, b, core.lat))


def _band_tilt(a: ae.Spherical, b: ae.Spherical, core_alt: float) -> float:
    d_az = b.lon - a.lon
    if d_az > 180:
        d_az -= 360
    if d_az < -180:
        d_az += 360
    dx = d_az * math.cos(math.radians(core_alt))
    dy = b.lat - a.lat
    return math.degrees(math.atan2(abs(dy), abs(dx)))


def classify_orientation(tilt: float) -> str:
    if tilt < 30:
        return "arch"
    if tilt < 60:
        return "angled"
    return "upright"


# --- darkness and light domes ----------------------------------------------------------------


def limiting_magnitude(sun_alt: float, moon_alt: float, moon_illum: float, bortle: float) -> float:
    base = _BORTLE_NELM[int(min(9, max(1, _js_round(bortle))))]
    if sun_alt >= -6:
        lm = 0.0
    elif sun_alt >= -18:
        lm = 1.5 + ((-6 - sun_alt) / 12) * (base - 1.5)
    else:
        lm = base
    if moon_alt > 0:
        penalty = 3.2 * moon_illum**1.5 * math.sqrt(math.sin(math.radians(moon_alt)))
        lm = min(lm, base - penalty)
    return max(0.0, lm)


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def distance_km(a: Observer, lat: float, lon: float) -> float:
    d_lat = math.radians(lat - a.latitude)
    d_lon = math.radians(lon - a.longitude)
    h = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(a.latitude)) * math.cos(math.radians(lat)) * math.sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def bearing(a: Observer, lat: float, lon: float) -> float:
    p1, p2 = math.radians(a.latitude), math.radians(lat)
    dl = math.radians(lon - a.longitude)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def glow_index(population: float, km: float) -> float:
    return (population / 1e5) * (max(km, 5) / 30) ** -2.5


def dome_geometry(site: Observer, domes: Sequence[LightDome]) -> tuple[DomeGeometry, ...]:
    out = []
    for d in domes:
        km = distance_km(site, d.latitude, d.longitude)
        out.append(DomeGeometry(d, km, bearing(site, d.latitude, d.longitude), glow_index(d.population, km)))
    return tuple(out)


def angular_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def dome_impact(geoms: Sequence[DomeGeometry], azimuth: float, altitude: float, sigma: float = 25.0) -> tuple[float, DomeGeometry | None]:
    if altitude < 0 or not geoms:
        return 0.0, None
    alt_factor = math.exp(-altitude / 25)
    total = 0.0
    worst: DomeGeometry | None = None
    worst_c = 0.0
    for g in geoms:
        d_az = angular_diff(azimuth, g.bearing)
        c = g.glow * math.exp(-(d_az * d_az) / (2 * sigma * sigma)) * alt_factor
        total += c
        if c > worst_c:
            worst_c, worst = c, g
    return 1 - math.exp(-total / 0.15), worst


def ambient_dome_penalty(geoms: Sequence[DomeGeometry], sigma: float = 25.0, altitude: float = 35.0) -> float:
    """Average city glow over eight compass directions at a typical imaging altitude (for targets that aren't the core)."""
    if not geoms:
        return 0.0
    return sum(dome_impact(geoms, az, altitude, sigma)[0] for az in range(0, 360, 45)) / 8


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_point(deg: float) -> str:
    return COMPASS[_js_round(deg / 22.5) % 16]


# --- sky sampling ----------------------------------------------------------------------------


def body_altitude(body: ae.Body, t: ae.Time, obs: ae.Observer) -> float:
    eq = ae.Equator(body, t, obs, True, True)
    return ae.Horizon(t, obs, eq.ra, eq.dec, ae.Refraction.Normal).altitude


def sample_sky(dt: datetime, observer: Observer, opts: SkyOptions, obs: ae.Observer | None = None) -> SkySample:
    obs = obs or observer.ae()
    t = to_time(dt)
    sun_alt = body_altitude(ae.Body.Sun, t, obs)
    moon_alt = body_altitude(ae.Body.Moon, t, obs)
    moon_ill = ae.Illumination(ae.Body.Moon, t).phase_fraction
    gc = galactic_center_position(t, obs)
    moon_ok = moon_alt < -0.5 or moon_ill * 100 <= opts.max_moon_illumination
    dark = sun_alt < -18 and moon_ok
    penalty, worst = dome_impact(opts.domes, gc.azimuth, gc.altitude, opts.dome_sigma)
    return SkySample(
        dt, sun_alt, moon_alt, moon_ill, gc, dark, dark and gc.altitude >= opts.min_gc_altitude,
        limiting_magnitude(sun_alt, moon_alt, moon_ill, opts.bortle), penalty, worst,
    )


def _whole_second(dt: datetime | None) -> datetime | None:
    """Presentation rounding, so published times don't carry sub-second noise (sampling itself stays exact)."""
    return None if dt is None else (dt + timedelta(milliseconds=500)).replace(microsecond=0)


def solar_noon(d: date, longitude: float) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=UTC) - timedelta(hours=longitude / 15)


def night_bounds(d: date, observer: Observer) -> tuple[datetime | None, datetime | None, datetime]:
    """Astronomical dusk and dawn (sun at -18°) for the night beginning on `d`, plus solar midnight; None when the sun never gets that low."""
    obs = observer.ae()
    noon = solar_noon(d, observer.longitude)
    dusk_t = ae.SearchAltitude(ae.Body.Sun, obs, ae.Direction.Set, to_time(noon), 1, -18)
    dawn_t = ae.SearchAltitude(ae.Body.Sun, obs, ae.Direction.Rise, dusk_t, 1, -18) if dusk_t else None
    return (to_datetime(dusk_t) if dusk_t else None, to_datetime(dawn_t) if dawn_t else None, noon + timedelta(hours=12))


def compute_night(d: date, observer: Observer, opts: SkyOptions, step_minutes: int = 10, keep_samples: bool = True) -> NightSummary:
    """Summarise the night that begins on the evening of `d` at `observer`."""
    obs = observer.ae()
    dusk, dawn, midnight = night_bounds(d, observer)

    samples: list[SkySample] = []
    if dusk and dawn:
        step = timedelta(minutes=step_minutes)
        t = dusk
        while t <= dawn:
            samples.append(sample_sky(t, observer, opts, obs))
            t += step

    visible = [s for s in samples if s.gc_visible]
    first = visible[0] if visible else None
    last = visible[-1] if visible else None
    peak = max(visible, key=lambda s: s.gc.altitude, default=None)
    mid = visible[len(visible) // 2] if visible else None

    penalty_sum = 0.0
    worst_weight: dict[int, tuple[DomeGeometry, float]] = {}
    for s in visible:
        penalty_sum += s.dome_penalty
        if s.worst_dome is not None:
            g, w = worst_weight.get(id(s.worst_dome), (s.worst_dome, 0.0))
            worst_weight[id(s.worst_dome)] = (g, w + s.dome_penalty)
    dome_penalty = penalty_sum / len(visible) if visible else 0.0
    ambient = ambient_dome_penalty(opts.domes, opts.dome_sigma)
    worst: DomeGeometry | None = None
    worst_score = 0.0
    for g, w in worst_weight.values():
        if w > worst_score:
            worst, worst_score = g, w

    moon_rise = moon_set = None
    for prev, cur in pairwise(samples):
        was, now = prev.moon_altitude > _MOON_UP_ALT, cur.moon_altitude > _MOON_UP_ALT
        if not was and now and moon_rise is None:
            moon_rise = cur.time
        if was and not now and moon_set is None:
            moon_set = cur.time

    mt = to_time(midnight)
    return NightSummary(
        date=d,
        dusk=_whole_second(dusk),
        dawn=_whole_second(dawn),
        samples=samples if keep_samples else [],
        step_minutes=step_minutes,
        moon_phase=ae.MoonPhase(mt),
        moon_illumination=ae.Illumination(ae.Body.Moon, mt).phase_fraction,
        moon_rise=_whole_second(moon_rise),
        moon_set=_whole_second(moon_set),
        dark_minutes=sum(1 for s in samples if s.dark) * step_minutes,
        gc_minutes=len(visible) * step_minutes,
        gc_start=_whole_second(first.time) if first else None,
        gc_end=_whole_second(last.time + timedelta(minutes=step_minutes)) if last else None,
        gc_peak_altitude=peak.gc.altitude if peak else None,
        gc_tilt=mid.gc.tilt if mid else None,
        orientation=classify_orientation(mid.gc.tilt) if mid else None,
        gc_azimuth_start=first.gc.azimuth if first else None,
        gc_azimuth_end=last.gc.azimuth if last else None,
        dome_penalty=dome_penalty,
        worst_dome=worst if dome_penalty > 0.02 else None,
        visible=visible,
        astro_minutes=round((dawn - dusk).total_seconds() / 60) if dusk and dawn else 0,
        ambient_dome_penalty=ambient,
    )


def classify_night(gc_minutes: float, best_minutes: float) -> str:
    """Capture the Atlas-style grade: "best" (pink), "short" (blue) or "none"."""
    if gc_minutes <= 0:
        return "none"
    return "best" if gc_minutes >= best_minutes else "short"


# --- live sky / event times ------------------------------------------------------------------


def moon_state(dt: datetime, observer: Observer) -> tuple[float, float, float]:
    """(altitude°, illumination 0-1, phase angle°) of the moon."""
    t = to_time(dt)
    obs = observer.ae()
    return body_altitude(ae.Body.Moon, t, obs), ae.Illumination(ae.Body.Moon, t).phase_fraction, ae.MoonPhase(t)


def next_crossing(
    altitude_at, start: datetime, rising: bool, threshold: float = 0.0, horizon_hours: float = 40, step_minutes: int = 10
) -> datetime | None:
    """First time after `start` that `altitude_at(dt)` crosses `threshold` upward (rising) or downward."""
    step = timedelta(minutes=step_minutes)
    prev_t, prev_alt = start, altitude_at(start)
    end = start + timedelta(hours=horizon_hours)
    t = start
    while t < end:
        t += step
        alt = altitude_at(t)
        crossed = (prev_alt < threshold <= alt) if rising else (prev_alt >= threshold > alt)
        if crossed:
            lo, hi = prev_t, t
            for _ in range(10):  # bisect to ~1 second
                mid = lo + (hi - lo) / 2
                above = altitude_at(mid) >= threshold
                if above == rising:
                    hi = mid
                else:
                    lo = mid
            return (lo + (hi - lo) / 2).replace(microsecond=0)
        prev_t, prev_alt = t, alt
    return None
