"""Solar pricing regressions found in review: restarts, long outages, half-hour zones."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_power_bill_au.const import DOMAIN
from custom_components.ha_power_bill_au.engine import UNTRACKED_KEY

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
STATS = "custom_components.ha_power_bill_au.runtime.statistics_during_period"


@pytest.fixture(autouse=True)
async def _inputs(recorder_mock, enable_custom_integrations, hass, freezer):
    freezer.move_to("2026-11-07T12:00:00+00:00")
    await hass.config.async_set_time_zone("UTC")
    for entity, value in ((IMPORT, "100"), (EXPORT, "20"), (SOLAR, "50"), (EV, "10")):
        hass.states.async_set(entity, value, ATTRS)


async def _setup(hass):
    plan = {
        "effective_from": "2000-01-01",
        "import_bands": [{"name": "flat", "start": 0, "end": 24, "rate": RATE}],
        "export_bands": [{"name": "fit", "start": 0, "end": 24, "rate": 0.05}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Solar",
        data={"provider": "Solar", "import_entity": IMPORT, "export_entity": EXPORT},
        options={"plans": [plan], "tracked_devices": [EV], "solar_entity": SOLAR},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _cost(rt, key):
    if key == EV:
        key = next(iter(rt._device_keys))
    return rt.ledger.lifetime_cost(key)


async def _set(hass, freezer, at, **values):
    freezer.move_to(at)
    for entity, value in values.items():
        hass.states.async_set(entity, value, ATTRS)
    await hass.async_block_till_done()


def _row(day, hour, kwh):
    return {
        "start": datetime(2026, 11, day, hour, tzinfo=UTC).timestamp(),
        "change": kwh * 1000.0,
    }


async def _restart(hass, entry, monkeypatch, freezer, at, rows):
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    monkeypatch.setattr(
        STATS,
        lambda h, s, e, ids, period, u, t: (
            {k: v for k, v in rows.items() if k in ids} if period == "hour" else {}
        ),
    )
    freezer.move_to(at)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


@pytest.mark.parametrize("import_row_in_outage_hour", [True, False])
async def test_an_outage_group_is_priced_the_same_after_a_restart(
    hass, freezer, monkeypatch, import_row_in_outage_hour
):
    entry = await _setup(hass)
    rt = entry.runtime_data
    await _set(
        hass,
        freezer,
        "2026-11-07T12:01:00+00:00",
        **{IMPORT: "unavailable", SOLAR: "55", EV: "11"},
    )
    freezer.move_to("2026-11-07T13:03:00+00:00")
    rt._on_tick(None)
    await _set(hass, freezer, "2026-11-07T13:09:00+00:00", **{IMPORT: "101"})
    freezer.move_to("2026-11-07T14:03:00+00:00")
    rt._on_tick(None)
    live = (_cost(rt, EV), _cost(rt, UNTRACKED_KEY))
    assert live == pytest.approx((RATE / 6, 5 * RATE / 6))
    imp = [_row(7, 13, 1.0)]
    if import_row_in_outage_hour:
        imp.insert(0, _row(7, 12, 0.0))
    rows = {
        IMPORT: imp,
        EXPORT: [_row(7, 12, 0.0), _row(7, 13, 0.0)],
        SOLAR: [_row(7, 12, 5.0), _row(7, 13, 0.0)],
        EV: [_row(7, 12, 1.0), _row(7, 13, 0.0)],
    }
    rt = await _restart(
        hass, entry, monkeypatch, freezer, "2026-11-07T14:10:00+00:00", rows
    )
    after = (_cost(rt, EV), _cost(rt, UNTRACKED_KEY))
    assert after == pytest.approx(live)


async def test_downtime_over_a_day_keeps_the_last_hour_from_the_recorder(
    hass, freezer, monkeypatch
):
    entry = await _setup(hass)
    await _set(hass, freezer, "2026-11-07T12:01:00+00:00", **{IMPORT: "101", EV: "11"})
    freezer.move_to("2026-11-07T12:30:00+00:00")
    rows = {
        IMPORT: [_row(7, 12, 1.0)],
        EXPORT: [_row(7, 12, 0.0)],
        SOLAR: [_row(7, 12, 0.0)],
        EV: [_row(7, 12, 1.0)],
    }
    rt = await _restart(
        hass, entry, monkeypatch, freezer, "2026-11-09T10:00:00+00:00", rows
    )
    assert _cost(rt, EV) == pytest.approx(RATE)


async def test_a_long_outage_is_not_counted_twice_when_the_meter_returns(hass, freezer):
    entry = await _setup(hass)
    rt = entry.runtime_data
    await _set(
        hass, freezer, "2026-11-07T12:01:00+00:00", **{IMPORT: "unavailable", EV: "11"}
    )
    # ticks every hour while offline, as the 1-minute timer would
    for h in range(13, 24):
        freezer.move_to(f"2026-11-07T{h:02d}:30:00+00:00")
        rt._on_tick(None)
    for h in range(0, 12):
        freezer.move_to(f"2026-11-08T{h:02d}:30:00+00:00")
        rt._on_tick(None)
    assert _cost(rt, EV) == pytest.approx(RATE)  # forced at full rate
    await _set(hass, freezer, "2026-11-08T11:40:00+00:00", **{IMPORT: "101"})
    freezer.move_to("2026-11-08T12:03:00+00:00")
    rt._on_tick(None)
    parts = _cost(rt, EV) + _cost(rt, UNTRACKED_KEY)
    assert parts == pytest.approx(rt.ledger.lifetime_cost("import"))


async def test_half_hour_time_zone_prices_each_slot_in_its_own_band(hass, freezer):
    """Adelaide (+10:30): UTC hour 06:00-07:00 is 16:30-17:30 local; peak starts 17:00."""
    await hass.config.async_set_time_zone("Australia/Adelaide")
    plan = {
        "effective_from": "2000-01-01",
        "import_bands": [
            {"name": "off", "start": 0, "end": 17, "rate": 0.2},
            {"name": "peak", "start": 17, "end": 21, "rate": 0.4},
            {"name": "off2", "start": 21, "end": 24, "rate": 0.2},
        ],
        "export_bands": [{"name": "fit", "start": 0, "end": 24, "rate": 0.05}],
        "daily_supply": 0.0,
        "daily_membership": 0.0,
        "rates_include_gst": True,
    }
    freezer.move_to("2026-11-07T06:00:00+00:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Solar",
        data={"provider": "Solar", "import_entity": IMPORT, "export_entity": EXPORT},
        options={"plans": [plan], "tracked_devices": [EV], "solar_entity": SOLAR},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rt = entry.runtime_data
    await _set(
        hass, freezer, "2026-11-07T06:10:00+00:00", **{IMPORT: "101", EV: "11"}
    )  # 16:40
    await _set(
        hass, freezer, "2026-11-07T06:40:00+00:00", **{IMPORT: "102", EV: "12"}
    )  # 17:10
    freezer.move_to("2026-11-07T07:03:00+00:00")
    rt._on_tick(None)
    assert _cost(rt, EV) + _cost(rt, UNTRACKED_KEY) == pytest.approx(
        rt.ledger.lifetime_cost("import")
    )


async def test_the_import_a_long_outage_already_charged_does_not_inflate_the_next_hour(
    hass, freezer
):
    """The returning lump covers the outage: it must not raise the next hour's grid share."""
    entry = await _setup(hass)
    rt = entry.runtime_data
    await _set(
        hass, freezer, "2026-11-07T12:01:00+00:00", **{IMPORT: "unavailable", EV: "11"}
    )
    for h in range(13, 24):
        freezer.move_to(f"2026-11-07T{h:02d}:30:00+00:00")
        rt._on_tick(None)
    for h in range(0, 12):
        freezer.move_to(f"2026-11-08T{h:02d}:30:00+00:00")
        rt._on_tick(None)
    assert _cost(rt, EV) == pytest.approx(RATE)  # The outage hour, at the full rate.
    # Back at 12:10 with 2 kWh: 1 for the outage hour, 1 for this hour. This hour the
    # car also took 1 kWh while 1 kWh of solar was used: a grid share of 1/2.
    await _set(
        hass,
        freezer,
        "2026-11-08T12:10:00+00:00",
        **{IMPORT: "102", SOLAR: "51", EV: "12"},
    )
    freezer.move_to("2026-11-08T13:03:00+00:00")
    rt._on_tick(None)
    assert _cost(rt, EV) == pytest.approx(RATE + 0.5 * RATE)
    assert _cost(rt, UNTRACKED_KEY) == pytest.approx(0.5 * RATE)
    parts = _cost(rt, EV) + _cost(rt, UNTRACKED_KEY)
    assert parts == pytest.approx(rt.ledger.lifetime_cost("import"))
