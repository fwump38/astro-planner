"""Runtime data shared by the platforms."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .bortle import BortleEstimates
from .coordinator import LiveCoordinator, PlannerCoordinator
from .drive import DriveTimes
from .engine import Engine


@dataclass
class PlannerRuntime:
    engine: Engine
    planner: PlannerCoordinator
    live: LiveCoordinator
    drive: DriveTimes
    bortle: BortleEstimates


type PlannerEntry = ConfigEntry[PlannerRuntime]
