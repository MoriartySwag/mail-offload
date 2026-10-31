import unittest
from datetime import datetime, timezone

from mailoffload.gmail import (
    GmailSession,
    decode_subject,
    imap_date,
    imap_quote,
    iter_fetch_literals,
    parse_fetch_head,
    parse_list_line,
    parse_quota_response,
    unquote_mailbox,
)


class DateTests(unittest.TestCase):
    def test_english_month_names(self):
        self.assertEqual(imap_date(datetime(2020, 1, 5)), "05-Jan-2020")
        self.assertEqual(imap_date(datetime(2024, 12, 31)), "31-Dec-2024")


class QuoteTests(unittest.TestCase):
    def test_quotes_spaces(self):
        self.assertEqual(imap_quote("[Gmail]/All Mail"), '"[Gmail]/All Mail"')

    def test_round_trip(self):
        self.assertEqual(unquote_mailbox(imap_quote("[Gmail]/All Mail")), "[Gmail]/All Mail")


class ListLineTests(unittest.TestCase):
    def test_gmail_all_mail(self):
        flags, delimiter, name = parse_list_line(
            '(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"'
        )
        self.assertIn("\\All", flags)
        self.assertEqual(delimiter, "/")
        self.assertEqual(name, "[Gmail]/All Mail")

    def test_inbox(self):
        flags, _delimiter, name = parse_list_line('(\\HasNoChildren) "/" "INBOX"')
        self.assertEqual(flags, ["\\HasNoChildren"])
        self.assertEqual(name, "INBOX")


class QuotaTests(unittest.TestCase):
    def test_kilobytes_become_bytes(self):
        self.assertEqual(
            parse_quota_response('"" (STORAGE 3145728 15728640)'),
            (3145728 * 1024, 15728640 * 1024),
        )

    def test_unknown_is_none(self):
        self.assertIsNone(parse_quota_response('"" ()'))


class FetchParsingTests(unittest.TestCase):
    def test_head_fields(self):
        head = (
            '12 (UID 481 RFC822.SIZE 2048 '
            'INTERNALDATE "04-Mar-2020 12:00:00 +0000" '
            "BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT)] {57}"
        )
        uid, size, when = parse_fetch_head(head)
        self.assertEqual(uid, "481")
        self.assertEqual(size, 2048)
        self.assertEqual(when, datetime(2020, 3, 4, 12, 0, tzinfo=timezone.utc))

    def test_literals(self):
        reply = [
            (b"1 (UID 9 BODY[HEADER.FIELDS (MESSAGE-ID)] {20}", b"Message-ID: <a>\r\n\r\n"),
            b")",
        ]
        pairs = list(iter_fetch_literals(reply))
        self.assertEqual(len(pairs), 1)
        self.assertIn("UID 9", pairs[0][0])
        self.assertEqual(pairs[0][1], b"Message-ID: <a>\r\n\r\n")

    def test_subject_decoding(self):
        self.assertEqual(decode_subject("=?UTF-8?B?SGVsbG8=?="), "Hello")
        self.assertIsNone(decode_subject(None))


class LoginHelpTests(unittest.TestCase):
    def test_app_password_hint(self):
        text = GmailSession._explain_login_error(
            Exception("[ALERT] Application-specific password required")
        )
        self.assertIn("app password", text)

    def test_other_errors_are_passed_through(self):
        text = GmailSession._explain_login_error(Exception("CAPABILITY failure"))
        self.assertIn("CAPABILITY failure", text)
