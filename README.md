# Qualifying after-hours leads on WhatsApp

System design (product and engineering) for qualifying L2R leads that arrive outside dealer working hours.

When a lead lands in L2R outside the dealer's working hours, a templated WhatsApp message (sent through Gupshup) asks the customer when they plan to buy. If they pick a timeline of two months or less, we dispose the first call and move the lead to **Qualification** through the L2R API, so the sales executive starts the next morning with the hottest leads already sorted.

The full styled version, with all sections, is in [`docs/design.html`](docs/design.html). Open it in a browser; the diagrams there use Mermaid.

## Prototype (docs/index.html)

A clickable, four-step journey hosted on Netlify:

1. **Customer website** — the customer enquires about a vehicle with a dealer. A demo control sets the enquiry time so you can try after-hours and open-hours cases.
2. **Lead management (L2R)** — the lead lands, the working-hours check runs, and the Gupshup template call is shown with its six inputs: dealer ID, lead ID, template ID, customer mobile, WABA number, and variables (customer name, make, model, working hours, virtual number, sales executive, dealer name). The response `messageId` is saved against the lead.
3. **Customer's WhatsApp** — the template offers 15 Days / 1 Month / 1–2 Months / More than 2 Months / Still Researching. The first three qualify the lead and ask for a visit date (next 7 open days) and then a time slot (working hours). More than 2 Months and Still Researching each get their own message and a follow-up task.
4. **Tracking & engagement** — every tap is stored in `wa_interaction` with the inbound message ID, the `messageId` it replied to, and the flow, step and option. Engagement % is shown per template ID, with a funnel from sent to visit booked.

## Code

A working reference implementation in Python (FastAPI + SQLite) lives in [`app/`](app), with tests in [`tests/`](tests).

| File | What it does |
|---|---|
| `app/main.py` | HTTP API: Gupshup webhook, lead-created hook, dealer config, maintenance job |
| `app/service.py` | Core flows: eligibility and template send, inbound reply/status processing, L2R sync, expiry and retries |
| `app/working_hours.py` | Per-dealer calendar (hours, weekly offs, holidays, pre-close buffer) and callback phrase ("tomorrow", "on Monday") |
| `app/rules.py` | Postback codes, qualification rule (≤ `qualify_max_days` → Qualified), typed-reply keyword classifier |
| `app/security.py` | Constant-time token check with rotation, IP allowlist |
| `app/db.py` | Tables `dealer_config`, `wa_message`, `wa_event` (dedupe), `opt_out`, `audit_log` |
| `app/clients.py` | Gupshup partner API and L2R API clients (L2R paths are assumed) |

**Run it**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m pytest -q
```

```bash
cp .env.example .env   # fill in tokens, then export the variables
.venv/bin/uvicorn app.main:app --reload
```

**Endpoints**

| Method | Path | Caller |
|---|---|---|
| `POST` | `/v1/webhooks/gupshup` | Gupshup (callback URL in dashboard), header `X-Webhook-Token` |
| `POST` | `/internal/leads/created` | L2R `LeadCreated` event or queue consumer |
| `PUT` | `/internal/dealers/{dealerId}` | Dealer admin: hours, holidays, template, threshold |
| `GET` | `/internal/db`, `/internal/db/{table}` | Row counts, and rows of any table (`?limit`, `?offset`, `?lead_id`) |
| `GET` | `/internal/leads/{leadId}/audit` | Full audit trail for one lead |
| `POST` | `/internal/maintenance` | Scheduler every few minutes: expire 24h conversations, retry failed L2R syncs |


**See every table and the log**

*Prototype (Netlify):* open step 4, **Tracking & engagement**, and scroll to **Database & logs**. It shows `leads_l2r`, `dealer_config`, `wa_message`, `wa_interaction`, `wa_event`, `audit_log` and the live log, with a filter box, **Export table CSV** and **Export full DB JSON**. Everything is kept in the browser and survives a refresh; **Reset** clears it.

*Python service:* every step writes a row to the `audit_log` table and a JSON line to `logs/qualifier.log` (rotated at 10 MB, 5 files; path set by `LOG_FILE`). Read them with:

```bash
curl -s localhost:8000/internal/db -H "X-Internal-Key: $INTERNAL_API_KEY"
```

```bash
curl -s "localhost:8000/internal/db/wa_message?limit=50" -H "X-Internal-Key: $INTERNAL_API_KEY"
```

```bash
curl -s localhost:8000/internal/leads/L12345/audit -H "X-Internal-Key: $INTERNAL_API_KEY"
```

```bash
tail -f logs/qualifier.log
```

Or open the database directly: `sqlite3 qualifier.db "SELECT * FROM audit_log ORDER BY id DESC LIMIT 20;"`


**Shared Postgres for the prototype**

The Netlify site includes a function (`netlify/functions/db.mjs`) that saves the prototype's tables to Postgres in a schema called `nwh` (`leads_l2r`, `dealer_config`, `wa_message`, `wa_interaction`, `wa_event`, `audit_log`, plus a `snapshot` of the full page state). When a database is connected, everyone who opens the site sees the same data, and the header shows **Storage: Postgres (shared)**. Without one, it falls back to **Storage: this browser only**.

To connect one:
1. Create a free Postgres database (for example on neon.tech or supabase.com) and copy its connection string (`postgresql://user:pass@host/db?sslmode=require`).
2. In Netlify: **Site configuration → Environment variables → Add a variable**, key `DATABASE_URL`, value = that connection string. (If you use Netlify's own Neon database, it sets `NETLIFY_DATABASE_URL`, which the function also reads.)
3. **Deploys → Trigger deploy**. The tables are created automatically on the first request.

Query them from the Neon/Supabase SQL editor, for example `SELECT * FROM nwh.wa_interaction ORDER BY received_at DESC;`, or via `GET /api/db/tables/wa_message` on your site.

To run the Python service on the same database, set `DATABASE_URL` for it too; its tables live in the default `public` schema.

Run the API tests against a local Postgres: `TEST_DATABASE_URL=postgresql://... npm run test:api`. Run the Python tests against Postgres: `TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest -q`.

In production, replace the in-process `BackgroundTasks` with a real queue (SQS/Kafka) and set `DATABASE_URL` to Postgres; the boundaries are already split that way.

---

## Contents

1. [Problem and goals](#problem-and-goals)
2. [Customer journey](#customer-journey)
3. [Qualification rules](#qualification-rules)
4. [Success metrics](#success-metrics)
5. [Architecture](#architecture)
6. [End-to-end sequence](#end-to-end-sequence)
7. [Data model](#data-model)
8. [Conversation states](#conversation-states)
9. [API contracts](#api-contracts)
10. [Webhook processing](#webhook-processing)
11. [Webhook security](#webhook-security)
12. [Reliability](#reliability)
13. [Edge cases](#edge-cases)
14. [Compliance and cost](#compliance-and-cost)
15. [Rollout and roadmap](#rollout-and-roadmap)
16. [Open questions](#open-questions)

---

## Problem and goals

A lead created at 12:26 AM sits in *First Call* until the showroom opens. By then the customer has often enquired with other dealers, and every overnight lead gets the same callback priority whether the buyer is ready this fortnight or just browsing.

**Goals**
- Respond to every non-working-hours (NWH) lead within a minute, with the dealer's name and the callback window.
- Capture purchase timeline as structured data and auto-qualify leads with a timeline of two months or less.
- Give sales executives a prioritised morning queue without changing how they work in L2R.

**Non-goals for v1**
- Free-text chatbot or LLM conversation (buttons only).
- Bot replies during working hours.
- Booking or test-drive scheduling inside WhatsApp (see roadmap).

| Who | What they need |
|---|---|
| Customer | Quick acknowledgement, a clear callback time, one-tap answers |
| Sales executive (lead owner) | Hot leads flagged first, bot answer visible in the L2R Activity Feed |
| Dealer manager | Per-dealer switch, working-hours setup, reports on bot-qualified leads to visit and booking |
| CarWale ops | Template approval status, delivery failures, audit trail per lead |

## Customer journey

```mermaid
flowchart TD
  A["Lead created in L2R<br/>outside dealer hours"] --> B{"Eligible?<br/>NWH, bot on, valid mobile,<br/>not contacted in last 24h"}
  B -- No --> Z["Normal flow:<br/>callback in working hours"]
  B -- Yes --> C["Send template:<br/>We are closed, Sathya will call tomorrow 9:30–5:30.<br/>How soon are you planning to buy?"]
  C --> D{"Customer taps"}
  D -- "15 Days / 1 Month / 1–2 Months" --> Q["Dispose first call: Connected, WhatsApp Bot<br/>Move lead to Qualification<br/>Flag HOT for morning queue"]
  D -- "More than 2 Months" --> N["Stay in First Call, tag Nurture<br/>Follow-up task at chosen timeline"]
  D -- "Still Researching" --> R["Stay in First Call, tag Researching<br/>Brochure + follow-up task"]
  D -- "Chat-Sales Executive" --> H["Priority callback task<br/>for lead owner at shift start"]
  D -- "No reply in 24h" --> E["Mark No Response<br/>Normal callback"]
```

## Qualification rules

Each button carries a stable `postbackText` code. Rules key on the code, never the display label.

| Button | Postback code | Outcome | L2R action |
|---|---|---|---|
| 15 Days | `TIMELINE_15D` | Qualified · Hot | Call status Connected, disposition "WhatsApp Bot", stage → Qualification, P1 |
| 1 Month | `TIMELINE_1M` | Qualified · Hot | Same, P1 |
| 1 – 2 Months | `TIMELINE_1_2M` | Qualified · Warm | Stage → Qualification, P2 |
| More than 2 Months | `TIMELINE_GT2M` | Nurture | Comment, follow-up task at +60 days, stays in First Call |
| Still Researching | `RESEARCHING` | Nurture | Comment, follow-up task at +7 days |
| Chat-Sales Executive | `HANDOFF_SE` | Handoff | Callback task for lead owner at next shift start |
| Free text "STOP" | – | Opt-out | Suppress future bot messages for that mobile and dealer |

The two-month threshold and priority mapping live in dealer config, not code.

**Which leads count as non-working hours**
- Each dealer has working hours per weekday, a timezone (default `Asia/Kolkata`), weekly offs and a holiday calendar.
- A lead is NWH if `lead.created_at` falls outside those windows, including the last 15 minutes before close.
- The callback day in the template comes from the same calendar (a Saturday-night lead before a Sunday off says "Monday").

## Success metrics

| Type | Metric | Why |
|---|---|---|
| North star | Visit and booking rate of NWH leads, bot vs holdout | Proves the bot creates sales |
| Funnel | Sent → Delivered → Read → Replied → Qualified % | Shows where the drop-off is |
| Speed | Lead creation to first touch; shift start to first call on hot leads | Speed to lead is the core promise |
| Quality | % of bot-qualified leads marked junk or lost within 7 days | Catches false qualification |
| Guardrail | Opt-out, block/report rate, WhatsApp quality rating | A low rating throttles the dealer's number |
| Cost | Cost per qualified lead | Justifies spend to dealers |

Hold out ~10% of eligible NWH leads per dealer for the first 4–6 weeks to measure lift.

## Architecture

Meta never calls CarWale directly. Inbound events go Customer → Meta → Gupshup → our webhook (the callback URL set in the Gupshup dashboard).

```mermaid
flowchart LR
  subgraph L2R["L2R (LMS)"]
    LS["Lead Service"] -->|LeadCreated event| BUS[("Event bus / queue")]
    L2RAPI["L2R Lead API<br/>dispose · stage · task · comment"]
  end
  subgraph BOT["WhatsApp Qualifier Service"]
    EL["Eligibility worker"]
    SND["Sender (outbox + retry)"]
    WH["Webhook API<br/>auth, validate, ack 200"]
    Q[("Inbound queue")]
    PR["Reply processor"]
    SYNC["L2R sync worker<br/>retry + DLQ"]
    DB[("Postgres")]
  end
  GS["Gupshup"] <--> META["Meta WhatsApp Cloud API"] <--> CUST(("Customer"))
  BUS --> EL --> SND -->|template API| GS
  SND --> DB
  GS -->|callback URL| WH --> Q --> PR --> SYNC --> L2RAPI
  PR --> DB
```

- **Event-driven:** L2R lead creation never waits on Gupshup; sends retry from an outbox.
- **Ack-then-process:** the webhook authenticates, stores the raw event and returns 200 fast. Slow L2R calls happen in workers.

## End-to-end sequence

```mermaid
sequenceDiagram
  autonumber
  participant L as L2R
  participant B as Qualifier Service
  participant G as Gupshup
  participant M as Meta
  participant C as Customer
  L->>B: LeadCreated (leadId, dealerId, mobile, model, createdAt)
  B->>B: Eligibility check
  B->>G: POST template msg (templateId, params, postbacks)
  G-->>B: 202 {messageId: gsId}
  B->>B: Save gsId → leadId (synchronously)
  G->>M: Send template
  M->>C: Template with quick-reply buttons
  G-->>B: message-event callbacks (sent/delivered/read)
  C->>M: Taps "1 Month"
  M->>G: Inbound reply
  G->>B: quick_reply, postbackText=TIMELINE_1M, context.gsId
  B-->>G: 200 OK
  B->>B: Dedupe, map, check mobile, apply rule
  B->>L: Dispose (Connected, WhatsApp Bot) + stage → Qualification
  B->>G: Session message: "Thanks! Sathya will call you tomorrow."
```

Gupshup delays inbound events by about 5 seconds, but we still write the `gsId → lead_id` mapping synchronously and retry a lookup miss instead of dropping it.

## Data model

```text
dealer_config
  dealer_id PK, enabled, gupshup_app_id, template_id, template_lang,
  timezone, working_hours JSONB, holidays DATE[], pre_close_buffer_min,
  qualify_max_days (60), rep_name, rep_phone, holdout_pct, updated_at

wa_message                          -- one row per template sent
  id PK, lead_id, dealer_id, mobile_e164, template_id,
  gs_message_id UNIQUE, wamid UNIQUE NULL,
  status  -- PENDING|SENT|DELIVERED|READ|REPLIED|FAILED|EXPIRED
  fail_reason, sent_at, delivered_at, read_at, reply_code, replied_at,
  outcome -- QUALIFIED|NURTURE|HANDOFF|NO_RESPONSE|OPT_OUT
  l2r_sync_status, l2r_attempts, l2r_last_error

wa_event                            -- raw inbound log, used for dedupe
  event_id PK, type, payload JSONB, received_at, processed_at, result

opt_out
  PK (mobile_e164, dealer_id), created_at, source
```

Mapping by `context.gsId` rather than mobile matters because one customer can have several open leads. The lead ID is also embedded in the postback (`TIMELINE_1M|L12345`) as a fallback key.

## Conversation states

```mermaid
stateDiagram-v2
  [*] --> PENDING
  PENDING --> SENT: Gupshup 202
  PENDING --> FAILED: 4xx / retries exhausted
  SENT --> DELIVERED
  SENT --> FAILED: not on WhatsApp
  DELIVERED --> READ
  SENT --> REPLIED
  DELIVERED --> REPLIED
  READ --> REPLIED
  READ --> EXPIRED: 24h no reply
  DELIVERED --> EXPIRED: 24h no reply
  REPLIED --> [*]
  FAILED --> [*]
  EXPIRED --> [*]
```

Status events can arrive out of order; transitions only move forward by rank.

## API contracts

**1. Send template (Gupshup partner API)**

```http
POST https://partner.gupshup.io/partner/app/{appId}/template/msg
Content-Type: application/x-www-form-urlencoded

source=91XXXXXXXXXX
destination=91YYYYYYYYYY
template={"id":"<templateId>","params":["Prakhar","Maruti Suzuki Brezza","Vishnu Cars TN","Sathya","tomorrow","9:30 AM","5:30 PM","9403897047"]}
postbackTexts=[{"index":0,"text":"TIMELINE_15D|L12345"},{"index":1,"text":"TIMELINE_1M|L12345"}]

→ 202 {"status":"submitted","messageId":"<gsId>"}
```

**2. Inbound webhook (our endpoint)**

```json
POST /v1/webhooks/gupshup
X-Webhook-Token: <secret>

{
  "app": "VishnuCarsTN", "timestamp": 1793904000000, "version": 2,
  "type": "message",
  "payload": {
    "id": "<inbound WhatsApp msg id>",
    "source": "91YYYYYYYYYY",
    "type": "quick_reply",
    "payload": { "text": "1 Month", "postbackText": "TIMELINE_1M|L12345" },
    "context": { "id": "<wamid of template>", "gsId": "<gsId of template>" }
  }
}
```

Delivery/read updates arrive as `type: "message-event"`. Gupshup's Enterprise API uses a different shape; confirm against a real webhook log.

**3. Qualify lead (L2R API, assumed shape)**

```json
POST /api/v1/leads/{leadId}/dispositions
Idempotency-Key: wa-<inbound msg id>

{
  "callStatus": "CONNECTED",
  "disposition": "WHATSAPP_BOT",
  "targetStage": "QUALIFICATION",
  "purchaseTimeline": "1_MONTH",
  "priority": "P1",
  "comment": "Customer chose '1 Month' on WhatsApp at 06 Nov 2026, 12:31 AM",
  "actor": "system:whatsapp-bot"
}
```

## Webhook processing

1. **Authenticate** – constant-time token compare plus source IP check; 401 on failure.
2. **Persist and ack** – insert into `wa_event` (`ON CONFLICT DO NOTHING`), enqueue, return 200.
3. **Deduplicate** on event ID.
4. **Reject stale events.**
5. **Map** via `context.gsId`, then `wamid`, then postback lead ID.
6. **Cross-check** sender mobile and that the template is one we sent.
7. **Check lead state in L2R** – act only if still in First Call and open; otherwise add a comment. Never downgrade.
8. **Apply the rule** from dealer config.
9. **Sync to L2R** with an idempotency key, then send the follow-up session message.

```python
def handle_reply(evt):
    if not wa_event.mark_processing(evt.id): return           # dedupe
    msg = wa_message.find(gs_id=evt.context.gsId) \
          or wa_message.find(wamid=evt.context.id) \
          or wa_message.find_by_postback(evt.postback)
    if msg is None:      return retry_later(evt, delay=10)     # race with send
    if msg.mobile != evt.source: return reject(evt, "MOBILE_MISMATCH")
    code = parse_code(evt.postback)                            # "TIMELINE_1M"
    lead = l2r.get_lead(msg.lead_id)
    if lead.stage != "FIRST_CALL" or lead.closed:
        return l2r.add_comment(lead.id, f"WhatsApp reply: {evt.text}")
    action = rules.for_dealer(msg.dealer_id).resolve(code)
    wa_message.record_reply(msg.id, code, action.outcome)
    outbox.enqueue("l2r_sync", msg.id, idempotency_key=f"wa-{evt.id}")
```

## Webhook security

The callback is token-authenticated. A token proves who sent the request but not that the body wasn't altered or replayed, so business checks are layered on top.

**Token hygiene:** header not URL · secrets manager · constant-time compare · HTTPS only · rotation with two valid tokens · one token per Gupshup app/dealer.

**Closing the gap:** Gupshup IP allowlist · dedupe on message ID · reject stale timestamps · context ID must match a sent template and sender must match the lead's mobile · rate limit and alert on spikes · move to HMAC signing if Gupshup offers it.

> "The webhook is authenticated with a header token stored in a secrets manager and restricted to Gupshup IPs. Every event is validated against the message ID we sent and the lead's mobile number, so a leaked token alone can't qualify leads."

## Reliability

| Concern | Approach |
|---|---|
| Lead event lost | Transactional outbox / CDC in L2R; consumer idempotent on `lead_id` |
| Gupshup send fails | Backoff + jitter on 5xx/timeouts; no retry on 4xx; failure written as L2R comment |
| Duplicate sends | Unique `(lead_id, template_id)`; one template per mobile+dealer per 24h |
| Webhook retries | Dedupe on event ID; return 200 once stored |
| L2R API down | Retry with backoff → DLQ + alert; safe replay via idempotency keys |
| Night-time spikes | Queue absorbs bursts; respect Gupshup rate limits and Meta messaging tier |
| Expiry | Job marks `EXPIRED` 24h after send and writes NO_RESPONSE |

**Observability:** funnel per dealer/template, webhook p95 < 300 ms, L2R sync lag, DLQ depth; alerts on delivery drops, 401 spikes, mapping misses; one trace ID from `LeadCreated` to L2R call.

## Edge cases

| Scenario | Handling |
|---|---|
| Two different taps | Latest wins while bot-controlled; after human action, comments only |
| Typed reply instead of tap | v1 keyword match, else resend buttons once; v2 intent classification |
| Reply after showroom opens | Processed; if already disposed, added as comment |
| Several leads, same dealer | One template per mobile+dealer per 24h; others linked as duplicates |
| Different dealers | Separate Gupshup apps/numbers, separate mappings |
| Not on WhatsApp | FAILED, normal callback flow |
| Template paused by Meta | Circuit breaker per dealer, alert ops, fall back to normal flow |
| Lead deleted/merged/reassigned | State check before acting; act on surviving lead |
| Lead 5 min before close | Pre-close buffer |
| STOP | Opt-out + confirmation, never message again |

## Compliance and cost

- **Opt-in:** Meta requires consent; capture a WhatsApp consent line on lead forms. No consent, no template.
- **DPDP Act (India):** purpose limitation, honour opt-outs, retention limit on raw payloads (e.g. 90 days).
- **Template category:** submit as Utility; budget for reclassification as Marketing.
- **Cost:** one template per lead; follow-ups are free-form session messages inside the 24h window.
- **Number quality:** clear copy, dealer branding and easy opt-out protect the messaging tier.

## Rollout and roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| Pilot (2 weeks) | 3–5 dealers, 10% holdout, shadow mode in week 1 | Mapping accuracy ~100%, no wrong-lead qualifications |
| Beta (4–6 weeks) | ~50 dealers, live L2R writes, dealer admin UI | Reply and qualified-to-visit rates beat holdout |
| GA | All opted-in dealers, 2–5% ongoing holdout | Stable quality rating, cost per qualified lead on target |

**Next:** second-step questions (budget, fuel, exchange) · test-drive booking via WhatsApp Flows · morning digest for sales executives · reuse for working-hours leads unanswered after 30 minutes.

## Open questions

- Which Gupshup API (partner/self-serve or Enterprise)?
- Does Gupshup support HMAC signing and publish stable IP ranges?
- Does the L2R API support idempotency keys and a single dispose + stage call?
- Who owns dealer working hours today?
- Is WhatsApp consent captured on every lead source?

---

Sources: Gupshup docs on [inbound messages](https://docs.gupshup.io/docs/inbound-messages), [subscriptions and notifications](https://docs.gupshup.io/docs/subscriptions-and-notifications), [quick reply callbacks](https://docs.gupshup.io/docs/text), and the [partner template message API](https://partner-docs.gupshup.io/reference/post_partner-app-appid-template-msg). L2R API shapes are assumptions for this design.
