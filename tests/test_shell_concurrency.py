"""Argument policy, real entry scheduling and owned shell/process outcomes."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mewcode.agent import Agent, PermissionRequest, ToolResultEvent, partition_tool_calls
from mewcode.conversation import ConversationManager
from mewcode.permissions.replies import PermissionReply, PermissionResponse
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from mewcode.tools.bash import Bash, Params
from mewcode.tools.shell_policy import concurrent_shell, semantic_nonerror
from tests.test_permission_security import MockLLMClient, _make_agent
from tests.test_request_pairing import assert_wire_pairs, capture


@pytest.mark.parametrize('command', ['git status', 'git status --short --branch', 'git status --porcelain=v2',
    'git status --untracked-files=no --no-renames'])
def test_portable_status_forms(command):
    assert all(concurrent_shell({'command': command}, windows=windows) for windows in [True, False])


@pytest.mark.parametrize('windows,command', [(True, 'cd'), (True, 'type sample.txt'),
                                           (False, 'pwd'), (False, 'cat src/sample.txt')])
def test_native_queries(windows, command):
    assert concurrent_shell({'command': command}, windows=windows)


@pytest.mark.parametrize('command', [
    'pwd; echo mutation', 'pwd\nwhoami', 'pwd && whoami', 'pwd | cat', 'pwd > output', 'pwd < input',
    'pwd $(touch marker)', 'pwd `id`', 'type %FILE%', 'type !FILE!', 'type file^&x',
    'A=1 pwd', 'git -c alias.status=evil status', 'git -C elsewhere status', 'git st', 'git status --help',
    'git status --ignored', 'git status --ignore-submodules=none', 'git status --short file', 'git diff',
    'git status --short --short', 'git branch -D branch', 'cat -n file', 'cat file other', 'cat ../outside',
    'cat /etc/passwd', 'cat "space name"', 'cat file*', 'cat file?', 'cat ./NUL.txt', 'type CON',
    'type C:/file', 'type src\\file', 'echo harmless', 'python --version', '', 'pwd\t', 'pwd\u00a0',
])
def test_ambiguous_or_unvalidated_commands_stay_serial(command):
    assert not concurrent_shell({'command': command}, windows=False)
    assert not concurrent_shell({'command': command}, windows=True)


@pytest.mark.parametrize('arguments', [{'command': 1}, {'command': 'pwd', 'timeout': True},
    {'command': 'pwd', 'timeout': -1}, {'command': 'pwd', 'timeout': 601}, {'command': 'pwd', 'extra': 'x'}])
def test_unknown_arguments_are_serial(arguments):
    assert not concurrent_shell(arguments, windows=False)


def test_legacy_boolean_and_accidental_method_are_not_confused(tmp_path):
    from mewcode.tools.read_file import ReadFile
    class Accidental(ReadFile):
        def is_concurrency_safe(self):
            return True
    registry = create_default_registry().bind_work_dir(str(tmp_path))
    assert partition_tool_calls([ToolCallComplete('r', 'ReadFile', {})], registry)[0].concurrent
    registry.register(Accidental().bind(str(tmp_path)))
    assert not partition_tool_calls([ToolCallComplete('r', 'ReadFile', {})], registry)[0].concurrent


def native_read(name='sample.txt'):
    return ('type ' if os.name == 'nt' else 'cat ') + name


async def run(agent, mode, conv=None, reply=None):
    conv = conv or ConversationManager()
    events = []
    if mode == 'interactive':
        conv.add_user_message('offline queries')
        async for event in agent.run(conv):
            events.append(event)
            if isinstance(event, PermissionRequest):
                event.future.set_result(reply or PermissionResponse.ALLOW)
    else:
        await agent.run_to_completion('offline queries', conv)
    return conv, events


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_actual_agent_overlap_barriers_order_and_real_outputs(tmp_path, mode):
    (tmp_path / 'sample.txt').write_text('before')
    trace, active, maximum = [], 0, 0
    writing = False
    gates = [asyncio.Event(), asyncio.Event()]
    count = 0
    class ObservedBash(Bash):
        async def execute(self, params):
            nonlocal count, active, maximum
            index = count
            count += 1
            trace.append(('start', index))
            if index >= 2:
                assert not writing and (tmp_path / 'barrier.txt').exists()
            if self.can_run_concurrently(params.model_dump()):
                active += 1
                maximum = max(maximum, active)
                gate = gates[0 if index < 2 else 1]
                if active == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 5)
            result = await super().execute(params)
            if self.can_run_concurrently(params.model_dump()):
                active -= 1
            trace.append(('end', index))
            return result
    from mewcode.tools.write_file import WriteFile
    class ObservedWrite(WriteFile):
        async def execute(self, params):
            nonlocal writing
            assert active == 0
            writing = True
            trace.append(('write-start', -1))
            await asyncio.sleep(0.02)
            result = await super().execute(params)
            writing = False
            trace.append(('write-end', -1))
            return result
    calls = [ToolCallComplete(str(i), 'Bash', {'command': native_read()}) for i in range(2)]
    calls += [ToolCallComplete('write', 'WriteFile', {'file_path': 'barrier.txt', 'content': 'written'}),
              ToolCallComplete('normal', 'Bash', {'command': 'echo checkpoint'})]
    calls += [ToolCallComplete(str(i), 'Bash', {'command': native_read()}) for i in range(3, 5)]
    registry = create_default_registry().bind_work_dir(str(tmp_path))
    registry.register(ObservedBash().bind(str(tmp_path)))
    registry.register(ObservedWrite().bind(str(tmp_path)))
    agent = Agent(MockLLMClient([[*calls, StreamEnd('tool_use')], [TextDelta('done'), StreamEnd('end_turn')]]),
                  registry, 'anthropic', work_dir=str(tmp_path))
    conv, events = await asyncio.wait_for(run(agent, mode), 15)
    assert maximum == 2 and trace[:2] == [('start', 0), ('start', 1)]
    assert trace.index(('start', 2)) > trace.index(('end', 0)) and trace.index(('start', 2)) > trace.index(('end', 1))
    assert trace.index(('start', 3)) > trace.index(('end', 2))
    assert trace.index(('write-end', -1)) < trace.index(('start', 2))
    assert (tmp_path / 'barrier.txt').read_text() == 'written'
    results = [r for message in conv.history for r in message.tool_results]
    assert [r.tool_use_id for r in results] == [c.tool_id for c in calls]
    assert all(not r.is_error for r in results)
    assert all('Exit code: 0' in r.content and 'before' in r.content for r in results if r.tool_use_id in {'0','1','3','4'})
    if mode == 'interactive':
        assert [e.tool_id for e in events if isinstance(e, ToolResultEvent)] == [c.tool_id for c in calls]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('effect', ['deny', 'ask'])
async def test_parallel_shell_still_uses_permission_feedback(tmp_path, monkeypatch, mode, effect):
    for name in ['one.txt', 'two.txt']:
        (tmp_path / name).write_text(name)
    first, second = native_read('one.txt'), native_read('two.txt')
    rules = f"- rule: 'Bash({second})'\n  effect: allow\n"
    if effect == 'deny':
        rules += f"- rule: 'Bash({first})'\n  effect: deny\n"
    else:
        # POSIX cat is auto-approved. Exercise escalation with only that helper
        # stubbed; the actual PermissionChecker/default-mode policy still runs.
        from mewcode.permissions.checker import is_safe_command
        monkeypatch.setattr('mewcode.permissions.checker.is_safe_command',
                            lambda command: command != first and is_safe_command(command))
    (tmp_path / 'local_rules.yaml').write_text(rules)
    calls = [ToolCallComplete('first', 'Bash', {'command': first}), ToolCallComplete('second', 'Bash', {'command': second})]
    agent = _make_agent(tmp_path, MockLLMClient([[*calls, StreamEnd('tool_use')], [TextDelta('done'), StreamEnd('end_turn')]]))
    agent.registry = agent.registry.bind_work_dir(str(tmp_path))
    conv, _ = await run(agent, mode, reply=PermissionReply(PermissionResponse.DENY, 'only-first'))
    results = [r for message in conv.history for r in message.tool_results]
    assert [r.tool_use_id for r in results] == ['first', 'second']
    assert results[0].is_error and not results[1].is_error and 'two.txt' in results[1].content
    assert ('only-first' in results[0].content) == (effect == 'ask' and mode == 'interactive')
    assert 'only-first' not in results[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_parallel_pre_rejection_post_hooks_and_supervision(tmp_path, monkeypatch, mode):
    from mewcode.hooks import Action, Hook, HookEngine
    from mewcode.hooks.conditions import Condition, ConditionGroup

    first, second = native_read('one.txt'), native_read('two.txt')
    for name in ['one.txt', 'two.txt']:
        (tmp_path / name).write_text(name)
    logger, log = tmp_path / 'hook.py', tmp_path / 'hooks.jsonl'
    logger.write_text('import sys, json\nfrom pathlib import Path\ndata=json.load(sys.stdin)\n'
                      f'with Path({str(log)!r}).open("a") as f: f.write(json.dumps(data) + "\\n")\n'
                      f'sys.exit(1 if data["event"] == "pre_tool_use" and data["args"]["command"] == {first!r} else 0)\n')
    action = Action(type='command', command=shell_command([sys.executable, str(logger)]))
    hooks = HookEngine([
        Hook('pre-first', 'pre_tool_use', action, ConditionGroup([Condition('args.command', '==', first)]), reject=True),
        Hook('pre-second', 'pre_tool_use', action, ConditionGroup([Condition('args.command', '==', second)])),
        Hook('post', 'post_tool_use', action),
    ])
    executed = []
    class Observed(Bash):
        async def execute(self, params):
            executed.append(params.command)
            return await super().execute(params)
    registry = create_default_registry().bind_work_dir(str(tmp_path))
    registry.register(Observed().bind(str(tmp_path)))
    agent = Agent(MockLLMClient([[ToolCallComplete('one', 'Bash', {'command': first}),
                                 ToolCallComplete('two', 'Bash', {'command': second}), StreamEnd('tool_use')]]),
                  registry, 'anthropic', work_dir=str(tmp_path), hook_engine=hooks, enable_supervision=True)
    observed, original = [], agent.supervision.observe
    def observe(event, name):
        observed.append((event, name))
        original(event, name)
    monkeypatch.setattr(agent.supervision, 'observe', observe)
    conv, _ = await run(agent, mode)
    rows = [json.loads(row) for row in log.read_text().splitlines()]
    assert [(r['event'], r['args']['command']) for r in rows].count(('pre_tool_use', first)) == 1
    assert [(r['event'], r['args']['command']) for r in rows].count(('pre_tool_use', second)) == 1
    assert [(r['event'], r['args']['command']) for r in rows if r['event'] == 'post_tool_use'] == [('post_tool_use', second)]
    assert executed == [second] and observed.count(('tool_success', 'Bash')) == 1
    assert [r.is_error for message in conv.history for r in message.tool_results] == [True, False]


@pytest.mark.parametrize('command,code,nonerror', [
    ('rg NOMATCH file.txt', 1, True), ('grep -n NOMATCH file.txt', 1, True), ('diff -u a.txt b.txt', 1, True),
    ('rg NOMATCH file.txt && echo done', 1, False), ('grep --unknown file', 1, False),
    ('diff --to-file=out a', 1, False), ('pytest', 1, False), ('git diff', 1, False),
    ('rg NOMATCH file.txt', 2, False), ('false', 1, False),
])
def test_nonzero_exceptions_require_a_recognized_simple_query(command, code, nonerror):
    assert semantic_nonerror(command, code) is nonerror


def shell_command(argv):
    return subprocess.list2cmdline(argv) if os.name == 'nt' else shlex.join(argv)


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [0, 1, 7])
async def test_real_shell_exit_codes_and_streams(tmp_path, code):
    command = shell_command([sys.executable, '-c', f'import sys; print("out"); print("err", file=sys.stderr); sys.exit({code})'])
    result = await Bash().bind(str(tmp_path)).execute(Params(command=command))
    assert result.output.startswith(f'Exit code: {code}\n') and result.is_error == (code != 0)
    assert 'STDOUT:\nout' in result.output and 'STDERR:\nerr' in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['rg', 'grep', 'diff'])
async def test_real_no_match_and_difference_results(tmp_path, name):
    if not shutil.which(name):
        pytest.skip(f'{name} is not installed on this platform')
    (tmp_path / 'a.txt').write_text('alpha\n')
    (tmp_path / 'b.txt').write_text('beta\n')
    command = f'{name} a.txt b.txt' if name == 'diff' else f'{name} NOMATCH a.txt'
    result = await Bash().bind(str(tmp_path)).execute(Params(command=command))
    assert result.output.startswith('Exit code: 1\n') and not result.is_error
    result = await Bash().bind(str(tmp_path)).execute(Params(command=command + ' && echo later'))
    assert result.output.startswith('Exit code: 1\n') and result.is_error


@pytest.mark.asyncio
async def test_failure_timeout_and_output_budget_keep_exit_status(tmp_path, monkeypatch):
    script = tmp_path / 'test_failure.py'
    script.write_text('def test_failure():\n    assert False\n')
    result = await Bash().bind(str(tmp_path)).execute(Params(command=shell_command([sys.executable, '-m', 'pytest', '-q', script.name])))
    assert result.is_error and result.output.startswith('Exit code: 1\n')
    result = await Bash().bind(str(tmp_path)).execute(Params(command=shell_command([sys.executable, '-c', 'import time; time.sleep(10)']), timeout=1))
    assert result.is_error and 'timeout termination' in result.output and 'timed out' in result.output
    monkeypatch.setattr('mewcode.tools.bash.MAX_CAPTURE_BYTES', 32)
    result = await Bash().bind(str(tmp_path)).execute(Params(command=shell_command([sys.executable, '-c', 'print("x" * 1000)'])))
    assert result.is_error and 'output limit' in result.output and result.output.startswith('Exit code:')


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, check=True).stdout


@pytest.mark.asyncio
async def test_controlled_git_disables_filters_fsmonitor_pager_and_preserves_index(tmp_path):
    git(tmp_path, 'init')
    git(tmp_path, 'config', 'user.name', 'Offline')
    git(tmp_path, 'config', 'user.email', 'offline@example.invalid')
    (tmp_path / 'a.txt').write_text('original\n')
    git(tmp_path, 'add', 'a.txt')
    git(tmp_path, 'commit', '-m', 'fixture')
    marker, script = tmp_path / 'executed', tmp_path / 'external.py'
    script.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    command = shell_command([sys.executable, str(script)])
    for key in ['core.fsmonitor', 'core.pager', 'diff.external', 'filter.test.clean', 'filter.test.process', 'alias.st']:
        git(tmp_path, 'config', key, command)
    git(tmp_path, 'config', 'filter.test.required', 'true')
    (tmp_path / '.gitattributes').write_text('*.txt filter=test\n')
    (tmp_path / 'a.txt').write_text('changed\n')
    index = (tmp_path / '.git/index').read_bytes()
    result = await Bash().bind(str(tmp_path)).execute(Params(command='git status --short'))
    assert not result.is_error and result.output.startswith('Exit code: 0\n') and 'a.txt' in result.output
    assert not marker.exists() and (tmp_path / '.git/index').read_bytes() == index
    git(tmp_path, 'config', 'filter.bad=name.clean', command)
    result = await Bash().bind(str(tmp_path)).execute(Params(command='git status'))
    assert result.is_error and 'Unsupported Git filter name' in result.output and not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_parallel_cancellation_owns_children_and_preserves_request_pairs(tmp_path, monkeypatch, mode):
    from mewcode.processes import _jobs, create_shell_process
    from tests.test_process_cleanup import assert_reaped, cleanup, wait_for_descendants

    roots = [tmp_path / 'one', tmp_path / 'two']
    helper = Path(__file__).parent / 'helpers' / 'process_tree.py'
    for root in roots:
        root.mkdir()
    started = 0
    async def spawn(command, **kwargs):
        nonlocal started
        root = roots[started]
        started += 1
        return await create_shell_process(shell_command([sys.executable, str(helper), str(root), 'tree', 'parent']), **kwargs)
    monkeypatch.setattr('mewcode.tools.bash.create_shell_process', spawn)
    query = 'cd' if os.name == 'nt' else 'pwd'
    calls = [ToolCallComplete(str(i), 'Bash', {'command': query}) for i in range(2)]
    registry = create_default_registry().bind_work_dir(str(tmp_path))
    agent = Agent(MockLLMClient([[*calls, StreamEnd('tool_use')]]), registry, 'anthropic', work_dir=str(tmp_path))
    conv = ConversationManager()
    task = asyncio.create_task(run(agent, mode, conv))
    try:
        await asyncio.gather(*(wait_for_descendants(root) for root in roots))
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert started == 2 and not _jobs
        for root in roots:
            await assert_reaped(root)
        for protocol in ['anthropic', 'openai', 'openai-compat']:
            payload = await capture(protocol, conv)
            assert_wire_pairs(protocol, payload)
            assert 'unknown' in str(payload).lower()
    finally:
        for root in roots:
            await cleanup(root, task)
