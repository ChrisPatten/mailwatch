"""Model output schemas. JSON Schema subset that both OpenAI strict mode and Anthropic
output_config accept: every property required, additionalProperties false, no length or
range keywords, no nullable unions (empty string means "none")."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


S = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


def _arr(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


TIER_ENUM = ["priority", "personal", "transactional", "bulk_relevant", "marketing", "spam"]
CATEGORIES = ["bill", "statement", "receipt", "order", "shipment", "appointment", "travel", "event",
              "account_notice", "school", "personal", "request", "other"]

TRIAGE_SCHEMA = _obj({
    "tier": {"type": "string", "enum": TIER_ENUM},
    "importance": INT,
    "reason": S,
    "watch_match": S,
    "fetch": _arr(_obj({"link_id": S, "why": S})),
})

EXTRACT_SCHEMA = _obj({
    "summary": S,
    "category": {"type": "string", "enum": CATEGORIES},
    "obligations": _arr(_obj({"payee": S, "amount": S, "due_date": S, "account_ref": S,
                              "autopay": {"type": "string", "enum": ["yes", "no", "unknown"]}})),
    "money": _arr(_obj({"amount": S, "kind": {"type": "string", "enum": ["due", "paid", "charged", "refund", "other"]},
                        "payee": S, "date": S})),
    "events": _arr(_obj({"title": S, "start": S, "end": S, "location": S,
                         "source": {"type": "string", "enum": ["ics", "text"]}})),
    "shipments": _arr(_obj({"merchant": S, "order_no": S, "carrier": S, "tracking_no": S, "status": S, "eta": S})),
    "action_items": _arr(_obj({"what": S, "by_when": S, "owner": {"type": "string", "enum": ["owner", "other"]}})),
    "commitments": _arr(_obj({"what": S, "to": S, "by_when": S})),
    "questions_for_owner": _arr(S),
    "people": _arr(_obj({"name": S, "email": S, "relation": S})),
    "artifacts": _arr(_obj({"id": S, "note": S})),
    "injection_suspected": BOOL,
    "injection_quote": S,
})

ASK_SCHEMA = _obj({"answer": S, "sources": _arr(S), "injection_suspected": BOOL})


# --- validators (code-side caps; schema keywords can't carry them) -----------

def _cap(v: str, n: int) -> str:
    return (v or "").strip()[:n]


class FetchPick(BaseModel):
    link_id: str
    why: str = ""

    @field_validator("why")
    @classmethod
    def _w(cls, v: str) -> str:
        return _cap(v, 120)


class Triage(BaseModel):
    tier: Literal["priority", "personal", "transactional", "bulk_relevant", "marketing", "spam"]
    importance: int = Field(ge=1, le=5)
    reason: str
    watch_match: str = ""
    fetch: list[FetchPick] = []

    @field_validator("importance", mode="before")
    @classmethod
    def _imp(cls, v: Any) -> int:
        return max(1, min(5, int(v)))

    @field_validator("reason")
    @classmethod
    def _r(cls, v: str) -> str:
        return _cap(v, 140)


class Obligation(BaseModel):
    payee: str
    amount: str
    due_date: str
    account_ref: str = ""
    autopay: Literal["yes", "no", "unknown"] = "unknown"


class Money(BaseModel):
    amount: str
    kind: Literal["due", "paid", "charged", "refund", "other"]
    payee: str = ""
    date: str = ""


class Event(BaseModel):
    title: str
    start: str
    end: str = ""
    location: str = ""
    source: Literal["ics", "text"] = "text"


class Shipment(BaseModel):
    merchant: str = ""
    order_no: str = ""
    carrier: str = ""
    tracking_no: str = ""
    status: str = ""
    eta: str = ""


class Action(BaseModel):
    what: str
    by_when: str = ""
    owner: Literal["owner", "other"] = "owner"


class Commitment(BaseModel):
    what: str
    to: str = ""
    by_when: str = ""


class Person(BaseModel):
    name: str = ""
    email: str = ""
    relation: str = ""


class ArtNote(BaseModel):
    id: str
    note: str = ""


class Extract(BaseModel):
    summary: str
    category: str
    obligations: list[Obligation] = []
    money: list[Money] = []
    events: list[Event] = []
    shipments: list[Shipment] = []
    action_items: list[Action] = []
    commitments: list[Commitment] = []
    questions_for_owner: list[str] = []
    people: list[Person] = []
    artifacts: list[ArtNote] = []
    injection_suspected: bool = False
    injection_quote: str = ""

    @field_validator("summary")
    @classmethod
    def _s(cls, v: str) -> str:
        return _cap(v, 600)

    @field_validator("injection_quote")
    @classmethod
    def _q(cls, v: str) -> str:
        return _cap(v, 200)

    @field_validator("category")
    @classmethod
    def _c(cls, v: str) -> str:
        return v if v in CATEGORIES else "other"


class Ask(BaseModel):
    answer: str
    sources: list[str] = []
    injection_suspected: bool = False

    @field_validator("answer")
    @classmethod
    def _a(cls, v: str) -> str:
        return _cap(v, 1500)
