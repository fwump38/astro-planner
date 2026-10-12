import json
from datetime import date
from pathlib import Path

import pytest

from custom_components.milky_way_planner.astro import Observer, SkyOptions, compute_night
from custom_components.milky_way_planner.scoring import DEFAULT_WEIGHTS, EMPTY_FORECAST, Forecast, hour_quality, score_band, score_night

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


def test_no_core_means_no_score():
    n = compute_night(date(2026, 12, 25), Observer(37.047, -112.527, 1500), SkyOptions(10, 10, 2))
    assert score_night(n, 2, EMPTY_FORECAST, DEFAULT_WEIGHTS).score is None


@pytest.mark.parametrize("score,band", [(None, "none"), (10, "poor"), (40, "fair"), (60, "good"), (80, "great")])
def test_bands(score, band):
    assert score_band(score) == band
