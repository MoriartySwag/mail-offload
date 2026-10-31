import tempfile
import unittest
from pathlib import Path

from mailoffload.mbox import build_mboxes, mbox_escape


class EscapeTests(unittest.TestCase):
    def test_leading_from_is_escaped(self):
        self.assertEqual(mbox_escape(b"From me\nhello\n"), b">From me\nhello\n")

    def test_from_inside_a_line_is_untouched(self):
        self.assertEqual(mbox_escape(b"mail from me\n"), b"mail from me\n")


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "archive"
        for year in ("2019", "2020"):
            folder = self.source / year / "01"
            folder.mkdir(parents=True)
            (folder / f"{year}-01-02_5_abcdef12.eml").write_bytes(
                f"Subject: {year}\r\n\r\nFrom here\r\n".encode()
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_mbox_per_year(self):
        files = build_mboxes(self.source, self.root / "out")
        self.assertEqual([f.name for f in files], ["MailArchive-2019.mbox", "MailArchive-2020.mbox"])

    def test_separator_and_escaping(self):
        build_mboxes(self.source, self.root / "out")
        text = (self.root / "out" / "MailArchive-2019.mbox").read_bytes()
        self.assertTrue(text.startswith(b"From MAILER-DAEMON "))
        self.assertIn(b">From here", text)
