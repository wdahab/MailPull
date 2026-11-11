"""Microsoft Graph access: OAuth login, and finding messages that match a Pull.

Server-side, we narrow with what Graph supports reliably ($filter on sender
address, received date, hasAttachments — or $search for full text). Every
criterion is then re-checked client-side, so results are exact either way.
"""

from __future__ import annotations

import re
import sys
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

from O365 import Account, FileSystemTokenBackend
from rich.console import Console

from .config import Config, Pull

console = Console(stderr=True)

REDIRECT_PORT = 8400
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
# offline_access → refresh token; mailbox_shared → Mail.Read + Mail.Read.Shared,
# i.e. your own mailbox plus any mailbox that has been shared with you.
SCOPES = ["offline_access", "mailbox_shared"]

SELECT_FIELDS = [
    "id", "internetMessageId", "subject", "from", "sender", "toRecipients",
    "ccRecipients", "bccRecipients", "receivedDateTime", "sentDateTime",
    "hasAttachments", "parentFolderId", "conversationId", "bodyPreview",
]


# ============================================================================
# Authentication
# ============================================================================

def token_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = home / "AppData" / "Roaming"
    else:
        base = home / ".local" / "share"
    d = base / "MailPull"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _wait_for_redirect() -> str:
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            result["url"] = f"{REDIRECT_URI}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>MailPull: authentication complete. You can close this tab.</h2>")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    server.handle_request()
    server.server_close()
    return result.get("url", "")


def get_account(cfg: Config, force_login: bool = False) -> Account:
    if not (cfg.client_id and cfg.client_secret):
        console.print(
            "[red]No Azure app credentials.[/] Set MAILPULL_CLIENT_ID / MAILPULL_CLIENT_SECRET, "
            f"or add an \\[azure] section to {cfg.path} (see mailpull.example.toml)."
        )
        sys.exit(2)
    backend = FileSystemTokenBackend(token_path=str(token_dir()), token_filename="o365_token.txt")
    account = Account((cfg.client_id, cfg.client_secret), token_backend=backend)

    if account.is_authenticated and not force_login:
        return account

    console.print("[yellow]Opening browser for Office 365 login…[/]")
    url, flow = account.get_authorization_url(SCOPES, redirect_uri=REDIRECT_URI)
    webbrowser.open(url)
    console.print(f"[dim]If the browser didn't open, visit:[/]\n{url}")
    redirect = _wait_for_redirect()
    if redirect and account.request_token(redirect, flow=flow):
        console.print("[green]Authenticated.[/]")
        return account
    console.print("[red]Authentication failed.[/]")
    sys.exit(1)


# ============================================================================
# Query building
# ============================================================================

class GraphQuery:
    """Raw OData params; O365's get_messages() merges as_params() into the request."""

    def __init__(self, params: dict):
        self.params = params

    def as_params(self) -> dict:
        return dict(self.params)


def _odata_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _odata_dt(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def is_address(s: str) -> bool:
    return "@" in s and not s.startswith("@")


def build_query(pull: Pull, since: datetime | None, until: datetime | None) -> tuple[GraphQuery, str | None]:
    """Return (query, order_by) for Graph.

    With $search, Graph forbids $filter and $orderby, so date/sender limits
    are expressed in KQL instead (and re-checked client-side regardless).
    """
    select = list(SELECT_FIELDS)
    if pull.body:
        select.append("body")

    if pull.search:
        kql = [f"({pull.search})"]
        addrs = [s for s in pull.senders if is_address(s)]
        if addrs and len(addrs) == len(pull.senders):
            kql.append("(" + " OR ".join(f"from:{a}" for a in addrs) + ")")
        if since:
            kql.append(f"received>={since:%Y-%m-%d}")
        if until:
            kql.append(f"received<{until:%Y-%m-%d}")
        if pull.has_attachments is not None:
            kql.append(f"hasattachments:{str(pull.has_attachments).lower()}")
        search = " AND ".join(kql).replace('"', '\\"')
        return GraphQuery({"$search": f'"{search}"', "$select": ",".join(select)}), None

    # receivedDateTime must lead the filter for "$orderby=receivedDateTime" to be
    # accepted by Exchange, so always include a lower bound.
    lower = since or datetime(1990, 1, 1).astimezone()
    clauses = [f"receivedDateTime ge {_odata_dt(lower)}"]
    if until:
        clauses.append(f"receivedDateTime lt {_odata_dt(until)}")
    addrs = [s for s in pull.senders if is_address(s)]
    # Only push sender to the server if *every* sender is a full address;
    # domains ("@x.com") are matched client-side.
    if addrs and len(addrs) == len(pull.senders):
        ors = " or ".join(f"from/emailAddress/address eq {_odata_str(a)}" for a in addrs)
        clauses.append(f"({ors})" if len(addrs) > 1 else ors)
    if pull.has_attachments is not None:
        clauses.append(f"hasAttachments eq {str(pull.has_attachments).lower()}")
    return (
        GraphQuery({"$filter": " and ".join(clauses), "$select": ",".join(select)}),
        "receivedDateTime desc",
    )


# ============================================================================
# Client-side matching
# ============================================================================

_TAG_RE = re.compile(r"<[^>]+>")


def _contains_any(text: str, needles: list[str]) -> bool:
    t = (text or "").lower()
    return any(n.lower() in t for n in needles)


def matches(msg, pull: Pull, since: datetime | None, until: datetime | None) -> bool:
    """Exact check of every criterion against a Graph message."""
    if since and msg.received and msg.received < since:
        return False
    if until and msg.received and msg.received >= until:
        return False
    if pull.senders:
        addr = (msg.sender.address if msg.sender else "") or ""
        addr = addr.lower()
        ok = False
        for s in pull.senders:
            s = s.lower()
            ok |= addr.endswith(s) if s.startswith("@") else addr == s
        if not ok:
            return False
    if pull.recipients:
        people = [f"{r.name or ''} <{r.address or ''}>" for coll in (msg.to, msg.cc, msg.bcc) for r in coll]
        if not _contains_any(" | ".join(people), pull.recipients):
            return False
    if pull.subject and not _contains_any(msg.subject, pull.subject):
        return False
    if pull.exclude_subject and _contains_any(msg.subject, pull.exclude_subject):
        return False
    if pull.has_attachments is not None and bool(msg.has_attachments) != pull.has_attachments:
        return False
    if pull.body and not _contains_any(_TAG_RE.sub(" ", msg.body or ""), pull.body):
        return False
    return True


# ============================================================================
# Fetching
# ============================================================================

def resolve_folder(mailbox, path: str):
    """Find a folder by name or slash path ("Inbox/Clients/Acme")."""
    parts = [p for p in path.split("/") if p]
    folder = mailbox.get_folder(folder_name=parts[0])
    for part in parts[1:]:
        if folder is None:
            break
        folder = folder.get_folder(folder_name=part)
    return folder


class FolderNames:
    """Lazily map parentFolderId → display name (one request per folder)."""

    def __init__(self, mailbox):
        self.mailbox = mailbox
        self.cache: dict[str, str] = {}

    def __call__(self, folder_id: str | None) -> str:
        if not folder_id:
            return ""
        if folder_id not in self.cache:
            try:
                f = self.mailbox.get_folder(folder_id=folder_id)
                self.cache[folder_id] = f.name if f else ""
            except Exception:  # noqa: BLE001
                self.cache[folder_id] = ""
        return self.cache[folder_id]


def iter_candidates(mailbox, pull: Pull, since, until) -> Iterator:
    """Yield server-side candidates (newest first unless using $search)."""
    query, order_by = build_query(pull, since, until)
    if pull.folders:
        sources = []
        for path in pull.folders:
            f = resolve_folder(mailbox, path)
            if f is None:
                console.print(f"[yellow]Folder not found in {pull.mailbox or 'your mailbox'}: {path!r} — skipped[/]")
            else:
                sources.append(f)
    else:
        sources = [mailbox]  # root: /messages spans every folder
    for src in sources:
        yield from src.get_messages(limit=None, query=query, order_by=order_by, batch=200)
