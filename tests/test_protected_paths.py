from __future__ import annotations

import pytest

from mewcode.agent import PermissionRequest, PermissionResponse, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.permissions import PermissionMode
from mewcode.permissions.rules import Rule
from mewcode.tools.base import StreamEnd, ToolCallComplete
from tests.test_permission_security import MockLLMClient, _make_agent


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', list(PermissionMode))
@pytest.mark.parametrize('name', ['WriteFile', 'EditFile'])
async def test_protected_actual_agent(tmp_path, isolated_home, mode, name):
    target = tmp_path / '.mewcode/config.yaml'
    target.parent.mkdir()
    target.write_text('original', encoding='utf-8')
    args = {'file_path': '.mewcode/config.yaml'}
    args.update({'content': 'changed'} if name == 'WriteFile' else
                {'old_string': 'original', 'new_string': 'changed'})
    client = MockLLMClient([[ToolCallComplete('read', 'ReadFile', {'file_path': args['file_path']}), StreamEnd('tool_use')],
                            [ToolCallComplete('protected', name, args), StreamEnd('tool_use')]])
    agent = _make_agent(tmp_path, client)
    agent.registry = agent.registry.bind_work_dir(str(tmp_path))
    agent.permission_checker.mode = mode
    agent.permission_checker.rule_engine.append_local_rule(Rule('ReadFile', '*', 'allow'))
    agent.permission_checker.plan_file_path = str(target)  # Even a forged plan exception cannot bypass protection.
    agent.permission_checker.rule_engine.append_local_rule(Rule(name, '*', 'allow'))
    conv = ConversationManager()
    conv.add_user_message('edit')
    events = [e async for e in agent.run(conv)]
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results[-1].is_error and 'protected' in results[-1].output.lower()
    assert target.read_text(encoding='utf-8') == 'original'
    assert not any(isinstance(e, PermissionRequest) for e in events)


@pytest.mark.parametrize('relative', [
    '.mewcode/config.yaml', '.mewcode/permissions.yaml', '.mewcode/permissions.local.yaml',
    '.mewcode/skills/new/SKILL.md', '.mewcode/skills', 'sub/../.mewcode/config.yaml',
])
def test_protected_missing_and_global(tmp_path, isolated_home, relative):
    from mewcode.permissions.sandbox import PathSandbox
    root = tmp_path / 'worktree'
    root.mkdir()
    sandbox = PathSandbox(str(root))
    assert not sandbox.check_write(relative)[0]
    assert not sandbox.check_write(str(tmp_path / relative))[0]
    for path in ['source.py', '.mewcode/config.yaml.bak', '.mewcode/skills-extra/a',
                 '.mewcode/plans/a.md', '.mewcode/memory/facts.md', '.mewcode/sessions/a.jsonl']:
        assert sandbox.check_write(path)[0]
    assert sandbox.check(relative)[0]  # Read scope is unchanged.


def test_protected_links(tmp_path, isolated_home):
    from mewcode.permissions.sandbox import PathSandbox
    protected = tmp_path / '.mewcode/skills'
    protected.mkdir(parents=True)
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(protected, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlinks unavailable on this filesystem')
    sandbox = PathSandbox(str(tmp_path))
    assert not sandbox.check_write('alias/nonexistent.md')[0]
    # A protected definition symlinked out remains protected by its lexical identity too.
    external = tmp_path / 'external.yaml'
    external.write_text('original')
    (tmp_path / '.mewcode/config.yaml').symlink_to(external)
    assert not sandbox.check_write('.mewcode/config.yaml')[0]
    assert not sandbox.check_write(str(external))[0]


@pytest.mark.asyncio
async def test_always_allow_management_and_bound_write(tmp_path, isolated_home):
    target = tmp_path / 'source.py'
    client = MockLLMClient([[ToolCallComplete('normal', 'WriteFile',
                              {'file_path': 'source.py', 'content': 'ok'}), StreamEnd('tool_use')]])
    agent = _make_agent(tmp_path, client)
    agent.registry = agent.registry.bind_work_dir(str(tmp_path))
    local = tmp_path / '.mewcode/permissions.local.yaml'
    agent.permission_checker.rule_engine._local_path = local
    conv = ConversationManager()
    conv.add_user_message('write')
    async for e in agent.run(conv):
        if isinstance(e, PermissionRequest):
            e.future.set_result(PermissionResponse.ALLOW_ALWAYS)
    assert target.read_text() == 'ok'
    assert local.is_file()
    assert agent.permission_checker.rule_engine.evaluate('WriteFile', 'source.py') == 'allow'



@pytest.mark.skipif(__import__('os').name != 'nt', reason='Win32 path aliases')
@pytest.mark.parametrize('path', ['.MEWCODE/CONFIG.YAML', '.mewcode\\permissions.yaml',
                                  '.mewcode/config.yaml.', '.mewcode/config.yaml:stream'])
def test_windows_protected_aliases(tmp_path, isolated_home, path):
    from mewcode.permissions.sandbox import PathSandbox
    assert not PathSandbox(str(tmp_path)).check_write(path)[0]


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows junctions')
def test_windows_junction_nonexistent_target(tmp_path, isolated_home):
    import subprocess

    from mewcode.permissions.sandbox import PathSandbox
    protected = tmp_path / '.mewcode/skills'
    protected.mkdir(parents=True)
    alias = tmp_path / 'junction'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(protected)], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not PathSandbox(str(tmp_path)).check_write('junction/new.md')[0]

@pytest.mark.parametrize('mode', list(PermissionMode))
@pytest.mark.parametrize('name', ['WriteFile', 'EditFile'])
@pytest.mark.parametrize('path', ['.mewcode/permissions.yaml', '.mewcode/permissions.local.yaml',
                                  '.mewcode/skills/custom/SKILL.md'])
def test_policy_targets_before_allow_and_plan(tmp_path, isolated_home, mode, name, path):
    from mewcode.tools import create_default_registry
    agent = _make_agent(tmp_path, MockLLMClient([]))
    checker = agent.permission_checker
    checker.mode = mode
    checker.plan_file_path = str(tmp_path / path)
    checker.rule_engine.append_local_rule(Rule(name, '*', 'allow'))
    assert checker.check(create_default_registry().get(name), {'file_path': path}).effect == 'deny'
