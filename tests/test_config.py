import unittest

from mailoffload.config import ConfigError, human_bytes, parse_size


class ParseSizeTests(unittest.TestCase):
    def test_plain_number_is_bytes(self):
        self.assertEqual(parse_size("1234"), 1234)

    def test_suffixes(self):
        self.assertEqual(parse_size("2G"), 2 * 1024 ** 3)
        self.assertEqual(parse_size("500M"), 500 * 1024 ** 2)
        self.assertEqual(parse_size("1.5k"), int(1.5 * 1024))

    def test_garbage_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_size("lots")


class HumanBytesTests(unittest.TestCase):
    def test_readable(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(1536), "1.5 KB")
        self.assertEqual(human_bytes(15 * 1024 ** 3), "15.0 GB")
