"""Cost-per-100 km regressions found in review: backfill, odometer glitches, windows."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from homeassistant.core import State

from tests.test_distance import (  # noqa: F401 - _inputs is the autouse fixture
    EV,
    KM,
    ODO,
    PCT,
    RATE,
    SOC,
    STATES,
    STATS,
    WHERE,
    _charger_days,
    _day_rows,
    _hour_rows,
    _inputs,
    _link,
    _options,
    _set,
    _setup,
    _values,
)

ODO2 = "sensor.other_car_mileage"


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


# 1. Backfill: two rows of one entity with the same last_changed (attribute-only update)
async def test_x1_backfill_with_attribute_only_updates_does_not_fail_setup(
    hass, monkeypatch
):
    history = {
        WHERE: [
            State(WHERE, "home", {"latitude": 1.0}, last_changed=at(3, 0)),
            # A GPS update while home: same state, same last_changed.
            State(WHERE, "home", {"latitude": 1.1}, last_changed=at(3, 0)),
        ],
        SOC: [
            State(SOC, "40", PCT, last_changed=at(3, 1)),
            State(SOC, "60", PCT, last_changed=at(3, 2)),
        ],
    }
    monkeypatch.setattr(STATES, lambda *args, **kwargs: history)
    entry = await _setup(hass, link=_link())
    assert entry.runtime_data.distance.charge_split(EV, date(2026, 11, 1)) == (
        20.0,
        0.0,
    )


# 2. Charging reported in steps smaller than one point is never counted
async def test_x2_charging_in_half_percent_steps_is_counted(hass, freezer):
    entry = await _setup(hass, link=_link())
    level = 50.0
    for step in range(40):  # 50 % -> 70 % in 0.5 % steps, at home
        level += 0.5
        await _set(hass, freezer, at(7, 13, step), **{SOC: f"{level}"})
    home, away = entry.runtime_data.distance.charge_split(EV, date(2026, 11, 1))
    assert home == pytest.approx(20.0)


# 3. One bad high odometer reading is kept forever
async def test_x3_one_bad_high_reading_does_not_stick(hass, freezer):
    entry = await _setup(hass, link=_link())
    await _set(hass, freezer, at(7, 13), **{ODO: "1100"})
    await _set(hass, freezer, at(7, 14), **{ODO: "999999"})  # glitch
    await _set(hass, freezer, at(7, 15), **{ODO: "1120"})
    values = _values(entry)
    assert values["km_total"] == pytest.approx(120.0)


# 4. The odometer's statistics start long before the charger's
async def test_x4_window_does_not_start_before_the_chargers_data(hass, monkeypatch):
    def stats(hass_, start, end, ids, period, units, types):
        charger = _hour_rows((5, 8, 5.0), (6, 8, 5.0))
        if period == "day" and ODO in ids:
            # A car with statistics from the 1st; the charger was installed on the 5th.
            odometer = _day_rows((1, 500.0), (4, 900.0), (6, 990.0))
            return {ODO: odometer, EV: _charger_days(charger)}
        if period == "hour" and EV in ids:
            return {EV: charger}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    hass.states.async_set(ODO, "1000", KM)
    entry = await _setup(hass, link=_link())
    values = _values(entry)
    # 10 kWh charged over the 100 km driven since the charger has data.
    assert values["cost_100_total"] == pytest.approx(10 * RATE)


# 5. Relinking another odometer when the charger has no recorder rows
async def test_x5_another_odometer_does_not_inherit_the_old_cost(
    hass, freezer, monkeypatch
):
    monkeypatch.setattr(STATS, lambda *args: {})
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    entry = await _setup(hass, link=_link())
    await _set(hass, freezer, at(7, 13), **{EV: "20"})  # 10 kWh for the first car
    assert entry.runtime_data.ledger.totals[f"distance|{EV}"]["kwh"] == pytest.approx(
        10
    )
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(ODO2, "5000", KM)
    hass.config_entries.async_update_entry(
        entry,
        options=_options(link=_link(odometer=ODO2, location=False, battery=False)),
    )
    freezer.move_to(at(7, 14))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    freezer.move_to(at(7, 15))
    hass.states.async_set(ODO2, "5100", KM)
    await _set(hass, freezer, at(7, 15), **{EV: "21"})  # 1 kWh, 100 km
    values = _values(entry)
    assert values["kwh_100_total"] == pytest.approx(1.0)


# 6. An odometer without statistics, HA off across the month boundary
async def test_x6_month_driven_before_a_restart_in_a_new_month_is_counted(
    hass, freezer, monkeypatch
):
    monkeypatch.setattr(STATS, lambda *args: {})
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    freezer.move_to("2026-10-30T12:00:00+00:00")
    hass.states.async_set(ODO, "1000", KM)
    entry = await _setup(hass, link=_link())
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Off until the 5th; 200 km driven in November before HA is back.
    freezer.move_to("2026-11-05T12:00:00+00:00")
    hass.states.async_set(ODO, "1200", KM)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data._on_tick(None)
    values = _values(entry)
    assert values["km_month"] == pytest.approx(200.0)


# 1b. Same, with the real recorder: a device tracker's GPS updates while home
async def test_x1b_real_recorder_gps_updates_do_not_fail_setup(hass, freezer):
    from homeassistant.components.recorder import get_instance

    hass.states.async_remove(WHERE)  # Recorded from the 6th, not the fixture's time.
    freezer.move_to(at(6, 10))
    hass.states.async_set(WHERE, "home", {"latitude": 1.0})
    hass.states.async_set(SOC, "40", PCT)
    await hass.async_block_till_done()
    freezer.move_to(at(6, 11))
    hass.states.async_set(WHERE, "home", {"latitude": 1.1})
    hass.states.async_set(SOC, "60", PCT)
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    freezer.move_to(at(7, 12))
    entry = await _setup(hass, link=_link())
    assert entry.runtime_data.distance.charge_split(EV, date(2026, 11, 1)) == (
        20.0,
        0.0,
    )


async def test_a_long_drive_while_ha_was_off_is_accepted_but_glitches_are_not(
    hass, freezer
):
    entry = await _setup(hass, link=_link(location=False, battery=False))
    # Faster than any car, right after the last reading: a glitch, ignored.
    await _set(hass, freezer, at(7, 12, 30), **{ODO: "1400"})
    assert _values(entry)["km_total"] == 0.0
    # Backwards: a glitch too.
    await _set(hass, freezer, at(7, 13), **{ODO: "990"})
    assert _values(entry)["km_total"] == 0.0
    # HA was off for a day while the car did a 1,500 km road trip.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.move_to(at(8, 18))
    hass.states.async_set(ODO, "2500", KM)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _values(entry)["km_total"] == 1500.0


async def test_a_rise_while_the_location_is_unknown_is_not_counted_later(hass, freezer):
    entry = await _setup(hass, link=_link())
    await _set(hass, freezer, at(7, 13), **{WHERE: "unavailable"})
    await _set(hass, freezer, at(7, 14), **{SOC: "70"})  # Where? Not counted.
    await _set(hass, freezer, at(7, 15), **{WHERE: "home"})
    await _set(hass, freezer, at(7, 16), **{SOC: "75"})  # Only this rise counts.
    tracker = entry.runtime_data.distance
    assert tracker.charge_split(EV, date(2026, 11, 1)) == (5.0, 0.0)


async def test_todays_partial_row_is_not_a_closing_reading(hass, monkeypatch):
    def stats(hass_, start, end, ids, period, units, types):
        if period == "day" and ODO in ids:
            # Only today's (still open) row: no complete day to start from.
            return {
                ODO: _day_rows((7, 990.0)),
                EV: _charger_days(_hour_rows((7, 8, 1.0))),
            }
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    entry = await _setup(hass, link=_link(location=False, battery=False))
    values = _values(entry)
    assert values["km_total"] == 0.0  # Starts from the live reading, not 990.
    assert values["since"] == "2026-11-07"


async def test_per_100_km_needs_a_whole_km(hass, freezer):
    entry = await _setup(hass, link=_link(location=False, battery=False))
    await _set(hass, freezer, at(7, 13), **{EV: "11", ODO: "1000.5"})
    assert _values(entry)["cost_100_total"] is None
    await _set(hass, freezer, at(7, 14), **{ODO: "1002"})
    assert _values(entry)["cost_100_total"] == pytest.approx(RATE / 2 * 100)


async def test_relinking_with_recorder_history_starts_at_the_link(
    hass, freezer, monkeypatch
):
    """The new car's window must not reach back into the old car's charging."""
    charger = _hour_rows((5, 8, 4.0), (7, 9, 10.0))  # the old car, including today

    def stats(hass_, start, end, ids, period, units, types):
        if period == "day":
            return {
                ODO2: _day_rows((3, 4000.0), (6, 4900.0)),
                ODO: _day_rows((3, 900.0)),
                EV: _charger_days(charger),
            }
        if period == "hour" and EV in ids:
            return {EV: charger}
        return {}

    monkeypatch.setattr(STATS, stats)
    monkeypatch.setattr(STATES, lambda *args, **kwargs: {})
    entry = await _setup(hass, link=_link(location=False, battery=False))
    assert _values(entry)["since"] == "2026-11-05"  # From when the charger has data.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    freezer.move_to(at(7, 14))
    hass.states.async_set(ODO2, "5000", KM)
    hass.config_entries.async_update_entry(
        entry,
        options=_options(link=_link(odometer=ODO2, location=False, battery=False)),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    freezer.move_to(at(7, 15))
    hass.states.async_set(ODO2, "5100", KM)
    await _set(hass, freezer, at(7, 15), **{EV: "11"})  # the new car: 1 kWh, 100 km
    values = _values(entry)
    assert values["since"] == "2026-11-07"
    assert values["km_total"] == 100.0
    assert values["kwh_100_total"] == pytest.approx(1.0)  # Not the 09:00 charge.


async def test_any_failure_in_the_charging_backfill_leaves_setup_working(
    hass, monkeypatch
):
    malformed = State(SOC, "60", PCT, last_changed=at(3, 2))
    malformed.last_changed = "not a time"  # Can't be ordered against the others.
    history = {SOC: [State(SOC, "40", PCT, last_changed=at(3, 1)), malformed]}
    monkeypatch.setattr(STATES, lambda *args, **kwargs: history)
    entry = await _setup(hass, link=_link())
    assert entry.runtime_data.distance_values(EV)["since"] == "2026-11-07"


async def test_y1_a_glitch_after_a_quiet_day_does_not_freeze_the_odometer(
    hass, freezer
):
    entry = await _setup(hass, link=_link())  # 1000 km at 7 Nov 12:00
    # Parked for two days, then the integration reconnects with a garbage reading
    # that is within 250 km/h x 50 h of the last one, and then the real reading.
    await _set(hass, freezer, at(9, 14), **{ODO: "11000"})
    await _set(hass, freezer, at(9, 15), **{ODO: "1020"})
    await _set(hass, freezer, at(9, 18), **{ODO: "1100"})
    values = _values(entry)
    assert values["km_total"] == pytest.approx(100.0)


async def test_y2_a_downward_correction_is_followed(hass, freezer):
    entry = await _setup(hass, link=_link())  # 1000 km
    await _set(hass, freezer, at(7, 13), **{ODO: "1040"})  # 40 km a bit high
    # The car's integration corrects itself; driving continues from 1030.
    await _set(hass, freezer, at(7, 14), **{ODO: "1030"})
    await _set(hass, freezer, at(8, 9), **{ODO: "1130"})
    await _set(hass, freezer, at(8, 18), **{ODO: "1230"})
    values = _values(entry)
    assert values["km_total"] == pytest.approx(230.0)


async def test_a_long_trip_while_nothing_reported_counts_once_confirmed(hass, freezer):
    entry = await _setup(hass, link=_link(location=False, battery=False))
    # A 3,000 km road trip over two days with no readings: held, then confirmed.
    await _set(hass, freezer, at(9, 12), **{ODO: "4000"})
    assert _values(entry)["km_total"] == 0.0
    await _set(hass, freezer, at(9, 13), **{ODO: "4060"})
    assert _values(entry)["km_total"] == 3060.0
    # A held reading that is never confirmed is dropped by the next normal one.
    await _set(hass, freezer, at(12, 12), **{ODO: "99999"})
    await _set(hass, freezer, at(12, 13), **{ODO: "4100"})
    await _set(hass, freezer, at(12, 14), **{ODO: "99999"})
    assert _values(entry)["km_total"] == 3100.0


async def test_two_different_glitches_do_not_confirm_each_other(hass, freezer):
    entry = await _setup(hass, link=_link(location=False, battery=False))
    # Possible after two days parked, but more than 1,500 km: held for confirmation.
    await _set(hass, freezer, at(9, 12), **{ODO: "9000"})
    await _set(hass, freezer, at(9, 13), **{ODO: "6000"})  # lower: not a confirmation
    assert _values(entry)["km_total"] == 0.0
    await _set(hass, freezer, at(9, 14), **{ODO: "1050"})
    assert _values(entry)["km_total"] == 50.0
