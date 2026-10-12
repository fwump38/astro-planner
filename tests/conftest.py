import json
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


FORECAST = json.loads((Path(__file__).parent / "fixtures" / "astroweather-forecast.json").read_text())["forecast"]
