"""Cost-per-mode regressions found in review: saving, database errors, two devices."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant.common import MockConfigEntry
from sqlalchemy.exc import OperationalError

from custom_components.ha_power_bill_au import runtime as runtime_module
from custom_components.ha_power_bill_au.const import DOMAIN

IMPORT = "sensor.modes_import"
AC = "sensor.modes_ac"
AC2 = "sensor.modes_ac2"
CLIMATE = "climate.modes_ac"
ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}
MODES = {"hvac_modes": ["off", "cool", "heat"]}
STATES = "custom_components.ha_power_bill_au.runtime.get_significant_states"


@pytest.fixture(autouse=True)
async def _inputs(recorder_mock, enable_custom_integrations, hass):
    for entity, value in ((IMPORT, "100"), (AC, "10"), (AC2, "10")):
        hass.states.async_set(entity, value, ATTRS)
    hass.states.async_set(CLIMATE, "cool", MODES)


async def _setup(hass, devices=(AC,), expect=True):
    plan = {
        "effective_from": "2000-01-01",
        "import_bands": [{"name": "flat", "start": 0, "end": 24, "rate": 0.2}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Modes",
        data={"provider": "Modes", "import_entity": IMPORT},
        options={
            "plans": [plan],
            "tracked_devices": list(devices),
            "device_modes": {
                d: {"entity_id": CLIMATE, "registry_id": None} for d in devices
            },
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is expect
    await hass.async_block_till_done()
    return entry


async def test_p1_modes_are_written_to_disk_while_readings_keep_arriving(
    hass, monkeypatch
):
    monkeypatch.setattr(runtime_module, "SAVE_DELAY_SECONDS", 0.5)  # 60 s in real life
    entry = await _setup(hass)
    rt = entry.runtime_data
    await asyncio.sleep(0.8)  # let the save scheduled at startup happen
    writes = 0
    snapshot = rt._snapshot

    def counting():
        nonlocal writes
        writes += 1
        return snapshot()

    rt._snapshot = counting
    # A meter reporting every 0.2 s for 4 s = every 12 s for 4 minutes at the real delay.
    for step in range(20):
        hass.states.async_set(AC, str(10 + (step + 1) / 10), ATTRS)
        await hass.async_block_till_done()
        await asyncio.sleep(0.2)
    during = writes
    await asyncio.sleep(0.9)
    writes = during
    assert rt.ledger.totals["mode|sensor.modes_ac|cool"]["kwh"] == pytest.approx(2.0)
    assert writes >= 1


async def test_p2_database_error_in_mode_history_only_delays_the_split(
    hass, monkeypatch
):
    def broken(*args, **kwargs):
        raise OperationalError("select", {}, Exception("database is locked"))

    monkeypatch.setattr(STATES, broken)
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED


async def test_p3_two_devices_on_one_mode_entity(hass):
    entry = await _setup(hass, devices=(AC, AC2))
    hass.states.async_set(CLIMATE, "heat", MODES)
    await hass.async_block_till_done()
    hass.states.async_set(AC, "11", ATTRS)
    hass.states.async_set(AC2, "12", ATTRS)
    await hass.async_block_till_done()
    totals = entry.runtime_data.ledger.totals
    assert totals["mode|sensor.modes_ac|heat"]["kwh"] == pytest.approx(1.0)
    assert totals["mode|sensor.modes_ac2|heat"]["kwh"] == pytest.approx(2.0)


async def test_unlinking_one_of_two_devices_forgets_only_that_device(hass):
    entry = await _setup(hass, devices=(AC, AC2))
    hass.states.async_set(AC, "11", ATTRS)
    hass.states.async_set(AC2, "12", ATTRS)
    await hass.async_block_till_done()
    links = dict(entry.options["device_modes"])
    kept = links.pop(AC)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "device_modes": links}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    tracker = entry.runtime_data.modes
    assert AC not in tracker.pending and AC not in tracker.names
    assert AC2 in tracker.pending

    # Linking it again starts a new record instead of reviving the old one.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "device_modes": {**links, AC: kept}}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    totals = entry.runtime_data.ledger.totals
    assert "mode|sensor.modes_ac|cool" not in totals
    assert totals["mode|sensor.modes_ac2|cool"]["kwh"] == pytest.approx(2.0)
