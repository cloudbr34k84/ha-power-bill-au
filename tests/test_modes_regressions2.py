"""Cost-per-mode regressions from the second review: history speed, long downtime, identity."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_power_bill_au.const import DOMAIN
from custom_components.ha_power_bill_au.modes import ModeTracker, mode_key

IMPORT = "sensor.modes_import"
EXPORT = "sensor.modes_export"
SOLAR = "sensor.modes_solar"
AC = "sensor.modes_ac"
CLIMATE = "climate.modes_ac"
ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}
MODES = {"hvac_modes": ["off", "cool", "heat", "dry", "fan_only", "heat_cool"]}
RATE = 0.2
STATS = "custom_components.ha_power_bill_au.runtime.statistics_during_period"
STATES = "custom_components.ha_power_bill_au.runtime.get_significant_states"


@pytest.fixture(autouse=True)
async def _inputs(recorder_mock, enable_custom_integrations, hass, freezer):
    freezer.move_to("2026-11-07T12:00:00+00:00")
    await hass.config.async_set_time_zone("UTC")
    for entity, value in ((IMPORT, "100"), (EXPORT, "20"), (SOLAR, "50"), (AC, "10")):
        hass.states.async_set(entity, value, ATTRS)
    hass.states.async_set(CLIMATE, "cool", MODES)


def _options(*, solar=False, link=None):
    plan = {
        "effective_from": "2000-01-01",
        "import_bands": [{"name": "flat", "start": 0, "end": 24, "rate": RATE}],
        "export_bands": [{"name": "fit", "start": 0, "end": 24, "rate": 0.05}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }
    options = {"plans": [plan], "tracked_devices": [AC]}
    if solar:
        options["solar_entity"] = SOLAR
    if link:
        options["device_modes"] = {AC: link}
    return options


async def _setup(
    hass, *, solar=False, linked=True, mode_entity=CLIMATE, registry_id=None
):
    link = {"entity_id": mode_entity, "registry_id": registry_id} if linked else None
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Modes",
        data={"provider": "Modes", "import_entity": IMPORT, "export_entity": EXPORT},
        options=_options(solar=solar, link=link),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _set(hass, freezer, at, **values):
    freezer.move_to(f"2026-11-07T{at}:00+00:00")
    for entity, value in values.items():
        attrs = MODES if entity == CLIMATE else ATTRS
        hass.states.async_set(entity, value, attrs)
    await hass.async_block_till_done()


def _mode_kwh(runtime):
    prefix = mode_key(AC, "")
    return {
        key[len(prefix) :]: round(cell["kwh"], 6)
        for key, cell in runtime.ledger.totals.items()
        if key.startswith(prefix) and cell["kwh"] > 0
    }


def _at(hour, minute=0, day=7):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def _hour_rows(*pairs):
    return [
        {"start": _at(hour).timestamp(), "change": kwh * 1000} for hour, kwh in pairs
    ]


def _slots(*triples):
    return [
        {"start": _at(hour, minute).timestamp(), "change": kwh * 1000}
        for hour, minute, kwh in triples
    ]


# ---------------------------------------------------------------------------------------
# A: pricing years of history when the device is linked
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("linked", [False, True])
async def test_a1_history_build_time_with_three_years_of_hourly_rows(
    hass, monkeypatch, linked
):
    entry = await _setup(hass, linked=True)
    runtime = entry.runtime_data
    # A device that has reported in every five-minute slot of the last day.
    now = _at(12)
    for step in range(1, 288):
        slot = (now - timedelta(minutes=5 * step)).isoformat()
        runtime.modes.add(AC, slot, "cool", 0.05)
        runtime._pending_energy.setdefault(AC, {})[slot] = 0.05
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    if not linked:
        hass.config_entries.async_update_entry(entry, options=_options())
    first = _at(11) - timedelta(hours=3 * 8760)
    rows = [
        {"start": (first + timedelta(hours=i)).timestamp(), "change": 500.0}
        for i in range(3 * 8760)
    ]

    def stats(hass_, start, end, ids, period, units, types):
        return {AC: rows} if period == "hour" and AC in ids else {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    began = time.process_time()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    took = time.process_time() - began
    assert took < 5.0


# ---------------------------------------------------------------------------------------
# B: HA is off for a day; the hour in progress at shutdown
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("recorded", [True, False])
async def test_b1_downtime_of_a_day_keeps_the_last_hours_split(
    hass, freezer, monkeypatch, recorded
):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:31", **{AC: "11"})  # 1 kWh, arrives while cooling
    await _set(hass, freezer, "12:33", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})  # 3 kWh in dry
    before = _mode_kwh(runtime)
    assert before == {"cool": 1.0, "dry": 3.0}
    freezer.move_to("2026-11-07T12:50:00+00:00")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    history = [
        State(CLIMATE, "cool", last_changed=_at(8)),
        State(CLIMATE, "dry", last_changed=_at(12, 33)),
    ]

    def stats(hass_, start, end, ids, period, units, types):
        if AC not in ids:
            return {}
        if period == "hour":
            return {AC: _hour_rows((12, 4.0))}
        if period == "5minute":
            return {AC: _slots((12, 30, 1.0), (12, 40, 3.0))}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(
        STATES, lambda *args, **kwargs: {CLIMATE: history} if recorded else {}
    )
    freezer.move_to("2026-11-08T13:10:00+00:00")  # 24 h 20 min later
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    after = _mode_kwh(entry.runtime_data)
    assert entry.runtime_data.ledger.totals[AC]["kwh"] == pytest.approx(4.0)
    assert after == before


# ---------------------------------------------------------------------------------------
# C: the identity of the linked entity
# ---------------------------------------------------------------------------------------
async def test_c1_resaving_the_same_entity_after_it_got_a_registry_entry(hass, freezer):
    entry = await _setup(hass)  # linked while the entity had no registry entry
    runtime = entry.runtime_data
    old = "2026-09-01T03:00:00+00:00"
    runtime.modes._set_hour(AC, old, {"heat": 1.0})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_remove(CLIMATE)  # e.g. a YAML climate that was given a unique_id
    registered = er.async_get(hass).async_get_or_create(
        "climate", "test", "ac", suggested_object_id="modes_ac"
    )
    assert registered.entity_id == CLIMATE
    hass.states.async_set(CLIMATE, "cool", MODES)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.modes.hour_shares(AC, old) == {"heat": 1.0}
    # The user opens Configure > Modes and submits the entity that is already selected.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "modes"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"device": AC}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"mode_entity": CLIMATE}
    )
    await hass.async_block_till_done()
    assert entry.options["device_modes"][AC]["entity_id"] == CLIMATE
    assert entry.runtime_data.modes.hour_shares(AC, old) == {"heat": 1.0}


async def test_c2_upgrade_from_a_store_without_source_keeps_history(hass, hass_storage):
    entry = await _setup(hass)
    old = "2026-09-01T03:00:00+00:00"
    entry.runtime_data.modes._set_hour(AC, old, {"heat": 1.0})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["modes"]
    saved.pop("source")
    saved.pop("names")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.modes.hour_shares(AC, old) == {"heat": 1.0}


async def test_c3_rename_keeps_history_and_updates_the_stored_entity_id(hass):
    hass.states.async_remove(CLIMATE)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create(
        "climate", "test", "ac", suggested_object_id="modes_ac"
    )
    hass.states.async_set(climate.entity_id, "cool", MODES)
    entry = await _setup(hass, registry_id=climate.id)
    old = "2026-09-01T03:00:00+00:00"
    entry.runtime_data.modes._set_hour(AC, old, {"heat": 1.0})
    registry.async_update_entity(CLIMATE, new_entity_id="climate.renamed_ac")
    hass.states.async_set("climate.renamed_ac", "heat", MODES)
    await hass.async_block_till_done()
    assert entry.options["device_modes"][AC] == {
        "entity_id": "climate.renamed_ac",
        "registry_id": climate.id,
    }
    assert entry.runtime_data.mode_links() == {AC: (AC, "climate.renamed_ac")}
    assert entry.runtime_data.modes.hour_shares(AC, old) == {"heat": 1.0}


# ---------------------------------------------------------------------------------------
# D: linking a few minutes after a reading the five-minute statistics do not hold yet
# ---------------------------------------------------------------------------------------
async def test_d1_reading_just_before_the_link_is_stable_across_a_restart(
    hass, freezer, monkeypatch
):
    hass.states.async_set(CLIMATE, "heat", MODES)
    entry = await _setup(hass, linked=False)
    await _set(hass, freezer, "12:05", **{AC: "13"})  # 3 kWh heating
    await _set(hass, freezer, "12:31", **{AC: "14"})  # 1 kWh heating, slot 12:30
    history = [State(CLIMATE, "heat", last_changed=_at(8))]
    five = _slots((12, 5, 3.0))  # 12:30-12:35 is not compiled yet
    hourly = _hour_rows((11, 1.0))

    def stats(hass_, start, end, ids, period, units, types):
        if AC not in ids:
            return {}
        if period == "hour":
            return {AC: list(hourly)}
        if period == "5minute":
            return {AC: list(five)}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {CLIMATE: list(history)})
    freezer.move_to("2026-11-07T12:32:00+00:00")
    hass.config_entries.async_update_entry(
        entry, options=_options(link={"entity_id": CLIMATE, "registry_id": None})
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:33", **{CLIMATE: "cool"})
    history.append(State(CLIMATE, "cool", last_changed=_at(12, 33)))
    await _set(hass, freezer, "12:34", **{AC: "15"})  # 1 kWh cooling, slot 12:30
    five.extend(_slots((12, 30, 2.0)))
    freezer.move_to("2026-11-07T13:03:00+00:00")
    runtime._on_tick(None)
    before = _mode_kwh(runtime)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hourly.extend(_hour_rows((12, 5.0)))
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    after = _mode_kwh(entry.runtime_data)
    assert after == before


# ---------------------------------------------------------------------------------------
# E: sensors for modes that appear later
# ---------------------------------------------------------------------------------------
async def test_e1_auto_appearing_after_heat_cool(hass, freezer, caplog):
    hass.states.async_set(CLIMATE, "cool", {"hvac_modes": ["off", "cool", "heat_cool"]})
    await _setup(hass)
    hass.states.async_set(
        CLIMATE, "auto", {"hvac_modes": ["off", "cool", "heat_cool", "auto"]}
    )
    await hass.async_block_till_done()
    hass.states.async_set(
        CLIMATE, "cool", {"hvac_modes": ["off", "cool", "heat_cool", "auto"]}
    )
    hass.states.async_set(
        CLIMATE, "auto", {"hvac_modes": ["off", "cool", "heat_cool", "auto"]}
    )
    await hass.async_block_till_done()
    names = sorted(
        (state.attributes["friendly_name"], state.entity_id)
        for state in hass.states.async_all("sensor")
        if state.attributes.get("friendly_name", "").endswith("cost total")
        and "modes_ac" in state.entity_id
    )
    assert "does not generate unique IDs" not in caplog.text
    assert len({name for name, _ in names}) == len(names), names


async def test_e2_unknown_mode_gets_sensors_when_first_used(hass, freezer):
    hass.states.async_remove(CLIMATE)
    entry = await _setup(hass)
    await _set(hass, freezer, "12:05", **{AC: "11"})
    assert _mode_kwh(entry.runtime_data) == {"unknown": 1.0}
    ids = sorted(
        s.entity_id for s in hass.states.async_all("sensor") if "unknown" in s.entity_id
    )
    assert len(ids) == 6


def test_f1_corrupt_runs_do_not_fail_setup():
    assert ModeTracker.from_dict({"hours": {AC: {"runs": 5}}}).hours == {AC: {}}


# ---------------------------------------------------------------------------------------
# G: gap-fillers for surviving mutants (expected to pass on the real code)
# ---------------------------------------------------------------------------------------
async def test_g1_an_hour_settled_after_a_save_is_in_the_next_save(
    hass, freezer, hass_storage
):
    entry = await _setup(hass)
    rt = entry.runtime_data
    await _set(hass, freezer, "12:01", **{AC: "11"})
    freezer.move_to("2026-11-07T13:03:00+00:00")
    rt._on_tick(None)
    rt._snapshot()  # a periodic save serialises the hours
    await _set(hass, freezer, "13:10", **{AC: "12"})
    freezer.move_to("2026-11-07T14:03:00+00:00")
    rt._on_tick(None)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["modes"]["hours"][AC]
    assert saved["runs"] == [["2026-11-07T12:00:00+00:00", 2, "cool"]]


async def test_g2_two_new_modes_in_a_row_add_no_duplicates(hass, freezer, caplog):
    hass.states.async_set("select.heater", "eco", {"options": ["eco"]})
    entry = await _setup(hass, mode_entity="select.heater")
    hass.states.async_set("select.heater", "boost", {"options": ["eco"]})
    await hass.async_block_till_done()
    hass.states.async_set("select.heater", "away", {"options": ["eco"]})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    count = len(
        [
            e
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if "_mode_" in e.unique_id
        ]
    )
    assert "does not generate unique IDs" not in caplog.text
    assert count == 18


async def test_g3_without_the_recorder_a_relinked_device_drops_old_mode_costs(
    hass, freezer
):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("select.heater", "eco", {"options": ["eco", "boost"]})
    hass.config_entries.async_update_entry(
        entry,
        options=_options(link={"entity_id": "select.heater", "registry_id": None}),
    )
    hass.config.components.remove("recorder")
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _mode_kwh(entry.runtime_data) == {}
        assert entry.runtime_data.ledger.totals[AC]["kwh"] == pytest.approx(1.0)
    finally:
        hass.config.components.add("recorder")
