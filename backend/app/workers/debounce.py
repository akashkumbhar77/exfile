"""Debounce (SPEC-PATCH-002 A.2): per-sheet due time in a Redis sorted set.

touch(sheet) sets due = now + delay (re-arming on every further change), so a
burst of edits yields exactly one run, `delay` after the last change seen.
dispatch_due(now) pops due sheets and hands them to the run queue (RQ).

Single-dispatcher assumption: only the fleet watcher calls touch/dispatch_due,
so a re-arm can never race the pop.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

DUE_KEY = "debounce:due"


class RunQueue(Protocol):
    def enqueue_run(self, sheet_id: int) -> None: ...


class Debouncer:
    def __init__(self, redis: Any, delay: timedelta) -> None:
        self.redis = redis
        self.delay = delay

    def touch(self, sheet_id: int, now: datetime) -> None:
        self.redis.zadd(DUE_KEY, {str(sheet_id): (now + self.delay).timestamp()})

    def touch_now(self, sheet_id: int, now: datetime) -> None:
        self.redis.zadd(DUE_KEY, {str(sheet_id): now.timestamp()})

    def pending(self) -> dict[int, float]:
        return {int(m): float(s) for m, s in self.redis.zrange(DUE_KEY, 0, -1, withscores=True)}

    def dispatch_due(self, now: datetime, queue: RunQueue) -> list[int]:
        due = self.redis.zrangebyscore(DUE_KEY, "-inf", now.timestamp())
        sent: list[int] = []
        for member in due:
            if self.redis.zrem(DUE_KEY, member):
                sheet_id = int(member)
                queue.enqueue_run(sheet_id)
                sent.append(sheet_id)
        return sent


class RqRunQueue:
    """Enqueue `run:{sheet_id}` jobs on RQ."""

    def __init__(self, redis: Any, name: str = "runs") -> None:
        from rq import Queue

        self.queue = Queue(name, connection=redis)

    def enqueue_run(self, sheet_id: int) -> None:
        self.queue.enqueue(
            "app.workers.execute_run.run_sheet_job", sheet_id,
            description=f"run:{sheet_id}", job_timeout=600, result_ttl=3600, failure_ttl=86400,
        )
