"""Successful edit output and actual file/cache/history boundaries in temporary projects."""
from __future__ import annotations

import pytest

from mewcode.cache import FileCache
from mewcode.filehistory import FileHistory
from mewcode.tools.edit_diff import MAX_DIFF_CHARS, MAX_DIFF_LINES, edit_summary
from mewcode.tools.edit_file import EditFile, Params
from mewcode.tools.file_state_cache import FileStateCache
from mewcode.tools.read_file import Params as ReadParams
from mewcode.tools.read_file import ReadFile
from mewcode.tui.tools import ToolCallBlock, _format_detail


@pytest.mark.parametrize('before,after,counts', [
    ('', '中文\n', '+1 additions, -0 removals'),
    ('x\n', '', '+0 additions, -1 removals'),
    ('x', 'x\n', '+1 additions, -1 removals'),
    ('a\r\nb\nc\r\n', 'a\r\nB\nc\r\n', '+1 additions, -1 removals'),
    ('same\n', 'same\n', '+0 additions, -0 removals'),
], ids=['insert', 'delete', 'newline', 'mixed', 'identical'])
def test_counts_and_newline(before, after, counts):
    output = edit_summary('file', before, after)
    assert counts in output
    if before == 'x':
        assert '\\ No newline at end of file' in output


def test_separated_hunks_are_not_counted_as_one_replacement():
    before = ''.join(f'line {i}\n' for i in range(70))
    after = before.replace('line 2\n', 'new 2\n').replace('line 60\n', 'new 60\n')
    output = edit_summary('file', before, after)
    assert '+2 additions, -2 removals' in output
    assert output.count('@@ -') == 2
    assert 'line 30' not in output


@pytest.mark.parametrize('before,after', [
    (''.join(f'old {i}\n' for i in range(350)), ''.join(f'new {i}\n' for i in range(350))),
    ('x' * 30000, 'y' * 30000),
], ids=['many-lines', 'long-line'])
def test_preview_is_bounded_without_truncating_counts(before, after):
    output = edit_summary('file', before, after)
    assert 'Diff preview truncated' in output
    assert 'statistics omitted' not in output
    assert len(output) <= MAX_DIFF_CHARS
    assert len(output.splitlines()) <= MAX_DIFF_LINES


def test_comparison_budget_avoids_pathological_diff(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('must not compare')
    monkeypatch.setattr('mewcode.tools.edit_diff.SequenceMatcher', fail)
    for before in ['x' * 1_000_001, 'x\n' * 2000]:
        assert 'statistics omitted' in edit_summary('file', before, before + 'new')


@pytest.mark.asyncio
@pytest.mark.parametrize('raw,old,new,expected', [
    (b'a\r\nb\nc\r\n', 'b\n', 'B\n', b'a\r\nB\r\nc\r\n'),
    (b'\xef\xbb\xbfa\n', 'a', '中文', b'\xef\xbb\xbf\xe4\xb8\xad\xe6\x96\x87\n'),
    (b'a\nb\n', 'a\nb\n', '', b''),
], ids=['mixed', 'bom-unicode', 'delete-all'])
async def test_actual_edit_bytes_state_cache_and_undo(tmp_path, raw, old, new, expected):
    path = tmp_path / 'sample.txt'
    path.write_bytes(raw)
    cache, state = FileCache(), FileStateCache()
    history = FileHistory(str(tmp_path), session_id='diff-test')
    history.make_snapshot(0, 'before edit')
    read = ReadFile(file_cache=cache, file_state_cache=state).bind(str(tmp_path))
    edit = EditFile(cache, history, state).bind(str(tmp_path))
    params = Params(file_path='sample.txt', old_string=old, new_string=new)
    assert (await edit.execute(params)).is_error  # read-before-write remains enforced
    assert path.read_bytes() == raw
    assert not (await read.execute(ReadParams(file_path='sample.txt'))).is_error
    result = await edit.execute(params)
    assert not result.is_error and 'additions' in result.output
    assert path.read_bytes() == expected
    assert cache.get(str(path.resolve())) is None
    assert state.check(str(path.resolve()))[0]
    assert history.rewind(0) == [str(path.resolve())]
    assert path.read_bytes() == raw


@pytest.mark.asyncio
async def test_preview_failure_is_still_success_and_write_failure_has_no_diff(tmp_path, monkeypatch):
    path = tmp_path / 'sample.txt'
    path.write_text('old')
    edit = EditFile().bind(str(tmp_path))
    params = Params(file_path='sample.txt', old_string='old', new_string='new')
    def fail(*args, **kwargs):
        raise OSError('probe')
    monkeypatch.setattr('mewcode.tools.edit_diff.edit_summary', fail)
    result = await edit.execute(params)
    assert not result.is_error and 'edit completed' in result.output
    assert path.read_text() == 'new'
    monkeypatch.setattr('mewcode.tools.base.write_text_preserve', fail)
    result = await edit.execute(Params(file_path='sample.txt', old_string='new', new_string='bad'))
    assert result.is_error and 'Successfully' not in result.output
    assert path.read_text() == 'new'


def test_live_and_history_use_same_plain_safe_detail():
    output = edit_summary('file', 'old\n', '[red]x[/red]\x1b[2J\x00\x9b\u202e\n')
    live, history = ToolCallBlock('EditFile', {}), ToolCallBlock('EditFile', {})
    live.set_result(output, False, 1)
    history.set_history(output)
    assert live._full_output == history._full_output
    detail = _format_detail('EditFile', {}, live._full_output)
    assert '[red]x[/red]' in detail and '\x1b' not in detail and '\x00' not in detail
    assert '\x9b' not in detail and '\u202e' not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['interactive', 'headless'])
async def test_actual_agent_returns_diff_in_next_request(tmp_path, mode):
    from test_self_verify import agent_for, calls
    from test_supervision import done, request_text, run

    (tmp_path / 'app.py').write_text('x = 1\n')
    agent = agent_for(tmp_path, [calls(('ReadFile', {'file_path': 'app.py'})),
                               calls(('EditFile', {'file_path': 'app.py', 'old_string': '1', 'new_string': '2'})),
                               done()])
    await run(agent, mode)
    payload = request_text(agent.client.requests[2])
    assert '+1 additions, -1 removals' in payload and '@@ -1,1 +1,1 @@' in payload
    assert agent.supervision.self_verify.revision == 1
    assert (tmp_path / 'app.py').read_text() == 'x = 2\n'
