"""Sleep schedule parsing and encoding for the Ooler BLE protocol.

The Ooler stores one active sleep schedule across four BLE characteristics
on service b430cd72:

  SCHEDULE_TIMES  140 bytes  70 × uint16 LE minute-of-week (Mon 00:00 = 0)
  SCHEDULE_TEMPS   70 bytes  1:1 with times: 0=OFF, 1-120=°F, 0xFE=warm wake, 0xFF=unused
  SCHEDULE_HEADER   2 bytes  uint16 LE sequence counter (incremented on write)
  SCHEDULE_META     4 bytes  read-only firmware state flag (safe to ignore)

Events are "set and hold" — each sets the device state until the next.
Nights group as sequences ending with an OFF event.  Warm wake is encoded
as three events: target temp, 0xFE marker at +1 min, OFF at +duration.

Max capacity: 70 events.  A simple 7-day schedule uses 14 (2/day).
Warm wake adds 3/day.  Multi-zone adds 1 per temperature step.

Day repeat is structural — the app includes/excludes days by adding or
removing their events.  Schedule names and enabled state are app-side only.

GATT write quirk: the device byte-swaps uint16 values on write to the
schedule service.  The client compensates by pre-swapping times and
header bytes.  This module works in the logical LE format throughout;
the swap is applied at the client.py write layer only.

All functions here are pure data transformations — no BLE I/O.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import time

_TIMES_LENGTH = 140  # 70 × uint16 LE
_TEMPS_LENGTH = 70
_MAX_EVENTS = 70
_MINUTES_PER_DAY = 1440
_MINUTES_PER_WEEK = _MINUTES_PER_DAY * 7

_TEMP_OFF = 0x00
_TEMP_WARM_WAKE_MARKER = 0xFE
_TEMP_UNUSED = 0xFF


@dataclass(frozen=True)
class SleepScheduleEvent:
    """A single event in the flat wire-format schedule.

    Each event sets the device state at a specific minute of the week.
    The state persists until the next event (set-and-hold semantics).
    """

    minute_of_week: int  # 0–10079
    temp_f: int  # 0=OFF, 1-120=temp, 254=warm_wake_marker

    @property
    def day(self) -> int:
        """Day of week (0=Monday, 6=Sunday)."""
        return self.minute_of_week // _MINUTES_PER_DAY

    @property
    def time(self) -> time:
        """Time of day as a datetime.time."""
        day_minute = self.minute_of_week % _MINUTES_PER_DAY
        return time(day_minute // 60, day_minute % 60)

    @property
    def is_off(self) -> bool:
        return self.temp_f == _TEMP_OFF

    @property
    def is_warm_wake_marker(self) -> bool:
        return self.temp_f == _TEMP_WARM_WAKE_MARKER


@dataclass(frozen=True)
class WarmWake:
    """Warm wake configuration for a single night."""

    target_temp_f: int  # typically 116 (HI)
    duration_min: int  # typically 30


@dataclass(frozen=True)
class SleepScheduleNight:
    """One night's temperature program.

    ``day`` is the evening start day (0=Mon, 6=Sun).  Temperature events
    and the off time may fall on the following calendar day (e.g. a Monday
    night bedtime at 22:00 with a Tuesday 06:00 wake time).

    ``temps`` lists temperature set-points in chronological order.  Each
    entry is a ``(time, temp_f)`` pair where *time* is a :class:`datetime.time`
    and *temp_f* is a Fahrenheit temperature (1–120).

    ``warm_wake`` is ``None`` when warm wake is disabled for this night.
    """

    day: int  # 0=Mon, 6=Sun — the evening the schedule starts
    temps: list[tuple[time, int]]  # [(time(22,0), 68), (time(2,0), 62)]
    off_time: time
    warm_wake: WarmWake | None = None


@dataclass
class OolerSleepSchedule:
    """High-level representation of the full weekly sleep schedule."""

    nights: list[SleepScheduleNight] = field(default_factory=list)
    seq: int = 0  # SCHEDULE_HEADER sequence counter


# ---------------------------------------------------------------------------
# Wire format → Python
# ---------------------------------------------------------------------------


def decode_sleep_schedule_events(
    times_bytes: bytes | bytearray, temps_bytes: bytes | bytearray
) -> list[SleepScheduleEvent]:
    """Decode raw BLE bytes into a list of schedule events.

    *times_bytes* is the 140-byte SCHEDULE_TIMES characteristic and
    *temps_bytes* is the 70-byte SCHEDULE_TEMPS characteristic.
    """
    if len(times_bytes) != _TIMES_LENGTH:
        raise ValueError(
            f"SCHEDULE_TIMES must be {_TIMES_LENGTH} bytes, got {len(times_bytes)}"
        )
    if len(temps_bytes) != _TEMPS_LENGTH:
        raise ValueError(
            f"SCHEDULE_TEMPS must be {_TEMPS_LENGTH} bytes, got {len(temps_bytes)}"
        )

    events: list[SleepScheduleEvent] = []
    for i in range(_MAX_EVENTS):
        minute = struct.unpack_from("<H", times_bytes, i * 2)[0]
        temp = temps_bytes[i]

        if temp == _TEMP_UNUSED:
            break
        # A zero minute with a non-OFF temp at position > 0 is valid
        # (Mon 00:00 event).  But a zero minute after we've already seen
        # trailing zeros is padding — check if the rest is all zeros.
        if minute == 0 and i > 0 and temp == _TEMP_OFF:
            remaining_times = times_bytes[i * 2 :]
            if all(b == 0 for b in remaining_times):
                break

        events.append(SleepScheduleEvent(minute_of_week=minute, temp_f=temp))

    return events


def events_to_sleep_schedule(
    events: list[SleepScheduleEvent], seq: int = 0
) -> OolerSleepSchedule:
    """Parse a flat event list into a structured :class:`OolerSleepSchedule`.

    Events are grouped into "nights" — sequences of temperature events ending
    with an OFF event.  Warm wake is detected by the characteristic three-event
    pattern: target temp, 0xFE marker at +1 minute, OFF at target + duration.

    The first group of events before the first bedtime (e.g. Mon 02:00 deep
    sleep + Mon 06:00 OFF) are the tail of Sunday night wrapping around.  They
    are joined with any trailing Sunday events at the end of the list to form
    one complete Sunday night.
    """
    if not events:
        return OolerSleepSchedule(seq=seq)

    # Split into groups delimited by OFF events
    groups: list[list[SleepScheduleEvent]] = []
    current: list[SleepScheduleEvent] = []

    for event in events:
        current.append(event)
        if event.is_off:
            groups.append(current)
            current = []

    trailing: list[SleepScheduleEvent] = current  # may be empty

    # Detect if the first group is the "morning tail" of a wrapped Sunday night.
    # The first group is a morning tail when:
    # - It contains no evening events (all events are before noon in day-time)
    # - There are trailing events (Sunday bedtime) that should join with it
    #
    # Examples of morning tails:
    # - Simple: [Mon 06:00=OFF] (just a wake-up OFF)
    # - Multi-zone: [Mon 02:00=62°F, Mon 06:00=OFF] (deep sleep + wake)
    # - Warm wake: [Mon 02:00=62°F, Mon 07:00=116°F, Mon 07:01=FE, Mon 07:30=OFF]
    head_group: list[SleepScheduleEvent] | None = None
    if groups:
        first_has_evening = any(
            e.minute_of_week % _MINUTES_PER_DAY >= 720
            for e in groups[0]
            if not e.is_off and not e.is_warm_wake_marker
        )
        if not first_has_evening and (trailing or len(groups) > 1):
            head_group = groups.pop(0)

    # Parse middle groups (complete nights)
    nights: list[SleepScheduleNight] = []
    for group in groups:
        night = _parse_night(group)
        if night is not None:
            nights.append(night)

    # Handle trailing + head_group (wrapped Sunday night)
    if trailing or head_group:
        combined = (trailing or []) + (head_group or [])
        night = _parse_night(combined)
        if night is not None:
            nights.append(night)

    return OolerSleepSchedule(nights=nights, seq=seq)


def _parse_night(events: list[SleepScheduleEvent]) -> SleepScheduleNight | None:
    """Parse a single night's events into a SleepScheduleNight."""
    if not events:
        return None

    # Detect warm wake: look for (target_temp, 0xFE at +1min, OFF) at the end
    warm_wake: WarmWake | None = None
    off_time: time | None = None
    temp_events = events

    if len(events) >= 3:
        e_target, e_marker, e_off = events[-3], events[-2], events[-1]
        if (
            e_marker.is_warm_wake_marker
            and e_off.is_off
            and not e_target.is_off
            and not e_target.is_warm_wake_marker
            and e_marker.minute_of_week == e_target.minute_of_week + 1
        ):
            duration = e_off.minute_of_week - e_target.minute_of_week
            warm_wake = WarmWake(
                target_temp_f=e_target.temp_f, duration_min=duration
            )
            off_time = e_target.time  # The "real" off time is the warm wake start
            temp_events = events[:-3]
        elif events[-1].is_off:
            off_time = events[-1].time
            temp_events = events[:-1]
    elif len(events) >= 1 and events[-1].is_off:
        off_time = events[-1].time
        temp_events = events[:-1]

    if off_time is None:
        # No OFF event — open-ended night (e.g. Sunday wrapping)
        # Use midnight as a sentinel
        off_time = time(0, 0)
        temp_events = events

    # Filter out OFF-only "nights" (orphan OFF at start of week)
    if not temp_events:
        return None

    # The night's "day" is the day of the first temperature event
    day = temp_events[0].day
    temps: list[tuple[time, int]] = [
        (e.time, e.temp_f) for e in temp_events if not e.is_off
    ]

    if not temps:
        return None

    return SleepScheduleNight(
        day=day, temps=temps, off_time=off_time, warm_wake=warm_wake
    )


# ---------------------------------------------------------------------------
# Python → Wire format
# ---------------------------------------------------------------------------


def encode_sleep_schedule_events(
    events: list[SleepScheduleEvent],
) -> tuple[bytes, bytes]:
    """Encode a list of schedule events into raw BLE bytes.

    Returns a ``(times_bytes, temps_bytes)`` tuple ready for writing to
    SCHEDULE_TIMES and SCHEDULE_TEMPS characteristics.
    """
    if len(events) > _MAX_EVENTS:
        raise ValueError(f"Too many events: {len(events)} (max {_MAX_EVENTS})")

    times = bytearray(_TIMES_LENGTH)
    temps = bytearray([_TEMP_UNUSED] * _TEMPS_LENGTH)

    # The device allows minute_of_week values slightly past 10079 for
    # Sunday-night events that wrap into Monday morning (e.g. 10080 = Mon 00:00).
    # uint16 max (65535) is the hard limit of the wire format.
    for i, event in enumerate(events):
        if not 0 <= event.minute_of_week <= 0xFFFF:
            raise ValueError(
                f"minute_of_week {event.minute_of_week} out of range (0–65535)"
            )
        struct.pack_into("<H", times, i * 2, event.minute_of_week)
        temps[i] = event.temp_f

    return bytes(times), bytes(temps)


def sleep_schedule_to_events(
    schedule: OolerSleepSchedule,
) -> list[SleepScheduleEvent]:
    """Convert a structured schedule into a flat sorted event list.

    This is the inverse of :func:`events_to_sleep_schedule`.
    """
    events: list[SleepScheduleEvent] = []

    for night in schedule.nights:
        day_offset = night.day * _MINUTES_PER_DAY
        # The bedtime (first temp event) determines the "pivot": any events
        # with a time-of-day earlier than bedtime are on the following calendar day.
        bedtime_minute = night.temps[0][0].hour * 60 + night.temps[0][0].minute

        for t, temp_f in night.temps:
            mow = _time_to_minute_of_week(t, day_offset, bedtime_minute)
            events.append(SleepScheduleEvent(minute_of_week=mow, temp_f=temp_f))

        if night.warm_wake is not None:
            wake_mow = _time_to_minute_of_week(
                night.off_time, day_offset, bedtime_minute
            )
            events.append(
                SleepScheduleEvent(
                    minute_of_week=wake_mow,
                    temp_f=night.warm_wake.target_temp_f,
                )
            )
            events.append(
                SleepScheduleEvent(
                    minute_of_week=wake_mow + 1, temp_f=_TEMP_WARM_WAKE_MARKER
                )
            )
            events.append(
                SleepScheduleEvent(
                    minute_of_week=wake_mow + night.warm_wake.duration_min,
                    temp_f=_TEMP_OFF,
                )
            )
        else:
            off_mow = _time_to_minute_of_week(
                night.off_time, day_offset, bedtime_minute
            )
            # off_time of 00:00 with no warm wake on an open-ended night is a
            # sentinel — don't emit an OFF event for it.
            if not (night.off_time == time(0, 0) and off_mow == day_offset):
                events.append(
                    SleepScheduleEvent(minute_of_week=off_mow, temp_f=_TEMP_OFF)
                )

    events.sort(key=lambda e: e.minute_of_week)
    return events


def _time_to_minute_of_week(
    t: time, day_offset: int, bedtime_minute: int
) -> int:
    """Convert a time-of-day to minute-of-week.

    Events with a time-of-day earlier than *bedtime_minute* are placed on the
    next calendar day (e.g. a 02:00 deep-sleep event on a Monday-night
    schedule means Tuesday 02:00 = minute 1560).
    """
    minute_of_day = t.hour * 60 + t.minute
    if minute_of_day < bedtime_minute:
        # Next calendar day
        mow = day_offset + _MINUTES_PER_DAY + minute_of_day
    else:
        mow = day_offset + minute_of_day
    # Wrap past end of week
    if mow >= _MINUTES_PER_WEEK:
        mow -= _MINUTES_PER_WEEK
    return mow


# ---------------------------------------------------------------------------
# Convenience builder
# ---------------------------------------------------------------------------


def build_sleep_schedule(
    *,
    bedtime: time,
    wake_time: time,
    temp_f: int,
    days: list[int] | None = None,
    warm_wake: WarmWake | None = None,
    extra_temps: list[tuple[time, int]] | None = None,
) -> OolerSleepSchedule:
    """Build an app-compatible uniform sleep schedule.

    This produces the same schedule pattern the Ooler app creates: the same
    temperature program repeated across selected days.

    Args:
        bedtime: When to turn on (e.g. ``time(22, 0)`` for 10pm).
        wake_time: When to turn off (e.g. ``time(6, 0)`` for 6am).
        temp_f: Target temperature in Fahrenheit (1–120).
        days: Days to include (0=Mon, 6=Sun). Defaults to all 7 days.
        warm_wake: Optional warm wake configuration.
        extra_temps: Additional temperature steps as ``(time, temp_f)`` pairs.
            These are inserted chronologically between bedtime and wake_time.
    """
    if days is None:
        days = list(range(7))

    nights: list[SleepScheduleNight] = []
    for day in sorted(days):
        temps: list[tuple[time, int]] = [(bedtime, temp_f)]
        if extra_temps:
            temps.extend(extra_temps)
        nights.append(
            SleepScheduleNight(
                day=day,
                temps=temps,
                off_time=wake_time,
                warm_wake=warm_wake,
            )
        )

    return OolerSleepSchedule(nights=nights)
