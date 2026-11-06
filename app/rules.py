"""Flow definition: steps, options, postback format and the qualification rule.

Every button and list row carries a postbackText of the form

    NWH_QUALIFIER|<STEP>|<OPTION>|<LEAD_ID>
    e.g. NWH_QUALIFIER|TIMELINE|1M|L12345
         NWH_QUALIFIER|VISIT_DATE|2026-11-07|L12345
         NWH_QUALIFIER|VISIT_TIME|1130|L12345

Rules key on these codes, never on the label the customer sees, so labels can be
reworded or translated without breaking tracking or logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Outcome

FLOW = "NWH_QUALIFIER"
STEP_TIMELINE = "TIMELINE"
STEP_VISIT_DATE = "VISIT_DATE"
STEP_VISIT_TIME = "VISIT_TIME"
STEP_CONFIRM = "CONFIRM_CHANGE"      # "Do you want to change your answer?" Yes / No
STEPS = {STEP_TIMELINE, STEP_VISIT_DATE, STEP_VISIT_TIME, STEP_CONFIRM}
QUALIFYING = ("15D", "1M", "1_2M")

# Template quick-reply buttons, in button order: (option code, label, timeline in days)
TIMELINE_OPTIONS: list[tuple[str, str, int | None]] = [
    ("15D", "15 Days", 15),
    ("1M", "1 Month", 30),
    ("1_2M", "1 - 2 Months", 60),
    ("GT2M", "More than 2 Months", 90),
    ("RESEARCH", "Just Researching", None),
]
TIMELINE_DAYS = {c: d for c, _, d in TIMELINE_OPTIONS}
TIMELINE_LABEL = {c: label for c, label, _ in TIMELINE_OPTIONS}

STOP_WORDS = {"stop", "unsubscribe", "opt out", "optout"}


@dataclass(frozen=True)
class Postback:
    flow: str
    step: str
    option: str
    lead_id: str | None


def build_postback(step: str, option: str, lead_id: str) -> str:
    return f"{FLOW}|{step}|{option}|{lead_id}"


def parse_postback(text: str | None) -> Postback | None:
    if not text:
        return None
    parts = text.split("|")
    if len(parts) < 3 or parts[0] != FLOW or parts[1] not in STEPS:
        return None
    step, option = parts[1], parts[2]
    if step == STEP_TIMELINE and option not in TIMELINE_DAYS:
        return None
    if step == STEP_VISIT_DATE and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", option):
        return None
    if step == STEP_VISIT_TIME and not re.fullmatch(r"\d{4}", option):
        return None
    if step == STEP_CONFIRM and not re.fullmatch(r"YES:(15D|1M|1_2M|GT2M|RESEARCH)|NO", option):
        return None
    return Postback(FLOW, step, option, parts[3] if len(parts) > 3 and parts[3] else None)


_TEXT_PATTERNS = [
    (re.compile(r"\b(15|fifteen)\s*days?\b|\bthis week\b|\bnext week\b|\bimmediate"), "15D"),
    (re.compile(r"\b(1|one|a)\s*months?\b"), "1M"),
    (re.compile(r"\b(1\s*-\s*2|2|two)\s*months?\b"), "1_2M"),
    (re.compile(r"\b([3-9]|three|four|six)\s*months?\b|\bnext year\b"), "GT2M"),
    (re.compile(r"\bresearch|\bjust looking\b|\bnot sure\b"), "RESEARCH"),
]


def classify_text(text: str) -> str | None:
    """Best-effort mapping of a typed reply to a timeline option (or STOP). Keyword based."""
    t = text.strip().lower()
    if t in STOP_WORDS:
        return "STOP"
    if re.search(r"more than\s*(2|two)\s*months?", t):
        return "GT2M"
    for pattern, code in _TEXT_PATTERNS:
        if pattern.search(t):
            return code
    return None


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    priority: str | None = None      # P1/P2 for qualified leads
    follow_up_days: int | None = None
    ask_visit: bool = False          # continue to the visit date question
    reason: str | None = None        # L2R "Not interested" reason, when the lead is closed
    sub_reason: str | None = None


def decide_timeline(option: str, qualify_max_days: int) -> Decision:
    if option == "STOP":
        return Decision(Outcome.OPT_OUT)
    if option == "RESEARCH":
        # Close the lead: Not interested -> "Not interested in buying" -> "Just researching"
        return Decision(Outcome.NOT_INTERESTED, reason="Not interested in buying", sub_reason="Just researching")
    days = TIMELINE_DAYS[option]
    if days is not None and days <= qualify_max_days:
        return Decision(Outcome.QUALIFIED, priority="P1" if days <= 30 else "P2", ask_visit=True)
    return Decision(Outcome.NURTURE, follow_up_days=60)


# ---- messages sent back to the customer (session messages, free inside 24h) ----
def gt2m_text(name: str, virtual_number: str) -> str:
    return (f"Thanks, {name}!\nWe'll check in closer to your timeline.\n"
            f"Need help before that? Call us anytime at {virtual_number}.")


def research_text(virtual_number: str) -> str:
    return f"Thanks for the confirmation. If you need any assistance in future, please feel free to reach us at {virtual_number}."


def visit_confirm_text(dealer: str, city: str, day_label: str, slot_label: str,
                       sales_executive_number: str) -> str:
    where = f"{dealer}, {city}" if city else dealer
    return (f"\u2705 Your visit is booked\n\n\U0001F4CD {where}\n\U0001F4C5 {day_label}\n\U0001F552 {slot_label}\n\n"
            f"To connect with the dealership, call {sales_executive_number}.")


# ---- changed answers (the customer taps the template again) -------------
def same_answer_text(label: str) -> str:
    return f"Got it, '{label}' is already noted. Thank you!"


def updated_text(label: str) -> str:
    return "Thank you for the confirmation."


def confirm_change_text(prev_label: str, new_label: str) -> str:
    return f'You earlier told us "{prev_label}". Do you want to change it to "{new_label}"?'


def kept_text() -> str:
    return "No problem, we've kept your earlier answer."



# ---- typed messages (the customer types instead of tapping) -------------
HI_PATTERN = re.compile(r"^\s*(hi+|hello|hey)\b", re.I)


def is_hi(text: str) -> bool:
    return bool(HI_PATTERN.match(text or ""))


def ask_hi_text() -> str:
    return "Please type Hi to start the journey."


def dealer_assigned_text(dealer: str, sales_executive_number: str, brand: str, support_number: str) -> str:
    return (f"{dealer} dealership has been assigned to you. Call them at {sales_executive_number} for assistance.\n"
            f"To connect with the {brand} team, call {support_number}.")


def reengage_text(brand: str, vehicle: str) -> str:
    return (f"Thanks for connecting with {brand}. We can see you have shown interest in the {vehicle}. "
            f"Can you tell us how soon you are planning to buy?")
