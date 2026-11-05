from datetime import datetime

from app.clients import SendError
from app.models import MsgStatus

from .conftest import IST, TOKEN, dealer, lead, quick_reply

H = {"X-Webhook-Token": TOKEN}


def send(env, **kw):
    ld = lead(**kw)
    env.l2r.leads[ld.lead_id] = ld
    return env.svc.on_lead_created(ld)


# ---------------------------------------------------------------- sending
def test_nwh_lead_gets_template_with_postbacks(env):
    r = send(env)
    assert r.sent
    req = env.gs.templates[0]
    assert req.destination == "919876543210"
    assert req.params[:3] == ["Prakhar", "Maruti Suzuki Brezza", "Vishnu Cars TN"]
    assert req.params[3:7] == ["Sathya", "today", "9:30 AM", "5:30 PM"]
    assert req.postbacks[1] == "TIMELINE_1M|L1"
    assert env.repo.get_message(r.message_id).gs_message_id == "gs-1"


def test_working_hours_lead_is_skipped(env):
    r = send(env, created=datetime(2026, 11, 6, 11, 0, tzinfo=IST))
    assert (r.sent, r.reason) == (False, "working_hours")


def test_no_consent_opt_out_and_duplicate_are_skipped(env):
    assert send(env, whatsapp_consent=False).reason == "no_consent"
    assert send(env).sent
    assert send(env).reason == "recently_messaged"  # same mobile+dealer within 24h
    env.repo.add_opt_out("919000000000", "D1")
    assert send(env, lead_id="L9", mobile="919000000000").reason == "opted_out"


def test_disabled_dealer(env):
    env.repo.upsert_dealer(dealer(enabled=False))
    assert send(env).reason == "dealer_not_enabled"


def test_holdout_leads_not_messaged(env):
    env.repo.upsert_dealer(dealer(holdout_pct=100))
    assert send(env).reason == "holdout"


def test_send_failure_is_recorded_and_commented(env):
    env.gs.fail = SendError("400: invalid destination", retryable=False)
    r = send(env)
    assert r.reason == "send_failed"
    assert env.repo.get_message(r.message_id).status == MsgStatus.FAILED
    assert env.l2r.comments


# ---------------------------------------------------------------- webhook
def test_webhook_rejects_bad_token(env):
    res = env.client.post("/v1/webhooks/gupshup", json={}, headers={"X-Webhook-Token": "nope"})
    assert res.status_code == 401
    res = env.client.post("/v1/webhooks/gupshup", json={})
    assert res.status_code == 401


def test_rotated_token_still_accepted(env):
    res = env.client.post("/v1/webhooks/gupshup", json={"type": "user-event"},
                          headers={"X-Webhook-Token": "old-token"})
    assert res.status_code == 200


def test_one_month_reply_qualifies_lead(env):
    send(env)
    res = env.client.post("/v1/webhooks/gupshup", json=quick_reply("TIMELINE_1M", "L1", "gs-1"), headers=H)
    assert res.status_code == 200 and res.json()["accepted"]
    lead_id, body, key = env.l2r.dispositions[0]
    assert lead_id == "L1"
    assert body["targetStage"] == "QUALIFICATION"
    assert body["disposition"] == "WHATSAPP_BOT"
    assert body["priority"] == "P1"
    assert key == "wa-wamid.in1"
    assert env.l2r.leads["L1"].stage == "QUALIFICATION"
    assert "Sathya" in env.gs.texts[0][1]


def test_more_than_two_months_is_nurtured(env):
    send(env)
    env.client.post("/v1/webhooks/gupshup",
                    json=quick_reply("TIMELINE_GT2M", "L1", "gs-1", text="More than 2 Months"), headers=H)
    body = env.l2r.dispositions[0][1]
    assert body["targetStage"] == "FIRST_CALL"
    assert body["followUpInDays"] == 60
    assert env.l2r.leads["L1"].stage == "FIRST_CALL"


def test_duplicate_webhook_is_ignored(env):
    send(env)
    evt = quick_reply("TIMELINE_1M", "L1", "gs-1")
    env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)
    res = env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)
    assert res.json() == {"accepted": False, "reason": "duplicate"}
    assert len(env.l2r.dispositions) == 1


def test_stale_event_rejected(env):
    send(env)
    evt = quick_reply("TIMELINE_1M", "L1", "gs-1")
    evt["timestamp"] = 1_000_000_000_000  # 2001
    assert env.client.post("/v1/webhooks/gupshup", json=evt, headers=H).json()["reason"] == "stale"


def test_mobile_mismatch_cannot_qualify(env):
    send(env)
    evt = quick_reply("TIMELINE_1M", "L1", "gs-1", mobile="911111111111")
    env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)
    assert env.l2r.dispositions == []
    assert env.repo.event_result("wamid.in1") == "rejected_mobile_mismatch"


def test_reply_to_unknown_template_is_not_mapped(env):
    evt = quick_reply("TIMELINE_1M", "L404", "gs-unknown")
    env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)
    assert env.repo.event_result("wamid.in1") == "unmapped_reply"


def test_never_downgrades_lead_already_worked(env):
    send(env)
    env.l2r.leads["L1"].stage = "VISITS"  # sales exec already moved it on
    env.client.post("/v1/webhooks/gupshup", json=quick_reply("TIMELINE_GT2M", "L1", "gs-1"), headers=H)
    assert env.l2r.dispositions == []
    assert "GT_2_MONTHS" in env.l2r.comments[-1][1]


def test_second_tap_after_sync_is_comment_only(env):
    send(env)
    env.client.post("/v1/webhooks/gupshup", json=quick_reply("TIMELINE_1M", "L1", "gs-1"), headers=H)
    env.client.post("/v1/webhooks/gupshup",
                    json=quick_reply("RESEARCHING", "L1", "gs-1", msg_id="wamid.in2"), headers=H)
    assert len(env.l2r.dispositions) == 1
    assert env.repo.event_result("wamid.in2") == "comment_only"


def test_typed_reply_is_classified(env):
    send(env)
    evt = {
        "type": "message", "timestamp": quick_reply("X", "L1", "gs-1")["timestamp"],
        "payload": {"id": "wamid.t1", "source": "919876543210", "type": "text",
                    "payload": {"text": "within 15 days"}},
    }
    env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)
    assert env.l2r.dispositions[0][1]["targetStage"] == "QUALIFICATION"


def test_stop_opts_out(env):
    send(env)
    evt = {
        "type": "message", "timestamp": quick_reply("X", "L1", "gs-1")["timestamp"],
        "payload": {"id": "wamid.s1", "source": "919876543210", "type": "text",
                    "payload": {"text": "STOP"}},
    }
    env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)
    assert env.repo.is_opted_out("919876543210", "D1")


def test_status_events_only_move_forward(env):
    r = send(env)

    def status(t):
        return {"type": "message-event", "payload": {"id": "wamid.out1", "gsId": "gs-1", "type": t}}

    env.client.post("/v1/webhooks/gupshup", json=status("read"), headers=H)
    env.client.post("/v1/webhooks/gupshup", json=status("delivered"), headers=H)  # late, out of order
    m = env.repo.get_message(r.message_id)
    assert m.status == MsgStatus.READ
    assert m.wamid == "wamid.out1"


def test_l2r_outage_is_retried_by_maintenance(env):
    send(env)
    env.l2r.down = True
    env.client.post("/v1/webhooks/gupshup", json=quick_reply("TIMELINE_1M", "L1", "gs-1"), headers=H)
    assert env.l2r.dispositions == []
    env.l2r.down = False
    assert env.svc.run_maintenance()["synced"] == 1
    assert env.l2r.dispositions[0][1]["targetStage"] == "QUALIFICATION"


def test_expiry_marks_no_response(env):
    r = send(env)
    env.repo._exec("UPDATE wa_message SET sent_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
                   (r.message_id,))
    out = env.svc.run_maintenance()
    assert out["expired"] == 1
    assert env.repo.get_message(r.message_id).status == MsgStatus.EXPIRED
    assert "No reply" in env.l2r.comments[-1][1]


def test_lead_created_endpoint(env):
    env.l2r.leads["L7"] = lead(lead_id="L7")
    res = env.client.post("/internal/leads/created", json={
        "leadId": "L7", "dealerId": "D1", "mobile": "919812345678", "customerName": "Prakhar",
        "modelName": "Tata Harrier EV", "createdAt": "2026-11-06T00:26:00+05:30",
    })
    assert res.json()["sent"] is True
