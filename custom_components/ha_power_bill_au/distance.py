"""Distance for a tracked charger: cost and energy per 100 km driven.

A tracked device (an EV charger's meter) can be linked to the car's odometer. Kilometres in
a period are the odometer's reading now minus its reading at the start of the period; the
start readings come from the recorder's daily statistics, so nothing is counted twice and
nothing has to be rebuilt. The charger's cost for the same days is kept as a copy of its
bookings from the day odometer data starts (see `UsageLedger.mirror`).

Optionally the car's battery level and location tell home charging from charging
elsewhere: a rise in battery level while the car is away from home is charging the
charger's meter never saw, which makes the cost per km look lower than it is.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from homeassistant.core import State
from homeassistant.util.unit_conversion import DistanceConverter

DISTANCE_PREFIX = "distance|"
# A battery level that rises by less than this is noise, not charging (percentage points).
MIN_CHARGE_STEP = 1.0
# A car covers at most this much per hour (plus a margin): faster odometer jumps are bad
# readings and are ignored.
MAX_KMH = 250.0
JUMP_MARGIN_KM = 50.0
# A jump larger than this is only taken once a later reading confirms it, however long
# the car was away: a single garbage reading must not move the odometer for good.
CONFIRM_JUMP_KM = 1500.0


def distance_key(ledger_key: str) -> str:
    """The usage-ledger source holding the charger's bookings from the distance start."""
    return f"{DISTANCE_PREFIX}{ledger_key}"


def odometer_km(state: State | None) -> float | None:
    """An odometer reading in km, or None while it is unavailable or not a distance."""
    if state is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit not in DistanceConverter.VALID_UNITS:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return DistanceConverter.convert(value, unit, "km")


def battery_level(state: State | None) -> float | None:
    """A battery level in percent, or None while it is unavailable or not a percentage."""
    if state is None or state.attributes.get("unit_of_measurement") != "%":
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def at_home(state: State | None) -> bool | None:
    """Whether a device tracker or person is home, or None when it is not known."""
    if state is None or state.state in ("unavailable", "unknown", ""):
        return None
    return state.state == "home"


def _reachable(km: float, base: float, since: Any, when: datetime) -> bool:
    """Whether a car could have driven from `base` to `km` since `since`."""
    hours = 24.0
    if isinstance(since, str):
        hours = max((when - datetime.fromisoformat(since)).total_seconds(), 0) / 3600
    return 0 <= km - base <= MAX_KMH * hours + JUMP_MARGIN_KM


def accept_km(point: dict[str, Any], km: float, when: datetime) -> bool:
    """Take an odometer reading into a window's point, unless it is a glitch.

    A reading below the last one, or further ahead than the car could have driven since,
    is ignored. A big but possible jump (a long trip while nothing reported) is held
    until a later reading confirms it. Returns whether the reading was taken.
    """
    stamp = when.isoformat()
    last = point.get("last_km")
    if last is None or (
        _reachable(km, last, point.get("last_at"), when)
        and km - last <= CONFIRM_JUMP_KM
    ):
        taken = True
    else:
        held, held_at = point.get("held_km"), point.get("held_at")
        taken = held is not None and km >= last and _reachable(km, held, held_at, when)
        if not taken:
            if km > last and _reachable(km, last, point.get("last_at"), when):
                point["held_km"], point["held_at"] = km, stamp
            return False
    point["last_km"], point["last_at"] = km, stamp
    point.pop("held_km", None)
    point.pop("held_at", None)
    return True


class DistanceTracker:
    """Odometer starting points and charging tallies for every linked charger."""

    def __init__(self) -> None:
        # ledger key -> {"start_day": ISO date, "start_km": km, "month": "YYYY-MM",
        #                "month_km": km, "last_km": km, "source": identity}
        self.points: dict[str, dict[str, Any]] = {}
        # ledger key -> ISO day -> [home, away] battery percentage points added
        self.charges: dict[str, dict[str, list[float]]] = {}
        # ledger key -> last battery level seen (to measure the next rise)
        self.last_level: dict[str, float] = {}

    def forget(self, key: str) -> None:
        for table in (self.points, self.charges, self.last_level):
            table.pop(key, None)

    def keep_only(self, keys: set[str]) -> None:
        for key in {*self.points, *self.charges, *self.last_level} - keys:
            self.forget(key)

    def note_charge(self, key: str, level: float, home: bool | None, day: date) -> bool:
        """Record a battery level. A rise is charging, at home or away. Returns whether
        anything was added.

        Small rises add up until they reach a whole point, so a car that reports in tenths
        of a percent is still counted.
        """
        previous = self.last_level.get(key)
        if previous is None or level < previous:
            self.last_level[key] = level
            return False
        if level - previous < MIN_CHARGE_STEP:
            return False
        self.last_level[key] = level
        if home is None:
            return False  # Not known where: not counted either way.
        cell = self.charges.setdefault(key, {}).setdefault(day.isoformat(), [0.0, 0.0])
        cell[0 if home else 1] += level - previous
        return True

    def charge_split(self, key: str, since: date) -> tuple[float, float]:
        """Battery percentage points added at home and away, from a day on."""
        home = away = 0.0
        for day, (at_home_pts, away_pts) in self.charges.get(key, {}).items():
            if day >= since.isoformat():
                home += at_home_pts
                away += away_pts
        return home, away

    def to_dict(self) -> dict[str, Any]:
        return {
            "points": {key: dict(point) for key, point in self.points.items()},
            "charges": {
                key: {day: list(cell) for day, cell in days.items()}
                for key, days in self.charges.items()
            },
            "last_level": dict(self.last_level),
        }

    @classmethod
    def from_dict(cls, data: Any) -> DistanceTracker:
        """Load saved distance data, skipping anything malformed."""
        tracker = cls()
        if not isinstance(data, dict):
            return tracker

        def number(value: Any) -> float | None:
            return (
                float(value)
                if isinstance(value, (int, float))
                and math.isfinite(value)
                and value >= 0
                else None
            )

        def day_text(value: Any) -> str | None:
            try:
                return date.fromisoformat(str(value)).isoformat()
            except ValueError:
                return None

        points = data.get("points")
        for key, point in (points if isinstance(points, dict) else {}).items():
            if not isinstance(point, dict):
                continue
            start_day, start_km = (
                day_text(point.get("start_day")),
                number(point.get("start_km")),
            )
            if start_day is None or start_km is None:
                continue
            clean: dict[str, Any] = {"start_day": start_day, "start_km": start_km}
            if (held := number(point.get("held_km"))) is not None:
                clean["held_km"] = held
            for field in ("start_at", "last_at", "held_at"):
                if isinstance(point.get(field), str):
                    try:
                        moment = datetime.fromisoformat(point[field])
                    except ValueError:
                        continue
                    if moment.tzinfo is not None:
                        clean[field] = moment.isoformat()
            for field in ("month_km", "last_km"):
                if (value := number(point.get(field))) is not None:
                    clean[field] = value
            if isinstance(point.get("month"), str):
                clean["month"] = point["month"]
            if isinstance(point.get("source"), str):
                clean["source"] = point["source"]
            tracker.points[str(key)] = clean
        charges = data.get("charges")
        for key, days in (charges if isinstance(charges, dict) else {}).items():
            for day, cell in (days if isinstance(days, dict) else {}).items():
                if (
                    (text := day_text(day)) is not None
                    and isinstance(cell, list)
                    and len(cell) == 2
                    and None not in (values := [number(v) for v in cell])
                ):
                    tracker.charges.setdefault(str(key), {})[text] = values  # type: ignore[assignment]
        levels = data.get("last_level")
        for key, level in (levels if isinstance(levels, dict) else {}).items():
            if (value := number(level)) is not None and value <= 100:
                tracker.last_level[str(key)] = value
        return tracker
