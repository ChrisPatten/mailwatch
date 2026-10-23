"""Code checks on model output: every number must appear in the sources, typed strings are
capped, and URLs are stripped from free text."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .schemas import Extract

URLISH = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|io|co|us|info|biz|xyz|top|ru|cn)(?:/\S*)?)", re.I)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


class Corpus:
    def __init__(self, texts: list[str]):
        raw = "\n".join(t for t in texts if t)
        self.low = raw.lower()
        self.num = re.sub(r"[$,\s€£]", "", self.low)
        self.alnum = re.sub(r"[^a-z0-9]", "", self.low)

    def amount(self, v: str) -> bool:
        n = re.sub(r"[$,\s€£]", "", v.lower())
        if not re.search(r"\d", n):
            return False
        n2 = n[:-3] if n.endswith(".00") else n
        return n in self.num or n2 in self.num

    def code(self, v: str) -> bool:
        a = re.sub(r"[^a-z0-9]", "", v.lower())
        return len(a) >= 3 and a in self.alnum

    def date(self, v: str) -> bool:
        v = v.strip()
        if not v:
            return True
        if v.lower() in self.low:
            return True
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", v)
        if not m:
            return self.code(v)
        y, mo, d = (int(x) for x in m.groups())
        try:
            date(y, mo, d)
        except ValueError:
            return False
        mon = MONTHS[mo - 1]
        forms = [f"{y}-{mo:02d}-{d:02d}", f"{mo}/{d}", f"{mo:02d}/{d:02d}", f"{mon} {d}", f"{mon[:3]} {d}",
                 f"{d} {mon}", f"{d} {mon[:3]}", f"{mon[:3]}. {d}", f"{y}{mo:02d}{d:02d}", f"{mo}-{d}-"]
        return any(f in self.low for f in forms)


def strip_urls(s: str) -> str:
    return URLISH.sub("[link removed]", s or "")


def cap(s: str, n: int) -> str:
    return strip_urls(s or "").replace("\n", " ").strip()[:n]


def ground(ex: Extract, corpus: Corpus) -> tuple[Extract, list[str]]:
    bad: list[str] = []
    obs = []
    for o in ex.obligations:
        if not corpus.amount(o.amount):
            bad.append(f"obligation amount {o.amount!r}")
            continue
        if o.due_date and not corpus.date(o.due_date):
            bad.append(f"due date {o.due_date!r}")
            o.due_date = ""
        if o.account_ref and not corpus.code(o.account_ref):
            bad.append(f"account ref {o.account_ref!r}")
            o.account_ref = ""
        o.payee = cap(o.payee, 60)
        obs.append(o)
    ex.obligations = obs
    money = []
    for m in ex.money:
        if corpus.amount(m.amount):
            m.payee = cap(m.payee, 60)
            if m.date and not corpus.date(m.date):
                m.date = ""
            money.append(m)
        else:
            bad.append(f"amount {m.amount!r}")
    ex.money = money
    for s in ex.shipments:
        for f in ("order_no", "tracking_no"):
            v = getattr(s, f)
            if v and not corpus.code(v):
                bad.append(f"{f} {v!r}")
                setattr(s, f, "")
        s.merchant, s.carrier, s.status, s.eta = (cap(s.merchant, 60), cap(s.carrier, 30), cap(s.status, 60),
                                                   cap(s.eta, 40))
    for e in ex.events:
        if e.start and not corpus.date(e.start[:10]):
            bad.append(f"event start {e.start!r}")
            e.start = ""
        e.title, e.location = cap(e.title, 120), cap(e.location, 120)
    for a in ex.action_items:
        a.what, a.by_when = cap(a.what, 200), cap(a.by_when, 40)
    for c in ex.commitments:
        c.what, c.to, c.by_when = cap(c.what, 200), cap(c.to, 80), cap(c.by_when, 40)
    ex.questions_for_owner = [cap(q, 200) for q in ex.questions_for_owner][:5]
    for p in ex.people:
        p.name, p.relation = cap(p.name, 80), cap(p.relation, 60)
        p.email = p.email.strip()[:120] if "@" in p.email and p.email.lower() in corpus.low else ""
    ex.summary = strip_urls(ex.summary)
    ex.injection_quote = strip_urls(ex.injection_quote)
    return ex, bad


def to_record_extract(ex: Extract, valid_artifacts: set[str]) -> dict[str, Any]:
    d = ex.model_dump()
    d["artifacts"] = [a for a in d["artifacts"] if a["id"] in valid_artifacts]
    for a in d["artifacts"]:
        a["note"] = cap(a["note"], 120)
    return d
