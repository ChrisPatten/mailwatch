"""Ingest: pull raw RFC 822 from each server, read-only, with a verifiable count."""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta
from email.parser import BytesHeaderParser
from email.policy import default as default_policy
from pathlib import Path
from typing import Any, Callable

from .config import Account, Config
from .imap_ro import ReadOnlyIMAP, to_ts
from .secrets import resolve
from .store import Store

log = logging.getLogger("mailwatch.ingest")

BATCH = 20
SKIP_FLAGS = {b"\\Noselect", b"\\NonExistent", b"\\Trash", b"\\Drafts"}


def folder_role(name: str, flags: tuple[bytes, ...]) -> str:
    f = {x.lower() for x in flags}
    if name.upper() == "INBOX":
        return "inbox"
    for flag, role in ((b"\\sent", "sent"), (b"\\junk", "junk"), (b"\\all", "all"), (b"\\archive", "archive")):
        if flag in f:
            return role
    low = name.lower()
    if low in ("junk", "spam", "bulk mail"):
        return "junk"
    if low in ("sent", "sent messages", "sent items", "sent mail"):
        return "sent"
    return "other"


def pick_folders(acct: Account, folders: list[tuple[tuple[bytes, ...], str]], is_gmail: bool) -> list[tuple[str, str]]:
    out = []
    skip = {s.lower() for s in acct.skip_folders}
    for flags, name in folders:
        fl = {x.lower() for x in flags}
        if is_gmail or acct.kind == "gmail":
            # All Mail has everything except Spam and Trash; Spam separately.
            if b"\\all" in fl or b"\\junk" in fl:
                out.append((name, folder_role(name, flags)))
            continue
        if {x.lower() for x in SKIP_FLAGS} & fl or name.lower() in skip:
            continue
        out.append((name, folder_role(name, flags)))
    return out


def dedupe_key(raw: bytes) -> tuple[str, str]:
    hdr = BytesHeaderParser(policy=default_policy).parsebytes(raw)
    mid = str(hdr.get("Message-ID", "") or "").strip().lower()
    sep = raw.find(b"\r\n\r\n")
    body = raw[sep + 4:] if sep >= 0 else raw[raw.find(b"\n\n") + 2:]
    key = hashlib.sha256(mid.encode() + b"|" + hashlib.sha256(body).digest()).hexdigest()
    return key, mid


def _uids_since(c: ReadOnlyIMAP, since: date, exists: int) -> list[int]:
    uids = c.uids_since(since)
    if uids or not exists:
        return uids
    # iCloud sometimes answers SEARCH SINCE with nothing. Fall back to
    # date-filtering the newest 2,000 UIDs by INTERNALDATE.
    tail = c.uids_all()[-2000:]
    meta = c.fetch(tail, ["INTERNALDATE"])
    cutoff = datetime.combine(since, datetime.min.time()).timestamp()
    return sorted(u for u, d in meta.items() if to_ts(d.get(b"INTERNALDATE")) >= cutoff)


class Ingestor:
    def __init__(self, cfg: Config, store: Store, connect: Callable[[Account], ReadOnlyIMAP] | None = None):
        self.cfg, self.store = cfg, store
        self.connect = connect or self._connect

    def _connect(self, acct: Account) -> ReadOnlyIMAP:
        c = ReadOnlyIMAP(acct.host, acct.port)
        c.login(acct.username, resolve(acct.secret))
        return c

    def sync(self, acct: Account) -> dict[str, Any]:
        stats: dict[str, Any] = {"account": acct.id, "new": 0, "dupes": 0, "folders": {}, "errors": []}
        c = self.connect(acct)
        try:
            for name, role in pick_folders(acct, c.list_folders(), c.is_gmail):
                try:
                    stats["folders"][name] = self._sync_folder(c, acct, name, role, stats)
                except Exception as e:  # one bad folder shouldn't stop the account
                    log.exception("folder %s/%s failed", acct.id, name)
                    stats["errors"].append(f"{name}: {e}")
        finally:
            c.logout()
        return stats

    def _sync_folder(self, c: ReadOnlyIMAP, acct: Account, folder: str, role: str, stats: dict[str, Any]) -> dict[str, int]:
        info = c.examine(folder)
        uv = int(info.get(b"UIDVALIDITY", 0))
        exists = int(info.get(b"EXISTS", 0))
        row = self.store.folder(acct.id, folder)
        # SINCE is day-granular: (today - (N-1)) at 00:00 is always inside the N x 24h retention,
        # so pruning never makes the count check re-fetch an old message.
        window_start = date.today() - timedelta(days=max(1, min(self.cfg.backfill_days, self.cfg.retention_days) - 1))
        if row is None or int(row["uidvalidity"]) != uv:
            uids = _uids_since(c, window_start, exists)  # first run, or UIDVALIDITY reset
        else:
            uids = c.uids_above(int(row["last_uid"]))
        gm = self._gmail_tags(c) if c.is_gmail else {}
        self._fetch_store(c, acct, folder, role, uv, uids, gm, stats, window_start)
        last = max([*uids, int(row["last_uid"]) if row is not None and int(row["uidvalidity"]) == uv else 0])
        self.store.set_folder(acct.id, folder, uv, last, role)

        # Count check: every UID the server has in the window must be in the index.
        server = set(_uids_since(c, window_start, exists))
        missing = sorted(server - self.store.known_uids(acct.id, folder, uv))
        if missing:
            log.warning("%s/%s: repairing %d missing", acct.id, folder, len(missing))
            self._fetch_store(c, acct, folder, role, uv, missing, gm, stats, window_start)
        known = self.store.known_uids(acct.id, folder, uv)
        return {"server": len(server), "seen": len(server & known)}

    def _gmail_tags(self, c: ReadOnlyIMAP) -> dict[int, str]:
        tags: dict[int, str] = {}
        since = (date.today() - timedelta(days=self.cfg.backfill_days + 1)).strftime("%Y/%m/%d")
        for cat in ("promotions", "social", "updates", "forums"):
            for u in c.gmail_search(f"category:{cat} after:{since}"):
                tags[u] = cat
        return tags

    def _fetch_store(self, c: ReadOnlyIMAP, acct: Account, folder: str, role: str, uv: int,
                     uids: list[int], gm: dict[int, str], stats: dict[str, Any], window_start: date) -> None:
        if not uids:
            return
        max_bytes = self.cfg.max_message_mb * 1024 * 1024
        meta = c.fetch(uids, ["RFC822.SIZE", "INTERNALDATE"])
        # Old mail moved into a folder gets a new, higher UID; it's outside the window, so skip it.
        cutoff = datetime.combine(window_start, datetime.min.time()).timestamp()
        uids = [u for u in uids if to_ts(meta.get(u, {}).get(b"INTERNALDATE")) >= cutoff]
        gm_items = ["X-GM-LABELS", "X-GM-THRID"] if c.is_gmail else []
        normal = [u for u in uids if int(meta.get(u, {}).get(b"RFC822.SIZE", 0)) <= max_bytes]
        big = [u for u in uids if u not in set(normal)]
        for chunk, items, trunc in [(normal, ["BODY.PEEK[]"], False), (big, ["BODY.PEEK[]<0.2097152>"], True)]:
            for i in range(0, len(chunk), BATCH):
                part = chunk[i:i + BATCH]
                got = c.fetch(part, items + gm_items)
                for uid in part:
                    d = got.get(uid)
                    if not d:
                        stats["errors"].append(f"{folder} uid {uid}: empty fetch")
                        continue
                    raw = d.get(b"BODY[]") or d.get(b"BODY[]<0>") or b""
                    if not raw:
                        stats["errors"].append(f"{folder} uid {uid}: no body")
                        continue
                    m = meta.get(uid, {})
                    labels = d.get(b"X-GM-LABELS") or ()
                    self._store_raw(acct, folder, role, uv, uid, raw, to_ts(m.get(b"INTERNALDATE")),
                                    int(m.get(b"RFC822.SIZE", len(raw))), trunc,
                                    labels=[x.decode() if isinstance(x, bytes) else str(x) for x in labels],
                                    thrid=str(d.get(b"X-GM-THRID") or ""), gm_cat=gm.get(uid, ""), stats=stats)

    def _store_raw(self, acct: Account, folder: str, role: str, uv: int, uid: int, raw: bytes, ts: float,
                   size: int, truncated: bool, labels: list[str], thrid: str, gm_cat: str,
                   stats: dict[str, Any]) -> None:
        key, mid = dedupe_key(raw)
        existing = self.store.by_dedupe(key)
        if existing:
            self.store.add_location(existing["id"], acct.id, folder, uv, uid, ts)
            stats["dupes"] += 1
            return
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        msg_id = f"m_{day.replace('-', '')}_{key[:8]}"
        path = self.cfg.raw / "eml" / day / f"{msg_id}.eml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if gm_cat:
            labels = [*labels, f"category:{gm_cat}"]
        self.store.insert_message(
            id=msg_id, dedupe_key=key, message_id=mid, account=acct.id, folder=folder, role=role,
            internal_ts=ts, raw_path=str(path), size=size, truncated=int(truncated),
            gm_labels=",".join(labels), gm_thrid=thrid,
        )
        self.store.add_location(msg_id, acct.id, folder, uv, uid, ts)
        stats["new"] += 1
