"""Single-process notification dispatcher; pending work survives cancellation.

Delivery is at least once: a crash after remote receipt but before the local
receipt may produce a duplicate. A stable event ID is included in every message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

from .jobs import Job, JobStore
from .notify import DeliveryResult, RepoPolicyNotifier

log = logging.getLogger(__name__)


class NotificationOutbox:
    def __init__(self, store: JobStore, notifier: Any, poll_interval: float = 0.2) -> None:
        self.store = store
        self.notifier = notifier
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    def notifier_for(self, repo: str) -> Any:
        if isinstance(self.notifier, RepoPolicyNotifier):
            return self.notifier.notifier_for(repo)
        return self.notifier

    def route_for(self, repo: str) -> str:
        value = getattr(self.notifier_for(repo), "route", "")
        return value if isinstance(value, str) else ""

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="mewcode-notification-outbox")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.process_once()
            except Exception:
                # A DB failure must not permanently kill the dispatcher or leak secrets.
                log.error("notification dispatcher iteration failed; retrying")
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except TimeoutError:
                pass

    async def process_once(self) -> int:
        async with self._lock:
            rows = await self.store.journal.ready_notifications()
            for row in rows:
                await self._deliver(row)
            return len(rows)

    async def flush(self) -> None:
        """Drain currently due rows only; does not wait through backoff."""
        while await self.process_once():
            pass

    async def _deliver(self, row: dict) -> None:
        job = Job(**json.loads(row["snapshot"]))
        try:
            notifier = self.notifier_for(job.repo)
            value = getattr(notifier, "route", "")
            route = value if isinstance(value, str) else ""
            pinned = await self.store.journal.pin_route(row, route)
            if pinned is None:
                return  # Superseded or explicitly re-queued after the batch was read.
            if pinned != route:
                result = DeliveryResult("blocked", "notification route changed; explicit retry required")
            elif not hasattr(notifier, "deliver_job_event"):
                result = DeliveryResult("blocked", "notifier has no confirmed-delivery interface")
            else:
                detail = f"{row['detail']}\n通知事件：{row['event_seq']}"
                result = await notifier.deliver_job_event(job, row["phase"], detail)
                if (not isinstance(result, DeliveryResult)
                        or result.status not in {"sent", "retry", "blocked", "skipped"}):
                    result = DeliveryResult("blocked", "notifier returned no delivery result")
        except Exception as exc:
            # Never persist arbitrary exception text: it may include a webhook URL.
            result = DeliveryResult("blocked", f"notification configuration/send failure ({type(exc).__name__})")
        delay = 0.0
        if result.status in {"retry", "blocked"}:
            backoff = min(300, 2 ** min(row["attempts"] + 1, 9))
            delay = max(result.retry_after, backoff * random.uniform(0.8, 1.2))
            if result.status == "blocked":
                delay = max(delay, 60)
        await self.store.journal.settle_notification(row, result.status, error=result.error, delay=delay)
