from __future__ import annotations

import os

import pytest

from mewcode.memory.instruction_includes import MAX_FILE_BYTES, MAX_TOTAL_BYTES, InstructionReader
from mewcode.memory.instructions import load_instructions, process_includes


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_layer_order_worktree_and_local(tmp_path, isolated_home):
    root = tmp_path / 'repo'
    leaf = root / '中文' / 'leaf'
    leaf.mkdir(parents=True)
    (root / '.git').write_text('gitdir: fixture')
    expected = []
    for index, path in enumerate([
        tmp_path / '.mewcode/MEWCODE.md', tmp_path / '.mewcode/AGENTS.md',
        root / 'MEWCODE.md', root / 'AGENTS.md', root / '.mewcode/MEWCODE.md',
        root / '中文/AGENTS.md', leaf / 'AGENTS.md', leaf / 'MEWCODE.local.md',
    ]):
        value = f'unique policy {index}'
        put(path, value)
        expected.append(value)
    text = load_instructions(str(leaf))
    assert [text.index(v) for v in expected] == sorted(text.index(v) for v in expected)
    assert text.count('instructions source:') == len(expected)
    assert '中文' in text


def test_no_git_does_not_scan_parents_and_global_include_has_own_scope(tmp_path, isolated_home):
    put(tmp_path / 'AGENTS.md', 'outside parent')
    put(tmp_path / '.mewcode/MEWCODE.md', '@include global.md\n@include ../AGENTS.md')
    put(tmp_path / '.mewcode/global.md', 'global included')
    leaf = tmp_path / 'leaf'
    leaf.mkdir()
    put(leaf / 'AGENTS.md', 'leaf policy')
    text = load_instructions(str(leaf))
    assert 'outside parent' not in text and 'global included' in text and 'leaf policy' in text
    assert 'blocked' in text


def test_cycles_fences_decode_and_missing_are_bounded(tmp_path):
    put(tmp_path / 'AGENTS.md', '@include child.md\n@include child.md')
    put(tmp_path / 'child.md', '@include AGENTS.md\n```md\n@include secret.md\n```\n~~~\n@include secret.md\n~~~\n@include bad.md\n@include missing.md')
    (tmp_path / 'bad.md').write_bytes(b'\xff')
    put(tmp_path / 'secret.md', 'must not expand')
    text = load_instructions(str(tmp_path))
    assert text.count('instructions source:') == 2 and 'cycle/duplicate' in text
    assert 'must not expand' not in text and 'unreadable' in text and 'file not found' in text
    assert 'absolute path' in process_includes('@include ' + str(tmp_path / 'secret.md'), tmp_path, tmp_path)


def test_total_file_count_depth_and_byte_limits(tmp_path):
    reader = InstructionReader()
    for i in range(6):
        path = tmp_path / f'{i}.md'
        put(path, str(i) * (MAX_FILE_BYTES - 100))
        text = reader.read(path, tmp_path)
    assert reader.remaining == 0 and 'budget exhausted' in text
    assert MAX_TOTAL_BYTES == 4 * MAX_FILE_BYTES
    put(tmp_path / 'large.md', '中' * MAX_FILE_BYTES)
    assert 'budget exhausted' in InstructionReader().read(tmp_path / 'large.md', tmp_path)
    for i in range(9):
        put(tmp_path / f'depth{i}.md', f'@include depth{i+1}.md')
    assert 'include depth' in InstructionReader().read(tmp_path / 'depth0.md', tmp_path)
    reader = InstructionReader()
    for i in range(130):
        put(tmp_path / f'empty{i}.md', '')
        text = reader.read(tmp_path / f'empty{i}.md', tmp_path)
    assert 'budget exhausted' in text


def test_symlink_escape_and_dedup(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    put(tmp_path / 'outside.md', 'outside must not load')
    put(root / 'AGENTS.md', 'only once')
    try:
        (root / 'MEWCODE.md').symlink_to(root / 'AGENTS.md')
        (root / 'child.md').symlink_to(tmp_path / 'outside.md')
    except OSError:
        pytest.skip('symlinks unavailable')
    text = load_instructions(str(root))
    assert text.count('only once') == 1
    assert 'outside must not load' not in process_includes('@include child.md', root, root)


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction')
def test_junction_scope_escape(tmp_path):
    import subprocess
    root = tmp_path / 'repo'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    put(outside / 'secret.md', 'outside must not load')
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'linked'), str(outside)], capture_output=True)
    assert result.returncode == 0
    assert 'blocked' in process_includes('@include linked/secret.md', root, root)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['headless', 'service', 'tui', 'tui-restored', 'subagent'])
async def test_real_entry_request_contains_policy_once(tmp_path, monkeypatch, entry):
    from unittest.mock import AsyncMock

    from mewcode.agent import Agent
    from mewcode.client import LLMClient
    from mewcode.config import AppConfig, ProviderConfig, ServiceConfig
    from mewcode.conversation import ConversationManager
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import StreamEnd, TextDelta

    monkeypatch.chdir(tmp_path)
    put(tmp_path / 'AGENTS.md', 'unique runtime policy')
    requests = []
    class Capture(LLMClient):
        async def stream(self, conversation, system='', tools=None):
            requests.append('\n'.join(m.content for m in conversation.history))
            yield TextDelta('done')
            yield StreamEnd('end_turn')
    client = Capture()
    provider = ProviderConfig('offline', 'anthropic', 'http://127.0.0.1:1', 'offline', 'fake')
    monkeypatch.setattr('mewcode.client.create_client', lambda _: client)
    monkeypatch.setattr('mewcode.client.resolve_context_window', AsyncMock())
    if entry == 'headless':
        from mewcode.__main__ import _run_prompt
        from mewcode.permissions import PermissionMode
        await _run_prompt(AppConfig([provider]), PermissionMode.DEFAULT, None, 'test')
    elif entry == 'service':
        from mewcode.service.execution import HeadlessAgentRunner
        agent = HeadlessAgentRunner(ServiceConfig(), provider)._build_agent(str(tmp_path))
        conversation = ConversationManager()
        await agent.run_to_completion('test', conversation)
        await agent.run_to_completion('again', conversation)
    elif entry.startswith('tui'):
        from mewcode.app import MewCodeApp
        monkeypatch.setattr('mewcode.app.create_client', lambda _: client)
        if entry == 'tui-restored':
            from types import SimpleNamespace
            put(tmp_path / 'restored/AGENTS.md', 'unique runtime policy\nrestored runtime scope')
            monkeypatch.setattr('mewcode.worktree.manager.WorktreeManager.restore_session',
                                lambda _: SimpleNamespace(worktree_path=str(tmp_path / 'restored')))
        app = MewCodeApp([provider])
        async with app.run_test():
            await app.agent.run_to_completion('test')
    else:
        from mewcode.agents.loader import AgentLoader
        from mewcode.agents.task_manager import TaskManager
        from mewcode.agents.trace import TraceManager
        from mewcode.tools.agent_tool import AgentTool, AgentToolParams
        parent = Agent(client, create_default_registry().bind_work_dir(str(tmp_path)), 'anthropic', work_dir=str(tmp_path))
        loader = AgentLoader(str(tmp_path))
        loader.load_all()
        tool = AgentTool(loader, TaskManager(), TraceManager(), parent)
        result = await tool.execute(AgentToolParams(prompt='test', description='policy test', subagent_type='general-purpose'))
        assert not result.is_error, result.output
    assert requests and all(text.count('unique runtime policy') == 1 for text in requests)
    if entry == 'tui-restored':
        assert all('restored runtime scope' in text for text in requests)


def test_directory_budget_and_unreadable_file(tmp_path, monkeypatch):
    import mewcode.memory.instructions as instructions
    root = tmp_path / 'repo'
    leaf = root / 'a/b/c'
    leaf.mkdir(parents=True)
    (root / '.git').mkdir()
    put(root / 'AGENTS.md', 'too distant')
    put(leaf / 'AGENTS.md', 'local policy')
    monkeypatch.setattr(instructions, 'MAX_DIRECTORY_DEPTH', 2)
    text = instructions.load_instructions(str(leaf))
    assert 'too distant' not in text and 'directory budget' in text and 'local policy' in text
    monkeypatch.setattr(type(leaf), 'open', lambda *a, **kw: (_ for _ in ()).throw(PermissionError()))
    assert 'unreadable' in InstructionReader().read(leaf / 'AGENTS.md', leaf)
