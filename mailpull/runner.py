"""Run a Pull: query Graph, filter, download, and store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .config import Pull, parse_when
from .convert import parse_mime
from .graph import FolderNames, console, iter_candidates, matches
from .store import Store, utc_iso

OVERLAP = timedelta(days=2)  # re-scan this far behind the newest stored message


@dataclass
class RunResult:
    scanned: int = 0
    matched: int = 0
    already: int = 0
    downloaded: int = 0
    failed: int = 0


def _window(pull: Pull, store: Store, full: bool) -> tuple[datetime | None, datetime | None]:
    since, until = parse_when(pull.since), parse_when(pull.until)
    state = store.get_state(pull.name)
    if full or state is None or not state["last_received"]:
        return since, until
    if state["criteria_hash"] != pull.criteria_hash():
        console.print(f"[yellow]{pull.name}: criteria changed since last run — scanning the full range.[/]")
        return since, until
    inc = datetime.fromisoformat(state["last_received"]).astimezone() - OVERLAP
    return (max(since, inc) if since else inc), until


def run_pull(pull: Pull, store: Store, account, *, full: bool = False,
             dry_run: bool = False, refetch: bool = False) -> RunResult:
    res = RunResult()
    since, until = _window(pull, store, full)
    where = pull.mailbox or "your mailbox"
    span = f"{since:%Y-%m-%d}" if since else "the beginning"
    console.print(f"[bold cyan]{pull.name}[/] — {where}, since {span}"
                  + (f" until {until:%Y-%m-%d}" if until else ""))

    mailbox = account.mailbox(resource=pull.mailbox) if pull.mailbox else account.mailbox()
    folder_name = FolderNames(mailbox)

    todo = []
    newest: datetime | None = None
    with console.status(f"Searching {where}…") as status:
        for msg in iter_candidates(mailbox, pull, since, until):
            res.scanned += 1
            if res.scanned % 100 == 0:
                status.update(f"Searching {where}… {res.scanned} scanned, {res.matched} matched")
            if not matches(msg, pull, since, until):
                continue
            res.matched += 1
            if msg.received and (newest is None or msg.received > newest):
                newest = msg.received
            key = msg.internet_message_id or msg.object_id
            if not refetch and store.has_message(pull.name, key):
                res.already += 1
                continue
            todo.append((key, msg))
            if pull.limit and len(todo) >= pull.limit:
                console.print(f"[yellow]Reached limit of {pull.limit} new messages.[/]")
                break

    console.print(f"  scanned {res.scanned}, matched {res.matched}, "
                  f"already saved {res.already}, new {len(todo)}")

    if dry_run:
        _print_matches(todo, folder_name)
        return res

    if todo:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
                      console=console) as progress:
            task = progress.add_task("Downloading", total=len(todo))
            for key, msg in todo:
                try:
                    raw = msg.get_mime_content()
                    if not raw:
                        raise RuntimeError("empty MIME content")
                    parsed = parse_mime(raw)
                    store.save(
                        pull=pull.name, key=key, graph_id=msg.object_id, mailbox=pull.mailbox,
                        folder=folder_name(msg.folder_id), received=msg.received,
                        conversation_id=msg.conversation_id, raw=raw, parsed=parsed,
                        save_eml=pull.save_eml, save_attachments=pull.save_attachments,
                        max_attachment_mb=pull.max_attachment_mb,
                    )
                    res.downloaded += 1
                except Exception as e:  # noqa: BLE001 — keep going, report at end
                    res.failed += 1
                    console.print(f"[red]Failed: {msg.subject!r} ({msg.received:%Y-%m-%d}): {e}[/]")
                progress.advance(task)

    # Only advance the incremental cursor if nothing failed, so failures retry.
    store.set_state(pull.name, pull.mailbox, pull.criteria_hash(),
                    utc_iso(newest) if newest and not res.failed else None)
    msg = f"[bold green]{pull.name}: saved {res.downloaded} new message(s)[/]"
    if res.failed:
        msg += f" [red]({res.failed} failed — they will be retried next run)[/]"
    console.print(msg)
    return res


def _print_matches(todo, folder_name) -> None:
    if not todo:
        return
    t = Table(title="Would download", show_lines=False)
    for col in ("Received", "From", "Subject", "Folder", "Att"):
        t.add_column(col)
    for _, m in todo[:200]:
        t.add_row(f"{m.received:%Y-%m-%d %H:%M}", (m.sender.address if m.sender else ""),
                  (m.subject or "")[:70], folder_name(m.folder_id), "📎" if m.has_attachments else "")
    console.print(t)
    if len(todo) > 200:
        console.print(f"… and {len(todo) - 200} more")
