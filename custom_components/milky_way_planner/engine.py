"""Night computation with caching. All heavy work is synchronous and meant to run in the executor."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, tzinfo

from .astro import NightSummary, Observer, SkyOptions, compute_night
from .config import PlannerConfig, Site
from .scoring import Forecast, NightScore, score_night

_MAX_CACHE = 1500


class Engine:
    def __init__(self, config: PlannerConfig, tz: tzinfo) -> None:
        self.config = config
        self.tz = tz
        self._cache: OrderedDict[tuple, NightSummary] = OrderedDict()
        self._lock = threading.Lock()
        self._cal_opts = config.calendar_options()
        self._site_opts = {s.key: config.site_options(s) for s in config.sites}

    def local_date(self, when: datetime) -> date:
        return when.astimezone(self.tz).date()

    def tonight(self, now: datetime) -> date:
        """The night that matters now: before ~6am it began yesterday evening."""
        local = now.astimezone(self.tz)
        return (local - timedelta(days=1)).date() if local.hour < 6 else local.date()

    # -- cached night computation ------------------------------------------------------------

    def _night(self, key_prefix: tuple, d: date, observer: Observer, opts: SkyOptions) -> NightSummary:
        key = (*key_prefix, d)
        with self._lock:
            if (hit := self._cache.get(key)) is not None:
                self._cache.move_to_end(key)
                return hit
        night = compute_night(d, observer, opts, 10, keep_samples=False)
        with self._lock:
            self._cache[key] = night
            while len(self._cache) > _MAX_CACHE:
                self._cache.popitem(last=False)
        return night

    def ref_night(self, d: date) -> NightSummary:
        """The calendar's astronomy for the night starting on `d`, at the centre of the sites."""
        ref = self.config.reference
        return self._night(("ref", ref, self.config.calendar_min_altitude, self.config.max_moon_illumination), d, ref, self._cal_opts)

    def site_night(self, site: Site, d: date) -> NightSummary | None:
        if not site.observer:
            return None
        opts = self._site_opts[site.key]
        return self._night(("site", site.key, site.observer, site.bortle, self.config.min_gc_altitude, self.config.max_moon_illumination), d, site.observer, opts)

    def score(self, site: Site, d: date, forecast: Forecast) -> NightScore | None:
        night = self.site_night(site, d)
        if night is None:
            return None
        return score_night(night, site.bortle, forecast, self.config.weights, self.config.dome_warning_threshold)

    # -- bulk helpers ------------------------------------------------------------------------

    def ref_nights(self, dates: Iterable[date]) -> list[NightSummary]:
        return [self.ref_night(d) for d in dates]

    def next_window(self, after: datetime, max_days: int = 400, min_grade: str = "short") -> NightSummary | None:
        """The first night whose core window ends after `after` (at the reference point)."""
        start = self.local_date(after) - timedelta(days=1)
        for i in range(max_days):
            n = self.ref_night(start + timedelta(days=i))
            if n.gc_end and n.gc_end > after and (min_grade == "short" or n.grade(self.config.calendar_best_minutes) == "best"):
                return n
        return None
