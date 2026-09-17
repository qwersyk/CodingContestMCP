"""Bounded, process-local game state. No website cookies or HTTP clients are kept."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

from .client import APIError

if TYPE_CHECKING:
    from .service import Game


def retry_seconds(error: APIError) -> int:
    value = error.retry_after
    if value is None and isinstance(error.detail, dict):
        value = error.detail.get(
            "retryAfterSeconds",
            error.detail.get("retryAfter", error.detail.get("cooldownSec")),
        )
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = 30
    return max(1, math.ceil(seconds)) if math.isfinite(seconds) else 30


@dataclass
class GameSessions:
    games: dict[str, Game] = field(default_factory=dict, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    submission_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    submission_cooldowns: dict[str, float] = field(default_factory=dict, repr=False)
    blocked_until: float = 0
    users: int = 0
    touched: float = field(default_factory=time.monotonic)

    def can_evict(self, now):
        return (
            not self.users
            and self.blocked_until <= now
            and all(deadline <= now for deadline in self.submission_cooldowns.values())
        )

    def check_cooldown(self):
        remaining = math.ceil(self.blocked_until - time.monotonic())
        if remaining > 0:
            raise APIError(
                429,
                {
                    "source": "codingcontest.org",
                    "endpoint": "/api/game-token",
                    "message": "Shared token cooldown; no upstream request was sent",
                },
                str(remaining),
            )

    def rate_limited(self, error):
        seconds = retry_seconds(error)
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)
        error.retry_after = str(seconds)


class AccountSessions:
    def __init__(self, limit=1024, idle_seconds=900):
        self.entries: OrderedDict[str, GameSessions] = OrderedDict()
        self.limit, self.idle_seconds = limit, idle_seconds

    @contextmanager
    def use(self, account):
        now = time.monotonic()
        for key, value in list(self.entries.items()):
            if value.can_evict(now) and now - value.touched > self.idle_seconds:
                del self.entries[key]
        state = self.entries.get(account)
        if state is None:
            if len(self.entries) >= self.limit:
                victim = next(
                    (
                        key
                        for key, value in self.entries.items()
                        if value.can_evict(now)
                    ),
                    None,
                )
                if victim is None:
                    raise APIError(503, "Game session cache busy", "1")
                del self.entries[victim]
            state = self.entries[account] = GameSessions()
        self.entries.move_to_end(account)
        state.users += 1
        try:
            yield state
        finally:
            state.users -= 1
            state.touched = time.monotonic()
