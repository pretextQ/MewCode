from __future__ import annotations

import asyncio

import pytest
from textual.events import Paste
from textual.widgets import Input

from mewcode.agent import LoopComplete, PermissionRequest, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.permission_dialog import InlinePermissionWidget
from mewcode.permissions.replies import MAX_FEEDBACK_CHARS, PermissionReply, PermissionResponse, normalize_reply
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from tests.helpers.tui_offline import OfflineApp
from tests.test_permission_security import MockLLMClient, _make_agent
from tests.test_request_pairing import assert_wire_pairs, capture


@pytest.mark.parametrize('response', list(PermissionResponse))
def test_normalization_legacy_and_draft_isolation(response):
    assert normalize_reply(response) == PermissionReply(response)
    reply=normalize_reply(PermissionReply(response, '\x1b[31m[red]\x00\u202e' + '中' * 2000))
    if response == PermissionResponse.DENY:
        assert len(reply.feedback) == MAX_FEEDBACK_CHARS and reply.feedback.startswith('[red]')
        assert '\x1b' not in reply.feedback and '\x00' not in reply.feedback and '\u202e' not in reply.feedback
    else:
        assert not reply.feedback


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['anthropic', 'openai', 'openai-compat'])
async def test_actual_denial_reaches_next_request_and_sdk(tmp_path, protocol):
    target=tmp_path / 'source.py'
    target.write_text('original')
    payloads=[]
    class CaptureNext(MockLLMClient):
        async def stream(self, conversation, system='', tools=None):
            if self._call_index == 1:
                payloads.append(await capture(protocol, conversation))
            async for event in super().stream(conversation, system, tools):
                yield event
    client=CaptureNext([[ToolCallComplete('denied-id', 'WriteFile', {'file_path': str(target), 'content':'changed'}),
                           StreamEnd('tool_use')], [TextDelta('understood'), StreamEnd('end_turn')]])
    agent=_make_agent(tmp_path,client)
    agent.registry=agent.registry.bind_work_dir(str(tmp_path))
    conv=ConversationManager()
    conv.add_user_message('edit')
    events=[]
    async for event in agent.run(conv):
        events.append(event)
        if isinstance(event, PermissionRequest):
            event.future.set_result(PermissionReply(PermissionResponse.DENY, '请保存到 backup [red]'))
    assert target.read_text() == 'original' and client._call_index == 2
    result=next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.tool_id == 'denied-id' and result.is_error and '请保存到 backup [red]' in result.output
    assert sum(m.content == 'edit' for m in conv.history) == 1
    assert len(payloads) == 1
    payload=payloads[0]
    assert_wire_pairs(protocol,payload)
    assert str(payload).count('请保存到 backup [red]') == 1


@pytest.mark.asyncio
async def test_parallel_asks_never_share_feedback(tmp_path):
    from mewcode.tools.write_file import WriteFile
    class ParallelWrite(WriteFile):
        is_concurrency_safe=True
    calls=[ToolCallComplete(str(i),'WriteFile',{'file_path':str(tmp_path / str(i)), 'content':'no'}) for i in range(2)]
    agent=_make_agent(tmp_path,MockLLMClient([[*calls,StreamEnd('tool_use')]]))
    agent.registry.register(ParallelWrite().bind(str(tmp_path)))
    conv=ConversationManager()
    conv.add_user_message('edit')
    index=0
    results=[]
    async for event in agent.run(conv):
        if isinstance(event, PermissionRequest):
            event.future.set_result(PermissionReply(PermissionResponse.DENY, 'only first' if index == 0 else ''))
            index+=1
        if isinstance(event, ToolResultEvent):
            results.append(event)
    assert index == 2 and [r.tool_id for r in results] == ['0','1']
    assert 'only first' in results[0].output and 'only first' not in results[1].output
    assert not (tmp_path / '0').exists() and not (tmp_path / '1').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['enter','escape','ctrl+c','switch'])
@pytest.mark.parametrize('size', [(80,24), (60,18)])
async def test_real_inline_feedback_input_paste_and_cancellation(tmp_path, ending, size):
    app=OfflineApp(str(tmp_path))
    future=asyncio.get_running_loop().create_future()
    async def run(_):
        yield PermissionRequest('Bash','offline request',future)
        await future
        yield LoopComplete(1)
    async with app.run_test(size=size) as pilot:
        app.agent.run=run
        task=asyncio.create_task(app._send_message('go'))
        for _ in range(100):
            await pilot.pause(0.01)
            if app.query('#perm-inline'):
                break
        widget=app.query_one(InlinePermissionWidget)
        await pilot.press('down','down')
        field=widget.query_one(Input)
        await pilot.pause()
        assert field.display and app.focused is field
        assert field.region in app.screen.region
        field.post_message(Paste('feedback [red] ' + 'x' * 1500))
        await pilot.pause()
        assert len(field.value) <= MAX_FEEDBACK_CHARS
        if ending == 'switch':
            await pilot.press('up')
            assert not field.value and not field.display
            await pilot.press('down','enter')
        else:
            await pilot.press(ending)
        await task
        if ending == 'ctrl+c':
            assert future.cancelled() or future.done()
        else:
            reply=normalize_reply(future.result())
            assert reply.response == PermissionResponse.DENY
            assert bool(reply.feedback) == (ending == 'enter')
            if ending == 'enter':
                assert '[red]' in reply.feedback
            widget.action_deny()  # A late duplicate cannot change the completed reply.
            assert normalize_reply(future.result()) == reply
        assert app.query_one('#chat-input').disabled is False
        assert not app.query('#perm-inline')



@pytest.mark.asyncio
async def test_structured_always_allow_persists_without_draft(tmp_path):
    target=tmp_path / 'normal.txt'
    client=MockLLMClient([[ToolCallComplete('allowed','WriteFile',{'file_path':str(target), 'content':'allowed'}),StreamEnd('tool_use')]])
    agent=_make_agent(tmp_path,client)
    agent.registry=agent.registry.bind_work_dir(str(tmp_path))
    local=tmp_path / '.mewcode/permissions.local.yaml'
    agent.permission_checker.rule_engine._local_path=local
    conv=ConversationManager()
    conv.add_user_message('write')
    async for event in agent.run(conv):
        if isinstance(event, PermissionRequest):
            event.future.set_result(PermissionReply(PermissionResponse.ALLOW_ALWAYS,'discarded draft'))
    assert target.read_text() == 'allowed' and local.exists()
    assert agent.permission_checker.rule_engine.evaluate('WriteFile',str(target)) == 'allow'
    assert 'discarded draft' not in str(conv.history)


@pytest.mark.asyncio
async def test_noninteractive_never_invents_feedback(tmp_path):
    target=tmp_path / 'denied.txt'
    agent=_make_agent(tmp_path,MockLLMClient([[ToolCallComplete('no-ui','WriteFile',{'file_path':str(target),'content':'denied'}),StreamEnd('tool_use')]]))
    agent.registry=agent.registry.bind_work_dir(str(tmp_path))
    conv=ConversationManager()
    await agent.run_to_completion('write',conv)
    results=[result for message in conv.history for result in message.tool_results]
    assert results[0].is_error and 'non-interactive' in results[0].content
    assert 'User feedback:' not in results[0].content and not target.exists()


@pytest.mark.asyncio
async def test_late_cancelled_dialog_reply_cannot_answer_next_request(tmp_path):
    app=OfflineApp(str(tmp_path))
    loop=asyncio.get_running_loop()
    old=PermissionRequest('Bash','old',loop.create_future())
    new=PermissionRequest('Bash','new',loop.create_future())
    async with app.run_test() as pilot:
        await app._handle_permission_request(old)
        widget=app.query_one(InlinePermissionWidget)
        old.future.cancel()
        await widget.remove()
        await app._handle_permission_request(new)
        current=app.query_one(InlinePermissionWidget)
        app.on_inline_permission_widget_responded(InlinePermissionWidget.Responded(
            PermissionReply(PermissionResponse.DENY,'stale feedback'), old.future))
        assert not new.future.done() and app.query_one(InlinePermissionWidget) is current
        assert app.query_one('#chat-input').disabled
        current.action_deny()
        await pilot.pause()
        assert normalize_reply(new.future.result()) == PermissionReply(PermissionResponse.DENY)
        assert not app.query_one('#chat-input').disabled
