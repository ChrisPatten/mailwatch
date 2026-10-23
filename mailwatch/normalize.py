"""Normalize: raw message -> clean text, numbered links, parsed attachments, sender trust.

Strips the places injections hide before any model sees the text. Plain code only.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup, Comment

from . import attachments as att

ZERO_WIDTH = re.compile("[​-‏⁠﻿­͏؜ᅟᅠ឴឵᠎ㅤ]")
BIDI = re.compile("[‪-‮⁦-⁩]")
URL_RE = re.compile(r"\bhttps?://[^\s<>\"'\])}]+", re.I)
FOOTER_WORDS = re.compile(r"unsubscribe|opt.?out|privacy|terms|preferences|view (it )?in (your )?browser|"
                          r"facebook|twitter|instagram|linkedin|youtube|tiktok|pinterest|manage (your )?(email|subscription)",
                          re.I)
ESP_HEADERS = ("Feedback-ID", "X-SG-EID", "X-Mailchimp-Campaign-ID", "X-MC-User", "X-Campaign", "X-CampaignID",
               "X-Braze-Campaign-Id", "X-MarketoID", "X-Mailgun-Tag", "X-SES-Outgoing", "X-Klaviyo-Campaign",
               "X-Iterable-Campaign", "X-Mailer-RecptId", "X-Mandrill-User", "X-PM-Message-Id", "X-Rpcampaign")
QUOTE_CAP = 4000
TEXT_CAP = 30000

# Reply/forward markers (common Apple Mail, Gmail and Outlook forms).
REPLY_RE = re.compile(
    r"^(On\s.{5,250}\bwrote:\s*$|-{2,}\s*Original Message\s*-{2,}|_{10,}\s*$|"
    r"From:\s.+\n(Sent|Date):\s.+)", re.M)
FORWARD_RE = re.compile(r"^(Begin forwarded message:|-{5,}\s*Forwarded message\s*-{5,})", re.M | re.I)
MOBILE_SIG_RE = re.compile(r"^(Sent from my \w+.*|Get Outlook for \w+.*|Sent via .{1,40})$", re.M)

# Brand display names and their real domains, for display-name spoof detection.
BRANDS: dict[str, tuple[str, ...]] = {
    "american express": ("americanexpress.com", "aexp.com", "amex.com"),
    "amex": ("americanexpress.com", "aexp.com", "amex.com"),
    "discover": ("discover.com", "discovercard.com", "discoverfinancial.com"),
    "verizon": ("verizon.com", "verizonwireless.com", "vzw.com", "verizon.net"),
    "chase": ("chase.com", "jpmorgan.com", "jpmchase.com"),
    "bank of america": ("bankofamerica.com", "bofa.com", "ealerts.bankofamerica.com"),
    "citi": ("citi.com", "citibank.com", "citicards.com"), "capital one": ("capitalone.com",),
    "wells fargo": ("wellsfargo.com",), "paypal": ("paypal.com",), "venmo": ("venmo.com",),
    "apple": ("apple.com", "icloud.com", "me.com"), "amazon": ("amazon.com", "amazon.co.uk", "amazonses.com"),
    "netflix": ("netflix.com",), "usps": ("usps.com", "usps.gov"), "fedex": ("fedex.com",), "ups": ("ups.com",),
    "irs": ("irs.gov",), "microsoft": ("microsoft.com", "outlook.com", "live.com"),
    "google": ("google.com", "gmail.com", "youtube.com"), "fidelity": ("fidelity.com",),
    "vanguard": ("vanguard.com",), "schwab": ("schwab.com",), "comcast": ("comcast.net", "xfinity.com"),
    "xfinity": ("comcast.net", "xfinity.com"), "geico": ("geico.com",),
}
TWO_LEVEL = {"co.uk", "com.au", "co.jp", "com.br", "co.nz", "org.uk", "ac.uk", "gov.uk", "co.in", "com.mx"}


def reg_domain(host: str) -> str:
    host = (host or "").lower().strip(".").split(":")[0]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


@dataclass
class Link:
    id: str
    url: str
    anchor: str
    domain: str
    position: str = "body"
    tracked: bool = False


@dataclass
class Normalized:
    msg_id: str
    subject: str
    from_name: str
    from_addr: str
    to: list[str]
    cc: list[str]
    date: str
    message_id: str
    thread_key: str
    text: str
    quoted: str
    links: list[Link]
    attachments: list[dict[str, Any]]
    events: list[dict[str, Any]]
    signals: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Normalized":
        d = dict(d)
        d["links"] = [Link(**x) for x in d.get("links", [])]
        return cls(**d)


# ---------------------------------------------------------------------------
# text cleaning
# ---------------------------------------------------------------------------

def clean_chars(s: str) -> tuple[str, int]:
    before = len(s)
    s = ZERO_WIDTH.sub("", s)
    s = BIDI.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return s, max(0, before - len(s))


def collapse(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


_COLOR_NAMES = {"white": "#ffffff", "black": "#000000", "#fff": "#ffffff", "#000": "#000000"}


def _norm_color(v: str) -> str:
    v = v.strip().lower().replace(" ", "")
    v = _COLOR_NAMES.get(v, v)
    m = re.fullmatch(r"rgba?\((\d+),(\d+),(\d+)(?:,[\d.]+)?\)", v)
    if m:
        v = "#%02x%02x%02x" % tuple(int(x) for x in m.groups())
    if re.fullmatch(r"#[0-9a-f]{3}", v):
        v = "#" + "".join(c * 2 for c in v[1:])
    return v


def _style(tag: Any) -> dict[str, str]:
    st = tag.get("style") or ""
    out = {}
    for decl in st.split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return out


def _px(v: str) -> float | None:
    m = re.match(r"^(-?[\d.]+)\s*(px|pt|em|rem|%)?$", v.strip())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "px"
    return {"px": n, "pt": n * 1.33, "em": n * 16, "rem": n * 16, "%": n * 0.16}[unit]


def _hidden_classes(soup: BeautifulSoup) -> set[str]:
    """Classes hidden by simple <style> rules, e.g. `.preheader { display:none }`."""
    hidden: set[str] = set()
    for st in soup.find_all("style"):
        css = st.get_text(" ")
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            b = body.lower().replace(" ", "")
            if any(x in b for x in ("display:none", "visibility:hidden", "opacity:0;", "font-size:0", "max-height:0")):
                for cls in re.findall(r"\.([A-Za-z0-9_-]+)", sel):
                    hidden.add(cls)
    return hidden


def _bg_of(tag: Any) -> str:
    for anc in [tag, *tag.parents]:
        if not getattr(anc, "attrs", None):
            continue
        st = _style(anc)
        for key in ("background-color", "background"):
            if key in st and st[key]:
                c = _norm_color(st[key].split()[0])
                if c.startswith("#"):
                    return c
        if anc.get("bgcolor"):
            return _norm_color(anc["bgcolor"])
    return "#ffffff"


def _is_hidden(tag: Any, hidden_cls: set[str]) -> bool:
    if tag.has_attr("hidden"):
        return True
    if hidden_cls and set(tag.get("class") or []) & hidden_cls:
        return True
    st = _style(tag)
    if st.get("display") == "none" or st.get("visibility") == "hidden":
        return True
    if "opacity" in st:
        try:
            if float(st["opacity"]) <= 0.05:
                return True
        except ValueError:
            pass
    if "font-size" in st:
        px = _px(st["font-size"])
        if px is not None and px <= 1:
            return True
    if st.get("max-height") in ("0", "0px") and "hidden" in st.get("overflow", ""):
        return True
    if (st.get("width") in ("0", "0px") or st.get("height") in ("0", "0px")) and "hidden" in st.get("overflow", ""):
        return True
    if "color" in st and tag.name != "a" and tag.find("a") is None:
        # Links are exempt: a white button label on a CSS-class background would otherwise vanish.
        c = _norm_color(st["color"])
        if c.startswith("#") and c == _bg_of(tag):
            return True
    return False


class _LinkBook:
    def __init__(self) -> None:
        self.links: list[Link] = []
        self._by_url: dict[str, str] = {}

    def add(self, href: str, anchor: str) -> str | None:
        href = htmllib.unescape(href or "").strip()
        if not re.match(r"^https?://", href, re.I):
            return None
        url, tracked = unwrap(href)
        if url in self._by_url:
            return self._by_url[url]
        lid = f"L{len(self.links) + 1}"
        host = urlparse(url).hostname or ""
        anchor = collapse(anchor)[:120] or reg_domain(host)
        self.links.append(Link(id=lid, url=url, anchor=anchor, domain=host.lower(), tracked=tracked))
        self._by_url[url] = lid
        return lid


def unwrap(url: str) -> tuple[str, bool]:
    """Unwrap known redirectors offline. Returns (url, tracked)."""
    for _ in range(3):
        u = urlparse(url)
        host = (u.hostname or "").lower()
        qs = parse_qs(u.query)
        nxt = None
        if host.endswith("google.com") and u.path in ("/url", "/url/") and ("q" in qs or "url" in qs):
            nxt = (qs.get("q") or qs.get("url"))[0]
        elif host.endswith("safelinks.protection.outlook.com") and "url" in qs:
            nxt = qs["url"][0]
        elif host in ("l.facebook.com", "lm.facebook.com", "l.instagram.com") and "u" in qs:
            nxt = qs["u"][0]
        elif host.endswith("urldefense.com") and "/v3/__" in url:
            m = re.search(r"/v3/__(.+?)__;", url)
            nxt = m.group(1) if m else None
        elif host.endswith("urldefense.proofpoint.com") and "u" in qs:
            nxt = qs["u"][0].replace("-", "%").replace("_", "/")
            nxt = unquote(nxt)
        if not nxt or not re.match(r"^https?://", nxt, re.I):
            break
        url = nxt
    host = (urlparse(url).hostname or "").lower()
    tracked = bool(re.search(r"(^|\.)(click|clicks|links?|email|e|trk|track|t|go|r|mail|em|url\d*|ablink|"
                             r"sendgrid|mailchi\.mp|list-manage|ct\.sendgrid|links\.\w+|hubspotlinks|mandrillapp|"
                             r"rs6|mkt\d+|exacttarget|bnc\.lt|lnk\.|elink|clicktime)\.", host + ".")
                   or "list-manage.com" in host or "sendgrid.net" in host or "/ls/click" in url
                   or "mandrillapp.com/track" in url)
    return url, tracked


def html_to_text(html: str, book: _LinkBook) -> tuple[str, int]:
    soup = BeautifulSoup(html, "lxml")
    hidden_cls = _hidden_classes(soup)
    removed = 0
    for t in soup(["script", "style", "head", "title", "meta", "link", "noscript", "template", "svg", "iframe", "object"]):
        t.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        removed += len(c)
        c.extract()
    for t in list(soup.find_all(True)):
        if getattr(t, "decomposed", False) or t.attrs is None:
            continue
        try:
            if _is_hidden(t, hidden_cls):
                removed += len(t.get_text())
                t.decompose()
        except Exception:
            continue
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        img.replace_with(f" {alt} " if alt and len(alt) < 80 else " ")
    for a in soup.find_all("a"):
        label = a.get_text(" ", strip=True)
        lid = book.add(a.get("href", ""), label)
        a.replace_with(f" {label} [{lid}] " if lid else f" {label} ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for blk in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"]):
        blk.insert_before("\n")
        blk.insert_after("\n")
    text = soup.get_text()
    return text, removed


def bare_links(text: str, book: _LinkBook) -> str:
    def rep(m: re.Match[str]) -> str:
        url = m.group(0).rstrip(".,;:!?")
        tail = m.group(0)[len(url):]
        lid = book.add(url, "")
        return f"[{lid}]{tail}" if lid else m.group(0)
    return URL_RE.sub(rep, text)


def split_quoted(text: str) -> tuple[str, str]:
    """New text vs quoted history. Forwards keep their forwarded body as the main text."""
    fwd = FORWARD_RE.search(text)
    rep = REPLY_RE.search(text)
    lines = text.split("\n")
    first_q = next((i for i, l in enumerate(lines) if l.startswith(">")), None)
    cut = None
    if rep:
        cut = rep.start()
    if first_q is not None:
        qpos = sum(len(l) + 1 for l in lines[:first_q])
        cut = qpos if cut is None else min(cut, qpos)
    if fwd and (cut is None or fwd.start() <= cut):
        if len(text[:fwd.start()].strip()) < 400:
            return text.strip(), ""   # a forward: the forwarded part is the content
        cut = fwd.start() if cut is None else min(cut, fwd.start())
    if cut is None or cut == 0:
        return text.strip(), ""
    return text[:cut].strip(), text[cut:].strip()


# ---------------------------------------------------------------------------
# headers / trust
# ---------------------------------------------------------------------------

def auth_verdict(msg: EmailMessage, from_addr: str) -> dict[str, Any]:
    ars = msg.get_all("Authentication-Results") or []
    top = str(ars[0]) if ars else ""   # topmost = added by our provider; lower ones are sender-controlled
    low = top.lower()
    dmarc = (re.search(r"\bdmarc=(\w+)", low) or [None, "none"])[1]
    spf = (re.search(r"\bspf=(\w+)", low) or [None, "none"])[1]
    dkim = (re.search(r"\bdkim=(\w+)", low) or [None, "none"])[1]
    hfrom = (re.search(r"header\.from=([\w.-]+)", low) or [None, ""])[1]
    from_dom = from_addr.rsplit("@", 1)[-1].lower() if "@" in from_addr else ""
    aligned = bool(hfrom) and reg_domain(hfrom) == reg_domain(from_dom)
    if not hfrom and dmarc == "pass":
        aligned = True
    return {"dmarc": dmarc, "spf": spf, "dkim": dkim, "aligned": aligned,
            "sender_verified": dmarc == "pass" and aligned, "auth_header_present": bool(top)}


def display_name_mismatch(name: str, addr: str) -> bool:
    n = (name or "").lower()
    dom = reg_domain(addr.rsplit("@", 1)[-1]) if "@" in addr else ""
    if not n or not dom:
        return False
    m = re.search(r"[\w.+-]+@([\w-]+\.[\w.-]+)", n)
    if m and reg_domain(m.group(1)) != dom:
        return True
    for brand, doms in BRANDS.items():
        if re.search(rf"(^|[^a-z]){re.escape(brand)}([^a-z]|$)", n):
            if dom not in {reg_domain(d) for d in doms}:
                return True
    return False


def bulk_signals(msg: EmailMessage) -> dict[str, Any]:
    lu = str(msg.get("List-Unsubscribe", "") or "")
    prec = str(msg.get("Precedence", "") or "").lower()
    esp = [h for h in ESP_HEADERS if msg.get(h)]
    xm = str(msg.get("X-Mailer", "") or "")
    if re.search(r"mailchimp|sendgrid|marketo|braze|klaviyo|hubspot|salesforce|exacttarget|iterable|cheetah|"
                 r"responsys|constant contact|campaign monitor|mailgun|sparkpost", xm, re.I):
        esp.append("X-Mailer")
    return {"list_unsubscribe": bool(lu), "list_id": bool(msg.get("List-Id")),
            "precedence_bulk": prec in ("bulk", "list", "junk"), "esp_headers": esp,
            "auto_submitted": str(msg.get("Auto-Submitted", "no")).lower() not in ("", "no")}


def noise_score(subject: str, from_addr: str, sig: dict[str, Any]) -> int:
    """Noise heuristic: List-Unsubscribe +3, promo subject +2, bulk sender +2."""
    s = 3 if sig.get("list_unsubscribe") else 0
    if re.search(r"\b(sale|offer|discount|deal|promo|% off|newsletter|subscribe|save \$?\d+|free shipping|"
                 r"limited time|last chance|exclusive)\b", subject, re.I):
        s += 2
    if re.search(r"(noreply|no-reply|donotreply|do-not-reply|mailer|newsletter|marketing|promo|news@|hello@|info@)",
                 from_addr, re.I):
        s += 2
    return s


def thread_key(msg: EmailMessage, gm_thrid: str) -> str:
    if gm_thrid:
        pass  # Gmail thread IDs aren't shared across accounts; prefer header chains.
    refs = str(msg.get("References", "") or "").split()
    irt = str(msg.get("In-Reply-To", "") or "").strip()
    root = (refs[0] if refs else irt) or str(msg.get("Message-ID", "") or "")
    root = root.strip().lower() or (f"gm:{gm_thrid}" if gm_thrid else "")
    return "t_" + hashlib.sha256(root.encode()).hexdigest()[:10] if root else ""


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def normalize(msg_id: str, raw: bytes, art_dir: Any, gm_thrid: str = "", gm_labels: str = "",
              role: str = "") -> tuple[Normalized, list[dict[str, Any]]]:
    """Returns (normalized, artifacts). Artifacts are written under art_dir."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
    from_name, from_addr = parseaddr(str(msg.get("From", "") or ""))
    from_addr = from_addr.lower()
    from_name, _ = clean_chars(from_name)
    subject, sub_removed = clean_chars(str(msg.get("Subject", "") or ""))
    subject = collapse(subject)[:300]
    to = [a.lower() for _, a in getaddresses([str(x) for x in msg.get_all("To", [])]) if a]
    cc = [a.lower() for _, a in getaddresses([str(x) for x in msg.get_all("Cc", [])]) if a]
    try:
        dt = parsedate_to_datetime(str(msg.get("Date"))).isoformat() if msg.get("Date") else ""
    except Exception:
        dt = ""

    book = _LinkBook()
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        fname = part.get_filename()
        if ctype == "text/calendar" or (fname or "").lower().endswith(".ics"):
            payload = part.get_payload(decode=True) or b""
            evs = att.parse_ics(payload)
            events.extend(evs)
            attachments.append({"kind": "ics", "name": fname or "invite.ics", "data": payload, "text": att.ics_text(evs)})
            continue
        if ctype in ("text/plain", "text/html") and disp != "attachment" and not fname:
            try:
                content = part.get_content()
            except Exception:
                content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            (plain_parts if ctype == "text/plain" else html_parts).append(str(content))
            continue
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append({"kind": att.kind_of(ctype, fname or ""), "name": fname or ctype.replace("/", "."),
                                "data": payload, "ctype": ctype, "inline": disp == "inline" or bool(part.get("Content-ID"))})

    removed = sub_removed
    html_text = ""
    if html_parts:
        html_text, r = html_to_text("\n".join(html_parts), book)
        removed += r
    plain = "\n\n".join(plain_parts)
    html_c = collapse(html_text)
    plain_c = collapse(plain)
    # Plain parts are often a stub ("view in browser"); use HTML unless plain is the richer one.
    body = html_c if html_c and len(plain_c) < 0.6 * len(html_c) else plain_c or html_c
    body = bare_links(body, book)
    body, r = clean_chars(body)
    removed += r
    body = MOBILE_SIG_RE.sub("", collapse(body))
    new, quoted = split_quoted(body)

    # Footer positioning for links
    n = len(book.links)
    for i, l in enumerate(book.links):
        if FOOTER_WORDS.search(l.anchor) or (n >= 4 and i >= int(n * 0.8) and len(l.anchor) < 30):
            l.position = "footer"

    arts_out = att.materialize(msg_id, attachments, art_dir)
    sig = bulk_signals(msg)
    sig.update(auth_verdict(msg, from_addr))
    sig["display_name_mismatch"] = display_name_mismatch(from_name, from_addr)
    sig["hidden_chars"] = removed
    sig["noise_score"] = noise_score(subject, from_addr, sig)
    sig["gm_labels"] = gm_labels
    sig["folder_role"] = role
    norm = Normalized(
        msg_id=msg_id, subject=subject, from_name=collapse(from_name)[:120], from_addr=from_addr, to=to, cc=cc,
        date=dt, message_id=str(msg.get("Message-ID", "") or "").strip(),
        thread_key=thread_key(msg, gm_thrid), text=new[:TEXT_CAP], quoted=quoted[:QUOTE_CAP],
        links=book.links, attachments=[{k: v for k, v in a.items() if k != "text_path"} for a in arts_out],
        events=events, signals=sig,
    )
    return norm, arts_out
