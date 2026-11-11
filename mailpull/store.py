"""On-disk output: one folder per message plus a SQLite index.

Layout (under the data dir):

    mailpull.db                                  index of every pull (FTS5 search)
    <pull>/messages/<YYYY>/<date>_<subject>_<id>/
        message.md                               front matter + body (read this)
        message.eml                              original MIME
        attachments/<files>
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .convert import Parsed

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY,
    pull             TEXT NOT NULL,
    message_key      TEXT NOT NULL,     -- internetMessageId (or Graph id if missing)
    graph_id         TEXT,
    mailbox          TEXT,
    folder           TEXT,
    received         TEXT NOT NULL,     -- ISO 8601, UTC (sortable)
    sender           TEXT,
    recipients       TEXT,              -- JSON list (To)
    cc               TEXT,              -- JSON list
    subject          TEXT,
    conversation_id  TEXT,
    attachments      TEXT,              -- JSON list of relative paths
    path             TEXT NOT NULL,     -- message folder, relative to data dir
    body             TEXT,
    pulled_at        TEXT NOT NULL,
    UNIQUE (pull, message_key)
);
CREATE INDEX IF NOT EXISTS idx_messages_pull_received ON messages (pull, received);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id);

CREATE TABLE IF NOT EXISTS pull_state (
    pull           TEXT PRIMARY KEY,
    mailbox        TEXT,
    criteria_hash  TEXT,
    last_received  TEXT,
    last_run       TEXT
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, sender, recipients, body,
    content='messages', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, sender, recipients, body)
    VALUES (new.id, new.subject, new.sender, new.recipients || ' ' || new.cc, new.body);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients, body)
    VALUES ('delete', old.id, old.subject, old.sender, old.recipients || ' ' || old.cc, old.body);
END;
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "mailpull.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            self.has_fts = False
        self.db.commit()

    # ── state ───────────────────────────────────────────────────────────────

    def get_state(self, pull: str):
        return self.db.execute("SELECT * FROM pull_state WHERE pull = ?", (pull,)).fetchone()

    def set_state(self, pull: str, mailbox: str | None, criteria_hash: str, last_received: str | None):
        self.db.execute(
            """INSERT INTO pull_state (pull, mailbox, criteria_hash, last_received, last_run)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(pull) DO UPDATE SET
                 mailbox = excluded.mailbox,
                 criteria_hash = excluded.criteria_hash,
                 last_received = MAX(COALESCE(pull_state.last_received, ''), COALESCE(excluded.last_received, '')),
                 last_run = excluded.last_run""",
            (pull, mailbox, criteria_hash, last_received, datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        self.db.commit()

    def has_message(self, pull: str, key: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM messages WHERE pull = ? AND message_key = ?", (pull, key)
        ).fetchone() is not None

    # ── writing ─────────────────────────────────────────────────────────────

    def save(self, *, pull: str, key: str, graph_id: str | None, mailbox: str | None,
             folder: str, received: datetime, conversation_id: str | None,
             raw: bytes, parsed: Parsed, save_eml: bool, save_attachments: bool,
             max_attachment_mb: float) -> Path:
        received = received.astimezone()  # display + folder names in local time
        h = hashlib.sha1(key.encode()).hexdigest()[:8]
        name = f"{received:%Y-%m-%d_%H%M}_{slugify(parsed.subject, 50)}_{h}"
        rel = Path(pull) / "messages" / f"{received:%Y}" / name
        folder_path = self.data_dir / rel
        folder_path.mkdir(parents=True, exist_ok=True)

        if save_eml:
            (folder_path / "message.eml").write_bytes(raw)

        att_rel: list[str] = []
        skipped: list[str] = []
        if save_attachments and parsed.attachments:
            att_dir = folder_path / "attachments"
            used: set[str] = set()
            for att in parsed.attachments:
                fname = unique_name(safe_filename(att.filename), used)
                if len(att.data) > max_attachment_mb * 1024 * 1024:
                    skipped.append(f"{fname} ({human_size(len(att.data))}, over size limit)")
                    continue
                att_dir.mkdir(exist_ok=True)
                (att_dir / fname).write_bytes(att.data)
                att_rel.append(f"attachments/{fname} ({human_size(len(att.data))})")
        elif parsed.attachments:
            skipped = [f"{a.filename} (not saved)" for a in parsed.attachments]

        md = render_markdown(
            parsed=parsed, mailbox=mailbox, folder=folder, received=received,
            key=key, conversation_id=conversation_id, pull=pull,
            attachments=att_rel, skipped=skipped,
        )
        (folder_path / "message.md").write_text(md)

        # Explicit delete (not INSERT OR REPLACE) so the FTS delete trigger fires.
        self.db.execute("DELETE FROM messages WHERE pull = ? AND message_key = ?", (pull, key))
        self.db.execute(
            """INSERT INTO messages
               (pull, message_key, graph_id, mailbox, folder, received, sender, recipients, cc,
                subject, conversation_id, attachments, path, body, pulled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pull, key, graph_id, mailbox, folder, utc_iso(received),
             parsed.sender, json.dumps(parsed.to), json.dumps(parsed.cc), parsed.subject,
             conversation_id, json.dumps(att_rel), str(rel), parsed.body,
             datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        self.db.commit()
        return folder_path

    # ── reading ─────────────────────────────────────────────────────────────

    def query(self, *, pull: str | None = None, text: str | None = None,
              since: datetime | None = None, until: datetime | None = None,
              sender: str | None = None, limit: int | None = None,
              newest_first: bool = True) -> list[sqlite3.Row]:
        where, args = [], []
        if text:
            if self.has_fts:
                where.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
                args.append(text)
            else:
                where.append("(m.subject LIKE ? OR m.body LIKE ?)")
                args += [f"%{text}%"] * 2
        if pull:
            where.append("m.pull = ?"); args.append(pull)
        if since:
            where.append("m.received >= ?"); args.append(utc_iso(since))
        if until:
            where.append("m.received < ?"); args.append(utc_iso(until))
        if sender:
            where.append("m.sender LIKE ?"); args.append(f"%{sender}%")
        sql = "SELECT m.* FROM messages m"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY m.received {'DESC' if newest_first else 'ASC'}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.db.execute(sql, args).fetchall()

    def summary(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT m.pull, COUNT(*) AS n, MIN(m.received) AS first, MAX(m.received) AS last,
                      s.mailbox, s.last_run
               FROM messages m LEFT JOIN pull_state s ON s.pull = m.pull
               GROUP BY m.pull ORDER BY m.pull"""
        ).fetchall()


# ============================================================================
# Helpers
# ============================================================================

def render_markdown(*, parsed: Parsed, mailbox, folder, received: datetime, key,
                    conversation_id, pull, attachments, skipped) -> str:
    def j(v):  # JSON scalars/lists are valid YAML
        return json.dumps(v, ensure_ascii=False)

    fm = [
        "---",
        f"subject: {j(parsed.subject)}",
        f"from: {j(parsed.sender)}",
        f"to: {j(parsed.to)}",
    ]
    if parsed.cc:
        fm.append(f"cc: {j(parsed.cc)}")
    fm += [
        f"date: {received.isoformat(timespec='seconds')}",
        f"mailbox: {j(mailbox or 'me')}",
        f"folder: {j(folder)}",
        f"pull: {j(pull)}",
        f"message_id: {j(key)}",
    ]
    if conversation_id:
        fm.append(f"conversation_id: {j(conversation_id)}")
    if attachments:
        fm.append(f"attachments: {j(attachments)}")
    if skipped:
        fm.append(f"attachments_not_saved: {j(skipped)}")
    fm.append("---")

    head = [f"# {parsed.subject or '(no subject)'}", ""]
    head.append(f"**From:** {parsed.sender}  ")
    if parsed.to:
        head.append(f"**To:** {', '.join(parsed.to)}  ")
    if parsed.cc:
        head.append(f"**Cc:** {', '.join(parsed.cc)}  ")
    head.append(f"**Date:** {received:%a %Y-%m-%d %H:%M %Z}")
    if attachments:
        head.append("")
        head.append("**Attachments:** " + ", ".join(attachments))
    return "\n".join(fm) + "\n\n" + "\n".join(head) + "\n\n---\n\n" + parsed.body


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def slugify(s: str, maxlen: int = 50) -> str:
    s = re.sub(r"^\s*((re|fw|fwd|aw)\s*:\s*|\[external\]\s*)+", "", s or "", flags=re.I)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return (s[:maxlen].rstrip("-") or "no-subject")


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:110] + dot + ext[:10]) if dot else name[:120]
    return name or "attachment"


def unique_name(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    stem, dot, ext = name.rpartition(".")
    while candidate.lower() in used:
        candidate = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
        n += 1
    used.add(candidate.lower())
    return candidate


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
