"""The run loop: ingest -> normalize -> triage -> fetch -> extract -> emit, one message at a
time, each step committed so a crash resumes where it stopped."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import emit, prompts
from .attachments import extract_text, materialize
from .broker import DEFAULT_SKIP, Broker, Context
from .config import Config
from .grounding import Corpus, ground, strip_urls, to_record_extract
from .ingest import Ingestor
from .llm import LLMError, LLMUnavailable, Models
from .normalize import Link, Normalized, normalize
from .rules import (FULL_TIERS, INDEX_ONLY, LIGHT_TIERS, ignore_match, load_overrides, load_watchlist, pre_tier,
                    watch_match)
from .schemas import EXTRACT_SCHEMA, TRIAGE_SCHEMA, Extract, Triage
from .store import Store

log = logging.getLogger("mailwatch")
MAX_ATTEMPTS = 3


class Pipeline:
    def __init__(self, cfg: Config, store: Store | None = None, models: Models | None = None,
                 broker: Broker | None = None, ingestor: Ingestor | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.models = models or Models(cfg.models, cfg.keys)
        self.broker = broker or Broker(cfg.fetch)
        self.ingestor = ingestor or Ingestor(cfg, self.store)
        self.watch = load_watchlist(cfg.watchlist)
        self.overrides = load_overrides(cfg.out / "rules.yaml")
        # Fetch exclusions: config [fetch].skip_domains (default amazon.com) + rules.yaml no_fetch_domains.
        self.broker.skip |= {d.strip(".") for d in self.overrides.get("no_fetch_domains", [])}
        self.llm_down: str = ""
        self.workers = cfg.workers
        self._stat_lock = threading.Lock()
        self.stats: dict[str, Any] = {"accounts": {}, "tiers": {}, "fetch": {}, "errors": [], "new_priority": []}

    # -- paths ----------------------------------------------------------------
    def _p(self, kind: str, msg_id: str, ext: str = ".json") -> Path:
        p = self.cfg.raw / kind / f"{msg_id}{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def load_norm(self, msg_id: str) -> Normalized:
        return Normalized.from_json(json.loads(self._p("norm", msg_id).read_text()))

    def load_work(self, msg_id: str) -> dict[str, Any]:
        p = self._p("work", msg_id)
        return json.loads(p.read_text()) if p.exists() else {}

    def save_work(self, msg_id: str, work: dict[str, Any]) -> None:
        self._p("work", msg_id).write_text(json.dumps(work, ensure_ascii=False))

    # -- run ------------------------------------------------------------------
    def run(self, skip_ingest: bool = False, no_model: bool = False) -> dict[str, Any]:
        run_id = self.store.start_run()
        t0 = time.time()
        if not skip_ingest:
            for acct in self.cfg.accounts:
                try:
                    s = self.ingestor.sync(acct)
                    self.stats["accounts"][acct.id] = s
                    self.store.clear_fault(f"login:{acct.id}")
                    for e in s["errors"]:
                        self.stats["errors"].append(f"{acct.id}: {e}")
                except Exception as e:
                    log.exception("account %s failed", acct.id)
                    self.store.raise_fault(f"login:{acct.id}", f"{type(e).__name__}: {str(e)[:160]}")
                    self.stats["errors"].append(f"{acct.id}: {e}")
            self._count_faults()
        for row in self.store.pending("ingested"):
            self._step(row["id"], self.do_normalize)
        if not no_model:
            for stage, fn in (("normalized", self.do_triage), ("triaged", self.do_fetch), ("fetched", self.do_extract)):
                self._pool(stage, fn)
            if self.llm_down:
                self.stats["errors"].append(f"model calls stopped this run: {self.llm_down}")
        for row in self.store.pending("extracted"):
            self._step(row["id"], self.do_emit)
        self._llm_fault()
        self.rebuild_digests()
        self.maybe_notify()
        self.prune()
        self.stats["llm"] = {"calls": self.models.usage.calls, "input": self.models.usage.input,
                             "output": self.models.usage.output, "cost_usd": round(self.models.usage.cost, 4),
                             "by_model": self.models.usage.by_model}
        self.stats["duration_s"] = round(time.time() - t0, 1)
        self.store.finish_run(run_id, self.stats)
        self.cfg.logs.mkdir(parents=True, exist_ok=True)
        with (self.cfg.logs / "run.jsonl").open("a") as f:
            f.write(json.dumps({"run": run_id, "at": datetime.now().isoformat(timespec="seconds"),
                                **self._summary()}) + "\n")
        return self.stats

    def _summary(self) -> dict[str, Any]:
        return {"seen": self.seen_summary(), "tiers": self.stats["tiers"], "fetch": self.stats["fetch"],
                "llm": self.stats.get("llm"), "errors": self.stats["errors"][:20],
                "duration_s": self.stats.get("duration_s")}

    def _pool(self, stage: str, fn: Any) -> None:
        """Model and fetch stages: `workers` messages at a time. Each message is still one call
        with one message in it; parallelism never mixes messages."""
        rows = self.store.pending(stage)
        if not rows or self.llm_down:
            return

        def go(msg_id: str) -> None:
            if not self.llm_down:
                self._step(msg_id, fn)
        if self.workers <= 1:
            for r in rows:
                go(r["id"])
            return
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=f"mw-{stage}") as ex:
            for f in [ex.submit(go, r["id"]) for r in rows]:
                f.result()

    def _bump(self, key: str, sub_key: str) -> None:
        with self._stat_lock:
            d = self.stats[key]
            d[sub_key] = d.get(sub_key, 0) + 1

    def _step(self, msg_id: str, fn: Any) -> None:
        try:
            fn(msg_id)
        except LLMUnavailable as e:
            self._fail(msg_id, f"llm: {e}", retry_forever=True)
            if e.status in (401, 403, 404) or "exhausted" in str(e):
                # Auth, quota, bad model name or a dead API: every further call would fail the same way.
                self.llm_down = str(e)[:160]
                self.store.raise_fault("llm-auth" if e.status in (401, 403) else "llm",
                                       f"model API unusable: {str(e)[:160]}")
        except LLMError as e:
            self._fail(msg_id, f"llm: {e}", retry_forever=True)
        except Exception as e:
            log.exception("%s failed on %s", fn.__name__, msg_id)
            self._fail(msg_id, f"{fn.__name__}: {type(e).__name__}: {str(e)[:200]}")

    def _fail(self, msg_id: str, err: str, retry_forever: bool = False) -> None:
        row = self.store.msg(msg_id)
        attempts = int(row["attempts"] or 0) + 1 if row else 1
        self.store.update_message(msg_id, attempts=attempts, error=err)
        self.stats["errors"].append(f"{msg_id}: {err}")
        if row and attempts >= MAX_ATTEMPTS and not retry_forever:
            # Give up on this stage but still count the message.
            self._emit_minimal(msg_id, err)

    # -- normalize ------------------------------------------------------------
    def do_normalize(self, msg_id: str) -> None:
        row = self.store.msg(msg_id)
        raw = Path(row["raw_path"]).read_bytes()
        norm, arts = normalize(msg_id, raw, self.cfg.raw / "art", gm_thrid=row["gm_thrid"] or "",
                               gm_labels=row["gm_labels"] or "", role=row["role"] or "")
        norm.signals["truncated"] = bool(row["truncated"])
        self._p("norm", msg_id).write_text(json.dumps(norm.to_json(), ensure_ascii=False))
        for l in norm.links:
            self.store.put_link(msg_id, {**l.__dict__, "status": "listed"})
        for a in arts:
            self.store.put_artifact(a)
        if norm.from_addr in self.cfg.owner_addresses:
            for addr in norm.to + norm.cc:
                if addr not in self.cfg.owner_addresses:
                    self.store.add_correspondent(addr)
        self.store.update_message(msg_id, stage="normalized", date=norm.date, from_addr=norm.from_addr,
                                  subject=norm.subject[:200], thread_key=norm.thread_key, attempts=0, error=None)

    # -- triage ---------------------------------------------------------------
    def do_triage(self, msg_id: str) -> None:
        n = self.load_norm(msg_id)
        wm = watch_match(n, self.watch)
        known = self.store.is_correspondent(n.from_addr)
        rr = pre_tier(n, owner=self.cfg.owner_addresses, exclude=self.cfg.exclude_senders,
                      overrides=self.overrides, watch=wm, known_sender=known)
        if rr.tier not in ("internal", "outbound") and self.overrides.get("ignore"):
            hit, orders = ignore_match(n, self.overrides["ignore"], self.store.ignored_orders())
            if hit:
                # rules.yaml `ignore`: indexed, never sent to a model. Order numbers are remembered
                # so later mail about the same order (no address in it) is ignored too.
                if orders:
                    self.store.add_ignored_orders(orders, hit)
                rr.tier, rr.reason = "ignored", f"ignore rule: {hit}"
        n.signals = rr.signals
        work: dict[str, Any] = {"rule": {"tier": rr.tier, "reason": rr.reason}}
        if rr.tier:
            tri = {"tier": rr.tier, "importance": 3 if rr.tier == "outbound" else 1, "reason": rr.reason,
                   "watch_match": wm, "fetch": [], "by": "rules"}
        else:
            raw = self.models.json("triage", prompts.triage_system(self.cfg.owner_name, self.cfg.owner_context, self.watch),
                                   prompts.triage_user(n), TRIAGE_SCHEMA, "triage", max_tokens=600)
            t = Triage.model_validate(raw)
            valid_ids = {l.id for l in n.links}
            titles = {w.title for w in self.watch}
            tri = {"tier": t.tier, "importance": t.importance, "reason": t.reason,
                   "watch_match": t.watch_match if t.watch_match in titles else "",
                   "fetch": [p.model_dump() for p in t.fetch if p.link_id in valid_ids][: int(self.cfg.fetch["max_per_message"])],
                   "by": "model"}
            if wm and not tri["watch_match"]:
                tri["watch_match"] = wm
            if tri["watch_match"] and tri["tier"] not in ("spam",):
                tri["tier"] = "priority"   # a watchlist hit is always surfaced
            if tri["tier"] in ("marketing", "spam"):
                tri["fetch"] = []
        work["triage"] = tri
        self.save_work(msg_id, work)
        self._p("norm", msg_id).write_text(json.dumps(n.to_json(), ensure_ascii=False))
        self.store.update_message(msg_id, stage="triaged", tier=tri["tier"], importance=tri["importance"],
                                  attempts=0, error=None)

    # -- fetch ----------------------------------------------------------------
    def do_fetch(self, msg_id: str) -> None:
        work = self.load_work(msg_id)
        tri = work["triage"]
        results = []
        if self.cfg.fetch.get("enabled", True) and tri["tier"] in FULL_TIERS and tri["tier"] != "outbound":
            links = {r["link_id"]: r for r in self.store.links(msg_id)}
            n = self.load_norm(msg_id)
            for pick in tri.get("fetch", []):
                lk = links.get(pick["link_id"])
                if not lk:
                    continue
                ctx = Context(sender_verified=bool(n.signals.get("sender_verified")), tier=tri["tier"],
                              anchor=lk["anchor"] or "")
                res = self.broker.fetch(lk["url"], ctx)
                self._bump("fetch", res.status)
                art_id = None
                if res.status == "fetched":
                    arts = materialize(msg_id, [{"kind": res.kind, "name": f"{lk['domain']} ({pick['link_id']})",
                                                 "data": res.data, "source": "fetch"}], self.cfg.raw / "art")
                    a = arts[0]
                    if not a["text_path"]:
                        res.status, res.reason = "js_required", "no readable text"
                    else:
                        self.store.put_artifact(a)
                        art_id = a["id"]
                self.store.set_link(msg_id, pick["link_id"], status=res.status, reason=res.reason[:120],
                                    artifact_id=art_id)
                results.append({"link_id": pick["link_id"], "status": res.status, "reason": res.reason,
                                "artifact_id": art_id})
        work["fetch"] = results
        self.save_work(msg_id, work)
        self.store.update_message(msg_id, stage="fetched", attempts=0, error=None)

    # -- extract --------------------------------------------------------------
    def do_extract(self, msg_id: str) -> None:
        work = self.load_work(msg_id)
        tri = work["triage"]
        if tri["tier"] in INDEX_ONLY:
            self.store.update_message(msg_id, stage="extracted")
            return
        n = self.load_norm(msg_id)
        docs = []
        for a in self.store.artifacts(msg_id):
            if a["text_path"] and Path(a["text_path"]).exists():
                docs.append((a["id"], f"{a['kind']} {a['name']} ({a['source']})", Path(a["text_path"]).read_text()))
        thread = []
        if n.thread_key:
            for t in self.store.thread_msgs(n.thread_key, msg_id):
                w = self.load_work(t["id"])
                s = (w.get("extract") or {}).get("summary")
                if s:
                    thread.append(f"{t['date'] or ''} {t['from_addr']}: {s}")
        role = "extract_important" if tri["tier"] in ("priority", "personal") else "extract"
        system = prompts.extract_system(self.cfg.owner_name, self.cfg.owner_context)
        user = prompts.extract_user(n, docs, thread)
        ex = None
        last_err = ""
        for _ in range(2):
            try:
                raw = self.models.json(role, system, user, EXTRACT_SCHEMA, "extract", max_tokens=2500)
                ex = Extract.model_validate(raw)
                break
            except LLMUnavailable:
                raise                       # not the message's fault: retry next run
            except (ValidationError, LLMError) as e:
                last_err = str(e)[:200]
        if ex is None:
            work["extract"] = None
            work["extraction_failed"] = last_err
        else:
            corpus = Corpus([n.subject, n.text, n.quoted, json.dumps(n.events), *[d[2] for d in docs]])
            ex, bad = ground(ex, corpus)
            if tri["tier"] != "outbound":
                ex.commitments = []
            work["extract"] = to_record_extract(ex, {d[0] for d in docs})
            work["ungrounded"] = bad
        self.save_work(msg_id, work)
        self.store.update_message(msg_id, stage="extracted", attempts=0, error=None)

    # -- emit -----------------------------------------------------------------
    def _record(self, msg_id: str, n: Normalized, work: dict[str, Any], minimal_err: str = "") -> dict[str, Any]:
        row = self.store.msg(msg_id)
        tri = work.get("triage") or {"tier": "unparsed", "importance": 1, "reason": "", "watch_match": ""}
        ex = work.get("extract") or {}
        links = [dict(r) for r in self.store.links(msg_id)]
        shown = [l for l in links if l["status"] != "listed" or l["position"] == "body"][:12]
        arts = {a["id"]: a for a in self.store.artifacts(msg_id)}
        notes = {a["id"]: a["note"] for a in ex.get("artifacts", [])}
        locs = self.store.q("SELECT DISTINCT account, folder FROM locations WHERE msg_id=?", (msg_id,))
        return {
            "id": msg_id, "account": row["account"], "folder": row["folder"],
            "locations": [f"{l['account']}:{l['folder']}" for l in locs],
            "thread_id": n.thread_key, "date": n.date or datetime.fromtimestamp(row["internal_ts"]).isoformat(),
            "from": {"name": n.from_name, "email": n.from_addr},
            "sender_verified": bool(n.signals.get("sender_verified")),
            "tier": tri["tier"], "importance": tri["importance"], "watch_match": tri.get("watch_match", ""),
            "category": ex.get("category", ""),
            "obligations": ex.get("obligations", []), "money": ex.get("money", []),
            "events": ex.get("events", []) or [{"title": e["title"], "start": e["start"], "end": e["end"],
                                                "location": e["location"], "source": "ics"} for e in n.events],
            "shipments": ex.get("shipments", []), "action_items": ex.get("action_items", []),
            "commitments": ex.get("commitments", []), "questions_for_owner": ex.get("questions_for_owner", []),
            "people": ex.get("people", []),
            "links": [{"id": l["link_id"], "anchor": l["anchor"], "domain": l["domain"], "status": l["status"],
                       "artifact_id": l["artifact_id"]} for l in shown],
            "artifacts": [{"id": a["id"], "kind": a["kind"], "source": a["source"], "name": a["name"],
                           "size": a["size"], "note": notes.get(a["id"], a["note"])} for a in arts.values()],
            "flags": {"injection_suspected": bool(ex.get("injection_suspected")),
                      "ungrounded": work.get("ungrounded", []), "hidden_chars": n.signals.get("hidden_chars", 0),
                      "display_name_mismatch": bool(n.signals.get("display_name_mismatch")),
                      "dmarc": n.signals.get("dmarc"), "truncated": bool(n.signals.get("truncated")),
                      "extraction_failed": bool(work.get("extraction_failed") or minimal_err)},
            "untrusted": {"subject": n.subject, "summary": ex.get("summary", ""),
                          "reason": strip_urls(tri.get("reason", "")),
                          "injection_quote": ex.get("injection_quote", "")},
        }

    def do_emit(self, msg_id: str, minimal_err: str = "") -> None:
        n = self.load_norm(msg_id)
        work = self.load_work(msg_id)
        tri = work.get("triage") or {}
        tier = tri.get("tier", "unparsed")
        now = datetime.now().isoformat(timespec="seconds")
        line = {"id": msg_id, "account": self.store.msg(msg_id)["account"], "folder": self.store.msg(msg_id)["folder"],
                "date": n.date, "from": n.from_addr, "subject": n.subject[:160], "tier": tier,
                "importance": tri.get("importance", 1), "emitted_at": now}
        if tier == "ignored":
            line["rule"] = tri.get("reason", "").removeprefix("ignore rule: ").removesuffix(" (order follow-up)")
        emit.append_index(self.cfg.out, line)
        stale = self.cfg.out / "records" / emit.day_of(line) / f"{msg_id}.json"
        if tier in INDEX_ONLY and stale.exists():
            stale.unlink()
        if tier not in INDEX_ONLY:
            rec = self._record(msg_id, n, work, minimal_err)
            rec["emitted_at"] = now
            emit.write_record(self.cfg.out, rec)
            if tier == "priority":
                with self._stat_lock:
                    self.stats["new_priority"].append(msg_id)
        self._bump("tiers", tier)
        self.store.update_message(msg_id, stage="done", attempts=0, error=minimal_err or None)

    def _emit_minimal(self, msg_id: str, err: str) -> None:
        try:
            if not self._p("norm", msg_id).exists():
                row = self.store.msg(msg_id)
                n = Normalized(msg_id=msg_id, subject=row["subject"] or "(unparsed)", from_name="",
                               from_addr=row["from_addr"] or "", to=[], cc=[], date="", message_id=row["message_id"],
                               thread_key="", text="", quoted="", links=[], attachments=[], events=[], signals={})
                self._p("norm", msg_id).write_text(json.dumps(n.to_json()))
            self.do_emit(msg_id, minimal_err=err)
        except Exception:
            log.exception("minimal emit failed for %s", msg_id)

    def reprocess(self, start: str, tier: str = "", failed_only: bool = False, domain: str = "") -> int:
        """Send done messages back through the model stages (after a key, rule or model change)."""
        from .normalize import reg_domain
        stage = {"triage": "normalized", "fetch": "triaged", "extract": "fetched"}[start]
        rows = self.store.q("SELECT id, tier, from_addr FROM messages WHERE stage='done'" + (" AND tier=?" if tier else ""),
                            (tier,) if tier else ())
        n = 0
        for r in rows:
            if domain and reg_domain((r["from_addr"] or "").rsplit("@", 1)[-1]) != reg_domain(domain):
                continue
            if failed_only and not self.load_work(r["id"]).get("extraction_failed"):
                continue
            if start in ("fetch", "extract") and not self.load_work(r["id"]).get("triage"):
                continue
            if start in ("triage", "fetch"):
                # forget earlier fetch results so the broker's current rules decide again
                self.store.x("UPDATE links SET status='listed', reason='', artifact_id=NULL WHERE msg_id=?", (r["id"],))
                self.store.x("DELETE FROM artifacts WHERE msg_id=? AND source='fetch'", (r["id"],))
            self.store.update_message(r["id"], stage=stage, attempts=0, error=None)
            n += 1
        return n

    # -- faults / digest / notify / prune --------------------------------------
    def seen_summary(self) -> dict[str, str]:
        out = {}
        for aid, s in self.stats["accounts"].items():
            server = sum(f["server"] for f in s["folders"].values())
            seen = sum(f["seen"] for f in s["folders"].values())
            out[aid] = f"{seen} / {server}"
        return out

    def _count_faults(self) -> None:
        for aid, s in self.stats["accounts"].items():
            for folder, f in s["folders"].items():
                key = f"count:{aid}:{folder}"
                if f["seen"] < f["server"]:
                    row = self.store.raise_fault(key, f"seen {f['seen']} of {f['server']} in {aid}/{folder}")
                    if row["count"] < 2:
                        self.store.x("UPDATE faults SET open=0 WHERE key=?", (key,))  # only after two runs
                else:
                    self.store.clear_fault(key)

    def _llm_fault(self) -> None:
        stuck = self.store.q("SELECT id, updated_at, error FROM messages WHERE stage NOT IN ('done') "
                             "AND error LIKE 'llm:%' AND first_seen < ?", (time.time() - 7200,))
        if stuck:
            self.store.raise_fault("llm", f"{len(stuck)} messages waiting on the model API: {stuck[0]['error'][:120]}")
        else:
            self.store.clear_fault("llm")
        if not self.llm_down and self.models.usage.calls:
            self.store.clear_fault("llm-auth")

    def rebuild_digests(self) -> None:
        faults = [dict(f) for f in self.store.open_faults()]
        seen = self.seen_summary()
        days = {date.today().isoformat()}
        for t in self.stats.get("touched_days", []):
            days.add(t)
        idx = self.cfg.out / "index"
        if idx.exists():
            cutoff = (date.today() - timedelta(days=self.cfg.retention_days)).isoformat()
            days |= {p.stem for p in idx.glob("*.jsonl") if p.stem >= cutoff}
        for d in sorted(days):
            emit.build_digest(self.cfg.out, d, seen if d == date.today().isoformat() else {}, faults)

    def maybe_notify(self) -> None:
        nc = self.cfg.notify
        if not nc.get("enabled"):
            return
        parts = []
        pri = self.stats["new_priority"]
        if pri:
            parts.append(f"{len(pri)} priority")
        repeat = float(nc.get("fault_repeat_hours", 6)) * 3600
        due = [f for f in self.store.open_faults() if not f["notified_at"] or time.time() - f["notified_at"] > repeat]
        if due:
            parts.append(f"{len(due)} watcher fault(s): " + "; ".join(f["key"] for f in due))
        if not parts:
            return
        digest = str(self.cfg.out / "digest" / f"{date.today().isoformat()}.md")
        ids = ", ".join(pri[:6])
        body = f"MAILWATCH: {', '.join(parts)}. Read {digest}" + (f" (records {ids})." if ids else ".")
        if emit.notify(nc, body, pri, [f["key"] for f in due], digest):
            for f in due:
                self.store.x("UPDATE faults SET notified_at=? WHERE key=?", (time.time(), f["key"]))

    def prune(self) -> None:
        self.store.prune_ignored_orders()
        keep_s = self.cfg.retention_days * 86400
        cutoff_day = (date.today() - timedelta(days=self.cfg.retention_days)).isoformat()
        for sub in ("records", "digest", "index"):
            base = self.cfg.out / sub
            if not base.exists():
                continue
            for p in base.iterdir():
                stem = p.stem if p.is_file() else p.name
                if len(stem) == 10 and stem < cutoff_day:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
        eml = self.cfg.raw / "eml"
        if eml.exists():
            for p in eml.iterdir():
                if p.is_dir() and p.name < cutoff_day:
                    shutil.rmtree(p)
        for sub in ("norm", "work", "art"):
            emit.prune_tree(self.cfg.raw / sub, keep_s)
        old = time.time() - keep_s
        ids = [r["id"] for r in self.store.q("SELECT id FROM messages WHERE internal_ts < ? AND stage='done'", (old,))]
        for i in ids:
            for t, col in (("links", "msg_id"), ("artifacts", "msg_id"), ("locations", "msg_id"), ("messages", "id")):
                self.store.db.execute(f"DELETE FROM {t} WHERE {col}=?", (i,))
        self.store.db.commit()
