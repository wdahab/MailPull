"""Offline tests: config parsing, query building, matching, MIME conversion,
storage/search/digest, and a full run_pull against a fake Graph mailbox."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace as NS

import pytest

from mailpull.cli import main
from mailpull.config import ConfigError, append_pull, load_config, make_pull, parse_when
from mailpull.convert import parse_mime
from mailpull.graph import build_query, matches
from mailpull.runner import run_pull
from mailpull.store import Store


def make_eml(subject="Q3 report", sender="Alice <alice@acme.com>", html=True, attach=True):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m["To"] = "Bob <bob@dahab.com>, carol@dahab.com"
    m["Date"] = "Tue, 10 Nov 2026 09:30:00 -0500"
    m["Message-ID"] = f"<{abs(hash(subject))}@acme.com>"
    m.set_content("Plain body: revenue up 12%.")
    if html:
        m.add_alternative("<p>Revenue <b>up 12%</b>.</p><table><tr><td>Q3</td><td>$5M</td></tr></table>"
                          "<p><a href='https://x.com'>link</a></p>", subtype="html")
    if attach:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="report.pdf")
        m.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="data.csv")
    return m.as_bytes()


def rcpt(addr, name=""):
    return NS(address=addr, name=name)


class FakeMsg:
    def __init__(self, subject, sender, received, to=("bob@dahab.com",), attach=True, body=""):
        self.subject = subject
        self.sender = rcpt(sender)
        self.to = [rcpt(a) for a in to]
        self.cc, self.bcc = [], []
        self.received = received
        self.has_attachments = attach
        self.body = body
        self.internet_message_id = f"<{subject}@x>"
        self.object_id = "g-" + subject
        self.folder_id = "F-inbox"
        self.conversation_id = "conv-1"
        self._raw = make_eml(subject=subject, sender=sender, attach=attach)

    def get_mime_content(self):
        return self._raw


class FakeMailbox:
    def __init__(self, msgs):
        self.msgs = msgs
        self.queries = []

    def get_messages(self, limit=None, query=None, order_by=None, batch=None):
        self.queries.append(query.as_params())
        return iter(sorted(self.msgs, key=lambda m: m.received, reverse=True))

    def get_folder(self, folder_id=None, folder_name=None):
        return NS(name="Inbox")


class FakeAccount:
    def __init__(self, mbox):
        self.mbox = mbox

    def mailbox(self, resource=None):
        return self.mbox


# ── config ──────────────────────────────────────────────────────────────────

def test_make_pull_normalises_and_validates():
    p = make_pull("x", {"from": "a@b.com", "subject": ["Hi", " "], "since": "30d"})
    assert p.senders == ["a@b.com"] and p.subject == ["Hi"]
    with pytest.raises(ConfigError, match="unknown key"):
        make_pull("x", {"form": "a@b.com"})
    with pytest.raises(ConfigError, match="Bad date"):
        make_pull("x", {"since": "last tuesday"})
    with pytest.raises(ConfigError, match="lowercase"):
        make_pull("Bad Name", {})


def test_parse_when():
    assert parse_when("2026-01-02").date().isoformat() == "2026-01-02"
    assert (datetime.now().astimezone() - parse_when("7d")).days in (6, 7)
    assert parse_when(None) is None


def test_save_roundtrip(tmp_path):
    cfg = tmp_path / "mailpull.toml"
    cfg.write_text('[settings]\ndata_dir = "out"\n')
    p = make_pull("acme", {"from": ["@acme.com"], "since": "2025-01-01", "subject": 'say "hi"'})
    append_pull(p, cfg)
    loaded = load_config(cfg)
    assert loaded.pulls["acme"].senders == ["@acme.com"]
    assert loaded.pulls["acme"].subject == ['say "hi"']
    assert loaded.data_dir.name == "out"
    with pytest.raises(ConfigError, match="already exists"):
        append_pull(p, cfg)


# ── query + matching ────────────────────────────────────────────────────────

def test_build_query_filter_and_search():
    since = datetime(2026, 1, 1).astimezone()
    q, order = build_query(make_pull("x", {"from": ["a@b.com", "o'neil@b.com"], "has_attachments": True}), since, None)
    f = q.as_params()["$filter"]
    assert f.startswith("receivedDateTime ge 2026-01-01")
    assert "from/emailAddress/address eq 'o''neil@b.com'" in f and " or " in f
    assert "hasAttachments eq true" in f and order == "receivedDateTime desc"

    # A domain sender can't be pushed server-side.
    q, _ = build_query(make_pull("x", {"from": ["a@b.com", "@c.com"]}), None, None)
    assert "from/" not in q.as_params()["$filter"]

    q, order = build_query(make_pull("x", {"search": 'budget AND "Q3"', "from": "a@b.com"}), since, None)
    s = q.as_params()["$search"]
    assert order is None and "$filter" not in q.as_params()
    assert s.startswith('"(budget AND \\"Q3\\")') and "from:a@b.com" in s and "received>=2026-01-01" in s


def test_matches():
    now = datetime.now(timezone.utc)
    m = FakeMsg("Re: Invoice 42", "bill@acme.com", now, to=("ops@dahab.com",))
    assert matches(m, make_pull("x", {"from": "@acme.com", "subject": "invoice"}), None, None)
    assert not matches(m, make_pull("x", {"from": "@other.com"}), None, None)
    assert not matches(m, make_pull("x", {"from": "acme.com"}), None, None)  # full address must be exact
    assert matches(m, make_pull("x", {"to": "ops@"}), None, None)
    assert not matches(m, make_pull("x", {"exclude_subject": "INVOICE"}), None, None)
    assert not matches(m, make_pull("x", {}), now + timedelta(hours=1), None)


# ── conversion + storage ────────────────────────────────────────────────────

def test_parse_mime_prefers_html_markdown():
    p = parse_mime(make_eml())
    assert "**up 12%**" in p.body and "Q3" in p.body and "$5M" in p.body
    assert p.sender == "Alice <alice@acme.com>"
    assert p.to == ["Bob <bob@dahab.com>", "carol@dahab.com"]
    assert [a.filename for a in p.attachments] == ["report.pdf", "data.csv"]
    assert parse_mime(make_eml(html=False, attach=False)).body.startswith("Plain body")


def test_run_pull_end_to_end(tmp_path, capsys):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    msgs = [
        FakeMsg("Q3 report", "alice@acme.com", now - timedelta(days=1)),
        FakeMsg("Lunch?", "alice@acme.com", now - timedelta(days=2), attach=False),
        FakeMsg("Newsletter", "news@other.com", now - timedelta(days=3)),
    ]
    mbox = FakeMailbox(msgs)
    store = Store(tmp_path)
    pull = make_pull("acme", {"from": "@acme.com", "mailbox": "w@dahab.com"})

    res = run_pull(pull, store, FakeAccount(mbox))
    assert (res.matched, res.downloaded, res.failed) == (2, 2, 0)

    rows = store.query(pull="acme")
    assert [r["subject"] for r in rows] == ["Q3 report", "Lunch?"]
    folder = tmp_path / rows[0]["path"]
    md = (folder / "message.md").read_text()
    assert md.startswith("---\nsubject: \"Q3 report\"") and 'folder: "Inbox"' in md
    assert "attachments/report.pdf" in md and (folder / "attachments" / "report.pdf").exists()
    assert (folder / "message.eml").exists()

    # Second run is incremental and finds nothing new.
    res2 = run_pull(pull, store, FakeAccount(mbox))
    assert (res2.already, res2.downloaded) == (2, 0)
    assert "receivedDateTime ge" in mbox.queries[-1]["$filter"]

    # Full-text search (FTS5, porter stemming) + digest.
    assert [r["subject"] for r in store.query(text="revenue")] == ["Q3 report", "Lunch?"]
    assert store.query(text="nonexistentword") == []


def test_cli_search_and_digest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAILPULL_DATA", str(tmp_path))
    store = Store(tmp_path)
    now = datetime.now(timezone.utc)
    run_pull(make_pull("acme", {"from": "@acme.com"}), store,
             FakeAccount(FakeMailbox([FakeMsg("Q3 report", "a@acme.com", now)])))
    capsys.readouterr()
    assert main(["search", "revenue", "--paths"]) == 0
    assert capsys.readouterr().out.strip().endswith("message.md")
    assert main(["digest", "acme", "-o", "-"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Digest: acme (1 messages)") and "**up 12%**" in out and "subject:" not in out
