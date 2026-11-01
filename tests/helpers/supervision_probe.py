"""Explicit offline live-container supervision probe; never loaded by runtime."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mewcode.agent import Agent
from mewcode.client import LLMClient
from mewcode.config import load_config
from mewcode.supervision import Advisory, Expectation
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete


class FakeClient(LLMClient):
    calls = 0
    first_request = ''

    async def stream(self, conversation, system='', tools=None):
        self.calls += 1
        text = system + str(conversation.transient_reminders)
        if self.calls == 1:
            self.first_request = text
            yield ToolCallComplete('check', 'Bash', {'command': 'exit 1'})
            yield StreamEnd('tool_use')
        else:
            assert 'live-advice' not in text
            yield TextDelta('done')
            yield StreamEnd('end_turn')


class VerifyClient(LLMClient):
    calls = 0

    async def stream(self, conversation, system='', tools=None):
        self.calls += 1
        text = system + str(conversation.transient_reminders)
        if self.calls == 1:
            yield ToolCallComplete('edit', 'WriteFile', {'file_path': 'probe.py', 'content': 'x = 1\n'})
        elif self.calls == 2:
            assert 'builtin:self-verify' in text
            # Installed pytest is not required: even command failure is an attempt.
            yield ToolCallComplete('verify', 'Bash', {'command': 'python -m pytest --mewcode-invalid-option'})
        else:
            assert 'builtin:self-verify' not in text
            yield TextDelta('done')
            yield StreamEnd('end_turn')
            return
        yield StreamEnd('tool_use')


async def main():
    config = load_config(Path('/workspace/supervision.yaml'))
    client = FakeClient()
    agent = Agent(client, create_default_registry().bind_work_dir('/workspace'), 'anthropic',
                  work_dir='/workspace', enable_supervision=config.enable_supervision)
    bus = agent.supervision
    bus.publish(Advisory('attempt', 'live-advice-attempt', channel='system',
                         expect=Expectation('tool_call', 'Bash', 1)))
    bus.publish(Advisory('success', 'live-advice-success',
                         expect=Expectation('tool_success', 'Bash', 1)))
    bus.publish(Advisory('status', 'live-status-only', channel='status'))
    await agent.run_to_completion('Explicit offline probe')
    assert 'live-advice-attempt' in client.first_request and 'live-advice-success' in client.first_request
    assert 'live-status-only' not in client.first_request
    assert bus.entries['attempt'].state == 'adopted'
    assert bus.entries['success'].state == 'ignored'
    assert bus.entries['status'].delivered_turn is None
    assert bus.turn == 2
    # A new run rearms the built-in detector inside the actual container Agent.
    agent.max_iterations = 1
    await agent.run_to_completion('Budget detector probe')
    assert bus.entries['builtin:iteration-budget'].state == 'delivered'
    assert bus.turn == 1 and bus.attempt == 1
    verify_client = VerifyClient()
    agent.client = verify_client
    agent.max_iterations = 5
    await agent.run_to_completion('Self-verify probe')
    assert verify_client.calls == 3
    assert bus.entries['builtin:self-verify'].state == 'adopted'
    assert bus.self_verify.revision == bus.self_verify.attempted_revision == 1
    assert bus.self_verify.successful_revision == 0
    assert bus.self_verify.report()['coverage'] == 'unknown'
    print(json.dumps({'probe': 'ok', 'report': bus.report()}))


if __name__ == '__main__':
    asyncio.run(main())
