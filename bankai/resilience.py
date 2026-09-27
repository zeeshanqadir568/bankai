"""Timeouts, retries with exponential backoff + jitter, and circuit breakers for LLM and MCP calls.

Retry policy is deliberately narrow: only *transient* failures (timeouts, connection errors, breaker-safe
5xx) are retried; business/authorization errors are answers, not failures, and are never retried.
Mutating tool calls are safe to retry only because they carry idempotency keys.
"""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from .common import clock

T = TypeVar("T")


class CircuitOpenError(Exception):
    def __init__(self, name: str):
        super().__init__(f"circuit '{name}' is open")
        self.name = name


class TransientError(Exception):
    """Raise (or wrap into) this for failures worth retrying."""


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (reset_seconds) -> half_open -> success -> closed / failure -> open."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_seconds: float = 20.0):
        self.name, self.threshold, self.reset = name, failure_threshold, reset_seconds
        self.failures, self.opened_at, self._probing = 0, None, False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if clock.now() - self.opened_at >= self.reset else "open"

    def before_call(self) -> None:
        st = self.state
        if st == "open":
            raise CircuitOpenError(self.name)
        if st == "half_open":
            if self._probing:
                raise CircuitOpenError(self.name)  # only one probe at a time
            self._probing = True

    def on_success(self) -> None:
        self.failures, self.opened_at, self._probing = 0, None, False

    def on_neutral(self) -> None:
        self._probing = False

    def on_failure(self) -> None:
        self._probing = False
        self.failures += 1
        if self.failures >= self.threshold or self.opened_at is not None:
            self.opened_at = clock.now()


async def resilient_call(
    fn: Callable[[], Awaitable[T]], *, breaker: CircuitBreaker | None = None, timeout: float = 10.0,
    attempts: int = 3, base_delay: float = 0.2, max_delay: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (TransientError, asyncio.TimeoutError, ConnectionError, OSError),
) -> T:
    last: BaseException | None = None
    for i in range(attempts):
        if breaker:
            breaker.before_call()  # raises CircuitOpenError: fail fast, do not hammer a dead dependency
        try:
            result = await asyncio.wait_for(fn(), timeout=timeout)
            if breaker:
                breaker.on_success()
            return result
        except retry_on as e:
            last = e
            if breaker:
                breaker.on_failure()
            if i == attempts - 1:
                break
            await asyncio.sleep(min(max_delay, base_delay * 2**i) * (0.5 + random.random() / 2))
        except BaseException:
            if breaker:
                breaker.on_neutral()  # a non-transient error means the dependency answered; don't count it as a failure
            raise
    assert last is not None
    raise last
