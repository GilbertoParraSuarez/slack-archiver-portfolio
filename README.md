# Slack Archiver

[![CI](https://github.com/GilbertoParraSuarez/slack-archiver-portfolio/actions/workflows/ci.yml/badge.svg)](https://github.com/GilbertoParraSuarez/slack-archiver-portfolio/actions/workflows/ci.yml)

**Keep selected channel history in SQLite. Export it as portable JSON.**

A small, dependency-free Python CLI for workspace owners, with a fully offline
synthetic demo. This is an incremental history snapshot tool, **not** a complete
Slack backup, Slack channel-archiving operation, or retention workaround.

## Try it in 30 seconds

Requirements: Python 3.10+ with SQLite support. No package installation, server,
Slack account, or credentials are needed for the demo. Run from this directory:

```console
python archiver.py demo
python archiver.py demo
python archiver.py list --db data/demo.db
python archiver.py export --db data/demo.db --channel CDEMO --output exports/demo.json
python -m json.tool exports/demo.json
```

The first run inserts **3 synthetic messages**. The second inserts **0**.
The export preserves message timestamps as strings and labels the data synthetic.
Choose a new filename when exporting again: existing files are never overwritten.
See the [recorded offline demo](docs/demo.txt).

## Inspect a local archive

Run `python archiver.py list --db data/demo.db` to see synced channel IDs, message
counts, and exact stored checkpoints, sorted by channel ID. Successfully synced
empty channels appear with a count of zero; an archive without completed syncs
prints `No synced channels in this archive.` The command opens SQLite read-only,
never shows message contents, and needs no token or network. Missing or invalid
databases fail without creating an archive. Omit `--db` to use `data/archive.db`.

## Snapshot your workspace

> [!IMPORTANT]
> Only collect data you are authorized to retain. This program never sends, edits,
> deletes, joins, or archives anything in Slack. Local SQLite and JSON files are
> **unencrypted**; keep real archives outside shared/cloud-synced project folders,
> restrict filesystem access, and apply your organization's retention policy.

1. Create/install a Slack app approved for your workspace. Use a **human user
   token** belonging to an active workspace owner, not a bot token or admin-only user.
2. Grant user scopes `users:read` and `channels:history` for public channels;
   private channels require `groups:history` and appropriate membership/access.
3. Inject the token as `SLACK_TOKEN` using your local secret manager. Never put it
   in a command argument, source file, `.env` file, CI output, or screenshot.
4. Run with channel IDs, not channel names. Repeat `--channel` to select more:

```console
python archiver.py sync --channel C0123456789 --channel C9876543210
python archiver.py export --channel C0123456789 --output exports/channel-snapshot.json
```

Those are illustrative channel IDs. Set `--db` to a private, non-synced local path
for real use; the default is `data/archive.db`. Demo data uses a separate database
and synthetic workspace. Owner status is verified at the start of every sync via
[`auth.test`](https://docs.slack.dev/reference/methods/auth.test/) and
[`users.info`](https://docs.slack.dev/reference/methods/users.info/).
Local export relies on filesystem access, not Slack authentication.

## How it works

```text
SLACK_TOKEN -> verify owner -> selected channel history -> SQLite transaction
                                                        -> JSON export
```

| Guarantee | Implementation |
| --- | --- |
| Resume safely | Each channel commits messages and its watermark only after every page succeeds. A failed channel rolls back; previously completed channels remain committed. |
| No duplicate inserts | One database is bound to one team; `(channel, timestamp)` is its message key. Incremental reads overlap the last timestamp. |
| Exact timestamps | Decimal comparison; original six-digit fractional timestamp strings are preserved. |
| Bounded API use | Fixed Slack origin, three read-only methods, no redirects, 30-second request timeout, 4 MiB response cap, repeated-cursor detection. |
| Explicit rate handling | 15 messages/page, 60 seconds between history calls by default. HTTP 429 stops with sanitized `Retry-After` guidance; no retry loop. |
| Portable output | Streamed, oldest-first JSON with a versioned envelope and original message objects. |
| Complete export publication | Write and close a temporary file in the output directory, then hard-link it to the final name without replacing an existing file. |

At the default budget of 100 pages, one channel can take roughly 100 minutes plus
network time. A budget exhaustion rolls back that channel: increase `--max-pages`
(maximum 1000) only after considering duration and workspace limits. `--interval`
accepts 0–3600 seconds; reduce it only when your app's actual rate tier permits it.
Run one sync at a time: the SQLite writer lock spans each channel's fetch.

For periodic operation, schedule this same `sync` command with your operating
system scheduler, a securely injected environment, and overlap prevention. No
scheduler is installed or enabled by this repository. Removing a `--channel`
argument stops future collection, without deleting already saved history.

## Verification

```console
python -m unittest -v
```

Tests use temporary databases and mocked transport only: pagination, deduplication,
incremental resume, failed-page rollback, malformed pages, cursor limits, owner
rejection, workspace separation, API failures, rate limits, credential transport,
export write/publication failures, competing output creation, and the complete
offline CLI demo/list/export, read-only inventory, and invalid database handling.
CI runs the same tests and a smoke check
on Windows/Linux with Python 3.10/3.14. Dependabot updates pinned Actions; there are
no third-party Python dependencies to install or lock.

## Scope and limitations

- Captures only history visible to the token through `conversations.history`.
  No thread-reply traversal, file downloads, edit/delete reconciliation, or
  late-arriving messages older than the watermark. Existing messages are immutable
  first-seen snapshots, not continuously updated records.
- Cannot recover expired, deleted, hidden, or retention-limited Slack content.
  Not a compliance archive, encryption service, or multi-user web application.
- Live integration has **not** been exercised with a real workspace. The adapter
  is tested offline; validate permissions and current Slack behavior in a workspace
  you control before relying on it. Enterprise-wide/org tokens are out of scope.
- Interrupted syncs roll back their channel. Export write failures do not publish
  a partial destination; existing files are never replaced, even if created during
  export. The output filesystem must support hard links or export fails safely.
  A process crash or failed cleanup may leave a private-data `.slack-export-*.tmp`
  staging file in the output directory; remove it only when no export is running.
  This protects publication, not durability against power loss. Keep the output
  directory access-restricted; Windows staging files inherit its access controls.
- Scheduled execution, persistent channel management, and a browser UI are deferred.
  The runnable CLI is the demo; there is no public deployment or background service.

Inspired by the [App Ideas Slack Archiver brief](https://github.com/florinpop17/app-ideas/blob/master/Projects/3-Advanced/Slack-Archiver.md).
Its historical `channels.history`/rate assumptions are not used. Current behavior
is based on [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/)
and [Slack's rate-limit documentation](https://docs.slack.dev/apis/web-api/rate-limits/),
checked September 8, 2026. Rate tiers depend on the app's distribution category;
15 items is a conservative page size, not a universal throughput promise.
