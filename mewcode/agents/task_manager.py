from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mewcode.agent import Agent

log = logging.getLogger(__name__)


@dataclass
class ProgressInfo:
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: str = ""


@dataclass
class BackgroundTask:
    id: str
    name: str
    agent: Agent
    task: str
    status: str = "running"
    result: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    cancel: Callable[[], bool] | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)
    stop_status: str = "cancelled"


class TaskManager:


    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._notify_queue: asyncio.Queue[BackgroundTask] = asyncio.Queue()
        self._async_tasks: dict[str, asyncio.Task[None]] = {}


    def launch(
        self,
        agent: Agent,
        task: str,
        name: str = "",
        fork_conversation: Any = None,
    ) -> str:
        task_id = secrets.token_hex(6)
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task,
        )
        self._tasks[task_id] = bg

        async_task = asyncio.create_task(
            self._run_background(task_id, fork_conversation)
        )
        self._async_tasks[task_id] = async_task

        bg.cancel = async_task.cancel
        async_task.add_done_callback(lambda task: self._finalize(bg, task))
        if agent.team_name and agent._team_manager:
            agent._team_manager.register_task_handle(agent.agent_id, self, task_id)
        return task_id


    async def _run_background(
        self, task_id: str, fork_conversation: Any = None
    ) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return

        try:
            async def initial_run() -> str:
                if fork_conversation is not None:
                    result = await bg.agent.run_to_completion("", fork_conversation)
                else:
                    result = await bg.agent.run_to_completion(bg.task)
                return (bg.result + "\n" + result).strip() if bg.result else result

            if bg.agent.team_name and bg.agent._team_manager:
                from mewcode.teams.worker_runtime import run_team_worker
                await run_team_worker(
                    bg.agent._team_manager, bg.agent.team_name, bg.agent.agent_id, bg.name,
                    initial_run, bg.agent.run_to_completion,
                    lambda result: self._completed_turn(bg, result),
                    lambda status: setattr(bg, "status", status),
                )
            else:
                self._completed_turn(bg, await initial_run())
        except asyncio.CancelledError:
            bg.status = bg.stop_status
            bg.result = "Worker was stopped" if bg.stop_status == "stopped" else "Task was cancelled"
            raise
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            bg.status = "failed"
            bg.result = f"Error: {e}"
        finally:
            self._finish(bg)

    def _completed_turn(self, bg: BackgroundTask, result: str) -> None:
        bg.result = result
        bg.status = "completed"
        self._notify_queue.put_nowait(replace(
            bg, end_time=time.monotonic(), progress=replace(bg.progress,
                input_tokens=bg.agent.total_input_tokens, output_tokens=bg.agent.total_output_tokens),
        ))

    def _finish(self, bg: BackgroundTask) -> None:
        if bg.end_time is not None:
            return
        bg.end_time = time.monotonic()
        bg.progress.input_tokens = bg.agent.total_input_tokens
        bg.progress.output_tokens = bg.agent.total_output_tokens
        if bg.agent.team_name and bg.agent._team_manager:
            team = bg.agent._team_manager.get_team(bg.agent.team_name)
            if team:
                team.set_member_active(bg.agent.agent_id, False)
                team.save()
        if bg.status != "completed":
            self._notify_queue.put_nowait(replace(bg, progress=replace(bg.progress)))

    def _finalize(self, bg: BackgroundTask, task: asyncio.Task[None]) -> None:
        # Cancellation before the coroutine's first instruction bypasses finally.
        if bg.end_time is None:
            bg.status = bg.stop_status if task.cancelled() else "failed"
            bg.result = f"Task was {bg.stop_status}" if task.cancelled() else "Worker exited unexpectedly"
            self._finish(bg)
        self._async_tasks.pop(bg.id, None)



    def adopt_running(
        self,
        agent: Agent,
        task_description: str,
        partial_result: str = "",
        name: str = "",
    ) -> str:
        task_id = secrets.token_hex(6)
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task_description,
            result=partial_result,
        )
        self._tasks[task_id] = bg

        async_task = asyncio.create_task(self._continue_background(task_id))
        self._async_tasks[task_id] = async_task
        bg.cancel = async_task.cancel
        async_task.add_done_callback(lambda task: self._finalize(bg, task))
        if agent.team_name and agent._team_manager:
            agent._team_manager.register_task_handle(agent.agent_id, self, task_id)
        return task_id


    async def _continue_background(self, task_id: str) -> None:
        await self._run_background(task_id)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        bg = self._tasks.get(task_id)
        if bg is None or bg.status not in ("running", "listening"):
            return False
        async_task = self._async_tasks.get(task_id)
        if async_task and not async_task.done():
            if not async_task.cancelling():
                async_task.cancel()
            return True
        return False

    async def stop(self, task_id: str, timeout: float = 5.0, *, terminal_status: str = "cancelled") -> bool:
        handle = self._async_tasks.get(task_id)
        if handle is None or handle.done():
            return True
        bg = self._tasks.get(task_id)
        if bg:
            bg.stop_status = terminal_status
        if not handle.cancelling():
            handle.cancel()
        done, _ = await asyncio.wait([handle], timeout=timeout)
        return bool(done)

    async def shutdown_all(self) -> None:
        handles = list(self._async_tasks.values())
        for handle in handles:
            if not handle.cancelling():
                handle.cancel()
        if handles:
            done, pending = await asyncio.wait(handles, timeout=5)
            if pending:
                raise RuntimeError("Background workers did not stop within 5 seconds")

    def is_live(self, task_id: str) -> bool:
        handle = self._async_tasks.get(task_id)
        return handle is not None and not handle.done() and not handle.cancelling()

    def has_pending_work(self) -> bool:
        """Outstanding turns/notifications keep headless alive; idle listeners do not."""
        if not self._notify_queue.empty():
            return True
        for bg in self._tasks.values():
            if not self.is_live(bg.id):
                continue
            if bg.status == "running":
                return True
            if bg.status == "listening" and bg.agent._team_manager:
                mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
                if mailbox and any(m.message_type in ("text", "shutdown_request")
                                   for m in mailbox.read(bg.agent.agent_id)):
                    return True
        return False

    def poll_completed(self) -> list[BackgroundTask]:
        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                snapshot = self._notify_queue.get_nowait()
                completed.append(snapshot)
                if not self.is_live(snapshot.id):
                    self._tasks.pop(snapshot.id, None)
            except asyncio.QueueEmpty:
                break
        return completed
