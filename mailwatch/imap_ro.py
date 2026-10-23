"""Read-only IMAP. App passwords are full-access, so this wrapper is the only thing
keeping the watcher read-only: it exposes no method that can change server state.

Allowed: LOGIN, CAPABILITY, LIST, STATUS, EXAMINE, SEARCH, FETCH (PEEK items only), NOOP, LOGOUT.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Any, Callable, TypeVar

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

T = TypeVar("T")

# Items that never set \Seen or change anything.
_FETCH_OK = re.compile(
    r"^(BODY\.PEEK\[[A-Z0-9.\s()-]*\](<\d+\.\d+>)?|RFC822\.SIZE|INTERNALDATE|FLAGS|UID|BODYSTRUCTURE|"
    r"ENVELOPE|X-GM-LABELS|X-GM-THRID|X-GM-MSGID)$",
    re.I,
)


class ReadOnlyViolation(RuntimeError):
    pass


def _retry(fn: Callable[[], T], attempts: int = 3, base: float = 0.8) -> T:
    """Retry transient failures (network, Gmail's too-many-connections/bandwidth). Not auth."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except IMAPClientError as e:
            msg = str(e).lower()
            if "authenticat" in msg or "invalid credentials" in msg or "login" in msg:
                raise
            last = e
        except (OSError, TimeoutError) as e:
            last = e
        time.sleep(base * (2 ** i))
    assert last is not None
    raise last


class ReadOnlyIMAP:
    __slots__ = ("_ReadOnlyIMAP__c", "host", "capabilities")

    def __init__(self, host: str, port: int = 993, timeout: float = 60):
        self.host = host
        self.__c = _retry(lambda: IMAPClient(host, port=port, ssl=True, timeout=timeout))
        self.capabilities: set[bytes] = set()

    # --- session ----------------------------------------------------------
    def login(self, username: str, password: str) -> None:
        self.__c.login(username, password)
        self.capabilities = set(self.__c.capabilities())

    def logout(self) -> None:
        try:
            self.__c.logout()
        except Exception:
            pass

    def noop(self) -> None:
        self.__c.noop()

    @property
    def is_gmail(self) -> bool:
        return b"X-GM-EXT-1" in self.capabilities

    # --- folders ----------------------------------------------------------
    def list_folders(self) -> list[tuple[tuple[bytes, ...], str]]:
        return [(tuple(flags), name) for flags, _delim, name in _retry(self.__c.list_folders)]

    def examine(self, folder: str) -> dict[bytes, Any]:
        """EXAMINE (read-only SELECT). Never SELECT."""
        return _retry(lambda: self.__c.select_folder(folder, readonly=True))

    # --- search -----------------------------------------------------------
    def uids_since(self, since: date) -> list[int]:
        return sorted(_retry(lambda: self.__c.search(["SINCE", since])))

    def uids_above(self, last_uid: int) -> list[int]:
        # "n:*" always returns the highest UID even if it is <= n, so filter.
        return sorted(u for u in _retry(lambda: self.__c.search(["UID", f"{last_uid + 1}:*"])) if u > last_uid)

    def uids_all(self) -> list[int]:
        return sorted(_retry(lambda: self.__c.search(["ALL"])))

    def gmail_search(self, query: str) -> list[int]:
        if not self.is_gmail:
            return []
        return sorted(_retry(lambda: self.__c.gmail_search(query)))

    # --- fetch ------------------------------------------------------------
    def fetch(self, uids: list[int], items: list[str]) -> dict[int, dict[bytes, Any]]:
        for it in items:
            if not _FETCH_OK.match(it.strip()):
                raise ReadOnlyViolation(f"fetch item {it!r} is not read-only")
        if not uids:
            return {}
        return _retry(lambda: self.__c.fetch(uids, items))

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - defensive
        raise ReadOnlyViolation(f"IMAP operation {name!r} is not allowed")


def to_ts(v: Any) -> float:
    if isinstance(v, datetime):
        return v.timestamp()
    return time.time()
