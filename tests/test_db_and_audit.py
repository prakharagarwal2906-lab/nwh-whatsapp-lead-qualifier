from .conftest import TOKEN, lead, quick_reply

H = {"X-Webhook-Token": TOKEN}


def _qualify(env):
    ld = lead()
    env.l2r.leads[ld.lead_id] = ld
    env.svc.on_lead_created(ld)
    env.client.post("/v1/webhooks/gupshup", json=quick_reply("TIMELINE_1M", "L1", "gs-1"), headers=H)


def test_db_overview_counts_every_table(env):
    _qualify(env)
    counts = env.client.get("/internal/db").json()["tables"]
    assert set(counts) == {"dealer_config", "wa_message", "wa_event", "opt_out", "audit_log"}
    assert counts["wa_message"] == 1 and counts["wa_event"] == 1 and counts["audit_log"] >= 3


def test_list_rows_and_lead_filter(env):
    _qualify(env)
    rows = env.client.get("/internal/db/wa_message?lead_id=L1").json()["rows"]
    assert rows[0]["outcome"] == "QUALIFIED" and rows[0]["l2r_sync_status"] == "DONE"
    ev = env.client.get("/internal/db/wa_event").json()["rows"][0]
    assert ev["payload"]["payload"]["type"] == "quick_reply"  # JSON decoded


def test_unknown_table_is_404(env):
    assert env.client.get("/internal/db/sqlite_master").status_code == 404


def test_lead_audit_trail_in_order(env):
    _qualify(env)
    events = [a["event"] for a in env.client.get("/internal/leads/L1/audit").json()["audit"]]
    assert events[0] == "template_sent"
    assert "l2r_synced" in events
    assert "webhook_processed:reply:QUALIFIED" in events


def test_rejections_are_audited(env):
    _qualify(env)
    evt = quick_reply("TIMELINE_1M", "L1", "gs-1")
    env.client.post("/v1/webhooks/gupshup", json=evt, headers=H)  # duplicate
    events = [r["event"] for r in env.client.get("/internal/db/audit_log").json()["rows"]]
    assert "webhook_duplicate" in events
