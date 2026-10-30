"""Supervision contracts exercised through both real Agent loops (offline LLM)."""
from __future__ import annotations

import asyncio
import copy

import pytest
from pydantic import BaseModel

from mewcode.agent import Agent
from mewcode.client import LLMClient
from mewcode.config import ProviderConfig, ServiceConfig, _merge_config, load_config
from mewcode.conversation import ConversationManager
from mewcode.supervision import Advisory, AdvisoryBus, Expectation
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta, Tool, ToolCallComplete, ToolResult


class Params(BaseModel):
    fail: bool = False


class CheckTool(Tool[Params]):
    name = 'Check'
    description = 'An observable check; success means only this tool succeeded.'
    params_model = Params

    async def execute(self, params):
        return ToolResult(output='failed' if params.fail else 'ok', is_error=params.fail)


class Client(LLMClient):
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []

    async def stream(self, conversation, system='', tools=None):
        self.requests.append((copy.deepcopy(conversation), system))
        script = self.scripts.pop(0)
        for item in script:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, asyncio.Event):
                await item.wait()
            else:
                yield item


def done():
    return [TextDelta('done'), StreamEnd('end_turn', input_tokens=10, output_tokens=2)]


def make_agent(tmp_path, scripts, enabled=True):
    registry = ToolRegistry()
    registry.register(CheckTool())
    agent = Agent(Client(scripts), registry, 'anthropic', work_dir=str(tmp_path), enable_supervision=enabled)
    agent.stream_retry_delays = ()
    return agent


async def run(agent, mode, conversation=None):
    conversation = conversation or ConversationManager()
    if mode == 'interactive':
        conversation.add_user_message('task')
        return [e async for e in agent.run(conversation)]
    return await agent.run_to_completion('task', conversation)


def request_text(request):
    conv, system = request
    return system + str(conv.history) + str(conv.transient_reminders)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('enabled', [True, False])
async def test_real_request_delivery_and_off_compatibility(tmp_path, mode, enabled):
    agent = make_agent(tmp_path, [done()], enabled)
    conv = ConversationManager()
    bus = agent.supervision
    assert bus.publish(Advisory('x', 'unique-advice')) == enabled
    await run(agent, mode, conv)
    assert len(agent.client.requests) == 1
    assert ('unique-advice' in request_text(agent.client.requests[0])) == enabled
    assert 'unique-advice' not in str(conv.history) + str(conv.transient_reminders)
    if enabled:
        assert bus.entries['x'].state == 'delivered'
        assert bus.turn == 1 and bus.closed
        assert not any(e['type'] == 'ignored' for e in bus.events)
    else:
        assert bus.report() == {'enabled': False}
        assert not bus.events


def test_channels_utf8_budgets_priority_dedupe_and_ttl():
    bus = AdvisoryBus(True, limits={'system': (1, 110), 'reminder': (1, 130), 'status': (1, 100)})
    bus.begin()
    assert bus.publish(Advisory('low', '低', channel='system', priority=1, ttl=1))
    assert bus.publish(Advisory('high', '高', channel='system', priority=100))
    assert not bus.publish(Advisory('high', 'duplicate', channel='reminder'))
    assert bus.publish(Advisory('r', '提醒', ttl=1))
    assert bus.publish(Advisory('s', 'status', channel='status', ttl=1))
    selected = bus.select()
    assert selected == {'system': ['high'], 'reminder': ['r'], 'status': ['s']}
    assert bus.entries['r'].state == 'pending'  # rendering/selection is not delivery
    bus.confirm(bus.request_started(), ['high', 'r'])
    bus.select()
    assert bus.entries['low'].state == 'expired'
    assert bus.entries['s'].state == 'expired'  # status-only never model delivered
    assert not any(e['type'] == 'ignored' for e in bus.events)
    for channel, keys in selected.items():
        size = sum(len(bus.render(bus.entries[key].advisory).encode('utf-8')) for key in keys)
        assert size <= bus.limits[channel][1]


def test_bounded_unique_entries_and_event_log():
    bus = AdvisoryBus(True)
    for i in range(128):
        assert bus.publish(Advisory(str(i), 'advice'))
    assert not bus.publish(Advisory('overflow', 'advice'))
    for _ in range(400):
        bus.request_started()
    assert len(bus.events) == 256 and len(bus.entries) == 128
    with pytest.raises(ValueError):
        Advisory('oversize', 'x' * 16385)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('fail', [True, False])
async def test_attempt_and_success_are_distinct(tmp_path, mode, fail):
    agent = make_agent(tmp_path, [[ToolCallComplete('t', 'Check', {'fail': fail}), StreamEnd('tool_use')], done()])
    bus = agent.supervision
    bus.publish(Advisory('attempt', 'try check', expect=Expectation('tool_call', 'Check', 1)))
    bus.publish(Advisory('pass', 'check must succeed', expect=Expectation('tool_success', 'Check', 1)))
    bus.publish(Advisory('plain', 'plain text'))
    await run(agent, mode)
    assert bus.entries['attempt'].state == 'adopted'
    assert bus.entries['pass'].state == ('ignored' if fail else 'adopted')
    assert bus.entries['plain'].state == 'delivered'
    assert bus.turn == 2  # tool calls do not increment model rounds
    assert 'try check' not in request_text(agent.client.requests[1])


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_failed_before_response_retries_without_false_delivery(tmp_path, mode):
    agent = make_agent(tmp_path, [[ConnectionError('fake')], done()])
    agent.stream_retry_delays = (0,)
    bus = agent.supervision
    bus.publish(Advisory('x', 'retry-advice', expect=Expectation('tool_call', 'Check', 2)))
    await run(agent, mode)
    assert bus.turn == 1 and bus.attempt == 2
    assert bus.entries['x'].state == 'waiting'
    assert all('retry-advice' in request_text(r) for r in agent.client.requests)
    assert len([e for e in bus.events if e['type'] == 'delivered']) == 1
    failed = next(e for e in bus.events if e['type'] == 'request_failed')
    assert failed['confirmed'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('started', [True, False])
async def test_failure_delivery_requires_first_response(tmp_path, mode, started):
    script = ([TextDelta('partial')] if started else []) + [ValueError('fake')]
    agent = make_agent(tmp_path, [script])
    bus = agent.supervision
    bus.publish(Advisory('x', 'advice', expect=Expectation('tool_call', 'Check', 1)))
    # interactive loop reports some request errors as ErrorEvent rather than raising.
    try:
        await run(agent, mode)
    except ValueError:
        pass
    assert bus.entries['x'].state == ('waiting' if started else 'pending')
    assert bus.turn == int(started)
    assert not any(e['type'] == 'ignored' for e in bus.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('started', [True, False])
async def test_cancel_keeps_unfinished_windows_unresolved(tmp_path, mode, started):
    gate = asyncio.Event()
    script = ([TextDelta('partial')] if started else []) + [gate]
    agent = make_agent(tmp_path, [script])
    bus = agent.supervision
    bus.publish(Advisory('x', 'advice', expect=Expectation('tool_call', 'Check', 1)))
    task = asyncio.create_task(run(agent, mode))
    while not agent.client.requests:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.closed and bus.entries['x'].state == ('waiting' if started else 'pending')
    assert bus.events[-1]['outcome'] == 'cancelled'
    assert not any(e['type'] == 'ignored' for e in bus.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_new_run_reset_and_isolation(tmp_path, mode):
    agent = make_agent(tmp_path, [done(), done()])
    other = make_agent(tmp_path, [done()])
    bus = agent.supervision
    bus.publish(Advisory('x', 'old-run-text'))
    old_id = bus.run_id
    await run(agent, mode)
    await run(agent, mode)
    assert bus.run_id != old_id and bus.turn == 1 and not bus.entries
    assert 'old-run-text' not in request_text(agent.client.requests[1])
    assert not other.supervision.entries and other.supervision.turn == 0
    assert other.supervision.run_id != bus.run_id


def test_steer_supersedes_waiting_and_pending_without_ignored():
    bus = AdvisoryBus(True)
    bus.begin()
    bus.publish(Advisory('sent', 'text', expect=Expectation('tool_call', 'Check')))
    bus.publish(Advisory('unsent', 'text', channel='status'))
    bus.confirm(bus.request_started(), ['sent'])
    bus.steer()
    bus.observe('tool_call', 'Check')
    bus.settle()
    assert bus.last_user_input_turn == 1
    assert {v.state for v in bus.entries.values()} == {'superseded'}


def test_config_off_override_and_container_roundtrip(tmp_path):
    from mewcode.config import SandboxConfig
    from mewcode.service.sandbox import DockerSandbox

    def config(name, text):
        path = tmp_path / name
        path.write_text('providers:\n  - {name: fake, protocol: anthropic, base_url: http://invalid, model: fake}\n' + text, encoding='utf-8')
        return load_config(path)
    base = config('base.yaml', 'enable_supervision: true\nservice:\n  enable_supervision: true\n')
    absent = config('absent.yaml', '')
    assert _merge_config(base, absent).enable_supervision
    off = config('off.yaml', 'enable_supervision: false\nservice:\n  enable_supervision: false\n')
    merged = _merge_config(base, off)
    assert not merged.enable_supervision and not merged.service.enable_supervision
    sandbox = DockerSandbox(SandboxConfig(), mewcode_src=str(tmp_path), work_root=str(tmp_path / 'sandbox'))
    provider = ProviderConfig('fake', 'anthropic', 'http://invalid', 'fake', api_key='secret')
    path = sandbox.write_container_config(tmp_path / 'container.yaml', provider, enable_supervision=True)
    assert load_config(path).enable_supervision
    assert 'secret' not in path.read_text()
    with pytest.raises(Exception, match='boolean'):
        config('invalid.yaml', 'enable_supervision: 1\n')


@pytest.mark.asyncio
async def test_direct_service_uses_same_kernel(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mewcode.service.execution import HeadlessAgentRunner

    provider = ProviderConfig('fake', 'anthropic', 'http://invalid', 'fake')
    monkeypatch.setattr('mewcode.client.create_client', lambda _: Client([done()]))
    runner = HeadlessAgentRunner(ServiceConfig(enable_supervision=True), provider)
    agent = runner._build_agent(str(tmp_path))
    try:
        assert agent.supervision.enabled
        agent.client = Client([done()])
        agent.supervision.publish(Advisory('service', 'service-advice'))
        async def build(work_dir, callback):
            return agent, None
        runner._build_agent_with_tools = build
        events = []
        await runner._run_direct_body(SimpleNamespace(tokens_in=0, tokens_out=0), str(tmp_path), 'task', events.append, 0)
        report = next(e['report'] for e in events if e['type'] == 'supervision')
        assert report['entries']['service']['state'] == 'delivered'
    finally:
        await agent.cancel_background_tasks()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_context_recovery_resends_pending_advice(tmp_path, monkeypatch, mode):
    from mewcode.client import ContextOverflowError
    from mewcode.context import CompactBoundary, CompactEvent

    agent = make_agent(tmp_path, [[ContextOverflowError('fake')], done()])
    async def compact(conversation, *args, manual=False, **kwargs):
        if manual:
            return CompactEvent(100, CompactBoundary(summary='short', keep=[]))
        return None
    monkeypatch.setattr('mewcode.agent.auto_compact', compact)
    agent.supervision.publish(Advisory('x', 'recover-advice', ttl=1))
    await run(agent, mode)
    assert agent.supervision.turn == 1 and agent.supervision.attempt == 2
    assert agent.supervision.entries['x'].state == 'delivered'
    assert all('recover-advice' in request_text(r) for r in agent.client.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_inflight_steer_retains_delivery_but_cancels_expectation(tmp_path, mode):
    gate = asyncio.Event()
    agent = make_agent(tmp_path, [[gate, *done()]])
    bus = agent.supervision
    bus.publish(Advisory('x', 'previous-intent', expect=Expectation('tool_call', 'Check', 1)))
    task = asyncio.create_task(run(agent, mode))
    while not agent.client.requests:
        await asyncio.sleep(0)
    bus.steer()
    gate.set()
    await task
    assert bus.entries['x'].state == 'superseded'
    assert bus.entries['x'].delivered_turn == 1  # actual request already carried it
    assert len([e for e in bus.events if e['type'] == 'delivered']) == 1
    assert not any(e['type'] == 'ignored' for e in bus.events)


@pytest.mark.asyncio
async def test_real_subagent_creation_inherits_flag_without_ledger(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mewcode.agents.parser import AgentDef
    from mewcode.agents.task_manager import TaskManager
    from mewcode.agents.trace import TraceManager
    from mewcode.tools.agent_tool import AgentTool, AgentToolParams

    parent = make_agent(tmp_path, [done()])
    parent.supervision.publish(Advisory('parent-only', 'parent-private-advice'))
    definition = AgentDef(agent_type='worker', when_to_use='test', tools=['Check'], permission_mode='dontAsk')
    loader = SimpleNamespace(get=lambda name: definition)
    children = []
    def create_child(**kwargs):
        child = Agent(**kwargs)
        children.append(child)
        return child
    monkeypatch.setattr('mewcode.agent.Agent', create_child)
    tool = AgentTool(loader, TaskManager(), TraceManager(), parent)
    result = await tool.execute(AgentToolParams(prompt='task', description='test', subagent_type='worker'))
    assert not result.is_error, result.output
    child = children[0]
    assert child.supervision.enabled and child.supervision.turn == 1
    assert child.supervision.entries == {} and child.supervision.run_id != parent.supervision.run_id
    assert parent.supervision.turn == 0
    assert 'parent-private-advice' not in request_text(parent.client.requests[0])


@pytest.mark.asyncio
async def test_service_container_flag_and_structured_audit(tmp_path):
    import json
    from types import SimpleNamespace

    from mewcode.service.execution import ExecutionChain, HeadlessAgentRunner
    from mewcode.service.jobs import JobStore

    calls = []
    report = {'enabled': True, 'run_id': 'container-run', 'turn': 2, 'attempts': 2,
              'closed': True, 'counts': {'delivered': 1}}
    async def run_agent(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(input_tokens=1, output_tokens=1, extra={'supervision': report},
                               timed_out=False, exit_code=0, result_text='ok', tool_calls=0)
    cfg = ServiceConfig(enable_supervision=True)
    provider = ProviderConfig('fake', 'anthropic', 'http://invalid', 'fake')
    runner = HeadlessAgentRunner(cfg, provider, sandbox=SimpleNamespace(run_agent=run_agent))
    store = JobStore(tmp_path / 'jobs.db')
    await store.connect()
    try:
        job = await store.create_job(fingerprint='f', repo='repo', severity='high', title='t', payload={})
        chain = ExecutionChain(cfg, store, runner)
        await runner._run_in_sandbox(job, str(tmp_path), 'task', chain._make_on_event(job), 0)
        await chain._drain_event_tasks(job.id)
        assert calls[0]['enable_supervision'] is True
        event = next(e for e in await store.events(job.id) if e.kind == 'agent_supervision')
        assert json.loads(event.detail) == report
    finally:
        await store.close()


def test_all_channels_obey_wire_budgets_and_unselected_never_ignored():
    bus = AdvisoryBus(True, limits={'system': (0, 0), 'reminder': (1, 70), 'status': (1, 70)})
    for channel in ('system', 'reminder', 'status'):
        bus.publish(Advisory(channel, '超' * 100, channel=channel,
                             expect=Expectation('tool_call', 'Check', 1)))
    assert not any(bus.select().values())
    bus.confirm(bus.request_started(), [])
    bus.settle()
    assert {e.state for e in bus.entries.values()} == {'pending'}
    assert not any(e['type'] in ('delivered', 'ignored') for e in bus.events)


def test_confirmation_is_idempotent_and_old_run_cannot_deliver():
    bus = AdvisoryBus(True)
    bus.begin()
    request_id = bus.request_started()
    bus.confirm(request_id, [])
    bus.confirm(request_id, [])
    assert bus.turn == 1
    bus.begin()
    bus.confirm(request_id, [])
    assert bus.turn == 0
