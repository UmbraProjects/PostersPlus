"""Live-path MDBList micro-batcher.

A Stremio catalog page asks for 20–100 posters at once, and every one whose
rating isn't cached used to cost its own MDBList request.  Requests for the
same ``(provider, type, key)`` that arrive within ``window`` seconds of the
first (or until ``max_ids`` are waiting) share one bulk POST instead
(ratings.fetch_ratings_bulk).  The key is part of the group, so a client's own
``mdblist_key`` is only ever batched with requests made on that same key —
never with the server key's, nor anyone else's.

A batch of one goes by the single call: the same one request, and it still
carries keywords.  When the bulk call fails in a way that turned bulk off, the
titles it didn't answer fall back to single calls; while bulk is off, ``enabled()`` says
so and the caller skips the batcher altogether.

*gate* is an async context manager factory entered around every outbound
request — the process's MDBList concurrency semaphore and pacing — so a title
waiting in a batch holds neither.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from awards import FETCH_FAILED

logger = logging.getLogger(__name__)


class _Batch:
    __slots__ = ("waiters", "genre_ids", "media_types", "timer")

    def __init__(self):
        self.waiters: dict[str, list[asyncio.Future]] = {}
        self.genre_ids: dict[str, list[int]] = {}
        self.media_types: dict[str, str] = {}
        self.timer: asyncio.TimerHandle | None = None


class RatingBatcher:
    def __init__(
        self,
        *,
        single: Callable[..., Awaitable[Any]],
        bulk: Callable[..., Awaitable[Any]],
        bulk_available: Callable[[], bool],
        gate: Callable[[], Any],
        window: "float | Callable[[str], float]" = 0.1,
        max_ids: int = 200,
    ):
        self._single = single
        self._bulk = bulk
        self._bulk_available = bulk_available
        self._gate = gate
        self.window = window
        self.max_ids = max_ids
        self._batches: dict[tuple, _Batch] = {}
        self._tasks: set[asyncio.Task] = set()

    def enabled(self) -> bool:
        return self._bulk_available()

    async def fetch(self, client, key: str, genre_ids: list[int], media_type: str, *,
                    media_id: str, provider: str):
        """fetch_rating's answer for one title, by way of a shared batch."""
        mdb_type = "show" if media_type in ("tv", "series") else "movie"
        group = (provider, mdb_type, key, id(client))
        loop = asyncio.get_running_loop()
        batch = self._batches.get(group)
        if batch is None:
            batch = self._batches[group] = _Batch()
            # A callable window is asked per batch, so it can follow the key
            # (main._mdblist_batch_window: shorter while its quota is spare).
            window = self.window(key) if callable(self.window) else self.window
            batch.timer = loop.call_later(window, self._flush, group, client)
        media_id = str(media_id)
        fut = loop.create_future()
        batch.waiters.setdefault(media_id, []).append(fut)
        batch.genre_ids.setdefault(media_id, list(genre_ids or []))
        batch.media_types.setdefault(media_id, media_type)
        if len(batch.waiters) >= self.max_ids:
            self._flush(group, client)
        return await fut

    def _flush(self, group: tuple, client) -> None:
        batch = self._batches.pop(group, None)
        if batch is None:
            return
        if batch.timer is not None:
            batch.timer.cancel()
        task = asyncio.get_running_loop().create_task(self._run(group, client, batch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _single_for(self, client, key, provider, batch: _Batch, media_id: str):
        async with self._gate():
            return await self._single(
                client, key, batch.genre_ids[media_id], batch.media_types[media_id],
                media_id=media_id, provider=provider,
            )

    async def _run(self, group: tuple, client, batch: _Batch) -> None:
        provider, mdb_type, key, _ = group
        ids = list(batch.waiters)
        try:
            if len(ids) == 1:
                answers = {ids[0]: await self._single_for(client, key, provider, batch, ids[0])}
            else:
                async with self._gate():
                    result = await self._bulk(client, key, provider, mdb_type, ids, genre_ids=batch.genre_ids)
                if isinstance(result, dict):
                    answers = {i: result.get(i, FETCH_FAILED) for i in ids}
                else:
                    # _RateLimited, or a failure: the same answer for all.
                    answers = {i: result for i in ids}
                retry = [] if self._bulk_available() else [
                    i for i, a in answers.items() if a is FETCH_FAILED]
                if retry:
                    # Bulk just turned itself off: what it didn't answer goes
                    # the old way.  (A network failure with bulk still on is a
                    # failure for those titles, as it is for a single call.)
                    logger.info(f"MDBList bulk unavailable — {len(retry)} batched titles fall back to single calls")
                    singles = await asyncio.gather(
                        *(self._single_for(client, key, provider, batch, i) for i in retry),
                        return_exceptions=True,
                    )
                    answers.update(zip(retry, singles))
                elif isinstance(result, dict):
                    logger.info(f"MDBList batch of {len(ids)} {provider}/{mdb_type} titles answered in bulk")
        except Exception as exc:  # noqa: BLE001 - every waiter must be woken
            answers = {i: exc for i in ids}
        for media_id, futures in batch.waiters.items():
            answer = answers.get(media_id, FETCH_FAILED)
            for fut in futures:
                if fut.done():
                    continue
                if isinstance(answer, BaseException):
                    fut.set_exception(answer)
                else:
                    fut.set_result(answer)
