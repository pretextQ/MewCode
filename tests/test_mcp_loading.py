"""Loading modes through SDK payloads, real Agent gates and local stdio MCP."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import types
from pydantic import ValidationError

from mewcode.agent import Agent, PermissionRequest, ToolResultEvent
from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import apply_plan_readonly_filter, resolve_agent_tools
from mewcode.client import create_client
from mewcode.config import ConfigError, MCPServerConfig, ProviderConfig, ServiceConfig, _load_single_file, _merge_config
from mewcode.conversation import ConversationManager
from mewcode.mcp.bootstrap import close_mcp, register_mcp_tools
from mewcode.mcp.loading import configure_loading
from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.permissions.replies import PermissionReply, PermissionResponse
from mewcode.tools import ToolRegistry, create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from mewcode.tools.impl.tool_search import ToolSearchParams, ToolSearchTool
from tests.test_permission_security import MockLLMClient, _make_agent
from tests.test_request_pairing import Captured, assert_wire_pairs

SCHEMA = {'type': 'object', 'properties': {
    'query': {'type': 'string', 'description': 'query text'},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
    'required': ['query'], 'additionalProperties': False}


class LocalClient:
    is_alive = True

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(arguments))])


def tool(client, name='query_000', schema=SCHEMA, read_only=True, server='bench'):
    return MCPToolWrapper(server, types.Tool(name=name, description='Offline query ' + 'x' * 100,
                          inputSchema=schema, annotations=types.ToolAnnotations(readOnlyHint=read_only)), client)


def registry_for(tmp_path, count=1):
    client = LocalClient()
    registry = ToolRegistry().bind_work_dir(str(tmp_path))
    for i in range(count):
        registry.register(tool(client, f'query_{i:03}').bind(str(tmp_path)))
    registry.register(ToolSearchTool(registry))
    return registry, client


def serialized(registry):
    return json.dumps(registry.get_all_schemas(), ensure_ascii=False, separators=(',', ':')).encode()


def call(target='mcp_bench_query_000', args=None, identifier='call-id'):
    return ToolCallComplete(identifier, 'MCPCall', {'tool_name': target, 'arguments': args or {'query': 'hello'}})


async def run_agent(agent, interactive=False, reply=None):
    conv = ConversationManager()
    events = []
    if interactive:
        conv.add_user_message('offline task')
        async for event in agent.run(conv):
            events.append(event)
            if isinstance(event, PermissionRequest):
                event.future.set_result(reply or PermissionResponse.ALLOW)
    else:
        await agent.run_to_completion('offline task', conv)
    return conv, events


async def capture_tools(protocol, conversation, tools):
    client = create_client(ProviderConfig(name='offline', protocol=protocol, base_url='http://127.0.0.1:1',
                                          model='offline', api_key='offline'))
    payload = {}
    def intercept(**kwargs):
        payload.update(kwargs)
        raise Captured
    async def intercept_async(**kwargs):
        intercept(**kwargs)
    sdk = client._client
    endpoint = sdk.messages if protocol == 'anthropic' else sdk.responses if protocol == 'openai' else sdk.chat.completions
    try:
        with patch.object(endpoint, 'stream' if protocol == 'anthropic' else 'create',
                          side_effect=intercept if protocol == 'anthropic' else intercept_async):
            with pytest.raises(Captured):
                async for _ in client.stream(conversation, tools=tools):
                    pass
        return payload
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['anthropic', 'openai', 'openai-compat'])
@pytest.mark.parametrize('mode', ['legacy', 'eager', 'dispatch'])
async def test_three_clients_real_search_execution_and_request_stability(tmp_path, protocol, mode):
    registry, mcp = registry_for(tmp_path)
    configure_loading(registry, mode, protocol=protocol)
    registry.register(ToolSearchTool(registry, protocol))
    captured = []
    class CaptureClient(MockLLMClient):
        async def stream(self, conversation, system='', tools=None):
            captured.append(await capture_tools(protocol, conversation, tools))
            async for event in super().stream(conversation, system, tools):
                yield event
    invocation = call() if mode == 'dispatch' else ToolCallComplete('call-id', 'mcp_bench_query_000', {'query': 'hello'})
    client = CaptureClient([[ToolCallComplete('search-id', 'ToolSearch', {'query': 'select:mcp_bench_query_000'}),
                             StreamEnd('tool_use')], [invocation, StreamEnd('tool_use')],
                            [TextDelta('done'), StreamEnd('end_turn')]])
    agent = Agent(client, registry, protocol, work_dir=str(tmp_path), enable_supervision=True)
    conv, events = await run_agent(agent, True)
    assert mcp.calls == [('query_000', {'query': 'hello'})]
    schemas = [json.dumps(p['tools'], sort_keys=True) for p in captured]
    assert ('mcp_bench_query_000' in schemas[0]) == (mode == 'eager')
    if mode == 'legacy':
        assert schemas[0] != schemas[1] == schemas[2]
    else:
        assert schemas[0] == schemas[1] == schemas[2]
    assert SCHEMA['properties'] == registry.get('mcp_bench_query_000').get_schema()['input_schema']['properties']
    assert 'additionalProperties' in (schemas[0] if mode == 'eager' else str(conv.history))
    assert all('tool_reference' not in s for s in schemas)
    result = [e for e in events if isinstance(e, ToolResultEvent) and e.tool_id == 'call-id']
    assert len(result) == 1 and result[0].tool_name == 'mcp_bench_query_000'
    for payload in captured:
        assert_wire_pairs(protocol, payload)
        assert 'extra_headers' not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [0, 5, 250])
@pytest.mark.parametrize('mode', ['legacy', 'eager', 'dispatch'])
async def test_scale_and_empty_collection(tmp_path, count, mode):
    registry, _ = registry_for(tmp_path, count)
    choice = configure_loading(registry, mode)
    before = serialized(registry)
    await registry.get('ToolSearch').execute(ToolSearchParams(query='query', max_results=250))
    after = serialized(registry)
    assert (before == after) == (mode != 'legacy' or count == 0)
    assert choice.schema_bytes >= 2
    print(json.dumps({'count': count, 'mode': mode, 'schema_bytes': choice.schema_bytes,
                      'before': len(before), 'after': len(after)}))


def test_auto_threshold_is_fixed_and_explicit_legacy_fallback(tmp_path):
    registry, _ = registry_for(tmp_path)
    estimate = configure_loading(registry, 'legacy').estimated_tokens
    assert configure_loading(registry, 'auto', estimate * 10).selected == 'eager'
    assert configure_loading(registry, 'auto', (estimate - 1) * 10).selected == 'dispatch'
    registry.mark_discovered('mcp_bench_query_000')
    assert 'mcp_bench_query_000' not in str(registry.get_all_schemas())
    configure_loading(registry, 'legacy')
    assert registry.get('mcp_bench_query_000').should_defer
    assert not registry.is_enabled('MCPCall')


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['missing', 'ReadFile', 'MCPCall', 'mcp_bench_query_000'])
async def test_dispatch_rejects_unavailable_or_non_mcp_identity(tmp_path, target):
    registry, mcp = registry_for(tmp_path)
    registry.register(create_default_registry().get('ReadFile').bind(str(tmp_path)))
    configure_loading(registry, 'dispatch')
    registry.disable('mcp_bench_query_000')
    agent = Agent(MockLLMClient([[call(target), StreamEnd('tool_use')]]), registry, 'anthropic', work_dir=str(tmp_path))
    conv, _ = await run_agent(agent)
    assert 'dispatch rejected' in str(conv.history)
    assert not mcp.calls
    # Explicit selection must not leak disabled schemas either.
    result = await registry.get('ToolSearch').execute(ToolSearchParams(query='select:mcp_bench_query_000'))
    assert 'No matching' in result.output


@pytest.mark.asyncio
async def test_bound_filtered_registry_owns_search_and_dispatch(tmp_path):
    parent, mcp = registry_for(tmp_path, 2)
    configure_loading(parent, 'dispatch')
    parent.disable('mcp_bench_query_001')
    definition = AgentDef(agent_type='limited', when_to_use='', tools=['ToolSearch', 'MCPCall', 'mcp_bench_*'],
                          disallowed_tools=['mcp_bench_query_000'])
    child = resolve_agent_tools(parent, definition).bind_work_dir(str(tmp_path / 'child'))
    assert not child.is_enabled('mcp_bench_query_001')
    assert child.get('MCPCall')._registry is child and child.get('ToolSearch')._registry is child
    for restricted in [child, apply_plan_readonly_filter(parent)]:
        agent = Agent(MockLLMClient([[call(), StreamEnd('tool_use')]]), restricted, 'anthropic', work_dir=str(tmp_path))
        await run_agent(agent)
    assert not mcp.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('interactive', [True, False])
@pytest.mark.parametrize('decision', ['allow', 'deny', 'ask-deny', 'disabled'])
async def test_real_target_permissions_and_feedback(tmp_path, interactive, decision):
    registry, mcp = registry_for(tmp_path)
    configure_loading(registry, 'dispatch')
    registry.get('mcp_bench_query_000').category = 'command'
    rules = tmp_path / 'local_rules.yaml'
    if decision in {'allow', 'deny'}:
        rules.write_text(f"- rule: 'mcp_bench_query_000(*)'\n  effect: {decision}\n")
    agent = _make_agent(tmp_path, MockLLMClient([[call(), StreamEnd('tool_use')],
                                               [TextDelta('done'), StreamEnd('end_turn')]]),
                        enable_supervision=True)
    agent.registry = registry
    if decision == 'disabled':
        registry.disable('MCPCall')
    conv, events = await run_agent(agent, interactive, PermissionReply(PermissionResponse.DENY, 'feedback-only-target'))
    assert bool(mcp.calls) == (decision == 'allow')
    if decision == 'ask-deny' and interactive:
        assert 'feedback-only-target' in str(conv.history)
        requests = [e for e in events if isinstance(e, PermissionRequest)]
        assert len(requests) == 1 and requests[0].tool_name == 'mcp_bench_query_000'


@pytest.mark.parametrize('arguments', [{'query': 1}, {'query': 'a', 'limit': 0},
    {'query': 'a', 'limit': True}, {'query': 'a', 'extra': 'no'}, {}, {'query': 'a', 'limit': '2'}])
def test_schema_rejects_instead_of_coercing(arguments):
    with pytest.raises(ValidationError):
        tool(LocalClient()).params_model.model_validate(arguments)


@pytest.mark.asyncio
async def test_complete_nested_schema_null_and_additional_properties_preserved(tmp_path):
    schema = {'type': 'object', '$defs': {'item': {'type': 'integer', 'minimum': 2}},
              'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/item'}},
                             'optional': {'type': ['string', 'null']}}, 'required': ['items']}
    client = LocalClient()
    wrapper = tool(client, schema=schema)
    assert wrapper.get_schema()['input_schema'] == schema
    args = {'items': [2, 3], 'optional': None, 'extra': {'kept': True}, 'model_config': 'kept', '_validator': 'kept'}
    await wrapper.execute(wrapper.params_model.model_validate(args))
    assert client.calls == [('query_000', args)]
    for invalid in [{'items': '2,3'}, {'items': [1]}, {'items': ['2']}]:
        with pytest.raises(ValidationError):
            wrapper.params_model.model_validate(invalid)
    with pytest.raises(ValueError, match='external references'):
        tool(client, schema={'$ref': 'https://127.0.0.1:1/schema'})


@pytest.mark.asyncio
@pytest.mark.parametrize('interactive', [True, False])
@pytest.mark.parametrize('reject', [False, True])
async def test_real_target_hooks_and_supervision_exactly_once(tmp_path, monkeypatch, interactive, reject):
    from mewcode.hooks import Action, Hook, HookEngine
    from mewcode.hooks.conditions import Condition, ConditionGroup

    registry, mcp = registry_for(tmp_path)
    configure_loading(registry, 'dispatch')
    probe, log = tmp_path / 'hook.py', tmp_path / 'hooks.jsonl'
    probe.write_text('import sys\nfrom pathlib import Path\n'
                     f'with Path({str(log)!r}).open("a") as f: f.write(sys.stdin.read() + "\\n")\n'
                     f'sys.exit({1 if reject else 0})\n')
    action = Action(type='command', command=f'"{sys.executable}" "{probe}"')
    condition = ConditionGroup([Condition('tool', '==', 'mcp_bench_query_000')])
    hooks = HookEngine([Hook('pre', 'pre_tool_use', action, condition, reject=reject),
                        Hook('post', 'post_tool_use', action, condition)])
    agent = Agent(MockLLMClient([[call(), StreamEnd('tool_use')]]), registry, 'anthropic',
                  work_dir=str(tmp_path), hook_engine=hooks, enable_supervision=True)
    observed, original = [], agent.supervision.observe
    def observe(event, name):
        observed.append((event, name))
        original(event, name)
    monkeypatch.setattr(agent.supervision, 'observe', observe)
    conv, _ = await run_agent(agent, interactive)
    rows = [json.loads(row) for row in log.read_text().splitlines()]
    assert [row['event'] for row in rows] == (['pre_tool_use'] if reject else ['pre_tool_use', 'post_tool_use'])
    assert all(row['tool'] == 'mcp_bench_query_000' and row['args'] == {'query': 'hello'} for row in rows)
    assert mcp.calls == ([] if reject else [('query_000', {'query': 'hello'})])
    assert observed.count(('tool_success', 'mcp_bench_query_000')) == (0 if reject else 1)
    assert observed.count(('tool_call', 'mcp_bench_query_000')) == 1
    assert ('tool_call', 'MCPCall') not in observed
    assert ('tool_success', 'MCPCall') not in observed
    assert sum(r.tool_use_id == 'call-id' for m in conv.history for r in m.tool_results) == 1


@pytest.mark.asyncio
async def test_invalid_dispatch_arguments_never_reach_client(tmp_path):
    registry, mcp = registry_for(tmp_path)
    configure_loading(registry, 'dispatch')
    agent = Agent(MockLLMClient([[call(args={'query': 1}), StreamEnd('tool_use')]]), registry, 'anthropic',
                  work_dir=str(tmp_path))
    conv, _ = await run_agent(agent)
    assert 'MCP schema validation failed' in str(conv.history) and not mcp.calls


@pytest.mark.asyncio
async def test_server_identity_and_collision_fail_closed(tmp_path, monkeypatch):
    from mewcode.mcp.manager import MCPManager

    clients = {}
    class Managed(LocalClient):
        def __init__(self, config):
            super().__init__()
            self.name = config.name
            clients[self.name] = self
        async def connect(self):
            pass
        async def list_tools(self):
            name = 'b_query' if self.name == 'a' else 'query'
            return [types.Tool(name=name, inputSchema=SCHEMA, annotations=types.ToolAnnotations(readOnlyHint=True))]
        async def close(self):
            self.is_alive = False
    monkeypatch.setattr('mewcode.mcp.manager.MCPClient', Managed)
    manager = MCPManager()
    manager.load_configs([MCPServerConfig(name=name, command='unused') for name in ['one', 'two']])
    registry = ToolRegistry().bind_work_dir(str(tmp_path))
    try:
        assert not await manager.register_all_tools(registry)
        configure_loading(registry, 'dispatch')
        agent = Agent(MockLLMClient([[call('mcp_two_query'), StreamEnd('tool_use')]]), registry, 'anthropic',
                      work_dir=str(tmp_path))
        await run_agent(agent)
        assert not clients['one'].calls and clients['two'].calls == [('query', {'query': 'hello'})]
    finally:
        await manager.shutdown()
    manager = MCPManager()
    manager.load_configs([MCPServerConfig(name=name, command='unused') for name in ['a_b', 'a']])
    registry = ToolRegistry().bind_work_dir(str(tmp_path))
    try:
        errors = await manager.register_all_tools(registry)
        assert any('collision' in error for error in errors)
        assert not registry.is_enabled('mcp_a_b_query')
        registry.enable_all()
        assert not registry.is_enabled('mcp_a_b_query')
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_reconnect_capability_change_invalidates_target_and_visibility(tmp_path):
    class Reconnected(LocalClient):
        generation = 0
        async def connect(self):
            self.is_alive = True
        async def list_tools(self):
            return [types.Tool(name='query_000', inputSchema=SCHEMA,
                               annotations=types.ToolAnnotations(readOnlyHint=False))]
    client = Reconnected()
    registry = ToolRegistry()
    wrapper = tool(client).bind(str(tmp_path))
    registry.register(wrapper)
    configure_loading(registry, 'eager')
    client.generation = 1  # Another target may have already reconnected the shared client.
    bound = registry.bind_work_dir(str(tmp_path / 'child'))
    result = await bound.get(wrapper.name).execute(wrapper.params_model(query='hello'))
    assert result.is_error and 'capabilities changed' in result.output and not client.calls
    assert not registry.is_enabled(wrapper.name) and not bound.is_enabled(wrapper.name)
    assert wrapper.name not in serialized(registry).decode()
    assert wrapper.name not in serialized(bound).decode()


def test_explicit_config_defaults_overrides_and_container_payload(tmp_path):
    from mewcode.config import AppConfig
    from mewcode.service.sandbox import DockerSandbox

    provider = ProviderConfig('offline', 'anthropic', 'http://127.0.0.1:1', 'offline', 'fake', context_window=12345)
    base = AppConfig([provider], mcp_loading='dispatch', service=ServiceConfig(mcp_loading='eager'))
    path = tmp_path / 'config.yaml'
    provider_yaml = 'providers:\n  - name: offline\n    protocol: anthropic\n    base_url: http://127.0.0.1:1\n    model: offline\n'
    path.write_text(provider_yaml + 'mcp_loading: legacy\nservice:\n  mcp_loading: legacy\n')
    parsed = _load_single_file(path)
    assert _merge_config(base, parsed).mcp_loading == 'legacy'
    assert base.service.mcp_loading == 'legacy'
    for mode in [True, None, 'native', 'tool_reference', []]:
        path.write_text(provider_yaml + 'mcp_loading: ' + json.dumps(mode))
        with pytest.raises(ConfigError, match='mcp_loading'):
            _load_single_file(path)
    sandbox = DockerSandbox(ServiceConfig().sandbox, mewcode_src=str(tmp_path), work_root=str(tmp_path / 'scratch'))
    config = sandbox.write_container_config(path, provider, mcp_loading='dispatch')
    parsed = _load_single_file(config)
    assert parsed.mcp_loading == 'dispatch' and parsed.providers[0].context_window == 12345


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['headless', 'service', 'tui'])
@pytest.mark.parametrize('mode', ['legacy', 'dispatch'])
async def test_actual_startup_entry_selects_mode(tmp_path, monkeypatch, entry, mode):
    from unittest.mock import AsyncMock

    from mewcode.config import AppConfig
    from mewcode.permissions import PermissionMode

    monkeypatch.chdir(tmp_path)
    helper = Path(__file__).parent.resolve() / 'helpers' / 'mcp_loading_server.py'
    server = MCPServerConfig(name='local', command=sys.executable, args=[str(helper), str(tmp_path)])
    captures = []
    invocation = call('mcp_local_query', {'text': 'entry called'}) if mode == 'dispatch' else (
        ToolCallComplete('call-id', 'mcp_local_query', {'text': 'entry called'}))
    class EntryClient(MockLLMClient):
        async def stream(self, conversation, system='', tools=None):
            captures.append(tools)
            async for event in super().stream(conversation, system, tools):
                yield event
    client = EntryClient([[invocation, StreamEnd('tool_use')], [TextDelta('done'), StreamEnd('end_turn')]])
    provider = ProviderConfig('offline', 'anthropic', 'http://127.0.0.1:1', 'offline', 'fake')
    monkeypatch.setattr('mewcode.client.create_client', lambda _: client)
    monkeypatch.setattr('mewcode.client.resolve_context_window', AsyncMock())
    # Every actual entry gets a registry bound to the temporary project.
    factory = create_default_registry
    monkeypatch.setattr('mewcode.tools.create_default_registry', lambda *a, **kw: factory(*a, **kw).bind_work_dir(str(tmp_path)))
    if entry == 'headless':
        from mewcode.__main__ import _run_prompt
        await _run_prompt(AppConfig([provider], mcp_servers=[server], mcp_loading=mode), PermissionMode.DEFAULT, None, 'test')
    elif entry == 'service':
        from mewcode.service.execution import HeadlessAgentRunner
        runner = HeadlessAgentRunner(ServiceConfig(mcp_servers=[server], mcp_loading=mode), provider)
        agent, result = await runner._build_agent_with_tools(str(tmp_path), lambda _: None)
        try:
            await run_agent(agent)
        finally:
            await close_mcp(result)
    else:
        from mewcode.app import MewCodeApp
        monkeypatch.setattr('mewcode.app.create_client', lambda _: client)
        monkeypatch.setattr('mewcode.app.create_default_registry', lambda *a, **kw: factory(*a, **kw).bind_work_dir(str(tmp_path)))
        app = MewCodeApp([provider], mcp_servers=[server], mcp_loading=mode)
        async with app.run_test():
            await app._mcp_init_task
            assert '1 个服务 / 2 个工具' in app._mcp_server_info
            await run_agent(app.agent)
    names = [s['name'] for s in captures[0]]
    assert ('MCPCall' in names) == (mode == 'dispatch')
    assert ('mcp_local_query' in names) == (mode == 'legacy' and entry != 'tui')
    assert (tmp_path / 'calls.jsonl').exists()
    assert not (tmp_path / 'forbidden-write').exists()


@pytest.mark.asyncio
async def test_real_stdio_dispatch_readonly_filter_errors_cancel_and_cleanup(tmp_path):
    helper = Path(__file__).parent / 'helpers' / 'mcp_loading_server.py'
    registry = ToolRegistry().bind_work_dir(str(tmp_path))
    result = await register_mcp_tools(registry, [MCPServerConfig(name='local', command=sys.executable,
                                     args=[str(helper), str(tmp_path)])], loading_mode='dispatch')
    try:
        manager = result.manager
        assert result.tool_names == ['mcp_local_query']
        assert any('non-read-only' in error for error in result.errors)
        client = manager._clients['local']
        pids = {int((tmp_path / 'server.pid').read_text())}
        agent = Agent(MockLLMClient([[call('mcp_local_query', {'text': 'real stdio'}),
                                      call('mcp_local_write', {'text': 'forbidden'}, 'bad-id'),
                                      call('mcp_local_query', {'text': 'failure', 'fail': True}, 'failure-id'),
                                      StreamEnd('tool_use')]]), registry, 'anthropic', work_dir=str(tmp_path))
        conv, _ = await run_agent(agent)
        assert 'real stdio' in str(conv.history) and 'offline failure' in str(conv.history)
        assert not (tmp_path / 'forbidden-write').exists()
        client.mark_unhealthy()
        agent.client = MockLLMClient([[call('mcp_local_query', {'text': 'reconnected'}), StreamEnd('tool_use')]])
        await run_agent(agent)
        assert client.generation == 2
        pids.add(int((tmp_path / 'server.pid').read_text()))
        assert len(pids) == 2
        # The genuine SDK/stdio request is cancelled by a caller timeout.
        agent.client = MockLLMClient([[call('mcp_local_query', {'text': 'slow', 'delay': 20}), StreamEnd('tool_use')]])
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run_agent(agent), timeout=0.3)
        assert all(json.loads(row)['cwd'] == str(tmp_path) for row in (tmp_path / 'calls.jsonl').read_text().splitlines())
    finally:
        await close_mcp(result)
    assert not client.is_alive
    # psutil is deliberately optional; use the platform's read-only PID probe.
    for pid in pids:
        if os.name != 'nt':
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        else:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                code = ctypes.c_ulong()
                assert ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                ctypes.windll.kernel32.CloseHandle(handle)
                assert code.value != 259
