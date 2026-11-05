from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MsgStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    REPLIED = "REPLIED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


# Status only moves forward by rank, so out-of-order events
# (read arriving before delivered) never move a message backwards.
STATUS_RANK = {
    MsgStatus.PENDING: 0,
    MsgStatus.SENT: 1,
    MsgStatus.DELIVERED: 2,
    MsgStatus.READ: 3,
    MsgStatus.REPLIED: 4,
    MsgStatus.FAILED: 5,
    MsgStatus.EXPIRED: 5,
}
TERMINAL = {MsgStatus.REPLIED, MsgStatus.FAILED, MsgStatus.EXPIRED}


class Outcome(str, Enum):
    QUALIFIED = "QUALIFIED"
    NURTURE = "NURTURE"
    HANDOFF = "HANDOFF"
    NO_RESPONSE = "NO_RESPONSE"
    OPT_OUT = "OPT_OUT"


@dataclass
class Lead:
    lead_id: str
    dealer_id: str
    mobile: str  # E.164 without "+", e.g. 919876543210
    customer_name: str
    model_name: str
    created_at: datetime
    whatsapp_consent: bool = True
    stage: str = "FIRST_CALL"
    closed: bool = False


@dataclass
class DealerConfig:
    dealer_id: str
    enabled: bool
    dealer_name: str
    gupshup_app_id: str
    source_number: str
    template_id: str
    timezone: str
    # {"mon": [["09:30", "17:30"]], ... } ; missing day = weekly off
    working_hours: dict[str, list[list[str]]]
    holidays: list[str] = field(default_factory=list)  # ISO dates
    pre_close_buffer_min: int = 15
    qualify_max_days: int = 60
    rep_name: str = "our representative"
    rep_phone: str = ""
    holdout_pct: int = 0


@dataclass
class WaMessage:
    id: int
    lead_id: str
    dealer_id: str
    mobile: str
    template_id: str
    gs_message_id: str | None
    wamid: str | None
    status: MsgStatus
    sent_at: datetime | None
    reply_code: str | None
    outcome: Outcome | None
    l2r_sync_status: str
