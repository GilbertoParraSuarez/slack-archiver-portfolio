"""Owner-only Slack history snapshots. Runtime dependencies: Python standard library."""
import argparse
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ArchiveError(Exception):
    """A safe-to-display operational error, never an API response or credential."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the Authorization header to another endpoint.


def slack_call(token, method, **params):
    if method not in {"auth.test", "users.info", "conversations.history"}:
        raise ArchiveError("Unsupported API method.")
    if not token or any(char.isspace() for char in token):
        raise ArchiveError("Set a valid SLACK_TOKEN environment variable.")
    request = Request("https://slack.com/api/" + method + "?" + urlencode(params),
                      headers={"Authorization": "Bearer " + token})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ArchiveError("API response exceeded the 4 MiB safety limit.")
        body = json.loads(raw)
    except HTTPError as error:
        if error.code == 429:
            retry = error.headers.get("Retry-After", "")
            delay = f"{int(retry)} seconds" if re.fullmatch(r"[0-9]{1,6}", retry) else "the Retry-After interval"
            raise ArchiveError(f"Slack rate limit: wait {delay} before rerunning.") from None
        raise ArchiveError(f"Slack HTTP error ({error.code}); no channel checkpoint advanced.") from None
    except (URLError, TimeoutError, OSError):
        raise ArchiveError("Slack connection failed; check network access and retry later.") from None
    except (ValueError, UnicodeError):
        raise ArchiveError("Slack returned invalid JSON.") from None
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise ArchiveError("Slack API rejected the request; check token, scopes and channel access.")
    return body


def valid_id(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"[A-Z0-9]+", value) is not None


def require_owner(call):
    auth = call("auth.test")
    if (not isinstance(auth, dict) or auth.get("ok") is not True or auth.get("bot_id")
            or not valid_id(auth.get("team_id"), "T") or not valid_id(auth.get("user_id"), "[UW]")):
        raise ArchiveError("A workspace-scoped human user token is required.")
    result = call("users.info", user=auth["user_id"])
    user = result.get("user") if isinstance(result, dict) and result.get("ok") is True else None
    if (not isinstance(user, dict) or user.get("id") != auth["user_id"]
            or user.get("team_id") != auth["team_id"] or user.get("is_owner") is not True
            or user.get("is_bot") is not False or user.get("deleted") is not False
            or user.get("is_app_user", False) is not False):
        raise ArchiveError("Archiving requires an active human owner of this workspace.")
    return auth["team_id"]


def open_archive(path, team):
    if not valid_id(team, "T"):
        raise ArchiveError("Invalid workspace ID.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY CHECK(id=1), team TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO workspace VALUES (1, ?)", (team,))
            if db.execute("SELECT team FROM workspace WHERE id=1").fetchone()[0] != team:
                raise ArchiveError("Database belongs to another workspace; choose a separate --db path.")
            db.execute("CREATE TABLE IF NOT EXISTS messages (channel TEXT, ts TEXT, payload TEXT NOT NULL, PRIMARY KEY(channel, ts))")
            db.execute("CREATE TABLE IF NOT EXISTS checkpoints (channel TEXT PRIMARY KEY, ts TEXT NOT NULL)")
        return db
    except Exception:
        db.close()
        raise


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,15})\.[0-9]{6}", value):
        raise ArchiveError("Malformed message timestamp; channel sync rolled back.")
    return Decimal(value)


def sync_channel(db, call, channel, *, interval=60, max_pages=100):
    if not valid_id(channel, "[CG]"):
        raise ArchiveError("Use a public/private channel ID starting with C or G.")
    if not 0 <= interval <= 3600 or not 1 <= max_pages <= 1000:
        raise ArchiveError("Interval must be 0–3600 seconds; max pages must be 1–1000.")
    microseconds = time.time_ns() // 1000
    latest = f"{microseconds // 1000000}.{microseconds % 1000000:06d}"
    with db:
        # ponytail: one writer lock per channel sync; stage pages if concurrent writers become necessary.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT ts FROM checkpoints WHERE channel=?", (channel,)).fetchone()
        oldest = row[0] if row else "0.000000"
        watermark, cursor, seen, inserted = oldest, "", set(), 0
        for number in range(max_pages):
            if number:
                time.sleep(interval)
            page = call("conversations.history", channel=channel, oldest=oldest, latest=latest,
                        inclusive="true", limit=15, cursor=cursor)
            if (not isinstance(page, dict) or page.get("ok") is not True
                    or not isinstance(page.get("messages"), list) or type(page.get("has_more")) is not bool):
                raise ArchiveError("Malformed history page; channel sync rolled back.")
            metadata = page.get("response_metadata", {})
            following = metadata.get("next_cursor", "") if isinstance(metadata, dict) else None
            if (not isinstance(following, str) or len(following) > 4096
                    or (page["has_more"] and not following) or (following and following in seen)):
                raise ArchiveError("Incomplete or repeated pagination; channel sync rolled back.")
            for message in page["messages"]:
                if not isinstance(message, dict):
                    raise ArchiveError("Malformed message; channel sync rolled back.")
                ts = message.get("ts")
                if not timestamp(oldest) <= timestamp(ts) <= timestamp(latest):
                    raise ArchiveError("Message outside requested time window; channel sync rolled back.")
                encoded = json.dumps(message, ensure_ascii=False, allow_nan=False)
                inserted += db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?)",
                                       (channel, ts, encoded)).rowcount
                if timestamp(ts) > timestamp(watermark):
                    watermark = ts
            if not following:
                db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?)", (channel, watermark))
                return inserted
            seen.add(following)
            cursor = following
        raise ArchiveError("Page budget reached; channel sync rolled back. Consider increasing --max-pages.")


def export_channel(db, channel, output):
    if not valid_id(channel, "[CG]"):
        raise ArchiveError("Invalid channel ID.")
    if not db.execute("SELECT 1 FROM checkpoints WHERE channel=?", (channel,)).fetchone():
        raise ArchiveError("Channel has no completed sync to export.")
    team = db.execute("SELECT team FROM workspace WHERE id=1").fetchone()[0]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create protects existing exports; stream rows instead of loading the archive into memory.
    with output.open("x", encoding="utf-8") as file:
        header = {"format_version": 1, "team_id": team, "channel_id": channel, "synthetic": team == "TDEMO"}
        file.write(json.dumps(header)[:-1] + ', "messages": [\n')
        separator = ""
        for (payload,) in db.execute("SELECT payload FROM messages WHERE channel=? ORDER BY length(ts), ts", (channel,)):
            file.write(separator + payload)
            separator = ",\n"
        file.write("\n]}\n")


DEMO_MESSAGES = [
    {"ts": "1700000000.000001", "user": "USAMPLE1", "text": "Synthetic demo: preserve decisions, not credentials."},
    {"ts": "1700000001.000001", "user": "USAMPLE2", "text": "A completed sync advances the checkpoint."},
    {"ts": "1700000002.000001", "user": "USAMPLE1", "text": "Rerunning this demo adds zero duplicate messages."},
]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Slack history snapshots into local SQLite.")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Load synthetic messages offline; no token or network.")
    demo.add_argument("--db", default="data/demo.db")
    sync = commands.add_parser("sync", help="Snapshot selected channels as a verified workspace owner.")
    sync.add_argument("--db", default="data/archive.db")
    sync.add_argument("--channel", action="append", required=True, help="Repeat for each channel ID.")
    sync.add_argument("--interval", type=float, default=60, help="Seconds between history pages/channels (default: 60).")
    sync.add_argument("--max-pages", type=int, default=100)
    export = commands.add_parser("export", help="Export one previously synced channel to a NEW JSON file.")
    export.add_argument("--db", default="data/archive.db")
    export.add_argument("--channel", required=True)
    export.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            with closing(sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                export_channel(db, args.channel, args.output)
            print("Export created. Treat it as private workspace data.")
        elif args.command == "demo":
            with closing(open_archive(args.db, "TDEMO")) as db:
                def synthetic_page(method, **params):
                    return {"ok": True, "has_more": False, "messages": [
                        m for m in DEMO_MESSAGES if timestamp(m["ts"]) >= timestamp(params["oldest"])]}
                count = sync_channel(db, synthetic_page, "CDEMO", interval=0)
            print(f"Synthetic demo: {count} new messages. No Slack connection was made.")
        else:
            token = os.environ.get("SLACK_TOKEN", "")
            if not token:
                raise ArchiveError("Set SLACK_TOKEN in your environment; never pass it as an argument.")
            def call(method, **params):
                return slack_call(token, method, **params)
            team = require_owner(call)
            with closing(open_archive(args.db, team)) as db:
                for number, channel in enumerate(dict.fromkeys(args.channel)):
                    if number:
                        time.sleep(args.interval)
                    count = sync_channel(db, call, channel, interval=args.interval, max_pages=args.max_pages)
                    print(f"{channel}: {count} new messages; checkpoint committed.")
        return 0
    except ArchiveError as error:
        print(f"Error: {error}", file=sys.stderr)
    except FileExistsError:
        print("Error: output already exists; choose a new export filename.", file=sys.stderr)
    except (OSError, sqlite3.Error, ValueError):
        print("Error: local operation failed; check paths, permissions and database availability.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
