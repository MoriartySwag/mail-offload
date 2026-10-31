"""Load and validate the small INI config file.

No third-party dependencies, so this runs on the Python that ships with
macOS. Everything has a default except the account name.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

CONFIG_SEARCH = (
    Path.home() / ".config" / "mailoffload" / "config.ini",
    Path("mailoffload.ini"),
)

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:b)?\s*$", re.IGNORECASE)
_MULTIPLIERS = {
    "": 1,
    "k": 1024,
    "m": 1024 ** 2,
    "g": 1024 ** 3,
    "t": 1024 ** 4,
}


class ConfigError(Exception):
    """Something in the config file does not make sense."""


def parse_size(text: str) -> int:
    """Turn '500M' or '2G' into a byte count. Plain numbers are bytes."""
    match = _SIZE_RE.match(str(text))
    if not match:
        raise ConfigError(f"Cannot read size {text!r} (try 500M or 2G)")
    value, suffix = match.groups()
    return int(float(value) * _MULTIPLIERS[suffix.lower()])


def human_bytes(count: float) -> str:
    """A byte count people can read."""
    steps = ("B", "KB", "MB", "GB", "TB")
    value = float(count)
    for step in steps:
        if abs(value) < 1024 or step == steps[-1]:
            if step == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {step}"
        value /= 1024.0
    return f"{value:.1f} TB"


@dataclass(frozen=True)
class Account:
    user: str
    password: str
    host: str = "imap.gmail.com"
    port: int = 993

    def __repr__(self) -> str:  # never print the password by accident
        return f"Account(user={self.user!r}, host={self.host!r}, port={self.port})"


@dataclass(frozen=True)
class Policy:
    """When to act, and how much to move."""

    trigger_percent: float = 80.0
    target_percent: float = 70.0
    min_age_days: int = 180
    max_messages_per_run: int = 500
    max_bytes_per_run: int = 2 * 1024 ** 3
    order: str = "largest"  # "largest" frees space fastest, "oldest" keeps years tidy


@dataclass(frozen=True)
class Config:
    account: Account
    policy: Policy
    archive_dir: Path
    state_dir: Path

    @property
    def manifest_path(self) -> Path:
        return self.archive_dir / "manifest.jsonl"

    @property
    def run_log_path(self) -> Path:
        return self.state_dir / "runs.jsonl"


def _find_config(path: Optional[Path]) -> Optional[Path]:
    if path is not None:
        return path
    for candidate in CONFIG_SEARCH:
        if candidate.is_file():
            return candidate
    return None


def _read_ini(path: Optional[Path]) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if path is None:
        return parser
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        try:
            parser.read_file(handle)
        except configparser.Error as exc:
            raise ConfigError(f"Could not read {path}: {exc}") from exc
    return parser


def _get(parser: configparser.ConfigParser, section: str, key: str) -> Optional[str]:
    if parser.has_option(section, key):
        value = parser.get(section, key)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def load_config(
    path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Config:
    """Build a Config from a file plus environment overrides.

    Precedence: environment > config file > defaults.
    """
    env = os.environ if env is None else env
    config_path = _find_config(Path(path).expanduser() if path else None)
    parser = _read_ini(config_path)

    user = env.get("MAILOFFLOAD_USER") or _get(parser, "account", "user") or ""
    password = env.get("MAILOFFLOAD_APP_PASSWORD") or _get(parser, "account", "password") or ""
    host = _get(parser, "account", "host") or env.get("MAILOFFLOAD_HOST") or "imap.gmail.com"
    port = int(_get(parser, "account", "port") or 993)

    policy = Policy(
        trigger_percent=float(_get(parser, "policy", "trigger_percent") or 80.0),
        target_percent=float(_get(parser, "policy", "target_percent") or 70.0),
        min_age_days=int(_get(parser, "policy", "min_age_days") or 180),
        max_messages_per_run=int(_get(parser, "policy", "max_messages_per_run") or 500),
        max_bytes_per_run=parse_size(
            _get(parser, "policy", "max_bytes_per_run") or "2G"
        ),
        order=(_get(parser, "policy", "order") or "largest").lower(),
    )

    archive_dir = Path(
        env.get("MAILOFFLOAD_ARCHIVE_DIR")
        or _get(parser, "storage", "archive_dir")
        or "~/MailArchive"
    ).expanduser()
    state_dir = Path(
        env.get("MAILOFFLOAD_STATE_DIR")
        or _get(parser, "storage", "state_dir")
        or "~/.local/state/mailoffload"
    ).expanduser()

    if not user:
        raise ConfigError(
            "No account configured. Run 'mailoffload init', then set 'user' in the config."
        )
    if policy.order not in ("largest", "oldest"):
        raise ConfigError("policy.order must be 'largest' or 'oldest'")
    if not (0 < policy.target_percent <= policy.trigger_percent <= 100):
        raise ConfigError(
            "policy must satisfy 0 < target_percent <= trigger_percent <= 100"
        )
    if policy.min_age_days < 1:
        raise ConfigError("policy.min_age_days must be at least 1")
    if policy.max_messages_per_run < 1:
        raise ConfigError("policy.max_messages_per_run must be at least 1")

    return Config(
        account=Account(user=user, password=password, host=host, port=port),
        policy=policy,
        archive_dir=archive_dir,
        state_dir=state_dir,
    )


TEMPLATE = """\
# mailoffload settings
# Keep this file private: it holds your app password. chmod 600 it.

[account]
user = you@gmail.com
# Use a Google *app password*, not your normal password.
# Leave this blank and export MAILOFFLOAD_APP_PASSWORD instead if you prefer.
password =
host = imap.gmail.com
port = 993

[policy]
# Start moving mail once the mailbox is this full...
trigger_percent = 80
# ...and stop when it is back down to this.
target_percent = 70
# Never touch anything newer than this many days.
min_age_days = 180
# Hard limits so one bad run cannot empty the account.
max_messages_per_run = 500
max_bytes_per_run = 2G
# "largest" frees space fastest; "oldest" keeps the archive tidy by year.
order = largest

[storage]
archive_dir = ~/MailArchive
state_dir = ~/.local/state/mailoffload
"""


def write_template(path: Path, force: bool = False) -> Path:
    """Create a starter config file that only the owner can read."""
    path = path.expanduser()
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
