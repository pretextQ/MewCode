"""F3.7 teams 修复：共享任务板并发、缓存键一致性、spawn 引号。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

from mewcode.teams.shared_task import SharedTaskStore


class TestSharedTaskConcurrency:
    def test_create_does_not_lose_concurrent_writes(self, tmp_path: Path):
        """旧实现 _save 全量盲覆盖：两个 store 各自创建任务后只有一方存活。

        模拟多进程：两个 SharedTaskStore 指向同一文件，交替创建。
        """
        path = tmp_path / "tasks.json"
        a = SharedTaskStore(path)
        b = SharedTaskStore(path)

        a.create(title="from A1")
        b.create(title="from B1")
        a.create(title="from A2")
        b.create(title="from B2")

        merged = SharedTaskStore(path)
        titles = sorted(t.title for t in merged.list_tasks())
        assert titles == ["from A1", "from A2", "from B1", "from B2"]
        ids = [t.id for t in merged.list_tasks()]
        assert len(set(ids)) == 4, f"id 撞号: {ids}"

    def test_create_syncs_next_id(self, tmp_path: Path):
        """create 先 _load 同步 _next_id，避免撞号。"""
        path = tmp_path / "tasks.json"
        a = SharedTaskStore(path)
        a.create(title="t1")  # id=1
        a.create(title="t2")  # id=2

        b = SharedTaskStore(path)  # 另一进程视角
        c = a.create(title="t3")
        d = b.create(title="t4")
        assert {c.id, d.id} == {"3", "4"}, (c.id, d.id)

    def test_thread_concurrent_create_no_loss(self, tmp_path: Path):
        """两线程并发 create，barrier 后同时写，记录不丢。"""
        path = tmp_path / "tasks.json"
        store = SharedTaskStore(path)
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker(prefix: str) -> None:
            try:
                barrier.wait()
                for i in range(5):
                    store.create(title=f"{prefix}-{i}")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=("A",)),
            threading.Thread(target=worker, args=("B",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        final = SharedTaskStore(path)
        titles = [t.title for t in final.list_tasks()]
        assert len(titles) == len(set(titles)), f"记录丢失/重复: {sorted(titles)}"

    def test_update_merges_remote_changes(self, tmp_path: Path):
        """update 的读-合并-写不覆盖别的写入者刚创建的任务。"""
        path = tmp_path / "tasks.json"
        a = SharedTaskStore(path)
        t1 = a.create(title="first")

        b = SharedTaskStore(path)  # 另一进程
        b.create(title="second")   # 先于 a.update 落盘

        updated = a.update(t1.id, status="in_progress")
        assert updated is not None and updated.status == "in_progress"

        final = SharedTaskStore(path)
        titles = sorted(t.title for t in final.list_tasks())
        assert titles == ["first", "second"]

    def test_save_is_atomic_no_partial_file(self, tmp_path: Path):
        """写盘走临时文件 + os.replace：读取方永远看不到半截 JSON。"""
        path = tmp_path / "tasks.json"
        store = SharedTaskStore(path)
        store.create(title="t1")

        seen_partial = []

        def _reader() -> None:
            for _ in range(50):
                if path.exists():
                    try:
                        raw = path.read_bytes()
                    except OSError:
                        # Windows 替换瞬间的共享冲突，不是数据问题
                        continue
                    if raw:
                        try:
                            json.loads(raw)
                        except json.JSONDecodeError:
                            seen_partial.append(raw[:40])

        reader = threading.Thread(target=_reader)
        reader.start()
        for i in range(30):
            store.create(title=f"t{i}")
        reader.join()
        assert not seen_partial, f"观察到半截写入: {seen_partial[:1]}"


class TestTeamCacheKeys:
    def test_sanitized_name_hits_same_cache(self, tmp_path: Path, monkeypatch):
        """create 用 sanitize 后 slug 作缓存键；get/delete 必须同样 sanitize。

        旧实现 delete_team("My Team") 找不到 "my-team" 缓存 → 残留。
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from mewcode.teams.manager import TeamManager

        tm = TeamManager()
        team = tm.create_team(
            name="My Team", lead_agent_id="lead1", teammate_mode="in-process"
        )
        slug = team.name

        # 用原始展示名（含空格大写）访问，必须命中同一缓存
        assert tm.get_team("My Team") is not None
        assert tm.get_team("My Team").name == slug

        # task store 同理
        assert tm.get_task_store("My Team") is not None

    def test_delete_with_display_name_removes_cache(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        from mewcode.teams.manager import TeamManager

        tm = TeamManager()
        team = tm.create_team(
            name="My Team", lead_agent_id="lead1", teammate_mode="in-process"
        )
        slug = team.name

        tm.delete_team("My Team")
        assert "My Team" not in tm._teams
        assert slug not in tm._teams
        assert slug not in tm._task_stores
        assert slug not in tm._mailboxes


class TestSpawnQuoting:
    def test_env_values_quoted(self):
        """teammate 名由 LLM 生成，可含空格：env 值必须引号包裹。"""
        from mewcode.teams.spawn_tmux import build_cli_command

        cmd = build_cli_command(
            team_name="my team",
            teammate_name="alice smith",
            worktree_path="/tmp/wt",
            prompt="do it",
            mailbox_dir="/tmp/mb",
        )
        assert "MEWCODE_TEAM_NAME='my team'" in cmd
        assert "MEWCODE_TEAMMATE_NAME='alice smith'" in cmd
        assert "MEWCODE_MAILBOX_DIR='/tmp/mb'" in cmd

    def test_quote_in_value_is_escaped(self):
        from mewcode.teams.spawn_tmux import build_cli_command

        cmd = build_cli_command(
            team_name="t",
            teammate_name="eve'; rm -rf /",
            worktree_path="/tmp/wt",
            prompt="x",
        )
        assert "rm -rf /" in cmd
        assert "eve'; rm -rf /" not in cmd.replace("'\\''", ""), (
            f"单引号未转义: {cmd}"
        )
