"""Core flows: send on lead creation, process inbound Gupshup events, sync to L2R."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .clients import GupshupClient, L2RClient, SendError, TemplateSend
from .db import Repo
from .models import Lead, MsgStatus, Outcome
from .rules import BUTTONS, build_postback, classify_text, decide, parse_postback
from .working_hours import DealerCalendar

log = logging.getLogger("qualifier")
audit_log = logging.getLogger("qualifier.audit")

EVENT_STATUS = {
    "enqueued": MsgStatus.SENT,
    "sent": MsgStatus.SENT,
    "delivered": MsgStatus.DELIVERED,
    "read": MsgStatus.READ,
    "failed": MsgStatus.FAILED,
}
TIMELINE_LABEL = {
    "TIMELINE_15D": "15_DAYS", "TIMELINE_1M": "1_MONTH", "TIMELINE_1_2M": "1_2_MONTHS",
    "TIMELINE_GT2M": "GT_2_MONTHS", "RESEARCHING": "RESEARCHING",
}


@dataclass
class SendResult:
    sent: bool
    reason: str
    message_id: int | None = None


def in_holdout(lead_id: str, pct: int) -> bool:
    """Deterministic, so a retried lead lands in the same group."""
    if pct <= 0:
        return False
    bucket = int(hashlib.sha256(lead_id.encode()).hexdigest(), 16) % 100
    return bucket < pct


class QualifierService:
    def __init__(self, repo: Repo, gupshup: GupshupClient, l2r: L2RClient,
                 max_event_age_seconds: int = 86400, session_window_hours: int = 24):
        self.repo = repo
        self.gupshup = gupshup
        self.l2r = l2r
        self.max_event_age = max_event_age_seconds
        self.window_hours = session_window_hours

    def audit(self, level: str, event: str, lead_id: str | None = None,
              message_id: int | None = None, **detail) -> None:
        """Write one audit row to the DB and one JSON line to the log file."""
        try:
            self.repo.add_audit(level, event, lead_id, message_id, detail or None)
        except Exception:
            log.exception("audit write failed")
        audit_log.log(logging.WARNING if level in ("WARN", "ERROR") else logging.INFO,
                      json.dumps({"event": event, "lead_id": lead_id, "message_id": message_id, **detail},
                                 default=str))

    # ------------------------------------------------------------------ send
    def on_lead_created(self, lead: Lead) -> SendResult:
        r = self._on_lead_created(lead)
        self.audit("INFO" if r.sent else ("ERROR" if r.reason == "send_failed" else "INFO"),
                   "template_sent" if r.sent else f"send_skipped:{r.reason}",
                   lead.lead_id, r.message_id, mobile=lead.mobile, dealer_id=lead.dealer_id)
        return r

    def _on_lead_created(self, lead: Lead) -> SendResult:
        cfg = self.repo.get_dealer(lead.dealer_id)
        if cfg is None or not cfg.enabled:
            return SendResult(False, "dealer_not_enabled")
        if not lead.whatsapp_consent:
            return SendResult(False, "no_consent")
        if not lead.mobile.isdigit() or len(lead.mobile) < 11:
            return SendResult(False, "invalid_mobile")
        cal = DealerCalendar(cfg)
        if not cal.is_non_working(lead.created_at):
            return SendResult(False, "working_hours")
        if self.repo.is_opted_out(lead.mobile, lead.dealer_id):
            return SendResult(False, "opted_out")
        if self.repo.recently_messaged(lead.mobile, lead.dealer_id, self.window_hours):
            return SendResult(False, "recently_messaged")
        if in_holdout(lead.lead_id, cfg.holdout_pct):
            return SendResult(False, "holdout")

        msg_id = self.repo.create_pending(lead.lead_id, lead.dealer_id, lead.mobile, cfg.template_id)
        if msg_id is None:
            return SendResult(False, "already_sent")

        day, start, end = cal.callback_phrase(lead.created_at)
        req = TemplateSend(
            app_id=cfg.gupshup_app_id,
            source=cfg.source_number,
            destination=lead.mobile,
            template_id=cfg.template_id,
            params=[lead.customer_name or "there", lead.model_name, cfg.dealer_name,
                    cfg.rep_name, day, start, end, cfg.rep_phone],
            postbacks=[build_postback(code, lead.lead_id) for code, _, _ in BUTTONS],
        )
        try:
            gs_id = self.gupshup.send_template(req)
        except SendError as e:
            self.repo.mark_failed(msg_id, str(e))
            self._safe_comment(lead.lead_id, f"WhatsApp qualifier could not send message: {e}",
                               f"wa-sendfail-{msg_id}")
            return SendResult(False, "send_failed", msg_id)
        # Written synchronously, before any reply can be processed.
        self.repo.mark_sent(msg_id, gs_id)
        return SendResult(True, "sent", msg_id)

    # --------------------------------------------------------------- inbound
    @staticmethod
    def event_id(evt: dict) -> str | None:
        p = evt.get("payload") or {}
        if evt.get("type") == "message":
            return p.get("id")
        if evt.get("type") == "message-event":
            key = p.get("gsId") or p.get("id")
            return f"{key}:{p.get('type')}" if key else None
        return None

    def accept(self, evt: dict) -> tuple[bool, str]:
        """Synchronous part of the webhook: validate shape and store for dedupe.

        Returns (should_process, reason).
        """
        eid = self.event_id(evt)
        if not eid:
            return False, "ignored_unknown_event"
        ok, reason = self._accept(evt, eid)
        if not ok:
            self.audit("WARN", f"webhook_{reason}", event_id=eid, type=evt.get("type"))
        return ok, reason

    def _accept(self, evt: dict, eid: str) -> tuple[bool, str]:
        ts = evt.get("timestamp")
        if isinstance(ts, (int, float)):
            age = datetime.now(timezone.utc).timestamp() - ts / 1000
            if age > self.max_event_age:
                return False, "stale"
        if not self.repo.store_event(eid, evt.get("type", ""), evt):
            return False, "duplicate"
        return True, eid

    def process(self, evt: dict) -> str:
        eid = self.event_id(evt) or "?"
        try:
            if evt.get("type") == "message-event":
                result = self._handle_status(evt["payload"])
            else:
                result = self._handle_message(evt["payload"])
        except Exception as e:  # keep the event for replay
            log.exception("processing failed for %s", eid)
            result = f"error:{type(e).__name__}"
        self.repo.finish_event(eid, result)
        msg = None
        p = evt.get("payload") or {}
        ctx = p.get("context") or {}
        gs = ctx.get("gsId") or p.get("gsId")
        if gs:
            msg = self.repo.find_message(gs_id=gs)
        level = "ERROR" if result.startswith(("error", "rejected")) else \
            "WARN" if result.startswith(("unmapped", "unclassified", "ignored")) else "INFO"
        self.audit(level, f"webhook_processed:{result}", msg.lead_id if msg else None,
                   msg.id if msg else None, event_id=eid, type=evt.get("type"))
        return result

    def _handle_status(self, p: dict) -> str:
        status = EVENT_STATUS.get(p.get("type", ""))
        if status is None:
            return "ignored_status"
        msg = self.repo.find_message(gs_id=p.get("gsId"), wamid=p.get("id"))
        if msg is None:
            return "unmapped_status"
        reason = None
        if status == MsgStatus.FAILED:
            reason = str((p.get("payload") or {}).get("reason", "failed"))
        changed = self.repo.advance_status(msg.id, status, wamid=p.get("id"), reason=reason)
        if changed and status == MsgStatus.FAILED:
            self._safe_comment(msg.lead_id, f"WhatsApp message failed: {reason}", f"wa-fail-{msg.id}")
        return f"status:{status.value}" if changed else "status_unchanged"

    def _handle_message(self, p: dict) -> str:
        inner = p.get("payload") or {}
        ctx = p.get("context") or {}
        sender = p.get("source", "")
        postback_code, postback_lead = parse_postback(inner.get("postbackText"))

        if p.get("type") == "quick_reply":
            code = postback_code or classify_text(inner.get("text", ""))
        elif p.get("type") == "text":
            code = classify_text(inner.get("text", ""))
        else:
            return "ignored_message_type"

        msg = self.repo.find_message(gs_id=ctx.get("gsId"), wamid=ctx.get("id"), lead_id=postback_lead)
        if msg is None and p.get("type") == "text":
            # Typed replies may have no context; use the latest open message to this sender.
            msg = self._latest_open_for(sender)
        if msg is None:
            return "unmapped_reply"
        # A valid token alone must not be able to qualify an unrelated lead.
        if msg.mobile != sender:
            log.warning("mobile mismatch for msg %s", msg.id)
            return "rejected_mobile_mismatch"
        if postback_lead and postback_lead != msg.lead_id:
            return "rejected_lead_mismatch"
        if code is None:
            return "unclassified_reply"

        cfg = self.repo.get_dealer(msg.dealer_id)
        decision = decide(code, cfg.qualify_max_days if cfg else 60, cfg.rep_name if cfg else "we")

        if decision.outcome == Outcome.OPT_OUT:
            self.repo.add_opt_out(msg.mobile, msg.dealer_id)

        if msg.status in (MsgStatus.EXPIRED, MsgStatus.FAILED):
            return "ignored_closed_conversation"
        if msg.status == MsgStatus.REPLIED and msg.l2r_sync_status == "DONE":
            # Lead already acted on; record the later answer as a comment only.
            self._safe_comment(msg.lead_id, f"Later WhatsApp reply: {inner.get('text', code)}",
                               f"wa-{p.get('id')}")
            return "comment_only"

        self.repo.record_reply(msg.id, code, decision.outcome)
        self.sync_to_l2r(msg.id, idempotency_key=f"wa-{p.get('id')}")

        if decision.reply_text and cfg:
            try:
                self.gupshup.send_text(cfg.gupshup_app_id, cfg.source_number, msg.mobile,
                                       decision.reply_text)
            except SendError:
                log.warning("follow-up text failed for msg %s", msg.id)
        return f"reply:{decision.outcome.value}"

    def _latest_open_for(self, mobile: str):
        row = self.repo._exec(
            "SELECT id FROM wa_message WHERE mobile = ? AND status IN ('SENT','DELIVERED','READ') "
            "ORDER BY id DESC LIMIT 1", (mobile,)
        ).fetchone()
        return self.repo.get_message(row["id"]) if row else None

    # ------------------------------------------------------------- L2R sync
    def sync_to_l2r(self, msg_id: int, idempotency_key: str | None = None) -> str:
        msg = self.repo.get_message(msg_id)
        if msg is None or msg.outcome is None:
            return "nothing_to_sync"
        key = idempotency_key or f"wa-msg-{msg.id}-{msg.outcome.value}"
        try:
            lead = self.l2r.get_lead(msg.lead_id)
            if lead is None or lead.closed:
                self.repo.set_sync(msg.id, "SKIPPED", "lead missing or closed")
                return "skipped"
            label = TIMELINE_LABEL.get(msg.reply_code or "", msg.reply_code)
            note = f"Customer answered '{label}' on WhatsApp" if msg.reply_code else \
                "No reply to WhatsApp qualifier within 24h"

            if lead.stage != "FIRST_CALL":
                # A human has already moved this lead on. Never downgrade.
                self.l2r.add_comment(lead.lead_id, note, key)
            elif msg.outcome == Outcome.QUALIFIED:
                cfg = self.repo.get_dealer(msg.dealer_id)
                decision = decide(msg.reply_code, cfg.qualify_max_days if cfg else 60, "")
                self.l2r.dispose(lead.lead_id, {
                    "callStatus": "CONNECTED",
                    "disposition": "WHATSAPP_BOT",
                    "targetStage": "QUALIFICATION",
                    "purchaseTimeline": label,
                    "priority": decision.priority,
                    "comment": note,
                    "actor": "system:whatsapp-bot",
                }, key)
            elif msg.outcome in (Outcome.NURTURE, Outcome.HANDOFF):
                decision = decide(msg.reply_code, 60, "")
                self.l2r.dispose(lead.lead_id, {
                    "callStatus": "CONNECTED",
                    "disposition": "WHATSAPP_BOT",
                    "targetStage": "FIRST_CALL",
                    "purchaseTimeline": label,
                    "followUpInDays": decision.follow_up_days,
                    "priority": decision.priority,
                    "comment": note,
                    "actor": "system:whatsapp-bot",
                }, key)
            else:  # NO_RESPONSE, OPT_OUT
                self.l2r.add_comment(lead.lead_id, note if msg.outcome == Outcome.NO_RESPONSE
                                     else "Customer opted out of WhatsApp messages", key)
        except Exception as e:
            self.repo.set_sync(msg.id, "FAILED", str(e)[:500])
            self.audit("ERROR", "l2r_sync_failed", msg.lead_id, msg.id, error=str(e)[:300])
            return "failed"
        self.repo.set_sync(msg.id, "DONE")
        self.audit("INFO", "l2r_synced", msg.lead_id, msg.id, outcome=msg.outcome.value,
                   reply_code=msg.reply_code)
        return "done"

    # ------------------------------------------------------------ scheduled
    def run_maintenance(self) -> dict:
        """Run every few minutes: expire 24h-old conversations, retry failed syncs."""
        expired = self.repo.expire_stale(self.window_hours)
        retried = [self.sync_to_l2r(m.id) for m in self.repo.pending_sync()]
        return {"expired": len(expired), "synced": retried.count("done"),
                "failed": retried.count("failed")}

    def _safe_comment(self, lead_id: str, text: str, key: str) -> None:
        try:
            self.l2r.add_comment(lead_id, text, key)
        except Exception:
            log.warning("could not add comment to lead %s", lead_id)
