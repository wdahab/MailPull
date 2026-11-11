"""mailpull command line.

    mailpull auth                         sign in (browser) and test access
    mailpull run [PULL ...]               run saved pulls from mailpull.toml (all by default)
    mailpull pull --from x@y.com ...      ad-hoc pull from flags (--save to keep it)
    mailpull list                         saved pulls and what's been downloaded
    mailpull search "words" [--pull P]    full-text search of downloaded mail
    mailpull digest PULL [-o FILE]        concatenate messages into one Markdown file
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import ConfigError, Pull, append_pull, load_config, make_pull, parse_when
from .graph import console, get_account
from .store import Store


def _pull_from_args(a) -> Pull:
    data = {}
    for flag, key in [("mailbox", "mailbox"), ("sender", "from"), ("to", "to"),
                      ("subject", "subject"), ("exclude_subject", "exclude_subject"),
                      ("body", "body"), ("search", "search"), ("folder", "folders"),
                      ("since", "since"), ("until", "until"), ("limit", "limit"),
                      ("description", "description")]:
        v = getattr(a, flag, None)
        if v not in (None, []):
            data[key] = v
    if a.attachments_only:
        data["has_attachments"] = True
    if a.no_attachments:
        data["attachments"] = False
    if a.no_eml:
        data["save_eml"] = False
    return make_pull(a.name, data)


def _has_criteria(p: Pull) -> bool:
    return any([p.senders, p.recipients, p.subject, p.body, p.search, p.folders,
                p.since, p.until, p.has_attachments is not None])


# ============================================================================
# Commands
# ============================================================================

def cmd_auth(a, cfg):
    account = get_account(cfg, force_login=a.relogin)
    for box in [None] + ([a.mailbox] if a.mailbox else []):
        label = box or "your own mailbox"
        try:
            mb = account.mailbox(resource=box) if box else account.mailbox()
            inbox = mb.inbox_folder()
            next(iter(inbox.get_messages(limit=1)), None)
            console.print(f"[green]✓ Can read {label}[/]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]✗ Cannot read {label}: {e}[/]")


def cmd_run(a, cfg):
    if not cfg.pulls:
        console.print(f"[yellow]No pulls defined in {cfg.path}. "
                      "Copy mailpull.example.toml to mailpull.toml, or use `mailpull pull ... --save`.[/]")
        return 1
    names = a.pulls or list(cfg.pulls)
    missing = [n for n in names if n not in cfg.pulls]
    if missing:
        console.print(f"[red]Unknown pull(s): {', '.join(missing)}. Defined: {', '.join(cfg.pulls)}[/]")
        return 1
    return _execute([cfg.pulls[n] for n in names], a, cfg)


def cmd_pull(a, cfg):
    pull = _pull_from_args(a)
    if pull.mailbox is None:
        pull.mailbox = cfg.default_mailbox
    if not _has_criteria(pull):
        console.print("[red]Give at least one criterion (--from, --subject, --since, --search, …).[/]")
        return 1
    if a.save:
        append_pull(pull, cfg.path)
        console.print(f"[green]Saved as [pulls.{pull.name}] in {cfg.path}[/] — rerun with `mailpull run {pull.name}`")
    else:
        console.print("[dim]To keep this pull, add --save, or paste into mailpull.toml:[/]")
        console.print(pull.to_toml(), markup=False, highlight=False)
    return _execute([pull], a, cfg)


def _execute(pulls, a, cfg):
    from .runner import run_pull  # imported late so `list`/`search` work without O365 login

    store = Store(cfg.data_dir)
    account = get_account(cfg)
    rc = 0
    for p in pulls:
        try:
            res = run_pull(p, store, account, full=a.full, dry_run=a.dry_run, refetch=a.refetch)
            rc |= 1 if res.failed else 0
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]{p.name}: {e}[/]")
            if "403" in str(e) or "ErrorAccessDenied" in str(e):
                console.print(f"[yellow]  Your account may not have access to {p.mailbox}. "
                              "The mailbox owner must share it with you (or grant Full Access).[/]")
            rc = 1
    if not a.dry_run:
        console.print(f"\nOutput: {cfg.data_dir}")
    return rc


def cmd_list(a, cfg):
    store = Store(cfg.data_dir)
    stats = {r["pull"]: r for r in store.summary()}
    t = Table(title=f"Pulls ({cfg.path.name})")
    for col in ("Pull", "Mailbox", "Criteria", "Saved", "Newest", "Last run"):
        t.add_column(col)
    for name in sorted(set(cfg.pulls) | set(stats)):
        p, s = cfg.pulls.get(name), stats.get(name)
        crit = _describe(p) if p else "[dim](not in config)[/]"
        t.add_row(name, (p.mailbox if p else s["mailbox"]) or "me", crit,
                  str(s["n"]) if s else "0",
                  s["last"][:10] if s else "", (s["last_run"] or "")[:16] if s else "")
    console.print(t)
    console.print(f"Data: {cfg.data_dir}")


def _describe(p: Pull) -> str:
    bits = []
    if p.senders: bits.append("from " + ", ".join(p.senders))
    if p.recipients: bits.append("to " + ", ".join(p.recipients))
    if p.subject: bits.append("subject~" + "|".join(p.subject))
    if p.exclude_subject: bits.append("not subject~" + "|".join(p.exclude_subject))
    if p.body: bits.append("body~" + "|".join(p.body))
    if p.search: bits.append(f"search {p.search!r}")
    if p.folders: bits.append("in " + ", ".join(p.folders))
    if p.since: bits.append(f"since {p.since}")
    if p.until: bits.append(f"until {p.until}")
    if p.has_attachments is not None: bits.append("with attachments" if p.has_attachments else "no attachments")
    return "; ".join(bits)


def cmd_search(a, cfg):
    store = Store(cfg.data_dir)
    rows = store.query(pull=a.pull, text=a.text, since=parse_when(a.since),
                       until=parse_when(a.until), sender=a.sender, limit=a.limit)
    if a.paths:
        for r in rows:
            print(cfg.data_dir / r["path"] / "message.md")
        return 0
    t = Table(show_lines=False)
    for col in ("Received", "Pull", "From", "Subject", "Path"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(r["received"][:10], r["pull"], r["sender"] or "", (r["subject"] or "")[:60],
                  str(Path(r["path"]) / "message.md"))
    Console().print(t)  # stdout, so results can be piped
    console.print(f"{len(rows)} result(s); paths are relative to {cfg.data_dir}")
    return 0


def cmd_digest(a, cfg):
    store = Store(cfg.data_dir)
    rows = store.query(pull=a.pull, text=a.text, since=parse_when(a.since),
                       until=parse_when(a.until), limit=a.limit, newest_first=False)
    if not rows:
        console.print("[yellow]No messages match.[/]")
        return 1
    parts = [f"# Digest: {a.pull} ({len(rows)} messages)\n"]
    for r in rows:
        md = (cfg.data_dir / r["path"] / "message.md").read_text()
        body = md.split("\n---\n", 1)[1] if md.startswith("---\n") else md  # drop front matter
        if a.max_chars and len(body) > a.max_chars:
            body = body[: a.max_chars] + f"\n\n… [truncated — full text: {r['path']}/message.md]\n"
        parts.append(f"\n\n<!-- {r['path']} -->\n{body.strip()}\n\n---")
    text = "\n".join(parts) + "\n"
    if a.output == "-":
        sys.stdout.write(text)
    else:
        out = Path(a.output) if a.output else cfg.data_dir / a.pull / "digest.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        console.print(f"[green]Wrote {len(rows)} messages to {out}[/]")
    return 0


# ============================================================================
# Parser
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailpull", description="Pull Office 365 email matching criteria into Claude-readable files.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("auth", help="Sign in and check mailbox access")
    s.add_argument("--mailbox", help="Also check access to this shared mailbox")
    s.add_argument("--relogin", action="store_true", help="Force a fresh browser login")
    s.set_defaults(fn=cmd_auth)

    def run_flags(sp):
        sp.add_argument("--full", action="store_true", help="Ignore incremental state; rescan the whole date range")
        sp.add_argument("--dry-run", action="store_true", help="Show what would be downloaded, save nothing")
        sp.add_argument("--refetch", action="store_true", help="Re-download messages already saved")

    s = sub.add_parser("run", help="Run saved pulls from mailpull.toml")
    s.add_argument("pulls", nargs="*", help="Pull names (default: all)")
    run_flags(s)
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("pull", help="Ad-hoc pull from command-line criteria")
    s.add_argument("--name", default="adhoc", help="Name (output folder); default 'adhoc'")
    s.add_argument("--mailbox", help="Mailbox address (default: settings.default_mailbox, else your own)")
    s.add_argument("--from", dest="sender", action="append", default=[], help="Sender address or @domain (repeatable)")
    s.add_argument("--to", action="append", default=[], help="Recipient address/name substring, To/Cc/Bcc (repeatable)")
    s.add_argument("--subject", action="append", default=[], help="Subject contains (repeatable, any)")
    s.add_argument("--exclude-subject", action="append", default=[], help="Subject must NOT contain (repeatable)")
    s.add_argument("--body", action="append", default=[], help="Body contains (repeatable, any)")
    s.add_argument("--search", help="Server full-text search (KQL), e.g. 'invoice AND acme'")
    s.add_argument("--folder", action="append", default=[], help="Folder name/path (repeatable; default all)")
    s.add_argument("--since", help="YYYY-MM-DD or 30d / 8w / 6m / 1y")
    s.add_argument("--until", help="YYYY-MM-DD (exclusive)")
    s.add_argument("--limit", type=int, help="Max new messages (newest first)")
    s.add_argument("--attachments-only", action="store_true", help="Only messages with attachments")
    s.add_argument("--no-attachments", action="store_true", help="Don't save attachment files")
    s.add_argument("--no-eml", action="store_true", help="Don't keep the raw .eml")
    s.add_argument("--description", help="Note stored with --save")
    s.add_argument("--save", action="store_true", help="Save this pull into mailpull.toml")
    run_flags(s)
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("list", help="List pulls and download counts")
    s.set_defaults(fn=cmd_list)

    def query_flags(sp):
        sp.add_argument("--since"); sp.add_argument("--until")
        sp.add_argument("--limit", type=int)

    s = sub.add_parser("search", help="Full-text search downloaded mail (SQLite FTS5 syntax)")
    s.add_argument("text", nargs="?", help='e.g. "quarterly report", acme AND invoice, budg*')
    s.add_argument("--pull"); s.add_argument("--sender", help="Sender contains")
    s.add_argument("--paths", action="store_true", help="Print only message.md paths")
    query_flags(s)
    s.set_defaults(fn=cmd_search, limit=50)

    s = sub.add_parser("digest", help="Concatenate a pull's messages into one Markdown file")
    s.add_argument("pull")
    s.add_argument("--text", help="Only messages matching this full-text query")
    s.add_argument("--max-chars", type=int, default=0, help="Truncate each message body")
    s.add_argument("-o", "--output", help="Output file, or - for stdout (default: data/<pull>/digest.md)")
    query_flags(s)
    s.set_defaults(fn=cmd_digest)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        return a.fn(a, cfg) or 0
    except ConfigError as e:
        console.print(f"[red]Config error:[/] {e}")
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
