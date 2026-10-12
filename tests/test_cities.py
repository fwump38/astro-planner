import io
import json
import zipfile
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.milky_way_planner import cities
from custom_components.milky_way_planner.astro import Observer
from custom_components.milky_way_planner.cities import (
    CityLookupError,
    Place,
    async_find_domes,
    domes_from_places,
    merge_metros,
    parse_geonames,
    places_from_sparql,
)

CHACO = Observer(36.06, -107.96, 1890)
ABIQUIU = Observer(36.33, -106.47, 1900)


def row(name, lat, lon, pop, fclass="P", fcode="PPL"):
    cols = ["1", name, name, "", str(lat), str(lon), fclass, fcode, "US", "", "NM", "001", "", "", str(pop), "", "1500", "America/Denver", "2026-01-01"]
    return "\t".join(cols)


# Synthetic rows in GeoNames' cities1000 layout (coordinates approximate): a metro with two close suburbs, a separate city, junk rows.
SAMPLE = [
    row("Albuquerque", 35.0844, -106.6511, 564559, fcode="PPLA2"),
    row("Corrales", 35.2378, -106.6070, 8500),  # ~17 km from Albuquerque: folds into the metro
    row("Los Ranchos de Albuquerque", 35.1595, -106.6317, 6000),  # ~9 km: folds in
    row("Rio Rancho", 35.2328, -106.6630, 104000),  # ~19 km: a metro in its own right, but within 20 km of the larger anchor
    row("Santa Fe", 35.6870, -105.9378, 89008, fcode="PPLA"),
    row("Tinytown", 35.0, -104.0, 1500),  # too small and far to matter
    row("Mount Taylor", 35.2, -107.6, 50000, fclass="T", fcode="MT"),  # not a populated place
    row("Hamlet", 35.1, -106.6, 400),  # under the population floor
    "garbage line",
]


def test_parse_geonames_keeps_populated_places_in_range():
    places = parse_geonames(SAMPLE, [CHACO, ABIQUIU], 250)
    names = {p.name for p in places}
    assert {"Albuquerque", "Santa Fe", "Rio Rancho", "Corrales"} <= names
    assert not names & {"Mount Taylor", "Hamlet", "Tinytown"}  # wrong class, too small, outside the radius
    assert all(p.population >= 1000 for p in places)


def test_merge_metros_folds_suburbs_into_the_largest_place():
    merged = {p.name: p for p in merge_metros(parse_geonames(SAMPLE, [CHACO], 250))}
    abq = merged["Albuquerque"]
    assert abq.population == 564559 + 8500 + 6000 + 104000
    assert abs(abq.latitude - 35.0844) < 0.1  # population-weighted: barely moves from the big city
    assert "Corrales" not in merged and "Rio Rancho" not in merged
    assert merged["Santa Fe"].population == 89008  # 80+ km away: untouched


def test_merge_does_not_chain_through_a_string_of_towns():
    # Towns every 15 km along a line: each is within 20 km of its neighbours, but only the anchor's direct neighbours may join it.
    towns = [Place(f"T{i}", 36.0, -108.0 + i * 0.17, 10000 - i * 100) for i in range(8)]
    merged = merge_metros(towns)
    assert len(merged) >= 3
    assert sum(p.population for p in merged) == sum(t.population for t in towns)


def test_domes_from_places_ranks_by_glow_and_limits_per_point():
    metros = merge_metros(parse_geonames(SAMPLE, [CHACO, ABIQUIU], 250))
    domes = domes_from_places(metros, [CHACO, ABIQUIU], 250)
    assert domes[0].name in ("Albuquerque", "Santa Fe")
    assert {d.name for d in domes} == {"Albuquerque", "Santa Fe"}
    many = [Place(f"P{i}", 36.06 + i * 0.01, -107.9, 20000) for i in range(40)]
    assert len(domes_from_places(many, [CHACO], 250)) == 12  # PER_POINT_LIMIT


def test_places_from_sparql_reads_the_real_wikidata_response():
    data = json.loads((Path(__file__).parent / "fixtures" / "wikidata.json").read_text())
    places = places_from_sparql(data)
    assert places and any(p.name == "Albuquerque" and p.population > 500000 for p in places)


def _zip(lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cities1000.txt", "\n".join(lines) + "\n")
    return buf.getvalue()


@pytest.fixture
def no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cities, "cache_path", lambda hass: tmp_path / "cities1000.zip")
    return tmp_path / "cities1000.zip"


async def test_lookup_uses_geonames_first_and_caches_the_dump(hass: HomeAssistant, aioclient_mock, no_cache):
    aioclient_mock.get(cities.GEONAMES_URL, content=_zip(SAMPLE))
    result = await async_find_domes(hass, [CHACO, ABIQUIU], 250)
    assert result.source == "geonames"
    assert {d.name for d in result.domes} == {"Albuquerque", "Santa Fe"}
    assert aioclient_mock.mock_calls[0][3]["User-Agent"].startswith("milky-way-planner")
    assert no_cache.exists()
    # Second lookup reads the cached dump: no further request.
    await async_find_domes(hass, [CHACO], 250)
    assert aioclient_mock.call_count == 1


async def test_lookup_falls_back_to_wikidata(hass: HomeAssistant, aioclient_mock, no_cache):
    aioclient_mock.get(cities.GEONAMES_URL, status=503)
    aioclient_mock.get(cities.WIKIDATA_URL, text=(Path(__file__).parent / "fixtures" / "wikidata.json").read_text())
    result = await async_find_domes(hass, [CHACO, ABIQUIU], 250)
    assert result.source == "wikidata"
    assert result.failures == ["geonames: HTTP 503"]
    assert any(d.name == "Albuquerque" for d in result.domes)
    assert not no_cache.exists()


async def test_corrupt_download_is_not_cached_and_falls_through(hass: HomeAssistant, aioclient_mock, no_cache):
    aioclient_mock.get(cities.GEONAMES_URL, content=b"<html>oops</html>")
    aioclient_mock.get(cities.WIKIDATA_URL, status=500)
    with pytest.raises(CityLookupError) as err:
        await async_find_domes(hass, [CHACO], 250)
    assert len(err.value.failures) == 2
    assert not no_cache.exists()


# -- options flow and websocket ------------------------------------------------------------


async def test_options_flow_finds_and_stores_domes(hass: HomeAssistant, aioclient_mock, no_cache):
    from homeassistant.data_entry_flow import FlowResultType
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.milky_way_planner.const import DOMAIN

    aioclient_mock.get(cities.GEONAMES_URL, content=_zip(SAMPLE))
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}, options={"sites": [], "light_domes": [{"name": "Manual", "latitude": 35, "longitude": -106, "population": 5000}]})
    entry.add_to_hass(hass)
    hass.config.latitude, hass.config.longitude = 36.06, -107.96
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "domes"})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "find_domes"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"radius_km": 250})
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["step_id"] == "review_domes"
    default = next(k for k in result["data_schema"].schema if k == "light_domes").default()
    assert [d["name"] for d in default] == ["Manual", "Albuquerque", "Santa Fe"]  # existing entries kept, new ones added
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"light_domes": default})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    info = result["data"]["light_domes_info"]
    assert info["source"] == "geonames" and info["count"] == 3 and info["radius_km"] == 250


async def test_options_flow_reports_lookup_failure(hass: HomeAssistant, aioclient_mock, no_cache):
    from homeassistant.data_entry_flow import FlowResultType
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.milky_way_planner.const import DOMAIN

    aioclient_mock.get(cities.GEONAMES_URL, status=500)
    aioclient_mock.get(cities.WIKIDATA_URL, status=500)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}, options={"sites": []})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "domes"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "find_domes"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"radius_km": 100})
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "lookup_failed"


async def test_websocket_lookup_and_stored_domes(hass: HomeAssistant, hass_ws_client, aioclient_mock, no_cache, hass_admin_user):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.milky_way_planner.const import DOMAIN

    stored = [{"name": "Albuquerque", "latitude": 35.0844, "longitude": -106.6511, "population": 600000}]
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}, options={"sites": [], "light_domes": stored, "light_domes_info": {"source": "geonames"}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    aioclient_mock.get(cities.GEONAMES_URL, content=_zip(SAMPLE))
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "milky_way_planner/light_domes"})
    res = (await client.receive_json())["result"]
    assert res["domes"] == stored and res["info"] == {"source": "geonames"}
    await client.send_json_auto_id({"type": "milky_way_planner/find_light_domes", "points": [{"latitude": 36.06, "longitude": -107.96}], "radius_km": 250})
    res = (await client.receive_json())["result"]
    assert res["source"] == "geonames" and {d["name"] for d in res["domes"]} == {"Albuquerque", "Santa Fe"}


async def test_metro_outside_the_radius_still_absorbs_suburbs_inside_it(hass: HomeAssistant, aioclient_mock, no_cache):
    """A big city just beyond the radius must fold in its suburbs that fall inside it (like Las Vegas for a site near Kanab)."""
    site = Observer(37.0, -112.5)
    # Centre ~262 km east of the site (outside 250); the suburb is ~12 km nearer the site (inside 250) and within 20 km of the centre.
    centre_lon = -112.5 + 262 / (111.32 * 0.7986)
    suburb_lon = centre_lon - 12 / (111.32 * 0.7986)
    rows = [row("Bigville", 37.0, centre_lon, 900000), row("Suburbia", 37.0, suburb_lon, 120000)]
    aioclient_mock.get(cities.GEONAMES_URL, content=_zip(rows))
    result = await async_find_domes(hass, [site], 250)
    assert "Suburbia" not in {d.name for d in result.domes}  # absorbed into Bigville, which is beyond the radius
    # With a larger radius the merged metro appears once, with the combined population.
    result = await async_find_domes(hass, [site], 300)
    assert [(d.name, d.population) for d in result.domes] == [("Bigville", 1020000)]
