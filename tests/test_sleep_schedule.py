"""Tests for sleep schedule parsing, encoding, and round-trip consistency."""
from __future__ import annotations

from datetime import time

import pytest

from ooler_ble_client.sleep_schedule import (
    OolerSleepSchedule,
    SleepScheduleEvent,
    SleepScheduleNight,
    WarmWake,
    build_sleep_schedule,
    decode_sleep_schedule_events,
    encode_sleep_schedule_events,
    events_to_sleep_schedule,
    sleep_schedule_to_events,
    _TEMPS_LENGTH,
    _TIMES_LENGTH,
    _MAX_EVENTS,
)


# ---------------------------------------------------------------------------
# Real wire data captured from Ooler device snapshots (firmware 15.20)
# ---------------------------------------------------------------------------

# Empty schedule: times all zeros, temps all 0xFF
_EMPTY_TIMES = bytes(_TIMES_LENGTH)
_EMPTY_TEMPS = bytes([0xFF] * _TEMPS_LENGTH)

# Simple 2-event/day: 10pm–6am at 68°F, all 7 days (snapshot 104219 on device 603)
_SIMPLE_TIMES = bytes.fromhex(
    "68 01 28 05 08 07 c8 0a a8 0c 68 10 48 12 08 16"
    " e8 17 a8 1b 88 1d 48 21 28 23 e8 26"
    + " 00" * (140 - 28)
)
_SIMPLE_TEMPS = bytes.fromhex(
    "00 44 00 44 00 44 00 44 00 44 00 44 00 44"
    + " ff" * (70 - 14)
)

# 3-event/day: bedtime 65°F at 23:00, deep sleep 62°F at 02:00, off at 07:00
# (snapshot 104801 on device 603)
_MULTIZONE_TIMES = bytes.fromhex(
    "78 00 68 01 64 05 18 06 08 07 04 0b b8 0b a8 0c"
    " a4 10 58 11 48 12 44 16 f8 16 e8 17 e4 1b 98 1c"
    " 88 1d 84 21 38 22 28 23 24 27"
    + " 00" * (140 - 42)
)
_MULTIZONE_TEMPS = bytes.fromhex(
    "3e 00 41 3e 00 41 3e 00 41 3e 00 41 3e 00 41 3e"
    " 00 41 3e 00 41"
    + " ff" * (70 - 21)
)

# Warm wake: 3 extra events/day — target 116°F, marker 0xFE, off 30min later
# (snapshot 104926 on device 603)
_WARMWAKE_TIMES = bytes.fromhex(
    "78 00 a4 01 a5 01 c2 01 64 05 18 06 44 07 45 07"
    " 62 07 04 0b b8 0b e4 0c e5 0c 02 0d a4 10 58 11"
    " 84 12 85 12 a2 12 44 16 f8 16 24 18 25 18 42 18"
    " e4 1b 98 1c c4 1d c5 1d e2 1d 84 21 38 22 64 23"
    " 65 23 82 23 24 27"
    + " 00" * (140 - 70)
)
_WARMWAKE_TEMPS = bytes.fromhex(
    "3e 74 fe 00 41 3e 74 fe 00 41 3e 74 fe 00 41 3e"
    " 74 fe 00 41 3e 74 fe 00 41 3e 74 fe 00 41 3e 74"
    " fe 00 41"
    + " ff" * (70 - 35)
)

# Partial week (5 days, Tue-Sat nights): 3 events/day (snapshot 105045 on device 603)
_PARTIAL_TIMES = bytes.fromhex(
    "04 0b b8 0b e4 0c a4 10 58 11 84 12 44 16 f8 16"
    " 24 18 e4 1b 98 1c c4 1d 84 21 38 22 64 23"
    + " 00" * (140 - 30)
)
_PARTIAL_TEMPS = bytes.fromhex(
    "41 3e 00 41 3e 00 41 3e 00 41 3e 00 41 3e 00"
    + " ff" * (70 - 15)
)

# Max capacity: 70 events (snapshot from device 601 with many custom events)
_MAX_TIMES = bytes.fromhex(
    "1e 00 3c 00 68 01 69 01 86 01 28 05 46 05 64 05"
    " 82 05 a0 05 be 05 dc 05 08 07 09 07 26 07 c8 0a"
    " e6 0a 04 0b 22 0b 40 0b 5e 0b 7c 0b a8 0c a9 0c"
    " c6 0c 68 10 86 10 a4 10 c2 10 e0 10 fe 10 1c 11"
    " 48 12 49 12 66 12 08 16 26 16 44 16 62 16 80 16"
    " 9e 16 bc 16 e8 17 e9 17 06 18 a8 1b c6 1b e4 1b"
    " 02 1c 20 1c 3e 1c 5c 1c 88 1d 89 1d a6 1d 48 21"
    " 66 21 84 21 a2 21 c0 21 de 21 fc 21 28 23 29 23"
    " 46 23 e8 26 06 27 24 27 42 27 60 27"
)
_MAX_TEMPS = bytes.fromhex(
    "4e 50 74 fe 00 44 46 48 4a 4c 4e 50 74 fe 00 44"
    " 46 48 4a 4c 4e 50 74 fe 00 44 46 48 4a 4c 4e 50"
    " 74 fe 00 44 46 48 4a 4c 4e 50 74 fe 00 44 46 48"
    " 4a 4c 4e 50 74 fe 00 44 46 48 4a 4c 4e 50 74 fe"
    " 00 44 46 48 4a 4c"
)

# Simple warm wake on device 601: 10pm–6am at 68°F, warm wake 116°F/30min
_SIMPLE_WW_TIMES = bytes.fromhex(
    "68 01 69 01 86 01 28 05 08 07 09 07 26 07 c8 0a"
    " a8 0c a9 0c c6 0c 68 10 48 12 49 12 66 12 08 16"
    " e8 17 e9 17 06 18 a8 1b 88 1d 89 1d a6 1d 48 21"
    " 28 23 29 23 46 23 e8 26"
    + " 00" * (140 - 56)
)
_SIMPLE_WW_TEMPS = bytes.fromhex(
    "74 fe 00 44 74 fe 00 44 74 fe 00 44 74 fe 00 44"
    " 74 fe 00 44 74 fe 00 44 74 fe 00 44"
    + " ff" * (70 - 28)
)


# ---------------------------------------------------------------------------
# SleepScheduleEvent
# ---------------------------------------------------------------------------


class TestSleepScheduleEvent:
    def test_day_property(self) -> None:
        e = SleepScheduleEvent(minute_of_week=1320, temp_f=68)  # Mon 22:00
        assert e.day == 0

    def test_day_sunday(self) -> None:
        e = SleepScheduleEvent(minute_of_week=9960, temp_f=68)  # Sun 22:00
        assert e.day == 6

    def test_time_property(self) -> None:
        e = SleepScheduleEvent(minute_of_week=1320, temp_f=68)
        assert e.time == time(22, 0)

    def test_time_with_minutes(self) -> None:
        e = SleepScheduleEvent(minute_of_week=1350, temp_f=68)  # Mon 22:30
        assert e.time == time(22, 30)

    def test_is_off(self) -> None:
        assert SleepScheduleEvent(minute_of_week=360, temp_f=0).is_off
        assert not SleepScheduleEvent(minute_of_week=360, temp_f=68).is_off

    def test_is_warm_wake_marker(self) -> None:
        assert SleepScheduleEvent(minute_of_week=361, temp_f=0xFE).is_warm_wake_marker
        assert not SleepScheduleEvent(minute_of_week=361, temp_f=68).is_warm_wake_marker

    def test_frozen(self) -> None:
        e = SleepScheduleEvent(minute_of_week=0, temp_f=0)
        with pytest.raises(AttributeError):
            e.minute_of_week = 1  # type: ignore[misc]


class TestWarmWake:
    def test_basic(self) -> None:
        ww = WarmWake(target_temp_f=116, duration_min=30)
        assert ww.target_temp_f == 116
        assert ww.duration_min == 30

    def test_frozen(self) -> None:
        ww = WarmWake(target_temp_f=116, duration_min=30)
        with pytest.raises(AttributeError):
            ww.target_temp_f = 120  # type: ignore[misc]


class TestSleepScheduleNight:
    def test_basic(self) -> None:
        night = SleepScheduleNight(
            day=0,
            temps=[(time(22, 0), 68)],
            off_time=time(6, 0),
        )
        assert night.day == 0
        assert night.off_time == time(6, 0)
        assert night.warm_wake is None

    def test_with_warm_wake(self) -> None:
        night = SleepScheduleNight(
            day=0,
            temps=[(time(22, 0), 68)],
            off_time=time(6, 0),
            warm_wake=WarmWake(target_temp_f=116, duration_min=30),
        )
        assert night.warm_wake is not None
        assert night.warm_wake.target_temp_f == 116


class TestOolerSleepSchedule:
    def test_defaults(self) -> None:
        s = OolerSleepSchedule()
        assert s.nights == []
        assert s.seq == 0

    def test_with_data(self) -> None:
        night = SleepScheduleNight(
            day=0, temps=[(time(22, 0), 68)], off_time=time(6, 0)
        )
        s = OolerSleepSchedule(nights=[night], seq=42)
        assert len(s.nights) == 1
        assert s.seq == 42


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


class TestDecodeEvents:
    def test_empty_schedule(self) -> None:
        events = decode_sleep_schedule_events(_EMPTY_TIMES, _EMPTY_TEMPS)
        assert events == []

    def test_simple_schedule(self) -> None:
        events = decode_sleep_schedule_events(_SIMPLE_TIMES, _SIMPLE_TEMPS)
        assert len(events) == 14
        # First event: Mon 06:00 OFF
        assert events[0] == SleepScheduleEvent(minute_of_week=360, temp_f=0)
        # Second event: Mon 22:00 68°F
        assert events[1] == SleepScheduleEvent(minute_of_week=1320, temp_f=68)
        # All even-indexed events are OFF, all odd are 68°F
        for i, e in enumerate(events):
            if i % 2 == 0:
                assert e.is_off, f"event {i} should be OFF"
            else:
                assert e.temp_f == 68, f"event {i} should be 68°F"

    def test_multizone_schedule(self) -> None:
        events = decode_sleep_schedule_events(_MULTIZONE_TIMES, _MULTIZONE_TEMPS)
        assert len(events) == 21
        # Pattern per night: 62°F (deep sleep), OFF, 65°F (bedtime)
        # First 3 events: Mon 02:00=62, Mon 06:00=OFF (actually 07:00), Mon 23:00=65
        assert events[0].temp_f == 62  # deep sleep
        assert events[1].is_off  # wake/off
        assert events[2].temp_f == 65  # bedtime

    def test_warm_wake_schedule(self) -> None:
        events = decode_sleep_schedule_events(_WARMWAKE_TIMES, _WARMWAKE_TEMPS)
        assert len(events) == 35
        # Warm wake pattern: ..., 116°F, 0xFE, OFF
        # Check first warm wake triplet (events 1, 2, 3)
        assert events[1].temp_f == 116
        assert events[2].is_warm_wake_marker
        assert events[3].is_off

    def test_partial_week(self) -> None:
        events = decode_sleep_schedule_events(_PARTIAL_TIMES, _PARTIAL_TEMPS)
        assert len(events) == 15
        # Should only have events on Tue-Sun (5 nights)
        days = {e.day for e in events}
        assert 0 not in days  # No Monday events

    def test_max_capacity(self) -> None:
        events = decode_sleep_schedule_events(_MAX_TIMES, _MAX_TEMPS)
        assert len(events) == 70

    def test_invalid_times_length(self) -> None:
        with pytest.raises(ValueError, match="SCHEDULE_TIMES must be 140 bytes"):
            decode_sleep_schedule_events(b"\x00" * 10, _EMPTY_TEMPS)

    def test_invalid_temps_length(self) -> None:
        with pytest.raises(ValueError, match="SCHEDULE_TEMPS must be 70 bytes"):
            decode_sleep_schedule_events(_EMPTY_TIMES, b"\xff" * 10)


# ---------------------------------------------------------------------------
# Events → Structured schedule
# ---------------------------------------------------------------------------


class TestEventsToSchedule:
    def test_empty(self) -> None:
        schedule = events_to_sleep_schedule([], seq=42)
        assert schedule.nights == []
        assert schedule.seq == 42

    def test_simple_schedule(self) -> None:
        events = decode_sleep_schedule_events(_SIMPLE_TIMES, _SIMPLE_TEMPS)
        schedule = events_to_sleep_schedule(events, seq=100)
        assert len(schedule.nights) == 7
        assert schedule.seq == 100
        for night in schedule.nights:
            assert len(night.temps) == 1
            assert night.temps[0][1] == 68
            assert night.warm_wake is None
        # First 6 nights have explicit OFF at 06:00; Sunday wraps to Monday
        for night in schedule.nights[:6]:
            assert night.off_time == time(6, 0)
        # Sunday night is open-ended (wraps to next week's Monday OFF)
        assert schedule.nights[6].off_time == time(6, 0)

    def test_simple_schedule_days(self) -> None:
        events = decode_sleep_schedule_events(_SIMPLE_TIMES, _SIMPLE_TEMPS)
        schedule = events_to_sleep_schedule(events)
        days = [n.day for n in schedule.nights]
        assert days == [0, 1, 2, 3, 4, 5, 6]

    def test_multizone_schedule(self) -> None:
        events = decode_sleep_schedule_events(_MULTIZONE_TIMES, _MULTIZONE_TEMPS)
        schedule = events_to_sleep_schedule(events)
        assert len(schedule.nights) == 7
        for night in schedule.nights:
            assert len(night.temps) == 2  # bedtime + deep sleep
            assert night.warm_wake is None

    def test_warm_wake_detection(self) -> None:
        events = decode_sleep_schedule_events(_WARMWAKE_TIMES, _WARMWAKE_TEMPS)
        schedule = events_to_sleep_schedule(events)
        assert len(schedule.nights) == 7
        for night in schedule.nights:
            assert night.warm_wake is not None
            assert night.warm_wake.target_temp_f == 116
            assert night.warm_wake.duration_min == 30

    def test_warm_wake_off_time(self) -> None:
        """Warm wake off_time should be the wake start, not the actual off."""
        events = decode_sleep_schedule_events(_WARMWAKE_TIMES, _WARMWAKE_TEMPS)
        schedule = events_to_sleep_schedule(events)
        for night in schedule.nights:
            assert night.off_time == time(7, 0)

    def test_partial_week(self) -> None:
        events = decode_sleep_schedule_events(_PARTIAL_TIMES, _PARTIAL_TEMPS)
        schedule = events_to_sleep_schedule(events)
        assert len(schedule.nights) == 5
        days = [n.day for n in schedule.nights]
        assert 0 not in days  # No Monday night

    def test_simple_warm_wake_on_device_601(self) -> None:
        """Simple warm wake: 10pm-6am at 68°F with warm wake 116°F/30min."""
        events = decode_sleep_schedule_events(_SIMPLE_WW_TIMES, _SIMPLE_WW_TEMPS)
        schedule = events_to_sleep_schedule(events)
        assert len(schedule.nights) == 7
        for night in schedule.nights:
            assert len(night.temps) == 1
            assert night.temps[0][1] == 68
            assert night.warm_wake is not None
            assert night.warm_wake.target_temp_f == 116
            assert night.warm_wake.duration_min == 30
            assert night.off_time == time(6, 0)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


class TestEncodeEvents:
    def test_empty(self) -> None:
        times, temps = encode_sleep_schedule_events([])
        assert times == _EMPTY_TIMES
        assert temps == _EMPTY_TEMPS

    def test_too_many_events(self) -> None:
        events = [SleepScheduleEvent(minute_of_week=i, temp_f=68) for i in range(71)]
        with pytest.raises(ValueError, match="Too many events"):
            encode_sleep_schedule_events(events)

    def test_invalid_minute(self) -> None:
        events = [SleepScheduleEvent(minute_of_week=70000, temp_f=68)]
        with pytest.raises(ValueError, match="minute_of_week.*out of range"):
            encode_sleep_schedule_events(events)

    def test_negative_minute(self) -> None:
        events = [SleepScheduleEvent(minute_of_week=-1, temp_f=68)]
        with pytest.raises(ValueError, match="minute_of_week.*out of range"):
            encode_sleep_schedule_events(events)

    def test_sunday_overflow_allowed(self) -> None:
        """Events past 10079 (Sun 23:59) are valid for week-wrapping."""
        events = [SleepScheduleEvent(minute_of_week=10080, temp_f=68)]
        times, temps = encode_sleep_schedule_events(events)
        assert len(times) == _TIMES_LENGTH

    def test_single_event(self) -> None:
        events = [SleepScheduleEvent(minute_of_week=1320, temp_f=68)]
        times, temps = encode_sleep_schedule_events(events)
        assert len(times) == _TIMES_LENGTH
        assert len(temps) == _TEMPS_LENGTH
        # First 2 bytes should be 1320 (0x0528) in LE
        assert times[0:2] == b"\x28\x05"
        assert temps[0] == 68
        # Rest should be padding
        assert all(b == 0 for b in times[2:])
        assert all(b == 0xFF for b in temps[1:])


# ---------------------------------------------------------------------------
# Round-trip: decode → encode
# ---------------------------------------------------------------------------


class TestRoundTripDecodeEncode:
    """Verify that decoding then re-encoding produces identical wire bytes."""

    @pytest.mark.parametrize(
        "label, times_hex, temps_hex",
        [
            ("empty", _EMPTY_TIMES, _EMPTY_TEMPS),
            ("simple", _SIMPLE_TIMES, _SIMPLE_TEMPS),
            ("multizone", _MULTIZONE_TIMES, _MULTIZONE_TEMPS),
            ("warm_wake", _WARMWAKE_TIMES, _WARMWAKE_TEMPS),
            ("partial", _PARTIAL_TIMES, _PARTIAL_TEMPS),
            ("max_capacity", _MAX_TIMES, _MAX_TEMPS),
            ("simple_ww", _SIMPLE_WW_TIMES, _SIMPLE_WW_TEMPS),
        ],
    )
    def test_decode_encode_roundtrip(
        self, label: str, times_hex: bytes, temps_hex: bytes
    ) -> None:
        events = decode_sleep_schedule_events(times_hex, temps_hex)
        re_times, re_temps = encode_sleep_schedule_events(events)
        assert re_times == times_hex, f"{label}: times mismatch"
        assert re_temps == temps_hex, f"{label}: temps mismatch"


# ---------------------------------------------------------------------------
# Round-trip: structured → events → structured
# ---------------------------------------------------------------------------


class TestRoundTripStructured:
    """Verify that structured schedule survives events round-trip."""

    @pytest.mark.parametrize(
        "label, times_bytes, temps_bytes",
        [
            ("simple", _SIMPLE_TIMES, _SIMPLE_TEMPS),
            ("multizone", _MULTIZONE_TIMES, _MULTIZONE_TEMPS),
            ("warm_wake", _WARMWAKE_TIMES, _WARMWAKE_TEMPS),
            ("partial", _PARTIAL_TIMES, _PARTIAL_TEMPS),
            ("simple_ww", _SIMPLE_WW_TIMES, _SIMPLE_WW_TEMPS),
        ],
    )
    def test_events_to_schedule_to_events(
        self, label: str, times_bytes: bytes, temps_bytes: bytes
    ) -> None:
        """events → schedule → events should produce the same event list."""
        original_events = decode_sleep_schedule_events(times_bytes, temps_bytes)
        schedule = events_to_sleep_schedule(original_events)
        reconstructed_events = sleep_schedule_to_events(schedule)
        assert reconstructed_events == original_events, f"{label}: events mismatch"

    @pytest.mark.parametrize(
        "label, times_bytes, temps_bytes",
        [
            ("simple", _SIMPLE_TIMES, _SIMPLE_TEMPS),
            ("multizone", _MULTIZONE_TIMES, _MULTIZONE_TEMPS),
            ("warm_wake", _WARMWAKE_TIMES, _WARMWAKE_TEMPS),
            ("partial", _PARTIAL_TIMES, _PARTIAL_TEMPS),
            ("simple_ww", _SIMPLE_WW_TIMES, _SIMPLE_WW_TEMPS),
        ],
    )
    def test_full_roundtrip_bytes(
        self, label: str, times_bytes: bytes, temps_bytes: bytes
    ) -> None:
        """bytes → events → schedule → events → bytes should reproduce wire data."""
        events = decode_sleep_schedule_events(times_bytes, temps_bytes)
        schedule = events_to_sleep_schedule(events)
        rt_events = sleep_schedule_to_events(schedule)
        rt_times, rt_temps = encode_sleep_schedule_events(rt_events)
        assert rt_times == times_bytes, f"{label}: times bytes mismatch"
        assert rt_temps == temps_bytes, f"{label}: temps bytes mismatch"


# ---------------------------------------------------------------------------
# Schedule → Events encoding
# ---------------------------------------------------------------------------


class TestScheduleToEvents:
    def test_simple_night(self) -> None:
        night = SleepScheduleNight(
            day=0,
            temps=[(time(22, 0), 68)],
            off_time=time(6, 0),
        )
        schedule = OolerSleepSchedule(nights=[night])
        events = sleep_schedule_to_events(schedule)
        assert len(events) == 2
        assert events[0] == SleepScheduleEvent(minute_of_week=1320, temp_f=68)
        assert events[1] == SleepScheduleEvent(minute_of_week=1800, temp_f=0)

    def test_warm_wake_night(self) -> None:
        night = SleepScheduleNight(
            day=0,
            temps=[(time(22, 0), 68)],
            off_time=time(6, 0),
            warm_wake=WarmWake(target_temp_f=116, duration_min=30),
        )
        schedule = OolerSleepSchedule(nights=[night])
        events = sleep_schedule_to_events(schedule)
        assert len(events) == 4
        assert events[0] == SleepScheduleEvent(minute_of_week=1320, temp_f=68)
        # Warm wake at 06:00 (Tue) = minute 1800
        assert events[1] == SleepScheduleEvent(minute_of_week=1800, temp_f=116)
        assert events[2] == SleepScheduleEvent(minute_of_week=1801, temp_f=0xFE)
        assert events[3] == SleepScheduleEvent(minute_of_week=1830, temp_f=0)

    def test_multizone_night(self) -> None:
        night = SleepScheduleNight(
            day=0,
            temps=[(time(22, 0), 68), (time(2, 0), 62)],
            off_time=time(6, 0),
        )
        schedule = OolerSleepSchedule(nights=[night])
        events = sleep_schedule_to_events(schedule)
        assert len(events) == 3
        # Sorted chronologically
        assert events[0].temp_f == 68
        assert events[1].temp_f == 62
        assert events[2].is_off

    def test_events_sorted(self) -> None:
        """Events from multiple nights should be sorted by minute_of_week."""
        nights = [
            SleepScheduleNight(
                day=d,
                temps=[(time(22, 0), 68)],
                off_time=time(6, 0),
            )
            for d in [3, 0, 6]  # Thu, Mon, Sun — out of order
        ]
        schedule = OolerSleepSchedule(nights=nights)
        events = sleep_schedule_to_events(schedule)
        minutes = [e.minute_of_week for e in events]
        assert minutes == sorted(minutes)


# ---------------------------------------------------------------------------
# Convenience builder
# ---------------------------------------------------------------------------


class TestBuildSleepSchedule:
    def test_simple_all_days(self) -> None:
        schedule = build_sleep_schedule(
            bedtime=time(22, 0),
            wake_time=time(6, 0),
            temp_f=68,
        )
        assert len(schedule.nights) == 7
        for night in schedule.nights:
            assert night.temps == [(time(22, 0), 68)]
            assert night.off_time == time(6, 0)
            assert night.warm_wake is None

    def test_with_warm_wake(self) -> None:
        schedule = build_sleep_schedule(
            bedtime=time(22, 0),
            wake_time=time(6, 0),
            temp_f=68,
            warm_wake=WarmWake(target_temp_f=116, duration_min=30),
        )
        for night in schedule.nights:
            assert night.warm_wake is not None
            assert night.warm_wake.target_temp_f == 116

    def test_weekdays_only(self) -> None:
        schedule = build_sleep_schedule(
            bedtime=time(22, 0),
            wake_time=time(6, 0),
            temp_f=68,
            days=[0, 1, 2, 3, 4],
        )
        assert len(schedule.nights) == 5
        assert [n.day for n in schedule.nights] == [0, 1, 2, 3, 4]

    def test_with_extra_temps(self) -> None:
        schedule = build_sleep_schedule(
            bedtime=time(22, 0),
            wake_time=time(6, 0),
            temp_f=68,
            extra_temps=[(time(2, 0), 62)],
        )
        for night in schedule.nights:
            assert len(night.temps) == 2
            assert night.temps[0] == (time(22, 0), 68)
            assert night.temps[1] == (time(2, 0), 62)

    def test_days_sorted(self) -> None:
        schedule = build_sleep_schedule(
            bedtime=time(22, 0),
            wake_time=time(6, 0),
            temp_f=68,
            days=[5, 2, 0],
        )
        assert [n.day for n in schedule.nights] == [0, 2, 5]

    def test_builder_encodes_to_valid_wire_format(self) -> None:
        """Builder output should encode without errors."""
        schedule = build_sleep_schedule(
            bedtime=time(22, 0),
            wake_time=time(6, 0),
            temp_f=68,
            warm_wake=WarmWake(target_temp_f=116, duration_min=30),
        )
        events = sleep_schedule_to_events(schedule)
        times, temps = encode_sleep_schedule_events(events)
        assert len(times) == _TIMES_LENGTH
        assert len(temps) == _TEMPS_LENGTH


# ---------------------------------------------------------------------------
# Per-night warm wake variation
# ---------------------------------------------------------------------------


class TestPerNightVariation:
    def test_different_warm_wake_per_night(self) -> None:
        """The device supports per-night warm wake variation."""
        nights = [
            SleepScheduleNight(
                day=0,
                temps=[(time(22, 0), 68)],
                off_time=time(6, 0),
                warm_wake=WarmWake(target_temp_f=116, duration_min=30),
            ),
            SleepScheduleNight(
                day=1,
                temps=[(time(22, 0), 68)],
                off_time=time(7, 0),
                warm_wake=WarmWake(target_temp_f=100, duration_min=45),
            ),
            SleepScheduleNight(
                day=2,
                temps=[(time(22, 0), 68)],
                off_time=time(6, 0),
                warm_wake=None,  # No warm wake on Wednesday night
            ),
        ]
        schedule = OolerSleepSchedule(nights=nights)
        events = sleep_schedule_to_events(schedule)

        # Round-trip through structured
        rt_schedule = events_to_sleep_schedule(events)
        assert len(rt_schedule.nights) == 3

        mon = rt_schedule.nights[0]
        assert mon.warm_wake is not None
        assert mon.warm_wake.target_temp_f == 116
        assert mon.warm_wake.duration_min == 30

        tue = rt_schedule.nights[1]
        assert tue.warm_wake is not None
        assert tue.warm_wake.target_temp_f == 100
        assert tue.warm_wake.duration_min == 45

        wed = rt_schedule.nights[2]
        assert wed.warm_wake is None

    def test_different_temps_per_night(self) -> None:
        """Different temperature programs per night."""
        nights = [
            SleepScheduleNight(
                day=0,
                temps=[(time(22, 0), 68)],
                off_time=time(6, 0),
            ),
            SleepScheduleNight(
                day=4,
                temps=[(time(23, 0), 65), (time(2, 0), 62)],
                off_time=time(8, 0),
            ),
        ]
        schedule = OolerSleepSchedule(nights=nights)
        events = sleep_schedule_to_events(schedule)
        rt_schedule = events_to_sleep_schedule(events)

        assert len(rt_schedule.nights) == 2
        assert len(rt_schedule.nights[0].temps) == 1
        assert len(rt_schedule.nights[1].temps) == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestDefensiveGuards:
    """Cover defensive guard clauses for malformed/edge-case data."""

    def test_zero_minute_off_padding_detected(self) -> None:
        """A Mon 00:00 OFF followed by all-zero padding should stop decoding."""
        times = bytearray(_TIMES_LENGTH)
        temps = bytearray([0xFF] * _TEMPS_LENGTH)
        # First event: Mon 22:00 = 68°F
        import struct
        struct.pack_into("<H", times, 0, 1320)
        temps[0] = 68
        # Second event: Mon 00:00 (minute=0) OFF — followed by all zeros = padding
        # minute 0 is already zero, temp = OFF
        struct.pack_into("<H", times, 2, 0)
        temps[1] = 0x00
        # Rest is all zeros — should be treated as padding
        events = decode_sleep_schedule_events(bytes(times), bytes(temps))
        # Should get 2 events: the 68°F and the OFF at minute 0
        # Actually the OFF at minute 0 with all-zero remainder triggers the
        # padding check and stops. But minute 0 at index 1 with temp OFF
        # and all remaining zeros = padding, so we get only 1 event.
        assert len(events) == 1
        assert events[0].temp_f == 68

    def test_events_to_schedule_no_groups_no_trailing(self) -> None:
        """All-OFF events produce no nights."""
        events = [SleepScheduleEvent(minute_of_week=360, temp_f=0)]
        schedule = events_to_sleep_schedule(events)
        assert schedule.nights == []

    def test_parse_night_empty_list(self) -> None:
        """_parse_night with empty list returns None."""
        from ooler_ble_client.sleep_schedule import _parse_night
        assert _parse_night([]) is None

    def test_parse_night_off_only(self) -> None:
        """A group with only OFF events produces no night."""
        from ooler_ble_client.sleep_schedule import _parse_night
        result = _parse_night([SleepScheduleEvent(minute_of_week=360, temp_f=0)])
        assert result is None

    def test_parse_night_all_off_events(self) -> None:
        """A group with multiple OFF events and no temps produces no night."""
        from ooler_ble_client.sleep_schedule import _parse_night
        events = [
            SleepScheduleEvent(minute_of_week=300, temp_f=0),
            SleepScheduleEvent(minute_of_week=360, temp_f=0),
        ]
        result = _parse_night(events)
        assert result is None

    def test_parse_night_off_sandwich_no_real_temps(self) -> None:
        """temp_events non-empty but all are OFF → no night."""
        from ooler_ble_client.sleep_schedule import _parse_night
        # Two OFFs then a final OFF (last stripped as off_time, leaving two OFFs)
        events = [
            SleepScheduleEvent(minute_of_week=100, temp_f=0),
            SleepScheduleEvent(minute_of_week=200, temp_f=0),
            SleepScheduleEvent(minute_of_week=360, temp_f=0),
        ]
        result = _parse_night(events)
        assert result is None


class TestEdgeCases:
    def test_midnight_event(self) -> None:
        """Events at exactly midnight (minute 0 of a day) should decode correctly."""
        events = [
            SleepScheduleEvent(minute_of_week=1320, temp_f=68),  # Mon 22:00
            SleepScheduleEvent(minute_of_week=1440, temp_f=62),  # Tue 00:00
            SleepScheduleEvent(minute_of_week=1800, temp_f=0),   # Tue 06:00
        ]
        times, temps = encode_sleep_schedule_events(events)
        decoded = decode_sleep_schedule_events(times, temps)
        assert decoded == events

    def test_week_boundary_sunday_open(self) -> None:
        """Sunday bedtime with no OFF wraps to next week — should parse as open night."""
        events = [
            SleepScheduleEvent(minute_of_week=9960, temp_f=68),  # Sun 22:00
        ]
        schedule = events_to_sleep_schedule(events)
        assert len(schedule.nights) == 1
        assert schedule.nights[0].day == 6
        assert schedule.nights[0].temps == [(time(22, 0), 68)]

    def test_monday_00_00_event(self) -> None:
        """An event at minute 0 (Mon 00:00) is valid, not padding."""
        events = [
            SleepScheduleEvent(minute_of_week=0, temp_f=62),     # Mon 00:00
            SleepScheduleEvent(minute_of_week=360, temp_f=0),    # Mon 06:00
            SleepScheduleEvent(minute_of_week=1320, temp_f=68),  # Mon 22:00
        ]
        times, temps = encode_sleep_schedule_events(events)
        decoded = decode_sleep_schedule_events(times, temps)
        assert len(decoded) == 3
        assert decoded[0].minute_of_week == 0
        assert decoded[0].temp_f == 62
