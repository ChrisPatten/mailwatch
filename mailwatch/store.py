"""SQLite state. Content lives in raw/ files; the DB holds pointers, stages and counts."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
  account TEXT, folder TEXT, uidvalidity INTEGER, last_uid INTEGER, role TEXT,
  updated_at REAL, PRIMARY KEY (account, folder)
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, message_id TEXT, account TEXT, folder TEXT,
  role TEXT, date TEXT, internal_ts REAL, from_addr TEXT, subject TEXT, thread_key TEXT,
  raw_path TEXT, size INTEGER, truncated INTEGER DEFAULT 0, gm_labels TEXT, gm_thrid TEXT,
  stage TEXT, tier TEXT, importance INTEGER, attempts INTEGER DEFAULT 0, error TEXT,
  first_seen REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_messages_stage ON messages(stage);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_key);
CREATE TABLE IF NOT EXISTS locations (
  msg_id TEXT, account TEXT, folder TEXT, uidvalidity INTEGER, uid INTEGER, internal_ts REAL,
  PRIMARY KEY (account, folder, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS links (
  msg_id TEXT, link_id TEXT, url TEXT, anchor TEXT, domain TEXT, position TEXT, tracked INTEGER,
  status TEXT, reason TEXT, artifact_id TEXT, PRIMARY KEY (msg_id, link_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, msg_id TEXT, kind TEXT, source TEXT, name TEXT, path TEXT,
  text_path TEXT, size INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS correspondents (
  address TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, count INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, finished REAL, stats TEXT
);
CREATE TABLE IF NOT EXISTS faults (
  key TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, count INTEGER, detail TEXT,
  notified_at REAL, open INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS labels (
  msg_id TEXT PRIMARY KEY, tier TEXT, gold TEXT, labelled_at REAL
);
"""

STAGES = ["ingested", "normalized", "triaged", "fetched", "extracted", "done"]


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- generic -----------------------------------------------------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.db.execute(sql, tuple(args)))

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self._lock:
            self.db.execute(sql, tuple(args))
            self.db.commit()

    # -- folders -----------------------------------------------------------
    def folder(self, account: str, folder: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM folders WHERE account=? AND folder=?", (account, folder))

    def set_folder(self, account: str, folder: str, uidvalidity: int, last_uid: int, role: str) -> None:
        self.x("INSERT INTO folders VALUES (?,?,?,?,?,?) ON CONFLICT(account,folder) DO UPDATE SET "
               "uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, role=excluded.role, "
               "updated_at=excluded.updated_at", (account, folder, uidvalidity, last_uid, role, time.time()))

    # -- messages ----------------------------------------------------------
    def by_dedupe(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM messages WHERE dedupe_key=?", (key,))

    def msg(self, msg_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM messages WHERE id=?", (msg_id,))

    def insert_message(self, **f: Any) -> None:
        now = time.time()
        f.setdefault("first_seen", now)
        f.setdefault("updated_at", now)
        f.setdefault("stage", "ingested")
        cols = ",".join(f)
        self.x(f"INSERT INTO messages ({cols}) VALUES ({','.join('?' * len(f))})", f.values())

    def update_message(self, msg_id: str, **f: Any) -> None:
        f["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE messages SET {sets} WHERE id=?", [*f.values(), msg_id])

    def add_location(self, msg_id: str, account: str, folder: str, uidvalidity: int, uid: int, ts: float) -> None:
        self.x("INSERT OR IGNORE INTO locations VALUES (?,?,?,?,?,?)", (msg_id, account, folder, uidvalidity, uid, ts))

    def known_uids(self, account: str, folder: str, uidvalidity: int) -> set[int]:
        return {r["uid"] for r in self.q("SELECT uid FROM locations WHERE account=? AND folder=? AND uidvalidity=?",
                                          (account, folder, uidvalidity))}

    def pending(self, stage: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM messages WHERE stage=? ORDER BY internal_ts", (stage,))

    def thread_msgs(self, thread_key: str, exclude: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM messages WHERE thread_key=? AND id<>? AND stage='done' "
                      "ORDER BY internal_ts DESC LIMIT 3", (thread_key, exclude))

    # -- links / artifacts -------------------------------------------------
    def put_link(self, msg_id: str, link: dict[str, Any]) -> None:
        self.x("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?,?,?,?,?)",
               (msg_id, link["id"], link["url"], link["anchor"], link["domain"], link["position"],
                int(link.get("tracked", False)), link.get("status", "listed"), link.get("reason", ""),
                link.get("artifact_id")))

    def links(self, msg_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM links WHERE msg_id=? ORDER BY CAST(substr(link_id,2) AS INTEGER)", (msg_id,))

    def set_link(self, msg_id: str, link_id: str, **f: Any) -> None:
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE links SET {sets} WHERE msg_id=? AND link_id=?", [*f.values(), msg_id, link_id])

    def put_artifact(self, a: dict[str, Any]) -> None:
        self.x("INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
               (a["id"], a["msg_id"], a["kind"], a["source"], a.get("name", ""), a.get("path", ""),
                a.get("text_path", ""), a.get("size", 0), a.get("note", "")))

    def artifacts(self, msg_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM artifacts WHERE msg_id=? ORDER BY id", (msg_id,))

    def artifact(self, art_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM artifacts WHERE id=?", (art_id,))

    # -- correspondents ----------------------------------------------------
    def add_correspondent(self, addr: str) -> None:
        now = time.time()
        self.x("INSERT INTO correspondents VALUES (?,?,?,1) ON CONFLICT(address) DO UPDATE SET "
               "last_seen=excluded.last_seen, count=count+1", (addr.lower(), now, now))

    def is_correspondent(self, addr: str) -> bool:
        return self.one("SELECT 1 FROM correspondents WHERE address=?", (addr.lower(),)) is not None

    # -- runs / faults -----------------------------------------------------
    def start_run(self) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO runs (started) VALUES (?)", (time.time(),))
            self.db.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.x("UPDATE runs SET finished=?, stats=? WHERE id=?", (time.time(), json.dumps(stats), run_id))

    def raise_fault(self, key: str, detail: str) -> sqlite3.Row:
        now = time.time()
        self.x("INSERT INTO faults (key, first_seen, last_seen, count, detail, open) VALUES (?,?,?,1,?,1) "
               "ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen, count=count+1, "
               "detail=excluded.detail, open=1", (key, now, now, detail))
        return self.one("SELECT * FROM faults WHERE key=?", (key,))  # type: ignore[return-value]

    def clear_fault(self, key: str) -> None:
        self.x("UPDATE faults SET open=0, count=0 WHERE key=?", (key,))

    def open_faults(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM faults WHERE open=1 ORDER BY first_seen")
