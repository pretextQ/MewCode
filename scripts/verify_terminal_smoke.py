"""Drive real OS terminal drivers through ConPTY / POSIX PTY, without a GUI.

Optional runner dependencies: pyte, plus pywinpty on Windows. The child app
uses repository dependencies and an offline fixture; no model or tools run.
This checks terminal protocols, not desktop fonts, selection or visual repaint.
"""
import argparse
import codecs
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pyte

parser = argparse.ArgumentParser()
parser.add_argument('--platform', choices=['windows', 'linux'], required=True)
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--crash', action='store_true', help='Windows: check outer launcher after abrupt fixture exit')
args = parser.parse_args()
if args.crash and args.platform != 'windows':
    parser.error('--crash requires Windows and its production outer launcher')
args.root = args.root.resolve()
project = Path(__file__).resolve().parents[1]
out = args.root / args.platform
out.mkdir(parents=True, exist_ok=True)
event_path = out / 'events.jsonl'
if event_path.exists():
    archive = out / 'attempts' / str(time.time_ns())
    archive.mkdir(parents=True)
    for item in out.iterdir():
        if item.is_file():
            item.rename(archive / item.name)
if args.platform == 'windows':
    shim = out / 'shim'
    shim.mkdir(exist_ok=True)
    (shim / 'uv.cmd').write_text(
        f'@echo off\n"{sys.executable}" -m tests.helpers.tui_terminal_smoke --evidence "{out}" '
        f'2> "{out / "stderr.log"}"\nexit /b %errorlevel%\n', encoding='utf-8',
    )
    launcher = str(project / 'scripts' / 'run_tui.ps1').replace("'", "''")
    (out / 'launch.ps1').write_text('''$ErrorActionPreference = 'Stop'
$env:PATH = "$PSScriptRoot\\shim;$env:PATH"
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Write-Host 'MEWCODE_SMOKE_SCROLLBACK_BEFORE: ASCII sentinel before app'
. '__LAUNCHER__'
[uint32]$finalInputMode = 0
[uint32]$finalOutputMode = 0
$inputOk = [MewCode.TerminalModes]::GetConsoleMode($inputHandle, [ref]$finalInputMode)
$outputOk = [MewCode.TerminalModes]::GetConsoleMode($outputHandle, [ref]$finalOutputMode)
@{
    input_before = $inputMode; input_after = $finalInputMode
    output_before = $outputMode; output_after = $finalOutputMode
    input_ok = $inputOk; output_ok = $outputOk
    child_exit = $LASTEXITCODE
} | ConvertTo-Json | Set-Content -Encoding utf8 "$PSScriptRoot\\console-modes.json"
Write-Host 'MEWCODE_SMOKE_RECOVERY: enter a marker (plain input, no shell command).'
$line = [Console]::ReadLine()
Set-Content -Encoding utf8 -LiteralPath "$PSScriptRoot\\recovery-input.txt" -Value $line
Write-Host "MEWCODE_SMOKE_RECOVERED: $line"
'''.replace('__LAUNCHER__', launcher), encoding='utf-8-sig')
screen = pyte.HistoryScreen(120, 36, history=3000)
stream = pyte.Stream(screen)
lock = threading.Lock()
raw = []
checks = []

if args.platform == 'windows':
    from winpty import PtyProcess
    child = PtyProcess.spawn([
        'powershell.exe', '-NoLogo', '-NoProfile', '-File',
        str(out / 'launch.ps1'),
    ], cwd=str(project), dimensions=(36, 120), env={**os.environ, 'TERM': 'xterm-256color'})
    write = child.write
    resize = child.setwinsize
    alive = child.isalive

    def read():
        return child.read(65536)
else:
    import fcntl
    import pty
    import struct
    import termios
    pid, fd = pty.fork()
    if pid == 0:
        before = termios.tcgetattr(0)
        print('MEWCODE_SMOKE_SCROLLBACK_BEFORE: 中文 English ＡＢＣ', flush=True)
        import subprocess
        result = subprocess.run([
            sys.executable, '-m', 'tests.helpers.tui_terminal_smoke', '--evidence', str(out),
        ], cwd=str(project), env={**os.environ, 'TERM': 'xterm-256color'})
        after = termios.tcgetattr(0)
        (out / 'termios.json').write_text(json.dumps({'before': str(before), 'after': str(after),
                                                      'equal': before == after, 'returncode': result.returncode}))
        print('\nMEWCODE_SMOKE_RECOVERY: type recovery-中文 then Enter', flush=True)
        recovered = input()
        (out / 'recovery-input.txt').write_text(recovered, encoding='utf-8')
        print('MEWCODE_SMOKE_RECOVERED: ' + recovered, flush=True)
        os._exit(0)
    decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def read():
        return decoder.decode(os.read(fd, 65536))

    def write(text):
        os.write(fd, text.encode('utf-8'))

    def resize(rows, cols):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def alive():
        return os.waitpid(pid, os.WNOHANG) == (0, 0)

    resize(36, 120)


screen.write_process_input = lambda data: write(data)


def reader():
    with (out / 'terminal.raw').open('w', encoding='utf-8') as handle:
        while True:
            try:
                data = read()
                if not data:
                    break
                with lock:
                    raw.append(data)
                    stream.feed(data)
                    handle.write(data)
                    handle.flush()
            except (EOFError, OSError):
                break


threading.Thread(target=reader, daemon=True).start()


def events():
    if not event_path.exists():
        return []
    rows = []
    for line in event_path.read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def state():
    return next((row for row in reversed(events()) if row['kind'] == 'state'), {})


def wait(predicate, label, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            checks.append({'check': label, 'passed': True})
            print('PASS ' + label, flush=True)
            return
        time.sleep(0.1)
    raise AssertionError(label + ': ' + json.dumps(state(), ensure_ascii=False))


def capture(name):
    time.sleep(0.35)
    with lock:
        (out / (name + '.txt')).write_text('\n'.join(screen.display), encoding='utf-8')
    (out / (name + '.json')).write_text(json.dumps(state(), ensure_ascii=False, indent=2), encoding='utf-8')


def send(text):
    write(text)
    time.sleep(0.15)


def submit(text):
    send(text)
    send('\r')
    wait(lambda: any(e['kind'] == 'input' and e['text'] == text for e in events()), 'input received: ' + text)


def verify_recovery():
    wait(lambda: 'MEWCODE_SMOKE_RECOVERY' in ''.join(raw), 'parent terminal resumes')
    send('recovery-中文\r')
    wait(lambda: (out / 'recovery-input.txt').exists(), 'post-exit cooked input and echo work')
    assert (out / 'recovery-input.txt').read_text(encoding='utf-8-sig').strip() == 'recovery-中文'
    wait(lambda: 'MEWCODE_SMOKE_RECOVERED' in ''.join(raw), 'post-exit echo reaches terminal')
    output = ''.join(raw)
    assert output.rfind('\x1b[?1006l') > output.find('\x1b[?1006h')
    assert output.rfind('\x1b[?25h') > output.find('\x1b[?25l')
    if args.platform == 'windows':
        modes = json.loads((out / 'console-modes.json').read_text(encoding='utf-8-sig'))
        assert modes['input_ok'] and modes['output_ok']
        assert modes['input_before'] == modes['input_after']
        assert modes['output_before'] == modes['output_after']
        assert modes['child_exit'] == (7 if args.crash else 0)
    else:
        restored = json.loads((out / 'termios.json').read_text())
        assert restored['equal'] and restored['returncode'] == 0
    checks.append({'check': 'console modes / termios restored; mouse disabled; cursor restored', 'passed': True})
    capture('09-recovered')


try:
    wait(lambda: state().get('max_scroll_y', 0) > 20, 'real OS driver mounted with long history')
    capture('01-initial')
    if args.crash:
        submit('crash')
        verify_recovery()
        raise SystemExit(0)
    with lock:
        assert 'MEWCODE_SMOKE_SCROLLBACK_BEFORE' in ''.join(raw)
        assert '\x1b[?1049h' not in ''.join(raw)
    checks.append({'check': 'primary screen: preceding output retained; no alternate-screen entry', 'passed': True})
    # Actual terminal input parser receives SGR mouse wheel reports.
    send('\x1b[<64;30;10M' * 10)
    wait(lambda: state().get('scroll_y', 0) < state().get('max_scroll_y', 0) - 2, 'SGR mouse wheel scrolls history')
    previous = state()['scroll_y']
    time.sleep(1)
    assert abs(state()['scroll_y'] - previous) < 1
    checks.append({'check': 'history remains detached while idle', 'passed': True})
    capture('02-mouse-scroll')
    send('\x1b[1;5F')
    wait(lambda: state().get('scroll_y') == state().get('max_scroll_y'), 'Ctrl+End returns to latest')
    send('\x1bOQ')
    wait(lambda: state().get('details') is True, 'F2 opens details')
    for cols, rows in [(80, 24), (60, 18), (160, 48), (120, 36)]:
        with lock:
            screen.resize(lines=rows, columns=cols)
        resize(rows, cols)
        wait(lambda cols=cols, rows=rows: state().get('size') == [cols, rows], f'OS resize delivered {cols}x{rows}')
        time.sleep(0.5)
        current = state()
        x, y, width, height = current['input']
        assert x >= 0 and y >= 0 and x + width <= cols and y + height <= rows
        chat = current['chat']
        assert chat[1] + chat[3] <= y
        checks.append({'check': f'input visible and chat non-overlapping {cols}x{rows}', 'passed': True})
        capture(f'03-resize-{cols}x{rows}')
    send('\x1b')
    wait(lambda: state().get('details') is False, 'Esc closes details')
    sample = '中文 English 全角ＡＢＣ /workspace/含 空格/[demo]/parser.py'
    submit(sample)
    wait(lambda: state().get('phase') == '本次运行结束', 'mixed-width input and response finish')
    capture('04-cjk-long-path')
    submit('permission')
    wait(lambda: state().get('phase') == '等待权限', 'permission prompt receives focus')
    capture('05-permission')
    send('\x1b')
    wait(lambda: any(e['kind'] == 'permission' for e in events()), 'Esc resolves permission without executing')
    wait(lambda: state().get('phase') == '本次运行结束', 'input restored after permission')
    submit('failure')
    wait(lambda: state().get('phase') == '出错', 'failure is shown without retry')
    capture('06-failure')
    submit('stream')
    wait(lambda: state().get('phase') == '接收回复', 'live streaming begins')
    send('\x1b[<64;30;10M' * 10)
    time.sleep(1)
    previous = state()['scroll_y']
    time.sleep(1.2)
    assert abs(state()['scroll_y'] - previous) < 1, 'stream pulled reader to bottom'
    checks.append({'check': 'stream does not pull detached history reader', 'passed': True})
    capture('07-stream-detached')
    send('\x03')
    wait(lambda: state().get('phase') == '已停止', 'first Ctrl+C stops active run')
    capture('08-cancelled')
    send('\x03')
    wait(lambda: any(e['kind'] == 'exited' for e in events()), 'idle Ctrl+C exits application')
    verify_recovery()
except Exception as error:
    checks.append({'check': 'run completed', 'passed': False, 'error': repr(error)})
    capture('failure-diagnostic')
    raise
finally:
    (out / 'checks.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.platform == 'windows':
        if child.isalive():
            child.terminate(force=True)
    else:
        try:
            if alive():
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
        except ChildProcessError:
            pass
