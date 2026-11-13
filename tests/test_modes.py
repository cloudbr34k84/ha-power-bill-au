"""Cost per mode: a tracked device's energy and cost split by the mode it ran in."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.core import State
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_power_bill_au.const import DOMAIN
from custom_components.ha_power_bill_au.modes import ModeTracker, mode_key, pretty_mode

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


async def _setup(hass, *, solar=False, linked=True):
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
    if linked:
        options["device_modes"] = {AC: {"entity_id": CLIMATE, "registry_id": None}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Modes",
        data={"provider": "Modes", "import_entity": IMPORT, "export_entity": EXPORT},
        options=options,
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


def _mode(runtime, mode):
    return runtime.ledger.totals.get(mode_key(AC, mode), {"kwh": 0.0, "cost": 0.0})


def _mode_sum(runtime):
    prefix = mode_key(AC, "")
    return sum(
        cell["cost"]
        for key, cell in runtime.ledger.totals.items()
        if key.startswith(prefix)
    )


def _hour_rows(*pairs):
    return [
        {
            "start": datetime(2026, 11, 7, hour, tzinfo=UTC).timestamp(),
            "change": kwh * 1000,
        }
        for hour, kwh in pairs
    ]


async def test_live_readings_are_booked_to_the_mode_in_force(hass, freezer):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await _set(hass, freezer, "12:10", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:20", **{AC: "13"})
    # While the controller is unavailable the last known mode is kept.
    await _set(hass, freezer, "12:30", **{CLIMATE: "unavailable"})
    await _set(hass, freezer, "12:40", **{AC: "14"})
    assert _mode(runtime, "cool") == pytest.approx({"kwh": 1.0, "cost": RATE})
    assert _mode(runtime, "dry") == pytest.approx({"kwh": 3.0, "cost": 3 * RATE})
    assert _mode_sum(runtime) == pytest.approx(runtime.ledger.lifetime_cost(AC))

    values = runtime.mode_values(AC, "dry")
    assert values["cost_today"] == values["cost_month"] == values["cost_total"] == 0.6
    assert values["kwh_today"] == values["kwh_month"] == values["kwh_total"] == 3.0
    names = {
        state.attributes["friendly_name"]: state
        for state in hass.states.async_all("sensor")
        if "modes_ac" in state.entity_id and "_dry_" in state.entity_id
    }
    assert len(names) == 6  # cost and energy: today, month to date, total
    assert float(names["Modes Modes ac Dry cost total"].state) == pytest.approx(0.6)
    assert float(names["Modes Modes ac Dry energy total"].state) == pytest.approx(3.0)
    assert names["Modes Modes ac Dry cost total"].attributes["pricing"] == "import_rate"
    # A sensor exists for every mode the controller offers, used or not.
    auto = hass.states.get("sensor.modes_modes_ac_heat_cool_cost_total")
    assert auto.attributes["friendly_name"] == "Modes Modes ac Heat/Cool cost total"


async def test_a_restart_rebuilds_the_same_mode_costs_from_the_saved_shares(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})
    freezer.move_to("2026-11-07T13:03:00+00:00")
    runtime._on_tick(None)
    live = (_mode(runtime, "cool"), _mode(runtime, "dry"))
    assert runtime.mode_since(AC) == "2026-11-07T12:00:00+00:00"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        return {AC: _hour_rows((12, 4.0))} if period == "hour" and AC in ids else {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rebuilt = entry.runtime_data
    assert _mode(rebuilt, "cool") == pytest.approx(live[0])
    assert _mode(rebuilt, "dry") == pytest.approx(live[1])
    assert _mode_sum(rebuilt) == pytest.approx(rebuilt.ledger.lifetime_cost(AC))


async def test_readings_after_the_recorders_last_hour_are_replayed_by_mode(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        return {AC: _hour_rows((11, 1.0))} if period == "hour" and AC in ids else {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-11-07T12:30:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    # 11:00 has no recorded mode and is not split; the 12:00 reading is cool.
    assert _mode(runtime, "cool") == pytest.approx({"kwh": 1.0, "cost": RATE})
    assert runtime.ledger.lifetime_cost(AC) == pytest.approx(2 * RATE)


async def test_with_solar_each_mode_pays_its_share_of_the_devices_hour(hass, freezer):
    entry = await _setup(hass, solar=True)
    runtime = entry.runtime_data
    # 3 kWh in cool, then 1 kWh in dry; half of the house's 4 kWh came from the grid.
    await _set(hass, freezer, "12:01", **{AC: "13", IMPORT: "102", SOLAR: "52"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})
    assert _mode_sum(runtime) == 0  # Priced with the hour.
    freezer.move_to("2026-11-07T13:03:00+00:00")
    runtime._on_tick(None)
    assert _mode(runtime, "cool") == pytest.approx({"kwh": 3.0, "cost": 1.5 * RATE})
    assert _mode(runtime, "dry") == pytest.approx({"kwh": 1.0, "cost": 0.5 * RATE})
    assert _mode_sum(runtime) == pytest.approx(runtime.ledger.lifetime_cost(AC))
    assert runtime.modes.hour_shares(AC, "2026-11-07T12:00:00+00:00") == pytest.approx(
        {"cool": 0.75, "dry": 0.25}
    )


async def test_with_solar_a_restart_rebuilds_the_same_mode_costs(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass, solar=True)
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:01", **{AC: "13", IMPORT: "102", SOLAR: "52"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})
    freezer.move_to("2026-11-07T13:03:00+00:00")
    runtime._on_tick(None)
    live = (_mode(runtime, "cool"), _mode(runtime, "dry"))
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        rows = {IMPORT: 2.0, EXPORT: 0.0, SOLAR: 2.0, AC: 4.0}
        return {e: _hour_rows((12, rows[e])) for e in ids if e in rows}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rebuilt = entry.runtime_data
    assert _mode(rebuilt, "cool") == pytest.approx(live[0])
    assert _mode(rebuilt, "dry") == pytest.approx(live[1])


async def test_the_last_week_is_filled_in_from_the_recorders_mode_history(
    hass, monkeypatch
):
    def at(hour, minute=0):
        return datetime(2026, 11, 7, hour, minute, tzinfo=UTC)

    history = [
        State(CLIMATE, "cool", last_changed=at(8)),
        State(CLIMATE, "unavailable", last_changed=at(10, 10)),
        State(CLIMATE, "dry", last_changed=at(10, 30)),
    ]

    def slots(*triples):
        return [
            {"start": at(hour, minute).timestamp(), "change": kwh * 1000}
            for hour, minute, kwh in triples
        ]

    def stats(hass_, start, end, ids, period, units, types):
        if AC not in ids:
            return {}
        if period == "hour":
            return {AC: _hour_rows((7, 1.0), (9, 2.0), (10, 4.0))}
        if period == "5minute":
            # 07:00 is before any known mode; 10:00 is 1 kWh cool (one slot while the
            # controller was unavailable) and 3 kWh dry; a spike and a bad row are ignored.
            return {
                AC: [
                    *slots((7, 0, 1.0), (9, 0, 2.0), (10, 0, 0.5), (10, 15, 0.5)),
                    *slots((10, 30, 3.0), (10, 45, 99.0)),
                    {"start": "bad", "change": 1},
                ]
            }
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {CLIMATE: history})
    entry = await _setup(hass)
    runtime = entry.runtime_data
    assert _mode(runtime, "cool") == pytest.approx({"kwh": 3.0, "cost": 3 * RATE})
    assert _mode(runtime, "dry") == pytest.approx({"kwh": 3.0, "cost": 3 * RATE})
    assert runtime.ledger.lifetime_cost(AC) == pytest.approx(7 * RATE)  # 07:00 unsplit
    assert runtime.mode_since(AC) == "2026-11-07T09:00:00+00:00"


async def test_unreadable_mode_history_only_delays_the_split(hass, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("recorder busy")

    monkeypatch.setattr(STATES, broken)
    entry = await _setup(hass)
    assert entry.runtime_data.mode_since(AC) is None
    hass.states.async_set(AC, "11", ATTRS)
    await hass.async_block_till_done()
    assert _mode(entry.runtime_data, "cool")["kwh"] == pytest.approx(1.0)


async def test_unlinking_a_device_removes_its_mode_sensors_and_costs(hass, freezer):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    assert hass.states.get("sensor.modes_modes_ac_cool_cost_total") is not None
    options = dict(entry.options)
    options.pop("device_modes")
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.modes_modes_ac_cool_cost_total") is None
    assert _mode_sum(entry.runtime_data) == 0
    assert entry.runtime_data.ledger.lifetime_cost(AC) == pytest.approx(RATE)


async def test_a_renamed_mode_entity_is_followed_by_its_registry_identity(hass):
    from homeassistant.helpers import entity_registry as er

    hass.states.async_remove(CLIMATE)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create(
        "climate", "test", "ac", suggested_object_id="modes_ac"
    )
    hass.states.async_set(climate.entity_id, "cool", MODES)
    entry = await _setup(hass, linked=False)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "device_modes": {
                AC: {"entity_id": "climate.old_name", "registry_id": climate.id},
                "sensor.not_tracked": {"entity_id": CLIMATE, "registry_id": None},
                "bad": "link",
            },
        },
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.mode_links() == {AC: (AC, CLIMATE)}

    registry.async_update_entity(CLIMATE, new_entity_id="climate.renamed_ac")
    hass.states.async_set("climate.renamed_ac", "heat", MODES)
    await hass.async_block_till_done()
    assert entry.runtime_data.mode_links() == {AC: (AC, "climate.renamed_ac")}
    hass.states.async_set(AC, "11", ATTRS)
    await hass.async_block_till_done()
    assert _mode(entry.runtime_data, "heat")["kwh"] == pytest.approx(1.0)


async def test_today_and_month_to_date_are_different_periods(hass, freezer):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    freezer.move_to("2026-11-08T09:00:00+00:00")
    hass.states.async_set(AC, "13", ATTRS)
    await hass.async_block_till_done()
    values = entry.runtime_data.mode_values(AC, "cool")
    assert (values["kwh_today"], values["kwh_month"], values["kwh_total"]) == (2, 3, 3)
    assert values["cost_today"] == pytest.approx(2 * RATE)
    assert values["cost_month"] == pytest.approx(3 * RATE)

    def state(suffix):
        return float(hass.states.get(f"sensor.modes_modes_ac_cool_{suffix}").state)

    assert state("energy_today") == 2
    assert state("energy_month_to_date") == 3
    assert state("cost_today") == pytest.approx(2 * RATE)
    assert state("cost_month_to_date") == pytest.approx(3 * RATE)


async def test_the_last_mode_is_remembered_across_a_restart(hass, freezer):
    """The controller is unavailable when HA comes back: the saved mode still applies."""
    entry = await _setup(hass)
    await _set(hass, freezer, "12:05", **{CLIMATE: "dry"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(CLIMATE, "unavailable", MODES)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _set(hass, freezer, "12:20", **{AC: "12"})
    assert _mode(entry.runtime_data, "dry")["kwh"] == pytest.approx(2.0)


async def test_mode_sensors_exist_while_the_mode_entity_is_still_missing(hass):
    """At boot the controller may load after this integration: nothing is removed."""
    from homeassistant.helpers import entity_registry as er

    entry = await _setup(hass)
    heat = "sensor.modes_modes_ac_heat_cost_total"
    registry = er.async_get(hass)
    original = registry.async_get(heat).id
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_remove(CLIMATE)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(heat) is not None
    assert registry.async_get(heat).id == original  # Never deleted and re-created.
    assert entry.runtime_data.mode_names(AC) == sorted(MODES["hvac_modes"])


async def test_the_mode_buffer_is_pruned_after_a_day(hass, freezer):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:01", **{AC: "11"})
    freezer.move_to("2026-11-07T13:03:00+00:00")
    runtime._on_tick(None)
    assert runtime.modes.pending[AC]
    freezer.move_to("2026-11-08T13:05:00+00:00")
    runtime._on_tick(None)
    assert runtime.modes.pending[AC] == {}
    # The hour's split outlives the buffer.
    assert runtime.modes.hour_shares(AC, "2026-11-07T12:00:00+00:00") == {"cool": 1.0}


async def test_without_the_recorder_unlinked_mode_costs_are_dropped(hass, freezer):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    options = dict(entry.options)
    options.pop("device_modes")
    hass.config_entries.async_update_entry(entry, options=options)
    hass.config.components.remove("recorder")
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        assert _mode_sum(runtime) == 0
        assert runtime.ledger.lifetime_cost(AC) == pytest.approx(RATE)
    finally:
        hass.config.components.add("recorder")


async def test_only_a_rename_of_the_mode_entity_reloads_the_contract(hass):
    from homeassistant.helpers import entity_registry as er

    hass.states.async_remove(CLIMATE)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create(
        "climate", "test", "ac", suggested_object_id="modes_ac"
    )
    hass.states.async_set(CLIMATE, "cool", MODES)
    entry = await _setup(hass, linked=False)
    other = {"entity_id": "select.other", "registry_id": "another-registry-id"}
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "device_modes": {
                AC: {"entity_id": CLIMATE, "registry_id": climate.id},
                "sensor.other_meter": other,
            },
        },
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    registry.async_update_entity(CLIMATE, name="Lounge AC")
    await hass.async_block_till_done()
    assert entry.runtime_data is runtime  # Not reloaded.

    registry.async_update_entity(CLIMATE, new_entity_id="climate.lounge")
    hass.states.async_set("climate.lounge", "cool", MODES)
    await hass.async_block_till_done()
    assert entry.runtime_data is not runtime
    # The stored entity ID follows, for when the registry entry disappears. A link to
    # another entity is left alone.
    assert entry.options["device_modes"] == {
        AC: {"entity_id": "climate.lounge", "registry_id": climate.id},
        "sensor.other_meter": other,
    }

    # Removing the mode entity reloads too, so the link falls back to its stored ID.
    runtime = entry.runtime_data
    registry.async_remove("climate.lounge")
    await hass.async_block_till_done()
    assert entry.runtime_data is not runtime


def test_an_hour_partly_out_of_the_buffer_keeps_its_stored_split():
    from datetime import timedelta

    tracker = ModeTracker()
    now = datetime(2026, 11, 8, 12, 30, tzinfo=UTC)
    day = timedelta(days=1)
    old_hour = "2026-11-07T13:00:00+00:00"
    tracker.fill(AC, old_hour, {"cool": 1.0})
    # Only the end of that hour is still buffered; the next hour is complete.
    tracker.add(AC, "2026-11-07T13:55:00+00:00", "dry", 1.0)
    tracker.add(AC, "2026-11-07T14:10:00+00:00", "dry", 1.0)
    assert tracker.settle(now, delay=timedelta(0), retention=day)
    assert tracker.hour_shares(AC, old_hour) == {"cool": 1.0}
    assert tracker.hour_shares(AC, "2026-11-07T14:00:00+00:00") == {"dry": 1.0}
    assert not tracker.settle(now, delay=timedelta(0), retention=day)  # Nothing new.


def test_saved_modes_round_trip_compactly_and_survive_bad_data():
    tracker = ModeTracker()
    for hour in (8, 9, 10):
        tracker.hours.setdefault(AC, {})[f"2026-11-07T{hour:02d}:00:00+00:00"] = {
            "cool": 1.0
        }
    tracker.hours[AC]["2026-11-07T11:00:00+00:00"] = {"cool": 0.25, "dry": 0.75}
    tracker.hours[AC]["2026-11-07T12:00:00+00:00"] = {"dry": 1.0}
    tracker.hours[AC]["2026-11-07T14:00:00+00:00"] = {"dry": 1.0}
    tracker.add(AC, "2026-11-07T15:05:00+00:00", "heat", 0.5)
    tracker.add(AC, "2026-11-07T15:05:00+00:00", "heat", float("nan"))
    tracker.last[AC] = "heat"
    saved = tracker.to_dict()
    assert saved["hours"][AC]["runs"] == [
        ["2026-11-07T08:00:00+00:00", 3, "cool"],
        ["2026-11-07T12:00:00+00:00", 1, "dry"],
        ["2026-11-07T14:00:00+00:00", 1, "dry"],
    ]
    loaded = ModeTracker.from_dict(saved)
    assert loaded.hours == tracker.hours
    assert loaded.pending == tracker.pending
    assert loaded.last == {AC: "heat"}
    assert loaded.modes(AC) == {"cool", "dry", "heat"}
    assert loaded.slot_shares(AC, "2026-11-07T15:05:00+00:00") == {"heat": 1.0}
    assert loaded.slot_shares(AC, "2026-11-07T11:20:00+00:00") == {
        "cool": 0.25,
        "dry": 0.75,
    }

    # A backfilled hour never replaces one recorded live, and fills a missing one.
    loaded.fill(AC, "2026-11-07T08:00:00+00:00", {"heat": 1.0})
    loaded.fill(AC, "2026-11-07T07:00:00+00:00", {"heat": 1.0, "cool": 0.0})
    assert loaded.hour_shares(AC, "2026-11-07T08:00:00+00:00") == {"cool": 1.0}
    assert loaded.hour_shares(AC, "2026-11-07T07:00:00+00:00") == {"heat": 1.0}
    assert loaded.since(AC) == "2026-11-07T07:00:00+00:00"

    assert ModeTracker.from_dict("nonsense").hours == {}
    damaged = ModeTracker.from_dict(
        {
            "hours": {
                AC: {"runs": [["not a date", 2, "cool"], [1]], "mixed": {"x": "y"}},
                "other": "bad",
            },
            "pending": {AC: {"slot": "bad"}, "other": "bad"},
            "last": "bad",
        }
    )
    assert damaged.hours == {AC: {}}
    assert damaged.pending == {}
    assert pretty_mode("heat_cool") == "Heat/Cool"
    assert pretty_mode("h_powerful") == "H powerful"
