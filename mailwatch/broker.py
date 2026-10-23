"""Fetch broker. The model nominates link IDs; this code decides. Nothing the model says
can loosen a rule here."""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import ssl
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from .attachments import kind_of
from .normalize import reg_domain

log = logging.getLogger("mailwatch.broker")

# Path/query words that mean "this link does something". A match becomes needs_human.
ACTION_RE = re.compile(
    r"(^|[/_.?=&#-])(unsubscribe|opt-?out|optout|preferences|email-?prefs|confirm(?!ation)|verify|validate|approve|accept|"
    r"decline|rsvp|respond|reset|log-?in|sign-?in|signin|logout|sign-?out|magic|activate|authori[sz]e|oauth|otp|"
    r"pay(ment|now|bill)?([/_.?=&#-]|$)|checkout|cancel|delete|remove|unlink|deactivate|vote|claim|redeem|"
    r"subscribe)", re.I)
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+=%.~]{32,}")
DOC_HOSTS = {"docs.google.com", "drive.google.com", "dropbox.com", "icloud.com", "1drv.ms", "notion.site",
             "github.com", "onedrive.live.com", "box.com", "sharepoint.com"}
LOGIN_HOSTS = re.compile(r"^(accounts\.google\.com|login\.|signin\.|auth\.|sso\.|idp\.|secure\.|myaccount\.)", re.I)
OK_TYPES = ("text/html", "text/plain", "application/pdf", "text/calendar", "image/", "application/json",
            "text/csv", "application/vnd.openxmlformats-officedocument", "application/xhtml+xml")
# Link text that reads like "show me this record". Combined with a verified sender and a
# transactional/priority message, it lets a long-ID link through the one-time-token rule.
VIEW_RE = re.compile(r"\b(view|see|track|tracking|order|orders|details?|invoice|statement|receipt|itinerary|"
                     r"reservation|booking|confirmation|bill|document|report|download|pdf|shipment|package|"
                     r"delivery|status|summary|eticket|boarding)\b", re.I)
LOGINISH_RE = re.compile(r"\b(log ?in|sign ?in|sign ?up|account|password|verify|confirm(?!ation)|activate|"
                         r"unsubscribe|manage|preferences|reset|join|accept|rsvp|claim|redeem)\b", re.I)
DEFAULT_SKIP = ("amazon.com",)
# Servers that send files as "generic binary". Accepted only if the bytes really are one of
# the allowed document types (checked by magic number, never by trusting the label).
GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/force-download",
                 "application/x-download", "application/download", "application/unknown"}


def sniff(data: bytes, filename: str = "") -> str:
    """Document kind from the first bytes, or "" if it's nothing we parse."""
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return "image"
    if head.startswith(b"PK\x03\x04"):            # zip container: only Office docs, never archives
        k = kind_of("", filename)
        if k in ("docx", "xlsx") and (b"word/" in data[:4096] or b"xl/" in data[:4096] or b"[Content_Types].xml" in data[:4096]):
            return k
        return ""
    if data.lstrip()[:15].upper().startswith(b"BEGIN:VCALENDAR"):
        return "ics"
    return ""


def _disposition_name(headers: httpx.Headers) -> str:
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", headers.get("content-disposition", ""), re.I)
    return m.group(1).strip() if m else ""


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"


@dataclass
class Context:
    """What the code (not the model) knows about the message a link came from."""
    sender_verified: bool = False
    tier: str = ""
    anchor: str = ""

    @property
    def view_link_ok(self) -> bool:
        return (self.sender_verified and self.tier in ("transactional", "priority")
                and bool(VIEW_RE.search(self.anchor)) and not LOGINISH_RE.search(self.anchor))


@dataclass
class Result:
    status: str            # fetched | needs_human | requires_login | js_required | skipped | failed
    reason: str = ""
    kind: str = ""
    data: bytes = b""
    final_url: str = ""


class Blocked(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def ip_allowed(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified:
        return False
    if isinstance(a, ipaddress.IPv4Address) and a in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT
        return False
    if isinstance(a, ipaddress.IPv6Address) and a in ipaddress.ip_network("fc00::/7"):       # ULA
        return False
    return True


def resolve_public(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise Blocked("failed", f"DNS: {e}") from e
    ips = [i[4][0] for i in infos]
    if not ips or not all(ip_allowed(ip) for ip in ips):
        raise Blocked("failed", "blocked IP (private, loopback or reserved)")
    return ips[0]


class PinnedBackend(httpcore.SyncBackend):
    """Resolve once, check, connect to that exact IP. TLS SNI and cert checks still use the hostname."""

    def __init__(self, resolver: Callable[[str, int], str] = resolve_public):
        self.resolver = resolver

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None, socket_options: Any = None) -> httpcore.NetworkStream:
        ip = self.resolver(host, port)
        return super().connect_tcp(ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)


def check_url(url: str, ctx: Context | None = None) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise Blocked("failed", "scheme not allowed")
    if u.username or u.password:
        raise Blocked("needs_human", "credentials in URL")
    if u.port not in (None, 80, 443):
        raise Blocked("failed", "non-standard port")
    host = (u.hostname or "").lower()
    if not host:
        raise Blocked("failed", "no host")
    try:
        if not ip_allowed(host):
            raise Blocked("failed", "blocked IP literal")
    except ValueError:
        pass
    pq = f"{u.path}?{u.query}"
    if ACTION_RE.search(pq):
        raise Blocked("needs_human", "action link")
    if reg_domain(host) not in DOC_HOSTS and host not in DOC_HOSTS:
        for piece in [*u.path.split("/"), *re.split(r"[&=]", u.query)]:
            if TOKEN_RE.fullmatch(piece or "") and re.search(r"\d", piece) and re.search(r"[A-Za-z]", piece):
                if ctx is not None and ctx.view_link_ok:
                    return          # verified sender's "view your order" style link
                raise Blocked("needs_human", "one-time token in URL")


def google_export(url: str) -> str:
    u = urlparse(url)
    if u.hostname == "docs.google.com":
        m = re.match(r"^/(document|spreadsheets|presentation)/d/([\w-]+)", u.path)
        if m:
            fmt = {"document": "txt", "spreadsheets": "csv", "presentation": "txt"}[m.group(1)]
            return f"https://docs.google.com/{m.group(1)}/d/{m.group(2)}/export?format={fmt}"
    if u.hostname == "drive.google.com":
        m = re.match(r"^/file/d/([\w-]+)", u.path)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


class Broker:
    def __init__(self, cfg: dict[str, Any], transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.max_bytes = int(cfg.get("max_mb", 10)) * 1024 * 1024
        self.per_run = int(cfg.get("max_per_run", 100))
        self.per_domain = int(cfg.get("max_per_domain", 5))
        self.skip = {d.lower().strip(".") for d in (cfg.get("skip_domains") or DEFAULT_SKIP)}
        self.timeout = float(cfg.get("timeout_s", 15))
        self._transport = transport
        self.count = 0
        self.domains: Counter[str] = Counter()
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        if self._transport is not None:
            t = self._transport
        else:
            t = httpx.HTTPTransport(verify=ssl.create_default_context(), trust_env=False)
            t._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(),
                                              network_backend=PinnedBackend(), max_connections=4)
        # New client per fetch: no cookies, no auth, no Referer. Redirects handled by hand.
        return httpx.Client(transport=t, follow_redirects=False, trust_env=False, timeout=self.timeout,
                            headers={"User-Agent": UA, "Accept": "text/html,application/pdf,text/*;q=0.9,*/*;q=0.5"})

    def skipped(self, host: str) -> bool:
        host = (host or "").lower()
        return reg_domain(host) in self.skip or host in self.skip or any(host.endswith("." + d) for d in self.skip)

    def fetch(self, url: str, ctx: Context | None = None) -> Result:
        if self.skipped(urlparse(url).hostname or ""):
            return Result("skipped", "domain excluded from fetching", final_url=url)
        try:
            check_url(url, ctx)
        except Blocked as b:
            return Result(b.status, b.reason, final_url=url)
        dom = reg_domain(urlparse(url).hostname or "")
        with self._lock:
            if self.count >= self.per_run:
                return Result("failed", "per-run fetch cap", final_url=url)
            if self.domains[dom] >= self.per_domain:
                return Result("failed", "per-domain fetch cap", final_url=url)
            self.count += 1
            self.domains[dom] += 1
        target = google_export(url)
        try:
            with self._client() as client:
                return self._get(client, target, ctx)
        except Blocked as b:
            return Result(b.status, b.reason, final_url=target)
        except httpx.TimeoutException:
            return Result("failed", "timeout", final_url=target)
        except (httpx.HTTPError, OSError) as e:
            return Result("failed", f"{type(e).__name__}", final_url=target)

    def _get(self, client: httpx.Client, url: str, ctx: Context | None = None) -> Result:
        for _hop in range(6):
            with client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    nxt = urljoin(url, r.headers.get("location", ""))
                    host = urlparse(nxt).hostname or ""
                    if LOGIN_HOSTS.match(host) or re.search(r"/(login|signin|sign-in|auth)\b", urlparse(nxt).path, re.I):
                        return Result("requires_login", "redirects to a login page", final_url=nxt)
                    if self.skipped(host):
                        return Result("skipped", "redirects to an excluded domain", final_url=nxt)
                    check_url(nxt, ctx)
                    url = nxt
                    continue
                if r.status_code in (401, 403):
                    return Result("requires_login", f"HTTP {r.status_code}", final_url=url)
                if r.status_code >= 400:
                    return Result("failed", f"HTTP {r.status_code}", final_url=url)
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                path = urlparse(url).path
                fname = _disposition_name(r.headers) or path
                generic = ctype in GENERIC_TYPES
                if not generic and not any(ctype.startswith(t) for t in OK_TYPES):
                    return Result("failed", f"type not allowed: {ctype}", final_url=url)
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        return Result("failed", "size cap", final_url=url)
                data = bytes(buf)
                if generic:
                    kind = sniff(data, fname)
                    if not kind:
                        return Result("failed", f"type not allowed: {ctype or 'unknown'} (content not a document)",
                                      final_url=url)
                else:
                    kind = kind_of(ctype, fname)
                if kind == "html" and re.search(rb"<input[^>]+type=[\"']?password", data, re.I):
                    return Result("requires_login", "login form", final_url=url)
                return Result("fetched", "", kind=kind, data=data, final_url=url)
        return Result("failed", "too many redirects", final_url=url)
