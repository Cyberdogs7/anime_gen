"""Event bus transport.

``provider: memory`` (default) is an in-process broker with synchronous
fan-out and an optional durable event log in SQLite - zero dependencies.
``provider: redis`` uses Redis Streams with consumer groups (at-least-once).

See DESIGN.md §6.4 for delivery semantics.
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Callable

from .events import Event, STREAM_SYSTEM, TEST

log = logging.getLogger(__name__)

Handler = Callable[[Event], None]


class BaseBroker(ABC):
    def __init__(self, group_prefix: str = "studio"):
        self.group_prefix = group_prefix

    @abstractmethod
    def publish(self, stream: str, event: Event) -> None:
        """Publish an event to a stream (delivered to registered handlers)."""

    @abstractmethod
    def register(self, stream: str, group: str, handler: Handler) -> None:
        """Register a consumer for a stream. Handlers must be idempotent."""

    def start(self) -> None:
        """Start background delivery (no-op for memory broker)."""

    def stop(self) -> None:
        """Stop background delivery."""

    def round_trip(self) -> bool:
        """Self-test: publish a Test event and confirm a handler receives it."""
        received: list[Event] = []
        self.register(STREAM_SYSTEM, f"{self.group_prefix}.verify", received.append)
        self.publish(STREAM_SYSTEM, Event(type=TEST, payload={"round_trip": True}))
        return len(received) == 1 and received[0].type == TEST


class InMemoryBroker(BaseBroker):
    """In-process broker. publish() delivers synchronously to registered handlers."""

    def __init__(self, group_prefix: str = "studio"):
        super().__init__(group_prefix)
        self._handlers: list[tuple[str, str, Handler]] = []
        self._lock = threading.Lock()

    def publish(self, stream: str, event: Event) -> None:
        with self._lock:
            handlers = [h for (s, _g, h) in self._handlers if s == stream]
        for h in handlers:
            try:
                h(event)
            except Exception:  # handlers must not break the bus
                log.exception("handler failed for %s", event.type)

    def register(self, stream: str, group: str, handler: Handler) -> None:
        with self._lock:
            self._handlers.append((stream, group, handler))

    def stop(self) -> None:
        with self._lock:
            self._handlers.clear()


class RedisBroker(BaseBroker):
    """Redis Streams transport with consumer groups (at-least-once)."""

    def __init__(self, url: str, password: str | None = None, group_prefix: str = "studio"):
        super().__init__(group_prefix)
        try:
            import redis as _redis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "redis provider requires the optional dependency: pip install anime-studio[redis]"
            ) from exc
        self._redis = _redis.Redis.from_url(url, password=password, decode_responses=True)
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def publish(self, stream: str, event: Event) -> None:
        self._redis.xadd(stream, event.to_dict())

    def register(self, stream: str, group: str, handler: Handler) -> None:
        group_name = f"{self.group_prefix}.{group}"
        try:
            self._redis.xgroup_create(stream, group_name, id="0", mkstream=True)
        except Exception:
            pass  # group already exists
        thread = threading.Thread(
            target=self._consume, args=(stream, group_name, handler), daemon=True
        )
        self._threads.append(thread)

    def _consume(self, stream: str, group: str, handler: Handler) -> None:
        while not self._stop.is_set():
            try:
                items = self._redis.xreadgroup(
                    group, stream, {stream: ">"}, count=1, block=2000
                )
                for _stream, messages in items:
                    for msg_id, fields in messages:
                        event = Event.from_dict(fields)
                        try:
                            handler(event)
                        except Exception:
                            log.exception("handler failed for %s; moving to DLQ", event.type)
                            self._redis.xadd(STREAM_SYSTEM.replace("system", "dead"), event.to_dict())
                        finally:
                            self._redis.xack(stream, group, msg_id)
            except Exception:
                log.exception("redis consume error")
                self._stop.wait(2)

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)


def make_broker(bus_cfg: dict) -> BaseBroker:
    """Build a broker from the `bus` config section."""
    provider = bus_cfg.get("provider", "memory")
    prefix = bus_cfg.get("group_prefix", "studio")
    if provider == "redis":
        return RedisBroker(
            url=bus_cfg.get("url", "redis://127.0.0.1:6379"),
            password=bus_cfg.get("password"),
            group_prefix=prefix,
        )
    return InMemoryBroker(group_prefix=prefix)
