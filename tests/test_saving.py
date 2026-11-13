"""Saving: a pending save is not postponed, and shutdown and reload write the right data."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_power_bill_au import runtime as runtime_module
from custom_components.ha_power_bill_au.const import DOMAIN

IMPORT = "sensor.modes_import"
AC = "sensor.modes_ac"
CLIMATE = "climate.modes_ac"
ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}
MODES = {"hvac_modes": ["off", "cool", "heat"]}


@pytest.fixture(autouse=True)
async def _inputs(recorder_mock, enable_custom_integrations, hass, monkeypatch):
    monkeypatch.setattr(runtime_module, "SAVE_DELAY_SECONDS", 0.5)
    for entity, value in ((IMPORT, "100"), (AC, "10")):
        hass.states.async_set(entity, value, ATTRS)
    hass.states.async_set(CLIMATE, "cool", MODES)


async def _setup(hass, *, linked=True):
    plan = {
        "effective_from": "2000-01-01",
        "import_bands": [{"name": "flat", "start": 0, "end": 24, "rate": 0.2}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }
    options = {"plans": [plan], "tracked_devices": [AC]}
    if linked:
        options["device_modes"] = {AC: {"entity_id": CLIMATE, "registry_id": None}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Modes",
        data={"provider": "Modes", "import_entity": IMPORT},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _saved_import(hass_storage, entry):
    data = hass_storage.get(f"{DOMAIN}.{entry.entry_id}", {}).get("data", {})
    return data.get("totals", {}).get("import", {}).get("kwh", 0.0)


async def _reading(hass, value):
    hass.states.async_set(IMPORT, str(value), ATTRS)
    await hass.async_block_till_done()


@pytest.mark.parametrize("linked", [False, True])
async def test_s1_steady_readings_are_saved_about_once_per_delay(
    hass, hass_storage, linked
):
    entry = await _setup(hass, linked=linked)
    rt = entry.runtime_data
    await asyncio.sleep(0.7)
    writes = 0
    snapshot = rt._snapshot

    def counting():
        nonlocal writes
        writes += 1
        return snapshot()

    rt._snapshot = counting
    for step in range(20):  # 4 s = 8 save delays
        await _reading(hass, 100 + (step + 1) / 10)
        await asyncio.sleep(0.2)
    assert 5 <= writes <= 9
    await asyncio.sleep(0.8)
    assert rt._save_pending is False
    assert _saved_import(hass_storage, entry) == pytest.approx(2.0)


async def test_s3_shutdown_writes_the_latest_readings(hass, hass_storage):
    entry = await _setup(hass)
    rt = entry.runtime_data
    await asyncio.sleep(0.7)
    await _reading(hass, 101)  # a save is pending when HA starts to stop
    hass.set_state(CoreState.stopping)
    await asyncio.sleep(0.8)  # the timer fires while stopping: nothing is written yet
    assert _saved_import(hass_storage, entry) == pytest.approx(0.0)
    await _reading(hass, 103)  # readings keep arriving while stopping
    hass.set_state(CoreState.final_write)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    assert _saved_import(hass_storage, entry) == pytest.approx(3.0)
    assert rt._save_pending is False


async def test_s3b_shutdown_with_no_save_pending(hass, hass_storage):
    entry = await _setup(hass)
    rt = entry.runtime_data
    await asyncio.sleep(0.7)
    assert rt._save_pending is False
    hass.set_state(CoreState.stopping)
    await _reading(hass, 104)
    assert rt._save_pending is True
    hass.set_state(CoreState.final_write)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    assert _saved_import(hass_storage, entry) == pytest.approx(4.0)


async def test_s4_reload_with_a_save_pending_never_writes_old_data(hass, hass_storage):
    entry = await _setup(hass)
    old = entry.runtime_data
    await asyncio.sleep(0.7)
    await _reading(hass, 101)
    assert old._save_pending is True
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    new = entry.runtime_data
    assert new is not old
    late = 0
    snapshot = old._snapshot

    def counting():
        nonlocal late
        late += 1
        return snapshot()

    old._snapshot = counting
    await _reading(hass, 105)
    await asyncio.sleep(1.2)
    assert late == 0
    assert old._save_pending is False
    # Without recorder rows the import comes back from the saved ledger: 1 + 4 kWh.
    assert _saved_import(hass_storage, entry) == pytest.approx(5.0)
    assert new.ledger.totals["import"]["kwh"] == pytest.approx(5.0)


async def test_s5_idle_contract_still_saves_after_a_tick(hass, hass_storage):
    entry = await _setup(hass, linked=False)
    rt = entry.runtime_data
    await asyncio.sleep(0.7)
    writes = 0
    snapshot = rt._snapshot

    def counting():
        nonlocal writes
        writes += 1
        return snapshot()

    rt._snapshot = counting
    rt._on_tick(None)
    rt._on_tick(None)
    await asyncio.sleep(0.8)
    assert writes == 1
