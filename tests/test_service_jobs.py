"""M1 W1: JobStore 状态机、幂等去重与审计事件的测试。

覆盖 docs/evolution/02-architecture.md 第四节的关键规则：
- 每个非法转移被拒（状态机是硬约束，不是文档约定）；
- fix/verify 重试上限触发 escalate 而非死循环；
- 指纹去重（同仓库 + 指纹 + 未完结 + 窗口内）；
- 每个状态变化落库 = 审计日志。

store 在测试体内显式开启/关闭（仓库既有风格：不依赖 async fixture，
且 close 必须显式收尾——sqlite 连接与后台线程不能留给 GC）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mewcode.service.jobs import (
    DEFAULT_MAX_FIX_ATTEMPTS,
    TRANSITIONS,
    InvalidTransition,
    JobNotFound,
    JobStore,
    JobStoreError,
)

HAPPY_PATH = [
    "triaging",
    "reproducing",
    "fixing",
    "verifying",
    "pr_opened",
    "ci_gate",
    "human_review",
    "merged",
]


@asynccontextmanager
async def open_store(db_path: Path, **kwargs):
    store = JobStore(db_path, **kwargs)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def make_job(store: JobStore, **kwargs):
    defaults = dict(fingerprint="fp-1", repo="demo", severity="critical", title="latency high")
    defaults.update(kwargs)
    return await store.create_job(**defaults)


# =========================================================================
# A. 状态机
# =========================================================================

class TestStateMachine:
    @pytest.mark.asyncio
    async def test_happy_path_reaches_merged(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            assert job.status == "received"
            for state in HAPPY_PATH:
                job = await store.transition(job.id, state)
                assert job.status == state
            assert job.is_terminal

    @pytest.mark.asyncio
    async def test_illegal_forward_jump_rejected(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            with pytest.raises(InvalidTransition):
                await store.transition(job.id, "fixing")  # received -> fixing 跳过 triage
            # 拒绝后状态不变
            assert (await store.get(job.id)).status == "received"

    @pytest.mark.asyncio
    async def test_terminal_state_is_final(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            await store.transition(job.id, "invalid", reason="empty payload")
            for target in ("triaging", "fixing", "escalate"):
                with pytest.raises(InvalidTransition):
                    await store.transition(job.id, target)

    @pytest.mark.asyncio
    async def test_unknown_state_rejected(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            with pytest.raises(JobStoreError):
                await store.transition(job.id, "no_such_state")

    @pytest.mark.asyncio
    async def test_missing_job_raises(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            with pytest.raises(JobNotFound):
                await store.transition("job-missing", "triaging")

    def test_every_non_terminal_state_can_escalate(self):
        """escalate 必须从任何非终态可达——否则 job 会卡在中间态无人接手。"""
        terminal = {"merged", "invalid", "cant_repro", "escalate"}
        for state, allowed in TRANSITIONS.items():
            if state in terminal:
                continue
            assert "escalate" in allowed, f"{state} cannot escalate"

    @pytest.mark.asyncio
    async def test_failure_branches_exist(self, tmp_path: Path):
        """架构文档里每个失败分支都必须是状态机里的真实边。"""
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            await store.transition(job.id, "triaging")
            assert (await store.transition(job.id, "invalid")).status == "invalid"

            job2 = await make_job(store, fingerprint="fp-2")
            await store.transition(job2.id, "triaging")
            await store.transition(job2.id, "reproducing")
            assert (await store.transition(job2.id, "cant_repro")).status == "cant_repro"

            job3 = await make_job(store, fingerprint="fp-3")
            await store.transition(job3.id, "triaging")
            await store.transition(job3.id, "reproducing")
            await store.transition(job3.id, "fixing")
            assert (await store.transition(job3.id, "fix_failed")).status == "fix_failed"


# =========================================================================
# B. 重试上限（成本控制的地基）
# =========================================================================

class TestRetryCeiling:
    async def _to_fix_failed(self, store: JobStore, fingerprint: str = "fp") -> str:
        job = await make_job(store, fingerprint=fingerprint)
        await store.transition(job.id, "triaging")
        await store.transition(job.id, "reproducing")
        await store.transition(job.id, "fixing")
        await store.transition(job.id, "fix_failed")
        return job.id

    @pytest.mark.asyncio
    async def test_retry_increments_attempts(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job_id = await self._to_fix_failed(store)
            job = await store.get(job_id)
            assert job.attempts == 1  # 首次进入 fixing

            job = await store.transition(job_id, "fixing")  # 重试 1
            assert job.attempts == 2

    @pytest.mark.asyncio
    async def test_retry_ceiling_forces_escalate(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job_id = await self._to_fix_failed(store)
            # 首次 + (max-1) 次重试合法
            for _ in range(DEFAULT_MAX_FIX_ATTEMPTS - 1):
                await store.transition(job_id, "fixing")
                await store.transition(job_id, "fix_failed")

            with pytest.raises(InvalidTransition) as ei:
                await store.transition(job_id, "fixing")
            assert "retry ceiling" in str(ei.value)

            # 超限后唯一出路是 escalate，且能成功
            job = await store.transition(job_id, "escalate", reason="retry ceiling reached")
            assert job.status == "escalate"
            assert job.is_terminal

    @pytest.mark.asyncio
    async def test_ceiling_is_configurable(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db", max_fix_attempts=1) as store:
            job = await make_job(store)
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "reproducing")
            await store.transition(job.id, "fixing")
            await store.transition(job.id, "verifying")
            await store.transition(job.id, "verify_failed")
            with pytest.raises(InvalidTransition):
                await store.transition(job.id, "fixing")

    @pytest.mark.asyncio
    async def test_changes_requested_rework_not_capped_by_retries(self, tmp_path: Path):
        """人工评审驱动的返工不占自动重试预算（架构文档：重试上限针对自动循环）。"""
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            for state in ("triaging", "reproducing", "fixing", "verifying", "pr_opened", "ci_gate"):
                await store.transition(job.id, state)
            await store.transition(job.id, "human_review")
            await store.transition(job.id, "changes_requested")
            back = await store.transition(job.id, "fixing")
            assert back.status == "fixing"


# =========================================================================
# C. 审计日志（每个状态变化落库）
# =========================================================================

class TestAuditLog:
    @pytest.mark.asyncio
    async def test_transitions_recorded_in_order(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            await store.transition(job.id, "triaging", reason="analysis started")
            await store.transition(job.id, "escalate", reason="insufficient logs")

            events = await store.events(job.id)
            kinds = [e.kind for e in events]
            assert kinds == ["created", "transition", "transition"]
            assert "received -> triaging" in events[1].detail
            assert "insufficient logs" in events[-1].detail

    @pytest.mark.asyncio
    async def test_add_event_does_not_change_state(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            await store.add_event(job.id, "tool_use", "Bash: pytest -q")
            assert (await store.get(job.id)).status == "received"
            events = await store.events(job.id)
            assert any(e.kind == "tool_use" for e in events)

    @pytest.mark.asyncio
    async def test_rejected_transition_leaves_no_trace(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            before = len(await store.events(job.id))
            with pytest.raises(InvalidTransition):
                await store.transition(job.id, "merged")
            assert len(await store.events(job.id)) == before


# =========================================================================
# D. 幂等去重（同告警重复触发）
# =========================================================================

class TestDedup:
    @pytest.mark.asyncio
    async def test_open_job_matched_by_fingerprint(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store, fingerprint="fp-alert-1")
            found = await store.find_open_by_fingerprint("demo", "fp-alert-1", 1800)
            assert found is not None and found.id == job.id

    @pytest.mark.asyncio
    async def test_terminal_job_does_not_block_new_intake(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store, fingerprint="fp-alert-1")
            await store.transition(job.id, "invalid", reason="empty")
            assert await store.find_open_by_fingerprint("demo", "fp-alert-1", 1800) is None

    @pytest.mark.asyncio
    async def test_other_repo_not_matched(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            await make_job(store, fingerprint="fp-alert-1", repo="demo")
            assert await store.find_open_by_fingerprint("other-repo", "fp-alert-1", 1800) is None

    @pytest.mark.asyncio
    async def test_empty_fingerprint_never_matches(self, tmp_path: Path):
        """没有指纹的告警不能互相吞并——否则所有空指纹告警并成一个 job。"""
        async with open_store(tmp_path / "jobs.db") as store:
            await make_job(store, fingerprint="")
            assert await store.find_open_by_fingerprint("demo", "", 1800) is None

    @pytest.mark.asyncio
    async def test_stale_job_outside_window_not_matched(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store, fingerprint="fp-old")
            old = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            await store.update(job.id, last_error="")  # 走公开 API 验证 update 不破坏窗口查询
            conn = store._require_conn()
            conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (old, job.id))
            conn.commit()
            assert await store.find_open_by_fingerprint("demo", "fp-old", 1800) is None


# =========================================================================
# E. 持久化与恢复
# =========================================================================

class TestPersistence:
    @pytest.mark.asyncio
    async def test_state_survives_reopen(self, tmp_path: Path):
        path = tmp_path / "jobs.db"
        async with open_store(path) as store:
            job = await store.create_job(fingerprint="fp", repo="demo", payload={"alert": "x"})
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "escalate", reason="no logs")
            job_id = job.id

        async with open_store(path) as store:
            reloaded = await store.get_or_raise(job_id)
            assert reloaded.status == "escalate"
            assert reloaded.payload == {"alert": "x"}
            assert [e.kind for e in await store.events(job_id)][-1] == "transition"

    @pytest.mark.asyncio
    async def test_list_unfinished_excludes_terminal(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            open_job = await make_job(store, fingerprint="fp-open")
            done_job = await make_job(store, fingerprint="fp-done")
            await store.transition(done_job.id, "invalid")

            unfinished = await store.list_unfinished()
            ids = [j.id for j in unfinished]
            assert open_job.id in ids
            assert done_job.id not in ids

    @pytest.mark.asyncio
    async def test_reset_for_recovery_returns_to_received(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "reproducing")
            await store.transition(job.id, "fixing")

            recovered = await store.reset_for_recovery(job.id, reason="service restart")
            assert recovered.status == "received"
            events = await store.events(job.id)
            assert events[-1].kind == "recovery_reset"
            assert "fixing -> received" in events[-1].detail

    @pytest.mark.asyncio
    async def test_reset_for_recovery_rejects_terminal(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            await store.transition(job.id, "escalate")
            with pytest.raises(InvalidTransition):
                await store.reset_for_recovery(job.id)


# =========================================================================
# F. 字段更新与列表
# =========================================================================

class TestFieldUpdates:
    @pytest.mark.asyncio
    async def test_transition_can_carry_result_fields(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            for state in ("triaging", "reproducing", "fixing", "verifying"):
                await store.transition(job.id, state)
            job = await store.transition(job.id, "pr_opened", pr_url="https://x/pr/1", branch="mewfix/1")
            assert job.pr_url == "https://x/pr/1"
            assert job.branch == "mewfix/1"

    @pytest.mark.asyncio
    async def test_update_rejects_status(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            with pytest.raises(JobStoreError):
                await store.update(job.id, status="merged")

    @pytest.mark.asyncio
    async def test_update_rejects_unknown_field(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            with pytest.raises(JobStoreError):
                await store.update(job.id, nope="x")

    @pytest.mark.asyncio
    async def test_list_jobs_filters_by_status(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            a = await make_job(store, fingerprint="fp-a")
            b = await make_job(store, fingerprint="fp-b")
            await store.transition(b.id, "invalid")

            received = await store.list_jobs(status="received")
            assert [j.id for j in received] == [a.id]
            assert len(await store.list_jobs()) == 2


# =========================================================================
# G. payload 保真
# =========================================================================

class TestPayloadSafety:
    @pytest.mark.asyncio
    async def test_payload_roundtrips_unicode(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            payload = {"summary": "接口超时 (504)", "labels": {"service": "订单"}}
            job = await make_job(store, payload=payload)
            assert (await store.get_or_raise(job.id)).payload == payload
            assert "订单" in job.payload_text
