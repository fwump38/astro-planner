"""Scoring of nights and targets from the sky and the weather forecast."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .astro import NightSummary
from .targets import TargetNight

DEFAULT_WEIGHTS = {"cloud": 3.0, "transparency": 2.0, "seeing": 0.5, "calm": 1.0}
_FORECAST_SLACK = timedelta(minutes=45)


@dataclass
class Forecast:
    """Hourly forecast rows sorted by time."""

    times: list[datetime]
    hours: list[dict[str, Any]]

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]] | None) -> Forecast:
        parsed: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows or []:
            try:
                parsed.append((datetime.fromisoformat(str(row["datetime"])), row))
            except (KeyError, ValueError):
                continue
        parsed.sort(key=lambda p: p[0])
        return cls([p[0] for p in parsed], [p[1] for p in parsed])

    @property
    def end(self) -> datetime | None:
        return self.times[-1] if self.times else None

    def at(self, when: datetime) -> dict[str, Any] | None:
        """The forecast hour nearest `when` (within 45 minutes), or None beyond the horizon."""
        if not self.times or when < self.times[0] - _FORECAST_SLACK or when > self.times[-1] + _FORECAST_SLACK:
            return None
        i = bisect_left(self.times, when)
        best = min((j for j in (i - 1, i) if 0 <= j < len(self.times)), key=lambda j: abs(self.times[j] - when))
        return self.hours[best] if abs(self.times[best] - when) <= _FORECAST_SLACK else None


EMPTY_FORECAST = Forecast([], [])


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def hour_quality(h: dict[str, Any], w: dict[str, float]) -> float | None:
    cloudless = _num(h.get("cloudless_percentage"))
    cover = _num(h.get("cloudcover_percentage"))
    if cloudless is None and cover is not None:
        cloudless = 100 - cover
    parts = [
        (cloudless, w["cloud"]),
        (_num(h.get("transparency_percentage")), w["transparency"]),
        (_num(h.get("seeing_percentage")), w["seeing"]),
        (_num(h.get("calm_percentage")), w["calm"]),
    ]
    total = weight = 0.0
    for v, wt in parts:
        if v is None or wt <= 0:
            continue
        total += max(0.0, min(100.0, v)) * wt
        weight += wt
    if weight == 0:
        return None
    q = total / weight
    if (_num(h.get("precipitation_amount")) or 0) > 0.1:
        q *= 0.3
    if (_num(h.get("fog2m_area_fraction")) or 0) > 50:
        q *= 0.5
    return q


def bortle_factor(bortle: float) -> float:
    return 1 - (max(1, min(9, bortle)) - 1) * 0.06


def window_factor(minutes: float) -> float:
    return 0.6 + 0.4 * min(1.0, minutes / 180)


# The target-independent sky quality is worth this much of a night's score; the Milky Way core being up adds the rest.
BASE_SKY = 0.7


def dark_factor(night: NightSummary) -> float:
    """0.5-1: a long stretch of moon-free astronomical darkness is worth more than a short or moonlit one."""
    return 0.5 + 0.5 * min(1.0, night.dark_minutes / 240)


def sky_factor(night: NightSummary, bortle: float) -> float:
    """0-1 quality of the sky itself at a site: Bortle class, moon-free darkness and city glow, whatever the target."""
    return bortle_factor(bortle) * dark_factor(night) * (1 - night.ambient_dome_penalty)


@dataclass
class NightScore:
    night: NightSummary
    score: int | None  # 0-100; None only for a site without coordinates
    astro_only: bool  # no forecast covers the night, so the score is astronomy-only
    weather: float | None  # mean weather quality 0-1 over the part of the night that was scored
    cloud: float | None  # mean cloud cover % over the same hours
    dome_warning: bool
    sky: float = 0.0  # target-independent sky quality, 0-1
    milky_way: bool = False  # the core is up in dark, moonless sky tonight (this is what lifts the score above the base)


def _cover(h: dict[str, Any]) -> float | None:
    cover = _num(h.get("cloudcover_percentage"))
    if cover is None and (cl := _num(h.get("cloudless_percentage"))) is not None:
        cover = 100 - cl
    return cover


def weather_over(times: Sequence[datetime], forecast: Forecast, weights: dict[str, float]) -> tuple[float | None, float | None, bool]:
    """(mean quality 0-1, mean cloud cover %, astro_only) over the given moments; astro_only when the forecast covers under half."""
    q_sum = cloud_sum = 0.0
    q_n = cloud_n = 0
    for when in times:
        h = forecast.at(when)
        if h is None:
            continue
        q = hour_quality(h, weights)
        if q is not None:
            q_sum += q
            q_n += 1
        if (cover := _cover(h)) is not None:
            cloud_sum += cover
            cloud_n += 1
    if not times or q_n < len(times) / 2:
        return None, cloud_sum / cloud_n if cloud_n else None, True
    return q_sum / q_n / 100, cloud_sum / cloud_n if cloud_n else None, False


def night_hours(night: NightSummary, step_minutes: int = 60) -> list[datetime]:
    """Hourly moments between astronomical dusk and dawn."""
    if not night.dusk or not night.dawn:
        return []
    out, t = [], night.dusk
    while t <= night.dawn:
        out.append(t)
        t += timedelta(minutes=step_minutes)
    return out


def score_night(night: NightSummary, bortle: float, forecast: Forecast, weights: dict[str, float], dome_warning_threshold: float = 0.15) -> NightScore:
    """Score a night at a site: the sky quality alone (max 70), plus the Milky Way core if it's up (the rest of the 100)."""
    warn = night.dome_penalty >= dome_warning_threshold
    visible = night.visible
    # Weather over the core window when it's up, otherwise over the whole dark part of the night.
    times = [s.time for s in visible] or night_hours(night)
    weather, cloud, astro_only = weather_over(times, forecast, weights)
    sky = sky_factor(night, bortle)
    mw = bortle_factor(bortle) * (1 - night.dome_penalty) * window_factor(night.gc_minutes) if visible else 0.0
    astro = BASE_SKY * sky + (1 - BASE_SKY) * mw
    score = _js_round(100 * astro * (1 if weather is None else weather))
    return NightScore(night, score, astro_only, weather, cloud, warn, sky, bool(visible))


# --- other targets ----------------------------------------------------------------------------


@dataclass
class TargetScore:
    night: TargetNight
    score: int  # 0-100
    astro_only: bool
    weather: float | None
    cloud: float | None


def target_factor(tn: TargetNight) -> float:
    """0-1: how well the target is placed, from time above the minimum altitude, its height and the moon."""
    if not tn.minutes:
        return 0.0
    height = 1.0 if tn.target.kind == "milky_way" else 0.6 + 0.4 * min(1.0, max(0.0, tn.peak_altitude - 20) / 50)
    return window_factor(tn.minutes) * height * (1 - 0.8 * tn.moon_penalty)


def score_target(tn: TargetNight, bortle: float, forecast: Forecast, weights: dict[str, float]) -> TargetScore:
    glow = bortle_factor(bortle) * (1 - tn.dome_penalty)
    light = 1 - tn.target.sensitivity * (1 - glow)
    weather, cloud, astro_only = weather_over(tn.times, forecast, weights)
    score = _js_round(100 * light * target_factor(tn) * (1 if weather is None else weather))
    return TargetScore(tn, score, astro_only, weather, cloud)


def _js_round(x: float) -> int:
    import math

    return math.floor(x + 0.5)


def score_band(score: int | None) -> str:
    if score is None:
        return "none"
    if score >= 75:
        return "great"
    if score >= 55:
        return "good"
    if score >= 35:
        return "fair"
    return "poor"
