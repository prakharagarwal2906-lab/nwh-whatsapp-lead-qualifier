// Shared database API for the prototype (Netlify Function, Node 18+).
//
//   GET  /api/db               -> { ok, version, state, counts }
//   POST /api/db               -> save { state, tables } ; returns { version }
//   POST /api/db/reset         -> empty every prototype table
//   GET  /api/db/tables/:name  -> rows of one table
//
// Connection string: NETLIFY_DATABASE_URL (Netlify DB / Neon) or DATABASE_URL
// (any Postgres: Neon, Supabase, RDS ...). Tables live in schema "nwh" so they
// never collide with the Python service's tables in the same database.
import pg from "pg";

const SCHEMA = "nwh";
const MAX_BODY = 2_000_000;

// name -> [primary key columns, column definitions]
export const TABLES = {
  leads_l2r: [["lead_id"], {
    lead_id: "text", customer_name: "text", mobile: "text", make: "text", model: "text", city: "text",
    dealer_id: "text", source: "text", created_at_local: "text", lead_status: "text", qualified_by: "text",
    whatsapp_qualified: "boolean", inquiries: "integer", stage: "text",
    eagerness: "text", priority: "text", purchase_timeline: "text", visit_date: "text", visit_slot: "text",
    sent_to_dealer_at: "text", open_call_due: "text", bot_state: "text",
  }],
  // Inquiries on a lead (a new vehicle version on an open lead adds one; the same version does not).
  lead_inquiries: [["inq_id"], {
    inq_id: "text", lead_id: "text", make: "text", model: "text", created_at: "text", source: "text",
  }],
  // One row per call. A lead always has at most one OPEN call; disposing it schedules the next.
  lead_calls: [["call_id"], {
    call_id: "text", lead_id: "text", seq: "integer", status: "text", created_at: "text", due_at: "text",
    due_reason: "text", disposed_at: "text", disposed_by: "text", call_status: "text", followup_status: "text",
    interest: "text", buying_span: "text", reason: "text", sub_reason: "text", td_required: "text", td_at: "text", comment: "text", next_call_at: "text",
  }],
  dealer_config: [["dealer_id"], {
    dealer_id: "text", dealer_name: "text", city: "text", waba_number: "text", virtual_number: "text",
    sales_executive: "text", sales_executive_number: "text", working_hours: "text", weekly_off: "text", template_id: "text",
  }],
  // One row per outbound message. message_id is Gupshup's id from the send response.
  wa_message: [["message_id"], {
    message_id: "text", wamid: "text", lead_id: "text", dealer_id: "text", template_id: "text",
    started_by_template: "text", flow: "text", step: "text", msg_type: "text", to_mobile: "text",
    from_waba: "text", status: "text", sent_at: "text", delivered_at: "text", read_at: "text",
    replied_at: "text", reply_option: "text",
  }],
  // One row per customer tap, linked to the message it answered.
  wa_interaction: [["inbound_id"], {
    inbound_id: "text", context_message_id: "text", context_wamid: "text", lead_id: "text",
    dealer_id: "text", template_id: "text", flow: "text", step: "text", option_code: "text",
    option_title: "text", postback: "text", received_at: "text",
  }],
  // Tasks against a lead, e.g. a Showroom Visit created by WhatsApp once date and time are known.
  lead_tasks: [["task_id"], {
    task_id: "text", lead_id: "text", type: "text", status: "text", created_by: "text", created_at: "text",
    dealer_id: "text", assigned_to: "text", date: "text", time: "text", slot: "text", location: "text",
    comments: "text", changed_at: "text", completed_at: "text",
  }],
  wa_event: [["event_id"], {
    event_id: "text", type: "text", received_at: "text", lead_id: "text", payload: "jsonb",
  }],
  audit_log: [["id"], {
    id: "integer", ts: "text", level: "text", source: "text", lead_id: "text",
    message: "text", detail: "jsonb",
  }],
};

let pool = null;
/* Hosted Postgres URLs (Netlify DB / Neon) carry query options such as sslmode=require and
   channel_binding=require. node-postgres can fail on channel_binding, and sslmode in the URL
   overrides the ssl option below, so both are removed and SSL is set explicitly. */
export function cleanDatabaseUrl(url) {
  try {
    const u = new URL(url);
    for (const k of ["sslmode", "channel_binding", "sslrootcert", "sslcert", "sslkey"]) u.searchParams.delete(k);
    return u.toString();
  } catch {
    return url;
  }
}
/* Find the Postgres connection string. Known names first, then any environment variable whose
   name looks database-related and whose value is a postgres:// URL (hosting platforms name it
   differently, e.g. NETLIFY_DATABASE_URL, NETLIFY_DB_URL, DATABASE_URL, POSTGRES_URL). */
const KNOWN_DB_VARS = ["NETLIFY_DATABASE_URL", "DATABASE_URL", "NETLIFY_DB_URL", "NETLIFY_DATABASE_URL_UNPOOLED",
                       "POSTGRES_URL", "POSTGRES_PRISMA_URL", "NEON_DATABASE_URL", "PG_URL"];
const looksDb = k => /DATABASE|POSTGRES|NEON|(^|_)DB(_|$)|PG/i.test(k);
export function findDatabaseUrl(env = process.env) {
  for (const k of KNOWN_DB_VARS) if (env[k]) return { name: k, url: env[k] };
  for (const [k, v] of Object.entries(env)) {
    if (looksDb(k) && typeof v === "string" && /^postgres(ql)?:\/\//i.test(v)) return { name: k, url: v };
  }
  return null;
}
export function dbVarNames(env = process.env) {   // names only, never values
  return Object.keys(env).filter(looksDb).sort();
}
export function getPool(url = (findDatabaseUrl() || {}).url) {
  if (pool) return pool;
  if (!url) return null;
  const local = /localhost|127\.0\.0\.1/.test(url);
  pool = new pg.Pool({ connectionString: cleanDatabaseUrl(url), max: 3, connectionTimeoutMillis: 10000,
                       ssl: local ? false : { rejectUnauthorized: false } });
  // A dropped idle connection (e.g. a serverless database scaling to zero) must not crash the function.
  pool.on("error", (err) => console.error("idle database connection error:", err.message));
  return pool;
}
export function setPool(p) { pool = p; }

let ready = null;
async function ensureSchema(db) {
  if (!ready) {
    ready = (async () => {
      await db.query(`CREATE SCHEMA IF NOT EXISTS ${SCHEMA}`);
      await db.query(`CREATE TABLE IF NOT EXISTS ${SCHEMA}.snapshot (
        id integer PRIMARY KEY, state jsonb NOT NULL, version bigint NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now())`);
      for (const [name, [pk, cols]] of Object.entries(TABLES)) {
        const defs = Object.entries(cols).map(([c, t]) => `${c} ${t}`).join(", ");
        await db.query(`CREATE TABLE IF NOT EXISTS ${SCHEMA}.${name} (${defs},
          updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (${pk.join(", ")}))`);
        // Add columns introduced after the table was first created.
        for (const [col, type] of Object.entries(cols)) {
          await db.query(`ALTER TABLE ${SCHEMA}.${name} ADD COLUMN IF NOT EXISTS ${col} ${type}`);
        }
      }
    })().catch(e => { ready = null; throw e; });
  }
  return ready;
}

function clean(name, row) {
  const cols = TABLES[name][1];
  const out = {};
  for (const [c, t] of Object.entries(cols)) {
    let v = row[c];
    if (v === undefined) v = null;
    if (t === "jsonb" && typeof v === "string") { try { v = JSON.parse(v); } catch { /* keep text */ } }
    out[c] = v;
  }
  return out;
}

// Replace one table's contents with `rows`: upsert incoming, delete the rest.
async function syncTable(client, name, rows) {
  const [pk, cols] = TABLES[name];
  const names = Object.keys(cols);
  const data = JSON.stringify(rows.map(r => clean(name, r)));
  const nonPk = names.filter(c => !pk.includes(c));
  await client.query(
    `INSERT INTO ${SCHEMA}.${name} (${names.join(", ")})
     SELECT ${names.join(", ")} FROM jsonb_populate_recordset(NULL::${SCHEMA}.${name}, $1::jsonb)
     ON CONFLICT (${pk.join(", ")}) DO UPDATE SET
       ${nonPk.map(c => `${c} = EXCLUDED.${c}`).join(", ")}, updated_at = now()`,
    [data],
  );
  const keyExpr = pk.length === 1 ? pk[0] : `(${pk.join(", ")})`;
  const incoming = pk.length === 1
    ? `SELECT ${pk[0]} FROM jsonb_populate_recordset(NULL::${SCHEMA}.${name}, $1::jsonb)`
    : `SELECT ${pk.join(", ")} FROM jsonb_populate_recordset(NULL::${SCHEMA}.${name}, $1::jsonb)`;
  await client.query(`DELETE FROM ${SCHEMA}.${name} WHERE ${keyExpr} NOT IN (${incoming})`, [data]);
}

const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "content-type": "application/json", "cache-control": "no-store" },
});

export default async function handler(req) {
  const db = getPool();
  if (!db) return json({ ok: false, error: "No database configured. Set NETLIFY_DATABASE_URL or DATABASE_URL.",
                         databaseLikeVariablesSeen: dbVarNames() }, 503);
  const url = new URL(req.url);
  const path = url.pathname.replace(/\/+$/, "");
  try {
    await ensureSchema(db);

    if (req.method === "GET" && path === "/api/db") {
      const snap = await db.query(`SELECT state, version, updated_at FROM ${SCHEMA}.snapshot WHERE id = 1`);
      const counts = {};
      for (const name of Object.keys(TABLES)) {
        counts[name] = Number((await db.query(`SELECT count(*) AS n FROM ${SCHEMA}.${name}`)).rows[0].n);
      }
      const s = snap.rows[0];
      return json({ ok: true, backend: "postgresql", version: s ? Number(s.version) : 0,
                    updated_at: s?.updated_at ?? null, state: s?.state ?? null, counts });
    }

    const m = path.match(/^\/api\/db\/tables\/([a-z_]+)$/);
    if (req.method === "GET" && m) {
      if (!TABLES[m[1]]) return json({ ok: false, error: "unknown table" }, 404);
      const limit = Math.min(Number(url.searchParams.get("limit")) || 200, 1000);
      const rows = (await db.query(`SELECT * FROM ${SCHEMA}.${m[1]} ORDER BY updated_at DESC LIMIT $1`, [limit])).rows;
      return json({ ok: true, table: m[1], count: rows.length, rows });
    }

    if (req.method === "POST" && path === "/api/db/reset") {
      await db.query(`TRUNCATE ${Object.keys(TABLES).map(t => `${SCHEMA}.${t}`).join(", ")}, ${SCHEMA}.snapshot`);
      return json({ ok: true, version: 0 });
    }

    if (req.method === "POST" && path === "/api/db") {
      const text = await req.text();
      if (text.length > MAX_BODY) return json({ ok: false, error: "payload too large" }, 413);
      let body;
      try { body = JSON.parse(text); } catch { return json({ ok: false, error: "invalid json" }, 400); }
      if (!body || typeof body.state !== "object" || typeof body.tables !== "object") {
        return json({ ok: false, error: "expected { state, tables }" }, 400);
      }
      const client = await db.connect();
      try {
        await client.query("BEGIN");
        const r = await client.query(
          `INSERT INTO ${SCHEMA}.snapshot (id, state, version) VALUES (1, $1::jsonb, 1)
           ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state,
             version = ${SCHEMA}.snapshot.version + 1, updated_at = now()
           RETURNING version`, [JSON.stringify(body.state)]);
        for (const name of Object.keys(TABLES)) {
          if (Array.isArray(body.tables[name])) await syncTable(client, name, body.tables[name]);
        }
        await client.query("COMMIT");
        return json({ ok: true, version: Number(r.rows[0].version) });
      } catch (e) {
        await client.query("ROLLBACK").catch(() => {});
        throw e;
      } finally {
        client.release();
      }
    }

    return json({ ok: false, error: "not found" }, 404);
  } catch (e) {
    console.error(e);
    return json({ ok: false, error: String(e.message || e) }, 500);
  }
}

export const config = { path: ["/api/db", "/api/db/*"] };
