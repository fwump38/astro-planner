"""The target list for a run of nights at the Home Assistant location, shared by the websocket API and the `visible_targets` service."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .engine import Engine
from .targets import CONSTELLATIONS, KIND_LABELS, Target, TargetNight


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def night_json(tn: TargetNight) -> dict[str, Any]:
    return {
        "date": tn.date.isoformat(),
        "minutes": tn.minutes,
        "peak_altitude": round(tn.peak_altitude, 1),
        "peak_time": _iso(tn.peak_time),
        "direction": tn.direction,
        "start": _iso(tn.start),
        "end": _iso(tn.end),
        "moon_penalty": round(tn.moon_penalty, 2),
        "moon_separation": None if tn.moon_separation is None else round(tn.moon_separation),
    }


def target_json(t: Target, year: int, fov: float) -> dict[str, Any]:
    best = t.best_date(year)
    return {
        "id": t.id,
        "designation": t.designation,
        "name": t.name,
        "label": t.label,
        "kind": t.kind,
        "kind_label": KIND_LABELS.get(t.kind, t.kind),
        "magnitude": t.magnitude,
        "size": t.size,
        "constellation": CONSTELLATIONS.get(t.constellation, t.constellation) if t.constellation else None,
        "ra": t.ra,
        "dec": t.dec,
        "best_date": best.isoformat() if best else None,
        "framing": t.framing(fov),
    }


def targets_payload(engine: Engine, start: date, days: int, kinds: set[str] | None = None, min_minutes: int = 1) -> dict[str, Any]:
    """Every target (within the magnitude limit) that is up in the dark on at least one of the nights, with each night's details."""
    cfg = engine.config
    dates = [start + timedelta(days=i) for i in range(days)]
    seen: dict[str, tuple[Target, list[TargetNight]]] = {}
    for d in dates:
        for tn in engine.home_targets(d):
            t = tn.target
            if tn.minutes < min_minutes or (kinds and t.kind not in kinds):
                continue
            if t.magnitude is not None and t.magnitude > cfg.target_max_magnitude:
                continue
            seen.setdefault(t.id, (t, []))[1].append(tn)
    targets = [{**target_json(t, start.year, cfg.fov_arcmin), "nights": [night_json(n) for n in nights]} for t, nights in seen.values()]
    dark = []
    for d in dates:
        dn = engine.home_dark(d)
        dark.append(
            {
                "date": d.isoformat(),
                "dusk": _iso(dn.dusk),
                "dawn": _iso(dn.dawn),
                "dark_minutes": dn.astro_minutes,
                "moon_free_minutes": dn.moon_free_minutes,
                "moon_illumination": round(dn.moon_illumination, 3),
            }
        )
    return {
        "location": {"latitude": cfg.home.latitude, "longitude": cfg.home.longitude},
        "settings": {"min_altitude": cfg.target_min_altitude, "max_magnitude": cfg.target_max_magnitude, "fov_arcmin": cfg.fov_arcmin},
        "dark": dark,
        "targets": targets,
        "total": len(cfg.targets),
    }
