"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_DRIVE,
    CONF_BORTLE,
    CONF_CAL_BEST_MINUTES,
    CONF_CAL_MIN_ALTITUDE,
    CONF_DOME_SIGMA,
    CONF_DOME_WARNING,
    CONF_DRIVE_MINUTES,
    CONF_ENTITY,
    CONF_GOOD_SCORE,
    CONF_LATITUDE,
    CONF_LIGHT_DOMES,
    CONF_LONGITUDE,
    CONF_MAX_MOON,
    CONF_MIN_GC_ALTITUDE,
    CONF_NAME,
    CONF_NIGHTS,
    CONF_SITES,
    CONF_WEIGHTS,
    DEFAULT_BORTLE,
    DEFAULTS,
    DOMAIN,
)
from .scoring import DEFAULT_WEIGHTS


def _num(lo: float, hi: float, step: float | str = 1, unit: str | None = None) -> selector.NumberSelector:
    cfg: dict[str, Any] = {"min": lo, "max": hi, "step": step, "mode": selector.NumberSelectorMode.BOX}
    if unit:
        cfg["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**cfg))


SITE_PICKER = selector.EntitySelector(selector.EntitySelectorConfig(domain="weather", integration="astroweather", multiple=True))


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
                vol.Required(CONF_BORTLE, default=existing.get(CONF_BORTLE, DEFAULT_BORTLE)): _num(1, 9, 1),
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

    def _start_sites(self, entities: list[str], existing: list[dict[str, Any]]) -> None:
        self._sites = []
        self._pending = list(entities)
        self._existing = {s[CONF_ENTITY]: s for s in existing}


class MilkyWayPlannerConfigFlow(SitesFlowMixin, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MilkyWayPlannerOptionsFlow:
        return MilkyWayPlannerOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            self._start_sites(user_input.get(CONF_SITES, []), [])
            return await self._next_site()
        return self.async_show_form(step_id="user", data_schema=vol.Schema({vol.Optional(CONF_SITES, default=[]): SITE_PICKER}))

    async def _sites_done(self) -> ConfigFlowResult:
        return self.async_create_entry(title="Milky Way Planner", data={}, options={CONF_SITES: self._sites})


class MilkyWayPlannerOptionsFlow(SitesFlowMixin, OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["sites", "sky", "scoring", "domes"])

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    def _opt(self, key: str) -> Any:
        return self.config_entry.options.get(key, DEFAULTS.get(key))

    # -- sites -------------------------------------------------------------------------------

    async def async_step_sites(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self.config_entry.options.get(CONF_SITES, [])
        if user_input is not None:
            self._start_sites(user_input.get(CONF_SITES, []), current)
            return await self._next_site()
        schema = vol.Schema({vol.Optional(CONF_SITES, default=[s[CONF_ENTITY] for s in current]): SITE_PICKER})
        return self.async_show_form(step_id="sites", data_schema=schema)

    async def _sites_done(self) -> ConfigFlowResult:
        return self._save(**{CONF_SITES: self._sites})

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

    async def async_step_scoring(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(**{CONF_WEIGHTS: user_input})
        current = {**DEFAULT_WEIGHTS, **self.config_entry.options.get(CONF_WEIGHTS, {})}
        schema = vol.Schema({vol.Required(k, default=current[k]): _num(0, 10, 0.5) for k in DEFAULT_WEIGHTS})
        return self.async_show_form(step_id="scoring", data_schema=schema)

    async def async_step_domes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            domes = user_input.get(CONF_LIGHT_DOMES) or []
            try:
                cleaned = [
                    {"name": str(d["name"]), "latitude": float(d["latitude"]), "longitude": float(d["longitude"]), "population": float(d.get("population", 0))}
                    for d in domes
                ]
            except (KeyError, TypeError, ValueError):
                errors["base"] = "invalid_domes"
            else:
                return self._save(**{CONF_LIGHT_DOMES: cleaned, CONF_DOME_SIGMA: user_input[CONF_DOME_SIGMA], CONF_DOME_WARNING: user_input[CONF_DOME_WARNING]})
        schema = vol.Schema(
            {
                vol.Optional(CONF_LIGHT_DOMES, default=self.config_entry.options.get(CONF_LIGHT_DOMES, [])): selector.ObjectSelector(),
                vol.Required(CONF_DOME_SIGMA, default=self._opt(CONF_DOME_SIGMA)): _num(5, 90, 1, "°"),
                vol.Required(CONF_DOME_WARNING, default=self._opt(CONF_DOME_WARNING)): _num(0.01, 1, 0.01),
            }
        )
        return self.async_show_form(step_id="domes", data_schema=schema, errors=errors)
