from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SharedTask:
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    assignee: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_by: str = ""


    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SharedTask:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _atomic_replace(src: str, dst: Path) -> None:
    """os.replace 在 Windows 上与并发的读取者存在共享冲突（WinError 5/32），
    短暂重试即可。其余平台一次成功。"""
    import time as _time

    last: OSError | None = None
    for _ in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # Windows sharing violation
            last = e
            _time.sleep(0.05)
    if last is not None:
        raise last


class SharedTaskStore:


    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._next_id = 1
        self._tasks: dict[str, SharedTask] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        next_id = data.get("next_id", 1)
        self._next_id = max(self._next_id if self._tasks else 1, next_id)
        for t in data.get("tasks", []):
            try:
                task = SharedTask.from_dict(t)
            except (TypeError, ValueError):
                continue
            # 合并而非覆盖：磁盘记录优先，但本地后写的字段不被丢
            self._tasks[task.id] = task

    def _save(self) -> None:
        """读-合并-写：写前重新 _load 吸收其它进程的并发写入，
        再用临时文件 + os.replace 原子替换，避免盲覆盖与半截文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[str, SharedTask] = {}
        next_id = self._next_id
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                next_id = max(next_id, data.get("next_id", 1))
                for t in data.get("tasks", []):
                    try:
                        task = SharedTask.from_dict(t)
                    except (TypeError, ValueError):
                        continue
                    merged[task.id] = task
            except (json.JSONDecodeError, OSError):
                pass
        # 本地视图覆盖磁盘同名记录（本地才是最新的修改者）
        merged.update(self._tasks)
        self._tasks = merged
        self._next_id = next_id

        payload = {
            "next_id": self._next_id,
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".tasks-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
            _atomic_replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def create(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        created_by: str = "",
    ) -> SharedTask:
        # 先同步磁盘状态：另一进程可能已创建任务，_next_id 需要跟上
        self._load()
        task_id = str(self._next_id)
        self._next_id += 1
        task = SharedTask(
            id=task_id,
            title=title,
            description=description,
            assignee=assignee,
            blocks=blocks or [],
            blocked_by=blocked_by or [],
            created_by=created_by,
        )
        self._tasks[task_id] = task
        self._save()
        return task

    def get(self, task_id: str) -> SharedTask | None:
        self._load()
        return self._tasks.get(task_id)


    def list_tasks(
        self,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[SharedTask]:
        self._load()
        result = list(self._tasks.values())
        if status:
            result = [t for t in result if t.status == status]
        if assignee:
            result = [t for t in result if t.assignee == assignee]
        return result


    def update(
        self,
        task_id: str,
        status: str | None = None,
        assignee: str | None = None,
        description: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> SharedTask | None:
        self._load()
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if status is not None:
            task.status = status
        if assignee is not None:
            task.assignee = assignee
        if description is not None:
            task.description = description
        if add_blocks:
            for bid in add_blocks:
                if bid not in task.blocks:
                    task.blocks.append(bid)
        if add_blocked_by:
            for bid in add_blocked_by:
                if bid not in task.blocked_by:
                    task.blocked_by.append(bid)
        self._save()
        return task

    def init_empty(self) -> None:
        self._tasks.clear()
        self._next_id = 1
        # 重置语义：绕过 _save 的读-合并，直接从空状态落盘
        payload = {"next_id": 1, "tasks": []}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".tasks-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
            _atomic_replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
