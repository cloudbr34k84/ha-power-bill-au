"""Solar-share pricing: tracked devices pay only for the energy they took from the grid."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_power_bill_au.const import DOMAIN
from custom_components.ha_power_bill_au.engine import UNTRACKED_KEY, solar_split

IMPORT = "sensor.solar_import"
EXPORT = "sensor.solar_export"
SOLAR = "sensor.solar_production"
EV = "sensor.solar_ev"
ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}
RATE = 0.2
FIT = 0.05


@pytest.fixture(autouse=True)
async def _inputs(recorder_mock, enable_custom_integrations, hass, freezer):
    freezer.move_to("2026-11-07T12:00:00+00:00")
    await hass.config.async_set_time_zone("UTC")
    for entity, value in ((IMPORT, "100"), (EXPORT, "20"), (SOLAR, "50"), (EV, "10")):
        hass.states.async_set(entity, value, ATTRS)


def _plan():
    return {
        "effective_from": "2000-01-01",
        "import_bands": [{"name": "flat", "start": 0, "end": 24, "rate": RATE}],
        "export_bands": [{"name": "fit", "start": 0, "end": 24, "rate": FIT}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }


async def _setup(hass, *, solar=True, export=True):
    data = {"provider": "Solar", "import_entity": IMPORT}
    if export:
        data["export_entity"] = EXPORT
    options = {"plans": [_plan()], "tracked_devices": [EV]}
    if solar:
        options["solar_entity"] = SOLAR
    entry = MockConfigEntry(domain=DOMAIN, title="Solar", data=data, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _hour(hour):
    return datetime(2026, 11, 6, hour, tzinfo=UTC).timestamp()


def _rows(*pairs):
    return [{"start": _hour(hour), "change": kwh * 1000.0} for hour, kwh in pairs]


@pytest.mark.parametrize(
    ("imported", "exported", "solar", "devices", "share", "untracked"),
    [
        (0.0, 2.0, 5.0, 2.0, 0.0, 1.0),  # exporting: everything ran on solar
        (2.0, 0.0, 2.0, 1.0, 0.5, 3.0),  # half the house came from the grid
        (3.0, 0.0, 0.0, 1.0, 1.0, 2.0),  # no solar: full grid rate
        (3.0, 0.0, 0.5, 4.0, 3 / 3.5, 0.0),  # devices over-read: no negative rest
        (2.0, 2.0, 0.5, 0.0, 1.0, 2.0),  # lagging solar: never above the grid rate
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),  # nothing used
    ],
)
def test_solar_split(imported, exported, solar, devices, share, untracked):
    assert solar_split(imported, exported, solar, devices) == pytest.approx(
        (share, untracked)
    )


async def test_history_prices_devices_at_each_hours_grid_share(hass, monkeypatch):
    rows = {
        IMPORT: _rows((9, 0.0), (10, 2.0), (11, 3.0)),
        EXPORT: _rows((9, 2.0), (10, 0.0), (11, 0.0)),
        SOLAR: _rows((9, 5.0), (10, 2.0)),
        # The 08:00 row has no import reading at all: priced at the full rate.
        EV: _rows((8, 1.0), (9, 2.0), (10, 2.0), (11, 1.0)),
    }

    def stats(hass_, start, end, ids, period, units, types):
        assert SOLAR in ids
        return {entity: rows[entity] for entity in ids} if period == "hour" else {}

    monkeypatch.setattr(
        "custom_components.ha_power_bill_au.runtime.statistics_during_period", stats
    )
    entry = await _setup(hass)
    runtime = entry.runtime_data
    ev_key = next(iter(runtime._device_keys))
    # 08:00 full (1), 09:00 free (0), 10:00 half of 2 kWh (1), 11:00 full (1)
    assert runtime.lifetime_device(EV) == pytest.approx(3 * RATE)
    # The rest of the house: 09:00 1 kWh free, 10:00 2 kWh at half, 11:00 2 kWh full.
    assert runtime.lifetime_device(UNTRACKED_KEY) == pytest.approx(3 * RATE)
    # Where an import reading exists, devices and untracked add up to the import cost.
    import_cost = runtime.ledger.lifetime_cost("import")
    assert import_cost == pytest.approx(5 * RATE)
    matched = (
        runtime.lifetime_device(EV) - RATE + runtime.lifetime_device(UNTRACKED_KEY)
    )
    assert matched == pytest.approx(import_cost)
    assert runtime.ledger.totals[ev_key]["kwh"] == pytest.approx(6.0)

    ev_total = hass.states.get("sensor.solar_solar_ev_cost_total")
    assert ev_total.attributes["pricing"] == "solar_share"
    untracked = hass.states.get("sensor.solar_untracked_usage_cost_total")
    assert float(untracked.state) == pytest.approx(3 * RATE)
    assert hass.states.get("sensor.solar_untracked_usage_cost_today") is not None


@pytest.mark.parametrize(("solar", "export"), [(False, True), (True, False)])
async def test_without_solar_and_export_devices_keep_the_full_rate(hass, solar, export):
    entry = await _setup(hass, solar=solar, export=export)
    hass.states.async_set(EV, "12", ATTRS)
    await hass.async_block_till_done()
    assert entry.runtime_data.lifetime_device(EV) == pytest.approx(2 * RATE)
    assert entry.runtime_data.device_pricing == "import_rate"
    total = hass.states.get("sensor.solar_solar_ev_cost_total")
    assert total.attributes["pricing"] == "import_rate"
    assert hass.states.get("sensor.solar_untracked_usage_cost_total") is None


STATS = "custom_components.ha_power_bill_au.runtime.statistics_during_period"


def _cost(runtime, key):
    """Unrounded ledger cost; `EV` means the tracked car, whatever its ledger key."""
    if key == EV:
        key = next(iter(runtime._device_keys))
    return runtime.ledger.lifetime_cost(key)


def _parts(runtime):
    return _cost(runtime, EV) + _cost(runtime, UNTRACKED_KEY)


def _reconciles(runtime):
    return _parts(runtime) == pytest.approx(runtime.ledger.lifetime_cost("import"))


async def _reading(hass, freezer, at, **values):
    freezer.move_to(f"2026-11-07T{at}:00+00:00")
    for entity, value in values.items():
        hass.states.async_set(entity, value, ATTRS)
    await hass.async_block_till_done()


async def _tick(runtime, freezer, at):
    freezer.move_to(f"2026-11-07T{at}:00+00:00")
    runtime._on_tick(None)


async def test_live_hours_are_priced_once_complete(hass, freezer):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    # 12:00-13:00: 1 kWh from the grid, 1 kWh of solar used, the car took 1 kWh.
    await _reading(hass, freezer, "12:01", **{IMPORT: "101", SOLAR: "51", EV: "11"})
    await _tick(runtime, freezer, "12:59")
    assert _cost(runtime, EV) == 0  # The hour is not over yet.
    await _tick(runtime, freezer, "13:01")
    assert _cost(runtime, EV) == 0  # Readings from 12:59 may still arrive.
    await _tick(runtime, freezer, "13:02")
    assert _cost(runtime, EV) == pytest.approx(0.5 * RATE)
    assert _cost(runtime, UNTRACKED_KEY) == pytest.approx(0.5 * RATE)
    assert _reconciles(runtime)
    await _tick(runtime, freezer, "13:30")  # Pricing again books nothing twice.
    assert _cost(runtime, EV) == pytest.approx(0.5 * RATE)

    # A reload continues from the saved ledger without pricing the hour again.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _tick(entry.runtime_data, freezer, "14:05")
    assert _cost(entry.runtime_data, EV) == pytest.approx(0.5 * RATE)
    assert _reconciles(entry.runtime_data)


async def test_an_import_meter_reporting_late_in_the_hour_is_shared_fairly(
    hass, freezer
):
    """A meter that reports every 10 minutes must not make devices free."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _reading(hass, freezer, "12:02", **{SOLAR: "51", EV: "11"})
    await _reading(hass, freezer, "12:07", **{SOLAR: "52", EV: "12", IMPORT: "102"})
    await _tick(runtime, freezer, "13:03")
    assert _cost(runtime, EV) == pytest.approx(2 * 0.5 * RATE)
    assert _reconciles(runtime)


async def test_an_import_outage_is_priced_with_the_hour_the_meter_returns(
    hass, freezer
):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _reading(
        hass, freezer, "12:01", **{IMPORT: "unavailable", SOLAR: "55", EV: "11"}
    )
    await _tick(runtime, freezer, "13:03")
    assert _cost(runtime, EV) == 0  # Held: the import for it is unknown.
    # The meter returns in the next hour with everything it missed.
    await _reading(hass, freezer, "13:09", **{IMPORT: "101"})
    await _tick(runtime, freezer, "14:03")
    # 1 kWh imported, 5 kWh of solar used: 6 kWh of house at a grid share of 1/6.
    assert _cost(runtime, EV) == pytest.approx(RATE / 6)
    assert _cost(runtime, UNTRACKED_KEY) == pytest.approx(5 * RATE / 6)
    assert _reconciles(runtime)


async def test_a_long_outage_falls_back_to_the_full_rate(hass, freezer):
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _reading(
        hass, freezer, "12:01", **{IMPORT: "unavailable", SOLAR: "55", EV: "11"}
    )
    freezer.move_to("2026-11-08T11:30:00+00:00")
    runtime._on_tick(None)
    assert _cost(runtime, EV) == pytest.approx(RATE)
    assert _cost(runtime, UNTRACKED_KEY) == 0  # Nothing guessed.
    freezer.move_to("2026-11-08T13:30:00+00:00")
    runtime._on_tick(None)
    # The outage from the day before expires with the buffer; today's hours are new.
    assert "2026-11-07T12:00:00+00:00" not in runtime._import_gaps
    assert "2026-11-08T13:00:00+00:00" in runtime._import_gaps


async def test_an_hour_not_priced_before_a_reload_is_priced_after_it(hass, freezer):
    entry = await _setup(hass)
    await _reading(hass, freezer, "12:01", **{IMPORT: "101", EV: "11"})
    freezer.move_to("2026-11-07T12:05:30+00:00")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _tick(entry.runtime_data, freezer, "13:05")
    assert _cost(entry.runtime_data, EV) == pytest.approx(RATE)
    assert _reconciles(entry.runtime_data)


async def test_an_hour_not_priced_before_a_restart_survives_a_later_restart(
    hass, freezer
):
    """Without recorder rows, the saved progress says where live pricing stopped."""
    entry = await _setup(hass)
    await _reading(hass, freezer, "12:01", **{IMPORT: "101", EV: "11"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.move_to("2026-11-07T13:30:00+00:00")  # Back in a later hour.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _cost(entry.runtime_data, EV) == pytest.approx(RATE)
    assert _reconciles(entry.runtime_data)


async def test_device_history_newer_than_the_import_history_is_not_booked_twice(
    hass, freezer, monkeypatch
):
    """The car has a recorder row for 12:00, the import meter not yet: the buffer prices it."""
    entry = await _setup(hass)
    await _reading(hass, freezer, "12:01", **{IMPORT: "101", EV: "11"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        return _shift({IMPORT: _rows((11, 1.0)), EV: _rows((11, 1.0), (12, 1.0))})

    monkeypatch.setattr(STATS, stats)
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # 11:00 from history, 12:00 once (from the buffer), both at the full rate.
    assert _cost(entry.runtime_data, EV) == pytest.approx(2 * RATE)


async def test_a_device_without_recorder_rows_is_not_counted_twice(
    hass, freezer, monkeypatch
):
    """Meters have recorder history, the car doesn't: its cost comes from the saved ledger."""
    entry = await _setup(hass)
    await _reading(hass, freezer, "12:01", **{IMPORT: "101", EV: "11"})
    await _tick(entry.runtime_data, freezer, "13:03")
    assert _cost(entry.runtime_data, EV) == pytest.approx(RATE)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        return {
            IMPORT: _rows((12, 1.0)),
            EXPORT: _rows((12, 0.0)),
            SOLAR: _rows((12, 0.0)),
        }

    monkeypatch.setattr(STATS, lambda *args: _shift(stats(*args)))
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _reconciles(entry.runtime_data)


def _shift(result):
    """`_rows` uses 6 November; move rows to the 7th for the live tests."""
    return {
        entity: [{**row, "start": row["start"] + 86400} for row in rows]
        for entity, rows in result.items()
    }


async def test_a_restart_never_lowers_the_totals(hass, freezer, monkeypatch):
    """Live pricing and the rebuild after a restart must agree hour by hour."""
    entry = await _setup(hass)
    runtime = entry.runtime_data
    await _reading(hass, freezer, "12:01", **{IMPORT: "101", EV: "11"})
    await _reading(hass, freezer, "12:31", **{SOLAR: "53"})
    await _tick(runtime, freezer, "13:03")
    live = (_cost(runtime, EV), _cost(runtime, UNTRACKED_KEY))
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        return _shift(
            {
                IMPORT: _rows((12, 1.0)),
                EXPORT: _rows((12, 0.0)),
                SOLAR: _rows((12, 3.0)),
                EV: _rows((12, 1.0)),
            }
        )

    monkeypatch.setattr(STATS, stats)
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rebuilt = entry.runtime_data
    assert _cost(rebuilt, EV) == pytest.approx(live[0])
    assert _cost(rebuilt, UNTRACKED_KEY) == pytest.approx(live[1])
    assert _reconciles(rebuilt)


async def test_the_repeated_hour_at_the_end_of_daylight_saving(hass, monkeypatch):
    """Australia/Sydney, 5 April 2026: local 02:00 happens twice."""
    await hass.config.async_set_time_zone("Australia/Sydney")
    first = datetime(2026, 4, 4, 15, tzinfo=UTC).timestamp()  # 02:00 AEDT
    second = datetime(2026, 4, 4, 16, tzinfo=UTC).timestamp()  # 02:00 AEST

    def twice(kwh):
        return [
            {"start": first, "change": kwh * 1000},
            {"start": second, "change": kwh * 1000},
        ]

    rows = {IMPORT: twice(2.0), EXPORT: twice(0.0), SOLAR: twice(0.0), EV: twice(1.0)}

    def stats(hass_, start, end, ids, period, units, types):
        return {entity: rows[entity] for entity in ids} if period == "hour" else {}

    monkeypatch.setattr(STATS, stats)
    entry = await _setup(hass)
    runtime = entry.runtime_data
    assert _cost(runtime, UNTRACKED_KEY) == pytest.approx(2 * RATE)
    assert _reconciles(runtime)


async def test_pending_hours_from_before_a_restart_are_priced_after_history(
    hass, freezer, monkeypatch
):
    """Hours after the recorder's last complete hour are priced from the saved buffer."""
    entry = await _setup(hass)
    await _reading(hass, freezer, "12:01", **{IMPORT: "102", EV: "12"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        # History is complete up to 12:00; the 12:00 hour comes from the buffer.
        return _shift({entity: _rows((11, 1.0)) for entity in (IMPORT, EV)})

    monkeypatch.setattr(STATS, stats)
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    # 11:00 from history plus 12:00 from the buffer, both without solar: full rate.
    assert _cost(runtime, EV) == pytest.approx(3 * RATE)
    assert _cost(runtime, UNTRACKED_KEY) == 0
    assert _reconciles(runtime)


async def test_import_history_newer_than_a_devices_is_left_to_the_buffer(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass)
    await _reading(hass, freezer, "12:01", **{IMPORT: "101", EV: "11"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "hour":
            return {}
        return _shift({IMPORT: _rows((11, 1.0), (12, 1.0)), EV: _rows((11, 1.0))})

    monkeypatch.setattr(STATS, stats)
    freezer.move_to("2026-11-07T13:10:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    assert _cost(runtime, EV) == pytest.approx(2 * RATE)
    assert _cost(runtime, UNTRACKED_KEY) == 0
    assert _reconciles(runtime)


async def test_daily_only_device_history_uses_the_days_grid_share(hass, monkeypatch):
    day = datetime(2026, 11, 1, tzinfo=UTC).timestamp()

    def stats(hass_, start, end, ids, period, units, types):
        if period != "day":
            return {}
        rows = {IMPORT: 4.0, EXPORT: 0.0, SOLAR: 4.0, EV: 2.0}
        return {
            entity: [{"start": day, "change": rows[entity] * 1000}]
            for entity in ids
            if entity in rows
        }

    monkeypatch.setattr(STATS, stats)
    entry = await _setup(hass)
    runtime = entry.runtime_data
    # 4 kWh imported of an 8 kWh day: the car pays half of its 2 kWh.
    assert _cost(runtime, EV) == pytest.approx(RATE)
    # The other 6 kWh of the house pay the rest of the day's import.
    assert _cost(runtime, UNTRACKED_KEY) == pytest.approx(3 * RATE)
    assert _reconciles(runtime)


async def test_cost_sensors_are_named_after_the_meters_device(hass):
    """A renamed meter entity must not replace the device name in the cost sensors."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    meter_entry = MockConfigEntry(domain="mqtt")
    meter_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=meter_entry.entry_id,
        identifiers={("mqtt", "clamp")},
        name="Izone Circuit EM Clamp",
    )
    hass.states.async_remove(EV)  # Let the registry hand out this entity ID.
    registry = er.async_get(hass)
    meter = registry.async_get_or_create(
        "sensor",
        "mqtt",
        "clamp_energy",
        config_entry=meter_entry,
        device_id=device.id,
        has_entity_name=True,
        original_name="Energy",
        suggested_object_id="solar_ev",
    )
    assert meter.entity_id == EV
    registry.async_update_entity(EV, name="Total Energy Consumed")
    hass.states.async_set(EV, "10", {**ATTRS, "friendly_name": "Total Energy Consumed"})

    await _setup(hass, solar=False)
    names = [
        state.attributes.get("friendly_name")
        for state in hass.states.async_all("sensor")
        if state.entity_id.startswith("sensor.solar_") and "cost" in state.entity_id
    ]
    assert "Solar Izone Circuit EM Clamp Energy cost total" in names
    assert not [name for name in names if "Total Energy Consumed" in name]
