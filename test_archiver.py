"""Offline behavioral tests. All identities and messages are synthetic."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

import archiver as a


def page(*timestamps, cursor="", more=False):
    return {"ok": True, "messages": [{"ts": ts, "text": "Synthetic message"} for ts in timestamps],
            "has_more": more, "response_metadata": {"next_cursor": cursor}}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.db"
        self.db = a.open_archive(self.path, "TDEMO")
        self.addCleanup(self.db.close)

    def test_pagination_dedup_and_incremental_watermark(self):
        call = Mock(side_effect=[page("10.000002", "10.000001", cursor="next", more=True),
                                 page("10.000001", "9.999999")])
        self.assertEqual(a.sync_channel(self.db, call, "CDEMO", interval=0), 3)
        self.assertEqual(call.call_args_list[1].kwargs["cursor"], "next")
        self.assertEqual(call.call_args_list[0].kwargs["oldest"], "0.000000")
        again = Mock(return_value=page("10.000003", "10.000002"))
        self.assertEqual(a.sync_channel(self.db, again, "CDEMO", interval=0), 1)
        self.assertEqual(again.call_args.kwargs["oldest"], "10.000002")
        self.assertEqual(self.db.execute("SELECT ts FROM checkpoints").fetchone()[0], "10.000003")

    def test_partial_failure_rolls_back_and_can_resume(self):
        a.sync_channel(self.db, Mock(return_value=page("1.000000")), "CDEMO", interval=0)
        broken = Mock(side_effect=[page("3.000000", cursor="next", more=True), a.ArchiveError("Unavailable")])
        with self.assertRaises(a.ArchiveError):
            a.sync_channel(self.db, broken, "CDEMO", interval=0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT ts FROM checkpoints").fetchone()[0], "1.000000")
        self.assertEqual(a.sync_channel(self.db, Mock(return_value=page("3.000000", "2.000000")),
                                        "CDEMO", interval=0), 2)

    def test_malformed_pages_fail_closed(self):
        for bad in [{"ok": True}, page("NaN"), page("1.000000", more=True),
                    {**page(), "messages": [None]}, {**page(), "has_more": "false"},
                    {**page(), "response_metadata": None}, {**page(), "ok": False}]:
            with self.subTest(bad=bad), self.assertRaises(a.ArchiveError):
                a.sync_channel(self.db, Mock(return_value=bad), "CDEMO", interval=0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_repeated_cursor_and_page_budget_fail(self):
        call = Mock(return_value=page("1.000000", cursor="loop", more=True))
        with self.assertRaises(a.ArchiveError):
            a.sync_channel(self.db, call, "CDEMO", interval=0)
        self.assertEqual(call.call_count, 2)
        with self.assertRaises(a.ArchiveError):
            a.sync_channel(self.db, call, "CDEMO", interval=0, max_pages=1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 0)

    def test_workspace_binding_rejects_other_team(self):
        with self.assertRaisesRegex(a.ArchiveError, "workspace"):
            a.open_archive(self.path, "TOTHER")
        self.assertEqual(self.db.execute("SELECT team FROM workspace").fetchone()[0], "TDEMO")

    def test_export_exact_order_and_no_overwrite(self):
        a.sync_channel(self.db, Mock(return_value=page("10.000001", "9.999999")), "CDEMO", interval=0)
        target = Path(self.tmp.name) / "export.json"
        a.export_channel(self.db, "CDEMO", target)
        doc = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(doc["team_id"], "TDEMO")
        self.assertEqual([m["ts"] for m in doc["messages"]], ["9.999999", "10.000001"])
        original = target.read_bytes()
        with self.assertRaises(FileExistsError):
            a.export_channel(self.db, "CDEMO", target)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(target.parent.glob(".slack-export-*.tmp")), [])

    def test_failed_export_write_leaves_no_output_or_staging_file(self):
        a.sync_channel(self.db, Mock(return_value=page("1.000000")), "CDEMO", interval=0)
        target = Path(self.tmp.name) / "exports" / "export.json"
        real_open = io.open

        def fail_write(*args, **kwargs):
            file = real_open(*args, **kwargs)
            broken = MagicMock(wraps=file)
            broken.__enter__.return_value = broken
            broken.__exit__.side_effect = file.__exit__

            def disk_full(text):
                file.write(text[:10])
                raise OSError("Simulated disk full")

            broken.write.side_effect = disk_full
            return broken

        with patch("pathlib.Path.open", autospec=True, side_effect=fail_write), \
                patch("io.open", side_effect=fail_write), self.assertRaisesRegex(OSError, "disk full"):
            a.export_channel(self.db, "CDEMO", target)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_export_publication_failures_preserve_competing_output(self):
        a.sync_channel(self.db, Mock(return_value=page("1.000000")), "CDEMO", interval=0)
        real_link = os.link
        for failure in ["competing", "unsupported"]:
            with self.subTest(failure=failure):
                target = Path(self.tmp.name) / failure / "export.json"

                def publish(source, destination):
                    staged = Path(source)
                    self.assertFalse(target.exists())
                    self.assertEqual(staged.parent, target.parent)
                    self.assertEqual(len(json.loads(staged.read_text(encoding="utf-8"))["messages"]), 1)
                    if os.name == "posix":
                        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
                    if failure == "unsupported":
                        raise OSError("Hard links unavailable")
                    target.write_text("Competing export", encoding="utf-8")
                    real_link(source, destination)

                with patch("archiver.os.link", side_effect=publish) as link, self.assertRaises(OSError):
                    a.export_channel(self.db, "CDEMO", target)
                link.assert_called_once()
                if failure == "competing":
                    self.assertEqual(target.read_text(encoding="utf-8"), "Competing export")
                self.assertEqual(list(target.parent.iterdir()), [target] if failure == "competing" else [])


class AuthAndTransportTests(unittest.TestCase):
    def identity(self, **changes):
        user = {"id": "UDEMO", "team_id": "TDEMO", "is_owner": True, "is_bot": False, "deleted": False}
        user.update(changes)
        return Mock(side_effect=[{"ok": True, "team_id": "TDEMO", "user_id": "UDEMO"},
                                 {"ok": True, "user": user}])

    def test_only_verified_active_workspace_owner(self):
        self.assertEqual(a.require_owner(self.identity()), "TDEMO")
        for change in [{"is_owner": False}, {"is_owner": "true"}, {"is_bot": True},
                       {"deleted": True}, {"team_id": "TOTHER"}, {"id": "UOTHER"}, {"is_bot": None}]:
            with self.subTest(change=change), self.assertRaises(a.ArchiveError):
                a.require_owner(self.identity(**change))

    def test_bot_auth_and_malformed_auth_rejected(self):
        for auth in [{"ok": True}, {"ok": True, "team_id": "TDEMO", "user_id": "UDEMO", "bot_id": "BDEMO"}]:
            with self.assertRaises(a.ArchiveError):
                a.require_owner(Mock(return_value=auth))

    def test_api_error_is_sanitized(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":false,"error":"SECRET_PAYLOAD"}'
        with patch("archiver.build_opener", return_value=opener):
            with self.assertRaises(a.ArchiveError) as caught:
                a.slack_call("test-secret", "auth.test")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_rate_limit_returns_retry_after_without_retrying(self):
        opener = MagicMock()
        opener.open.side_effect = HTTPError("https://slack.com/api/auth.test", 429, "limit",
                                           {"Retry-After": "60"}, io.BytesIO(b"SECRET"))
        with patch("archiver.build_opener", return_value=opener):
            with self.assertRaisesRegex(a.ArchiveError, "60 seconds"):
                a.slack_call("test-secret", "auth.test")
        self.assertEqual(opener.open.call_count, 1)

    def test_unapproved_endpoint_and_invalid_json_rejected(self):
        with self.assertRaises(a.ArchiveError):
            a.slack_call("test-secret", "https://example.com")
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b"not-json"
        with patch("archiver.build_opener", return_value=opener), self.assertRaises(a.ArchiveError):
            a.slack_call("test-secret", "auth.test")

    def test_transport_uses_fixed_origin_header_and_timeout(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
        with patch("archiver.build_opener", return_value=opener):
            a.slack_call("test-secret", "conversations.history", channel="CDEMO", cursor="a+b=")
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://slack.com/api/conversations.history?"))
        self.assertNotIn("test-secret", request.full_url)
        self.assertIn("cursor=a%2Bb%3D", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
        self.assertIsNone(a.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_transport_failures_are_safe(self):
        opener = MagicMock()
        for error in [TimeoutError("secret"), HTTPError("secret", 500, "secret", {}, None)]:
            opener.open.side_effect = error
            with patch("archiver.build_opener", return_value=opener), self.assertRaises(a.ArchiveError) as caught:
                a.slack_call("test-secret", "auth.test")
            self.assertNotIn("secret", str(caught.exception))


class CLITests(unittest.TestCase):
    def test_offline_demo_export_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            script = str(Path(a.__file__).resolve())
            env = {key: value for key, value in os.environ.items() if key != "SLACK_TOKEN"}
            def run(*args):
                return subprocess.run([sys.executable, script, *args], cwd=directory, env=env,
                                      text=True, capture_output=True, timeout=10)
            first = run("demo")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("3 new", first.stdout)
            self.assertIn("0 new", run("demo").stdout)
            exported = run("export", "--db", "data/demo.db", "--channel", "CDEMO", "--output", "exports/demo.json")
            self.assertEqual(exported.returncode, 0, exported.stderr)
            doc = json.loads((Path(directory) / "exports/demo.json").read_text(encoding="utf-8"))
            self.assertTrue(doc["synthetic"])
            self.assertEqual(len(doc["messages"]), 3)
            self.assertNotEqual(run("sync", "--channel", "CDEMO").returncode, 0)
            self.assertFalse((Path(directory) / "data/archive.db").exists())


if __name__ == "__main__":
    unittest.main()
