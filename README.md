# Qualifying after-hours leads on WhatsApp

System design (product and engineering) for qualifying L2R leads that arrive outside dealer working hours.

When a lead lands in L2R outside the dealer's working hours, a templated WhatsApp message (sent through Gupshup) asks the customer when they plan to buy. If they pick a timeline of two months or less, we dispose the first call and move the lead to **Qualification** through the L2R API, so the sales executive starts the next morning with the hottest leads already sorted.

The full styled version, with all sections, is in [`docs/design.html`](docs/design.html). Open it in a browser; the diagrams there use Mermaid.

## Prototype (docs/index.html)

A clickable, three-step journey hosted on Netlify:

1. **Customer website** — the customer enquires about a vehicle with a dealer. A demo control sets the enquiry time so you can try after-hours and open-hours cases.
2. **L2R + customer's WhatsApp, side by side** — tap the options on the phone as the customer and watch L2R update. L2R is built on the L2R lead page design (lead card, inquiries, stage tracker, Follow Up form, activity feed). Every lead has one open **call**; whoever tags it first, the telecaller or the WhatsApp bot, disposes it and the next call is created. The telecaller form has Follow-up status (Spoke to customer / Customer was busy), then Interested / Not interested (with reason and, for "Not interested in buying", a sub reason), expected buying span, TD required with date and time, next call date and comments; busy needs only a comment. **Next call = 7 days before the buying span ends, or 1 day after the visit/TD, whichever is earlier** (moved off the dealer's weekly off). Lead status is one of New, Qualified, Not qualified · open, Qualified · closed, Booked, Not qualified · closed. WhatsApp shows up in the activity feed only once the customer replies ("WhatsApp has qualified the lead", "WhatsApp has created a Showroom Visit task" once both date and time are chosen). The Gupshup template call (dealer ID, lead ID, template ID, mobile, WABA number, variables) and its `messageId` are in the live log and `wa_message` table on the tracking screen.
   On the phone: the template offers 15 Days / 1 Month / 1–2 Months / More than 2 Months / Still Researching. The first three qualify the lead and ask for a visit date (next 7 open days) and then a time slot (working hours). More than 2 Months gets a check-in message and a follow-up call; Still Researching gets a help message and the lead is closed (Not interested in buying · Just researching).
3. **Tracking & engagement** — every tap is stored in `wa_interaction` with the inbound message ID, the `messageId` it replied to, and the flow, step and option. Engagement % is shown per template ID, with a funnel from sent to visit booked.

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

The Netlify site includes a function (`netlify/functions/db.mjs`) that saves the prototype's tables to Postgres in a schema called `nwh` (`leads_l2r`, `lead_calls`, `dealer_config`, `wa_message`, `wa_interaction`, `wa_event`, `audit_log`, plus a `snapshot` of the full page state). When a database is connected, everyone who opens the site sees the same data, and the header shows **Storage: Postgres (shared)**. Without one, it falls back to **Storage: this browser only**.

To connect one:
1. Create a free Postgres database (for example on neon.tech or supabase.com) and copy its connection string (`postgresql://user:pass@host/db?sslmode=require`).
2. In Netlify: **Site configuration → Environment variables → Add a variable**, key `DATABASE_URL`, value = that connection string. (If you use Netlify's own Neon database, it sets `NETLIFY_DATABASE_URL`, which the function also reads.)
3. **Deploys → Trigger deploy**. The tables are created automatically on the first request.

Query them from the Neon/Supabase SQL editor, for example `SELECT * FROM nwh.wa_interaction ORDER BY received_at DESC;`, or via `GET /api/db/tables/wa_message` on your site.

To run the Python service on the same database, set `DATABASE_URL` for it too; its tables live in the default `public` schema.

Run the API tests against a local Postgres: `TEST_DATABASE_URL=postgresql://... npm run test:api`. Run the Python tests against Postgres: `TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest -q`.

In production, replace the in-process `BackgroundTasks` with a real queue (SQS/Kafka) and set `DATABASE_URL` to Postgres; the boundaries are already split that way.

---

## Design summary

The full design (product and engineering) is in [`docs/design.html`](docs/design.html). The short version:

```mermaid
flowchart TD
  A["Website enquiry → lead in L2R"] --> B{"Dealer closed?"}
  B -- No --> Z["Sales executive calls"]
  B -- Yes --> C["Template from WABA number<br/>How soon are you planning to buy?"]
  C -- "15 Days / 1 Month / 1–2 Months" --> Q["Lead → Qualification"] --> D["Choose date<br/>(next 7 open days)"] --> T["Choose time<br/>(working-hour slots)"] --> V["Showroom Visit task in L2R"]
  C -- "More than 2 Months" --> N["Check-in message, follow-up in 60 days"]
  C -- "Still Researching" --> R["Help message, lead closed (just researching)"]
```

**Template send (Gupshup) carries:** (1) dealer ID, (2) lead ID, (3) template ID, (4) customer mobile, (5) WABA number, (6) variables: customer name, make, model, working hours. No dealer name, sales executive or virtual number in the template (virtual numbers rotate). One template only: `tpl_nwh_timeline_v1`.

**Postbacks:** every button and list row carries `NWH_QUALIFIER|STEP|OPTION|LEAD_ID`, e.g. `NWH_QUALIFIER|TIMELINE|1M|L12345`, `NWH_QUALIFIER|VISIT_DATE|2026-11-07|L12345`, `NWH_QUALIFIER|VISIT_TIME|1130|L12345`.

| Step · option | Outcome | L2R action · next message |
|---|---|---|
| Timeline · 15 Days / 1 Month | Qualified · P1 | Stage → Qualification · ask visit date |
| Timeline · 1–2 Months | Qualified · P2 | Stage → Qualification · ask visit date |
| Timeline · More than 2 Months | Nurture | Follow-up +60 days · "Thanks! We'll check in closer to your timeline…" |
| Timeline · Still Researching | Closed | Not interested · "Not interested in buying" · "Just researching" · "Please feel free to reach us at … for any queries…" |
| Visit date | Date chosen | Nothing in L2R yet · ask time slot |
| Visit time | Visit booked | Showroom Visit task created against the lead · confirmation |

**When the customer taps again** (WhatsApp keeps buttons active), based on the answer already given:

| Already selected | Then taps | What happens |
|---|---|---|
| 15 Days / 1 Month / 1–2 Months | another of these three | Thank-you confirming the update |
| 15 Days / 1 Month / 1–2 Months (qualified) | More than 2 Months / Still Researching | Lead stays qualified (never disqualified); answer noted, thanks |
| More than 2 Months | 15 Days / 1 Month / 1–2 Months | Qualified straight away (dealer panel via L2R), thanks, visit date |
| More than 2 Months | Still Researching | Confirmation first; on Yes the lead is closed (Not interested in buying · Just researching) |
| Still Researching (closed) | 15 Days / 1 Month / 1–2 Months | Reopened and qualified straight away, thanks, visit date |
| Still Researching (closed) | More than 2 Months | Reopened, next call by the buying span, thanks |

**Who works where.** L2R is the internal calling team's tool: an internal telecaller owns the lead, qualifies it and keeps making follow-up calls until the customer books (the OEM contract pays on conversion). Dealers use a separate dealer panel. There is one path to it, owned by L2R: a telecaller tags the follow-up form on a call, or the WhatsApp bot fills the same form fields through the L2R API, and when the lead becomes qualified L2R's event calls the dealer panel API. The bot never calls the dealer panel itself.

**Visits:** a visit is a task against the lead, created by WhatsApp (date + time), by the telecaller ("Visit Required? Yes") or with **Schedule Visit** (date, time, comments). **View Visit** offers Edit, Cancel and Complete; All Tasks lists tasks with Complete / Edit / Delete. The visit shows at the top and on the Visits step in orange (scheduled), red (time passed, not completed) or green (completed).

**Working hours:** BikeWale's calling team hours (Mon–Sat, 9:30 AM – 6:30 PM; `TEAM_WORKING_HOURS`) decide after-hours, the template's hours and call times; dealer hours only set visit slots.

**Repeat enquiries:** lead open + same version → no new inquiry; lead open + different version → new inquiry, and the WhatsApp qualifier goes out only after hours on an unqualified lead (`POST /internal/leads/inquiry-added`); lead closed or booked → new lead. Leads qualified on WhatsApp are marked `qualifiedBy: WHATSAPP` and tagged "WhatsApp Qualified".

**Analysis (tracking screen and `GET /internal/engagement`):** templates sent, delivered, read, replied (unique per template send, so two taps count once), clicks against each option, replies apart from options, and the text customers typed.

**When the customer types instead of tapping:** anything but "Hi" gets "Please type Hi to start the journey." "Hi" from a qualified lead gets the assigned dealer's number and the BikeWale number; "Hi" from an unqualified lead asks the buying-timeline question again with the same buttons.

**Tracking and message IDs.** The template ID is shared by every customer. Gupshup's send API returns a unique `messageId` immediately (HTTP 202), saved against the lead, dealer, template, flow and step before any reply can arrive. WhatsApp's `wamid` arrives with the first status callback. Every tap carries `context.gsId` (the `messageId` it answers) plus the postback, and is stored in `wa_interaction`. Date and time lists are session messages that keep a pointer to the template that started the flow, so engagement for every step rolls up per template (`GET /internal/engagement`).

**Tables:** `dealer_config`, `wa_message` (one row per outbound message), `wa_interaction` (one row per tap), `wa_event` (raw webhook log for dedupe), `opt_out`, `audit_log`.

**Webhook security:** header token (constant-time compare, rotation with two valid tokens), optional Gupshup IP allowlist, dedupe on event ID, stale-event rejection, and every reply must match a message we sent and the lead's mobile, so a leaked token alone can't qualify leads.

**Open questions:** which Gupshup API (partner or Enterprise callback format); whether Gupshup supports HMAC signing; L2R API support for idempotency keys and visit scheduling; who owns dealer working hours; whether WhatsApp consent is captured on every lead source.

Sources: Gupshup docs on [inbound messages](https://docs.gupshup.io/docs/inbound-messages), [subscriptions and notifications](https://docs.gupshup.io/docs/subscriptions-and-notifications), and the [partner template message API](https://partner-docs.gupshup.io/reference/post_partner-app-appid-template-msg). L2R API shapes are assumptions.
