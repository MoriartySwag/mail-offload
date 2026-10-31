import tempfile
import unittest
from pathlib import Path

from mailoffload.config import ConfigError, load_config


SAMPLE = """
[account]
user = someone@gmail.com
password = abcdefghijklmnop
host = imap.gmail.com
port = 993

[policy]
trigger_percent = 85
target_percent = 60
min_age_days = 365
max_messages_per_run = 25
max_bytes_per_run = 100M
order = oldest

[storage]
archive_dir = {archive}
state_dir = {state}
"""


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.ini"
        self.config_path.write_text(
            SAMPLE.format(archive=self.root / "archive", state=self.root / "state")
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_values(self):
        config = load_config(self.config_path, env={})
        self.assertEqual(config.account.user, "someone@gmail.com")
        self.assertEqual(config.policy.target_percent, 60.0)
        self.assertEqual(config.policy.min_age_days, 365)
        self.assertEqual(config.policy.max_bytes_per_run, 100 * 1024 ** 2)
        self.assertEqual(config.policy.order, "oldest")
        self.assertEqual(config.archive_dir, self.root / "archive")

    def test_environment_wins(self):
        config = load_config(
            self.config_path,
            env={"MAILOFFLOAD_USER": "other@gmail.com", "MAILOFFLOAD_APP_PASSWORD": "pw"},
        )
        self.assertEqual(config.account.user, "other@gmail.com")
        self.assertEqual(config.account.password, "pw")

    def test_password_is_not_in_repr(self):
        config = load_config(self.config_path, env={})
        self.assertNotIn("abcdefghijklmnop", repr(config.account))

    def test_bad_order_is_rejected(self):
        text = self.config_path.read_text().replace("order = oldest", "order = sideways")
        self.config_path.write_text(text)
        with self.assertRaises(ConfigError):
            load_config(self.config_path, env={})

    def test_target_above_trigger_is_rejected(self):
        text = self.config_path.read_text().replace("target_percent = 60", "target_percent = 90")
        self.config_path.write_text(text)
        with self.assertRaises(ConfigError):
            load_config(self.config_path, env={})

    def test_missing_user_is_rejected(self):
        text = self.config_path.read_text().replace("user = someone@gmail.com", "user =")
        self.config_path.write_text(text)
        with self.assertRaises(ConfigError):
            load_config(self.config_path, env={})
