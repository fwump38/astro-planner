"""Write canned websocket responses for the card's dev harness (../astro-planner-card/dev/index.html).

    PYTHONPATH=. .venv/bin/python scripts/export_card_mock.py ../astro-planner-card/dev/mock.json

The data is for a fixed "now" (the harness freezes the browser's clock to match), so the harness works without Home Assistant.
"""

from __future__ import annotations

import json
import math
import sys
import zoneinfo
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from custom_components.astro_planner.astro import LightDome, Observer, SkyOptions, compute_night, dome_geometry, sample_sky
from custom_components.astro_planner.config import PlannerConfig, Site
from custom_components.astro_planner.engine import Engine
from custom_components.astro_planner.scoring import DEFAULT_WEIGHTS, Forecast, score_night
from custom_components.astro_planner.showers import is_active, outlook, upcoming
from custom_components.astro_planner.target_view import targets_payload
from custom_components.astro_planner.targets import all_targets
from custom_components.astro_planner.websocket_api import serialize_night

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)
TONIGHT = date(2026, 10, 10)
NIGHTS = 3
SITES = {
    "home": ("Home (baseline)", 35.687, -105.938, 5, 0, 0.55),
    "chaco": ("Chaco Canyon", 36.06, -107.96, 1, 150, 0.9),
    "abiquiu": ("Abiquiu", 36.33, -106.47, 3, 45, 0.85),
    "carrizozo": ("Valley of Fires", 33.68, -105.92, 2, 150, 0.7),
}
DOMES = [
    LightDome("Albuquerque", 35.084, -106.651, 560000),
    LightDome("Santa Fe", 35.687, -105.938, 89000),
    LightDome("Rio Rancho", 35.233, -106.664, 104000),
    LightDome("Farmington", 36.728, -108.219, 46000),
]


def forecast(site: tuple) -> Forecast:
    _, lat, _, _, _, clear_base = site
    rows = []
    for i in range(48):
        wave = 0.5 + 0.5 * math.sin(i / 5 + lat)
        clear = min(1, max(0, clear_base * 0.6 + wave * 0.45))
        rows.append(
            {
                "datetime": (NOW.replace(minute=0) + timedelta(hours=i)).isoformat(),
                "cloudless_percentage": round(clear * 100),
                "cloudcover_percentage": round((1 - clear) * 100),
                "transparency_percentage": round(55 + clear * 40),
                "seeing_percentage": round(40 + wave * 50),
                "calm_percentage": round(60 + (1 - wave) * 35),
            }
        )
    return Forecast.from_rows(rows)


def main(out: Path) -> None:
    sites, scores, sky = [], {}, {}
    site_objs = {k: Site(f"weather.astroweather_{k}", v[0], v[3], v[4] or None, False, Observer(v[1], v[2], 1800)) for k, v in SITES.items()}
    home = Observer(35.687, -105.938, 2100)
    cfg = PlannerConfig(
        home=home, sites=list(site_objs.values()), min_gc_altitude=10, max_moon_illumination=10, calendar_min_altitude=0, calendar_best_minutes=180,
        weights=DEFAULT_WEIGHTS, domes=DOMES, dome_sigma=25, dome_warning_threshold=0.15, nights=NIGHTS, good_score_threshold=70, auto_drive_time=False,
        targets=tuple(all_targets()),
    )
    engine = Engine(cfg, zoneinfo.ZoneInfo("America/Denver"))
    reference = Observer(sum(s[1] for s in SITES.values()) / 4, sum(s[2] for s in SITES.values()) / 4, 1800)
    for key, s in SITES.items():
        name, lat, lon, bortle, drive, _ = s
        entity = f"weather.astroweather_{key}"
        obs = Observer(lat, lon, 1800)
        opts = SkyOptions(10, 10, bortle, dome_geometry(obs, DOMES), 25)
        sites.append(
            {
                "entity": entity, "name": name, "bortle": bortle, "drive_minutes": drive or None, "drive_estimated": bool(drive),
                "latitude": lat, "longitude": lon, "elevation": 1800,
                "source": {"coordinates": "astroweather", "bortle": "estimated" if key == "chaco" else "configured", "drive_time": "route" if drive else None},
            }
        )
        fc = forecast(s)
        rows = []
        for i in range(NIGHTS):
            night = compute_night(TONIGHT + timedelta(days=i), obs, opts, 10, keep_samples=True)
            sc = score_night(night, bortle, fc, DEFAULT_WEIGHTS)
            ranked = engine.score_targets(site_objs[key], night.date, fc)[:5]
            rows.append(
                {"date": night.date.isoformat(), "score": sc.score, "milky_way": sc.milky_way, "sky": sc.sky, "astro_only": sc.astro_only,
                 "weather": sc.weather, "cloud": sc.cloud, "dome_warning": sc.dome_warning, "night": serialize_night(night, False),
                 "targets": [{"id": t.night.target.id, "label": t.night.target.label, "kind": t.night.target.kind, "score": t.score, "minutes": t.night.minutes,
                              "peak_altitude": round(t.night.peak_altitude), "direction": t.night.direction} for t in ranked]}
            )
        scores[entity] = rows
        start = NOW.replace(minute=0) - timedelta(hours=1)
        sky[entity] = {
            "from": int(start.timestamp() * 1000),
            "hours": [
                {"t": int(x.time.timestamp() * 1000), "sun": round(x.sun_altitude, 3), "moon": round(x.moon_altitude, 3), "lm": round(x.limiting_magnitude, 3),
                 "alt": round(x.gc.altitude, 3), "vis": x.gc_visible, "dp": round(x.dome_penalty, 5)}
                for x in (sample_sky(start + timedelta(hours=h), obs, opts) for h in range(73))
            ],
        }
    cal_opts = SkyOptions(0, 10, 2, (), 25)
    calendar = {}
    d = date(2026, 9, 1)
    while d < date(2027, 9, 1):
        calendar[d.isoformat()] = serialize_night(compute_night(d, reference, cal_opts, 10), True)
        d += timedelta(days=1)
    targets = {}
    for i in range(-7, 60):
        d = TONIGHT + timedelta(days=i)
        targets[d.isoformat()] = targets_payload(engine, d, 1)
    showers = []
    for sh, peak in upcoming(TONIGHT, 3):
        o = outlook(sh, peak, engine.home_dark(peak))
        showers.append({"id": sh.id, "name": sh.name, "peak": peak.isoformat(), "days_until": (peak - TONIGHT).days, "active": is_active(sh, TONIGHT), "zhr": sh.zhr,
                        "radiant_peak_altitude": round(o.radiant_peak_altitude), "moon_illumination": round(o.moon_illumination * 100),
                        "moon_free_minutes": o.moon_free_minutes, "rating": o.rating})
    overview = {
        "api_version": 3, "tonight": TONIGHT.isoformat(), "reference": {"latitude": reference.latitude, "longitude": reference.longitude, "elevation": 1800},
        "settings": {"nights": NIGHTS, "min_gc_altitude": 10, "max_moon_illumination": 10, "calendar_min_altitude": 0, "calendar_best_minutes": 180, "dome_warning_threshold": 0.15,
                     "target_min_altitude": 30, "target_max_magnitude": 12, "fov_arcmin": 0},
        "sites": sites, "scores": scores, "forecast_end": {},
    }
    out.write_text(json.dumps({"now": NOW.isoformat(), "overview": overview, "calendar": calendar, "sky": sky, "targets": targets, "showers": showers}, separators=(",", ":")))
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
