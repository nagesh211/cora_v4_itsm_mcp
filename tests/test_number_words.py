"""Spelled-out quantities must resolve identically to their digit form.

This is a silent-failure guard, not a nicety. ``resolve_dates`` never raises: an
unrecognised phrase falls back to month-to-date and sets ``matched=False``. So
"Compare AMESA vs APAC ... over the last six months" did not error — it answered
for the last two days, and looked entirely healthy doing it.
"""
import pytest

from cora_mcp.date_resolver import normalize_number_words, resolve_dates


@pytest.mark.parametrize("worded,digits", [
    ("last six months", "last 6 months"),
    ("last three months", "last 3 months"),
    ("over the last six months", "over the last 6 months"),
    ("last ninety days", "last 90 days"),
    ("last twenty one days", "last 21 days"),
    ("last twenty-one days", "last 21 days"),
    ("last ten weeks", "last 10 weeks"),
])
def test_worded_and_numeric_forms_agree(worded, digits):
    w, d = resolve_dates(worded), resolve_dates(digits)
    assert w["matched"] is True, f"{worded!r} silently fell back"
    assert (w["start_date"], w["end_date"]) == (d["start_date"], d["end_date"])


def test_the_question_that_exposed_this():
    q = "Compare AMESA vs APAC for incident volume, SLA, and MTTR over the last six months"
    assert resolve_dates("over the last six months")["matched"] is True
    assert "six" not in normalize_number_words(q)


# ---------------------------------------------------------------------------
# the rewrite itself
# ---------------------------------------------------------------------------
def test_compound_tens_and_units():
    assert normalize_number_words("twenty one") == "21"
    assert normalize_number_words("ninety-nine") == "99"


def test_articles_count_only_in_front_of_a_period_word():
    assert normalize_number_words("a month ago") == "1 month ago"
    # ...and never elsewhere, or "a lot of incidents" becomes "1 lot of incidents"
    assert normalize_number_words("a lot of incidents") == "a lot of incidents"


def test_is_idempotent_and_safe_on_plain_text():
    for s in ["last 6 months", "incident count by sector", ""]:
        assert normalize_number_words(s) == s
    once = normalize_number_words("last six months")
    assert normalize_number_words(once) == once


def test_digit_forms_are_untouched():
    assert normalize_number_words("last 90 days") == "last 90 days"