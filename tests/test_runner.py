import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mailoffload.archive import Archive
from mailoffload.config import Account, Config, Policy
from mailoffload.gmail import GmailError
from mailoffload.models import MessageInfo, Quota
from mailoffload.runner import read_usage, run, verify_archive

GIB = 1024 ** 3


class FakeSession:
    def __init__(self, messages, used=13 * GIB, limit=15 * GIB, fail_fetch=()):
        self.messages = messages
        self.used = used
        self.limit = limit
        self.fail_fetch = set(fail_fetch)
        self.deleted = []
        self.purged = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def quota(self):
        return Quota(used=self.used, limit=self.limit)

    def scan(self, older_than, progress=None):
        return list(self.messages)

    def fetch_raw(self, uid):
        if uid in self.fail_fetch:
            raise GmailError("simulated network failure")
        for message in self.messages:
            if message.uid == uid:
                return b"x" * message.size
        raise GmailError("not found")

    def delete_messages(self, uids):
        self.deleted.extend(uids)
        return len(uids)

    def purge_from_trash(self, message_ids):
        ids = [mid for mid in message_ids if mid]
        self.purged.extend(ids)
        return len(ids)


def make_messages():
    return [
        MessageInfo(uid="1", size=1024 * 1024,
                    internal_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
                    message_id="<a@example.com>"),
        MessageInfo(uid="2", size=2 * 1024 * 1024,
                    internal_date=datetime(2020, 2, 1, tzinfo=timezone.utc),
                    message_id="<b@example.com>"),
        MessageInfo(uid="3", size=3 * 1024 * 1024,
                    internal_date=datetime(2020, 3, 1, tzinfo=timezone.utc),
                    message_id="<c@example.com>"),
    ]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = Config(
            account=Account(user="me@example.com", password="pw"),
            policy=Policy(
                trigger_percent=80,
                target_percent=70,
                min_age_days=180,
                max_messages_per_run=500,
                max_bytes_per_run=2 * GIB,
            ),
            archive_dir=root / "archive",
            state_dir=root / "state",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, session, **kwargs):
        return run(self.config, session_factory=lambda: session, **kwargs)

    def test_below_threshold_does_nothing(self):
        session = FakeSession(make_messages(), used=1 * GIB)
        report = self.run_with(session, apply=True)
        self.assertEqual(report.status, "below-threshold")
        self.assertEqual(session.deleted, [])
        self.assertFalse((self.config.archive_dir).exists())

    def test_rehearsal_copies_but_never_deletes(self):
        session = FakeSession(make_messages())
        report = self.run_with(session, apply=False)
        self.assertEqual(report.status, "archived-only")
        self.assertEqual(len(report.archived), 3)
        self.assertEqual(session.deleted, [])
        self.assertEqual(len(list(self.config.archive_dir.rglob("*.eml"))), 3)

    def test_apply_deletes_only_after_copying(self):
        session = FakeSession(make_messages())
        report = self.run_with(session, apply=True)
        self.assertEqual(report.status, "ok")
        self.assertEqual(sorted(session.deleted), ["1", "2", "3"])
        self.assertEqual(len(list(self.config.archive_dir.rglob("*.eml"))), 3)
        self.assertEqual(len(session.purged), 3)
        self.assertFalse(report.errors)

    def test_manifest_records_every_message(self):
        session = FakeSession(make_messages())
        self.run_with(session, apply=True)
        lines = (self.config.archive_dir / "manifest.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual({json.loads(line)["uid"] for line in lines}, {"1", "2", "3"})

    def test_unverified_copy_is_not_deleted(self):
        session = FakeSession(make_messages())

        def sometimes_false(archive_self, item):
            return item.uid != "2"

        with mock.patch.object(Archive, "verify", sometimes_false):
            report = self.run_with(session, apply=True)

        self.assertNotIn("2", session.deleted)
        self.assertEqual(sorted(session.deleted), ["1", "3"])
        self.assertTrue(any("will not be deleted" in e for e in report.errors))

    def test_fetch_failure_does_not_stop_the_rest(self):
        session = FakeSession(make_messages(), fail_fetch={"3"})
        report = self.run_with(session, apply=True)
        self.assertEqual(sorted(session.deleted), ["1", "2"])
        self.assertTrue(any("3" in error for error in report.errors))

    def test_plan_only_downloads_nothing(self):
        session = FakeSession(make_messages())
        report = self.run_with(session, plan_only=True)
        self.assertEqual(report.status, "plan")
        self.assertEqual(len(report.chosen), 3)
        self.assertFalse(self.config.archive_dir.exists())
        self.assertEqual(session.deleted, [])

    def test_max_messages_is_respected(self):
        session = FakeSession(make_messages())
        report = self.run_with(session, apply=True, max_messages=1)
        self.assertEqual(len(report.chosen), 1)
        self.assertEqual(len(session.deleted), 1)

    def test_run_log_is_written(self):
        session = FakeSession(make_messages())
        self.run_with(session, apply=True)
        entries = (self.config.state_dir / "runs.jsonl").read_text().splitlines()
        self.assertEqual(len(entries), 1)
        self.assertEqual(json.loads(entries[0])["deleted"], 3)


class VerifyArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = Config(
            account=Account(user="me@example.com", password="pw"),
            policy=Policy(),
            archive_dir=root / "archive",
            state_dir=root / "state",
        )
        self.archive = Archive(self.config.archive_dir)
        info = MessageInfo(uid="1", size=4,
                           internal_date=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.item = self.archive.write(b"data", info)
        self.archive.append_manifest(self.item, "r1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_archive_passes(self):
        self.assertTrue(verify_archive(self.config).ok)

    def test_missing_file_is_reported(self):
        self.item.path.unlink()
        result = verify_archive(self.config)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.missing), 1)

    def test_changed_file_is_reported(self):
        self.item.path.write_bytes(b"nope")
        result = verify_archive(self.config)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.damaged), 1)


class ReadUsageTests(unittest.TestCase):
    def test_falls_back_to_assumed_value(self):
        class Silent:
            def quota(self):
                return None

        quota = read_usage(Silent(), assume_used=13 * GIB)
        self.assertEqual(quota.used, 13 * GIB)
        self.assertEqual(quota.source, "assumed")

    def test_explains_when_nothing_works(self):
        class Silent:
            def quota(self):
                return None

        with self.assertRaises(GmailError):
            read_usage(Silent())
