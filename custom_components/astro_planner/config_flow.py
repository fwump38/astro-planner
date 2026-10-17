"""Config and options flow."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .cities import CityLookupError, LookupResult, async_find_domes, lookup_points
from .config import build_config
from .const import (
    CONF_AUTO_DRIVE,
    CONF_BORTLE,
    CONF_CAL_BEST_MINUTES,
    CONF_CAL_MIN_ALTITUDE,
    CONF_CUSTOM_TARGETS,
    CONF_DOME_SIGMA,
    CONF_DOME_WARNING,
    CONF_DRIVE_MINUTES,
    CONF_ENTITY,
    CONF_FOV,
    CONF_GOOD_SCORE,
    CONF_LATITUDE,
    CONF_LIGHT_DOMES,
    CONF_LIGHT_DOMES_INFO,
    CONF_LONGITUDE,
    CONF_MAX_MOON,
    CONF_MIN_GC_ALTITUDE,
    CONF_NAME,
    CONF_NIGHTS,
    CONF_SITES,
    CONF_TARGET_MAX_MAGNITUDE,
    CONF_TARGET_MIN_ALTITUDE,
    CONF_WATCH_TARGETS,
    CONF_WEIGHTS,
    DEFAULTS,
    DOMAIN,
)
from .scoring import DEFAULT_WEIGHTS
from .targets import MILKY_WAY, all_targets, custom_targets


def _num(lo: float, hi: float, step: float | str = 1, unit: str | None = None) -> selector.NumberSelector:
    cfg: dict[str, Any] = {"min": lo, "max": hi, "step": step, "mode": selector.NumberSelectorMode.BOX}
    if unit:
        cfg["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**cfg))


SITE_PICKER = selector.EntitySelector(selector.EntitySelectorConfig(domain="weather", integration="astroweather", multiple=True))
CONF_REVIEW = "review_sites"


def discover_sites(hass: HomeAssistant) -> list[str]:
    """Every AstroWeather weather entity, so setup can pre-select them all."""
    reg = er.async_get(hass)
    return sorted(e.entity_id for e in reg.entities.values() if e.platform == "astroweather" and e.domain == "weather" and not e.disabled)


_SITE_NUMBERS = (CONF_BORTLE, CONF_DRIVE_MINUTES, CONF_LATITUDE, CONF_LONGITUDE)


def clean_sites(rows: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Validate a hand-edited site list: each needs a weather entity; numbers must be in range; unknown keys are dropped."""
    out: list[dict[str, Any]] = []
    try:
        for row in rows:
            entity = str(row[CONF_ENTITY])
            if not entity.startswith("weather."):
                return None
            site: dict[str, Any] = {CONF_ENTITY: entity}
            if row.get(CONF_NAME):
                site[CONF_NAME] = str(row[CONF_NAME])
            for key in _SITE_NUMBERS:
                if row.get(key) not in (None, ""):
                    site[key] = float(row[key]) if key in (CONF_LATITUDE, CONF_LONGITUDE) else int(row[key])
            if not 1 <= site.get(CONF_BORTLE, 1) <= 9 or not -90 <= site.get(CONF_LATITUDE, 0) <= 90 or not -180 <= site.get(CONF_LONGITUDE, 0) <= 180:
                return None
            if (CONF_LATITUDE in site) != (CONF_LONGITUDE in site):
                return None
            out.append(site)
    except (KeyError, TypeError, ValueError):
        return None
    return out


class SitesFlowMixin:
    """Pick AstroWeather sites, then ask for each site's details."""

    _sites: list[dict[str, Any]]
    _pending: list[str]
    _existing: dict[str, dict[str, Any]]

    def _site_form_schema(self, existing: dict[str, Any]) -> vol.Schema:
        def opt(key: str, default: Any = None):
            return vol.Optional(key, description={"suggested_value": existing.get(key, default)})

        return vol.Schema(
            {
                opt(CONF_NAME): selector.TextSelector(),
                opt(CONF_BORTLE): _num(1, 9, 1),
                opt(CONF_DRIVE_MINUTES): _num(0, 1440, 5, "min"),
                opt(CONF_LATITUDE): _num(-90, 90, "any"),
                opt(CONF_LONGITUDE): _num(-180, 180, "any"),
            }
        )

    async def _next_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._pending:
            return self.async_show_form(  # type: ignore[attr-defined]
                step_id="site",
                data_schema=self._site_form_schema(self._existing.get(self._pending[0], {})),
                description_placeholders={"site": self._pending[0]},
            )
        return await self._sites_done()  # type: ignore[attr-defined]

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            entity = self._pending.pop(0)
            self._sites.append({CONF_ENTITY: entity, **{k: v for k, v in user_input.items() if v not in (None, "")}})
        return await self._next_site()

    def _start_sites(self, entities: list[str], existing: list[dict[str, Any]], review: bool = True) -> None:
        self._existing = {s[CONF_ENTITY]: s for s in existing}
        if review:
            self._sites = []
            self._pending = list(entities)
        else:
            # Skip the per-site forms: keep what was already entered and leave the rest to AstroWeather and the defaults.
            self._sites = [self._existing.get(e, {CONF_ENTITY: e}) for e in entities]
            self._pending = []

    def _sites_schema(self, selected: list[str]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(CONF_SITES, default=selected): SITE_PICKER,
                vol.Optional(CONF_REVIEW, default=False): selector.BooleanSelector(),
            }
        )


class AstroPlannerConfigFlow(SitesFlowMixin, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AstroPlannerOptionsFlow:
        return AstroPlannerOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._start_sites(user_input.get(CONF_SITES, []), [], user_input.get(CONF_REVIEW, False))
            return await self._next_site()
        return self.async_show_form(step_id="user", data_schema=self._sites_schema(discover_sites(self.hass)))

    async def _sites_done(self) -> ConfigFlowResult:
        return self.async_create_entry(title="Astro Planner", data={}, options={CONF_SITES: self._sites})


class AstroPlannerOptionsFlow(SitesFlowMixin, OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["sites", "edit_sites", "sky", "targets", "custom_targets", "scoring", "domes"])

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    def _opt(self, key: str) -> Any:
        return self.config_entry.options.get(key, DEFAULTS.get(key))

    # -- sites -------------------------------------------------------------------------------

    async def async_step_sites(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self.config_entry.options.get(CONF_SITES, [])
        if user_input is not None:
            self._start_sites(user_input.get(CONF_SITES, []), current, user_input.get(CONF_REVIEW, False))
            return await self._next_site()
        return self.async_show_form(step_id="sites", data_schema=self._sites_schema([s[CONF_ENTITY] for s in current]))

    async def _sites_done(self) -> ConfigFlowResult:
        return self._save(**{CONF_SITES: self._sites})

    async def async_step_edit_sites(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """All sites and their details as one list, to edit, back up or restore in a single step."""
        errors: dict[str, str] = {}
        current = self.config_entry.options.get(CONF_SITES, [])
        if user_input is not None:
            cleaned = clean_sites(user_input.get(CONF_SITES) or [])
            if cleaned is None:
                errors["base"] = "invalid_sites"
            else:
                return self._save(**{CONF_SITES: cleaned})
        schema = vol.Schema({vol.Optional(CONF_SITES, default=current): selector.ObjectSelector()})
        return self.async_show_form(step_id="edit_sites", data_schema=schema, errors=errors)

    # -- sky and calendar thresholds -----------------------------------------------------------

    async def async_step_sky(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(**user_input)
        d = self._opt
        schema = vol.Schema(
            {
                vol.Required(CONF_MIN_GC_ALTITUDE, default=d(CONF_MIN_GC_ALTITUDE)): _num(0, 45, 1, "°"),
                vol.Required(CONF_MAX_MOON, default=d(CONF_MAX_MOON)): _num(0, 100, 1, "%"),
                vol.Required(CONF_CAL_MIN_ALTITUDE, default=d(CONF_CAL_MIN_ALTITUDE)): _num(0, 45, 1, "°"),
                vol.Required(CONF_CAL_BEST_MINUTES, default=d(CONF_CAL_BEST_MINUTES)): _num(15, 480, 15, "min"),
                vol.Required(CONF_NIGHTS, default=d(CONF_NIGHTS)): _num(1, 7, 1),
                vol.Required(CONF_GOOD_SCORE, default=d(CONF_GOOD_SCORE)): _num(1, 100, 1),
                vol.Required(CONF_AUTO_DRIVE, default=d(CONF_AUTO_DRIVE)): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="sky", data_schema=schema)

    # -- targets -------------------------------------------------------------------------------

    async def async_step_targets(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(**user_input)
        d = self._opt
        custom = self.config_entry.options.get(CONF_CUSTOM_TARGETS)
        choices = [selector.SelectOptionDict(value=t.id, label=t.label) for t in [MILKY_WAY, *all_targets(custom)]]
        valid = {c["value"] for c in choices}
        watched = [t for t in (self.config_entry.options.get(CONF_WATCH_TARGETS) or []) if t in valid]
        schema = vol.Schema(
            {
                vol.Required(CONF_TARGET_MIN_ALTITUDE, default=d(CONF_TARGET_MIN_ALTITUDE)): _num(10, 80, 1, "°"),
                vol.Required(CONF_TARGET_MAX_MAGNITUDE, default=d(CONF_TARGET_MAX_MAGNITUDE)): _num(4, 16, 0.5),
                vol.Required(CONF_FOV, default=d(CONF_FOV)): _num(0, 1200, 1, "arcmin"),
                vol.Optional(CONF_WATCH_TARGETS, default=watched): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=choices, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
            }
        )
        return self.async_show_form(step_id="targets", data_schema=schema)

    async def async_step_custom_targets(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options.get(CONF_CUSTOM_TARGETS, [])
        if user_input is not None:
            rows = user_input.get(CONF_CUSTOM_TARGETS) or []
            if len(custom_targets(rows)) != len(rows):
                errors["base"] = "invalid_targets"
            else:
                return self._save(**{CONF_CUSTOM_TARGETS: rows})
        schema = vol.Schema({vol.Optional(CONF_CUSTOM_TARGETS, default=current): selector.ObjectSelector()})
        return self.async_show_form(step_id="custom_targets", data_schema=schema, errors=errors)

    async def async_step_scoring(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(**{CONF_WEIGHTS: user_input})
        current = {**DEFAULT_WEIGHTS, **self.config_entry.options.get(CONF_WEIGHTS, {})}
        schema = vol.Schema({vol.Required(k, default=current[k]): _num(0, 10, 0.5) for k in DEFAULT_WEIGHTS})
        return self.async_show_form(step_id="scoring", data_schema=schema)

    # -- light domes -------------------------------------------------------------------------

    _lookup_task: asyncio.Task[LookupResult] | None = None
    _lookup_radius: float = 250
    _found: LookupResult | None = None

    async def async_step_domes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="domes", menu_options=["find_domes", "edit_domes"])

    async def async_step_find_domes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Look up nearby cities (GeoNames, then Wikidata) around Home Assistant and every located site."""
        if self._lookup_task is None:
            if user_input is None:
                schema = vol.Schema({vol.Required("radius_km", default=250): _num(25, 600, 25, "km")})
                return self.async_show_form(step_id="find_domes", data_schema=schema)
            self._lookup_radius = float(user_input["radius_km"])
            cfg = build_config(self.hass, dict(self.config_entry.options))
            points = lookup_points(cfg.home, [s.observer for s in cfg.sites if s.observer])
            self._lookup_task = self.hass.async_create_task(async_find_domes(self.hass, points, self._lookup_radius))
        if not self._lookup_task.done():
            return self.async_show_progress(step_id="find_domes", progress_action="lookup", progress_task=self._lookup_task)
        task, self._lookup_task = self._lookup_task, None
        try:
            self._found = task.result()
        except CityLookupError as err:
            self._lookup_error = "; ".join(err.failures)
            return self.async_show_progress_done(next_step_id="find_failed")
        return self.async_show_progress_done(next_step_id="review_domes")

    _lookup_error: str = ""

    async def async_step_find_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="lookup_failed", description_placeholders={"errors": self._lookup_error})

    async def async_step_review_domes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        found = self._found
        assert found is not None
        existing = list(self.config_entry.options.get(CONF_LIGHT_DOMES, []))
        if user_input is not None:
            domes = self._clean_domes(user_input.get(CONF_LIGHT_DOMES) or [])
            if domes is None:
                return self._review_form(existing, found, {"base": "invalid_domes"})
            info = {"source": found.source, "updated": datetime.now(UTC).isoformat(timespec="seconds"), "radius_km": self._lookup_radius, "count": len(domes)}
            return self._save(**{CONF_LIGHT_DOMES: domes, CONF_LIGHT_DOMES_INFO: info})
        return self._review_form(existing, found, {})

    def _review_form(self, existing: list[dict[str, Any]], found: LookupResult, errors: dict[str, str]) -> ConfigFlowResult:
        # Keep the user's existing entries (manual edits win on a name clash) and add what was found.
        names = {str(d["name"]).lower() for d in existing}
        merged = existing + [
            {"name": d.name, "latitude": d.latitude, "longitude": d.longitude, "population": d.population}
            for d in found.domes
            if d.name.lower() not in names
        ]
        schema = vol.Schema({vol.Optional(CONF_LIGHT_DOMES, default=merged): selector.ObjectSelector()})
        return self.async_show_form(
            step_id="review_domes",
            data_schema=schema,
            errors=errors,
            description_placeholders={"source": found.source, "found": str(len(found.domes)), "considered": str(found.places_considered), "radius": f"{self._lookup_radius:g}"},
        )

    @staticmethod
    def _clean_domes(domes: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        try:
            return [
                {"name": str(d["name"]), "latitude": float(d["latitude"]), "longitude": float(d["longitude"]), "population": float(d.get("population", 0))}
                for d in domes
            ]
        except (KeyError, TypeError, ValueError):
            return None

    async def async_step_edit_domes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = self._clean_domes(user_input.get(CONF_LIGHT_DOMES) or [])
            if cleaned is None:
                errors["base"] = "invalid_domes"
            else:
                changes: dict[str, Any] = {CONF_LIGHT_DOMES: cleaned, CONF_DOME_SIGMA: user_input[CONF_DOME_SIGMA], CONF_DOME_WARNING: user_input[CONF_DOME_WARNING]}
                if cleaned != self.config_entry.options.get(CONF_LIGHT_DOMES, []):
                    changes[CONF_LIGHT_DOMES_INFO] = {**self.config_entry.options.get(CONF_LIGHT_DOMES_INFO, {}), "source": "manual", "count": len(cleaned)}
                return self._save(**changes)
        schema = vol.Schema(
            {
                vol.Optional(CONF_LIGHT_DOMES, default=self.config_entry.options.get(CONF_LIGHT_DOMES, [])): selector.ObjectSelector(),
                vol.Required(CONF_DOME_SIGMA, default=self._opt(CONF_DOME_SIGMA)): _num(5, 90, 1, "°"),
                vol.Required(CONF_DOME_WARNING, default=self._opt(CONF_DOME_WARNING)): _num(0.01, 1, 0.01),
            }
        )
        return self.async_show_form(step_id="edit_domes", data_schema=schema, errors=errors)
