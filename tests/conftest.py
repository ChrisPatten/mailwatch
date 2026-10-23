from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import pytest

from mailwatch.config import load
from mailwatch.llm import Models


@pytest.fixture
def cfg(tmp_path: Path):
    home = tmp_path / "mw"
    out = tmp_path / "agent" / "data" / "mail"
    wl = tmp_path / "agent" / "memory" / "email_watchlist.md"
    wl.parent.mkdir(parents=True)
    wl.write_text(textwrap.dedent("""\
        # Email Watchlist
        ## Active Items
        ### Verizon
        - **Watch for:** Verizon (verizon.com) bills, payment confirmations, autopay notices.
        - **Added:** 2026-10-06
        ### Acme Bank
        - **Watch for:** Acme Bank (acmebank.example) statements.
        """))
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(f"""\
        [paths]
        home = "{home}"
        out = "{out}"
        watchlist = "{wl}"
        [fetch]
        enabled = true
        [keys]
        openai = "env://MW_TEST_OPENAI"
        anthropic = "env://MW_TEST_ANTHROPIC"
        [owner]
        name = "Alex Example"
        addresses = ["owner@example.com", "owner.alt@example.com"]
        exclude_senders = ["agent@example.com"]
        context = "Test owner."
        [[accounts]]
        id = "icloud"
        kind = "icloud"
        host = "imap.example"
        username = "owner@example.com"
        secret = "env://MW_TEST_PW"
        skip_folders = ["Deleted Messages"]
        """))
    return load(p)


def make_msg(subject: str, frm: str, body: str, html: str | None = None, dmarc: str = "pass",
             to: str = "owner@example.com", headers: dict[str, str] | None = None,
             when: datetime | None = None) -> bytes:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = (when or datetime.now()).strftime("%a, %d %b %Y %H:%M:%S -0400")
    m["Message-ID"] = f"<{abs(hash((subject, frm, body)))}@test>"
    dom = frm.split("@")[-1].rstrip(">")
    m["Authentication-Results"] = f"mx.icloud.com; spf={dmarc}; dkim={dmarc}; dmarc={dmarc} header.from={dom}"
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    return bytes(m)


class FakeIMAP:
    """Stands in for ReadOnlyIMAP. Records every call so tests can assert read-only use."""

    def __init__(self, folders: dict[str, list[tuple[int, bytes, datetime]]], uidvalidity: int = 7,
                 since_broken: bool = False):
        self.folders = folders
        self.uv = uidvalidity
        self.current = ""
        self.calls: list[str] = []
        self.since_broken = since_broken
        self.is_gmail = False

    def list_folders(self):
        self.calls.append("LIST")
        flags = {"INBOX": (), "Sent Messages": (b"\\Sent",), "Junk": (b"\\Junk",), "Deleted Messages": (b"\\Trash",)}
        return [(flags.get(n, ()), n) for n in self.folders]

    def examine(self, folder: str):
        self.calls.append(f"EXAMINE {folder}")
        self.current = folder
        return {b"UIDVALIDITY": self.uv, b"EXISTS": len(self.folders[folder])}

    def _msgs(self):
        return self.folders[self.current]

    def uids_since(self, since):
        self.calls.append("SEARCH SINCE")
        if self.since_broken:
            return []
        cut = datetime.combine(since, datetime.min.time())
        return sorted(u for u, _, d in self._msgs() if d >= cut)

    def uids_above(self, last):
        self.calls.append("SEARCH UID")
        return sorted(u for u, _, _ in self._msgs() if u > last)

    def uids_all(self):
        self.calls.append("SEARCH ALL")
        return sorted(u for u, _, _ in self._msgs())

    def gmail_search(self, q):
        return []

    def fetch(self, uids, items):
        self.calls.append("FETCH " + " ".join(items))
        assert all(("PEEK" in i or not i.startswith("BODY")) for i in items), items
        out = {}
        for u, raw, d in self._msgs():
            if u in uids:
                out[u] = {b"RFC822.SIZE": len(raw), b"INTERNALDATE": d, b"BODY[]": raw}
        return out

    def logout(self):
        self.calls.append("LOGOUT")


class FakeLLM:
    """httpx MockTransport that plays the OpenAI Responses API from a handler(kind, user) -> dict."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def h(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            self.requests.append(body)
            kind = body["text"]["format"]["name"]
            user = body["input"][1]["content"]
            out = self.handler(kind, user)
            return httpx.Response(200, json={"output_text": json.dumps(out),
                                             "usage": {"input_tokens": 1000, "output_tokens": 100}})
        return httpx.MockTransport(h)


def models_with(cfg, fake: FakeLLM) -> Models:
    import os
    os.environ["MW_TEST_OPENAI"] = "sk-test"
    return Models(cfg.models, cfg.keys, transport=fake.transport())


def default_handler(kind: str, user: str) -> dict[str, Any]:
    if kind == "triage":
        tier = "transactional" if "$" in user else "personal"
        fetch = [{"link_id": "L1", "why": "statement"}] if "[L1]" in user else []
        return {"tier": tier, "importance": 3, "reason": "test", "watch_match": "", "fetch": fetch}
    empty = {k: [] for k in ("obligations", "money", "events", "shipments", "action_items", "commitments",
                             "questions_for_owner", "people", "artifacts")}
    ex = {"summary": "Test summary. See https://evil.example/x for details.", "category": "bill", **empty,
          "injection_suspected": False, "injection_quote": ""}
    if "$142.18" in user:
        ex["obligations"] = [{"payee": "Verizon", "amount": "$142.18", "due_date": "2026-11-09",
                              "account_ref": "", "autopay": "yes"},
                             {"payee": "Verizon", "amount": "$9,999.00", "due_date": "2026-11-09",
                              "account_ref": "", "autopay": "no"}]
    return ex
