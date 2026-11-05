import pytest

from app.models import Outcome
from app.rules import classify_text, decide, parse_postback


@pytest.mark.parametrize("code,outcome,priority", [
    ("TIMELINE_15D", Outcome.QUALIFIED, "P1"),
    ("TIMELINE_1M", Outcome.QUALIFIED, "P1"),
    ("TIMELINE_1_2M", Outcome.QUALIFIED, "P2"),
    ("TIMELINE_GT2M", Outcome.NURTURE, None),
    ("RESEARCHING", Outcome.NURTURE, None),
    ("HANDOFF_SE", Outcome.HANDOFF, "P1"),
])
def test_decide(code, outcome, priority):
    d = decide(code, qualify_max_days=60, rep_name="Sathya")
    assert d.outcome == outcome
    assert d.priority == priority


def test_threshold_is_configurable():
    assert decide("TIMELINE_GT2M", qualify_max_days=90, rep_name="x").outcome == Outcome.QUALIFIED
    assert decide("TIMELINE_1_2M", qualify_max_days=30, rep_name="x").outcome == Outcome.NURTURE


def test_parse_postback():
    assert parse_postback("TIMELINE_1M|L12345") == ("TIMELINE_1M", "L12345")
    assert parse_postback("timeline_1m") == ("TIMELINE_1M", None)
    assert parse_postback("BOGUS|L1") == (None, "L1")
    assert parse_postback(None) == (None, None)


@pytest.mark.parametrize("text,code", [
    ("1 month", "TIMELINE_1M"),
    ("within a month", "TIMELINE_1M"),
    ("next week", "TIMELINE_15D"),
    ("2 months", "TIMELINE_1_2M"),
    ("more than 2 months", "TIMELINE_GT2M"),
    ("6 months", "TIMELINE_GT2M"),
    ("just looking", "RESEARCHING"),
    ("STOP", "STOP"),
    ("hello", None),
])
def test_classify_text(text, code):
    assert classify_text(text) == code
