"""Annual meteor showers: when they peak, how high the radiant gets and how much the moon interferes.

Peak dates are the usual International Meteor Organization values; they move by about a day from year to year
(and the Draconids and Taurids vary in strength), so treat them as "the night to go out", not an ephemeris.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .targets import DarkNight, Target, target_night

MIN_RADIANT_ALTITUDE = 10.0


@dataclass(frozen=True)
class Shower:
    id: str
    name: str
    active: tuple[tuple[int, int], tuple[int, int]]  # (month, day) it starts and ends
    peak: tuple[int, int]
    zhr: int  # zenithal hourly rate at the peak, under ideal skies
    ra: float  # radiant, hours
    dec: float  # degrees
    parent: str | None = None

    def peak_date(self, year: int) -> date:
        return date(year, *self.peak)

    def radiant(self) -> Target:
        return Target(f"shower_{self.id}", self.name, self.name, "radiant", self.ra, self.dec)


SHOWERS: tuple[Shower, ...] = (
    Shower("quadrantids", "Quadrantids", ((12, 28), (1, 12)), (1, 3), 80, 15.33, 49.5, "2003 EH1"),
    Shower("lyrids", "Lyrids", ((4, 14), (4, 30)), (4, 22), 18, 18.07, 34.0, "C/1861 G1 Thatcher"),
    Shower("eta_aquariids", "Eta Aquariids", ((4, 19), (5, 28)), (5, 6), 50, 22.5, -1.0, "1P/Halley"),
    Shower("delta_aquariids", "Southern Delta Aquariids", ((7, 12), (8, 23)), (7, 30), 25, 22.67, -16.4, "96P/Machholz"),
    Shower("alpha_capricornids", "Alpha Capricornids", ((7, 3), (8, 15)), (7, 30), 5, 20.47, -10.0, "169P/NEAT"),
    Shower("perseids", "Perseids", ((7, 17), (8, 24)), (8, 12), 100, 3.2, 58.0, "109P/Swift-Tuttle"),
    Shower("draconids", "Draconids", ((10, 6), (10, 10)), (10, 8), 10, 17.47, 54.0, "21P/Giacobini-Zinner"),
    Shower("orionids", "Orionids", ((10, 2), (11, 7)), (10, 21), 20, 6.33, 16.0, "1P/Halley"),
    Shower("southern_taurids", "Southern Taurids", ((9, 20), (11, 20)), (11, 5), 5, 3.47, 13.0, "2P/Encke"),
    Shower("northern_taurids", "Northern Taurids", ((10, 20), (12, 10)), (11, 12), 5, 3.87, 22.0, "2P/Encke"),
    Shower("leonids", "Leonids", ((11, 6), (11, 30)), (11, 17), 15, 10.13, 22.0, "55P/Tempel-Tuttle"),
    Shower("geminids", "Geminids", ((12, 4), (12, 20)), (12, 14), 150, 7.47, 33.0, "3200 Phaethon"),
    Shower("ursids", "Ursids", ((12, 17), (12, 26)), (12, 22), 10, 14.6, 75.0, "8P/Tuttle"),
)


def upcoming(today: date, count: int | None = None) -> list[tuple[Shower, date]]:
    """Showers by their next peak night (tonight counts), soonest first."""
    out = []
    for sh in SHOWERS:
        peak = sh.peak_date(today.year)
        if peak < today:
            peak = sh.peak_date(today.year + 1)
        out.append((sh, peak))
    out.sort(key=lambda p: p[1])
    return out[:count]


def is_active(shower: Shower, d: date) -> bool:
    (sm, sd), (em, ed) = shower.active
    start, end = (sm, sd), (em, ed)
    here = (d.month, d.day)
    return start <= here <= end if start <= end else here >= start or here <= end


@dataclass
class ShowerOutlook:
    shower: Shower
    peak: date  # the evening the peak night begins
    radiant_peak_altitude: float
    radiant_minutes: int  # dark minutes with the radiant above MIN_RADIANT_ALTITUDE
    moon_illumination: float  # 0-1
    moon_free_minutes: int
    moon_penalty: float
    rating: int  # 0-100


def outlook(shower: Shower, peak: date, night: DarkNight) -> ShowerOutlook:
    """How good the peak night is at this location: radiant height, time it's up, and the moon."""
    tn = target_night(shower.radiant(), night, MIN_RADIANT_ALTITUDE)
    height = min(1.0, max(0.0, tn.peak_altitude) / 50)
    strength = min(1.0, (shower.zhr / 60) ** 0.5)
    rating = round(100 * height * strength * (1 - 0.8 * tn.moon_penalty) * min(1.0, tn.minutes / 180)) if tn.observable else 0
    return ShowerOutlook(shower, peak, tn.peak_altitude, tn.minutes, night.moon_illumination, night.moon_free_minutes, tn.moon_penalty, rating)


def next_peak_night(shower: Shower, today: date) -> date:
    peak = shower.peak_date(today.year)
    return peak if peak >= today else shower.peak_date(today.year + 1)



