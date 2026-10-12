"""Timetable logic: which repeating calendar events fall on a given day, and how dates are read aloud."""
import datetime as dt

import skills

MONDAY = dt.datetime(2026, 8, 17, 10, 0)  # first day of a semester, a Monday


def on(day):
    return dt.date(2026, 8, day)


def test_one_off_event_only_on_its_day():
    assert skills._happens_on(on(17), MONDAY, "", set())
    assert not skills._happens_on(on(18), MONDAY, "", set())


def test_weekly_on_chosen_weekdays():
    rule = "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    assert [d for d in range(17, 24) if skills._happens_on(on(d), MONDAY, rule, set())] == [17, 19, 21]


def test_every_other_week():
    rule = "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"
    assert skills._happens_on(on(17), MONDAY, rule, set())
    assert not skills._happens_on(on(24), MONDAY, rule, set())
    assert skills._happens_on(on(31), MONDAY, rule, set())


def test_until_count_and_exceptions_end_a_series():
    assert not skills._happens_on(on(31), MONDAY, "FREQ=WEEKLY;UNTIL=20260825T000000Z", set())
    assert skills._happens_on(on(24), MONDAY, "FREQ=WEEKLY;COUNT=2", set())
    assert not skills._happens_on(on(31), MONDAY, "FREQ=WEEKLY;COUNT=2", set())
    assert not skills._happens_on(on(24), MONDAY, "FREQ=WEEKLY", {on(24)})


def test_nothing_before_the_series_starts():
    assert not skills._happens_on(on(10), MONDAY, "FREQ=DAILY", set())


def test_ical_dates_parse():
    assert skills._when("20260817T100000") == dt.datetime(2026, 8, 17, 10, 0)
    assert skills._when("20260817") == dt.datetime(2026, 8, 17)


def test_bible_references_read_aloud():
    assert skills._spoken_reference("Philippians 4:6-7") == "Philippians, chapter 4, verses 6 to 7"
    assert skills._spoken_reference("John 3:16") == "John, chapter 3, verse 16"


def test_app_names_match_despite_mishearing():
    assert skills._closest("spotfy", skills.APPS) == "spotify"
    assert skills._closest("vs code", skills.APPS) == "vs code"
    assert skills._closest("zzzz", skills.APPS) is None


def test_which_day_a_request_means():
    today = dt.date.today()
    assert skills._which_day("what classes do I have tomorrow") == today + dt.timedelta(days=1)
    assert skills._which_day("classes on friday").weekday() == 4
    assert skills._which_day("what's on today") == today
