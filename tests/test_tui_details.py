"""Details acceptance: real temporary Git repos and offline Textual controls."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from textual.widgets import Static, TabbedContent

from mewcode.agent import LoopComplete, PermissionRequest, PermissionResponse, ToolResultEvent, ToolUseEvent
from mewcode.app import ChatInput
from mewcode.tui.details import DetailsPanel, ExecutionHistory
from mewcode.tui.git import branch_name, read_git, safe_diff_path, workspace_changes
from tests.helpers.tui_offline import OfflineApp


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Offline Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "sample.txt").write_text("original\n", encoding="utf-8")
    (tmp_path / ".env").write_text("old secret\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "fixture")
    (tmp_path / "sample.txt").write_text("[red]dirty before task[/red]\n", encoding="utf-8")
    (tmp_path / ".env").write_text("private-content-never-preview\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("untracked-content-never-read", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored-content-never-read", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_git_explicit_diff_dirty_notice_untracked_and_secret_paths(repo):
    before = git(repo, "status", "--porcelain")
    summary = await workspace_changes(str(repo))
    assert "可能包含原有未提交改动" in summary
    assert "untracked.txt" in summary and "dirty before task" not in summary
    assert "ignored.txt" not in summary
    patch = await workspace_changes(str(repo), True)
    assert "[red]dirty before task[/red]" in patch
    assert "untracked-content-never-read" not in patch
    assert "private-content-never-preview" not in patch
    assert git(repo, "status", "--porcelain") == before
    assert await branch_name(str(repo)) == "main"


@pytest.mark.asyncio
async def test_git_disables_external_diff_and_textconv(repo, tmp_path):
    marker = repo / "external-ran"
    helper = repo / "external.py"
    helper.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    command = f'"{sys.executable}" "{helper}"'
    git(repo, "config", "diff.external", command)
    git(repo, "config", "diff.test.textconv", command)
    (repo / ".gitattributes").write_text("*.txt diff=test\n", encoding="utf-8")
    assert "dirty before task" in await workspace_changes(str(repo), True)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_git_disables_clean_filters_for_status_and_diff(repo):
    marker = repo / "clean-ran"
    helper = repo / "clean.py"
    helper.write_text(
        f"from pathlib import Path\nimport sys\nPath({str(marker)!r}).touch()\n"
        "sys.stdout.write(sys.stdin.read())\n", encoding="utf-8",
    )
    (repo / ".gitattributes").write_text("*.txt filter=preview-test\n", encoding="utf-8")
    git(repo, "config", "filter.preview-test.clean", f'"{sys.executable}" "{helper}"')
    git(repo, "config", "filter.preview-test.process", f'"{sys.executable}" "{helper}"')
    git(repo, "config", "filter.preview-test.required", "true")
    # Both a touched-but-unchanged file and a changed file require safe status reads.
    (repo / "sample.txt").write_text("original\n", encoding="utf-8")
    await workspace_changes(str(repo))
    assert not marker.exists()
    (repo / "sample.txt").write_text("[red]dirty before task[/red]\n", encoding="utf-8")
    assert "dirty before task" in await workspace_changes(str(repo), True)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_git_output_bound_and_large_diff(repo):
    (repo / "sample.txt").write_text("long line " * 200000, encoding="utf-8")
    result = await asyncio.wait_for(read_git(str(repo), ["diff", "--no-ext-diff", "--no-textconv"], 512), 5)
    assert result.truncated and len(result.data) == 512
    text = await workspace_changes(str(repo), True)
    assert "diff 已截断" in text and len(text) < 50000


@pytest.mark.asyncio
async def test_staged_and_unstaged_literal_paths_and_tracked_ignored_files(repo):
    file = repo / "[literal]中文.txt"
    file.write_text("old\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "literal paths")
    file.write_text("staged change\n", encoding="utf-8")
    git(repo, "add", "--", file.name)
    file.write_text("unstaged change\n", encoding="utf-8")
    (repo / ".gitignore").write_text("sample.txt\n", encoding="utf-8")
    (repo / "sample.txt").write_text("ignored-tracked-content\n", encoding="utf-8")
    patch = await workspace_changes(str(repo), True)
    assert "staged change" in patch and "unstaged change" in patch
    assert "ignored-tracked-content" not in patch


@pytest.mark.asyncio
async def test_git_timeout_kills_and_reaps(monkeypatch, repo):
    import mewcode.tui.git as module

    create = asyncio.create_subprocess_exec
    processes = []

    async def sleeping_process(*args, **kwargs):
        proc = await create(sys.executable, "-c", "import time; time.sleep(20)",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        processes.append(proc)
        return proc

    monkeypatch.setattr(module, "GIT_TIMEOUT", 0.05)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", sleeping_process)
    assert "超时" in await workspace_changes(str(repo))
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_non_git_and_unborn_empty_states(tmp_path):
    assert "非 Git" in await workspace_changes(str(tmp_path))
    assert "未知" in await branch_name(str(tmp_path))
    git(tmp_path, "init", "--initial-branch=main")
    assert "没有未提交改动" in await workspace_changes(str(tmp_path))
    assert await branch_name(str(tmp_path)) == "main"


@pytest.mark.parametrize("path", [".env", "src/.env.local", ".mewcode/config.yaml", ".ssh/id_ed25519", "credentials.json", "key.pem"])
def test_bulk_preview_omits_sensitive_paths(path):
    assert not safe_diff_path(path)


def test_execution_history_is_bounded_and_never_infers_verification():
    history = ExecutionHistory()
    history.begin("explicit request")
    for i in range(150):
        history.started(str(i), "WriteFile", {"file_path": "test.py", "content": "do not retain"})
        history.finished(str(i), "100 passed " * 2000, False, 1)
    assert len(history.records) == 100 and not history.current
    assert history.records[-1].state == "工具已结束"
    assert "content" not in history.records[-1].arguments
    assert len(history.records[-1].output) < 12100
    history.started("pending", "Bash", {})
    history.interrupted()
    assert history.records[-1].state == "已停止 / 结果未知"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36), (160, 48), (60, 18)])
async def test_f2_resize_preserves_draft_run_and_empty_facts(tmp_path, size):
    app = OfflineApp(str(tmp_path))
    async with app.run_test(size=size) as pilot:
        gate, started = asyncio.Event(), asyncio.Event()
        calls = []

        async def run(_):
            calls.append(1)
            yield ToolUseEvent("Bash", "t", {"command": "offline"})
            started.set()
            await gate.wait()
            yield ToolResultEvent("t", "Bash", "command ended", False, 0.3)
            yield LoopComplete(1)

        app.agent.run = run
        task = asyncio.create_task(app._send_message("explicit request"))
        await started.wait()
        inp = app.query_one(ChatInput)
        inp.load_text("draft\n中文")
        await pilot.press("f2")
        await pilot.pause()
        assert app.query_one(DetailsPanel).display
        assert app.query_one("#chat-area").display == (size[0] >= 120 and size[1] >= 24)
        if size[0] >= 120:
            chat = app.query_one("#chat-area")
            panel = app.query_one(DetailsPanel)
            assert chat.region.right <= panel.region.x
            assert panel.region in app.screen.region
        assert inp.region in app.screen.region
        app.query_one(TabbedContent).active = "execution-tab"
        await app._refresh_details()
        facts = str(app.query_one("#task-facts", Static).content)
        assert "无结构化任务" in facts
        assert "验证覆盖：未知" in str(app.query_one("#execution-facts", Static).content)
        assert "explicit request" in facts
        await pilot.resize_terminal(60, 18)
        await pilot.pause()
        assert inp.region in app.screen.region
        await pilot.press("f2")
        assert inp.text == "draft\n中文" and app.focused is inp
        assert app._streaming and len(calls) == 1
        gate.set()
        await task
        assert app._execution_history.records[-1].state == "工具已结束"


@pytest.mark.asyncio
async def test_permission_closes_narrow_details_and_f2_does_not_hide_it(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.press("f2")
        future = asyncio.get_running_loop().create_future()
        await app._handle_permission_request(PermissionRequest("Bash", "approval", future))
        await pilot.pause()
        assert not app.query_one(DetailsPanel).display
        assert app.query_one("#chat-area").display
        await pilot.press("f2")
        assert app.focused.id == "perm-inline"
        await pilot.press("escape")
        assert future.result() == PermissionResponse.DENY


@pytest.mark.asyncio
async def test_expanded_record_is_not_rebuilt_by_token_or_task_refresh(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test() as pilot:
        history = app._execution_history
        history.started("t", "Bash", {"command": "echo [literal]"})
        history.finished("t", "[red]literal[/red]", True, 0.1)
        await pilot.press("f2")
        panel = app.query_one(DetailsPanel)
        block = panel.query_one(".tool-block")
        block.action_expand_details()
        app.agent.total_input_tokens = 22
        await app._refresh_details()
        assert panel.query_one(".tool-block") is block and not block._collapsed
