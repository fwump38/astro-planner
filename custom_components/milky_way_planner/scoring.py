"""Weather scoring of a night, ported from the card (`src/scoring/score.ts`)."""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .astro import NightSummary

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


@dataclass
class NightScore:
    night: NightSummary
    score: int | None  # 0-100; None when the core isn't visible after dark
    astro_only: bool  # no forecast covers the window, so the score is astronomy-only
    weather: float | None  # mean weather quality 0-1 over the core window
    cloud: float | None  # mean cloud cover % over the core window
    dome_warning: bool


def score_night(night: NightSummary, bortle: float, forecast: Forecast, weights: dict[str, float], dome_warning_threshold: float = 0.15) -> NightScore:
    visible = night.visible
    warn = night.dome_penalty >= dome_warning_threshold
    if not visible:
        return NightScore(night, None, False, None, None, warn)
    q_sum = cloud_sum = 0.0
    q_n = cloud_n = 0
    for s in visible:
        h = forecast.at(s.time)
        if h is None:
            continue
        q = hour_quality(h, weights)
        if q is not None:
            q_sum += q
            q_n += 1
        cover = _num(h.get("cloudcover_percentage"))
        if cover is None and (cl := _num(h.get("cloudless_percentage"))) is not None:
            cover = 100 - cl
        if cover is not None:
            cloud_sum += cover
            cloud_n += 1
    astro_only = q_n < len(visible) / 2
    weather = None if astro_only else q_sum / q_n / 100
    astro = window_factor(night.gc_minutes) * (1 - night.dome_penalty) * bortle_factor(bortle)
    score = _js_round(100 * astro * (1 if weather is None else weather))
    return NightScore(night, score, astro_only, weather, cloud_sum / cloud_n if cloud_n else None, warn)


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
