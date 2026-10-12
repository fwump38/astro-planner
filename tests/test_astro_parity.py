"""The integration must agree with the card's TypeScript astronomy (fixtures generated from the card's code)."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from custom_components.milky_way_planner.astro import LightDome, Observer, SkyOptions, compute_night, dome_geometry

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ts-nights.json").read_text())
SITES = [Observer(37.047, -112.527, 1500), Observer(46.2, 7.3, 500), Observer(-33.9, 18.4, 20)]
DOMES = [LightDome("A", 36.17, -115.14, 650000), LightDome("B", 37.7, -113.1, 40000)]


def _same(a, b):
    assert (a is None) == (b is None)
    if a is not None:
        assert abs((a - b).total_seconds()) < 1


def _dt(s):
    return None if s is None else datetime.fromisoformat(s)


@pytest.mark.parametrize("row", FIXTURE, ids=lambda r: f"site{r['si']}-{r['date']}")
def test_matches_card(row):
    o = SITES[row["si"]]
    opts = SkyOptions(0 if row["si"] == 0 else 10, 10, 3, dome_geometry(o, DOMES), 25)
    n = compute_night(date(*row["date"]), o, opts, 10)
    _same(n.gc_start, _dt(row["gcStart"]))
    _same(n.gc_end, _dt(row["gcEnd"]))
    assert n.gc_minutes == row["gcMinutes"]
    assert n.dark_minutes == row["darkMinutes"]
    assert n.orientation == row["orientation"]
    _same(n.moon_rise, _dt(row["moonRise"]))
    _same(n.moon_set, _dt(row["moonSet"]))
    assert n.dusk is not None and abs((n.dusk - _dt(row["dusk"])).total_seconds()) < 2
    assert n.moon_phase == pytest.approx(row["moonPhase"], abs=1e-4)
    assert n.moon_illumination == pytest.approx(row["moonIll"], abs=1e-5)
    assert n.dome_penalty == pytest.approx(row["domePenalty"], abs=1e-4)
    assert (n.worst_dome.dome.name if n.worst_dome else None) == row["worst"]
    if row["tilt"] is not None:
        assert n.gc_tilt == pytest.approx(row["tilt"], abs=1e-3)
        assert n.gc_peak_altitude == pytest.approx(row["peak"], abs=1e-3)
        assert n.gc_azimuth_start == pytest.approx(row["azS"], abs=1e-3)
