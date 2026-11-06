"""Core flows: send on lead creation, process inbound Gupshup events, run the
qualification flow (timeline -> visit date -> visit time), sync to L2R."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .clients import GupshupClient, L2RClient, ListRow, SendError, TemplateSend
from .db import Repo
from .models import DealerConfig, Lead, MsgStatus, Outcome, WaMessage
from .rules import (
    FLOW, QUALIFYING, STEP_CONFIRM, STEP_TIMELINE, STEP_VISIT_DATE, STEP_VISIT_TIME, TIMELINE_LABEL,
    TIMELINE_DAYS, TIMELINE_OPTIONS, build_postback, classify_text, confirm_change_text, decide_timeline,
    ask_hi_text, dealer_assigned_text, gt2m_text, is_hi, kept_text, parse_postback, reengage_text,
    research_text, same_answer_text, updated_text, visit_confirm_text,
)
from .working_hours import DealerCalendar, day_label

log = logging.getLogger("qualifier")
audit_log = logging.getLogger("qualifier.audit")

EVENT_STATUS = {
    "enqueued": MsgStatus.SENT,
    "sent": MsgStatus.SENT,
    "delivered": MsgStatus.DELIVERED,
    "read": MsgStatus.READ,
    "failed": MsgStatus.FAILED,
}
# Inbound message types that carry a button/list choice.
CHOICE_TYPES = {"quick_reply", "button_reply", "list_reply"}


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


def template_variables(lead: Lead, cfg: DealerConfig) -> list[str]:
    """Template params in order: customer name, make, model, working hours,
    virtual number, sales executive, dealer name."""
    cal = DealerCalendar(cfg)
    first = next((w for d in cal.next_open_days(lead.created_at, 1) for w in cal.windows(d)), None)
    fmt = lambda d: d.strftime("%I:%M %p").lstrip("0")  # noqa: E731
    hours = f"{fmt(first[0])} - {fmt(first[1])}" if first else "working hours"
    return [lead.customer_name or "there", lead.make, lead.model_name, hours,
            cfg.rep_phone, cfg.rep_name, cfg.dealer_name]


class QualifierService:
    def __init__(self, repo: Repo, gupshup: GupshupClient, l2r: L2RClient,
                 max_event_age_seconds: int = 86400, session_window_hours: int = 24,
                 brand_name: str = "BikeWale", support_number: str = "918047104444"):
        self.repo = repo
        self.gupshup = gupshup
        self.l2r = l2r
        self.max_event_age = max_event_age_seconds
        self.window_hours = session_window_hours
        self.brand_name = brand_name
        self.support_number = support_number

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
        if not DealerCalendar(cfg).is_non_working(lead.created_at):
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

        req = TemplateSend(
            app_id=cfg.gupshup_app_id,
            source=cfg.source_number,                     # WABA number
            destination=lead.mobile,
            template_id=cfg.template_id,
            params=template_variables(lead, cfg),
            postbacks=[build_postback(STEP_TIMELINE, code, lead.lead_id) for code, _, _ in TIMELINE_OPTIONS],
        )
        try:
            gs_id = self.gupshup.send_template(req)
        except SendError as e:
            self.repo.mark_failed(msg_id, str(e))
            self._safe_comment(lead.lead_id, f"WhatsApp qualifier could not send message: {e}",
                               f"wa-sendfail-{msg_id}")
            return SendResult(False, "send_failed", msg_id)
        # Gupshup's messageId is unique per send (the template id is shared), so it is
        # written synchronously, before any reply can be processed.
        self.repo.mark_sent(msg_id, gs_id)
        return SendResult(True, "sent", msg_id)

    def _send_session(self, root: WaMessage, cfg: DealerConfig, step: str, kind: str, send) -> int | None:
        """Send a follow-up (text or list) in the same flow and track it like the template."""
        row_id = self.repo.create_pending(root.lead_id, root.dealer_id, root.mobile, None, step=step,
                                          msg_type=kind, root_template_id=root.root_template_id or root.template_id)
        try:
            gs_id = send()
        except SendError as e:
            self.repo.mark_failed(row_id, str(e))
            self.audit("ERROR", f"session_send_failed:{step}", root.lead_id, row_id, error=str(e)[:300])
            return None
        self.repo.mark_sent(row_id, gs_id)
        self.audit("INFO", f"session_sent:{step}", root.lead_id, row_id, gs_message_id=gs_id)
        return row_id

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
        """Synchronous part of the webhook: validate shape and store for dedupe."""
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
        p = evt.get("payload") or {}
        gs = (p.get("context") or {}).get("gsId") or p.get("gsId")
        msg = self.repo.find_message(gs_id=gs) if gs else None
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
        wamid = (p.get("payload") or {}).get("whatsappMessageId") or p.get("id")
        reason = str((p.get("payload") or {}).get("reason", "failed")) if status == MsgStatus.FAILED else None
        changed = self.repo.advance_status(msg.id, status, wamid=wamid, reason=reason)
        if changed and status == MsgStatus.FAILED and msg.msg_type == "template":
            self._safe_comment(msg.lead_id, f"WhatsApp message failed: {reason}", f"wa-fail-{msg.id}")
        return f"status:{status.value}" if changed else "status_unchanged"

    def _handle_message(self, p: dict) -> str:
        inner = p.get("payload") or {}
        ctx = p.get("context") or {}
        sender = p.get("source", "")
        mtype = p.get("type")
        if mtype not in CHOICE_TYPES and mtype != "text":
            return "ignored_message_type"

        if mtype == "text":
            return self._handle_text(p)

        pb = parse_postback(inner.get("postbackText") or inner.get("id"))
        msg = self.repo.find_message(gs_id=ctx.get("gsId"), wamid=ctx.get("id"),
                                     lead_id=pb.lead_id if pb else None)
        if msg is None:
            return "unmapped_reply"
        # A valid token alone must not be able to qualify an unrelated lead.
        if msg.mobile != sender:
            log.warning("mobile mismatch for msg %s", msg.id)
            return "rejected_mobile_mismatch"
        if pb and pb.lead_id and pb.lead_id != msg.lead_id:
            return "rejected_lead_mismatch"

        if not pb:
            return "unclassified_reply"
        step, option = pb.step, pb.option
        title = inner.get("title") or inner.get("text") or option
        if step != msg.step:
            return "rejected_step_mismatch"
        answered = msg   # the message the customer actually tapped
        if step == STEP_TIMELINE and msg.msg_type != "template":
            # Timeline asked again in a session message (after "Hi"): the answer belongs to the conversation's template.
            msg = self.repo.find_template(msg.lead_id) or msg

        cfg = self.repo.get_dealer(msg.dealer_id)
        if option == "STOP":
            self.repo.add_opt_out(msg.mobile, msg.dealer_id)
            self._safe_comment(msg.lead_id, "Customer opted out of WhatsApp messages", f"wa-{p.get('id')}")
            return "reply:OPT_OUT"
        if answered.status in (MsgStatus.EXPIRED, MsgStatus.FAILED):
            return "ignored_closed_conversation"

        self.repo.add_interaction({
            "inbound_id": p.get("id"), "context_message_id": msg.gs_message_id, "lead_id": msg.lead_id,
            "dealer_id": msg.dealer_id, "template_id": msg.root_template_id or msg.template_id,
            "flow": FLOW, "step": step, "option_code": option, "option_title": title,
            "received_at": datetime.now(timezone.utc).isoformat(),
        })

        key = f"wa-{p.get('id')}"
        if step == STEP_CONFIRM:
            if msg.status == MsgStatus.REPLIED:
                return "ignored_already_confirmed"
            self.repo.mark_replied(msg.id, option)
            return self._confirm_change(msg, cfg, option, key) if cfg else "ignored_no_dealer"
        if step == STEP_TIMELINE and msg.reply_code:
            # WhatsApp keeps buttons active: the customer changed (or repeated) the answer.
            return self._timeline_changed(msg, cfg, option, key) if cfg else "ignored_no_dealer"

        if step == STEP_TIMELINE:
            decision = decide_timeline(option, cfg.qualify_max_days if cfg else 60)
            outcome = decision.outcome
        elif step == STEP_VISIT_DATE:
            outcome = Outcome.DATE_CHOSEN
        else:
            outcome = Outcome.VISIT_BOOKED
        self.repo.record_reply(msg.id, option, outcome)
        self.sync_to_l2r(msg.id, idempotency_key=f"wa-{p.get('id')}")
        if cfg:
            self._next_step(msg, cfg, step, option)
        return f"reply:{outcome.value}"

    def _next_step(self, msg: WaMessage, cfg: DealerConfig, step: str, option: str) -> None:
        """Send the next message of the flow."""
        cal = DealerCalendar(cfg)
        lead_name = self._lead_name(msg.lead_id)
        send = self.gupshup
        app, src, dst = cfg.gupshup_app_id, cfg.source_number, msg.mobile
        if step == STEP_TIMELINE:
            if option in ("15D", "1M", "1_2M") and decide_timeline(option, cfg.qualify_max_days).ask_visit:
                days = cal.next_open_days(datetime.now(timezone.utc))
                rows = [ListRow(day_label(d), build_postback(STEP_VISIT_DATE, d.isoformat(), msg.lead_id)) for d in days]
                self._send_session(msg, cfg, STEP_VISIT_DATE, "list", lambda: send.send_list(
                    app, src, dst, "Plan your visit",
                    f"Great! When would you like to visit {cfg.dealer_name}?",
                    "Choose date", "Choose your preferred date", rows))
            elif option == "GT2M":
                self._send_session(msg, cfg, "GT2M_ACK", "text",
                                   lambda: send.send_text(app, src, dst, gt2m_text(lead_name, cfg.rep_phone)))
            elif option == "RESEARCH":
                self._send_session(msg, cfg, "RESEARCH_ACK", "text",
                                   lambda: send.send_text(app, src, dst, research_text(cfg.rep_phone)))
        elif step == STEP_VISIT_DATE:
            day = date.fromisoformat(option)
            rows = [ListRow(label, build_postback(STEP_VISIT_TIME, code, msg.lead_id)) for code, label in cal.hourly_slots(day)]
            self._send_session(msg, cfg, STEP_VISIT_TIME, "list", lambda: send.send_list(
                app, src, dst, day_label(day), f"{day_label(day)} it is. What time suits you?",
                "Choose time", "Choose your preferred time", rows))
        elif step == STEP_VISIT_TIME:
            chosen = self.repo.latest_interaction(msg.lead_id, STEP_VISIT_DATE)
            day = date.fromisoformat(chosen["option_code"]) if chosen else None
            slot = dict(cal.hourly_slots(day)).get(option, option) if day else option
            self._send_session(msg, cfg, "VISIT_CONFIRM", "text", lambda: send.send_text(
                app, src, dst, visit_confirm_text(cfg.dealer_name, cfg.city, day_label(day) if day else "",
                                                  slot, cfg.rep_name, cfg.rep_phone)))

    # ---- typed messages ---------------------------------------------------
    def _handle_text(self, p: dict) -> str:
        """Customer typed instead of tapping:
        anything but "Hi" -> ask them to type Hi;
        "Hi" + qualified lead -> share the assigned dealer's number and the brand's number;
        "Hi" + not qualified -> ask the buying-timeline question again with the same buttons."""
        sender, text = p.get("source", ""), (p.get("payload") or {}).get("text", "")
        tpl = self.repo.latest_template_for(sender)
        if tpl is None:
            return "unmapped_reply"
        cfg = self.repo.get_dealer(tpl.dealer_id)
        if cfg is None:
            return "ignored_no_dealer"
        if classify_text(text) == "STOP":
            self.repo.add_opt_out(tpl.mobile, tpl.dealer_id)
            self._safe_comment(tpl.lead_id, "Customer opted out of WhatsApp messages", f"wa-{p.get('id')}")
            return "reply:OPT_OUT"
        hi = is_hi(text)
        self.repo.add_interaction({
            "inbound_id": p.get("id"), "context_message_id": None, "lead_id": tpl.lead_id, "dealer_id": tpl.dealer_id,
            "template_id": tpl.template_id, "flow": FLOW, "step": "FREE_TEXT", "option_code": "HI" if hi else "OTHER",
            "option_title": text[:200], "received_at": datetime.now(timezone.utc).isoformat()})
        if not hi:
            self._text(tpl, cfg, "ASK_HI", ask_hi_text())
            return "reply:ASK_HI"
        try:
            lead = self.l2r.get_lead(tpl.lead_id)
        except Exception:
            lead = None
        qualified = lead is not None and lead.stage in ("QUALIFICATION", "VISITS", "BOOKING")
        if qualified:
            self._text(tpl, cfg, "DEALER_INFO",
                       dealer_assigned_text(cfg.dealer_name, cfg.rep_phone, self.brand_name, self.support_number))
            return "reply:DEALER_INFO"
        vehicle = f"{lead.make} {lead.model_name}".strip() if lead else "vehicle"
        buttons = [ListRow(label, build_postback(STEP_TIMELINE, code, tpl.lead_id)) for code, label, _ in TIMELINE_OPTIONS]
        self._send_session(tpl, cfg, STEP_TIMELINE, "buttons", lambda: self.gupshup.send_buttons(
            cfg.gupshup_app_id, cfg.source_number, tpl.mobile, reengage_text(self.brand_name, vehicle), buttons))
        return "reply:TIMELINE_ASKED_AGAIN"

    # ---- changed answers --------------------------------------------------
    def _span_call(self, cfg: DealerConfig, option: str, lead_id: str) -> datetime:
        """Next call: 7 days before the buying span ends, or 1 day after a booked visit,
        whichever is earlier, at the dealer's opening time on an open day."""
        cal, now = DealerCalendar(cfg), datetime.now(timezone.utc)
        candidates = [now + timedelta(days=max(TIMELINE_DAYS[option] - 7, 1))]
        visit = self.repo.latest_interaction(lead_id, STEP_VISIT_DATE)
        if visit:
            candidates.append(datetime.combine(date.fromisoformat(visit["option_code"]) + timedelta(days=1),
                                               datetime.min.time(), cal.tz))
        day = cal.next_open_days(min(candidates).replace(hour=0, minute=0), 1)
        wins = cal.windows(day[0]) if day else []
        return wins[0][0] if wins else min(candidates)

    def _text(self, root: WaMessage, cfg: DealerConfig, step: str, text: str) -> None:
        self._send_session(root, cfg, step, "text",
                           lambda: self.gupshup.send_text(cfg.gupshup_app_id, cfg.source_number, root.mobile, text))

    def _l2r(self, fn, lead_id: str, body: dict, key: str) -> None:
        try:
            fn(lead_id, body, key)
        except Exception as e:
            self.audit("ERROR", "l2r_call_failed", lead_id, error=str(e)[:300], body=body)

    def _timeline_changed(self, tpl: WaMessage, cfg: DealerConfig, option: str, key: str) -> str:
        """Rules keyed by the answer the customer already gave:
        earlier 15D/1M/1-2M -> another of these: thank-you confirming the update
        earlier 15D/1M/1-2M -> GT2M / RESEARCH: lead stays qualified (never disqualified), answer noted
        earlier GT2M / RESEARCH -> 15D/1M/1-2M: qualify straight away (form tagged in L2R), visit date next
        earlier GT2M -> RESEARCH: ask for confirmation first, then close the lead
        earlier RESEARCH (lead closed) -> GT2M: reopen the lead, next call by the new span, thanks
        """
        prev = tpl.reply_code
        prev_label, new_label = TIMELINE_LABEL[prev], TIMELINE_LABEL[option]
        if option == prev:
            self._text(tpl, cfg, "CHANGE_ACK", same_answer_text(new_label))
            return "reply:SAME_ANSWER"
        try:
            l2r_lead = self.l2r.get_lead(tpl.lead_id)
        except Exception:
            l2r_lead = None
        is_qualified = bool(l2r_lead and l2r_lead.stage in ("QUALIFICATION", "VISITS", "BOOKING"))
        # A qualified lead is never disqualified, whatever its last answer was.
        stays_qualified = prev in QUALIFYING or (is_qualified and option not in QUALIFYING)
        if stays_qualified or (prev == "GT2M" and option == "RESEARCH"):
            if option in QUALIFYING and prev in QUALIFYING:
                d = decide_timeline(option, cfg.qualify_max_days)
                self.repo.set_answer(tpl.id, option, d.outcome)
                self._l2r(self.l2r.dispose, tpl.lead_id, {
                    "callStatus": "CONNECTED", "disposition": "WHATSAPP_BOT", "purchaseTimeline": option,
                    "priority": d.priority, "comment": f"Customer changed the buying timeline from '{prev_label}' to '{new_label}' on WhatsApp",
                    "actor": "system:whatsapp-bot"}, key)
                self._text(tpl, cfg, "CHANGE_ACK", updated_text(new_label))
                return "reply:TIMELINE_UPDATED"
            if stays_qualified:
                # A qualified lead is never disqualified: it stays qualified, the new answer is noted,
                # and for More than 2 Months the next call follows the new buying span.
                self.repo.set_answer(tpl.id, option, Outcome.QUALIFIED)
                reason = f"Customer changed the buying timeline from '{prev_label}' to '{new_label}' on WhatsApp (lead stays qualified)"
                if option == "GT2M":
                    self._l2r(self.l2r.schedule_call, tpl.lead_id, {
                        "dueAt": self._span_call(cfg, option, tpl.lead_id).isoformat(), "reopen": False,
                        "purchaseTimeline": option, "reason": reason, "actor": "system:whatsapp-bot"}, key)
                else:
                    self._safe_comment(tpl.lead_id, reason, key)
                self._text(tpl, cfg, "CHANGE_ACK", updated_text(new_label))
                return "reply:NOTED_STAYS_QUALIFIED"
            # More than 2 Months -> Still Researching: confirm first, then close
            buttons = [ListRow("Yes, change it", build_postback(STEP_CONFIRM, f"YES:{option}", tpl.lead_id)),
                       ListRow("No, keep earlier answer", build_postback(STEP_CONFIRM, "NO", tpl.lead_id))]
            self._send_session(tpl, cfg, STEP_CONFIRM, "buttons", lambda: self.gupshup.send_buttons(
                cfg.gupshup_app_id, cfg.source_number, tpl.mobile, confirm_change_text(prev_label, new_label), buttons))
            return "reply:CONFIRMATION_ASKED"
        reopen = prev == "RESEARCH"
        if option in QUALIFYING:
            # Qualify straight away instead of waiting for a call: tag the follow-up form in L2R
            # (L2R disposes the call, schedules the next one and sends the lead to the dealer panel),
            # then continue to the visit date question.
            d = decide_timeline(option, cfg.qualify_max_days)
            self.repo.set_answer(tpl.id, option, d.outcome)
            self._l2r(self.l2r.dispose, tpl.lead_id, {
                "callStatus": "CONNECTED", "disposition": "WHATSAPP_BOT",
                "followUpStatus": "SPOKE_TO_CUSTOMER", "leadStatus": "INTERESTED",
                "expectedBuyingSpan": option, "eagerness": "HOT" if d.priority == "P1" else "WARM",
                "targetStage": "QUALIFICATION", "purchaseTimeline": option, "priority": d.priority,
                "reopen": reopen, "comment": f"Customer changed the answer from '{prev_label}' to '{new_label}' on WhatsApp",
                "actor": "system:whatsapp-bot"}, key)
            self._text(tpl, cfg, "CHANGE_ACK", updated_text(new_label))
            if not self.repo.latest_interaction(tpl.lead_id, STEP_VISIT_TIME):
                self._next_step(tpl, cfg, STEP_TIMELINE, option)
            return "reply:QUALIFIED_ON_CHANGE"
        self.repo.set_answer(tpl.id, option, decide_timeline(option, cfg.qualify_max_days).outcome)
        reason = f"Customer changed the answer from '{prev_label}' to '{new_label}' on WhatsApp"
        # Only remaining case: Still Researching (closed) -> More than 2 Months: reopen, span-based call.
        self._l2r(self.l2r.schedule_call, tpl.lead_id, {
            "dueAt": self._span_call(cfg, option, tpl.lead_id).isoformat(), "reopen": reopen,
            "purchaseTimeline": option, "reason": reason, "actor": "system:whatsapp-bot"}, key)
        self._text(tpl, cfg, "CHANGE_ACK", updated_text(new_label))
        return "reply:REOPENED" if reopen else "reply:TIMELINE_CHANGED"

    def _confirm_change(self, confirm: WaMessage, cfg: DealerConfig, option: str, key: str) -> str:
        tpl = self.repo.find_template(confirm.lead_id)
        if option == "NO" or tpl is None:
            self._text(confirm, cfg, "CHANGE_ACK", kept_text())
            return "reply:KEPT"
        target = option.split(":", 1)[1]
        d = decide_timeline(target, cfg.qualify_max_days)
        self.repo.set_answer(tpl.id, target, d.outcome)
        note = f"Customer confirmed changing the buying timeline to '{TIMELINE_LABEL[target]}' on WhatsApp"
        if target == "GT2M":
            self._l2r(self.l2r.dispose, tpl.lead_id, {
                "callStatus": "CONNECTED", "disposition": "WHATSAPP_BOT", "leadStatus": "NOT_QUALIFIED_OPEN",
                "targetStage": "FIRST_CALL", "purchaseTimeline": target, "followUpInDays": d.follow_up_days,
                "comment": note, "actor": "system:whatsapp-bot"}, key)
            self._text(tpl, cfg, "GT2M_ACK", gt2m_text(self._lead_name(tpl.lead_id), cfg.rep_phone))
        else:
            self._l2r(self.l2r.dispose, tpl.lead_id, {
                "callStatus": "CONNECTED", "disposition": "WHATSAPP_BOT", "followUpStatus": "SPOKE_TO_CUSTOMER",
                "leadStatus": "NOT_INTERESTED", "reason": d.reason, "subReason": d.sub_reason, "closeLead": True,
                "comment": note, "actor": "system:whatsapp-bot"}, key)
            self._text(tpl, cfg, "RESEARCH_ACK", research_text(cfg.rep_phone))
        return f"reply:CHANGED_TO_{target}"

    def _lead_name(self, lead_id: str) -> str:
        try:
            lead = self.l2r.get_lead(lead_id)
            return (lead.customer_name if lead else "") or "there"
        except Exception:
            return "there"

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
            cfg = self.repo.get_dealer(msg.dealer_id)

            if msg.outcome == Outcome.DATE_CHOSEN:
                pass  # nothing goes to L2R until the customer has also picked a time
            elif msg.outcome == Outcome.VISIT_BOOKED:
                # Date and time are both known: create the Showroom Visit task against the lead.
                chosen = self.repo.latest_interaction(msg.lead_id, STEP_VISIT_DATE)
                self.l2r.create_task(lead.lead_id, {
                    "type": "SHOWROOM_VISIT",
                    "date": chosen["option_code"] if chosen else None,
                    "slotStart": msg.reply_code,
                    "dealerId": msg.dealer_id,
                    "salesExecutive": cfg.rep_name if cfg else None,
                    "createdBy": "WHATSAPP",
                    "actor": "system:whatsapp-bot",
                }, key)
            else:
                label = TIMELINE_LABEL.get(msg.reply_code or "", msg.reply_code)
                note = f"Customer answered '{label}' on WhatsApp" if msg.reply_code else \
                    "No reply to WhatsApp qualifier within 24h"
                if lead.stage != "FIRST_CALL":
                    # A human has already moved this lead on. Never downgrade.
                    self.l2r.add_comment(lead.lead_id, note, key)
                elif msg.outcome == Outcome.QUALIFIED:
                    d = decide_timeline(msg.reply_code, cfg.qualify_max_days if cfg else 60)
                    # Same fields a telecaller fills in the follow-up form. L2R treats it like any
                    # other tag: the call is disposed, and L2R's "lead qualified" event sends the
                    # lead to the dealer panel. The bot never calls the dealer panel itself.
                    self.l2r.dispose(lead.lead_id, {
                        "callStatus": "CONNECTED", "disposition": "WHATSAPP_BOT",
                        "followUpStatus": "SPOKE_TO_CUSTOMER", "leadStatus": "INTERESTED",
                        "expectedBuyingSpan": msg.reply_code, "eagerness": "HOT" if d.priority == "P1" else "WARM",
                        "targetStage": "QUALIFICATION", "purchaseTimeline": msg.reply_code,
                        "priority": d.priority, "comment": note, "actor": "system:whatsapp-bot",
                    }, key)
                elif msg.outcome == Outcome.NOT_INTERESTED:
                    d = decide_timeline(msg.reply_code, cfg.qualify_max_days if cfg else 60)
                    self.l2r.dispose(lead.lead_id, {
                        "callStatus": "CONNECTED", "disposition": "WHATSAPP_BOT",
                        "followUpStatus": "SPOKE_TO_CUSTOMER", "leadStatus": "NOT_INTERESTED",
                        "reason": d.reason, "subReason": d.sub_reason, "closeLead": True,
                        "comment": note, "actor": "system:whatsapp-bot",
                    }, key)
                elif msg.outcome == Outcome.NURTURE:
                    d = decide_timeline(msg.reply_code, cfg.qualify_max_days if cfg else 60)
                    self.l2r.dispose(lead.lead_id, {
                        "callStatus": "CONNECTED", "disposition": "WHATSAPP_BOT",
                        "targetStage": "FIRST_CALL", "purchaseTimeline": msg.reply_code,
                        "followUpInDays": d.follow_up_days, "comment": note, "actor": "system:whatsapp-bot",
                    }, key)
                else:  # NO_RESPONSE
                    self.l2r.add_comment(lead.lead_id, note, key)
        except Exception as e:
            self.repo.set_sync(msg.id, "FAILED", str(e)[:500])
            self.audit("ERROR", "l2r_sync_failed", msg.lead_id, msg.id, error=str(e)[:300])
            return "failed"
        self.repo.set_sync(msg.id, "DONE")
        self.audit("INFO", "l2r_synced", msg.lead_id, msg.id, outcome=msg.outcome.value,
                   reply_code=msg.reply_code, step=msg.step)
        return "done"

    # ------------------------------------------------------------ scheduled
    def run_maintenance(self) -> dict:
        """Run every few minutes: expire 24h-old templates, retry failed syncs."""
        expired = self.repo.expire_stale(self.window_hours)
        retried = [self.sync_to_l2r(m.id) for m in self.repo.pending_sync()]
        return {"expired": len(expired), "synced": retried.count("done"),
                "failed": retried.count("failed")}

    def _safe_comment(self, lead_id: str, text: str, key: str) -> None:
        try:
            self.l2r.add_comment(lead_id, text, key)
        except Exception:
            log.warning("could not add comment to lead %s", lead_id)
