"""Best-effort process diagnostics. Never persist request bodies or config objects."""
from __future__ import annotations

import asyncio
import atexit
import faulthandler
import logging
import os
import re
import sys
import threading
import traceback
import uuid
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mewcode.diagnostic_storage import lock_lease, prune, trim_fault

MAX_LOG_BYTES = 2 * 1024 * 1024
_session: Diagnostics | None = None


def redact(text: str) -> str:
    text = re.sub(r'(?i)\bBearer\s+[^\s,\'"}]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'''(?ix)(\b(?:api[_-]?key|password|token|authorization)["']?\s*[:=]\s*["']?)[^\s,'"}]+''',
                  r'\1[REDACTED]', text)
    text = re.sub(r'\bsk-[a-zA-Z0-9_-]{8,}', '[REDACTED]', text)
    return re.sub(r'://[^/@\s]+:[^/@\s]+@', '://[REDACTED]@', text).replace('\x1b', '?')


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = redact(super().format(record))
        return text[:32000] + (' [log record truncated]' if len(text) > 32000 else '')


class QuietHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        pass


def _handler(path: Path) -> QuietHandler:
    handler = QuietHandler(path, mode='a', maxBytes=MAX_LOG_BYTES, backupCount=3, encoding='utf-8')
    handler.setFormatter(RedactingFormatter('%(asctime)s pid=%(process)d %(name)s %(message)s'))
    return handler


class Diagnostics:
    def __init__(self, log_path: Path) -> None:
        self.directory: Path | None = None
        self._fault: Any = None
        self._lease: Any = None
        self._logger = logging.getLogger('mewcode.process-diagnostics')
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self._seen: deque[BaseException] = deque(maxlen=32)
        self._finished = False
        self._closed = False
        self.session_id = uuid.uuid4().hex
        try:
            log_path = log_path.expanduser().absolute()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logging.getLogger().addHandler(_handler(log_path))
            logging.getLogger().setLevel(logging.INFO)
            root = log_path.with_name(log_path.name + '.diagnostics')
            self.directory = root / f'{os.getpid()}-{self.session_id}'
            self.directory.mkdir(parents=True)
            self._lease = (self.directory / '.lease').open('w+b')
            self._lease.write(b'1')
            self._lease.flush()
            lock_lease(self._lease)
            self._logger.addHandler(_handler(self.directory / 'events.log'))
            self._fault = (self.directory / 'fault.log').open('ab', buffering=0)
            faulthandler.enable(file=self._fault, all_threads=True)
            prune(root)
            self.write(f'start session={self.session_id}')
        except (OSError, ValueError, RuntimeError):
            self.write('diagnostics unavailable: storage/faulthandler setup failed')

    def write(self, message: str) -> None:
        try:
            self._logger.info(message)
        except Exception:
            pass

    def exception(self, context: str, error: BaseException) -> None:
        if any(previous is error for previous in self._seen):
            return
        self._seen.append(error)
        try:
            # No locals, request, conversation, or configuration dumps.
            stack = ''.join(traceback.format_tb(error.__traceback__, limit=30))[-16000:]
            message = redact(str(error))[:2000]
            self.write(f'exception context={context} type={type(error).__name__}\n{stack}{message}')
        except Exception:
            self.write(f'exception context={context} (formatting unavailable)')

    def finish(self, status: str = 'normal') -> None:
        if not self._finished:
            self.write(f'exit status={status}')
            self._finished = True

    def dump_traceback(self) -> None:
        if self._fault is not None and not self._closed:
            faulthandler.dump_traceback(file=self._fault, all_threads=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._finished:
            self.finish('atexit (exit code unknown)')
        try:
            if self._fault is not None:
                faulthandler.disable()
                self._fault.close()
            if self.directory is not None:
                trim_fault(self.directory / 'fault.log')
            if self._lease is not None:
                self._lease.close()
        except (OSError, ValueError):
            pass


def initialize(log_path: Path | None = None) -> Diagnostics:
    global _session
    if _session is not None:
        return _session
    _session = Diagnostics(log_path or Path(os.environ.get('MEWCODE_LOG_FILE') or '.mewcode/debug.log'))
    previous = sys.excepthook
    def exception_hook(kind: type[BaseException], error: BaseException, tb: Any) -> None:
        record_exception('unhandled', error)
        previous(kind, error, tb)
    sys.excepthook = exception_hook
    previous_thread = threading.excepthook
    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            record_exception('thread', args.exc_value)
        previous_thread(args)
    threading.excepthook = thread_hook
    atexit.register(_session.close)
    return _session


def record_exception(context: str, error: BaseException) -> None:
    if _session is not None:
        _session.exception(context, error)


def install_loop_handler() -> None:
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    if getattr(previous, '_mewcode_diagnostic', False):
        return
    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        error = context.get('exception')
        if isinstance(error, BaseException):
            record_exception('asyncio', error)
        if previous:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)
    handler._mewcode_diagnostic = True  # type: ignore[attr-defined]
    loop.set_exception_handler(handler)
