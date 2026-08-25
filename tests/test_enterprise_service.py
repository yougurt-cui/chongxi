from datetime import datetime

from services.enterprise_service import (
    _calendar_expiry,
    _is_calendar_expired,
    _normalize_legacy_expiry,
    _remaining_calendar_days,
    _serialize_local_datetimes,
)


def test_calendar_expiry_ignores_activation_clock_time():
    morning = _calendar_expiry(datetime(2026, 8, 24, 8, 5, 0), 7)
    evening = _calendar_expiry(datetime(2026, 8, 24, 19, 55, 9), 7)

    assert morning == datetime(2026, 8, 31, 0, 0, 0)
    assert evening == morning


def test_extension_adds_only_requested_whole_days():
    assert _calendar_expiry(datetime(2026, 9, 1, 0, 0, 0), 3) == datetime(2026, 9, 4, 0, 0, 0)


def test_legacy_clock_expiry_uses_same_calendar_date_boundary():
    assert _normalize_legacy_expiry(datetime(2026, 8, 31, 19, 55, 9)) == datetime(2026, 8, 31, 0, 0, 0)
    assert _normalize_legacy_expiry(datetime(2026, 9, 1, 0, 0, 0)) == datetime(2026, 9, 1, 0, 0, 0)


def test_remaining_days_only_changes_when_calendar_date_changes():
    expired_at = datetime(2026, 8, 31, 0, 0, 0)

    assert _remaining_calendar_days(expired_at, datetime(2026, 8, 25, 0, 0, 1)) == 6
    assert _remaining_calendar_days(expired_at, datetime(2026, 8, 25, 23, 59, 59)) == 6
    assert _remaining_calendar_days(expired_at, datetime(2026, 8, 30, 12, 0, 0)) == 1
    assert _remaining_calendar_days(expired_at, datetime(2026, 8, 31, 0, 0, 0)) == 0


def test_expiry_switches_at_start_of_expiry_date():
    expired_at = datetime(2026, 8, 31, 19, 55, 9)

    assert _is_calendar_expired(expired_at, datetime(2026, 8, 30, 23, 59, 59)) is False
    assert _is_calendar_expired(expired_at, datetime(2026, 8, 31, 0, 0, 0)) is True


def test_mysql_datetime_is_serialized_without_false_gmt_timezone():
    row = _serialize_local_datetimes({"used_at": datetime(2026, 8, 24, 11, 55, 9)})

    assert row["used_at"] == "2026-08-24T11:55:09"
