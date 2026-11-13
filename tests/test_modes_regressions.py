"""Cost-per-mode regressions found in review: restarts, linking, sensors, backfill."""

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


async def _setup(hass, *, solar=False, linked=True, mode_entity=CLIMATE):
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
        options["device_modes"] = {AC: {"entity_id": mode_entity, "registry_id": None}}
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


def _mode_kwh(runtime):
    prefix = mode_key(AC, "")
    return {
        key[len(prefix) :]: round(cell["kwh"], 6)
        for key, cell in runtime.ledger.totals.items()
        if key.startswith(prefix) and cell["kwh"] > 0
    }


def _at(hour, minute=0):
    return datetime(2026, 11, 7, hour, minute, tzinfo=UTC)


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
# D1: a restart in the two minutes after the hour (recorder already has the hour, the
# tracker has not summarised it) loses the hour's split.
# ---------------------------------------------------------------------------------------
async def test_d1_restart_just_after_the_hour_keeps_mode_totals(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})
    before = _mode_kwh(runtime)
    assert before == {"cool": 1.0, "dry": 3.0}
    # No tick after 13:02: HA is restarted at 13:01, after the recorder compiled 12:00.
    freezer.move_to("2026-11-07T13:01:00+00:00")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        return {AC: _hour_rows((12, 4.0))} if period == "hour" and AC in ids else {}

    monkeypatch.setattr(STATS, stats)
    # The mode entity has no rows in the recorder (excluded, or purged).
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rebuilt = entry.runtime_data
    assert rebuilt.ledger.totals[AC]["kwh"] == pytest.approx(4.0)
    assert _mode_kwh(rebuilt) == before


async def test_d1b_solar_restart_just_after_the_hour_modes_add_up(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass, solar=True)
    await _set(hass, freezer, "12:01", **{AC: "13", IMPORT: "102", SOLAR: "52"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})
    freezer.move_to("2026-11-07T13:01:00+00:00")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        rows = {IMPORT: 2.0, EXPORT: 0.0, SOLAR: 2.0, AC: 4.0}
        return {e: _hour_rows((12, rows[e])) for e in ids if e in rows}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rebuilt = entry.runtime_data
    freezer.move_to("2026-11-07T13:05:00+00:00")
    rebuilt._on_tick(None)
    assert rebuilt.ledger.totals[AC]["kwh"] == pytest.approx(4.0)
    # The hour has a recorded split (in the buffer, and by now in `hours`).
    assert rebuilt.modes.hour_shares(AC, "2026-11-07T12:00:00+00:00")
    assert _mode_kwh(rebuilt) == {"cool": 3.0, "dry": 1.0}


# ---------------------------------------------------------------------------------------
# D2: linking mid-hour. The hour is backfilled, then `settle` overwrites it with the split
# of only the readings since the link; the next restart re-attributes the whole hour.
# ---------------------------------------------------------------------------------------
async def test_d2_hour_of_linking_is_stable_across_a_restart(
    hass, freezer, monkeypatch
):
    hass.states.async_set(CLIMATE, "heat", MODES)
    entry = await _setup(hass, linked=False)
    await _set(hass, freezer, "12:05", **{AC: "13"})  # 3 kWh heating before the link

    history = [State(CLIMATE, "heat", last_changed=_at(8))]
    five = _slots((12, 5, 3.0))
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

    freezer.move_to("2026-11-07T12:30:00+00:00")
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "device_modes": {AC: {"entity_id": CLIMATE, "registry_id": None}},
        },
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert _mode_kwh(runtime) == {"heat": 3.0}  # backfilled from the recorder

    await _set(hass, freezer, "12:35", **{CLIMATE: "cool"})
    history.append(State(CLIMATE, "cool", last_changed=_at(12, 35)))
    await _set(hass, freezer, "12:40", **{AC: "14"})  # 1 kWh cooling
    five.extend(_slots((12, 40, 1.0)))
    freezer.move_to("2026-11-07T13:03:00+00:00")
    runtime._on_tick(None)
    before = _mode_kwh(runtime)
    assert before == {"heat": 3.0, "cool": 1.0}

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hourly.extend(_hour_rows((12, 4.0)))
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _mode_kwh(entry.runtime_data) == before


# ---------------------------------------------------------------------------------------
# D3: sensors of modes not yet used are deleted from the registry when the mode entity
# has no state at setup (HA start before the climate integration has loaded).
# ---------------------------------------------------------------------------------------
async def test_d3_mode_sensors_survive_a_start_before_the_mode_entity(hass, freezer):
    entry = await _setup(hass)
    registry = er.async_get(hass)
    heat = "sensor.modes_modes_ac_heat_cost_total"
    assert registry.async_get(heat) is not None
    registry.async_update_entity(heat, name="My heating cost")
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_remove(CLIMATE)  # the climate platform is not loaded yet
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(CLIMATE, "heat", MODES)  # ... and now it is
    await hass.async_block_till_done()
    await _set(hass, freezer, "12:10", **{AC: "12"})
    assert _mode(entry.runtime_data, "heat")["kwh"] == pytest.approx(2.0)
    kept = registry.async_get(heat)
    assert kept is not None, "registry entry (and its customisation) was deleted"
    assert kept.name == "My heating cost"
    assert hass.states.get(heat) is not None
    assert float(hass.states.get(heat).state) == pytest.approx(0.4)


async def test_d3b_a_mode_first_seen_after_setup_gets_sensors(hass, freezer):
    hass.states.async_set("select.heater", "eco", {"options": ["eco"]})
    entry = await _setup(hass, mode_entity="select.heater")
    hass.states.async_set("select.heater", "boost", {"options": ["eco", "boost"]})
    await hass.async_block_till_done()
    await _set(hass, freezer, "12:10", **{AC: "12"})
    assert _mode(entry.runtime_data, "boost")["kwh"] == pytest.approx(2.0)
    assert hass.states.get("sensor.modes_modes_ac_boost_cost_total") is not None


# ---------------------------------------------------------------------------------------
# D4: unique IDs are built from a slug of the mode, so two modes can collide.
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "options",
    [["Eco", "Eco+"], ["heat cool", "heat_cool"], ["暖房", "冷房"]],
)
async def test_d4_every_mode_gets_its_own_sensors(hass, freezer, caplog, options):
    hass.states.async_set("select.heater", options[0], {"options": options})
    entry = await _setup(hass, mode_entity="select.heater")
    await _set(hass, freezer, "12:05", **{AC: "11"})
    hass.states.async_set("select.heater", options[1], {"options": options})
    await hass.async_block_till_done()
    await _set(hass, freezer, "12:10", **{AC: "13"})
    runtime = entry.runtime_data
    assert _mode(runtime, options[0])["kwh"] == pytest.approx(1.0)
    assert _mode(runtime, options[1])["kwh"] == pytest.approx(2.0)
    registry = er.async_get(hass)
    mode_sensors = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if "_mode_" in e.unique_id
    ]
    totals = sorted(
        float(hass.states.get(e.entity_id).state)
        for e in mode_sensors
        if e.unique_id.endswith("_kwh_total")
    )
    assert "does not generate unique IDs" not in caplog.text
    assert len(mode_sensors) == 12
    assert totals == [1.0, 2.0]


async def test_d4b_auto_and_heat_cool_have_distinct_names(hass):
    hass.states.async_set(
        CLIMATE, "cool", {"hvac_modes": ["off", "cool", "auto", "heat_cool"]}
    )
    await _setup(hass)
    names = [
        state.attributes["friendly_name"]
        for state in hass.states.async_all("sensor")
        if state.attributes.get("friendly_name", "").endswith("cost total")
        and "modes_ac" in state.entity_id
    ]
    assert len(names) == len(set(names)), sorted(names)


# ---------------------------------------------------------------------------------------
# D5: backfill scans the whole mode timeline for every five-minute row, in the event loop.
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("rows", [20_000])
async def test_d5_backfill_does_not_block_the_event_loop(hass, monkeypatch, rows):
    start = datetime(2026, 10, 31, 12, tzinfo=UTC)
    span = timedelta(days=7).total_seconds()
    # A climate entity writes a state row for every attribute change (current temperature).
    history = [
        State(
            CLIMATE,
            "cool" if (i // 500) % 2 else "heat",
            last_changed=start + timedelta(seconds=span * i / rows),
        )
        for i in range(rows)
    ]
    five = [
        {
            "start": (start + timedelta(minutes=5 * i)).timestamp(),
            "change": 100.0,
        }
        for i in range(7 * 288)
    ]

    def stats(hass_, start_, end, ids, period, units, types):
        return {AC: five} if period == "5minute" and AC in ids else {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {CLIMATE: history})
    entry = await _setup(hass)
    runtime = entry.runtime_data
    runtime.modes = ModeTracker()

    blocked = 0.0
    original = runtime._async_backfill_modes

    async def fetch_only(job, *args):
        return job(*args)

    class Instant:
        async_add_executor_job = staticmethod(fetch_only)

    monkeypatch.setattr(
        "custom_components.ha_power_bill_au.runtime.get_instance", lambda hass_: Instant
    )
    began = time.thread_time()
    await original(datetime(2026, 11, 7, 12, tzinfo=UTC))
    blocked = time.thread_time() - began
    assert len(runtime.modes.hours[AC]) == 168
    assert blocked < 1.0


# ---------------------------------------------------------------------------------------
# D6: the backfill gives a slot to the mode in force at the END of the slot.
# ---------------------------------------------------------------------------------------
async def test_d6_backfill_slot_before_a_mode_change_keeps_the_old_mode(
    hass, monkeypatch
):
    history = [
        State(CLIMATE, "cool", last_changed=_at(8)),
        State(CLIMATE, "off", last_changed=_at(10, 30)),
    ]

    def stats(hass_, start, end, ids, period, units, types):
        if AC not in ids:
            return {}
        if period == "hour":
            return {AC: _hour_rows((10, 3.0))}
        if period == "5minute":
            # 10:25-10:30 is entirely before the switch to off at 10:30:00.
            return {AC: _slots((10, 0, 1.0), (10, 25, 2.0))}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {CLIMATE: history})
    entry = await _setup(hass)
    assert _mode_kwh(entry.runtime_data) == {"cool": 3.0}


# ---------------------------------------------------------------------------------------
# D7: saved data
# ---------------------------------------------------------------------------------------
def test_d7a_a_bad_saved_slot_or_hour_is_skipped():
    tracker = ModeTracker.from_dict(
        {
            "hours": {AC: {"runs": [], "mixed": {"x": {"cool": 1.0}}}},
            "pending": {AC: {"slot": {"cool": 1.0}}},
        }
    )
    now = datetime(2026, 11, 7, 13, 3, tzinfo=UTC)
    tracker.settle(now, delay=timedelta(minutes=2), retention=timedelta(days=1))
    tracker.prune(now - timedelta(days=1))
    tracker.to_dict()


async def test_d7b_mode_history_survives_an_unrelated_bad_ledger_value(
    hass, freezer, hass_storage
):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    old = "2026-09-01T03:00:00+00:00"  # long gone from the recorder's states
    runtime.modes.hours.setdefault(AC, {})[old] = {"heat": 1.0}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert saved["modes"]["hours"][AC]["runs"] == [[old, 1, "heat"]]
    saved["totals"]["fixed"] = {"kwh": 0.0, "cost": -1e-17}  # float dust from repricing
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.modes.hour_shares(AC, old) == {"heat": 1.0}


# ---------------------------------------------------------------------------------------
# D8: relinking to a different mode entity
# ---------------------------------------------------------------------------------------
async def test_d8_relinking_to_another_entity_drops_the_old_entitys_modes(
    hass, freezer
):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    freezer.move_to("2026-11-07T13:03:00+00:00")
    entry.runtime_data._on_tick(None)
    hass.states.async_set("select.heater", "eco", {"options": ["eco", "boost"]})
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "device_modes": {AC: {"entity_id": "select.heater", "registry_id": None}},
        },
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    names = sorted(
        s.entity_id
        for s in hass.states.async_all("sensor")
        if "modes_ac" in s.entity_id and s.entity_id.endswith("_cost_total")
    )
    assert "sensor.modes_modes_ac_cool_cost_total" not in names


# ---------------------------------------------------------------------------------------
# D9: kWh total sensors are total_increasing but can fall on a restart
# ---------------------------------------------------------------------------------------
async def test_d9_state_classes(hass):
    await _setup(hass)
    # Lifetime values are rebuilt on every start and may be corrected downwards, so a
    # fall must not be read as a meter reset.
    total = hass.states.get("sensor.modes_modes_ac_cool_energy_total")
    cost = hass.states.get("sensor.modes_modes_ac_cool_cost_total")
    today = hass.states.get("sensor.modes_modes_ac_cool_energy_today")
    assert total.attributes["state_class"] == "total"
    assert cost.attributes["state_class"] == "total"
    assert today.attributes["state_class"] == "total_increasing"


# ---------------------------------------------------------------------------------------
# Scaling: `since` and `to_dict` walk every stored hour
# ---------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------
# D1c: with the mode entity in the recorder, the same restart re-attributes the hour by
# the backfill's rule, and the next restart flips it back.
# ---------------------------------------------------------------------------------------
async def test_d1c_restart_just_after_the_hour_with_recorder_history(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _set(hass, freezer, "12:31", **{AC: "11"})  # 1 kWh, arrives while cooling
    await _set(hass, freezer, "12:33", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})  # 3 kWh in dry
    before = _mode_kwh(runtime)
    assert before == {"cool": 1.0, "dry": 3.0}
    freezer.move_to("2026-11-07T13:01:00+00:00")
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
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {CLIMATE: history})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first = _mode_kwh(entry.runtime_data)
    freezer.move_to("2026-11-07T13:03:00+00:00")
    entry.runtime_data._on_tick(None)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    second = _mode_kwh(entry.runtime_data)
    assert first == before
    assert second == before


# ---------------------------------------------------------------------------------------
# Sanity checks expected to pass (solar outage groups, restore paths, two devices)
# ---------------------------------------------------------------------------------------
def _sum(runtime, field):
    prefix = mode_key(AC, "")
    return sum(
        cell[field]
        for key, cell in runtime.ledger.totals.items()
        if key.startswith(prefix)
    )


async def test_s1_solar_outage_group_modes_add_up_live_and_after_restart(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass, solar=True)
    rt = entry.runtime_data
    await _set(hass, freezer, "12:01", **{IMPORT: "unavailable", SOLAR: "55", AC: "11"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "12"})
    freezer.move_to("2026-11-07T13:03:00+00:00")
    rt._on_tick(None)
    await _set(hass, freezer, "13:09", **{IMPORT: "101", AC: "12.5"})
    freezer.move_to("2026-11-07T14:03:00+00:00")
    rt._on_tick(None)
    live = _mode_kwh(rt), _sum(rt, "cost")
    assert rt.ledger.totals[AC]["kwh"] == pytest.approx(2.5)
    assert _sum(rt, "kwh") == pytest.approx(rt.ledger.totals[AC]["kwh"])
    assert _sum(rt, "cost") == pytest.approx(rt.ledger.totals[AC]["cost"])
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        rows = {
            IMPORT: _hour_rows((13, 1.0)),
            EXPORT: _hour_rows((12, 0.0), (13, 0.0)),
            SOLAR: _hour_rows((12, 5.0), (13, 0.0)),
            AC: _hour_rows((12, 2.0), (13, 0.5)),
        }
        return {e: rows[e] for e in ids if e in rows}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-11-07T14:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rt = entry.runtime_data
    assert _sum(rt, "kwh") == pytest.approx(rt.ledger.totals[AC]["kwh"])
    assert _sum(rt, "cost") == pytest.approx(rt.ledger.totals[AC]["cost"])
    assert (_mode_kwh(rt), _sum(rt, "cost")) == (live[0], pytest.approx(live[1]))


async def test_s2_solar_long_outage_forced_rate_modes_add_up(hass, freezer):
    entry = await _setup(hass, solar=True)
    rt = entry.runtime_data
    await _set(hass, freezer, "12:01", **{IMPORT: "unavailable", SOLAR: "55", AC: "11"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "12"})
    freezer.move_to("2026-11-08T11:30:00+00:00")
    rt._on_tick(None)
    assert rt.ledger.totals[AC]["cost"] == pytest.approx(2 * RATE)
    assert _mode_kwh(rt) == {"cool": 1.0, "dry": 1.0}
    assert _sum(rt, "cost") == pytest.approx(rt.ledger.totals[AC]["cost"])


async def test_s3_device_without_recorder_rows_keeps_modes_from_saved_ledger(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await _set(hass, freezer, "12:30", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:40", **{AC: "14"})
    before = _mode_kwh(entry.runtime_data)
    freezer.move_to("2026-11-07T13:03:00+00:00")
    entry.runtime_data._on_tick(None)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        return (
            {IMPORT: _hour_rows((12, 4.0))}
            if period == "hour" and IMPORT in ids
            else {}
        )

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rt = entry.runtime_data
    assert _mode_kwh(rt) == before
    await _set(hass, freezer, "13:20", **{AC: "15"})
    assert _mode_kwh(rt) == {"cool": 1.0, "dry": 4.0}
    assert _sum(rt, "kwh") == pytest.approx(rt.ledger.totals[AC]["kwh"])


async def test_s4_restart_mid_hour_replays_the_same_split(hass, freezer, monkeypatch):
    entry = await _setup(hass)
    await _set(hass, freezer, "12:01", **{AC: "11"})
    await _set(hass, freezer, "12:02", **{CLIMATE: "dry"})
    await _set(hass, freezer, "12:03", **{AC: "14"})  # same five-minute slot, two modes
    before = _mode_kwh(entry.runtime_data)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        return {AC: _hour_rows((11, 1.0))} if period == "hour" and AC in ids else {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-11-07T12:30:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _mode_kwh(entry.runtime_data) == before == {"cool": 1.0, "dry": 3.0}


async def test_s5_time_of_use_in_sydney_live_equals_rebuild(hass, freezer, monkeypatch):
    await hass.config.async_set_time_zone("Australia/Sydney")
    freezer.move_to("2026-11-07T02:50:00+00:00")  # 13:50 local
    plan = {
        "effective_from": "2000-01-01",
        "import_bands": [
            {"name": "offpeak", "start": 0, "end": 14, "rate": 0.1},
            {"name": "peak", "start": 14, "end": 24, "rate": 0.4},
        ],
        "export_bands": [{"name": "fit", "start": 0, "end": 24, "rate": 0.05}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Modes",
        data={"provider": "Modes", "import_entity": IMPORT, "export_entity": EXPORT},
        options={
            "plans": [plan],
            "tracked_devices": [AC],
            "device_modes": {AC: {"entity_id": CLIMATE, "registry_id": None}},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rt = entry.runtime_data
    await _set(hass, freezer, "02:55", **{AC: "11"})
    await _set(hass, freezer, "03:05", **{CLIMATE: "dry"})
    await _set(hass, freezer, "03:10", **{AC: "13"})
    freezer.move_to("2026-11-07T04:03:00+00:00")
    rt._on_tick(None)
    live = {
        m: (round(_mode(rt, m)["kwh"], 6), round(_mode(rt, m)["cost"], 6))
        for m in ("cool", "dry")
    }
    assert live == {"cool": (1.0, 0.1), "dry": (2.0, 0.8)}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        return (
            {AC: _hour_rows((2, 1.0), (3, 2.0))}
            if period == "hour" and AC in ids
            else {}
        )

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-11-07T04:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rt = entry.runtime_data
    rebuilt = {
        m: (round(_mode(rt, m)["kwh"], 6), round(_mode(rt, m)["cost"], 6))
        for m in ("cool", "dry")
    }
    assert rebuilt == live
    assert _sum(rt, "cost") == pytest.approx(rt.ledger.totals[AC]["cost"])
    values = rt.mode_values(AC, "dry")
    assert values["kwh_today"] == 2.0 and values["cost_today"] == 0.8
