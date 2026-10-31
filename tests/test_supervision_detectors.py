"""Budget advice through actual loops, including retries and hard stops."""
from __future__ import annotations

import asyncio

import pytest
from test_supervision import done, make_agent, request_text, run

from mewcode.supervision import Advisory, AdvisoryBus
from mewcode.supervision.detectors import warn_iteration_budget
from mewcode.tools.base import StreamEnd, ToolCallComplete

KEY = "builtin:iteration-budget"


def step(i):
    return [ToolCallComplete(str(i), 'Check', {}), StreamEnd('tool_use')]


@pytest.mark.parametrize('limit,first', [(1, 1), (2, 2), (10, 10), (11, 10), (50, 46)])
def test_threshold_once_per_run_and_new_run_rearms(limit, first):
    bus = AdvisoryBus(True)
    bus.begin()
    for iteration in range(1, limit + 1):
        warn_iteration_budget(bus, limit, iteration)
        assert (KEY in bus.entries) == (iteration >= first)
    events = [e for e in bus.events if e['type'] == 'iteration_budget_warning']
    assert len(events) == 1
    assert events[0]['remaining'] == limit - first + 1
    assert bus.entries[KEY].advisory.expect is None
    bus.begin()
    warn_iteration_budget(bus, limit, first)
    assert KEY in bus.entries


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
@pytest.mark.parametrize('enabled', [False, True])
async def test_budget_warning_delivered_once_and_no_extra_requests(tmp_path, mode, enabled):
    agent = make_agent(tmp_path, [step(i) for i in range(9)] + [done()], enabled)
    agent.max_iterations = 10
    await run(agent, mode)
    assert len(agent.client.requests) == 10
    assert all(KEY not in request_text(r) for r in agent.client.requests[:9])
    assert (KEY in request_text(agent.client.requests[-1])) == enabled
    if enabled:
        assert agent.supervision.entries[KEY].state == 'delivered'
        assert agent.supervision.entries[KEY].delivered_turn == 10
    else:
        assert not agent.supervision.events


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_short_task_and_hard_stop(tmp_path, mode):
    short = make_agent(tmp_path, [done()])
    short.max_iterations = 10
    await run(short, mode)
    assert KEY not in short.supervision.entries
    agent = make_agent(tmp_path, [step(1), step(2)])
    agent.max_iterations = 2
    await run(agent, mode)
    assert len(agent.client.requests) == 2
    assert agent.supervision.entries[KEY].state == 'delivered'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_retry_preserves_pending_warning_and_model_turn_count(tmp_path, mode):
    agent = make_agent(tmp_path, [[ConnectionError('offline')], done()])
    agent.max_iterations = 1
    agent.stream_retry_delays = (0,)
    await run(agent, mode)
    assert len(agent.client.requests) == 2
    assert all(KEY in request_text(r) for r in agent.client.requests)
    assert agent.supervision.turn == 1
    assert len([e for e in agent.supervision.events if e['type'] == 'delivered']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_failure_before_response_does_not_deliver(tmp_path, mode):
    agent = make_agent(tmp_path, [[ValueError('offline')]])
    agent.max_iterations = 1
    try:
        await run(agent, mode)
    except ValueError:
        pass
    assert agent.supervision.entries[KEY].state == 'pending'
    assert agent.supervision.turn == 0


def test_channel_budget_and_steer_do_not_fabricate_delivery_or_rearm():
    bus = AdvisoryBus(True, limits={'system': (0, 0), 'reminder': (0, 0), 'status': (0, 0)})
    bus.begin()
    warn_iteration_budget(bus, 10, 10)
    assert not any(bus.select().values())
    bus.confirm(bus.request_started(), [])
    bus.settle()
    assert bus.entries[KEY].state == 'pending'
    bus.steer()
    warn_iteration_budget(bus, 10, 10)
    assert bus.entries[KEY].state == 'superseded'


def test_disabled_invalid_boundaries_and_shared_capacity():
    for enabled in (False, True):
        bus = AdvisoryBus(enabled)
        for limit, iteration in [(0, 1), (-1, 1), (1, 0), (1, 2)]:
            warn_iteration_budget(bus, limit, iteration)
        assert not bus.entries
    bus = AdvisoryBus(True)
    for i in range(128):
        bus.publish(Advisory(str(i), 'existing'))
    warn_iteration_budget(bus, 1, 1)
    assert KEY not in bus.entries
    assert not any(e['type'] == 'iteration_budget_warning' for e in bus.events)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_cancel_before_response_keeps_warning_unconfirmed(tmp_path, mode):
    gate = asyncio.Event()
    agent = make_agent(tmp_path, [[gate]])
    agent.max_iterations = 1
    task = asyncio.create_task(run(agent, mode))
    while not agent.client.requests:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.supervision.entries[KEY].state == 'pending'
    assert agent.supervision.turn == 0 and agent.supervision.closed


@pytest.mark.asyncio
async def test_direct_service_delivers_builtin_warning(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from test_supervision import Client

    from mewcode.config import ProviderConfig, ServiceConfig
    from mewcode.service.execution import HeadlessAgentRunner

    monkeypatch.setattr('mewcode.client.create_client', lambda _: Client([done()]))
    runner = HeadlessAgentRunner(ServiceConfig(enable_supervision=True),
                                ProviderConfig('fake', 'anthropic', 'http://invalid', 'fake'))
    agent = runner._build_agent(str(tmp_path))
    agent.max_iterations = 1

    async def build(work_dir, callback):
        return agent, None

    runner._build_agent_with_tools = build
    events = []
    try:
        await runner._run_direct_body(SimpleNamespace(tokens_in=0, tokens_out=0), str(tmp_path),
                                      'task', events.append, 0)
        report = next(e['report'] for e in events if e['type'] == 'supervision')
        assert report['entries'][KEY]['state'] == 'delivered'
        assert KEY in request_text(agent.client.requests[0])
    finally:
        await agent.cancel_background_tasks()
