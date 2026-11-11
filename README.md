# MailPull

MailPull pulls Office 365 email that matches criteria you choose into plain Markdown files plus a searchable SQLite index. Claude (or you) can then read, search and summarize the mail.

It's a generalized version of `findaily`. findaily only fetched one newsletter; MailPull can fetch any sender, recipient, subject, keyword, folder or date range, from your own mailbox or any mailbox shared with you.

## Setup

```bash
git clone https://github.com/wdahab/MailPull && cd MailPull
python3 -m venv env && env/bin/pip install -e .
cp mailpull.example.toml mailpull.toml     # add the Azure client_id / client_secret
env/bin/mailpull auth                      # signs you in through your browser, once
```

You sign in as yourself. The token is stored per user in `~/Library/Application Support/MailPull/`.

## Use

Define a pull in `mailpull.toml`:

```toml
[pulls.acme]
description = "All correspondence with Acme"
from = ["@acme.com"]
since = "1y"
```

Then run it:

```bash
env/bin/mailpull run acme --dry-run   # preview what matches
env/bin/mailpull run acme             # download (incremental on later runs)
env/bin/mailpull run                  # every saved pull
```

You can also run a pull straight from the command line. Add `--save` to keep it in your config:

```bash
env/bin/mailpull pull --name invoices --from billing@vendor.com --subject invoice --since 6m --save
env/bin/mailpull pull --name board --search '"board meeting" AND minutes' --folder Inbox
env/bin/mailpull pull --name shared --mailbox colleague@dahab.com --to wdahab --since 30d
```

These are the criteria. Every criterion that's set must match. Within a list, any value can match.

| key | meaning |
|---|---|
| `mailbox` | mailbox to read (default: yours) |
| `from` | sender address, or a whole domain such as `"@acme.com"` |
| `to` | recipient address or name, checked in To/Cc/Bcc |
| `subject` / `exclude_subject` | subject contains / doesn't contain |
| `body` | body contains |
| `search` | server-side full-text search ([KQL](https://learn.microsoft.com/en-us/graph/search-query-parameter)) |
| `folders` | e.g. `["Inbox", "Archive", "Inbox/Clients"]` (default: all) |
| `since` / `until` | `2025-01-01`, or relative: `30d`, `8w`, `6m`, `1y` |
| `has_attachments` | `true` / `false` |
| `attachments`, `save_eml`, `max_attachment_mb`, `limit` | output options |

## Output

```
data/<pull>/messages/2026/2026-11-10_0930_q3-report_1a2b3c4d/
    message.md        ← headers as YAML front matter, body as Markdown
    message.eml
    attachments/report.pdf
data/mailpull.db      ← SQLite index with full-text search
```

```bash
env/bin/mailpull search "pricing NEAR increase" --pull acme
env/bin/mailpull digest acme --since 30d        # one Markdown file to hand to Claude
```

## With Claude Code

Open this folder in Claude Code and ask questions in plain language, for example "what has Acme said about renewal pricing this year?" `CLAUDE.md` tells Claude how to refresh pulls, search the index and read the messages.

## Security

This repo is public. Your `mailpull.toml`, which holds the app secret, and `data/`, which holds your mail, are gitignored. Keep them that way.
