"""Elapsed-time indicator for long-running LLM calls.

A prompt can take minutes with nothing on screen, which is indistinguishable
from a hang. This prints a single self-updating line to a TTY and nothing at
all when output is redirected, so logs stay clean.
"""

from __future__ import annotations

import sys
import threading
import time


class ElapsedIndicator:
    """Shows a live elapsed timer on stderr until stopped.

    Used as a context manager; a no-op unless stderr is a terminal.
    """

    def __init__(self, label: str, *, enabled: bool = True, interval: float = 1.0):
        self.label = label
        self.interval = interval
        self.enabled = enabled and sys.stderr.isatty()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._width = 0

    def __enter__(self) -> ElapsedIndicator:
        if self.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 0.5)
        self._clear()

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop.wait(self.interval):
            elapsed = time.monotonic() - started
            self._draw(f"         {self.label} {elapsed:.0f}s")

    def _draw(self, text: str) -> None:
        try:
            padding = " " * max(0, self._width - len(text))
            sys.stderr.write(f"\r\033[2m{text}\033[0m{padding}")
            sys.stderr.flush()
            self._width = len(text)
        except (OSError, ValueError):
            # The terminal went away; stop trying to draw.
            self._stop.set()

    def _clear(self) -> None:
        if not self.enabled or not self._width:
            return
        try:
            sys.stderr.write("\r" + " " * self._width + "\r")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        self._width = 0
