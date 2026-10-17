"""Binary sensors: is the Milky Way core usable right now, and is it above the horizon."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LiveState
from .entity import PlannerEntity, device_info
from .runtime import PlannerEntry


async def async_setup_entry(hass: HomeAssistant, entry: PlannerEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    live = entry.runtime_data.live
    async_add_entities(
        [
            MilkyWayBinary(live, entry.entry_id, "visible", "milky_way", "Milky Way", lambda s: s.visible, "mdi:eye"),
            MilkyWayBinary(live, entry.entry_id, "above_horizon", "galactic_core", "Galactic core", lambda s: s.gc_up, "mdi:arrow-up-circle-outline"),
        ]
    )


class MilkyWayBinary(PlannerEntity, BinarySensorEntity):
    def __init__(self, coordinator, entry_id: str, key: str, device: str, device_name: str, fn, icon: str) -> None:
        super().__init__(coordinator)
        self._fn = fn
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_bin_{key}"
        self._attr_device_info = device_info(entry_id, device, device_name, None if device == "milky_way" else "milky_way")

    @property
    def is_on(self) -> bool:
        state: LiveState = self.coordinator.data
        return bool(self._fn(state))
