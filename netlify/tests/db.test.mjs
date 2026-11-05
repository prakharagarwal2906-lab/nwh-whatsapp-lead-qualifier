// Run against a real Postgres:  TEST_DATABASE_URL=postgresql://... npm run test:api
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import pg from "pg";
import handler, { getPool, setPool } from "../functions/db.mjs";

const URL_ = process.env.TEST_DATABASE_URL;
const skip = !URL_ && "TEST_DATABASE_URL not set";
const call = (method, path, body) => handler(new Request("https://site.test" + path, {
  method, body: body ? JSON.stringify(body) : undefined, headers: { "content-type": "application/json" },
})).then(async r => ({ status: r.status, body: await r.json() }));

before(async () => {
  if (skip) return;
  const c = new pg.Client({ connectionString: URL_ }); await c.connect();
  await c.query("DROP SCHEMA IF EXISTS proto CASCADE"); await c.end();
  setPool(null); getPool(URL_);
});
after(async () => { if (!skip) await getPool().end(); });

const sample = (status = "SENT") => ({
  state: { leads: [{ id: "L1" }], note: "full client state" },
  tables: {
    leads_l2r: [{ lead_id: "L1", customer_name: "Prakhar", mobile: "919876543210", consent: true, stage: "FIRST_CALL" }],
    dealer_config: [{ dealer_id: "D1", enabled: true, dealer_name: "Vishnu Cars TN", qualify_max_days: 60 }],
    wa_message: [{ id: 1, lead_id: "L1", status, gs_message_id: "gs-100" }],
    wa_event: [{ event_id: "wamid.in1", type: "message", payload: JSON.stringify({ a: 1 }) }],
    opt_out: [],
    audit_log: [{ id: 1, ts: "2026-11-06T00:26:00Z", level: "INFO", source: "Qualifier", message: "LeadCreated L1", detail: null }],
  },
});

test("empty database returns no state", { skip }, async () => {
  const r = await call("GET", "/api/db");
  assert.equal(r.status, 200);
  assert.equal(r.body.version, 0);
  assert.equal(r.body.state, null);
});

test("save then load round-trips state and fills tables", { skip }, async () => {
  const w = await call("POST", "/api/db", sample());
  assert.equal(w.body.version, 1);
  const r = await call("GET", "/api/db");
  assert.equal(r.body.state.note, "full client state");
  assert.equal(r.body.counts.wa_message, 1);
  const ev = await call("GET", "/api/db/tables/wa_event");
  assert.deepEqual(ev.body.rows[0].payload, { a: 1 });
});

test("second save upserts rows and deletes removed ones", { skip }, async () => {
  const s = sample("REPLIED");
  s.tables.audit_log.push({ id: 2, ts: "x", level: "INFO", source: "Sync", message: "L2R updated" });
  s.tables.wa_event = [];
  const w = await call("POST", "/api/db", s);
  assert.equal(w.body.version, 2);
  const msg = await call("GET", "/api/db/tables/wa_message");
  assert.equal(msg.body.rows[0].status, "REPLIED");
  assert.equal((await call("GET", "/api/db")).body.counts.audit_log, 2);
  assert.equal((await call("GET", "/api/db")).body.counts.wa_event, 0);
});

test("unknown table and bad payload are rejected", { skip }, async () => {
  assert.equal((await call("GET", "/api/db/tables/pg_user")).status, 404);
  assert.equal((await call("POST", "/api/db", { nope: 1 })).status, 400);
});

test("reset empties everything", { skip }, async () => {
  await call("POST", "/api/db/reset");
  const r = await call("GET", "/api/db");
  assert.equal(r.body.state, null);
  assert.ok(Object.values(r.body.counts).every(n => n === 0));
});
