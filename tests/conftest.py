from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.clients import SendError, TemplateSend
from app.config import Settings
from app.db import Repo
from app.main import create_app
from app.models import DealerConfig, Lead
from app.service import QualifierService

IST = ZoneInfo("Asia/Kolkata")
TOKEN = "test-token"


class FakeGupshup:
    def __init__(self):
        self.templates: list[TemplateSend] = []
        self.texts: list[tuple[str, str]] = []
        self.fail: SendError | None = None
        self._n = 0

    def send_template(self, req: TemplateSend) -> str:
        if self.fail:
            raise self.fail
        self.templates.append(req)
        self._n += 1
        return f"gs-{self._n}"

    def send_text(self, app_id, source, destination, text):
        self.texts.append((destination, text))


class FakeL2R:
    def __init__(self):
        self.leads: dict[str, Lead] = {}
        self.dispositions: list[tuple[str, dict, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.down = False

    def get_lead(self, lead_id):
        if self.down:
            raise ConnectionError("L2R down")
        return self.leads.get(lead_id)

    def dispose(self, lead_id, body, idempotency_key):
        self.dispositions.append((lead_id, body, idempotency_key))
        if body["targetStage"] == "QUALIFICATION":
            self.leads[lead_id].stage = "QUALIFICATION"

    def add_comment(self, lead_id, text, idempotency_key):
        self.comments.append((lead_id, text))


def dealer(**kw) -> DealerConfig:
    base = dict(
        dealer_id="D1", enabled=True, dealer_name="Vishnu Cars TN", gupshup_app_id="app1",
        source_number="919403897047", template_id="tpl-nwh-1", timezone="Asia/Kolkata",
        working_hours={d: [["09:30", "17:30"]] for d in ["mon", "tue", "wed", "thu", "fri", "sat"]},
        holidays=[], rep_name="Sathya", rep_phone="9403897047",
    )
    base.update(kw)
    return DealerConfig(**base)


def lead(lead_id="L1", mobile="919876543210", created=None, **kw) -> Lead:
    return Lead(
        lead_id=lead_id, dealer_id="D1", mobile=mobile, customer_name="Prakhar",
        model_name="Maruti Suzuki Brezza",
        created_at=created or datetime(2026, 11, 6, 0, 26, tzinfo=IST),  # Fri 12:26 AM
        **kw,
    )


def quick_reply(code: str, lead_id: str, gs_id: str, mobile="919876543210", msg_id="wamid.in1",
                text="1 Month"):
    return {
        "app": "VishnuCarsTN", "timestamp": int(time.time() * 1000), "version": 2,
        "type": "message",
        "payload": {
            "id": msg_id, "source": mobile, "type": "quick_reply",
            "payload": {"text": text, "postbackText": f"{code}|{lead_id}"},
            "context": {"id": "wamid.out1", "gsId": gs_id},
        },
    }


@pytest.fixture
def env(tmp_path):
    pg = os.getenv("TEST_DATABASE_URL")
    if pg:  # run the same suite against Postgres
        import psycopg
        with psycopg.connect(pg, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS dealer_config, wa_message, wa_event, audit_log, opt_out")
    repo = Repo(pg or str(tmp_path / "t.db"))
    repo.upsert_dealer(dealer())
    gs, l2r = FakeGupshup(), FakeL2R()
    svc = QualifierService(repo, gs, l2r)
    settings = Settings(db_path=str(tmp_path / "t.db"), webhook_tokens=[TOKEN, "old-token"],
                        log_file=str(tmp_path / "logs" / "audit.log"))
    client = TestClient(create_app(svc, settings))

    class Env:
        pass

    e = Env()
    e.repo, e.gs, e.l2r, e.svc, e.client = repo, gs, l2r, svc, client
    return e
