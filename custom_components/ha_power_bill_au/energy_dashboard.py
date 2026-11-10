"""Select a contract's lifetime cost sensors in the Energy dashboard, on request.

The Energy dashboard's preferences belong to Home Assistant's energy integration.
This only fills an empty cost field of a grid meter the user already added there,
and only from the options flow after the user confirms.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_EXPORT_ENTITY, CONF_IMPORT_ENTITY, DOMAIN

ADD = "add"  # the cost field is empty and will be set
DONE = "done"  # the cost field already uses this contract's sensor
BLOCKED = "blocked"  # the user chose another cost sensor or a price
MISSING = "missing"  # the meter is not a grid source in the Energy dashboard


@dataclass(frozen=True)
class Link:
    """One grid meter and the cost sensor that belongs to it."""

    meter: str
    sensor: str | None
    status: str


def _slots(
    source: dict[str, Any], export: bool
) -> Iterator[tuple[dict[str, Any], tuple[str, str]]]:
    """Yield each grid meter entry of one direction with its price field names."""
    if ("stat_energy_to" if export else "stat_energy_from") in source:
        suffix = "_export" if export else ""
        yield source, (f"entity_energy_price{suffix}", f"number_energy_price{suffix}")
    # Home Assistant releases before the unified grid source keep a list per direction.
    for flow in source.get("flow_to" if export else "flow_from", ()):
        yield flow, ("entity_energy_price", "number_energy_price")


def link_sensor(
    sources: list[dict[str, Any]], meter: str, sensor: str, *, export: bool
) -> str:
    """Set `sensor` as the cost of the grid entry metering `meter`, if that field is free."""
    meter_key = "stat_energy_to" if export else "stat_energy_from"
    stat_key = "stat_compensation" if export else "stat_cost"
    status = MISSING
    for source in sources:
        if source.get("type") != "grid":
            continue
        for slot, price_keys in _slots(source, export):
            if slot.get(meter_key) != meter:
                continue
            if slot.get(stat_key) == sensor:
                return DONE
            if slot.get(stat_key) or any(
                slot.get(key) is not None for key in price_keys
            ):
                status = BLOCKED
                continue
            slot[stat_key] = sensor
            return ADD
    return status


async def async_plan(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[list[Link], list[dict[str, Any]]]:
    """Return what would change, and the energy sources with those changes applied."""
    manager = await async_get_manager(hass)
    sources = copy.deepcopy(list((manager.data or {}).get("energy_sources", [])))
    registry = er.async_get(hass)
    links: list[Link] = []
    for conf, key, export in (
        (CONF_IMPORT_ENTITY, "import_cost_total", False),
        (CONF_EXPORT_ENTITY, "export_credit_total", True),
    ):
        if not (meter := entry.data.get(conf)):
            continue
        sensor = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}"
        )
        status = (
            link_sensor(sources, meter, sensor, export=export) if sensor else MISSING
        )
        links.append(Link(meter, sensor, status))
    return links, sources


async def async_save(hass: HomeAssistant, sources: list[dict[str, Any]]) -> None:
    """Store the planned energy sources."""
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": sources})  # type: ignore[typeddict-item]
