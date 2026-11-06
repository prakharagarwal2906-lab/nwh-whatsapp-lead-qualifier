from datetime import datetime

from app.clients import SendError
from app.models import MsgStatus

from .conftest import IST, TOKEN, dealer, lead, list_reply, quick_reply

H = {"X-Webhook-Token": TOKEN}


def send(env, **kw):
    ld = lead(**kw)
    env.l2r.leads[ld.lead_id] = ld
    return env.svc.on_lead_created(ld)


def post(env, evt):
    return env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)


def gs_of(env, step):
    """Gupshup messageId of the latest outbound message for a step."""
    row = env.repo._exec("SELECT gs_message_id FROM wa_message WHERE step = ? ORDER BY id DESC LIMIT 1",
                         (step,)).fetchone()
    return row["gs_message_id"]


# ---------------------------------------------------------------- sending
def test_template_has_six_inputs_and_flow_postbacks(env):
    r = send(env)
    assert r.sent
    req = env.gs.templates[0]
    assert req.template_id == "tpl-nwh-1"             # template id
    assert req.destination == "919876543210"          # customer mobile
    assert req.source == "919403897047"               # WABA number
    # customer name, make, model, working hours, virtual number, sales executive, dealer name
    assert req.params == ["Prakhar", "Royal Enfield", "Classic 350", "9:30 AM - 5:30 PM",
                          "919403897047", "Sathya", "Vishnu Motors TN"]
    assert req.postbacks[1] == "NWH_QUALIFIER|TIMELINE|1M|L1"   # lead id travels with every tap
    msg = env.repo.get_message(r.message_id)
    assert msg.gs_message_id == "gs-1" and msg.step == "TIMELINE" and msg.msg_type == "template"


def test_working_hours_lead_is_skipped(env):
    r = send(env, created=datetime(2026, 11, 6, 11, 0, tzinfo=IST))
    assert (r.sent, r.reason) == (False, "working_hours")


def test_no_consent_opt_out_and_duplicate_are_skipped(env):
    assert send(env, whatsapp_consent=False).reason == "no_consent"
    assert send(env).sent
    assert send(env).reason == "recently_messaged"
    env.repo.add_opt_out("919000000000", "D1")
    assert send(env, lead_id="L9", mobile="919000000000").reason == "opted_out"


def test_disabled_dealer_and_holdout(env):
    env.repo.upsert_dealer(dealer(holdout_pct=100))
    assert send(env).reason == "holdout"
    env.repo.upsert_dealer(dealer(enabled=False))
    assert send(env, lead_id="L2", mobile="919000000001").reason == "dealer_not_enabled"


def test_send_failure_is_recorded_and_commented(env):
    env.gs.fail = SendError("400: invalid destination", retryable=False)
    r = send(env)
    assert r.reason == "send_failed"
    assert env.repo.get_message(r.message_id).status == MsgStatus.FAILED
    assert env.l2r.comments


# ---------------------------------------------------------------- webhook auth
def test_webhook_rejects_bad_token(env):
    assert env.client.post("/v1/webhooks/gupshup", json={}, headers={"X-Webhook-Token": "nope"}).status_code == 401
    assert env.client.post("/v1/webhooks/gupshup", json={}).status_code == 401


def test_rotated_token_still_accepted(env):
    res = env.client.post("/v1/webhooks/gupshup", json={"type": "user-event"},
                          headers={"X-Webhook-Token": "old-token"})
    assert res.status_code == 200


# ---------------------------------------------------------------- full visit flow
def test_one_month_qualifies_then_books_a_visit(env):
    send(env)
    assert post(env, quick_reply("1M", "L1", "gs-1")).json()["accepted"]
    body = env.l2r.dispositions[0][1]
    assert body["targetStage"] == "QUALIFICATION" and body["priority"] == "P1"
    assert env.l2r.leads["L1"].stage == "QUALIFICATION"

    date_list = env.gs.lists[0]
    assert date_list["section"] == "Choose your preferred date"
    assert len(date_list["rows"]) == 7
    first = date_list["rows"][0].postback
    assert first.startswith("NWH_QUALIFIER|VISIT_DATE|") and first.endswith("|L1")
    picked_day = env.gs.lists[0]["rows"][1]
    day_iso = picked_day.postback.split("|")[2]

    post(env, list_reply("VISIT_DATE", day_iso, picked_day.title, "L1", gs_of(env, "VISIT_DATE"), msg_id="wamid.in2"))
    assert "Preferred visit date" in env.l2r.comments[-1][1]
    time_list = env.gs.lists[1]
    assert time_list["section"] == "Choose your preferred time"
    assert time_list["rows"][0].title == "9:30 AM - 10:30 AM"

    slot = time_list["rows"][2]
    post(env, list_reply("VISIT_TIME", slot.postback.split("|")[2], slot.title, "L1",
                         gs_of(env, "VISIT_TIME"), msg_id="wamid.in3"))
    lead_id, visit, _ = env.l2r.visits[0]
    assert lead_id == "L1" and visit["date"] == day_iso and visit["slotStart"] == "1130" and visit["assignee"] == "Sathya"
    assert "Your visit is booked" in env.gs.texts[-1][1] and slot.title in env.gs.texts[-1][1]

    # every tap is tracked against the template that started the flow
    rows = env.client.get("/internal/db/wa_interaction").json()["rows"]
    assert sorted(r["step"] for r in rows) == ["TIMELINE", "VISIT_DATE", "VISIT_TIME"]
    assert {r["template_id"] for r in rows} == {"tpl-nwh-1"}
    assert {r["context_message_id"] for r in rows} == {"gs-1", gs_of(env, "VISIT_DATE"), gs_of(env, "VISIT_TIME")}


def test_more_than_two_months_gets_check_in_message(env):
    send(env)
    post(env, quick_reply("GT2M", "L1", "gs-1", text="More than 2 Months"))
    body = env.l2r.dispositions[0][1]
    assert body["targetStage"] == "FIRST_CALL" and body["followUpInDays"] == 60
    assert env.gs.texts[-1][1] == ("Thanks, Prakhar!\nWe'll check in closer to your timeline.\n"
                                   "Need help before that? Call us anytime at 919403897047.")
    assert env.gs.lists == []


def test_still_researching_closes_lead_and_gets_help_message(env):
    send(env)
    post(env, quick_reply("RESEARCH", "L1", "gs-1", text="Still Researching"))
    body = env.l2r.dispositions[0][1]
    assert body["leadStatus"] == "NOT_INTERESTED" and body["closeLead"] is True
    assert (body["reason"], body["subReason"]) == ("Not interested in buying", "Just researching")
    assert env.gs.texts[-1][1] == "Please feel free to reach us at 919403897047 for any queries you might have."


def test_date_postback_against_template_is_rejected(env):
    send(env)
    post(env, list_reply("VISIT_DATE", "2026-11-07", "Sat, 7 Nov", "L1", "gs-1"))
    assert env.repo.event_result("wamid.in2") == "rejected_step_mismatch"


# ---------------------------------------------------------------- safety
def test_duplicate_webhook_is_ignored(env):
    send(env)
    evt = quick_reply("1M", "L1", "gs-1")
    post(env, evt)
    assert post(env, evt).json() == {"accepted": False, "reason": "duplicate"}
    assert len(env.l2r.dispositions) == 1 and len(env.gs.lists) == 1


def test_stale_event_rejected(env):
    send(env)
    evt = quick_reply("1M", "L1", "gs-1")
    evt["timestamp"] = 1_000_000_000_000
    assert post(env, evt).json()["reason"] == "stale"


def test_mobile_mismatch_cannot_qualify(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1", mobile="911111111111"))
    assert env.l2r.dispositions == []
    assert env.repo.event_result("wamid.in1") == "rejected_mobile_mismatch"


def test_reply_to_unknown_message_is_not_mapped(env):
    post(env, quick_reply("1M", "L404", "gs-unknown"))
    assert env.repo.event_result("wamid.in1") == "unmapped_reply"


def test_never_downgrades_lead_already_worked(env):
    send(env)
    env.l2r.leads["L1"].stage = "VISITS"
    post(env, quick_reply("GT2M", "L1", "gs-1"))
    assert env.l2r.dispositions == []
    assert "More than 2 Months" in env.l2r.comments[-1][1]


# ---------------------------------------------------------------- changed answers
def confirm(env, answer, lead_id="L1", msg_id="wamid.c1"):
    """Customer taps Yes / No on the 'change your answer?' message."""
    btn = env.gs.buttons[-1]["buttons"][0 if answer == "YES" else 1]
    evt = quick_reply("1M", lead_id, gs_of(env, "CONFIRM_CHANGE"), msg_id=msg_id, text=btn.title)
    evt["payload"]["payload"]["postbackText"] = btn.postback
    return post(env, evt)


def test_qualified_to_qualified_sends_thank_you(env):
    send(env)
    post(env, quick_reply("15D", "L1", "gs-1"))
    post(env, quick_reply("1_2M", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:TIMELINE_UPDATED"
    assert env.l2r.dispositions[-1][1]["purchaseTimeline"] == "1_2M"
    assert env.l2r.dispositions[-1][1]["priority"] == "P2"
    assert env.gs.texts[-1][1] == "Thank you for confirming! We've updated your buying timeline to 1 - 2 Months."
    assert env.repo.find_template("L1").reply_code == "1_2M"


def test_qualified_to_research_asks_first_then_closes(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    n = len(env.l2r.dispositions)
    post(env, quick_reply("RESEARCH", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:CONFIRMATION_ASKED"
    assert env.gs.buttons[-1]["body"] == 'You earlier told us "1 Month". Do you want to change it to "Still Researching"?'
    assert len(env.l2r.dispositions) == n                     # nothing changes until confirmed
    confirm(env, "YES")
    body = env.l2r.dispositions[-1][1]
    assert body["closeLead"] is True and body["subReason"] == "Just researching"
    assert "Please feel free to reach us" in env.gs.texts[-1][1]
    assert env.repo.find_template("L1").reply_code == "RESEARCH"


def test_qualified_to_gt2m_no_keeps_answer(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    post(env, quick_reply("GT2M", "L1", "gs-1", msg_id="wamid.in9"))
    n = len(env.l2r.dispositions)
    confirm(env, "NO")
    assert len(env.l2r.dispositions) == n
    assert env.gs.texts[-1][1] == "No problem, we've kept your earlier answer."
    assert env.repo.find_template("L1").reply_code == "1M"


def test_qualified_to_gt2m_yes_moves_to_not_qualified_open(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    post(env, quick_reply("GT2M", "L1", "gs-1", msg_id="wamid.in9"))
    confirm(env, "YES")
    body = env.l2r.dispositions[-1][1]
    assert body["leadStatus"] == "NOT_QUALIFIED_OPEN" and body["followUpInDays"] == 60
    assert env.gs.texts[-1][1].startswith("Thanks, Prakhar!")


def test_gt2m_to_one_month_schedules_call_by_new_span(env):
    from datetime import datetime, timedelta, timezone
    send(env)
    post(env, quick_reply("GT2M", "L1", "gs-1"))
    post(env, quick_reply("1M", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:TIMELINE_CHANGED"
    lead_id, body, _ = env.l2r.calls[-1]
    assert lead_id == "L1" and body["reopen"] is False
    due = datetime.fromisoformat(body["dueAt"])
    days = (due - datetime.now(timezone.utc)).days
    assert 22 <= days <= 25            # 30-day span - 7 days, moved to an open day's opening time
    assert due.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%H:%M") == "09:30"
    assert env.gs.texts[-1][1] == "Thank you for confirming! We've updated your buying timeline to 1 Month."


def test_gt2m_to_research_keeps_call_and_notes_change(env):
    send(env)
    post(env, quick_reply("GT2M", "L1", "gs-1"))
    post(env, quick_reply("RESEARCH", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.l2r.calls == []
    assert "Still Researching" in env.l2r.comments[-1][1]
    assert env.gs.texts[-1][1].startswith("Thanks for letting us know")


def test_research_to_anything_reopens_lead(env):
    send(env)
    post(env, quick_reply("RESEARCH", "L1", "gs-1"))
    post(env, quick_reply("15D", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:REOPENED"
    body = env.l2r.calls[-1][1]
    assert body["reopen"] is True and body["purchaseTimeline"] == "15D"
    assert env.gs.texts[-1][1] == "Thank you for confirming! We've updated your buying timeline to 15 Days."


def test_same_answer_again_changes_nothing(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    n = len(env.l2r.dispositions)
    post(env, quick_reply("1M", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:SAME_ANSWER"
    assert len(env.l2r.dispositions) == n


def test_every_tap_is_tracked_including_changes(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    post(env, quick_reply("GT2M", "L1", "gs-1", msg_id="wamid.in9"))
    confirm(env, "YES")
    steps = [r["step"] for r in env.client.get("/internal/db/wa_interaction").json()["rows"]]
    assert sorted(steps) == ["CONFIRM_CHANGE", "TIMELINE", "TIMELINE"]


def test_typed_reply_is_classified(env):
    send(env)
    evt = quick_reply("1M", "L1", "gs-1")
    evt["payload"] = {"id": "wamid.t1", "source": "919876543210", "type": "text", "payload": {"text": "within 15 days"}}
    post(env, evt)
    assert env.l2r.dispositions[0][1]["targetStage"] == "QUALIFICATION"
    assert len(env.gs.lists) == 1


def test_stop_opts_out(env):
    send(env)
    evt = quick_reply("1M", "L1", "gs-1")
    evt["payload"] = {"id": "wamid.s1", "source": "919876543210", "type": "text", "payload": {"text": "STOP"}}
    post(env, evt)
    assert env.repo.is_opted_out("919876543210", "D1")


def test_status_events_only_move_forward_and_capture_wamid(env):
    r = send(env)

    def status(t, extra=None):
        p = {"id": "wamid.out1", "gsId": "gs-1", "type": t}
        if extra:
            p["payload"] = extra
        return {"type": "message-event", "payload": p}

    post(env, status("enqueued", {"whatsappMessageId": "wamid.HBgM1"}))
    post(env, status("read"))
    post(env, status("delivered"))  # late, out of order
    m = env.repo.get_message(r.message_id)
    assert m.status == MsgStatus.READ and m.wamid == "wamid.HBgM1"


def test_l2r_outage_is_retried_by_maintenance(env):
    send(env)
    env.l2r.down = True
    post(env, quick_reply("1M", "L1", "gs-1"))
    assert env.l2r.dispositions == []
    env.l2r.down = False
    assert env.svc.run_maintenance()["synced"] == 1
    assert env.l2r.dispositions[0][1]["targetStage"] == "QUALIFICATION"


def test_expiry_marks_no_response_for_templates_only(env):
    r = send(env)
    env.repo._exec("UPDATE wa_message SET sent_at = '2026-01-01T00:00:00+00:00'")
    out = env.svc.run_maintenance()
    assert out["expired"] == 1
    assert env.repo.get_message(r.message_id).status == MsgStatus.EXPIRED
    assert "No reply" in env.l2r.comments[-1][1]


# ---------------------------------------------------------------- endpoints
def test_lead_created_endpoint_with_make(env):
    res = env.client.post("/internal/leads/created", json={
        "leadId": "L7", "dealerId": "D1", "mobile": "919812345678", "customerName": "Prakhar",
        "make": "Bajaj", "modelName": "Pulsar NS200", "createdAt": "2026-11-06T00:26:00+05:30",
    })
    assert res.json()["sent"] is True
    assert env.gs.templates[-1].params[1:3] == ["Bajaj", "Pulsar NS200"]


def test_engagement_per_template(env):
    send(env)
    send(env, lead_id="L2", mobile="919800000002")
    send(env, lead_id="L3", mobile="919800000003")
    for gs in ("gs-1", "gs-2", "gs-3"):
        post(env, {"type": "message-event", "payload": {"id": "w" + gs, "gsId": gs, "type": "delivered"}})
    post(env, quick_reply("1M", "L1", "gs-1", msg_id="a1"))
    post(env, quick_reply("GT2M", "L2", "gs-2", mobile="919800000002", msg_id="a2"))
    t = env.client.get("/internal/engagement").json()["templates"][0]
    assert t["template_id"] == "tpl-nwh-1"
    assert (t["sent"], t["delivered"], t["replied"]) == (3, 3, 2)
    assert t["engagement_pct"] == 66.7
    assert t["options"] == {"1M": 1, "GT2M": 1} and t["qualified"] == 1


# ---------------------------------------------------------------- L2R follow-up form
def test_bot_tags_the_follow_up_form_like_a_telecaller(env):
    """The bot never calls the dealer panel. It fills the same fields as the follow-up form;
    L2R's own qualification event then sends the lead to the dealer panel."""
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    body = env.l2r.dispositions[0][1]
    assert body["followUpStatus"] == "SPOKE_TO_CUSTOMER" and body["leadStatus"] == "INTERESTED"
    assert body["expectedBuyingSpan"] == "1M" and body["eagerness"] == "HOT"
    assert body["targetStage"] == "QUALIFICATION"
