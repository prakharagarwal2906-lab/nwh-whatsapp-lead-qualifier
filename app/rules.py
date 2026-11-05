"""Quick-reply postback codes and the qualification rule.

Rules key on postback codes, never on button labels, so buttons can be
reworded or translated without breaking the logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Outcome

# Order matches the template's button order (index 0..n).
BUTTONS: list[tuple[str, str, int | None]] = [
    # (code, label, timeline in days or None)
    ("TIMELINE_15D", "15 Days", 15),
    ("TIMELINE_1M", "1 Month", 30),
    ("TIMELINE_1_2M", "1 - 2 Months", 60),
    ("TIMELINE_GT2M", "More than 2 Months", 90),
    ("RESEARCHING", "Still Researching", None),
]
EXTRA_CODES = {"HANDOFF_SE"}
KNOWN_CODES = {c for c, _, _ in BUTTONS} | EXTRA_CODES
TIMELINE_DAYS = {c: d for c, _, d in BUTTONS}

STOP_WORDS = {"stop", "unsubscribe", "opt out", "optout"}


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    code: str
    priority: str | None = None  # P1/P2 for qualified leads
    follow_up_days: int | None = None
    reply_text: str = ""


def build_postback(code: str, lead_id: str) -> str:
    return f"{code}|{lead_id}"


def parse_postback(postback: str | None) -> tuple[str | None, str | None]:
    """'TIMELINE_1M|L123' -> ('TIMELINE_1M', 'L123')."""
    if not postback:
        return None, None
    code, _, lead_id = postback.partition("|")
    code = code.strip().upper()
    return (code if code in KNOWN_CODES else None), (lead_id or None)


_TEXT_PATTERNS = [
    (re.compile(r"\b(15|fifteen)\s*days?\b|\bthis week\b|\bnext week\b|\bimmediate"), "TIMELINE_15D"),
    (re.compile(r"\b(1|one|a)\s*months?\b"), "TIMELINE_1M"),
    (re.compile(r"\b(1\s*-\s*2|2|two)\s*months?\b"), "TIMELINE_1_2M"),
    (re.compile(r"\b([3-9]|three|four|six)\s*months?\b|\bnext year\b"), "TIMELINE_GT2M"),
    (re.compile(r"\bresearch|\bjust looking\b|\bnot sure\b"), "RESEARCHING"),
]


def classify_text(text: str) -> str | None:
    """Best-effort mapping of a typed reply to a code. v1 is keyword based."""
    t = text.strip().lower()
    if t in STOP_WORDS:
        return "STOP"
    if re.search(r"more than\s*(2|two)\s*months?", t):
        return "TIMELINE_GT2M"
    for pattern, code in _TEXT_PATTERNS:
        if pattern.search(t):
            return code
    return None


def decide(code: str, qualify_max_days: int, rep_name: str) -> Decision:
    if code == "STOP":
        return Decision(Outcome.OPT_OUT, code, reply_text="You won't get further messages from us here.")
    if code == "HANDOFF_SE":
        return Decision(
            Outcome.HANDOFF, code, priority="P1",
            reply_text=f"Sure! {rep_name} will call you as soon as we open.",
        )
    if code == "RESEARCHING":
        return Decision(
            Outcome.NURTURE, code, follow_up_days=7,
            reply_text="No problem. We'll share details to help you compare and check in next week.",
        )
    days = TIMELINE_DAYS[code]
    if days is not None and days <= qualify_max_days:
        return Decision(
            Outcome.QUALIFIED, code,
            priority="P1" if days <= 30 else "P2",
            reply_text=f"Thanks! {rep_name} will call you first thing when we open.",
        )
    return Decision(
        Outcome.NURTURE, code, follow_up_days=60,
        reply_text="Thanks! We'll check in closer to your timeline.",
    )
