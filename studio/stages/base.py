"""Stage base class + registry.

Every stage is an event consumer: dedup by event_id, run, ack. See
DESIGN.md §9 (commit-then-ack discipline).
"""
from __future__ import annotations

import logging
from typing import Callable

from ..bus.events import Event
from ..db import DB

log = logging.getLogger(__name__)


class Stage:
    """A stage consumer. Subclasses implement handle_event()."""

    #: event type(s) this stage reacts to
    consumes: tuple[str, ...] = ()

    def __init__(self, db: DB, run_id: int = 0):
        self.db = db
        self.run_id = run_id

    def handle(self, event: Event) -> None:
        """Dedup gate then dispatch. Handlers must be idempotent."""
        if self.db.event_seen(event.event_id):
            return
        self.db.record_event(event.event_id, event.type, "bus", event.show_id, event.ts,
                             event.payload)
        self.handle_event(event)

    def handle_event(self, event: Event) -> None:
        raise NotImplementedError


# Registry: event type -> handler factory/function
_REGISTRY: dict[str, Callable[[Event], None]] = {}


def register_stage(event_types: tuple[str, ...]):
    """Decorator: register a callable(event) for the given event types."""

    def wrap(fn: Callable[[Event], None]) -> Callable[[Event], None]:
        for etype in event_types:
            _REGISTRY[etype] = fn
        return fn

    return wrap


def dispatch(event: Event) -> None:
    """Dispatch a single event to its registered handler (if any)."""
    handler = _REGISTRY.get(event.type)
    if handler is None:
        log.warning("no handler for event %s", event.type)
        return
    handler(event)
