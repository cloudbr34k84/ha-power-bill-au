"""Which mode a tracked device ran in, so its cost can be split by mode.

A tracked device can be linked to an entity whose state is its mode (an air conditioner's
`hvac_mode`, a water heater's operation mode, a select). Each energy reading from the device
is tagged with the mode in force when it arrived. Readings are buffered per five-minute
slot, like the meter readings, and every finished hour is summarised into the share of the
device's energy used in each mode.

Unlike meter readings, modes can't be rebuilt from recorder statistics (the recorder keeps
states for days, not years), so the hourly shares are kept permanently. Shares, not costs,
are kept: a rate change still reprices the past correctly.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta
from typing import Any

HOUR = timedelta(hours=1)
UNKNOWN_MODE = "unknown"


def mode_key(ledger_key: str, mode: str) -> str:
    """The usage-ledger source for one device in one mode."""
    return f"mode|{ledger_key}|{mode}"


def mode_slug(mode: str) -> str:
    """A unique-ID part for a mode: never empty, and different for every mode name."""
    if re.fullmatch(r"[a-z0-9_]+", mode):
        return mode
    slug = re.sub(r"[^a-z0-9]+", "_", mode.lower()).strip("_")
    digest = hashlib.sha1(mode.encode()).hexdigest()[:8]  # noqa: S324 - not security
    return f"{slug}_{digest}" if slug else digest


def hour_of(at: datetime) -> str:
    """The UTC hour a moment falls in, as stored."""
    return at.replace(minute=0, second=0, microsecond=0).isoformat()


def _moment(text: Any) -> datetime | None:
    """A stored slot or hour, or None when it is not a time-zone-aware timestamp."""
    try:
        at = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _shares(kwh_by_mode: dict[str, float]) -> dict[str, float]:
    total = sum(kwh_by_mode.values())
    if total <= 0:
        return {}
    return {mode: kwh / total for mode, kwh in kwh_by_mode.items() if kwh > 0}


class ModeTracker:
    """Mode tags for every linked device: a short buffer and the permanent hourly shares."""

    def __init__(self) -> None:
        # device ledger key -> UTC slot -> mode -> kWh (the last day, like meter readings)
        self.pending: dict[str, dict[str, dict[str, float]]] = {}
        # device ledger key -> UTC hour -> mode -> share of that hour's energy
        self.hours: dict[str, dict[str, dict[str, float]]] = {}
        # device ledger key -> last mode seen, held while the mode entity is unavailable
        self.last: dict[str, str] = {}
        # device ledger key -> every mode name seen, so sensors outlive a missing entity
        self.names: dict[str, list[str]] = {}
        # device ledger key -> identities (registry ID, entity ID) of the mode entity the
        # record belongs to
        self.source: dict[str, list[str]] = {}
        self._saved_hours: dict[str, dict[str, Any]] = {}  # serialised, until changed
        self._first: dict[str, str] = {}

    def add(self, key: str, slot: str, mode: str, kwh: float) -> None:
        if kwh > 0 and math.isfinite(kwh):
            cell = self.pending.setdefault(key, {}).setdefault(slot, {})
            cell[mode] = cell.get(mode, 0.0) + kwh

    def seed(self, key: str, slot: str, mode: str, kwh: float) -> None:
        """Add a backfilled slot, never touching one that has live readings."""
        if slot not in self.pending.get(key, {}):
            self.add(key, slot, mode, kwh)

    @staticmethod
    def buffered_from(now: datetime, retention: timedelta) -> datetime:
        """Hours starting at or after this are still completely in the buffer."""
        return now - retention + HOUR

    def _set_hour(self, key: str, hour: str, shares: dict[str, float]) -> bool:
        table = self.hours.setdefault(key, {})
        if table.get(hour) == shares:
            return False
        table[hour] = shares
        self._saved_hours.pop(key, None)
        if key in self._first and hour < self._first[key]:
            self._first[key] = hour
        return True

    def settle(self, now: datetime, *, delay: timedelta, retention: timedelta) -> bool:
        """Summarise every finished hour the buffer holds completely.

        Recomputing an hour from the buffer gives the same result each time, so this can
        run as often as needed. Hours the buffer no longer covers keep their stored shares.
        Returns whether any hour changed.
        """
        complete_from = self.buffered_from(now, retention)
        changed = False
        for key, slots in self.pending.items():
            per_hour: dict[datetime, dict[str, float]] = {}
            for slot, kwh_by_mode in slots.items():
                at = datetime.fromisoformat(slot)
                hour = at.replace(minute=0, second=0, microsecond=0)
                if hour < complete_from or hour + HOUR > now - delay:
                    continue
                cell = per_hour.setdefault(hour, {})
                for mode, kwh in kwh_by_mode.items():
                    cell[mode] = cell.get(mode, 0.0) + kwh
            for hour, kwh_by_mode in per_hour.items():
                if shares := _shares(kwh_by_mode):
                    changed |= self._set_hour(key, hour.isoformat(), shares)
        return changed

    def settle_loaded(self, now: datetime) -> None:
        """Summarise finished hours of a freshly loaded buffer that were never stored.

        After HA was off for a day the buffer still holds the hour it stopped in, whole:
        an hour no tick ever summarised was never partly pruned either.
        """
        for key, slots in self.pending.items():
            per_hour: dict[datetime, dict[str, float]] = {}
            for slot, kwh_by_mode in slots.items():
                at = datetime.fromisoformat(slot)
                hour = at.replace(minute=0, second=0, microsecond=0)
                if hour + HOUR > now or hour.isoformat() in self.hours.get(key, {}):
                    continue
                cell = per_hour.setdefault(hour, {})
                for mode, kwh in kwh_by_mode.items():
                    cell[mode] = cell.get(mode, 0.0) + kwh
            for hour, kwh_by_mode in per_hour.items():
                if shares := _shares(kwh_by_mode):
                    self._set_hour(key, hour.isoformat(), shares)

    def same_source(self, key: str, identities: list[str]) -> bool:
        """Whether the record was made with this mode entity (or there is no record)."""
        known = self.source.get(key)
        return not known or bool(set(known) & set(identities))

    def prune(self, cutoff: datetime) -> None:
        for slots in self.pending.values():
            for slot in [s for s in slots if datetime.fromisoformat(s) < cutoff]:
                del slots[slot]

    def stored_shares(self, key: str, hour: str) -> dict[str, float]:
        """An hour's stored split. History is priced with this alone: it runs once per
        hour of history, so it must not search the buffer."""
        return self.hours.get(key, {}).get(hour, {})

    def hour_shares(self, key: str, hour: str) -> dict[str, float]:
        """An hour's split: as stored, else from whatever the buffer holds of it."""
        if found := self.stored_shares(key, hour):
            return found
        totals: dict[str, float] = {}
        for slot, kwh_by_mode in self.pending.get(key, {}).items():
            if hour_of(datetime.fromisoformat(slot)) == hour:
                for mode, kwh in kwh_by_mode.items():
                    totals[mode] = totals.get(mode, 0.0) + kwh
        return _shares(totals)

    def slot_shares(self, key: str, slot: str) -> dict[str, float]:
        """A buffered slot's split, falling back to its hour's split."""
        if found := _shares(self.pending.get(key, {}).get(slot, {})):
            return found
        return self.hour_shares(key, hour_of(datetime.fromisoformat(slot)))

    def fill(self, key: str, hour: str, kwh_by_mode: dict[str, float]) -> None:
        """Add a backfilled hour, never replacing one that was recorded live."""
        if hour not in self.hours.get(key, {}) and (shares := _shares(kwh_by_mode)):
            self._set_hour(key, hour, shares)

    def clear(self, key: str) -> None:
        """Forget everything recorded for one device."""
        for table in (self.pending, self.hours, self.last, self.names, self.source):
            table.pop(key, None)
        self._saved_hours.pop(key, None)
        self._first.pop(key, None)

    def keep_only(self, keys: set[str]) -> None:
        """Forget devices that are no longer linked to a mode."""
        known = {*self.pending, *self.hours, *self.last, *self.names, *self.source}
        for key in known - keys:
            self.clear(key)

    def note_names(self, key: str, names: list[str]) -> bool:
        """Remember mode names. Returns whether any is new."""
        known = self.names.setdefault(key, [])
        new = [name for name in dict.fromkeys(names) if name not in known]
        known.extend(new)
        return bool(new)

    def modes(self, key: str) -> set[str]:
        """Every mode known for this device: offered by its entity, or recorded."""
        found = set(self.names.get(key, []))
        found.update(
            mode for cell in self.pending.get(key, {}).values() for mode in cell
        )
        found.update(
            mode for shares in self.hours.get(key, {}).values() for mode in shares
        )
        return found

    def since(self, key: str) -> str | None:
        """The first hour with a recorded mode."""
        if key not in self._first and (hours := self.hours.get(key)):
            self._first[key] = min(hours)
        return self._first.get(key)

    # ---- storage --------------------------------------------------------------------------

    def _hours_to_dict(self, key: str) -> dict[str, Any]:
        """Hours run in one mode are stored as runs of consecutive hours."""
        runs: list[list[Any]] = []
        mixed: dict[str, dict[str, float]] = {}
        previous: datetime | None = None
        table = self.hours[key]
        for hour in sorted(table):
            shares = table[hour]
            at = datetime.fromisoformat(hour)
            if len(shares) == 1:
                (mode,) = shares
                if (
                    runs
                    and previous is not None
                    and at - previous == HOUR
                    and runs[-1][2] == mode
                ):
                    runs[-1][1] += 1
                else:
                    runs.append([hour, 1, mode])
                previous = at
            else:
                mixed[hour] = {m: round(s, 6) for m, s in shares.items()}
                previous = None
        return {"runs": runs, "mixed": mixed}

    def to_dict(self) -> dict[str, Any]:
        for key in self.hours:
            if key not in self._saved_hours:
                self._saved_hours[key] = self._hours_to_dict(key)
        return {
            "hours": {key: self._saved_hours[key] for key in self.hours},
            "pending": {
                key: {slot: dict(cell) for slot, cell in slots.items()}
                for key, slots in self.pending.items()
            },
            "last": dict(self.last),
            "names": {key: list(names) for key, names in self.names.items()},
            "source": {key: list(ids) for key, ids in self.source.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> ModeTracker:
        """Load saved modes, skipping anything malformed rather than failing setup."""
        tracker = cls()
        if not isinstance(data, dict):
            return tracker

        def table(name: str) -> dict[Any, Any]:
            found = data.get(name)
            return found if isinstance(found, dict) else {}

        for key, stored in table("hours").items():
            if not isinstance(stored, dict):
                continue
            hours: dict[str, dict[str, float]] = {}
            runs = stored.get("runs")
            for run in runs if isinstance(runs, list) else []:
                try:
                    start, count, mode = _moment(run[0]), int(run[1]), str(run[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if start is not None:
                    for step in range(max(count, 0)):
                        hours[(start + step * HOUR).isoformat()] = {mode: 1.0}
            mixed = stored.get("mixed")
            for hour, shares in (mixed if isinstance(mixed, dict) else {}).items():
                if (
                    isinstance(shares, dict)
                    and (at := _moment(hour)) is not None
                    and (
                        clean := _shares(
                            {
                                str(m): float(s)
                                for m, s in shares.items()
                                if isinstance(s, (int, float)) and math.isfinite(s)
                            }
                        )
                    )
                ):
                    hours[at.isoformat()] = clean
            tracker.hours[str(key)] = hours
        for key, slots in table("pending").items():
            for slot, cell in (slots if isinstance(slots, dict) else {}).items():
                if isinstance(cell, dict) and (at := _moment(slot)) is not None:
                    for mode, kwh in cell.items():
                        if isinstance(kwh, (int, float)):
                            tracker.add(str(key), at.isoformat(), str(mode), float(kwh))
        tracker.last = {str(k): str(v) for k, v in table("last").items()}
        tracker.source = {
            str(key): [str(i) for i in (ids if isinstance(ids, list) else [ids])]
            for key, ids in table("source").items()
        }
        for key, names in table("names").items():
            if isinstance(names, list):
                tracker.note_names(str(key), [str(name) for name in names])
        return tracker


def pretty_mode(mode: str) -> str:
    """A mode as shown in sensor names."""
    names = {
        "heat_cool": "Heat/Cool",
        "fan_only": "Fan only",
        UNKNOWN_MODE: "Unknown mode",
    }
    return names.get(mode, mode.replace("_", " ").capitalize())


def pretty_modes(modes: list[str]) -> dict[str, str]:
    """Display names for a device's modes, kept distinct from each other."""
    names = {mode: pretty_mode(mode) for mode in modes}
    for fallback in (lambda m: m.replace("_", " ").capitalize(), lambda m: m):
        counts: dict[str, int] = {}
        for name in names.values():
            counts[name.lower()] = counts.get(name.lower(), 0) + 1
        names = {
            mode: fallback(mode) if counts[name.lower()] > 1 else name
            for mode, name in names.items()
        }
    return names
