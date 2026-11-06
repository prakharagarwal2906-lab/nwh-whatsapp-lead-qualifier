"""FastAPI entrypoint.

Endpoints
  POST /v1/webhooks/gupshup     callback URL configured in the Gupshup dashboard
  POST /internal/leads/created  LeadCreated event from L2R (or a queue consumer)
  POST /internal/maintenance    run by a scheduler every few minutes
  GET  /internal/db             row counts for every table
  GET  /internal/db/{table}     rows of one table (?limit, ?offset, ?lead_id)
  GET  /internal/leads/{id}/audit  full audit trail for one lead
  GET  /internal/engagement     engagement % per template, option split, visit steps
  PUT  /internal/dealers/{id}   dealer working hours and template config
  GET  /healthz
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

from .clients import HttpGupshupClient, HttpL2RClient
from .config import Settings, get_settings
from .db import Repo
from .models import DealerConfig, Lead
from .security import ip_allowed, token_valid
from .service import QualifierService

logging.basicConfig(level=logging.INFO)


def setup_file_log(path: str) -> None:
    """JSON-lines audit log, rotated at 10 MB, 5 files kept."""
    if not path:
        return
    lg = logging.getLogger("qualifier.audit")
    if any(isinstance(h, RotatingFileHandler) for h in lg.handlers):
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    h = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5)
    h.setFormatter(logging.Formatter('{"ts":"%(asctime)s","level":"%(levelname)s","data":%(message)s}'))
    lg.addHandler(h)


class LeadCreatedIn(BaseModel):
    leadId: str
    dealerId: str
    mobile: str
    customerName: str = ""
    make: str = ""
    modelName: str = ""
    city: str = ""
    createdAt: datetime
    whatsappConsent: bool = True


class InquiryAddedIn(LeadCreatedIn):
    inquiryId: str


class DealerConfigIn(BaseModel):
    whatsappEnabled: bool = True
    dealerName: str
    timezone: str = "Asia/Kolkata"
    workingHours: dict[str, list[list[str]]]   # a missing day is a day off
    holidays: list[str] = []
    repName: str = "our representative"   # sales executive
    salesExecutiveNumber: str = ""        # fixed sales executive number for this dealer
    city: str = ""
    holdoutPct: int = 0
    # MASTER only
    gupshupAppId: str = ""
    wabaNumber: str = ""
    templateId: str = ""
    supportNumber: str = ""


def create_app(service: QualifierService | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if service is None:
        service = QualifierService(
            Repo(settings.database_url or settings.db_path),
            HttpGupshupClient(settings.gupshup_base_url, settings.gupshup_app_token),
            HttpL2RClient(settings.l2r_base_url, settings.l2r_api_key),
            max_event_age_seconds=settings.max_event_age_seconds,
            session_window_hours=settings.session_window_hours,
            brand_name=settings.brand_name, support_number=settings.support_number,
            waba_number=settings.waba_number, gupshup_app_id=settings.gupshup_app_id,
            team_working_hours=settings.team_working_hours, team_timezone=settings.team_timezone,
        )
    setup_file_log(settings.log_file)
    app = FastAPI(title="WhatsApp After-Hours Lead Qualifier")
    app.state.service = service

    def internal_auth(x_internal_key: str | None = Header(default=None)) -> None:
        if settings.internal_api_key and not (
            x_internal_key and hmac.compare_digest(x_internal_key, settings.internal_api_key)
        ):
            raise HTTPException(status_code=401, detail="invalid internal key")

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "db": service.repo.backend}

    @app.post("/v1/webhooks/gupshup")
    async def gupshup_webhook(request: Request, background: BackgroundTasks) -> dict:
        client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else None
        )
        if not ip_allowed(client_ip, settings.allowed_ips):
            raise HTTPException(status_code=403, detail="source not allowed")
        if not token_valid(request.headers.get(settings.webhook_token_header), settings.webhook_tokens):
            raise HTTPException(status_code=401, detail="invalid token")
        try:
            evt = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid json")
        if not isinstance(evt, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        should_process, reason = service.accept(evt)
        if should_process:
            # Ack fast; the slow work (L2R calls) runs after the response.
            background.add_task(service.process, evt)
        return {"accepted": should_process, "reason": reason if not should_process else "queued"}

    @app.post("/internal/leads/created", dependencies=[Depends(internal_auth)])
    def lead_created(body: LeadCreatedIn) -> dict:
        lead = Lead(
            lead_id=body.leadId, dealer_id=body.dealerId, mobile=body.mobile,
            customer_name=body.customerName, model_name=body.modelName, make=body.make, city=body.city,
            created_at=body.createdAt, whatsapp_consent=body.whatsappConsent,
        )
        r = service.on_lead_created(lead)
        return {"sent": r.sent, "reason": r.reason, "messageId": r.message_id}

    @app.post("/internal/leads/inquiry-added", dependencies=[Depends(internal_auth)])
    def inquiry_added(body: InquiryAddedIn) -> dict:
        """L2R: a new inquiry (different vehicle version) was added to an open lead."""
        lead = Lead(
            lead_id=body.leadId, dealer_id=body.dealerId, mobile=body.mobile,
            customer_name=body.customerName, model_name=body.modelName, make=body.make, city=body.city,
            created_at=body.createdAt, whatsapp_consent=body.whatsappConsent,
        )
        r = service.on_inquiry_added(lead, body.inquiryId)
        return {"sent": r.sent, "reason": r.reason, "messageId": r.message_id}

    @app.put("/internal/dealers/{dealer_id}", dependencies=[Depends(internal_auth)])
    def put_dealer(dealer_id: str, body: DealerConfigIn) -> dict:
        service.repo.upsert_dealer(DealerConfig(
            dealer_id=dealer_id, enabled=body.whatsappEnabled, dealer_name=body.dealerName,
            timezone=body.timezone, working_hours=body.workingHours, holidays=body.holidays,
            rep_name=body.repName, sales_executive_number=body.salesExecutiveNumber, city=body.city,
            holdout_pct=body.holdoutPct, gupshup_app_id=body.gupshupAppId, source_number=body.wabaNumber,
            template_id=body.templateId, support_number=body.supportNumber,
        ))
        return {"ok": True, "db": service.repo.backend}

    @app.post("/internal/maintenance", dependencies=[Depends(internal_auth)])
    def maintenance() -> dict:
        return service.run_maintenance()

    @app.get("/internal/db", dependencies=[Depends(internal_auth)])
    def db_overview() -> dict:
        return {"tables": service.repo.table_counts()}

    @app.get("/internal/db/{table}", dependencies=[Depends(internal_auth)])
    def db_rows(table: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                lead_id: str | None = None) -> dict:
        try:
            rows = service.repo.list_rows(table, limit, offset, lead_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown table; use one of {list(service.repo.TABLES)}")
        return {"table": table, "count": len(rows), "rows": rows}

    @app.get("/internal/engagement", dependencies=[Depends(internal_auth)])
    def engagement() -> dict:
        return {"flow": "NWH_QUALIFIER", "templates": service.repo.engagement()}

    @app.get("/internal/leads/{lead_id}/audit", dependencies=[Depends(internal_auth)])
    def lead_audit(lead_id: str) -> dict:
        return {
            "lead_id": lead_id,
            "messages": service.repo.list_rows("wa_message", 100, 0, lead_id),
            "interactions": [r for r in service.repo.list_rows("wa_interaction", 1000) if r["lead_id"] == lead_id],
            "audit": list(reversed(service.repo.list_rows("audit_log", 1000, 0, lead_id))),
        }

    return app


app = create_app()
