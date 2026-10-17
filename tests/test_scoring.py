import json
from datetime import date
from pathlib import Path

import pytest

from custom_components.astro_planner.astro import Observer, SkyOptions, compute_night
from custom_components.astro_planner.scoring import (
    DEFAULT_WEIGHTS,
    EMPTY_FORECAST,
    Forecast,
    hour_quality,
    night_hours,
    score_band,
    score_night,
)

ROWS = json.loads((Path(__file__).parent / "fixtures" / "astroweather-forecast.json").read_text())["forecast"]


def test_hour_quality_weights_and_penalties():
    w = DEFAULT_WEIGHTS
    clear = {"cloudless_percentage": 100, "transparency_percentage": 100, "seeing_percentage": 100, "calm_percentage": 100}
    assert hour_quality(clear, w) == 100
    assert hour_quality({**clear, "precipitation_amount": 1}, w) == pytest.approx(30)
    assert hour_quality({**clear, "fog2m_area_fraction": 80}, w) == pytest.approx(50)
    assert hour_quality({"cloudcover_percentage": 25}, w) == 75  # falls back to cloud cover when cloudless is missing
    assert hour_quality({}, w) is None


def test_forecast_lookup_respects_horizon():
    f = Forecast.from_rows(ROWS)
    assert f.at(f.times[2]) is ROWS[2] or f.at(f.times[2])["datetime"] == ROWS[2]["datetime"]
    assert f.at(f.end.replace(year=f.end.year + 1)) is None
    assert EMPTY_FORECAST.at(f.times[0]) is None


def _night_with_core():
    o = Observer(37.047, -112.527, 1500)
    return compute_night(date(2026, 7, 11), o, SkyOptions(10, 10, 2)), o


def test_score_is_astronomy_only_without_forecast_and_weather_scaled_with_one():
    night, _ = _night_with_core()
    base = score_night(night, 2, EMPTY_FORECAST, DEFAULT_WEIGHTS)
    assert base.astro_only and base.score is not None and base.weather is None
    # A synthetic forecast covering the whole window with 50% quality halves the score.
    rows = [
        {"datetime": s.time.isoformat(), "cloudless_percentage": 50, "transparency_percentage": 50, "seeing_percentage": 50, "calm_percentage": 50, "cloudcover_percentage": 50}
        for s in night.visible
    ]
    half = score_night(night, 2, Forecast.from_rows(rows), DEFAULT_WEIGHTS)
    assert not half.astro_only and half.weather == pytest.approx(0.5)
    assert half.score == pytest.approx(base.score / 2, abs=1)
    assert half.cloud == pytest.approx(50)


def _perfect_forecast(night):
    from datetime import timedelta

    t, rows = night.dusk, []
    while t <= night.dawn + timedelta(hours=1):
        rows.append({"datetime": t.isoformat(), "cloudless_percentage": 100, "transparency_percentage": 100, "seeing_percentage": 100, "calm_percentage": 100, "cloudcover_percentage": 0})
        t += timedelta(minutes=30)
    return Forecast.from_rows(rows)


def test_a_night_without_the_core_still_scores_but_below_a_milky_way_night():
    site = Observer(37.047, -112.527, 1500)
    opts = SkyOptions(10, 10, 1)
    winter = compute_night(date(2026, 12, 12), site, opts)  # new-moon week, no core before dawn
    summer = compute_night(date(2026, 7, 14), site, opts)
    assert not winter.visible and summer.visible
    off = score_night(winter, 1, _perfect_forecast(winter), DEFAULT_WEIGHTS)
    on = score_night(summer, 1, _perfect_forecast(summer), DEFAULT_WEIGHTS)
    assert off.score is not None and not off.milky_way and not off.astro_only
    assert on.milky_way and 90 <= on.score <= 100
    assert 55 <= off.score <= 70  # the sky alone is worth at most 70
    assert on.score > off.score


def test_moonlit_nights_score_lower_than_dark_ones():
    site = Observer(37.047, -112.527, 1500)
    opts = SkyOptions(10, 10, 2)
    new_moon = compute_night(date(2026, 12, 12), site, opts)
    full_moon = compute_night(date(2026, 12, 25), site, opts)
    assert new_moon.dark_minutes > full_moon.dark_minutes
    assert score_night(new_moon, 2, EMPTY_FORECAST, DEFAULT_WEIGHTS).score > score_night(full_moon, 2, EMPTY_FORECAST, DEFAULT_WEIGHTS).score


def test_off_season_weather_uses_the_whole_dark_night():
    site = Observer(37.047, -112.527, 1500)
    n = compute_night(date(2026, 12, 12), site, SkyOptions(10, 10, 2))
    cloudy = Forecast.from_rows(
        [{"datetime": t.isoformat(), "cloudless_percentage": 10, "cloudcover_percentage": 90} for t in night_hours(n)]
    )
    sc = score_night(n, 2, cloudy, DEFAULT_WEIGHTS)
    assert not sc.astro_only and sc.cloud == pytest.approx(90)
    assert sc.score < score_night(n, 2, EMPTY_FORECAST, DEFAULT_WEIGHTS).score / 2


@pytest.mark.parametrize("score,band", [(None, "none"), (10, "poor"), (40, "fair"), (60, "good"), (80, "great")])
def test_bands(score, band):
    assert score_band(score) == band
