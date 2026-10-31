"""Small value objects shared by the rest of the tool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Quota:
    """How much of the mailbox is used, in bytes."""

    used: int
    limit: int
    source: str = "server"

    @property
    def percent(self) -> float:
        if self.limit <= 0:
            return 0.0
        return 100.0 * self.used / self.limit

    @property
    def free(self) -> int:
        return max(0, self.limit - self.used)


@dataclass(frozen=True)
class MessageInfo:
    """What we know about a message before fetching its body."""

    uid: str
    size: int
    internal_date: datetime
    message_id: Optional[str] = None
    subject: Optional[str] = None

    @property
    def short_subject(self) -> str:
        if not self.subject:
            return "(no subject)"
        text = " ".join(self.subject.split())
        return text if len(text) <= 60 else text[:57] + "..."
