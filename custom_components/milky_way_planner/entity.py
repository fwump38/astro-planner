"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN


def device_info(entry_id: str, key: str, name: str, via: str | None = None) -> DeviceInfo:
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{key}")},
        name=name,
        manufacturer="Milky Way Planner",
        entry_type=DeviceEntryType.SERVICE,
    )
    if via:
        info["via_device"] = (DOMAIN, f"{entry_id}_{via}")
    return info


class PlannerEntity(CoordinatorEntity[DataUpdateCoordinator]):
    _attr_has_entity_name = True
