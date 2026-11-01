"""Self-verify evidence and delivery through real loops, with no model calls."""
from __future__ import annotations

import asyncio

import pytest
from test_supervision import Client, done, request_text, run

from mewcode.agent import Agent
from mewcode.permissions import PermissionMode
from mewcode.supervision import Advisory
from mewcode.supervision.self_verify import KEY, verification_kind
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, ToolCallComplete


def calls(*items):
    return [ToolCallComplete(str(i), name, args) for i, (name, args) in enumerate(items)] + [StreamEnd('tool_use')]


def write(path='app.py'):
    return 'WriteFile', {'file_path': path, 'content': 'answer = 42\n'}


def bash(command):
    return 'Bash', {'command': command}


def agent_for(tmp_path, scripts, enabled=True):
    agent = Agent(Client(scripts), create_default_registry().bind_work_dir(str(tmp_path)),
                  'anthropic', work_dir=str(tmp_path), enable_supervision=enabled)
    agent.stream_retry_delays = ()
    return agent


@pytest.mark.parametrize('command,kind', [
    ('pytest', 'pytest'), ('pytest.exe -q tests/test_app.py', 'pytest'),
    ('python -m pytest -q', 'pytest'), ('python3.11 -m mypy app.py', 'mypy'),
    ('uv run pytest tests', 'pytest'), ('uv run python -m pytest', 'pytest'),
    ('"C:\\Python 311\\python.exe" -m pytest', 'pytest'), ('ruff check .', 'ruff'),
    ('uv run ruff check app.py', 'ruff'), ('python -m ruff check .', 'ruff'),
    ('echo pytest', None), ('git diff', None), ('pytest --collect-only', None),
    ('pytest --co -q', None), ('pytest --help', None), ('mypy --version', None),
    ('pytest --collectonly', None), ('pytest --markers', None), ('pytest --fixtures', None),
    ('ruff check --show-files', None), ('ruff check --show-settings', None),
    ('pytest --co=true', None), ('pytest %OPTS%', None), ('pytest # --collect-only', None),
    ('ruff check --fix .', None), ('ruff format .', None), ('pytest; exit 0', None),
    ('pytest && echo ok', None), ('pytest | cat', None), ('pytest > out', None),
    ('cd repo\npytest', None), ('python -c "print(42)"', None),
    ('uv run --locked pytest', None), ('py -m pytest', None), ('npm test', None),
    ('pytest "unterminated', None), ('', None), ('uv run', None),
])
def test_command_contract(command, kind):
    assert verification_kind(command) == kind


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('enabled', [True, False])
async def test_edit_reminder_is_transient_once_and_no_extra_request(tmp_path, mode, enabled):
    agent = agent_for(tmp_path, [calls(write()), calls(bash('echo reading')), done()], enabled)
    await run(agent, mode)
    assert (tmp_path / 'app.py').read_text() == 'answer = 42\n'
    assert len(agent.client.requests) == 3
    assert KEY not in request_text(agent.client.requests[0])
    assert (KEY in request_text(agent.client.requests[1])) == enabled
    assert KEY not in request_text(agent.client.requests[2])
    if enabled:
        assert agent.supervision.entries[KEY].state == 'ignored'
        assert agent.supervision.self_verify.report() == {
            'revision': 1, 'attempted_revision': 0, 'successful_revision': 0,
            'suspended': False, 'coverage': 'unknown'}
    else:
        assert agent.supervision.report() == {'enabled': False}
        assert not agent.supervision.events


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('path', ['README.md', 'docs/guide.txt', 'app.ts'])
async def test_non_python_and_readonly_are_exempt(tmp_path, mode, path):
    agent = agent_for(tmp_path, [calls(write(path)), calls(('ReadFile', {'file_path': path})), done()])
    await run(agent, mode)
    assert KEY not in agent.supervision.entries
    assert agent.supervision.self_verify.revision == 0
    assert not any(e['type'] == 'verification_attempt' for e in agent.supervision.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('success', [True, False])
async def test_actual_pytest_attempt_adopts_even_when_failed(tmp_path, mode, success):
    (tmp_path / 'test_app.py').write_text(f'def test_app():\n    assert {success}\n')
    agent = agent_for(tmp_path, [calls(write()), calls(bash('python -m pytest -q test_app.py')), done()])
    await run(agent, mode)
    bus = agent.supervision
    assert bus.entries[KEY].state == 'adopted'
    assert bus.self_verify.attempted_revision == 1
    assert bus.self_verify.successful_revision == int(success)
    result = next(e for e in bus.events if e['type'] == 'verification_result')
    assert result['tool_success'] == success and result['coverage'] == 'unknown'
    assert len(agent.client.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_attempt_before_edit_does_not_discharge_edit(tmp_path, mode):
    (tmp_path / 'test_app.py').write_text('def test_app():\n    pass\n')
    agent = agent_for(tmp_path, [calls(bash('python -m pytest -q test_app.py')), calls(write()), done()])
    await run(agent, mode)
    assert KEY in request_text(agent.client.requests[-1])
    assert agent.supervision.self_verify.attempted_revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_same_response_check_suppresses_and_later_edit_triggers(tmp_path, mode):
    (tmp_path / 'test_app.py').write_text('def test_app():\n    pass\n')
    agent = agent_for(tmp_path, [calls(write(), bash('python -m pytest -q test_app.py')),
                                 calls(write('other.pyi')), done()])
    await run(agent, mode)
    assert KEY not in request_text(agent.client.requests[1])
    assert KEY in request_text(agent.client.requests[2])
    assert agent.supervision.self_verify.revision == 2
    assert agent.supervision.self_verify.successful_revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_no_applicable_tests_is_advice_not_gate(tmp_path, mode):
    agent = agent_for(tmp_path, [calls(write()), done()])
    await run(agent, mode)
    text = request_text(agent.client.requests[1])
    assert 'no applicable runnable check' in text
    assert len(agent.client.requests) == 2
    assert agent.supervision.entries[KEY].state == 'waiting'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_failed_invalid_or_disabled_writes_do_not_trigger(tmp_path, mode):
    for item in [('EditFile', {'file_path': 'missing.py', 'old_string': 'a', 'new_string': 'b'}),
                 ('WriteFile', {'file_path': 'app.py'})]:
        agent = agent_for(tmp_path, [calls(item), done()])
        await run(agent, mode)
        assert KEY not in agent.supervision.entries
    agent = agent_for(tmp_path, [calls(write()), done()])
    agent.registry.disable('WriteFile')
    await run(agent, mode)
    assert KEY not in agent.supervision.entries


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_editfile_and_no_validator_or_plan_mode(tmp_path, mode):
    for unavailable in ['disabled', 'plan']:
        (tmp_path / 'app.py').write_text('x = 1\n')
        agent = agent_for(tmp_path, [calls(('ReadFile', {'file_path': 'app.py'})),
                                     calls(('EditFile', {'file_path': 'app.py', 'old_string': '1', 'new_string': '2'})),
                                     done()])
        if unavailable == 'disabled':
            agent.registry.disable('Bash')
        else:
            agent.set_permission_mode(PermissionMode.PLAN)
        await run(agent, mode)
        assert agent.supervision.self_verify.revision == 1
        assert KEY not in agent.supervision.entries


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_hard_limit_and_resume_reset(tmp_path, mode):
    agent = agent_for(tmp_path, [calls(write()), done()])
    agent.max_iterations = 1
    await run(agent, mode)
    old_id = agent.supervision.run_id
    assert len(agent.client.requests) == 1 and KEY not in agent.supervision.entries
    await run(agent, mode)
    assert agent.supervision.run_id != old_id
    assert agent.supervision.self_verify.revision == 0
    assert KEY not in agent.supervision.entries


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_retry_and_cancel_do_not_fabricate_delivery(tmp_path, mode):
    gate = asyncio.Event()
    agent = agent_for(tmp_path, [calls(write()), [ConnectionError('offline')], [gate]])
    agent.stream_retry_delays = (0,)
    task = asyncio.create_task(run(agent, mode))
    while len(agent.client.requests) < 3:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.supervision.entries[KEY].state == 'pending'
    assert agent.supervision.entries[KEY].delivered_turn is None
    assert all(KEY in request_text(r) for r in agent.client.requests[1:])
    assert agent.supervision.turn == 1 and agent.supervision.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_denied_validation_is_not_attempt(tmp_path, mode):
    agent = agent_for(tmp_path, [calls(write()), calls(bash('python -m pytest')), done()])
    # Missing/disabled/denied tools never reach the execution observer.
    agent.registry.disable('Bash')
    await run(agent, mode)
    assert agent.supervision.self_verify.attempted_revision == 0
    assert not any(e['type'] == 'verification_attempt' for e in agent.supervision.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('blocked', ['WriteFile', 'Bash'])
@pytest.mark.parametrize('gate', ['permission', 'hook'])
async def test_real_gates_exclude_observation(tmp_path, mode, blocked, gate):
    from mewcode.hooks import HookEngine
    from mewcode.hooks.conditions import parse_condition
    from mewcode.hooks.models import Action, Hook
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        Rule,
        RuleEngine,
    )

    agent = agent_for(tmp_path, [calls(write()), calls(bash('python -m pytest')), done()])
    if gate == 'permission':
        rules = RuleEngine(local_rules_path=tmp_path / 'rules.yaml')
        rules.append_local_rule(Rule(blocked, '*', 'deny'))
        agent.permission_checker = PermissionChecker(
            DangerousCommandDetector(), PathSandbox(str(tmp_path)),
            rules, mode=PermissionMode.DONT_ASK)
        agent.set_permission_mode(PermissionMode.DONT_ASK)
    else:
        agent.hook_engine = HookEngine([Hook(
            'reject', 'pre_tool_use', Action(type='prompt', prompt='blocked'),
            condition=parse_condition(f'tool == {blocked}'), reject=True,
        )]).for_owner(agent.agent_id)
    await run(agent, mode)
    assert agent.supervision.self_verify.revision == (0 if blocked == 'WriteFile' else 1)
    assert agent.supervision.self_verify.attempted_revision == 0
    if blocked == 'Bash':
        assert not any(e['type'] == 'verification_attempt' for e in agent.supervision.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_steer_inflight_does_not_resurrect_and_new_edit_can_trigger(tmp_path, mode):
    gate = asyncio.Event()
    agent = agent_for(tmp_path, [calls(write()), [gate, *calls(write('second.py'))], done()])
    task = asyncio.create_task(run(agent, mode))
    while len(agent.client.requests) < 2:
        await asyncio.sleep(0)
    bus = agent.supervision
    bus.steer()
    gate.set()
    await task
    # The sent reminder is physically delivered, but its expectation stays superseded.
    assert bus.entries[KEY].state == 'superseded'
    assert bus.entries[KEY].delivered_turn == 2
    assert bus.self_verify.revision == 2 and not bus.self_verify.suspended
    assert KEY not in request_text(agent.client.requests[2])


@pytest.mark.asyncio
async def test_inflight_tool_result_after_steer_and_tool_cancel(tmp_path, monkeypatch):
    from mewcode.supervision.runtime import execute_observed_tool
    from mewcode.tools.base import ToolResult
    from mewcode.tools.bash import Bash, Params
    from mewcode.tools.write_file import Params as WriteParams
    from mewcode.tools.write_file import WriteFile

    agent = agent_for(tmp_path, [])
    agent.supervision.begin()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(self, params):
        entered.set()
        await release.wait()
        return ToolResult('ok')

    monkeypatch.setattr(WriteFile, 'execute', held)
    task = asyncio.create_task(execute_observed_tool(agent, WriteFile(), WriteParams(file_path='x.py', content='x')))
    await entered.wait()
    agent.supervision.steer()
    release.set()
    await task
    assert agent.supervision.self_verify.revision == 0
    assert agent.supervision.self_verify.suspended
    entered.clear()
    release.clear()
    monkeypatch.setattr(Bash, 'execute', held)
    agent.supervision.self_verify.revision = 1
    task = asyncio.create_task(execute_observed_tool(agent, Bash(), Params(command='pytest')))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.supervision.self_verify.attempted_revision == 1
    assert agent.supervision.self_verify.successful_revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_service_direct_runner_uses_shared_detector(tmp_path, monkeypatch, enabled):
    from types import SimpleNamespace

    from mewcode.config import ProviderConfig, ServiceConfig
    from mewcode.service.execution import HeadlessAgentRunner

    client = Client([calls(write()), done()])
    monkeypatch.setattr('mewcode.client.create_client', lambda _: client)
    runner = HeadlessAgentRunner(ServiceConfig(enable_supervision=enabled),
                                ProviderConfig('fake', 'anthropic', 'http://invalid', 'fake'))
    agent = runner._build_agent(str(tmp_path))

    async def build(work_dir, callback):
        return agent, None

    runner._build_agent_with_tools = build
    events = []
    try:
        await runner._run_direct_body(SimpleNamespace(tokens_in=0, tokens_out=0), str(tmp_path),
                                      'task', events.append, 0)
        assert len(client.requests) == 2
        assert (KEY in request_text(client.requests[1])) == enabled
        reports = [e['report'] for e in events if e['type'] == 'supervision']
        assert bool(reports) == enabled
        if enabled:
            assert reports[0]['self_verify']['revision'] == 1
            assert reports[0]['entries'][KEY]['delivered_turn'] == 2
    finally:
        await agent.cancel_background_tasks()


@pytest.mark.asyncio
async def test_parallel_raw_execution_and_shared_budget(tmp_path):
    agent = agent_for(tmp_path, [calls(write(), write('other.py')), done()])
    # Explicitly exercise the raw parallel execution path, which has separate gates.
    agent.registry.get('WriteFile').is_concurrency_safe = True
    agent.max_iterations = 2
    await run(agent, 'interactive')
    bus = agent.supervision
    assert bus.self_verify.revision == 2
    assert bus.entries[KEY].delivered_turn == 2
    assert bus.entries['builtin:iteration-budget'].delivered_turn == 2
    assert len(agent.client.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('started', [False, True])
async def test_stream_failure_keeps_unresolved_evidence_and_next_run_resets(tmp_path, mode, started):
    from mewcode.tools.base import TextDelta

    failing = ([TextDelta('partial')] if started else []) + [ValueError('offline')]
    agent = agent_for(tmp_path, [calls(write()), failing, done()])
    try:
        await run(agent, mode)
    except ValueError:
        pass
    bus = agent.supervision
    assert bus.entries[KEY].delivered_turn == (2 if started else None)
    assert bus.entries[KEY].state == ('waiting' if started else 'pending')
    assert bus.self_verify.successful_revision == bus.self_verify.attempted_revision == 0
    old_id = bus.run_id
    await run(agent, mode)
    assert bus.run_id != old_id and bus.self_verify.revision == 0
    assert KEY not in bus.entries


@pytest.mark.asyncio
async def test_replacement_tools_are_not_trusted_by_name(tmp_path):
    from mewcode.tools.bash import Bash
    from mewcode.tools.write_file import WriteFile

    class ReplacementWrite(WriteFile):
        pass

    class ReplacementBash(Bash):
        pass

    agent = agent_for(tmp_path, [calls(write()), done()])
    agent.registry.register(ReplacementWrite().bind(str(tmp_path)))
    await run(agent, 'headless')
    assert (tmp_path / 'app.py').exists()
    assert agent.supervision.self_verify.revision == 0
    assert KEY not in agent.supervision.entries
    agent = agent_for(tmp_path, [calls(write('builtin.py')), calls(bash('python -m pytest')), done()])
    agent.registry.register(ReplacementBash().bind(str(tmp_path)))
    await run(agent, 'headless')
    assert agent.supervision.self_verify.revision == 1
    assert agent.supervision.self_verify.attempted_revision == 0
    assert KEY not in agent.supervision.entries


def test_budget_ttl_withdrawal_capacity_and_steer(tmp_path):
    agent = agent_for(tmp_path, [])
    bus = agent.supervision
    detector = bus.self_verify
    bus.begin()
    detector.revision = 1
    bus.limits['reminder'] = (0, 0)
    detector.suggest(True)
    assert KEY not in bus.select()['reminder']
    for _ in range(3):
        bus.confirm(bus.request_started(), [])
    bus.select()
    assert bus.entries[KEY].state == 'expired'
    detector.suggest(True)
    assert len([e for e in bus.events if e['type'] == 'self_verify_suggested']) == 1
    bus.begin()
    detector.revision = 1
    detector.suggest(True)
    detector.attempted_revision = 1
    detector.suggest(True)
    assert bus.entries[KEY].state == 'superseded'
    bus.begin()
    detector.revision = 1
    detector.suggest(True)
    bus.steer()
    detector.suggest(True)
    assert detector.suspended and bus.entries[KEY].state == 'superseded'
    bus.begin()
    detector.revision = 1
    for i in range(128):
        bus.publish(Advisory(str(i), 'existing'))
    detector.suggest(True)
    assert KEY not in bus.entries
