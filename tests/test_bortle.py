from custom_components.astro_planner.astro import Observer
from custom_components.astro_planner.bortle import bortle_from_sqm, estimate_bortle, sky_brightness
from custom_components.astro_planner.cities import Place
from custom_components.astro_planner.config import build_config

SITE = Observer(37.05, -112.5)


def test_remote_site_is_dark():
    assert estimate_bortle(SITE, [Place("Tiny", 38.5, -112.5, 1500)]) <= 2
    assert estimate_bortle(SITE, []) == 1


def test_big_city_nearby_is_bright():
    assert estimate_bortle(SITE, [Place("Metropolis", 37.1, -112.5, 1_500_000)]) >= 8


def test_more_people_or_less_distance_never_darkens():
    near = sky_brightness(SITE, [Place("A", 37.3, -112.5, 50_000)])
    far = sky_brightness(SITE, [Place("A", 38.3, -112.5, 50_000)])
    bigger = sky_brightness(SITE, [Place("A", 37.3, -112.5, 200_000)])
    assert bigger < near < far < 22.0


def test_class_boundaries():
    assert bortle_from_sqm(22.0) == 1 and bortle_from_sqm(21.8) == 3 and bortle_from_sqm(20.0) == 6 and bortle_from_sqm(16.0) == 9


async def test_build_config_prefers_override_then_estimate_then_default(hass):
    hass.config.latitude, hass.config.longitude = 37.0, -112.0
    opts = {"sites": [{"entity": "weather.a", "latitude": 37.1, "longitude": -112.4, "bortle": 2}, {"entity": "weather.b", "latitude": 37.2, "longitude": -112.4}]}
    cfg = build_config(hass, opts, bortle_lookup=lambda o: 5)
    assert [(s.bortle, s.bortle_source) for s in cfg.sites] == [(2, "configured"), (5, "estimated")]
    cfg = build_config(hass, opts, bortle_lookup=lambda o: None)
    assert cfg.sites[1].bortle == 3 and cfg.sites[1].bortle_source == "default"
