import unittest
from datetime import datetime, timezone

from mailoffload.models import MessageInfo, Quota
from mailoffload.select import bytes_to_free, choose, needs_run


def message(uid, size, year=2020, month=1, day=1):
    return MessageInfo(
        uid=uid,
        size=size,
        internal_date=datetime(year, month, day, tzinfo=timezone.utc),
    )


class ThresholdTests(unittest.TestCase):
    def test_needs_run(self):
        quota = Quota(used=13 * 1024 ** 3, limit=15 * 1024 ** 3)
        self.assertTrue(needs_run(quota, 80))
        self.assertFalse(needs_run(quota, 90))

    def test_bytes_to_free(self):
        quota = Quota(used=13 * 1024 ** 3, limit=15 * 1024 ** 3)
        self.assertEqual(bytes_to_free(quota, 70), int(2.5 * 1024 ** 3))

    def test_nothing_to_free_when_below_target(self):
        quota = Quota(used=5 * 1024 ** 3, limit=15 * 1024 ** 3)
        self.assertEqual(bytes_to_free(quota, 70), 0)


class ChooseTests(unittest.TestCase):
    def test_largest_first_reaches_target_with_fewer_messages(self):
        pool = [message("1", 10), message("2", 100), message("3", 50)]
        chosen = choose(pool, target_bytes=60, max_messages=10, max_bytes=10 ** 6)
        self.assertEqual([m.uid for m in chosen], ["2"])

    def test_oldest_first(self):
        pool = [
            message("new", 10, 2022),
            message("old", 10, 2019),
            message("mid", 10, 2020),
        ]
        chosen = choose(pool, target_bytes=100, max_messages=10,
                        max_bytes=10 ** 6, order="oldest")
        self.assertEqual([m.uid for m in chosen], ["old", "mid", "new"])

    def test_message_cap(self):
        pool = [message(str(i), 100) for i in range(10)]
        chosen = choose(pool, target_bytes=10 ** 6, max_messages=3, max_bytes=10 ** 6)
        self.assertEqual(len(chosen), 3)

    def test_byte_cap(self):
        pool = [message(str(i), 100) for i in range(10)]
        chosen = choose(pool, target_bytes=10 ** 6, max_messages=100, max_bytes=250)
        self.assertEqual(len(chosen), 2)

    def test_stops_once_target_reached(self):
        pool = [message("a", 500), message("b", 500)]
        chosen = choose(pool, target_bytes=400, max_messages=10, max_bytes=10 ** 6)
        self.assertEqual(len(chosen), 1)

    def test_zero_size_messages_are_skipped(self):
        pool = [message("empty", 0), message("real", 10)]
        chosen = choose(pool, target_bytes=5, max_messages=10, max_bytes=10 ** 6)
        self.assertEqual([m.uid for m in chosen], ["real"])
