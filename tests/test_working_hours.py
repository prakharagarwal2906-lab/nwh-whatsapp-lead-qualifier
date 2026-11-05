from datetime import datetime

from app.working_hours import DealerCalendar

from .conftest import IST, dealer


def cal(**kw):
    return DealerCalendar(dealer(**kw))


def test_midnight_is_non_working():
    assert cal().is_non_working(datetime(2026, 11, 6, 0, 26, tzinfo=IST))


def test_mid_morning_is_working():
    assert not cal().is_non_working(datetime(2026, 11, 6, 11, 0, tzinfo=IST))


def test_pre_close_buffer_counts_as_non_working():
    assert cal().is_non_working(datetime(2026, 11, 6, 17, 20, tzinfo=IST))
    assert not cal().is_non_working(datetime(2026, 11, 6, 17, 10, tzinfo=IST))


def test_weekly_off_and_holiday():
    assert cal().is_non_working(datetime(2026, 11, 8, 11, 0, tzinfo=IST))  # Sunday
    assert cal(holidays=["2026-11-09"]).is_non_working(datetime(2026, 11, 9, 11, 0, tzinfo=IST))


def test_utc_input_is_converted_to_dealer_timezone():
    # 05:30 UTC == 11:00 IST
    from datetime import timezone
    assert not cal().is_non_working(datetime(2026, 11, 6, 5, 30, tzinfo=timezone.utc))


def test_callback_phrase_tomorrow():
    assert cal().callback_phrase(datetime(2026, 11, 5, 22, 0, tzinfo=IST)) == ("tomorrow", "9:30 AM", "5:30 PM")


def test_callback_phrase_same_day_early_morning():
    assert cal().callback_phrase(datetime(2026, 11, 6, 0, 26, tzinfo=IST))[0] == "today"


def test_callback_phrase_skips_sunday():
    # Saturday 9 PM -> Monday
    assert cal().callback_phrase(datetime(2026, 11, 7, 21, 0, tzinfo=IST))[0] == "on Monday"
