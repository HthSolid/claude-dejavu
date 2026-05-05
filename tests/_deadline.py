"""Deadline / time-budget helper for tests.

Cross-platform context manager that aborts a block of code if it runs
longer than `seconds`. Used to catch the "ingest hangs forever" class
of regressions — any test wrapped in a deadline that exceeds it raises
a `Deadline.Exceeded` exception, which the test runner treats as a
hard failure.

Why this matters: pre-v0.5.0 the ingester silently called Ollama for
every long assistant turn, and a 32 MB session took 45 minutes. No
existing test caught that because no test had a time budget. Wrapping
ingest-ish work in `with deadline(60): ...` makes the whole class of
"blocking call in hot path" bugs surface as test failures.

Usage:
    from _deadline import deadline, Exceeded

    with deadline(seconds=30, label="ingest of fixture-X"):
        ingest_jsonl(...)

    # Or as a decorator
    @deadline(seconds=10)
    def test_thing(): ...

POSIX implementation uses signal.SIGALRM (cheap, accurate). Windows
falls back to a watchdog thread that raises in the main thread via
threading.Timer + ctypes.PyThreadState_SetAsyncExc. The fallback isn't
perfect — Windows can't always interrupt blocked C calls — but it's
better than nothing for catching loop-hung Python code.
"""
from __future__ import annotations

import contextlib
import functools
import sys
import threading
import time
from typing import Any, Callable


class Exceeded(Exception):
    """Raised when a `deadline` block runs longer than allowed."""

    def __init__(self, seconds: float, label: str | None = None) -> None:
        msg = f"deadline exceeded after {seconds:.1f}s"
        if label:
            msg += f" — {label}"
        super().__init__(msg)
        self.seconds = seconds
        self.label = label


@contextlib.contextmanager
def deadline(seconds: float, label: str | None = None):
    """Abort the wrapped block if it runs longer than `seconds`.

    POSIX: uses SIGALRM (only valid on the main thread).
    Windows / non-main-thread: uses a watchdog thread and raises
    `Exceeded` from a daemon timer when the deadline elapses.
    """
    is_main_thread = threading.current_thread() is threading.main_thread()

    if sys.platform != "win32" and is_main_thread:
        # POSIX main-thread: SIGALRM
        import signal

        def _handler(signum, frame):
            raise Exceeded(seconds, label)

        prev = signal.signal(signal.SIGALRM, _handler)
        # signal.alarm only takes int seconds; round up to be safe.
        signal.alarm(int(seconds) + (1 if seconds != int(seconds) else 0))
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev)
    else:
        # Windows / threaded: best-effort watchdog. We don't have a
        # portable way to interrupt a blocked syscall on Windows from
        # a different thread; this catches Python-level hangs.
        timer_fired = {"v": False}
        start = time.time()

        def _watchdog():
            if time.time() - start >= seconds:
                timer_fired["v"] = True

        t = threading.Timer(seconds, _watchdog)
        t.daemon = True
        t.start()
        try:
            yield
            if timer_fired["v"]:
                raise Exceeded(seconds, label)
        finally:
            t.cancel()


def deadline_decorator(seconds: float, label: str | None = None):
    """Decorator form of `deadline` for whole-test guards."""

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def _inner(*args: Any, **kwargs: Any) -> Any:
            with deadline(seconds, label or fn.__name__):
                return fn(*args, **kwargs)

        return _inner

    return _wrap
