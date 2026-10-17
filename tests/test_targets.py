from datetime import UTC, date, datetime

import astronomy as ae
import pytest

from custom_components.astro_planner.astro import Observer, SkyOptions, compute_night, to_time
from custom_components.astro_planner.scoring import DEFAULT_WEIGHTS, EMPTY_FORECAST, Forecast, score_target, target_factor
from custom_components.astro_planner.showers import SHOWERS, is_active, outlook, upcoming
from custom_components.astro_planner.targets import (
    CONSTELLATIONS,
    KIND_LABELS,
    MILKY_WAY,
    _altitude,
    all_targets,
    compute_dark_night,
    custom_targets,
    messier_targets,
    milky_way_night,
    parse_dec,
    parse_ra,
    planet_targets,
    target_night,
    target_nights,
)

DENVER = Observer(39.74, -104.99, 1600)
BY_ID = {t.id: t for t in all_targets()}


def test_catalog_has_all_110_messier_objects_with_names():
    ms = messier_targets()
    assert [t.designation for t in ms] == [f"M{i}" for i in range(1, 111)]
    assert BY_ID["m1"].label == "M1 – Crab Nebula"
    assert BY_ID["m31"].label == "M31 – Andromeda Galaxy"
    assert BY_ID["m45"].label == "M45 – Pleiades"
    # objects without a common name get a descriptive one rather than a bare number
    assert BY_ID["m2"].label == "M2 – Globular cluster in Aquarius"
    assert all(t.kind in KIND_LABELS and t.magnitude and -90 <= t.dec <= 90 and 0 <= t.ra < 24 for t in ms)
    assert all(t.constellation in CONSTELLATIONS for t in ms)


def test_altitude_formula_agrees_with_astronomy_engine():
    when = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    t = to_time(when)
    obs = DENVER.ae()
    lst = (ae.SiderealTime(t) * 15 + DENVER.longitude) % 360
    for tid in ("m31", "m42", "m13", "m8"):
        tgt = BY_ID[tid]
        hor = ae.Horizon(t, obs, tgt.ra, tgt.dec, ae.Refraction.Airless)
        # engine uses equator-of-date; we use J2000 as-is, so allow for precession
        assert _altitude(lst, tgt.ra * 15, tgt.dec, DENVER.latitude) == pytest.approx(hor.altitude, abs=0.6)


def test_best_date_is_when_the_object_culminates_at_midnight():
    assert BY_ID["m31"].best_date(2026).month in (9, 10)
    assert BY_ID["m42"].best_date(2026).month in (12, 1)
    assert BY_ID["m13"].best_date(2026).month in (6, 7)
    assert BY_ID["jupiter"].best_date(2026) is None


def test_framing_against_a_field_of_view():
    m31, m57 = BY_ID["m31"], BY_ID["m57"]
    assert m31.framing(0) is None
    assert m31.framing(180) == "fits" and m31.framing(100) == "too_large"
    assert m57.framing(1000) == "too_small" and m57.framing(10) == "fits"


def test_october_sky_from_colorado():
    night = compute_dark_night(date(2026, 10, 17), DENVER)
    assert 9 * 60 <= night.astro_minutes <= 11 * 60
    by = {tn.target.id: tn for tn in target_nights(all_targets(), night, 30)}
    assert by["m31"].observable and by["m31"].peak_altitude > 80 and by["m31"].minutes > 400
    assert by["m45"].observable and by["m42"].observable
    assert not by["m8"].observable  # the Lagoon Nebula never gets 30° up after dark in mid-October
    assert by["m13"].peak_altitude > 0
    assert sum(tn.observable for tn in by.values()) > 30
    assert by["m31"].direction in {"N", "NNW", "NNE", "NW", "NE", "W", "E", "ENE", "WNW", "S", "SSE", "SSW"}


def test_target_that_never_rises_far_enough_is_not_observable():
    far_north = Observer(66.0, 25.0)  # M7 at -35° never gets 30° up from 66°N
    night = compute_dark_night(date(2026, 11, 20), far_north)
    tn = target_night(BY_ID["m7"], night, 30)
    assert not tn.observable and tn.minutes == 0 and tn.peak_altitude < 0


def test_moon_penalty_is_zero_when_moon_is_down_and_grows_with_brightness():
    night = compute_dark_night(date(2026, 11, 9), DENVER)  # new moon
    full = compute_dark_night(date(2026, 11, 24), DENVER)  # full moon
    pen = lambda n: target_night(BY_ID["m31"], n, 30).moon_penalty
    assert pen(night) < 0.05
    assert pen(full) > pen(night) + 0.3


def test_planets_use_their_own_position():
    night = compute_dark_night(date(2026, 10, 17), DENVER)
    tn = {t.id: target_night(t, night, 20) for t in planet_targets()}
    assert tn["saturn"].observable and tn["saturn"].direction  # Saturn is an evening object in autumn 2026
    assert not tn["venus"].observable  # never in astronomical darkness this month


def test_custom_targets_parse_and_skip_bad_rows():
    rows = [
        {"name": "NGC 7000", "ra": "20:59:17", "dec": "+44:31:44", "magnitude": 4, "size": 120},
        {"name": "Bad", "ra": "99", "dec": "0"},
        {"name": "", "ra": 1, "dec": 1},
        {"name": "Decimal", "ra": 5.59, "dec": -5.4},
    ]
    out = custom_targets(rows)
    assert [t.name for t in out] == ["NGC 7000", "Decimal"]
    assert out[0].ra == pytest.approx(20.988, abs=0.001) and out[0].dec == pytest.approx(44.529, abs=0.001) and out[0].kind == "custom"
    assert parse_ra("5h35m17s") == pytest.approx(5.588, abs=0.001) and parse_ra(-1) is None and parse_dec("-90") == -90 and parse_dec("91") is None


def test_dark_night_moon_free_window_matches_minutes():
    night = compute_dark_night(date(2026, 11, 18), DENVER)  # waxing moon sets before dawn... leaving a late-evening gap, or none
    w = night.moon_free_window
    if w:
        assert (w[1] - w[0]).total_seconds() / 60 <= night.moon_free_minutes + night.step_minutes
    new = compute_dark_night(date(2026, 11, 9), DENVER)
    assert new.moon_free_minutes == new.astro_minutes and new.moon_free_window[1] > new.moon_free_window[0]


# -- scoring ----------------------------------------------------------------------------------


def _clear_forecast(times):
    rows = [{"datetime": t.isoformat(), "cloudless_percentage": 100, "transparency_percentage": 100, "seeing_percentage": 100, "calm_percentage": 100, "cloudcover_percentage": 0} for t in times]
    return Forecast.from_rows(rows)


def test_target_score_rewards_time_up_and_a_dark_site_and_punishes_a_bright_moon():
    dark = compute_dark_night(date(2026, 11, 9), DENVER)
    tn = target_night(BY_ID["m31"], dark, 30)
    fc = _clear_forecast(tn.times)
    best = score_target(tn, 1, fc, DEFAULT_WEIGHTS)
    assert not best.astro_only and best.weather == pytest.approx(1) and best.score >= 90
    assert score_target(tn, 8, fc, DEFAULT_WEIGHTS).score < best.score - 25  # a galaxy hates a bright sky
    jup = target_night(BY_ID["saturn"], dark, 30)
    assert score_target(jup, 8, _clear_forecast(jup.times), DEFAULT_WEIGHTS).score > 70  # a planet doesn't care so much
    full = compute_dark_night(date(2026, 11, 24), DENVER)
    assert score_target(target_night(BY_ID["m31"], full, 30), 1, fc, DEFAULT_WEIGHTS).score < best.score - 20


def test_target_score_is_astronomy_only_without_a_forecast():
    tn = target_night(BY_ID["m31"], compute_dark_night(date(2026, 10, 17), DENVER), 30)
    sc = score_target(tn, 3, EMPTY_FORECAST, DEFAULT_WEIGHTS)
    assert sc.astro_only and sc.weather is None and sc.score > 0
    assert target_factor(target_night(BY_ID["m8"], compute_dark_night(date(2026, 10, 17), DENVER), 30)) == 0


def test_milky_way_is_scored_like_any_other_target():
    site = Observer(37.047, -112.527, 1500)
    n = compute_night(date(2026, 7, 14), site, SkyOptions(10, 10, 2))
    tn = milky_way_night(n)
    assert tn.target is MILKY_WAY and tn.minutes == n.gc_minutes and tn.observable
    sc = score_target(tn, 2, EMPTY_FORECAST, DEFAULT_WEIGHTS)
    assert sc.score > 60


# -- meteor showers ---------------------------------------------------------------------------


def test_upcoming_showers_are_sorted_and_wrap_the_year():
    ups = upcoming(date(2026, 12, 20))
    assert [p for _, p in ups] == sorted(p for _, p in ups)
    assert ups[0][0].id == "ursids" and ups[-1][1].year == 2027
    assert upcoming(date(2026, 8, 12), 1)[0][0].id == "perseids"  # the peak night itself still counts


def test_geminids_from_colorado_rate_well_and_the_moon_matters():
    gem = next(s for s in SHOWERS if s.id == "geminids")
    night = compute_dark_night(date(2026, 12, 14), DENVER)
    o = outlook(gem, date(2026, 12, 14), night)
    assert o.radiant_peak_altitude > 70 and o.rating >= 80
    ori = next(s for s in SHOWERS if s.id == "orionids")
    bright = outlook(ori, date(2026, 10, 21), compute_dark_night(date(2026, 10, 21), DENVER))
    assert bright.moon_illumination > 0.7 and bright.rating < o.rating


def test_shower_activity_window_wraps_new_year():
    quad = next(s for s in SHOWERS if s.id == "quadrantids")
    assert is_active(quad, date(2026, 12, 30)) and is_active(quad, date(2026, 1, 5)) and not is_active(quad, date(2026, 6, 1))
