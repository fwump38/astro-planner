"""The astronomy against Capture the Atlas's published 2026 calendar (Southwest USA, Kanab UT, 37°N).

Moved here from the card's tests when the card stopped calculating anything itself. That calendar counts the core as up from the
horizon, hence a minimum altitude of 0. Times are local clock (America/Denver).
"""

import json
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.astro_planner.astro import Observer, SkyOptions, compute_night

KANAB = Observer(37.047, -112.527, 1500)
DENVER = ZoneInfo("America/Denver")
REFERENCE = [
    ((2026, 3, 21), "02:48", "06:02", "arch"),
    ((2026, 4, 11), "01:25", "05:27", "arch"),
    ((2026, 5, 16), "23:08", "04:34", "angled"),
    ((2026, 6, 13), "22:45", "04:15", "angled"),
    ((2026, 7, 11), "22:42", "04:15", "upright"),
    ((2026, 9, 5), "21:23", "00:35", "upright"),
    ((2026, 10, 3), "20:37", "22:45", "upright"),
    ((2026, 10, 10), "20:27", "22:17", "upright"),
    ((2026, 10, 31), "20:02", "20:55", "upright"),
]


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _circular_diff(a: int, b: int) -> int:
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def _clock(dt) -> int:
    local = dt.astimezone(DENVER)
    return local.hour * 60 + local.minute


@pytest.mark.parametrize(("when", "start", "end", "orientation"), REFERENCE, ids=lambda v: str(v))
def test_core_window_matches_capture_the_atlas(when, start, end, orientation):
    night = compute_night(date(*when), KANAB, SkyOptions(0, 100, 2, (), 25), 2)
    assert night.gc_start and night.gc_end
    assert _circular_diff(_clock(night.gc_start), _minutes(start)) <= 10
    assert _circular_diff(_clock(night.gc_end), _minutes(end)) <= 10
    assert night.orientation == orientation


def test_calendar_grading_matches_capture_the_atlas_colours():
    ref = json.loads((Path(__file__).parent / "fixtures" / "cta-2026.json").read_text())["rows"]
    opts = SkyOptions(0, 10, 2, (), 25)
    agree, err = 0, 0.0
    for i, row in enumerate(ref):
        night = compute_night(date(2026, 1, 3) + timedelta(days=7 * i), KANAB, opts, 5, keep_samples=False)
        want = {"pink": "best", "blue": "short"}.get(row["color"], "none")
        agree += night.grade(180) == want
        err += abs(night.gc_minutes - row["coreMinutes"])
    assert agree >= 49
    assert err / len(ref) < 10
