# CLAUDE.md

Guidance for Claude Code in this repository.

## What this is

MailPull downloads Office 365 email that matches saved criteria ("pulls") and stores each message as Markdown, so Claude can read and analyze it. It grew out of `wdahab/findaily`, which did the same for a single newsletter sender.

Each person runs it as themselves: they sign in with their own Microsoft account through the shared dahab.com Azure app. They can pull from their own mailbox, or from any mailbox that has been shared with them.

## Commands

```bash
python3 -m venv env && env/bin/pip install -e .     # one-time setup
cp mailpull.example.toml mailpull.toml              # then add credentials + pulls

env/bin/mailpull auth [--mailbox other@dahab.com]   # browser login, check access
env/bin/mailpull run                                # run all saved pulls (incremental)
env/bin/mailpull run acme --dry-run                 # preview matches, save nothing
env/bin/mailpull run acme --full                    # rescan the whole date range
env/bin/mailpull pull --from @acme.com --since 6m --name acme --save   # ad-hoc, saved for later
env/bin/mailpull list                               # pulls + counts
env/bin/mailpull search "invoice AND overdue" --pull acme
env/bin/mailpull digest acme --since 30d            # → data/acme/digest.md
env/bin/python -m pytest -q tests                   # offline tests (no credentials needed)
```

## Reading pulled email (Claude's main job here)

When the user asks about their email, such as "what did Acme send about pricing?" or "summarize the findaily alerts this month":

1. Run `env/bin/mailpull list` to see which pulls exist and how fresh they are. If the data is stale, run `env/bin/mailpull run <pull>` first. That step may need a browser login the first time, which the user has to complete.
2. If nothing covers the request, create a pull. Use `mailpull pull ... --name <slug> --dry-run` to check the criteria, then rerun with `--save`.
3. Find relevant messages:
   - Use `mailpull search "<terms>" [--pull P] [--since 30d] --paths` for full-text search. The syntax is SQLite FTS5: `AND`/`OR`/`NOT`, `"exact phrase"` and `prefix*`.
   - Or query SQLite directly: `sqlite3 data/mailpull.db "SELECT received, sender, subject, path FROM messages WHERE pull='acme' ORDER BY received DESC LIMIT 20"`.
4. Read the messages:
   - For a few messages, read each `data/<path>/message.md`.
   - To survey a whole pull, run `mailpull digest <pull> [--since ...] [--max-chars 4000]` and read the single `digest.md` file.
   - Attachments are in `attachments/` next to each `message.md`. Read PDFs, CSVs and images directly when they matter.
5. Save any analysis you're asked for under `data/<pull>/notes/` or wherever the user asks. Don't edit `message.md` files, because they're regenerated on `--refetch`.

### Output layout

```
data/
  mailpull.db                        # SQLite index of every pull
  <pull>/messages/<YYYY>/<YYYY-MM-DD_HHMM>_<subject-slug>_<hash>/
      message.md                     # YAML front matter + Markdown body  ← read this
      message.eml                    # original MIME
      attachments/…
  <pull>/digest.md                   # from `mailpull digest`
```

The `message.md` front matter has these fields: subject, from, to, cc, date, mailbox, folder, pull, message_id, conversation_id, attachments and attachments_not_saved. The body is the HTML part converted to Markdown, so tables and links survive. Messages with no HTML part use the plain-text part instead.

### Database (`data/mailpull.db`)

- `messages`: pull, message_key (the internetMessageId), graph_id, mailbox, folder, received (UTC ISO), sender, recipients (JSON), cc (JSON), subject, conversation_id, attachments (JSON), path (relative to `data/`), body (Markdown) and pulled_at. A row is unique per (pull, message_key), so a message can belong to several pulls.
- `messages_fts`: an FTS5 index over subject, sender, recipients and body. Join on `rowid = messages.id`.
- `pull_state`: the incremental cursor for each pull (last_received and criteria_hash).

Useful queries:
```sql
-- Threads: everything in one conversation
SELECT received, sender, subject, path FROM messages WHERE conversation_id = ? ORDER BY received;
-- Who emails most in a pull
SELECT sender, COUNT(*) n FROM messages WHERE pull='acme' GROUP BY sender ORDER BY n DESC;
-- Full-text with snippets
SELECT m.received, m.subject, snippet(messages_fts, 3, '[', ']', '…', 12)
FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
WHERE messages_fts MATCH 'pricing NEAR increase' ORDER BY m.received DESC;
```

## Architecture (`mailpull/`)

- `config.py`: loads `mailpull.toml`, which holds `[azure]` credentials, `[settings]` and `[pulls.<name>]`. Credentials can instead come from the `MAILPULL_CLIENT_ID`/`MAILPULL_CLIENT_SECRET` environment variables, with `../rfpdownloader/config.py` as a last fallback. Unknown pull keys are rejected. Dates can be `YYYY-MM-DD` or relative (`30d`, `8w`, `6m`, `1y`).
- `graph.py`: OAuth with scopes `offline_access` and `mailbox_shared`; the token lives in `~/Library/Application Support/MailPull/`. Builds the Graph query:
  - Normally it's `$filter` on receivedDateTime, plus exact sender addresses and hasAttachments, sorted newest first. receivedDateTime has to lead the filter or Exchange rejects the `$orderby`.
  - With `search` set, it uses `$search` (KQL) instead, because Graph won't combine `$search` with `$filter`/`$orderby`.
  - Every criterion is then re-checked client-side in `matches()`. Domain senders (`@acme.com`), to, subject, exclude_subject and body are only checked there.
- `runner.py`: runs a pull. It's incremental: it starts 2 days before the newest message already stored for that pull. If the pull's criteria change, it rescans the full range. It skips messages already saved, downloads MIME, saves the result, and only advances the cursor when nothing failed.
- `convert.py`: turns MIME into a Markdown body (via html2text) and a list of attachments, skipping inline images. It works offline from `.eml` bytes.
- `store.py`: writes the folder layout above and the SQLite tables, FTS triggers, query and summary.
- `cli.py`: argparse subcommands.

## Notes

- This repo is **public**. `mailpull.toml` (which holds the secret) and `data/` (the mail) are gitignored. Never commit them, and never paste email content into commits or PRs.
- Graph `$search` returns at most about 1000 results per query. For bigger sets, narrow by `since`/`until`, or use `from` + `$filter` instead.
- Reading someone else's mailbox only works if they have shared it with the signed-in user, or granted Full Access. Otherwise the result is a 403 / ErrorAccessDenied.
