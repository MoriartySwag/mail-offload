import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mailoffload.archive import Archive
from mailoffload.models import MessageInfo

RAW = b"Subject: hello\r\nMessage-ID: <one@example.com>\r\n\r\nbody\r\n"


def info(uid="7", size=None):
    return MessageInfo(
        uid=uid,
        size=len(RAW) if size is None else size,
        internal_date=datetime(2020, 3, 4, 12, 0, tzinfo=timezone.utc),
        message_id="<one@example.com>",
        subject="hello",
    )


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_by_year_and_month(self):
        item = self.archive.write(RAW, info())
        self.assertTrue(item.path.is_file())
        self.assertEqual(item.path.parent.name, "03")
        self.assertEqual(item.path.parent.parent.name, "2020")
        self.assertEqual(item.path.read_bytes(), RAW)

    def test_verify_passes_for_fresh_copy(self):
        self.assertTrue(self.archive.verify(self.archive.write(RAW, info())))

    def test_verify_catches_a_changed_file(self):
        item = self.archive.write(RAW, info())
        item.path.write_bytes(b"tampered")
        self.assertFalse(self.archive.verify(item))

    def test_verify_catches_a_deleted_file(self):
        item = self.archive.write(RAW, info())
        item.path.unlink()
        self.assertFalse(self.archive.verify(item))

    def test_same_message_twice_does_not_duplicate(self):
        first = self.archive.write(RAW, info())
        second = self.archive.write(RAW, info())
        self.assertEqual(first.path, second.path)
        self.assertEqual(len(list(self.root.rglob("*.eml"))), 1)

    def test_manifest_line(self):
        item = self.archive.write(RAW, info())
        self.archive.append_manifest(item, "run123")
        lines = (self.root / "manifest.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["uid"], "7")
        self.assertEqual(entry["run"], "run123")
        self.assertEqual(entry["sha256"], item.sha256)
        self.assertTrue((self.root / entry["path"]).is_file())
