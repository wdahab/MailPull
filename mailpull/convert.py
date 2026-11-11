"""Turn raw MIME (.eml bytes) into a readable body and attachment files.

Works purely from the MIME bytes, so it can be re-run on saved .eml files
without touching the server.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

import html2text


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Parsed:
    subject: str
    sender: str
    to: list[str]
    cc: list[str]
    date: str | None
    message_id: str | None
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def _html_to_markdown(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0          # don't hard-wrap
    h.ignore_images = True
    h.ignore_emphasis = False
    h.unicode_snob = True
    h.single_line_break = False
    return h.handle(html)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _fmt_addrs(values) -> list[str]:
    out = []
    for name, addr in getaddresses([str(v) for v in values if v]):
        if not addr and not name:
            continue
        out.append(f"{name} <{addr}>" if name and name != addr else addr)
    return out


def _get_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def parse_mime(raw: bytes) -> Parsed:
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)

    # Prefer HTML → Markdown (keeps tables, lists, links); fall back to plain.
    body = ""
    html_part = msg.get_body(preferencelist=("html",))
    plain_part = msg.get_body(preferencelist=("plain",))
    if html_part is not None:
        body = _html_to_markdown(_get_text(html_part))
    if not body.strip() and plain_part is not None:
        body = _get_text(plain_part)

    attachments = []
    for part in msg.iter_attachments():
        ctype = part.get_content_type()
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        # Skip inline images (signatures, logos) that have no real filename role.
        if disposition == "inline" and ctype.startswith("image/"):
            continue
        if ctype == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            data = inner.as_bytes() if inner is not None else (part.get_payload(decode=True) or b"")
            filename = filename or f"{(inner or {}).get('Subject', 'attached-message')}.eml"
        else:
            data = part.get_payload(decode=True) or b""
        if not filename:
            ext = {"text/calendar": ".ics", "application/pdf": ".pdf"}.get(ctype, ".bin")
            filename = f"attachment{ext}"
        attachments.append(Attachment(filename, ctype, data))

    date = None
    if msg["Date"]:
        try:
            date = parsedate_to_datetime(str(msg["Date"])).isoformat()
        except (TypeError, ValueError):
            date = str(msg["Date"])

    return Parsed(
        subject=str(msg["Subject"] or ""),
        sender=(_fmt_addrs([msg["From"]]) or [""])[0],
        to=_fmt_addrs(msg.get_all("To", [])),
        cc=_fmt_addrs(msg.get_all("Cc", [])),
        date=date,
        message_id=str(msg["Message-ID"]).strip() if msg["Message-ID"] else None,
        body=_clean(body),
        attachments=attachments,
    )
