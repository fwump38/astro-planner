"""Sensors: sun-style live values for the galactic core, the Milky Way window and the moon, plus per-site scores."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import DEGREE, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .astro import classify_orientation, compass_point
from .config import Site
from .coordinator import LiveState, PlannerData
from .entity import PlannerEntity, device_info
from .runtime import PlannerEntry
from .scoring import score_band


@dataclass(frozen=True, kw_only=True)
class LiveDescription(SensorEntityDescription):
    value_fn: Callable[[LiveState], Any]
    attrs_fn: Callable[[LiveState], dict[str, Any]] | None = None
    device: str = "milky_way"


STATUS_OPTIONS = ["visible", "twilight", "moonlit", "below_horizon"]
ORIENTATIONS = ["arch", "angled", "upright"]

LIVE_SENSORS: tuple[LiveDescription, ...] = (
    LiveDescription(
        key="milky_way", translation_key="milky_way", name=None, device_class=SensorDeviceClass.ENUM, options=STATUS_OPTIONS,
        value_fn=lambda s: s.status,
        attrs_fn=lambda s: {
            "elevation": round(s.gc.altitude, 2),
            "azimuth": round(s.gc.azimuth, 2),
            "direction": compass_point(s.gc.azimuth),
            "orientation": classify_orientation(s.gc.tilt),
            "tilt": round(s.gc.tilt, 1),
            "sun_elevation": round(s.sun_altitude, 2),
            "moon_elevation": round(s.moon_altitude, 2),
            "moon_illumination": round(s.moon_illumination * 100, 1),
            "next_window_start": s.window.gc_start.isoformat() if s.window and s.window.gc_start else None,
            "next_window_end": s.window.gc_end.isoformat() if s.window and s.window.gc_end else None,
        },
    ),
    LiveDescription(
        key="next_window_start", translation_key="next_window_start", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.window.gc_start if s.window else None,
    ),
    LiveDescription(
        key="next_window_end", translation_key="next_window_end", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.window.gc_end if s.window else None,
    ),
    LiveDescription(
        key="window_length", translation_key="window_length", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES, suggested_unit_of_measurement=UnitOfTime.HOURS, suggested_display_precision=1,
        value_fn=lambda s: s.window.gc_minutes if s.window else None,
        attrs_fn=lambda s: {"night": s.window.date.isoformat(), "orientation": s.window.orientation} if s.window else {},
    ),
    LiveDescription(
        key="next_good_night", translation_key="next_good_night", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.next_best.gc_start if s.next_best else None,
        attrs_fn=lambda s: (
            {
                "night": s.next_best.date.isoformat(),
                "window_end": s.next_best.gc_end.isoformat() if s.next_best.gc_end else None,
                "window_minutes": s.next_best.gc_minutes,
                "orientation": s.next_best.orientation,
                "moon_illumination": round(s.next_best.moon_illumination * 100),
            }
            if s.next_best
            else {}
        ),
    ),
    LiveDescription(
        key="tonight", translation_key="tonight", device_class=SensorDeviceClass.ENUM, options=["best", "short", "none"],
        value_fn=None,  # filled by the entity (needs the configured best-minutes threshold)
    ),
    # Galactic core
    LiveDescription(
        key="gc_elevation", translation_key="gc_elevation", device="galactic_core", native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1, value_fn=lambda s: round(s.gc.altitude, 2),
    ),
    LiveDescription(
        key="gc_azimuth", translation_key="gc_azimuth", device="galactic_core", native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1, value_fn=lambda s: round(s.gc.azimuth, 2),
    ),
    LiveDescription(
        key="gc_orientation", translation_key="gc_orientation", device="galactic_core", device_class=SensorDeviceClass.ENUM,
        options=ORIENTATIONS, value_fn=lambda s: classify_orientation(s.gc.tilt),
        attrs_fn=lambda s: {"tilt": round(s.gc.tilt, 1)},
    ),
    LiveDescription(
        key="gc_next_rising", translation_key="gc_next_rising", device="galactic_core", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.gc_next_rising,
    ),
    LiveDescription(
        key="gc_next_setting", translation_key="gc_next_setting", device="galactic_core", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.gc_next_setting,
    ),
    # Moon (rise/set aren't provided by the core Moon integration)
    LiveDescription(
        key="moon_next_rising", translation_key="moon_next_rising", device="moon", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.moon_next_rising,
    ),
    LiveDescription(
        key="moon_next_setting", translation_key="moon_next_setting", device="moon", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.moon_next_setting,
    ),
    LiveDescription(
        key="moon_elevation", translation_key="moon_elevation", device="moon", native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1, value_fn=lambda s: round(s.moon_altitude, 2),
    ),
    LiveDescription(
        key="moon_illumination", translation_key="moon_illumination", device="moon", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0, value_fn=lambda s: round(s.moon_illumination * 100, 1),
    ),
)

DEVICE_NAMES = {"milky_way": "Milky Way", "galactic_core": "Galactic core", "moon": "Moon"}


async def async_setup_entry(hass: HomeAssistant, entry: PlannerEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    entities: list[SensorEntity] = [LiveSensor(rt.live, entry.entry_id, d, rt.engine.config.calendar_best_minutes) for d in LIVE_SENSORS]
    entities.append(BestSiteSensor(rt.planner, entry.entry_id))
    entities += [SiteScoreSensor(rt.planner, entry.entry_id, site) for site in rt.engine.config.sites]
    async_add_entities(entities)


class LiveSensor(PlannerEntity, SensorEntity):
    entity_description: LiveDescription

    def __init__(self, coordinator, entry_id: str, description: LiveDescription, best_minutes: float) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._best_minutes = best_minutes
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = device_info(entry_id, description.device, DEVICE_NAMES[description.device], None if description.device == "milky_way" else "milky_way")

    @property
    def native_value(self) -> Any:
        s: LiveState = self.coordinator.data
        if self.entity_description.key == "tonight":
            return s.tonight.grade(self._best_minutes) if s.tonight else "none"
        return self.entity_description.value_fn(s)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        # Everything here is calculated by this integration from the sky (not read from AstroWeather).
        attrs = {**(fn(self.coordinator.data) if fn else {}), "source": "computed"}
        if self.entity_description.key == "milky_way":
            ref = self.coordinator.engine.config.reference
            attrs = {**attrs, "latitude": round(ref.latitude, 4), "longitude": round(ref.longitude, 4)}
        return attrs


class BestSiteSensor(PlannerEntity, SensorEntity):
    _attr_translation_key = "best_site"

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_best_site"
        self._attr_device_info = device_info(entry_id, "milky_way", "Milky Way")

    def _best(self):
        data: PlannerData = self.coordinator.data
        return data.best_site(data.tonight)

    @property
    def native_value(self) -> str | None:
        b = self._best()
        return b[0].name if b else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: PlannerData = self.coordinator.data
        b = self._best()
        attrs: dict[str, Any] = {
            "nights": [
                {"date": d.isoformat(), "site": best[0].name, "entity": best[0].entity, "score": best[1].score}
                for d in data.dates
                if (best := data.best_site(d))
            ]
        }
        if b:
            attrs |= {"entity": b[0].entity, "score": b[1].score, "band": score_band(b[1].score), "cloud": _r(b[1].cloud)}
        return attrs


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


class SiteScoreSensor(PlannerEntity, SensorEntity):
    _attr_translation_key = "site_score"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:star-four-points"

    def __init__(self, coordinator, entry_id: str, site: Site) -> None:
        super().__init__(coordinator)
        self._site = site
        self._attr_unique_id = f"{entry_id}_score_{site.entity}"
        self._attr_device_info = device_info(entry_id, f"site_{site.entity}", site.name, "milky_way")

    @property
    def native_value(self) -> int | None:
        data: PlannerData = self.coordinator.data
        sc = data.scores.get(self._site.entity, {}).get(data.tonight)
        return sc.score if sc else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: PlannerData = self.coordinator.data
        site = self._site
        nights = []
        for d in data.dates:
            sc = data.scores.get(site.entity, {}).get(d)
            if not sc:
                continue
            n = sc.night
            nights.append(
                {
                    "date": d.isoformat(),
                    "score": sc.score,
                    "band": score_band(sc.score),
                    "astro_only": sc.astro_only,
                    "cloud": _r(sc.cloud),
                    "window_start": n.gc_start.isoformat() if n.gc_start else None,
                    "window_end": n.gc_end.isoformat() if n.gc_end else None,
                    "window_minutes": n.gc_minutes,
                    "light_dome_warning": sc.dome_warning,
                }
            )
        tonight = data.scores.get(site.entity, {}).get(data.tonight)
        return {
            "site_entity": site.entity,
            "bortle": site.bortle,
            "drive_minutes": site.drive_minutes,
            # Where each input came from: the forecast is read from AstroWeather, the sky is computed here.
            "source": {
                "forecast": "astroweather" if data.forecast_end.get(site.entity) else None,
                "sky": "computed",
                "coordinates": site.coordinates_source,
                "bortle": site.bortle_source,
                "drive_time": site.drive_source,
            },
            "forecast_until": end.isoformat() if (end := data.forecast_end.get(site.entity)) else None,
            "astro_only": tonight.astro_only if tonight else None,
            "nights": nights,
        }
