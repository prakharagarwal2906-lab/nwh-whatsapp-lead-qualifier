import pytest

from app.models import Outcome
from app.rules import Postback, build_postback, classify_text, decide_timeline, parse_postback


@pytest.mark.parametrize("option,outcome,priority,ask_visit", [
    ("15D", Outcome.QUALIFIED, "P1", True),
    ("1M", Outcome.QUALIFIED, "P1", True),
    ("1_2M", Outcome.QUALIFIED, "P2", True),
    ("GT2M", Outcome.NURTURE, None, False),
    ("RESEARCH", Outcome.NOT_INTERESTED, None, False),
    ("STOP", Outcome.OPT_OUT, None, False),
])
def test_decide_timeline(option, outcome, priority, ask_visit):
    d = decide_timeline(option, qualify_max_days=60)
    assert (d.outcome, d.priority, d.ask_visit) == (outcome, priority, ask_visit)


def test_threshold_is_configurable():
    assert decide_timeline("GT2M", qualify_max_days=90).outcome == Outcome.QUALIFIED
    assert decide_timeline("1_2M", qualify_max_days=30).outcome == Outcome.NURTURE


def test_follow_up_days():
    assert decide_timeline("GT2M", 60).follow_up_days == 60
    d = decide_timeline("RESEARCH", 60)
    assert (d.reason, d.sub_reason, d.follow_up_days) == ("Not interested in buying", "Just researching", None)


def test_postback_round_trip():
    pb = build_postback("TIMELINE", "1M", "L12345")
    assert pb == "NWH_QUALIFIER|TIMELINE|1M|L12345"
    assert parse_postback(pb) == Postback("NWH_QUALIFIER", "TIMELINE", "1M", "L12345")
    assert parse_postback("NWH_QUALIFIER|VISIT_DATE|2026-11-07|L1").option == "2026-11-07"
    assert parse_postback("NWH_QUALIFIER|VISIT_TIME|1130|L1").step == "VISIT_TIME"


@pytest.mark.parametrize("bad", [None, "", "TIMELINE_1M|L1", "OTHER_FLOW|TIMELINE|1M|L1",
                                 "NWH_QUALIFIER|TIMELINE|BOGUS|L1", "NWH_QUALIFIER|VISIT_DATE|tomorrow|L1",
                                 "NWH_QUALIFIER|VISIT_TIME|11am|L1", "NWH_QUALIFIER|PAYMENT|X|L1"])
def test_parse_postback_rejects_bad_input(bad):
    assert parse_postback(bad) is None


@pytest.mark.parametrize("text,code", [
    ("1 month", "1M"), ("within a month", "1M"), ("next week", "15D"), ("2 months", "1_2M"),
    ("more than 2 months", "GT2M"), ("6 months", "GT2M"), ("just looking", "RESEARCH"),
    ("STOP", "STOP"), ("hello", None),
])
def test_classify_text(text, code):
    assert classify_text(text) == code
