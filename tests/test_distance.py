"""Charging cost and energy per 100 km: a tracked charger linked to a car's odometer."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_power_bill_au.const import DOMAIN
from custom_components.ha_power_bill_au.distance import (
    DistanceTracker,
    battery_level,
    distance_key,
    odometer_km,
)

IMPORT = "sensor.car_import"
EXPORT = "sensor.car_export"
SOLAR = "sensor.car_solar"
EV = "sensor.car_charger"
ODO = "sensor.car_mileage"
SOC = "sensor.car_battery"
WHERE = "device_tracker.car"
ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}
KM = {
    "device_class": "distance",
    "state_class": "total_increasing",
    "unit_of_measurement": "km",
}
PCT = {"device_class": "battery", "unit_of_measurement": "%"}
RATE = 0.2
STATS = "custom_components.ha_power_bill_au.runtime.statistics_during_period"
STATES = "custom_components.ha_power_bill_au.runtime.get_significant_states"


@pytest.fixture(autouse=True)
async def _inputs(recorder_mock, enable_custom_integrations, hass, freezer):
    freezer.move_to("2026-11-07T12:00:00+00:00")
    await hass.config.async_set_time_zone("UTC")
    for entity, value in ((IMPORT, "100"), (EXPORT, "20"), (SOLAR, "50"), (EV, "10")):
        hass.states.async_set(entity, value, ATTRS)
    hass.states.async_set(ODO, "1000", KM)
    hass.states.async_set(SOC, "50", PCT)
    hass.states.async_set(WHERE, "home")


def _link(*, location=True, battery=True, odometer=ODO):
    link = {"odometer": {"entity_id": odometer, "registry_id": None}}
    if location:
        link["location"] = {"entity_id": WHERE, "registry_id": None}
    if battery:
        link["battery"] = {"entity_id": SOC, "registry_id": None}
    return link


def _options(*, solar=False, link=None):
    plan = {
        "effective_from": "2000-01-01",
        "import_bands": [{"name": "flat", "start": 0, "end": 24, "rate": RATE}],
        "export_bands": [{"name": "fit", "start": 0, "end": 24, "rate": 0.05}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }
    options = {"plans": [plan], "tracked_devices": [EV]}
    if solar:
        options["solar_entity"] = SOLAR
    if link is not None:
        options["device_distance"] = {EV: link}
    return options


async def _setup(hass, **kwargs):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Car",
        data={"provider": "Car", "import_entity": IMPORT, "export_entity": EXPORT},
        options=_options(**kwargs),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _set(hass, freezer, at, **values):
    freezer.move_to(at)
    for entity, value in values.items():
        attrs = {ODO: KM, SOC: PCT, WHERE: {}}.get(entity, ATTRS)
        hass.states.async_set(entity, value, attrs)
    await hass.async_block_till_done()


def _day_rows(*pairs):
    """Daily statistics: (day of November, closing odometer km)."""
    return [
        {"start": datetime(2026, 11, day, tzinfo=UTC).timestamp(), "state": km}
        for day, km in pairs
    ]


def _hour_rows(*triples):
    """Hourly energy statistics: (day, hour, kWh)."""
    return [
        {
            "start": datetime(2026, 11, day, hour, tzinfo=UTC).timestamp(),
            "change": kwh * 1000,
        }
        for day, hour, kwh in triples
    ]


def _charger_days(hour_rows):
    """Daily statistics for the charger, as the recorder keeps them beside hourly ones."""
    days = {}
    for row in hour_rows:
        start = datetime.fromtimestamp(row["start"], UTC)
        day = datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp()
        days[day] = days.get(day, 0.0) + row["change"]
    return [{"start": day, "change": change} for day, change in sorted(days.items())]


def _values(entry):
    return entry.runtime_data.distance_values(EV)


def _state(hass, suffix):
    return hass.states.get(f"sensor.car_car_charger_{suffix}")


async def test_history_starts_the_window_where_the_odometer_data_starts(
    hass, monkeypatch
):
    """Charger costs before the odometer's first full day are not divided by kilometres."""
    # 2 kWh on the 3rd (before the window), 5 kWh on the 5th, 5 kWh on the 6th.
    charger = _hour_rows((3, 8, 2.0), (5, 8, 5.0), (6, 8, 5.0))

    def stats(hass_, start, end, ids, period, units, types):
        if period == "day" and ODO in ids:
            assert {"state", "change"} <= types
            closing = _day_rows(
                (3, 900.0), (4, 930.0), (5, 960.0), (6, 990.0), (7, 999.0)
            )
            return {ODO: closing, EV: _charger_days(charger)}
        if period == "hour" and EV in ids:
            return {EV: charger}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    hass.states.async_set(ODO, "1000", KM)
    entry = await _setup(hass, link=_link())
    values = _values(entry)
    # The window starts on the 4th, from the 3rd's closing reading of 900 km.
    assert values["since"] == "2026-11-04"
    assert values["km_total"] == 100.0
    assert values["km_month"] == 100.0  # Started this month: the same window.
    assert values["cost_100_total"] == pytest.approx(10 * RATE)  # 10 kWh, 100 km
    assert values["kwh_100_total"] == pytest.approx(10.0)
    assert entry.runtime_data.ledger.lifetime_cost(EV) == pytest.approx(12 * RATE)

    total = _state(hass, "charging_cost_per_100_km_total")
    assert float(total.state) == pytest.approx(2.0)
    assert total.attributes["since"] == "2026-11-04"
    assert total.attributes["unit_of_measurement"] == "AUD/100 km"
    energy = _state(hass, "energy_per_100_km_total")
    assert energy.attributes["device_class"] == "energy_distance"
    assert float(_state(hass, "distance_total").state) == 100.0
    assert _state(hass, "distance_total").attributes["state_class"] == "total"


async def test_the_month_window_starts_at_the_month_and_rolls_over(
    hass, freezer, monkeypatch
):
    charger = [
        {"start": datetime(2026, 10, 20, 8, tzinfo=UTC).timestamp(), "change": 0.0},
        {
            "start": datetime(2026, 10, 25, 8, tzinfo=UTC).timestamp(),
            "change": 10_000.0,
        },
        *_hour_rows((2, 8, 4.0)),
    ]

    def stats(hass_, start, end, ids, period, units, types):
        if period == "day" and ODO in ids:
            rows = [
                {
                    "start": datetime(2026, 10, 20, tzinfo=UTC).timestamp(),
                    "state": 500.0,
                },
                {
                    "start": datetime(2026, 10, 31, tzinfo=UTC).timestamp(),
                    "state": 800.0,
                },
            ]
            return {ODO: rows + _day_rows((6, 990.0)), EV: _charger_days(charger)}
        if period == "hour" and EV in ids:
            return {EV: charger}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    entry = await _setup(hass, link=_link(location=False, battery=False))
    values = _values(entry)
    assert values["since"] == "2026-10-21"
    assert values["km_total"] == 500.0
    assert values["km_month"] == 200.0  # From 31 October's closing 800 km.
    assert values["cost_100_month"] == pytest.approx(4 * RATE / 2)
    assert values["cost_100_total"] == pytest.approx(14 * RATE / 5)

    # Driving on, and into December: the month starts again from the last reading.
    await _set(hass, freezer, "2026-11-30T20:00:00+00:00", **{ODO: "1100"})
    assert _values(entry)["km_month"] == 300.0
    await _set(hass, freezer, "2026-12-01T00:01:00+00:00")
    entry.runtime_data._on_tick(None)
    await _set(hass, freezer, "2026-12-01T09:00:00+00:00", **{ODO: "1150"})
    values = _values(entry)
    assert values["km_month"] == 50.0
    assert values["km_total"] == 650.0
    assert values["cost_100_month"] == 0.0


async def test_without_history_the_window_starts_with_todays_reading(hass, freezer):
    entry = await _setup(hass, link=_link(location=False, battery=False))
    assert _values(entry)["since"] == "2026-11-07"
    assert _values(entry)["cost_100_total"] is None  # Under 1 km: no figure yet.
    await _set(hass, freezer, "2026-11-07T13:00:00+00:00", **{EV: "15"})
    await _set(hass, freezer, "2026-11-07T18:00:00+00:00", **{ODO: "1050"})
    values = _values(entry)
    assert values["km_total"] == 50.0
    assert values["cost_100_total"] == pytest.approx(5 * RATE * 2)
    assert values["kwh_100_total"] == pytest.approx(10.0)

    # A restart (no recorder rows yet) keeps the start, the charger costs and the
    # reading, even with the odometer offline.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(ODO, "unavailable", KM)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _values(entry) == values


async def test_an_odometer_offline_at_the_start_begins_with_its_first_reading(
    hass, freezer
):
    hass.states.async_set(ODO, "unavailable", KM)
    entry = await _setup(hass, link=_link(location=False, battery=False))
    assert _values(entry) == {}
    assert _state(hass, "distance_total").state == "unknown"
    await _set(hass, freezer, "2026-11-07T13:00:00+00:00", **{ODO: "2000"})
    await _set(hass, freezer, "2026-11-07T14:00:00+00:00", **{EV: "12", ODO: "2020"})
    values = _values(entry)
    assert values["km_total"] == 20.0
    assert values["cost_100_total"] == pytest.approx(2 * RATE * 5)


async def test_with_solar_the_per_km_cost_uses_the_solar_share(hass, freezer):
    entry = await _setup(hass, solar=True, link=_link(location=False, battery=False))
    # The car took 2 kWh while half the house's energy came from the grid.
    await _set(
        hass,
        freezer,
        "2026-11-07T12:10:00+00:00",
        **{EV: "12", IMPORT: "102", SOLAR: "52", ODO: "1020"},
    )
    await _set(hass, freezer, "2026-11-07T13:03:00+00:00")
    entry.runtime_data._on_tick(None)
    values = _values(entry)
    assert values["cost_100_total"] == pytest.approx(2 * 0.5 * RATE * 5)
    assert values["kwh_100_total"] == pytest.approx(10.0)


async def test_miles_are_converted_and_odometer_resets_never_go_negative(hass, freezer):
    hass.states.async_set(ODO, "100", {**KM, "unit_of_measurement": "mi"})
    entry = await _setup(hass, link=_link(location=False, battery=False))
    hass.states.async_set(ODO, "110", {**KM, "unit_of_measurement": "mi"})
    await hass.async_block_till_done()
    assert _values(entry)["km_total"] == pytest.approx(16.1)
    hass.states.async_set(ODO, "5", {**KM, "unit_of_measurement": "mi"})
    await hass.async_block_till_done()
    assert _values(entry)["km_total"] == pytest.approx(16.1)
    assert odometer_km(State(ODO, "10", {"unit_of_measurement": "kWh"})) is None
    assert odometer_km(State(ODO, "-1", {"unit_of_measurement": "km"})) is None
    assert odometer_km(None) is None
    assert battery_level(State(SOC, "55", PCT)) == 55.0
    assert battery_level(State(SOC, "55", ATTRS)) is None  # Not a percentage.
    assert battery_level(State(SOC, "101", PCT)) is None


async def test_charging_away_from_home_is_reported(hass, freezer):
    entry = await _setup(hass, link=_link())
    await _set(hass, freezer, "2026-11-07T13:00:00+00:00", **{SOC: "80"})  # +30 home
    await _set(hass, freezer, "2026-11-07T15:00:00+00:00", **{WHERE: "not_home"})
    await _set(hass, freezer, "2026-11-07T15:30:00+00:00", **{SOC: "70"})  # driving
    await _set(hass, freezer, "2026-11-07T16:00:00+00:00", **{SOC: "80"})  # +10 away
    await _set(hass, freezer, "2026-11-07T16:05:00+00:00", **{SOC: "80.4"})  # noise
    await _set(hass, freezer, "2026-11-07T17:00:00+00:00", **{WHERE: "unavailable"})
    await _set(hass, freezer, "2026-11-07T17:30:00+00:00", **{SOC: "90"})  # unknown
    values = _values(entry)
    assert values["away_month"] == pytest.approx(25.0)
    assert values["away_total"] == pytest.approx(25.0)
    await _set(hass, freezer, "2026-11-07T18:00:00+00:00", **{ODO: "1100"})
    cost = _state(hass, "charging_cost_per_100_km_month_to_date")
    assert cost.attributes["away_charging_percent"] == pytest.approx(25.0)


async def test_a_new_link_finds_the_last_weeks_charging(hass, monkeypatch):
    def at(day, hour):
        return datetime(2026, 11, day, hour, tzinfo=UTC)

    history = {
        WHERE: [
            State(WHERE, "home", last_changed=at(2, 0)),
            State(WHERE, "not_home", last_changed=at(4, 9)),
            State(WHERE, "home", last_changed=at(4, 18)),
        ],
        SOC: [
            State(SOC, "40", PCT, last_changed=at(2, 1)),
            State(SOC, "90", PCT, last_changed=at(3, 6)),  # +50 at home
            State(SOC, "60", PCT, last_changed=at(4, 10)),
            State(SOC, "unavailable", PCT, last_changed=at(4, 11)),
            State(SOC, "80", PCT, last_changed=at(4, 12)),  # +20 away
        ],
    }
    monkeypatch.setattr(STATES, lambda *args, **kwargs: history)
    entry = await _setup(hass, link=_link())
    tracker = entry.runtime_data.distance
    assert tracker.charge_split(EV, date(2026, 11, 1)) == (50.0, 20.0)
    # The backfill runs once: a restart doesn't add the week again.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.distance.charge_split(EV, date(2026, 11, 1)) == (
        50.0,
        20.0,
    )


async def test_unreadable_history_only_delays_the_figures(hass, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def broken(*args, **kwargs):
        raise OperationalError("select", {}, Exception("database is locked"))

    def stats(hass_, start, end, ids, period, units, types):
        if ODO in ids:
            broken()
        return {}

    monkeypatch.setattr(STATS, stats)  # The energy history still reads fine.
    monkeypatch.setattr(STATES, broken)
    entry = await _setup(hass, link=_link())
    assert entry.runtime_data.distance_values(EV)["since"] == "2026-11-07"


async def test_linking_another_odometer_starts_again_and_unlinking_cleans_up(
    hass, freezer
):
    entry = await _setup(hass, link=_link(location=False, battery=False))
    await _set(hass, freezer, "2026-11-07T13:00:00+00:00", **{ODO: "1100"})
    assert _values(entry)["km_total"] == 100.0

    hass.states.async_set("sensor.other_odometer", "5000", KM)
    hass.config_entries.async_update_entry(
        entry,
        options=_options(
            link=_link(location=False, battery=False, odometer="sensor.other_odometer")
        ),
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _values(entry)["km_total"] == 0.0

    hass.config_entries.async_update_entry(entry, options=_options())
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, "distance_total") is None
    assert distance_key(EV) not in entry.runtime_data.ledger.totals
    assert entry.runtime_data.distance.points == {}


async def test_without_the_recorder_an_unlinked_cars_costs_are_dropped(hass, freezer):
    entry = await _setup(hass, link=_link(location=False, battery=False))
    await _set(hass, freezer, "2026-11-07T13:00:00+00:00", **{EV: "11", ODO: "1010"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options=_options())
    hass.config.components.remove("recorder")
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert distance_key(EV) not in entry.runtime_data.ledger.totals
        assert entry.runtime_data.ledger.lifetime_cost(EV) == pytest.approx(RATE)
    finally:
        hass.config.components.add("recorder")


async def test_a_renamed_odometer_is_followed(hass, freezer):
    hass.states.async_remove(ODO)
    registry = er.async_get(hass)
    odometer = registry.async_get_or_create(
        "sensor", "test", "odo", suggested_object_id="car_mileage"
    )
    hass.states.async_set(ODO, "1000", KM)
    link = _link(location=False, battery=False)
    link["odometer"]["registry_id"] = odometer.id
    entry = await _setup(hass, link=link)
    registry.async_update_entity(ODO, new_entity_id="sensor.i4_mileage")
    hass.states.async_set("sensor.i4_mileage", "1040", KM)
    await hass.async_block_till_done()
    assert entry.options["device_distance"][EV]["odometer"]["entity_id"] == (
        "sensor.i4_mileage"
    )
    assert _values(entry)["km_total"] == 40.0  # The same window, followed.


async def test_a_reading_while_history_is_read_is_not_missed(hass, monkeypatch):
    from custom_components.ha_power_bill_au.runtime import TariffRuntime

    def stats(hass_, start, end, ids, period, units, types):
        if period == "day" and ODO in ids:
            charger = _charger_days(_hour_rows((3, 8, 1.0)))
            return {ODO: _day_rows((3, 900.0)), EV: charger}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    original = TariffRuntime._async_backfill

    async def backfill_while_the_car_reports(self, now):
        # After the odometer was first read, before its changes are listened to.
        self.hass.states.async_set(ODO, "1030", KM)
        await original(self, now)

    monkeypatch.setattr(
        TariffRuntime, "_async_backfill", backfill_while_the_car_reports
    )
    entry = await _setup(hass, link=_link(location=False, battery=False))
    assert _values(entry)["km_total"] == 130.0


def test_saved_distance_data_round_trips_and_survives_bad_data():
    tracker = DistanceTracker()
    tracker.points[EV] = {
        "start_day": "2026-11-04",
        "start_km": 900.0,
        "month": "2026-11",
        "month_km": 900.0,
        "last_km": 1000.0,
        "source": "id",
    }
    tracker.note_charge(EV, 40.0, True, date(2026, 11, 5))
    tracker.note_charge(EV, 60.0, True, date(2026, 11, 5))
    tracker.note_charge(EV, 70.0, False, date(2026, 11, 6))
    loaded = DistanceTracker.from_dict(tracker.to_dict())
    assert loaded.points == tracker.points
    assert loaded.charges == {
        EV: {"2026-11-05": [20.0, 0.0], "2026-11-06": [0.0, 10.0]}
    }
    assert loaded.charge_split(EV, date(2026, 11, 6)) == (0.0, 10.0)
    assert loaded.last_level == {EV: 70.0}

    damaged = DistanceTracker.from_dict(
        {
            "points": {
                EV: {"start_day": "never", "start_km": 1},
                "x": "bad",
                "y": {
                    "start_day": "2026-11-01",
                    "start_km": 5,
                    "month_km": -1,
                    "month": 3,
                },
            },
            "charges": {
                EV: {"bad-day": [1, 2], "2026-11-01": [1], "2026-11-02": [1, -2]},
                "z": 1,
            },
            "last_level": {EV: 140, "y": 50},
        }
    )
    assert damaged.points == {"y": {"start_day": "2026-11-01", "start_km": 5.0}}
    assert damaged.charges == {}
    assert damaged.last_level == {"y": 50.0}
    assert DistanceTracker.from_dict(None).points == {}
