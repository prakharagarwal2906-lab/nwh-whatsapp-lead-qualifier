"""HTTP clients for Gupshup and L2R.

The L2R endpoint shapes are assumptions; adjust paths and fields to the real API.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .models import Lead


class SendError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class TemplateSend:
    app_id: str
    source: str
    destination: str
    template_id: str
    params: list[str]
    postbacks: list[str]  # one per quick-reply button, in button order


class GupshupClient(Protocol):
    def send_template(self, req: TemplateSend) -> str: ...
    def send_text(self, app_id: str, source: str, destination: str, text: str) -> None: ...


class L2RClient(Protocol):
    def get_lead(self, lead_id: str) -> Lead | None: ...
    def dispose(self, lead_id: str, body: dict, idempotency_key: str) -> None: ...
    def add_comment(self, lead_id: str, text: str, idempotency_key: str) -> None: ...


def _with_retry(fn, attempts: int = 4, base: float = 0.5):
    for i in range(attempts):
        try:
            return fn()
        except SendError as e:
            if not e.retryable or i == attempts - 1:
                raise
            time.sleep(base * (2 ** i))


class HttpGupshupClient:
    def __init__(self, base_url: str, app_token: str, timeout: float = 10.0):
        self.http = httpx.Client(base_url=base_url, timeout=timeout,
                                 headers={"Authorization": app_token})

    def _post(self, path: str, data: dict) -> dict:
        try:
            r = self.http.post(path, data=data)
        except httpx.TransportError as e:
            raise SendError(f"transport: {e}", retryable=True) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise SendError(f"{r.status_code}: {r.text}", retryable=True)
        if r.status_code >= 400:
            raise SendError(f"{r.status_code}: {r.text}", retryable=False)
        return r.json()

    def send_template(self, req: TemplateSend) -> str:
        data = {
            "source": req.source,
            "destination": req.destination,
            "template": json.dumps({"id": req.template_id, "params": req.params}),
            "postbackTexts": json.dumps(
                [{"index": i, "text": t} for i, t in enumerate(req.postbacks)]
            ),
        }
        body = _with_retry(lambda: self._post(f"/partner/app/{req.app_id}/template/msg", data))
        msg_id = body.get("messageId")
        if not msg_id:
            raise SendError(f"no messageId in response: {body}", retryable=False)
        return msg_id

    def send_text(self, app_id: str, source: str, destination: str, text: str) -> None:
        data = {
            "channel": "whatsapp", "source": source, "destination": destination,
            "message": json.dumps({"type": "text", "text": text}),
        }
        _with_retry(lambda: self._post(f"/partner/app/{app_id}/v3/message", data))


class HttpL2RClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0):
        self.http = httpx.Client(base_url=base_url, timeout=timeout,
                                 headers={"X-Api-Key": api_key})

    def get_lead(self, lead_id: str) -> Lead | None:
        from datetime import datetime
        r = self.http.get(f"/api/v1/leads/{lead_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()
        return Lead(
            lead_id=d["leadId"], dealer_id=d["dealerId"], mobile=d["mobile"],
            customer_name=d.get("customerName", ""), model_name=d.get("modelName", ""),
            created_at=datetime.fromisoformat(d["createdAt"]),
            whatsapp_consent=d.get("whatsappConsent", True),
            stage=d.get("stage", "FIRST_CALL"), closed=d.get("closed", False),
        )

    def dispose(self, lead_id: str, body: dict, idempotency_key: str) -> None:
        r = self.http.post(f"/api/v1/leads/{lead_id}/dispositions", json=body,
                           headers={"Idempotency-Key": idempotency_key})
        r.raise_for_status()

    def add_comment(self, lead_id: str, text: str, idempotency_key: str) -> None:
        r = self.http.post(f"/api/v1/leads/{lead_id}/comments",
                           json={"comment": text, "actor": "system:whatsapp-bot"},
                           headers={"Idempotency-Key": idempotency_key})
        r.raise_for_status()
