"""Free, code-only triage rules. Settle the easy cases before any model call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .normalize import Normalized, reg_domain

TIERS = ["priority", "personal", "outbound", "transactional", "bulk_relevant", "marketing", "spam"]
FULL_TIERS = {"priority", "personal", "transactional", "outbound"}
LIGHT_TIERS = {"bulk_relevant"}
INDEX_ONLY = {"marketing", "spam", "internal"}


@dataclass
class WatchItem:
    title: str
    body: str
    domains: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)


def load_watchlist(path: Path | None) -> list[WatchItem]:
    """A markdown watchlist your agent maintains: one `### Title` section per active item. The section
    body is free text; domains in it (example.com) and the title words are matched by code."""
    if path is None or not path.exists():
        return []
    items: list[WatchItem] = []
    for block in re.split(r"^### ", path.read_text(), flags=re.M)[1:]:
        title, _, body = block.partition("\n")
        title = title.strip()
        body = body.split("\n## ")[0].strip()
        doms = sorted({reg_domain(d) for d in re.findall(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|gov|edu|io|us|co))\b",
                                                         body.lower() + " " + title.lower())})
        phrases = [p.strip().lower() for p in re.split(r"\s*/\s*|\s*\(.*?\)\s*", title) if len(p.strip()) >= 4]
        items.append(WatchItem(title=title, body=body[:800], domains=doms, phrases=phrases))
    return items


def watch_match(n: Normalized, items: list[WatchItem]) -> str:
    dom = reg_domain(n.from_addr.rsplit("@", 1)[-1]) if "@" in n.from_addr else ""
    hay = f"{n.from_name} {n.subject}".lower()
    for it in items:
        if dom and dom in it.domains:
            return it.title
        if any(re.search(rf"\b{re.escape(p)}\b", hay) for p in it.phrases):
            return it.title
    return ""


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"senders": {}, "domains": {}, "no_fetch_domains": []}
    d = yaml.safe_load(path.read_text()) or {}
    norm = lambda m: {str(k).lower(): str(v) for k, v in (m or {}).items() if str(v) in TIERS}
    return {"senders": norm(d.get("senders")), "domains": norm(d.get("domains")),
            "no_fetch_domains": [str(x).lower().strip() for x in (d.get("no_fetch_domains") or []) if str(x).strip()]}


@dataclass
class RuleResult:
    tier: str | None
    reason: str
    signals: dict[str, Any]


def pre_tier(n: Normalized, *, owner: list[str], exclude: list[str], overrides: dict[str, Any],
             watch: str, known_sender: bool) -> RuleResult:
    s = dict(n.signals)
    s["watch_match"] = watch
    s["known_sender"] = known_sender
    frm = n.from_addr
    dom = frm.rsplit("@", 1)[-1] if "@" in frm else ""

    if frm in exclude or any(a in exclude for a in n.to if frm in owner):
        return RuleResult("internal", "excluded sender (your agent's own mail)", s)
    if frm in owner or s.get("folder_role") == "sent":
        return RuleResult("outbound", "sent by the owner", s)

    ov = overrides["senders"].get(frm) or overrides["domains"].get(dom) or overrides["domains"].get(reg_domain(dom))
    if ov:
        # A spoofed sender must not ride an override up; downgrades are always fine.
        if s.get("sender_verified") or ov in ("marketing", "spam"):
            return RuleResult(ov, "rules.yaml override", s)

    if s.get("folder_role") == "junk" or "\\Spam" in s.get("gm_labels", ""):
        if not watch and not known_sender:
            return RuleResult("spam", "server junk folder", s)
    if s.get("dmarc") == "fail" and s.get("display_name_mismatch"):
        return RuleResult("spam", "DMARC fail + spoofed display name", s)

    if (not known_sender and not watch and s.get("list_unsubscribe") and s.get("esp_headers")):
        return RuleResult("marketing", "bulk headers from unknown sender", s)
    return RuleResult(None, "", s)
