"""Repository for PostgreSQL (production) or SQLite (local / tests).

Pass a `postgresql://...` URL to use Postgres, or a file path to use SQLite.
Queries are written once with `?` placeholders and translated for Postgres.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

from .models import STATUS_RANK, TERMINAL, DealerConfig, MsgStatus, Outcome, WaMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dealer_config (
  dealer_id TEXT PRIMARY KEY,
  data {json} NOT NULL
);
CREATE TABLE IF NOT EXISTS wa_message (
  id {pk},
  lead_id TEXT NOT NULL,
  dealer_id TEXT NOT NULL,
  mobile TEXT NOT NULL,
  template_id TEXT,
  root_template_id TEXT,
  flow TEXT NOT NULL DEFAULT 'NWH_QUALIFIER',
  step TEXT NOT NULL DEFAULT 'TIMELINE',
  msg_type TEXT NOT NULL DEFAULT 'template',
  gs_message_id TEXT UNIQUE,
  wamid TEXT UNIQUE,
  status TEXT NOT NULL,
  fail_reason TEXT,
  sent_at TEXT, delivered_at TEXT, read_at TEXT,
  reply_code TEXT, replied_at TEXT, outcome TEXT,
  l2r_sync_status TEXT NOT NULL DEFAULT 'NONE',
  l2r_attempts INTEGER NOT NULL DEFAULT 0,
  l2r_last_error TEXT,
  inquiry_id TEXT NOT NULL DEFAULT '',   -- a new inquiry on the same lead can get its own template
  UNIQUE (lead_id, template_id, inquiry_id)
);
CREATE INDEX IF NOT EXISTS ix_msg_mobile ON wa_message (mobile, dealer_id, sent_at);
CREATE INDEX IF NOT EXISTS ix_msg_lead ON wa_message (lead_id);
CREATE TABLE IF NOT EXISTS wa_event (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  payload {json} NOT NULL,
  received_at TEXT NOT NULL,
  processed_at TEXT,
  result TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id {pk},
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  event TEXT NOT NULL,
  lead_id TEXT,
  message_id INTEGER,
  detail {json}
);
CREATE INDEX IF NOT EXISTS ix_audit_lead ON audit_log (lead_id, id);
CREATE TABLE IF NOT EXISTS wa_interaction (
  inbound_id TEXT PRIMARY KEY,
  context_message_id TEXT,
  lead_id TEXT NOT NULL,
  dealer_id TEXT NOT NULL,
  template_id TEXT,
  flow TEXT NOT NULL,
  step TEXT NOT NULL,
  option_code TEXT NOT NULL,
  option_title TEXT,
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inter_lead ON wa_interaction (lead_id, step);
CREATE INDEX IF NOT EXISTS ix_inter_tpl ON wa_interaction (template_id, step);
CREATE TABLE IF NOT EXISTS opt_out (
  mobile TEXT NOT NULL,
  dealer_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (mobile, dealer_id)
);
"""

# Column used to sort each table newest-first when browsing.
_ORDER = {
    "dealer_config": "dealer_id",
    "wa_message": "id",
    "wa_event": "received_at",
    "wa_interaction": "received_at",
    "opt_out": "created_at",
    "audit_log": "id",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


class Repo:
    TABLES = tuple(_ORDER)

    def __init__(self, dsn: str):
        self.lock = threading.Lock()
        self.is_pg = dsn.startswith(("postgres://", "postgresql://"))
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            schema = _SCHEMA.format(pk="BIGSERIAL PRIMARY KEY", json="JSONB")
            with self.lock:
                self.conn.execute(schema)
        else:
            self.conn = sqlite3.connect(dsn, check_same_thread=False, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            schema = _SCHEMA.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", json="TEXT")
            self.conn.executescript(schema)

    @property
    def backend(self) -> str:
        return "postgresql" if self.is_pg else "sqlite"

    def _sql(self, sql: str) -> str:
        if not self.is_pg:
            return sql
        if "INSERT OR IGNORE" in sql:
            sql = sql.replace("INSERT OR IGNORE", "INSERT") + " ON CONFLICT DO NOTHING"
        return re.sub(r"\?", "%s", sql)

    def _exec(self, sql: str, params: tuple = ()):
        with self.lock:
            return self.conn.execute(self._sql(sql), params)

    def close(self) -> None:
        self.conn.close()

    # ---- dealer config -------------------------------------------------
    def upsert_dealer(self, cfg: DealerConfig) -> None:
        self._exec(
            "INSERT INTO dealer_config (dealer_id, data) VALUES (?, ?) "
            "ON CONFLICT(dealer_id) DO UPDATE SET data = excluded.data",
            (cfg.dealer_id, json.dumps(cfg.__dict__)),
        )

    def get_dealer(self, dealer_id: str) -> DealerConfig | None:
        row = self._exec("SELECT data FROM dealer_config WHERE dealer_id = ?", (dealer_id,)).fetchone()
        if not row:
            return None
        known = DealerConfig.__dataclass_fields__
        return DealerConfig(**{k: v for k, v in _loads(row["data"]).items() if k in known})

    # ---- opt-out -------------------------------------------------------
    def is_opted_out(self, mobile: str, dealer_id: str) -> bool:
        return self._exec(
            "SELECT 1 FROM opt_out WHERE mobile = ? AND dealer_id = ?", (mobile, dealer_id)
        ).fetchone() is not None

    def add_opt_out(self, mobile: str, dealer_id: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO opt_out (mobile, dealer_id, created_at) VALUES (?, ?, ?)",
            (mobile, dealer_id, now().isoformat()),
        )

    # ---- outbound messages ---------------------------------------------
    def recently_messaged(self, mobile: str, dealer_id: str, hours: int) -> bool:
        cutoff = now().timestamp() - hours * 3600
        rows = self._exec(
            "SELECT sent_at FROM wa_message WHERE mobile = ? AND dealer_id = ? AND sent_at IS NOT NULL",
            (mobile, dealer_id),
        ).fetchall()
        return any(datetime.fromisoformat(r["sent_at"]).timestamp() >= cutoff for r in rows)

    def create_pending(self, lead_id: str, dealer_id: str, mobile: str, template_id: str | None,
                       step: str = "TIMELINE", msg_type: str = "template",
                       root_template_id: str | None = None, inquiry_id: str = "") -> int | None:
        """Insert an outbound message row before sending it.

        Returns the row id, or None if this lead already got this template (idempotent).
        Session messages (template_id None) never conflict.
        """
        params = (lead_id, dealer_id, mobile, template_id, root_template_id or template_id, step, msg_type,
                  MsgStatus.PENDING.value, inquiry_id)
        cols = "lead_id, dealer_id, mobile, template_id, root_template_id, step, msg_type, status, inquiry_id"
        if self.is_pg:
            row = self._exec(
                f"INSERT INTO wa_message ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING RETURNING id", params
            ).fetchone()
            return row["id"] if row else None
        cur = self._exec(f"INSERT OR IGNORE INTO wa_message ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", params)
        return cur.lastrowid if cur.rowcount else None

    def mark_sent(self, msg_id: int, gs_message_id: str) -> None:
        self._exec(
            "UPDATE wa_message SET gs_message_id = ?, status = ?, sent_at = ? WHERE id = ?",
            (gs_message_id, MsgStatus.SENT.value, now().isoformat(), msg_id),
        )

    def mark_failed(self, msg_id: int, reason: str) -> None:
        self._exec(
            "UPDATE wa_message SET status = ?, fail_reason = ? WHERE id = ?",
            (MsgStatus.FAILED.value, reason[:500], msg_id),
        )

    @staticmethod
    def _row_to_msg(row) -> WaMessage | None:
        if row is None:
            return None
        return WaMessage(
            id=row["id"], lead_id=row["lead_id"], dealer_id=row["dealer_id"], mobile=row["mobile"],
            template_id=row["template_id"], gs_message_id=row["gs_message_id"], wamid=row["wamid"],
            status=MsgStatus(row["status"]),
            sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
            reply_code=row["reply_code"],
            outcome=Outcome(row["outcome"]) if row["outcome"] else None,
            l2r_sync_status=row["l2r_sync_status"],
            step=row["step"], msg_type=row["msg_type"], root_template_id=row["root_template_id"],
        )

    def get_message(self, msg_id: int) -> WaMessage | None:
        return self._row_to_msg(self._exec("SELECT * FROM wa_message WHERE id = ?", (msg_id,)).fetchone())

    def find_message(self, gs_id: str | None = None, wamid: str | None = None,
                     lead_id: str | None = None) -> WaMessage | None:
        for col, val in (("gs_message_id", gs_id), ("wamid", wamid), ("lead_id", lead_id)):
            if val:
                row = self._exec(
                    f"SELECT * FROM wa_message WHERE {col} = ? ORDER BY id DESC LIMIT 1", (val,)
                ).fetchone()
                if row:
                    return self._row_to_msg(row)
        return None

    def latest_open_for(self, mobile: str) -> WaMessage | None:
        return self._row_to_msg(self._exec(
            "SELECT * FROM wa_message WHERE mobile = ? AND status IN ('SENT','DELIVERED','READ') "
            "AND msg_type = 'template' ORDER BY id DESC LIMIT 1", (mobile,)
        ).fetchone())

    def advance_status(self, msg_id: int, new: MsgStatus, wamid: str | None = None,
                       reason: str | None = None) -> bool:
        """Move status forward only. Returns True if it changed."""
        msg = self.get_message(msg_id)
        if msg is None or msg.status in TERMINAL:
            return False
        if STATUS_RANK[new] <= STATUS_RANK[msg.status]:
            if wamid and not msg.wamid:
                self._exec("UPDATE wa_message SET wamid = ? WHERE id = ?", (wamid, msg_id))
            return False
        ts_col = {MsgStatus.DELIVERED: "delivered_at", MsgStatus.READ: "read_at"}.get(new)
        sets, params = ["status = ?"], [new.value]
        if ts_col:
            sets.append(f"{ts_col} = ?")
            params.append(now().isoformat())
        if wamid:
            sets.append("wamid = COALESCE(wamid, ?)")
            params.append(wamid)
        if reason:
            sets.append("fail_reason = ?")
            params.append(reason[:500])
        params.append(msg_id)
        self._exec(f"UPDATE wa_message SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return True

    def record_reply(self, msg_id: int, code: str, outcome: Outcome) -> None:
        self._exec(
            "UPDATE wa_message SET status = ?, reply_code = ?, outcome = ?, replied_at = ?, "
            "l2r_sync_status = 'PENDING' WHERE id = ?",
            (MsgStatus.REPLIED.value, code, outcome.value, now().isoformat(), msg_id),
        )

    def set_answer(self, msg_id: int, code: str, outcome: Outcome) -> None:
        """Store the customer's latest answer on a message without re-queuing an L2R sync."""
        self._exec("UPDATE wa_message SET reply_code = ?, outcome = ?, replied_at = ? WHERE id = ?",
                   (code, outcome.value, now().isoformat(), msg_id))

    def mark_replied(self, msg_id: int, code: str) -> None:
        self._exec("UPDATE wa_message SET status = 'REPLIED', reply_code = ?, replied_at = ? WHERE id = ?",
                   (code, now().isoformat(), msg_id))

    def latest_template_for(self, mobile: str) -> WaMessage | None:
        """The customer's most recent qualifier conversation, whatever its status."""
        return self._row_to_msg(self._exec(
            "SELECT * FROM wa_message WHERE mobile = ? AND msg_type = 'template' ORDER BY id DESC LIMIT 1",
            (mobile,)).fetchone())

    def find_template(self, lead_id: str) -> WaMessage | None:
        return self._row_to_msg(self._exec(
            "SELECT * FROM wa_message WHERE lead_id = ? AND msg_type = 'template' ORDER BY id DESC LIMIT 1",
            (lead_id,)).fetchone())

    def set_sync(self, msg_id: int, status: str, error: str | None = None) -> None:
        self._exec(
            "UPDATE wa_message SET l2r_sync_status = ?, l2r_attempts = l2r_attempts + 1, "
            "l2r_last_error = ? WHERE id = ?",
            (status, error, msg_id),
        )

    def expire_stale(self, hours: int) -> list[WaMessage]:
        cutoff = now().timestamp() - hours * 3600
        rows = self._exec(
            "SELECT * FROM wa_message WHERE status IN ('SENT','DELIVERED','READ') AND sent_at IS NOT NULL "
            "AND msg_type = 'template'"
        ).fetchall()
        expired = []
        for r in rows:
            if datetime.fromisoformat(r["sent_at"]).timestamp() < cutoff:
                self._exec(
                    "UPDATE wa_message SET status = 'EXPIRED', outcome = 'NO_RESPONSE', "
                    "l2r_sync_status = 'PENDING' WHERE id = ?", (r["id"],)
                )
                expired.append(self.get_message(r["id"]))
        return expired

    def pending_sync(self, max_attempts: int = 8) -> list[WaMessage]:
        rows = self._exec(
            "SELECT * FROM wa_message WHERE l2r_sync_status IN ('PENDING','FAILED') AND l2r_attempts < ?",
            (max_attempts,),
        ).fetchall()
        return [self._row_to_msg(r) for r in rows]

    # ---- interactions (one row per customer tap) ------------------------
    def add_interaction(self, row: dict) -> bool:
        """False if this inbound message was already recorded."""
        cols = ("inbound_id", "context_message_id", "lead_id", "dealer_id", "template_id", "flow", "step",
                "option_code", "option_title", "received_at")
        cur = self._exec(
            f"INSERT OR IGNORE INTO wa_interaction ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        return cur.rowcount == 1

    def latest_interaction(self, lead_id: str, step: str) -> dict | None:
        row = self._exec(
            "SELECT * FROM wa_interaction WHERE lead_id = ? AND step = ? ORDER BY received_at DESC LIMIT 1",
            (lead_id, step),
        ).fetchone()
        return dict(row) if row else None

    def engagement(self) -> list[dict]:
        """Per template, for analysis: sent, delivered, read, replied (unique per template send:
        a customer who taps twice or also types counts once), clicks against each option,
        unique customers per option, and replies apart from options (typed text)."""
        tpls = [dict(r) for r in self._exec(
            "SELECT id, lead_id, template_id, status, read_at, sent_at FROM wa_message "
            "WHERE msg_type = 'template' AND sent_at IS NOT NULL ORDER BY sent_at").fetchall()]
        inter = [dict(r) for r in self._exec(
            "SELECT lead_id, step, option_code, received_at FROM wa_interaction").fetchall()]

        def owner(i):  # the lead's template that was live when the reply arrived
            own = None
            for m in tpls:
                if m["lead_id"] == i["lead_id"] and m["sent_at"] <= i["received_at"]:
                    own = m
            return own

        by: dict[str, dict] = {}
        for m in tpls:
            t = by.setdefault(m["template_id"], {"template_id": m["template_id"], "sent": 0, "delivered": 0, "read": 0,
                                                  "_replied": set(), "option_clicks": {}, "_unique": {}, "text_replies": 0})
            t["sent"] += 1
            t["delivered"] += m["status"] in ("DELIVERED", "READ", "REPLIED")
            t["read"] += bool(m["read_at"]) or m["status"] == "REPLIED"
        for i in inter:
            m = owner(i)
            if m is None:
                continue
            t = by[m["template_id"]]
            t["_replied"].add(m["id"])
            if i["step"] == "TIMELINE":
                t["option_clicks"][i["option_code"]] = t["option_clicks"].get(i["option_code"], 0) + 1
                t["_unique"].setdefault(i["option_code"], set()).add(i["lead_id"])
            elif i["step"] == "FREE_TEXT":
                t["text_replies"] += 1
        out = []
        for t in by.values():
            replied = len(t.pop("_replied"))
            t["option_unique"] = {k: len(v) for k, v in t.pop("_unique").items()}
            t["replied"] = replied
            t["engagement_pct"] = round(100 * replied / t["delivered"], 1) if t["delivered"] else 0.0
            out.append(t)
        return sorted(out, key=lambda t: t["template_id"])

    # ---- audit log + table browsing -----------------------------------
    def add_audit(self, level: str, event: str, lead_id: str | None = None,
                  message_id: int | None = None, detail: dict | None = None) -> None:
        self._exec(
            "INSERT INTO audit_log (ts, level, event, lead_id, message_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (now().isoformat(), level, event, lead_id, message_id,
             json.dumps(detail, default=str) if detail is not None else None),
        )

    def table_counts(self) -> dict[str, int]:
        return {t: self._exec(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in self.TABLES}

    def list_rows(self, table: str, limit: int = 100, offset: int = 0,
                  lead_id: str | None = None) -> list[dict]:
        if table not in _ORDER:  # whitelist: table name is interpolated into SQL
            raise KeyError(table)
        order = f"{_ORDER[table]} DESC"
        if lead_id and table in ("wa_message", "audit_log"):
            sql = f"SELECT * FROM {table} WHERE lead_id = ? ORDER BY {order} LIMIT ? OFFSET ?"
            params = (lead_id, limit, offset)
        else:
            sql, params = f"SELECT * FROM {table} ORDER BY {order} LIMIT ? OFFSET ?", (limit, offset)
        rows = []
        for r in self._exec(sql, params).fetchall():
            row = dict(r)
            for k in ("data", "payload", "detail"):
                if k in row:
                    row[k] = _loads(row[k])
            rows.append(row)
        return rows

    # ---- inbound events (dedupe log) -----------------------------------
    def store_event(self, event_id: str, type_: str, payload: dict) -> bool:
        """Insert raw event; False if we've already seen this event id."""
        cur = self._exec(
            "INSERT OR IGNORE INTO wa_event (event_id, type, payload, received_at) VALUES (?, ?, ?, ?)",
            (event_id, type_, json.dumps(payload), now().isoformat()),
        )
        return cur.rowcount == 1

    def finish_event(self, event_id: str, result: str) -> None:
        self._exec(
            "UPDATE wa_event SET processed_at = ?, result = ? WHERE event_id = ?",
            (now().isoformat(), result, event_id),
        )

    def event_result(self, event_id: str) -> str | None:
        row = self._exec("SELECT result FROM wa_event WHERE event_id = ?", (event_id,)).fetchone()
        return row["result"] if row else None
