from datetime import date, datetime

import pytest

from app import planner


@pytest.mark.parametrize("raw, expected", [
    ("06 12 34 56 78", "+33612345678"),
    ("07.12.34.56.78", "+33712345678"),
    ("+33 6 12 34 56 78", "+33612345678"),
    ("0033612345678", "+33612345678"),
    ("01 23 45 67 89", None),       # landline
    ("+44 7700 900123", None),      # foreign
    ("0612", None),
    ("", None),
    (None, None),
])
def test_french_mobile(raw, expected):
    assert planner.french_mobile(raw) == expected


def test_to_gsm_strips_accents_and_extension_chars():
    assert planner.to_gsm("Hélène « à » l’œil {€}") == "Helene \" a \" l'oeil "


def test_send_time_moves_back_to_previous_working_day(env):
    open_weekdays = lambda d: d.isoweekday() <= 5 and d != date(2026, 11, 11)  # noqa: E731
    # Monday appointment, N=1 -> Sunday closed -> Saturday closed -> Friday 09:00
    assert planner.send_time_for(date(2026, 11, 16), open_weekdays) == datetime(2026, 11, 13, 9, 0)
    # Thursday 12 -> Wednesday 11 is a holiday -> Tuesday 10
    assert planner.send_time_for(date(2026, 11, 12), open_weekdays) == datetime(2026, 11, 10, 9, 0)


def test_render_has_no_accent_and_fits_one_sms(env):
    text = planner.render("Dr Hélène Carrio", datetime(2026, 2, 3, 14, 30))
    assert "Dr Helene Carrio" in text and "mardi 3 fevrier a 14h30" in text
    assert text.isascii() and len(text) <= 160
