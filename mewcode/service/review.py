"""Optional, bounded, tool-free review of a committed fix; never triggers repair."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from mewcode.client import LLMClient, create_client
from mewcode.config import ProviderConfig, ServiceConfig
from mewcode.conversation import ConversationManager
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolCallDelta, ToolCallStart

from .jobs import Job, JobStore
from .policy import RepoPolicyLoader
from .review_diff import digest, included_lines, select_patches, snapshot
from .review_models import ReviewResponse, ReviewResult
from .review_prompt import SYSTEM, render_input

if TYPE_CHECKING:
    from .execution import ExecutionContext


def review_history(events: list) -> tuple[int, int]:
    """Charge unknown/interrupted calls their reservation, not zero."""
    started = {}
    finished = {}
    for event in events:
        if event.kind not in ("review_started", "review_finished"):
            continue
        data = json.loads(event.detail)
        if event.kind == "review_started":
            started[data["review_id"]] = data["reserved_tokens"]
        else:
            finished[data["review_id"]] = data["budget_charge"]
    return len(started), sum(finished.get(key, reserved) for key, reserved in started.items())


class ReviewService:
    def __init__(
        self, config: ServiceConfig, store: JobStore, provider: ProviderConfig, *,
        client_factory: Callable[[ProviderConfig], LLMClient] = create_client,
        policy_loader: RepoPolicyLoader | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.provider = provider
        self.client_factory = client_factory
        self.policy_loader = policy_loader or RepoPolicyLoader(config.repos)
        self.root = Path(config.data_dir).resolve() / "reviews"
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def run(self, job: Job, context: ExecutionContext, head_sha: str) -> ReviewResult:
        lock = self._locks.setdefault(job.id, asyncio.Lock())
        async with lock:
            return await self._review(job, context, head_sha)

    async def _review(self, job: Job, context: ExecutionContext, head_sha: str) -> ReviewResult:
        result = ReviewResult(
            review_id=uuid.uuid4().hex, head_sha=head_sha,
            provider=self.provider.name, model=self.provider.model,
        )
        if not self.config.review.enabled:
            result.reason = "disabled"
            return result
        start = time.monotonic()
        directory: Path | None = None
        try:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", job.id) or not re.fullmatch(r"[a-f0-9]{40,64}", head_sha):
                raise ValueError("invalid review identity")
            directory = self.root / job.id / result.review_id
            directory.mkdir(parents=True)
            # One wall-clock deadline covers evidence, model streaming and close.
            async with asyncio.timeout(self.config.review.timeout_seconds):
                await self._run(job, context, result, directory)
        except asyncio.CancelledError:
            result.reason = "cancelled"
            await self._finish(job.id, result, directory, start)
            raise
        except TimeoutError:
            result.status = "incomplete"
            result.reason = "timeout"
        except Exception as exc:
            # SDK exceptions can contain URLs/keys. Do not persist raw exceptions.
            result.status, result.reason = "incomplete", f"review_error:{type(exc).__name__}"
        await self._finish(job.id, result, directory, start)
        return result

    async def _run(self, job: Job, context: ExecutionContext, result: ReviewResult, directory: Path) -> None:
        settings = self.config.review
        result.base_sha, result.coverage = await asyncio.to_thread(
            snapshot, context.work_dir,
            context.target_branch or context.repo.base_branch or self.config.vcs.base_branch,
            result.head_sha, directory,
        )
        result.artifacts["diff"] = str((directory / "full.diff").relative_to(self.root.parent))
        result.hashes["diff"] = await asyncio.to_thread(digest, directory / "full.diff")
        if (directory / "full.diff").stat().st_size > settings.max_evidence_bytes:
            result.reason = "evidence_size_limit"
            for file in result.coverage:
                file.reason = result.reason
            return
        sources: dict = {
            "alert": job.payload_text[:1000], "fixer_report": context.agent.final_text[:1000],
            "tests": {
                phase: None if test is None else {
                    "command": test.command[:300], "exit_code": test.exit_code,
                    "timed_out": test.timed_out, "output_tail": test.output[-1000:],
                }
                for phase, test in (("baseline", context.baseline_test), ("verify", context.verify_test))
            },
            "patches": [],
        }
        metadata = {"base_sha": result.base_sha, "head_sha": result.head_sha}
        remaining = settings.max_input_bytes - len(render_input(metadata, sources).encode("utf-8"))
        sources["patches"] = await asyncio.to_thread(
            select_patches, context.work_dir, result.base_sha, result.head_sha,
            result.coverage, directory, max(0, remaining),
        )
        # JSON quoting and filenames also count against the exact input byte cap.
        request = render_input(metadata, sources)
        while sources["patches"] and len(request.encode("utf-8")) > settings.max_input_bytes:
            patch = sources["patches"][-1]
            file = next(file for file in result.coverage if file.path == patch["path"])
            over = len(request.encode("utf-8")) - settings.max_input_bytes
            raw = patch["patch"].encode("utf-8")[:max(0, file.sent_bytes - over - 128)]
            patch["patch"] = (raw.rsplit(b"\n", 1)[0] + b"\n").decode("utf-8", errors="ignore") if b"\n" in raw else ""
            file.sent_bytes = len(patch["patch"].encode("utf-8"))
            file.status, file.reason = "partial", "input_budget"
            if not included_lines([patch]):
                sources["patches"].pop()
                file.status, file.sent_bytes = "none", 0
            request = render_input(metadata, sources)
        if not sources["patches"] or len(request.encode("utf-8")) > settings.max_input_bytes:
            result.reason = "no_reviewable_input"
            return
        (directory / "request.json").write_text(request, encoding="utf-8", newline="\n")
        result.artifacts["input"] = str((directory / "request.json").relative_to(self.root.parent))
        result.hashes["input"] = digest(directory / "request.json")
        (directory / "system.txt").write_text(SYSTEM, encoding="utf-8", newline="\n")
        result.artifacts["system"] = str((directory / "system.txt").relative_to(self.root.parent))
        result.hashes["system"] = digest(directory / "system.txt")
        calls, spent = review_history(await self.store.events(job.id))
        if calls >= settings.max_calls_per_job:
            result.reason = "call_limit"
            return
        # A conservative byte-based reservation, not an exact tokenizer. Actual
        # provider usage is checked afterwards; unknown usage keeps the reserve.
        reserved = len((SYSTEM + request).encode("utf-8")) + settings.max_output_tokens + 1024
        if reserved > self.provider.get_context_window():
            result.reason = "context_window"
            return
        current = await self.store.get_or_raise(job.id)
        job_budget = self.policy_loader.load(job.repo).token_budget or self.config.token_budget
        if reserved > settings.token_budget - spent or (
            job_budget and reserved > job_budget - current.tokens_in - current.tokens_out
        ):
            result.reason = "token_budget"
            return
        result.budget_charge = reserved
        await self.store.add_event(job.id, "review_started", json.dumps({
            "review_id": result.review_id, "head_sha": result.head_sha, "reserved_tokens": reserved,
        }))
        # A selected patch is not counted as delivered until the request starts.
        await self._request(request, sources["patches"], result, directory)

    async def _request(
        self, request: str, patches: list[dict[str, str]], result: ReviewResult, directory: Path,
    ) -> None:
        settings = self.config.review
        client = self.client_factory(replace(
            self.provider, thinking=False, max_output_tokens=settings.max_output_tokens,
        ))
        client.set_max_output_tokens(settings.max_output_tokens)
        client.set_max_retries(0)
        conversation = ConversationManager()
        conversation.add_user_message(request)
        text = ""
        end: StreamEnd | None = None
        try:
            result.request_attempted = True
            async for event in client.stream(conversation, system=SYSTEM, tools=[]):
                if isinstance(event, TextDelta):
                    text += event.text
                    if len(text.encode("utf-8")) > 65_536:
                        result.reason = "response_size_limit"
                        return
                elif isinstance(event, StreamEnd):
                    end = event
                    result.input_tokens += event.input_tokens
                    result.output_tokens += event.output_tokens
                    result.cache_read += event.cache_read
                    result.cache_creation += event.cache_creation
                    result.usage_known = bool(
                        result.input_tokens + result.output_tokens + result.cache_read + result.cache_creation
                    )
                elif isinstance(event, (ToolCallStart, ToolCallDelta, ToolCallComplete)):
                    result.reason = "unexpected_tool_call"
                    return
            if end is None or end.stop_reason != "end_turn":
                result.reason = "incomplete_response"
                return
            response = ReviewResponse.model_validate_json(text)
            locations = included_lines(patches)
            if any((item.path, item.side, item.line) not in locations for item in response.findings):
                result.reason = "invalid_finding_location"
                return
            result.findings = [item.model_dump() for item in response.findings]
            result.summary = response.summary
            result.coverage_complete = bool(result.coverage) and all(file.status == "full" for file in result.coverage)
            result.status = (
                "findings" if result.findings else "no_findings" if result.coverage_complete else "incomplete"
            )
            result.reason = "completed" if result.coverage_complete else "partial_coverage"
        finally:
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            finally:
                (directory / "response.txt").write_text(text[:65_536], encoding="utf-8", newline="\n")
                result.artifacts["response"] = str((directory / "response.txt").relative_to(self.root.parent))
                result.hashes["response"] = digest(directory / "response.txt")
                if result.usage_known:
                    used = result.input_tokens + result.output_tokens + result.cache_read + result.cache_creation
                    if used > result.budget_charge:
                        result.status, result.reason = "incomplete", "provider_usage_exceeded_reservation"
                    result.budget_charge = used

    async def _finish(self, job_id: str, result: ReviewResult, directory: Path | None, start: float) -> None:
        result.elapsed_seconds = round(time.monotonic() - start, 3)
        if not result.request_attempted:
            for file in result.coverage:
                if file.status != "none":
                    file.status, file.reason = "none", result.reason
                file.sent_bytes = 0
        if result.reason != "completed" and result.status != "findings":
            result.coverage_complete = False
        # Model usage contributes to existing job totals even on malformed output.
        await self.store.add_usage(
            job_id, result.input_tokens + result.cache_read + result.cache_creation, result.output_tokens,
        )
        data = json.dumps(result.to_dict(), ensure_ascii=False)
        if directory is not None:
            try:
                temporary = directory / "result.tmp"
                temporary.write_text(data, encoding="utf-8")
                temporary.replace(directory / "result.json")
            except OSError:
                result.status, result.reason = "incomplete", "artifact_write_failed"
                result.coverage_complete = False
                data = json.dumps(result.to_dict(), ensure_ascii=False)
        await self.store.add_event(job_id, "review_finished", data)
