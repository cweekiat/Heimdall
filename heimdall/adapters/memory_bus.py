"""In-process Bus: per-topic fan-out to bounded asyncio queues (drop-oldest + counter)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence

from pydantic import BaseModel

_CLOSED = object()


class MemoryBus:
    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._subs: dict[str, list[asyncio.Queue[object]]] = defaultdict(list)
        self.dropped: dict[str, int] = defaultdict(int)
        self._closed = False

    async def publish(self, topic: str, batch: Sequence[BaseModel]) -> None:
        if self._closed:
            return
        for q in self._subs[topic]:
            while q.full():
                try:
                    q.get_nowait()
                    self.dropped[topic] += 1
                except asyncio.QueueEmpty:  # pragma: no cover
                    break
            q.put_nowait(list(batch))

    def subscribe(self, topic: str) -> AsyncIterator[Sequence[BaseModel]]:
        # Register eagerly so batches published right after subscribe() are not lost.
        q: asyncio.Queue[object] = asyncio.Queue(maxsize=self._maxsize)
        self._subs[topic].append(q)
        return self._iter(topic, q)

    async def _iter(
        self, topic: str, q: asyncio.Queue[object]
    ) -> AsyncIterator[Sequence[BaseModel]]:
        try:
            while True:
                item = await q.get()
                if item is _CLOSED:
                    return
                yield item  # type: ignore[misc]
        finally:
            if q in self._subs[topic]:
                self._subs[topic].remove(q)

    async def close(self) -> None:
        self._closed = True
        for qs in self._subs.values():
            for q in qs:
                while q.full():
                    q.get_nowait()
                q.put_nowait(_CLOSED)
