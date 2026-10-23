"""Free, code-only triage rules. Settle the easy cases before any model call."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .normalize import Normalized, reg_domain

TIERS = ["priority", "personal", "outbound", "transactional", "bulk_relevant", "marketing", "spam", "ignored"]
FULL_TIERS = {"priority", "personal", "transactional", "outbound"}
LIGHT_TIERS = {"bulk_relevant"}
INDEX_ONLY = {"marketing", "spam", "internal", "ignored"}
DEFAULT_ORDER_RE = r"\b\d{3}-\d{7}-\d{7}\b"     # Amazon-style order numbers


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
            "no_fetch_domains": [str(x).lower().strip() for x in (d.get("no_fetch_domains") or []) if str(x).strip()],
            "ignore": [r for r in (_ignore_rule(x) for x in (d.get("ignore") or [])) if r]}


@dataclass
class IgnoreRule:
    """Content rule: mail matching it is indexed as `ignored` and never sent to a model.

    from_domains  sender's registrable domain must be one of these (empty = any sender)
    all           list of groups; every group must match. A group is a list of phrases, any of which
                  may match (case-insensitive, whole words, whitespace-insensitive)
    follow_orders remember order numbers from matching mail for 30 days; later mail from the same
                  domains that mentions one is ignored too (catches "delivered" emails with no address)
    """
    name: str
    from_domains: list[str]
    groups: list[list[str]]
    follow_orders: bool = False
    order_pattern: str = DEFAULT_ORDER_RE


def _ignore_rule(x: Any) -> IgnoreRule | None:
    if not isinstance(x, dict):
        return None
    groups: list[list[str]] = []
    for g in x.get("all") or []:
        phrases = g.get("any") if isinstance(g, dict) else g
        phrases = [str(p).strip() for p in (phrases if isinstance(phrases, list) else [phrases]) if str(p).strip()]
        if phrases:
            groups.append(phrases)
    if x.get("any"):
        groups.append([str(p).strip() for p in x["any"] if str(p).strip()])
    if not groups:
        return None   # a rule with no content test would ignore everything from the domain: refuse it
    return IgnoreRule(name=str(x.get("name") or "ignore rule")[:80],
                      from_domains=[reg_domain(str(dm)) for dm in (x.get("from_domains") or [])],
                      groups=groups, follow_orders=bool(x.get("follow_orders")),
                      order_pattern=str(x.get("order_pattern") or DEFAULT_ORDER_RE))


def _squash(s: str) -> str:
    """Lowercase, punctuation that varies between senders (", ; : |") to spaces, whitespace collapsed:
    "SHREWSBURY, MA" and "Shrewsbury MA" compare equal."""
    return re.sub(r"\s+", " ", re.sub(r"[,;:|]", " ", s or "")).strip().lower()


def _phrase_re(p: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in _squash(p).split(" ") if w]
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(words) + r"(?![a-z0-9])")


def ignore_match(n: Normalized, rules: list[IgnoreRule], known_orders: set[str]) -> tuple[str, list[str]]:
    """Returns (rule name, order numbers to remember) or ("", [])."""
    dom = reg_domain(n.from_addr.rsplit("@", 1)[-1]) if "@" in n.from_addr else ""
    text = _squash(" ".join([n.subject, n.text, n.quoted]))
    for r in rules:
        if r.from_domains and dom not in r.from_domains:
            continue
        orders = sorted(set(re.findall(r.order_pattern, " ".join([n.subject, n.text])))) if r.follow_orders else []
        if all(any(_phrase_re(p).search(text) for p in g) for g in r.groups):
            return r.name, orders
        if r.follow_orders and known_orders.intersection(orders):
            return f"{r.name} (order follow-up)", orders
    return "", []


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
