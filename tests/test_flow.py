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
    # customer name, make, model, working hours (no dealer name, executive or rotating virtual number)
    assert req.params == ["Prakhar", "Royal Enfield", "Classic 350", "Mon–Sat, 9:30 AM – 6:30 PM"]   # BikeWale team hours
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
    assert not any("visit date" in c[1].lower() for c in env.l2r.comments)   # nothing until the time is picked
    time_list = env.gs.lists[1]
    assert time_list["section"] == "Choose your preferred time"
    assert time_list["rows"][0].title == "9:30 AM - 10:30 AM"

    slot = time_list["rows"][2]
    post(env, list_reply("VISIT_TIME", slot.postback.split("|")[2], slot.title, "L1",
                         gs_of(env, "VISIT_TIME"), msg_id="wamid.in3"))
    lead_id, task, _ = env.l2r.visits[0]
    assert lead_id == "L1" and task["type"] == "SHOWROOM_VISIT" and task["createdBy"] == "WHATSAPP"
    assert task["date"] == day_iso and task["slotStart"] == "1130" and task["salesExecutive"] == "Sathya"
    assert "Your visit is booked" in env.gs.texts[-1][1] and slot.title in env.gs.texts[-1][1]
    # no executive name, no "will call to confirm"; the fixed sales executive number kept against the dealer
    assert env.gs.texts[-1][1].endswith("To connect with the dealership (Mon–Sat, 9:30 AM – 5:30 PM), call 919840011223.")
    assert "Sathya" not in env.gs.texts[-1][1] and "confirm" not in env.gs.texts[-1][1]

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
                                   "Need help before that? Call us at 918047105555 during working hours (Mon–Sat, 9:30 AM – 6:30 PM).")   # BikeWale support number
    assert env.gs.lists == []


def test_just_researching_closes_lead_and_gets_help_message(env):
    send(env)
    post(env, quick_reply("RESEARCH", "L1", "gs-1", text="Just Researching"))
    body = env.l2r.dispositions[0][1]
    assert body["leadStatus"] == "NOT_INTERESTED" and body["closeLead"] is True
    assert (body["reason"], body["subReason"]) == ("Not interested in buying", "Just researching")
    assert env.gs.texts[-1][1] == ("Thanks for the confirmation. If you need any assistance in future, please call us at 918047105555 "
                                   "during working hours (Mon–Sat, 9:30 AM – 6:30 PM).")


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


def change(env, option, msg_id="wamid.in9", confirm_id="wamid.c1"):
    """Customer taps a different timeline: it is reconfirmed first, then they tap Yes."""
    post(env, quick_reply(option, "L1", "gs-1", msg_id=msg_id))
    assert env.repo.event_result(msg_id) == "reply:CONFIRMATION_ASKED"
    confirm(env, "YES", msg_id=confirm_id)
    return env.repo.event_result(confirm_id)


def test_any_timeline_change_is_reconfirmed_before_it_applies(env):
    send(env)
    post(env, quick_reply("15D", "L1", "gs-1"))
    n, texts = len(env.l2r.dispositions), len(env.gs.texts)
    post(env, quick_reply("1_2M", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:CONFIRMATION_ASKED"
    assert env.gs.buttons[-1]["body"] == "Earlier, you selected ‘15 Days’. Would you like to change it to ‘1 - 2 Months’?"
    assert [b.title for b in env.gs.buttons[-1]["buttons"]] == ["Yes, change it", "No, keep earlier answer"]
    # nothing changes until the customer says yes
    assert len(env.l2r.dispositions) == n and len(env.gs.texts) == texts
    assert env.repo.find_template("L1").reply_code == "15D"


def test_no_keeps_the_earlier_answer(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    n = len(env.l2r.dispositions)
    post(env, quick_reply("GT2M", "L1", "gs-1", msg_id="wamid.in9"))
    confirm(env, "NO")
    assert env.repo.event_result("wamid.c1") == "reply:KEPT"
    assert env.gs.texts[-1][1] == "No problem, we've kept your earlier answer."
    assert len(env.l2r.dispositions) == n and env.l2r.calls == []
    assert env.repo.find_template("L1").reply_code == "1M"


def test_qualified_to_qualified_sends_thank_you(env):
    send(env)
    post(env, quick_reply("15D", "L1", "gs-1"))
    assert change(env, "1_2M") == "reply:TIMELINE_UPDATED"
    assert env.l2r.dispositions[-1][1]["purchaseTimeline"] == "1_2M"
    assert env.l2r.dispositions[-1][1]["priority"] == "P2"
    assert env.gs.texts[-1][1] == "Thank you for the confirmation."
    assert env.repo.find_template("L1").reply_code == "1_2M"


def test_qualified_lead_is_never_disqualified_by_research(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    n = len(env.l2r.dispositions)
    assert change(env, "RESEARCH") == "reply:NOTED_STAYS_QUALIFIED"
    assert len(env.l2r.dispositions) == n                                 # never closed
    assert "lead stays qualified" in env.l2r.comments[-1][1]
    assert env.gs.texts[-1][1] == "Thank you for the confirmation."

def test_qualified_lead_tapping_gt2m_stays_qualified_with_span_call(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    n = len(env.l2r.dispositions)
    assert change(env, "GT2M") == "reply:NOTED_STAYS_QUALIFIED"
    assert len(env.l2r.dispositions) == n
    body = env.l2r.calls[-1][1]
    assert body["reopen"] is False and body["purchaseTimeline"] == "GT2M"



def test_gt2m_to_one_month_qualifies_straight_away(env):
    send(env)
    post(env, quick_reply("GT2M", "L1", "gs-1"))
    assert change(env, "1M") == "reply:QUALIFIED_ON_CHANGE"
    body = env.l2r.dispositions[-1][1]
    assert body["targetStage"] == "QUALIFICATION" and body["leadStatus"] == "INTERESTED"
    assert body["expectedBuyingSpan"] == "1M" and body["reopen"] is False
    assert env.gs.texts[-1][1] == "Thank you for the confirmation."
    assert env.gs.lists[-1]["section"] == "Choose your preferred date"   # continues to the visit
    assert env.l2r.calls == []


def test_research_to_gt2m_reopens_with_span_call(env):
    send(env)
    post(env, quick_reply("RESEARCH", "L1", "gs-1"))
    assert change(env, "GT2M") == "reply:REOPENED"
    body = env.l2r.calls[-1][1]
    assert body["reopen"] is True and body["purchaseTimeline"] == "GT2M"


def test_gt2m_to_research_confirms_then_closes(env):
    send(env)
    post(env, quick_reply("GT2M", "L1", "gs-1"))
    n = len(env.l2r.dispositions)
    post(env, quick_reply("RESEARCH", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:CONFIRMATION_ASKED"
    assert env.gs.buttons[-1]["body"] == "Earlier, you selected ‘More than 2 Months’. Would you like to change it to ‘Just Researching’?"
    assert len(env.l2r.dispositions) == n
    confirm(env, "YES")
    body = env.l2r.dispositions[-1][1]
    assert body["closeLead"] is True and body["subReason"] == "Just researching"
    assert "If you need any assistance in future" in env.gs.texts[-1][1]
    assert env.l2r.calls == []

def test_research_to_fifteen_days_reopens_and_qualifies(env):
    send(env)
    post(env, quick_reply("RESEARCH", "L1", "gs-1"))
    assert change(env, "15D") == "reply:QUALIFIED_ON_CHANGE"
    body = env.l2r.dispositions[-1][1]
    assert body["reopen"] is True and body["targetStage"] == "QUALIFICATION" and body["priority"] == "P1"
    assert env.gs.lists[-1]["section"] == "Choose your preferred date"


def test_same_answer_again_changes_nothing(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    n = len(env.l2r.dispositions)
    post(env, quick_reply("1M", "L1", "gs-1", msg_id="wamid.in9"))
    assert env.repo.event_result("wamid.in9") == "reply:SAME_ANSWER"
    assert len(env.l2r.dispositions) == n


def test_every_tap_is_tracked_including_changes(env):
    send(env)
    post(env, quick_reply("GT2M", "L1", "gs-1"))
    post(env, quick_reply("RESEARCH", "L1", "gs-1", msg_id="wamid.in9"))
    confirm(env, "YES")
    steps = [r["step"] for r in env.client.get("/internal/db/wa_interaction").json()["rows"]]
    assert sorted(steps) == ["CONFIRM_CHANGE", "TIMELINE", "TIMELINE"]


def typed(env, text, msg_id="wamid.t1", mobile="919876543210"):
    evt = quick_reply("1M", "L1", "gs-1")
    evt["payload"] = {"id": msg_id, "source": mobile, "type": "text", "payload": {"text": text}}
    return post(env, evt)


def test_random_text_asks_to_type_hi(env):
    send(env)
    typed(env, "what is the price?")
    assert env.repo.event_result("wamid.t1") == "reply:ASK_HI"
    assert env.gs.texts[-1][1] == "Please type Hi to start the journey."
    assert env.l2r.dispositions == []


def test_hi_from_unqualified_lead_asks_timeline_again(env):
    send(env)
    typed(env, "Hi")
    assert env.repo.event_result("wamid.t1") == "reply:TIMELINE_ASKED_AGAIN"
    msg = env.gs.buttons[-1]
    assert msg["body"] == ("Thanks for connecting with BikeWale. We can see you have shown interest in the "
                           "Royal Enfield Classic 350. Can you tell us how soon you are planning to buy?")
    assert [b.title for b in msg["buttons"]][:3] == ["15 Days", "1 Month", "1 - 2 Months"]
    # tapping one of these buttons works like the template: the lead is qualified
    btn = msg["buttons"][1]
    evt = quick_reply("1M", "L1", gs_of(env, "TIMELINE"), msg_id="wamid.t2", text=btn.title)
    post(env, evt)
    assert env.l2r.dispositions[-1][1]["targetStage"] == "QUALIFICATION"
    assert env.repo.find_template("L1").reply_code == "1M"


def test_hi_from_qualified_lead_shares_dealer_and_brand_numbers(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    typed(env, "hii", msg_id="wamid.t3")
    assert env.repo.event_result("wamid.t3") == "reply:DEALER_INFO"
    assert env.gs.texts[-1][1] == ("Vishnu Motors TN dealership has been assigned to you. "
                                   "For any assistance, you can reach them at 919840011223.")


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
    post(env, quick_reply("15D", "L1", "gs-1", msg_id="a1b"))           # same customer taps again
    post(env, quick_reply("GT2M", "L2", "gs-2", mobile="919800000002", msg_id="a2"))
    typed(env, "Is there a discount?", msg_id="t9", mobile="919800000003")
    t = env.client.get("/internal/engagement").json()["templates"][0]
    assert t["template_id"] == "tpl-nwh-1"
    assert (t["sent"], t["delivered"]) == (3, 3)
    assert t["replied"] == 3                       # unique per template: L1 counted once
    assert t["engagement_pct"] == 100.0
    assert t["option_clicks"] == {"1M": 1, "15D": 1, "GT2M": 1}
    assert t["text_replies"] == 1
    texts = [r for r in env.client.get("/internal/db/wa_interaction").json()["rows"] if r["step"] == "FREE_TEXT"]
    assert texts[0]["option_title"] == "Is there a discount?"     # what the customer typed is kept

def test_qualified_lead_cannot_be_closed_after_gt2m_then_research(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    env.l2r.leads["L1"].stage = "QUALIFICATION"
    assert change(env, "GT2M", msg_id="wamid.a1", confirm_id="wamid.c1") == "reply:NOTED_STAYS_QUALIFIED"
    assert change(env, "RESEARCH", msg_id="wamid.a2", confirm_id="wamid.c2") == "reply:NOTED_STAYS_QUALIFIED"
    assert not any(d[1].get("closeLead") for d in env.l2r.dispositions)



def test_after_hours_uses_bikewale_team_hours_not_dealer_hours(env):
    from datetime import datetime
    # 6 PM Friday: the dealer (9:30-5:30) is closed but the BikeWale team (Mon-Sat 9:30-6:30) is working
    r = send(env, created=datetime(2026, 11, 6, 18, 0, tzinfo=IST))
    assert (r.sent, r.reason) == (False, "working_hours")
    # Sunday 11 AM: the BikeWale team is off -> WhatsApp goes out
    r2 = send(env, lead_id="L2", mobile="919800000002", created=datetime(2026, 11, 8, 11, 0, tzinfo=IST))
    assert r2.sent



def test_new_inquiry_after_hours_on_unqualified_lead_gets_template(env):
    from datetime import datetime
    send(env)
    ld = lead(created=datetime(2026, 11, 6, 22, 0, tzinfo=IST))
    ld.make, ld.model_name = "TVS", "Jupiter 125"
    r = env.svc.on_inquiry_added(ld, "INQ-2")
    assert r.sent and env.gs.templates[-1].params[1:3] == ["TVS", "Jupiter 125"]


def test_new_inquiry_on_qualified_lead_or_in_working_hours_sends_nothing(env):
    from datetime import datetime
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))                 # lead is now qualified
    assert env.svc.on_inquiry_added(lead(created=datetime(2026, 11, 6, 22, 0, tzinfo=IST)), "INQ-2").reason == "lead_already_qualified_or_closed"
    send(env, lead_id="L5", mobile="919800000005")
    assert env.svc.on_inquiry_added(lead(lead_id="L5", mobile="919800000005", created=datetime(2026, 11, 6, 11, 0, tzinfo=IST)), "INQ-3").reason == "working_hours"


def test_whatsapp_qualified_marker_is_sent_to_l2r(env):
    send(env)
    post(env, quick_reply("1M", "L1", "gs-1"))
    assert env.l2r.dispositions[0][1]["qualifiedBy"] == "WHATSAPP"


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


def test_master_dealer_holds_bikewale_hours_and_is_configurable(env):
    from datetime import datetime
    master = env.repo.get_dealer("MASTER")
    assert master is not None and master.dealer_name == "BikeWale (master dealer)"
    assert sorted(master.working_hours) == ["fri", "mon", "sat", "thu", "tue", "wed"]       # Mon-Sat
    assert master.working_hours["sat"] == [["09:30", "18:30"]]
    # Saturday 11 AM: the team is working -> no WhatsApp
    assert send(env, created=datetime(2026, 11, 7, 11, 0, tzinfo=IST)).reason == "working_hours"
    # Configure the master dealer (e.g. Saturday off, new support number) through the API
    hours = {d: [["09:30", "18:30"]] for d in ("mon", "tue", "wed", "thu", "fri")}
    r = env.client.put("/internal/dealers/MASTER", json={
        "dealerName": "BikeWale (master dealer)", "gupshupAppId": "app1", "wabaNumber": "918047104444",
        "templateId": "tpl_nwh_timeline_v1", "workingHours": hours, "supportNumber": "918000000000"})
    assert r.status_code == 200
    # Same Saturday time now counts as after hours -> WhatsApp goes out, quoting the master's hours
    r2 = send(env, lead_id="L2", mobile="919800000002", created=datetime(2026, 11, 7, 11, 0, tzinfo=IST))
    assert r2.sent and env.gs.templates[-1].params[3] == "Mon–Fri, 9:30 AM – 6:30 PM"
    assert env.svc.support_number == "918000000000"
    # every message goes out from the master's WABA number and template, never a dealer's
    assert env.gs.templates[-1].source == "918047104444" and env.gs.templates[-1].template_id == "tpl_nwh_timeline_v1"


def test_dealer_rows_hold_only_dealer_details(env):
    d = env.repo.get_dealer("D1")
    assert d.enabled is True                       # WhatsApp enabled for this dealer
    assert (d.source_number, d.template_id, d.gupshup_app_id) == ("", "", "")   # no WABA / template per dealer
    assert not hasattr(d, "rep_phone") and not hasattr(d, "qualify_max_days") and not hasattr(d, "pre_close_buffer_min")
    m = env.repo.get_dealer("MASTER")
    assert m.source_number != m.support_number     # BikeWale WABA and support numbers differ
