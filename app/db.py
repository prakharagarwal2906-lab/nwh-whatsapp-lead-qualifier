"""SQLite repository. Swap for Postgres in production; the SQL is portable."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .models import STATUS_RANK, TERMINAL, DealerConfig, MsgStatus, Outcome, WaMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS dealer_config (
  dealer_id TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wa_message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id TEXT NOT NULL,
  dealer_id TEXT NOT NULL,
  mobile TEXT NOT NULL,
  template_id TEXT NOT NULL,
  gs_message_id TEXT UNIQUE,
  wamid TEXT UNIQUE,
  status TEXT NOT NULL,
  fail_reason TEXT,
  sent_at TEXT, delivered_at TEXT, read_at TEXT,
  reply_code TEXT, replied_at TEXT, outcome TEXT,
  l2r_sync_status TEXT NOT NULL DEFAULT 'NONE',
  l2r_attempts INTEGER NOT NULL DEFAULT 0,
  l2r_last_error TEXT,
  UNIQUE (lead_id, template_id)
);
CREATE INDEX IF NOT EXISTS ix_msg_mobile ON wa_message (mobile, dealer_id, sent_at);
CREATE TABLE IF NOT EXISTS wa_event (
  event_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  received_at TEXT NOT NULL,
  processed_at TEXT,
  result TEXT
);
CREATE TABLE IF NOT EXISTS opt_out (
  mobile TEXT NOT NULL,
  dealer_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (mobile, dealer_id)
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


class Repo:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    # ---- dealer config -------------------------------------------------
    def upsert_dealer(self, cfg: DealerConfig) -> None:
        self._exec(
            "INSERT INTO dealer_config (dealer_id, data) VALUES (?, ?) "
            "ON CONFLICT(dealer_id) DO UPDATE SET data = excluded.data",
            (cfg.dealer_id, json.dumps(cfg.__dict__)),
        )

    def get_dealer(self, dealer_id: str) -> DealerConfig | None:
        row = self._exec("SELECT data FROM dealer_config WHERE dealer_id = ?", (dealer_id,)).fetchone()
        return DealerConfig(**json.loads(row["data"])) if row else None

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

    def create_pending(self, lead_id: str, dealer_id: str, mobile: str, template_id: str) -> int | None:
        """Returns the new row id, or None if this lead already has the template (idempotent)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO wa_message (lead_id, dealer_id, mobile, template_id, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (lead_id, dealer_id, mobile, template_id, MsgStatus.PENDING.value),
        )
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

    def _row_to_msg(self, row: sqlite3.Row | None) -> WaMessage | None:
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

    def set_sync(self, msg_id: int, status: str, error: str | None = None) -> None:
        self._exec(
            "UPDATE wa_message SET l2r_sync_status = ?, l2r_attempts = l2r_attempts + 1, "
            "l2r_last_error = ? WHERE id = ?",
            (status, error, msg_id),
        )

    def expire_stale(self, hours: int) -> list[WaMessage]:
        cutoff = now().timestamp() - hours * 3600
        rows = self._exec(
            "SELECT * FROM wa_message WHERE status IN ('SENT','DELIVERED','READ') AND sent_at IS NOT NULL"
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
