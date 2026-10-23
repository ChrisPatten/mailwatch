from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def _p(s: str) -> Path:
    return Path(os.path.expanduser(s)).resolve()


@dataclass
class Account:
    id: str
    kind: str
    host: str
    username: str
    secret: str
    port: int = 993
    skip_folders: list[str] = field(default_factory=list)


@dataclass
class Config:
    home: Path
    out: Path
    watchlist: Path | None
    retention_days: int
    backfill_days: int
    lock_stale_minutes: int
    workers: int
    max_message_mb: int
    fetch: dict[str, Any]
    notify: dict[str, Any]
    models: dict[str, Any]
    keys: dict[str, str]
    owner_name: str
    owner_addresses: list[str]
    exclude_senders: list[str]
    owner_context: str
    accounts: list[Account]

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def raw(self) -> Path:
        return self.home / "raw"

    @property
    def logs(self) -> Path:
        return self.home / "logs"


def find_config(path: str | Path | None = None) -> Path:
    """Explicit path, then $MAILWATCH_CONFIG, ./config.toml, ~/.config/mailwatch/config.toml, and
    finally config.toml next to the package (an editable install in a git checkout)."""
    cands = [path, os.environ.get("MAILWATCH_CONFIG"), "config.toml", "~/.config/mailwatch/config.toml",
             Path(__file__).resolve().parents[1] / "config.toml"]
    for c in cands:
        if c and Path(os.path.expanduser(str(c))).is_file():
            return Path(os.path.expanduser(str(c))).resolve()
    raise FileNotFoundError("no config.toml found (set MAILWATCH_CONFIG or copy config.example.toml)")


def load(path: str | Path | None = None) -> Config:
    path = find_config(path)
    with open(path, "rb") as f:
        d = tomllib.load(f)
    paths, run, owner = d.get("paths", {}), d.get("run", {}), d.get("owner", {})
    home = _p(paths["home"]) if paths.get("home") else path.parent
    fetch = {"enabled": True, "max_per_run": 100, "max_per_domain": 5, "max_per_message": 5,
             "skip_domains": ["amazon.com"],
             "timeout_s": 15, "max_mb": 10, **d.get("fetch", {})}
    notify: dict[str, Any] = {"enabled": False, "format": "json", "url": "", "command": [],
                              "fault_repeat_hours": 6}
    if "agentbus" in d:  # older configs: [agentbus] == [notify] with format "agentbus"
        notify.update({"format": "agentbus", **d["agentbus"]})
    notify.update(d.get("notify", {}))
    models = {"triage": "openai:gpt-6-luna", "extract": "openai:gpt-6-luna",
              "extract_important": "openai:gpt-6-sol", "ask": "openai:gpt-6-sol",
              "openai_reasoning_effort": "low", "anthropic_thinking_budget": 0, **d.get("models", {})}
    accounts = [Account(**a) for a in d.get("accounts", [])]
    return Config(
        home=home,
        out=_p(paths["out"]) if paths.get("out") else home / "out",
        watchlist=_p(paths["watchlist"]) if paths.get("watchlist") else None,
        retention_days=int(run.get("retention_days", 7)),
        backfill_days=int(run.get("backfill_days", 7)),
        lock_stale_minutes=int(run.get("lock_stale_minutes", 30)),
        workers=max(1, int(run.get("workers", 4))),
        max_message_mb=int(run.get("max_message_mb", 40)),
        fetch=fetch,
        notify=notify,
        models=models,
        keys=d.get("keys", {}),
        owner_name=owner.get("name", ""),
        owner_addresses=[a.lower() for a in owner.get("addresses", [])],
        exclude_senders=[a.lower() for a in owner.get("exclude_senders", [])],
        owner_context=owner.get("context", "").strip(),
        accounts=accounts,
    )
