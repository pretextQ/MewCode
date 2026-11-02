from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def process(tmp_path, code, name='debug.log'):
    return subprocess.run([sys.executable, '-c', code], cwd=tmp_path, capture_output=True, text=True, timeout=20,
                          env={**os.environ, 'PYTHONPATH': str(ROOT), 'MEWCODE_LOG_FILE': str(tmp_path / name)})


def logs(tmp_path):
    return '\n'.join(p.read_text(encoding='utf-8', errors='replace') for p in tmp_path.rglob('*.log*') if p.is_file())


def test_two_actual_cli_starts_append_and_normal_exit(tmp_path):
    for marker in ['first marker', 'second marker']:
        result = process(tmp_path, f'''import sys,logging
from mewcode.diagnostics import initialize
from mewcode.__main__ import main
session = initialize()
assert initialize() is session
logging.getLogger('mewcode.probe').warning({marker!r})
sys.argv = ['mewcode', '--help']
main()
''')
        assert result.returncode == 0, result.stderr
    assert 'first marker' in (tmp_path / 'debug.log').read_text() and 'second marker' in logs(tmp_path)
    assert logs(tmp_path).count('start session=') == 2
    assert logs(tmp_path).count('exit status=normal') == 2
    assert len(list(tmp_path.glob('debug.log.diagnostics/*/fault.log'))) == 2


@pytest.mark.parametrize('exit_code', [1, 7])
def test_real_failure_preserves_status_and_redacts(tmp_path, exit_code):
    if exit_code == 1:
        body = "raise RuntimeError('api_key=secret-value token=hidden-value Authorization: Bearer fake-credential')"
    else:
        body = 'raise SystemExit(7)'
    result = process(tmp_path, f'''import mewcode.__main__ as main
exec("def failure():\\n    " + {body!r})
main._main = failure
main.main()
''')
    assert result.returncode == exit_code
    text = logs(tmp_path)
    assert 'exception context=cli' in text and 'failure' in text
    assert all(value not in text for value in ['secret-value', 'hidden-value', 'fake-credential'])
    assert text.count('exception context=') == 1  # sys.excepthook does not duplicate the same exception.


def test_real_fault_dump_rotation_and_loop_forwarding(tmp_path):
    result = process(tmp_path, '''import asyncio,logging,faulthandler
import mewcode.diagnostics as d
d.MAX_LOG_BYTES = 1024
session = d.initialize()
assert faulthandler.is_enabled()
session.dump_traceback()
for i in range(30):
    logging.getLogger('mewcode.probe').warning('retained %s %s', i, 'x' * 400)
session.dump_traceback()
async def probe():
    loop=asyncio.get_running_loop()
    handled=[]
    loop.set_exception_handler(lambda loop, context: handled.append(context))
    d.install_loop_handler()
    installed=loop.get_exception_handler()
    d.install_loop_handler()
    assert loop.get_exception_handler() is installed
    loop.call_exception_handler({'exception': RuntimeError('offline loop probe')})
    assert len(handled)==1
asyncio.run(probe())
session.finish()
''', name='redirected/service.log')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'redirected/service.log.1').exists()
    fault = next(tmp_path.glob('redirected/service.log.diagnostics/*/fault.log')).read_text()
    assert fault.count('dump_traceback') >= 2
    assert 'exception context=asyncio' in logs(tmp_path)
    assert not (tmp_path / '.mewcode').exists()


def test_storage_failure_cannot_change_exit_status(tmp_path):
    (tmp_path / 'blocked').write_text('not a directory')
    result = process(tmp_path, "import sys; from mewcode.__main__ import main; sys.argv=['mewcode','--help'];main()", 'blocked/debug.log')
    assert result.returncode == 0, result.stderr


def test_retention_skips_live_lease_and_trims_fault(tmp_path, monkeypatch):
    from mewcode import diagnostic_storage as storage
    monkeypatch.setattr(storage, 'KEEP_SESSIONS', 2)
    paths=[]
    for i in range(3):
        p=tmp_path / f'{i}-{i:032x}'
        p.mkdir()
        (p / '.lease').write_bytes(b'1')
        os.utime(p,(i+1,i+1))
        paths.append(p)
    with (paths[0] / '.lease').open('r+b') as lease:
        storage.lock_lease(lease)
        storage.prune(tmp_path)
        assert paths[0].exists()
    storage.prune(tmp_path)
    assert not paths[0].exists() and paths[1].exists() and paths[2].exists()
    fault=tmp_path / 'trace'
    fault.write_bytes(b'x' * (storage.MAX_FAULT_BYTES * 2))
    storage.trim_fault(fault)
    assert fault.stat().st_size < storage.MAX_FAULT_BYTES + 100


def test_real_textual_exception_entry(tmp_path):
    result = process(tmp_path, '''import asyncio
from mewcode.diagnostics import initialize
from tests.helpers.tui_offline import OfflineApp
session=initialize()
class Broken(OfflineApp):
    def on_mount(self):
        raise RuntimeError('offline textual probe')
async def probe():
    async with Broken('.').run_test():
        pass
try:
    asyncio.run(probe())
except RuntimeError:
    pass
session.finish()
''')
    assert result.returncode == 0, result.stderr
    assert 'exception context=textual' in logs(tmp_path) and 'offline textual probe' in logs(tmp_path)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory permissions')
def test_unwritable_directory(tmp_path):
    directory=tmp_path / 'readonly'
    directory.mkdir()
    directory.chmod(0o500)
    try:
        result=process(tmp_path, "import sys;from mewcode.__main__ import main;sys.argv=['mewcode','--help'];main()", 'readonly/log')
        assert result.returncode == 0
        if os.geteuid() != 0:
            assert not (directory / 'log').exists()
    finally:
        directory.chmod(0o700)


def test_real_thread_exception_is_recorded_without_altering_thread_behavior(tmp_path):
    result = process(tmp_path, '''import threading
from mewcode.diagnostics import initialize
session=initialize()
def probe():
    raise RuntimeError('offline thread probe')
thread=threading.Thread(target=probe)
thread.start()
thread.join()
session.finish()
''')
    assert result.returncode == 0 and 'offline thread probe' in result.stderr
    assert 'exception context=thread' in logs(tmp_path)


def test_real_concurrent_processes_keep_separate_sessions(tmp_path):
    import time
    code='''import os,time
from pathlib import Path
from mewcode.diagnostics import initialize
session=initialize()
Path(str(os.getpid())+'.ready').touch()
end=time.monotonic()+8
while not Path('release').exists() and time.monotonic()<end:
    time.sleep(0.01)
session.finish()
'''
    env={**os.environ, 'PYTHONPATH': str(ROOT), 'MEWCODE_LOG_FILE': str(tmp_path / 'parallel.log')}
    processes=[subprocess.Popen([sys.executable, '-c', code], cwd=tmp_path, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    try:
        end=time.monotonic()+5
        while len(list(tmp_path.glob('*.ready'))) < 2 and time.monotonic()<end:
            time.sleep(0.01)
        assert len(list(tmp_path.glob('parallel.log.diagnostics/*/events.log'))) == 2
    finally:
        (tmp_path / 'release').touch()
        for child in processes:
            child.communicate(timeout=10)
    assert all(child.returncode == 0 for child in processes)


def test_retention_never_follows_directory_links(tmp_path, monkeypatch):
    from mewcode import diagnostic_storage as storage
    root=tmp_path / 'logs'
    root.mkdir()
    outside=tmp_path / 'unrelated'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    alias=root / ('123-' + 'a' * 32)
    if os.name == 'nt':
        result=subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        alias.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(storage, 'KEEP_SESSIONS', 0)
    storage.prune(root)
    assert (outside / 'keep').read_text() == 'keep'


def test_real_outer_tui_send_failure_is_retained(tmp_path):
    result = process(tmp_path, '''import asyncio
from mewcode.diagnostics import initialize
from tests.helpers.tui_offline import OfflineApp
session=initialize()
async def failure(*args):
    raise RuntimeError('offline outer send probe')
async def probe():
    app=OfflineApp('.')
    async with app.run_test():
        app._run_message=failure
        await app._send_message('go')
asyncio.run(probe())
session.finish()
''')
    assert result.returncode == 0, result.stderr
    assert 'exception context=tui-run' in logs(tmp_path) and 'offline outer send probe' in logs(tmp_path)
