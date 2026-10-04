from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Coroutine, Any

from mewcode.hooks.executors import execute_action
from mewcode.hooks.models import ActionResult, Hook, HookContext, ToolRejectedError

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        # 通知/prompt 队列按 owner（agent id）分桶：父代理与子代理共享
        # 同一 HookEngine，不隔离会互相污染 once 之外的输出消费。
        self._prompt_messages: dict[str, list[str]] = {}
        self._notifications: dict[str, list[HookNotification]] = {}
        # 事件循环只持弱引用——fire-and-forget 任务必须强引用保存，
        # 否则可能被 GC 中途回收。
        self._bg_tasks: set[asyncio.Task] = set()


    def _bucket(self, store: dict[str, list], owner: str) -> list:
        bucket = store.get(owner)
        if bucket is None:
            bucket = []
            store[owner] = bucket
        return bucket


    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task


    async def cancel_background(self) -> None:
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


    def for_owner(self, owner: str) -> OwnedHookEngine:
        return OwnedHookEngine(self, owner)


    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched


    async def run_hooks(
        self, event: str, ctx: HookContext, owner: str = ""
    ) -> None:
        matched = self.find_matching_hooks(event, ctx)
        for hook in matched:
            hook.mark_executed()
            if hook.async_exec:
                self.spawn(self._run_single(hook, ctx, owner))
            else:
                await self._run_single(hook, ctx, owner)


    async def _run_single(
        self, hook: Hook, ctx: HookContext, owner: str = ""
    ) -> None:
        try:
            result = await execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._bucket(self._prompt_messages, owner).append(result.output)
            self._bucket(self._notifications, owner).append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=result.output,
                    success=result.success,
                )
            )
            if not result.success:
                log.warning(
                    "Hook '%s' action failed: %s", hook.id, result.output
                )
        except Exception as e:
            log.warning("Hook '%s' execution error: %s", hook.id, e)
            self._bucket(self._notifications, owner).append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=str(e),
                    success=False,
                )
            )


    async def run_pre_tool_hooks(
        self, ctx: HookContext, owner: str = ""
    ) -> ToolRejectedError | None:
        matched = self.find_matching_hooks("pre_tool_use", ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await execute_action(hook.action, ctx)
                self._bucket(self._notifications, owner).append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=result.output,
                        success=result.success,
                    )
                )
                if hook.reject:
                    return ToolRejectedError(
                        tool=ctx.tool_name,
                        reason=result.output,
                        hook_id=hook.id,
                    )
            except Exception as e:
                log.warning("Hook '%s' execution error: %s", hook.id, e)
        return None

    def get_prompt_messages(self, owner: str = "") -> list[str]:
        bucket = self._prompt_messages.get(owner)
        if not bucket:
            return []
        messages = list(bucket)
        bucket.clear()
        return messages


    def drain_notifications(self, owner: str = "") -> list[HookNotification]:
        bucket = self._notifications.get(owner)
        if not bucket:
            return []
        notifications = list(bucket)
        bucket.clear()
        return notifications


class OwnedHookEngine:
    """共享 HookEngine 的 per-owner 视图。

    agent_tool 让父代理与所有子代理共享同一个 HookEngine（hook 定义与
    once 判定共享），但通知队列与 prompt 输出按 owner 隔离，互不串扰。
    """

    def __init__(self, engine: HookEngine, owner: str) -> None:
        self._engine = engine
        self.owner = owner
        self.hooks = engine.hooks


    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        return self._engine.find_matching_hooks(event, ctx)


    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        await self._engine.run_hooks(event, ctx, owner=self.owner)


    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        return await self._engine.run_pre_tool_hooks(ctx, owner=self.owner)


    def get_prompt_messages(self) -> list[str]:
        return self._engine.get_prompt_messages(self.owner)


    def drain_notifications(self) -> list[HookNotification]:
        return self._engine.drain_notifications(self.owner)


    async def cancel_background(self) -> None:
        await self._engine.cancel_background()
