"""Message queue abstraction with at-least-once delivery, bounded retries and a dead-letter stream.

* RedisQueue     : Redis Streams + consumer groups (production; durable, horizontally scalable, crash-recoverable)
* InMemoryQueue  : same semantics in-process (tests, single-process demos, evals)

Consumers MUST be idempotent (delivery is at-least-once) — every handler in this codebase is: tickets
transition only from `queued`, approval notifications key on approval_id, etc.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger("bankai.queue")
Handler = Callable[[dict], Awaitable[None]]
MAX_ATTEMPTS = 3


class MessageQueue(Protocol):
    async def publish(self, topic: str, payload: dict) -> None: ...
    async def consume_loop(self, topic: str, handler: Handler, stop: asyncio.Event, group: str = "workers") -> None: ...
    async def depth(self, topic: str) -> int: ...
    async def dead_letters(self, topic: str) -> list[dict]: ...
    async def close(self) -> None: ...


class InMemoryQueue:
    def __init__(self) -> None:
        self._q: dict[str, deque[tuple[dict, int]]] = defaultdict(deque)
        self._dlq: dict[str, list[dict]] = defaultdict(list)
        self._event = asyncio.Event()
        self.published: list[tuple[str, dict]] = []  # inspectable by tests/demos

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))
        self._q[topic].append((payload, 1))
        self._event.set()

    async def drain(self, topic: str, handler: Handler) -> int:
        """Process everything currently queued (deterministic; used by tests and the eval harness)."""
        n = 0
        while self._q[topic]:
            payload, attempt = self._q[topic].popleft()
            n += 1
            try:
                await handler(payload)
            except Exception as e:  # noqa: BLE001
                log.warning("handler failed topic=%s attempt=%s: %s", topic, attempt, type(e).__name__)
                if attempt >= MAX_ATTEMPTS:
                    self._dlq[topic].append({"payload": payload, "error": type(e).__name__})
                else:
                    self._q[topic].append((payload, attempt + 1))
        return n

    async def consume_loop(self, topic: str, handler: Handler, stop: asyncio.Event, group: str = "workers") -> None:
        while not stop.is_set():
            if not self._q[topic]:
                self._event.clear()
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
            await self.drain(topic, handler)

    async def depth(self, topic: str) -> int:
        return len(self._q[topic])

    async def dead_letters(self, topic: str) -> list[dict]:
        return list(self._dlq[topic])

    async def close(self) -> None:
        return None


class RedisQueue:
    def __init__(self, url: str | None = None, client: Any = None, consumer: str = "c1"):
        import redis.asyncio as aioredis
        self.r = client or aioredis.from_url(url, decode_responses=True)
        self.consumer = consumer

    @staticmethod
    def _s(topic: str) -> str:
        return f"bankai:stream:{topic}"

    async def publish(self, topic: str, payload: dict) -> None:
        await self.r.xadd(self._s(topic), {"data": json.dumps(payload), "attempt": 1}, maxlen=10_000, approximate=True)

    async def _ensure_group(self, topic: str, group: str) -> None:
        try:
            await self.r.xgroup_create(self._s(topic), group, id="0", mkstream=True)
        except Exception as e:  # noqa: BLE001 - BUSYGROUP is expected on every restart
            if "BUSYGROUP" not in str(e):
                raise

    async def consume_loop(self, topic: str, handler: Handler, stop: asyncio.Event, group: str = "workers") -> None:
        s = self._s(topic)
        await self._ensure_group(topic, group)
        while not stop.is_set():
            try:
                # crash recovery: take over entries another consumer read but never acked
                claimed = await self.r.xautoclaim(s, group, self.consumer, min_idle_time=30_000, start_id="0-0", count=10)
                entries = claimed[1] if claimed else []
                fresh = await self.r.xreadgroup(group, self.consumer, {s: ">"}, count=10, block=500)
                for _, msgs in fresh or []:
                    entries = list(entries) + list(msgs)
                for msg_id, fields in entries:
                    await self._handle(topic, group, msg_id, fields, handler)
                # never rely on the broker's BLOCK for backpressure: always yield, and idle briefly when there was no work
                await asyncio.sleep(0 if entries else 0.05)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - broker blips must not kill the worker
                log.warning("redis consume error: %s", type(e).__name__)
                await asyncio.sleep(1)

    async def _handle(self, topic: str, group: str, msg_id: str, fields: dict, handler: Handler) -> None:
        s, attempt = self._s(topic), int(fields.get("attempt", 1))
        payload = json.loads(fields["data"])
        try:
            await handler(payload)
        except Exception as e:  # noqa: BLE001
            if attempt >= MAX_ATTEMPTS:
                await self.r.xadd(f"bankai:dlq:{topic}", {"data": fields["data"], "error": type(e).__name__})
            else:
                await self.r.xadd(s, {"data": fields["data"], "attempt": attempt + 1})
        await self.r.xack(s, group, msg_id)

    async def depth(self, topic: str) -> int:
        return await self.r.xlen(self._s(topic))

    async def dead_letters(self, topic: str) -> list[dict]:
        rows = await self.r.xrange(f"bankai:dlq:{topic}")
        return [{"payload": json.loads(f["data"]), "error": f.get("error")} for _, f in rows]

    async def close(self) -> None:
        await self.r.aclose()


def make_queue(redis_url: str) -> MessageQueue:
    return RedisQueue(redis_url) if redis_url else InMemoryQueue()
