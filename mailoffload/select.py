"""Decide which messages to move, and how many."""

from __future__ import annotations

from typing import List, Sequence

from .models import MessageInfo, Quota


def needs_run(quota: Quota, trigger_percent: float) -> bool:
    return quota.percent >= trigger_percent


def bytes_to_free(quota: Quota, target_percent: float) -> int:
    """How many bytes we need to remove to get back under the target."""
    target = int(quota.limit * (target_percent / 100.0))
    return max(0, quota.used - target)


def choose(
    messages: Sequence[MessageInfo],
    target_bytes: int,
    max_messages: int,
    max_bytes: int,
    order: str = "largest",
) -> List[MessageInfo]:
    """Pick the smallest set of messages that frees roughly target_bytes.

    Never picks more than max_messages or max_bytes, so a wrong config
    cannot empty the account in one go.
    """
    pool = [message for message in messages if message.size > 0]
    if order == "oldest":
        pool.sort(key=lambda message: (message.internal_date, message.uid))
    else:
        pool.sort(key=lambda message: (-message.size, message.internal_date))

    chosen: List[MessageInfo] = []
    total = 0
    for message in pool:
        if len(chosen) >= max_messages:
            break
        if total >= target_bytes:
            break
        if total + message.size > max_bytes:
            continue
        chosen.append(message)
        total += message.size
    return chosen
