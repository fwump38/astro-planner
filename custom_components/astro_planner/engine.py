"""Night computation with caching. All heavy work is synchronous and meant to run in the executor."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from .astro import NightSummary, Observer, SkyOptions, compute_night
from .config import PlannerConfig, Site
from .scoring import Forecast, NightScore, TargetScore, score_night, score_target
from .targets import DarkNight, TargetNight, compute_dark_night, milky_way_night, target_nights

_MAX_CACHE = 1500


class Engine:
    def __init__(self, config: PlannerConfig, tz: tzinfo) -> None:
        self.config = config
        self.tz = tz
        self._cache: OrderedDict[tuple, Any] = OrderedDict()
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

    def _cached(self, key: tuple, compute) -> Any:
        with self._lock:
            if (hit := self._cache.get(key)) is not None:
                self._cache.move_to_end(key)
                return hit
        value = compute()
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > _MAX_CACHE:
                self._cache.popitem(last=False)
        return value

    def _night(self, key_prefix: tuple, d: date, observer: Observer, opts: SkyOptions) -> NightSummary:
        return self._cached((*key_prefix, d), lambda: compute_night(d, observer, opts, 10, keep_samples=False))

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

    # -- targets -------------------------------------------------------------------------------

    def dark_night(self, observer: Observer, d: date) -> DarkNight:
        moon = self.config.max_moon_illumination
        return self._cached(("dark", observer, moon, d), lambda: compute_dark_night(d, observer, moon))

    def home_dark(self, d: date) -> DarkNight:
        return self.dark_night(self.config.home, d)

    def _targets_at(self, scope: str, observer: Observer, d: date, domes) -> list[TargetNight]:
        cfg = self.config
        key = ("targets", scope, observer, cfg.target_min_altitude, cfg.dome_sigma, cfg.targets, cfg.max_moon_illumination, d)
        return self._cached(
            key, lambda: target_nights(list(cfg.targets), self.dark_night(observer, d), cfg.target_min_altitude, domes, cfg.dome_sigma)
        )

    def home_targets(self, d: date) -> list[TargetNight]:
        """Every target's night at the Home Assistant location (no city glow, since there's no site to measure it from)."""
        return self._targets_at("home", self.config.home, d, ())

    def site_targets(self, site: Site, d: date) -> list[TargetNight]:
        if not site.observer:
            return []
        domes = self._site_opts[site.key].domes
        return self._targets_at(f"site:{site.key}", site.observer, d, domes)

    def score_targets(self, site: Site, d: date, forecast: Forecast) -> list[TargetScore]:
        """The plannable targets that are up at `site` on night `d`, best first. The Milky Way core is one of them."""
        cfg = self.config
        plannable = {t.id for t in cfg.plannable_targets()}
        scores = [
            score_target(tn, site.bortle, forecast, cfg.weights)
            for tn in self.site_targets(site, d)
            if tn.observable and tn.target.id in plannable
        ]
        mw = self.site_night(site, d)
        if mw is not None and mw.gc_minutes:
            scores.append(score_target(milky_way_night(mw), site.bortle, forecast, cfg.weights))
        scores.sort(key=lambda s: (-s.score, s.night.target.id))
        return scores

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
