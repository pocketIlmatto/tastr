"""Where background work runs: in threads (the default: a laptop, Fly) or, on the Cloudflare Worker (worker/,
TAS-84), as tasks on its one event loop with the repeating work driven by the Durable Object's alarm.

Two kinds of work use it:

- `lane(name, limit).submit(fn, *args)`: a job such as a stem separation. `limit` is how many of the lane's jobs
  run at once (None: no limit); later ones wait their turn.
- `every(name, seconds, fn, delay)`: repeating work such as deleting expired audio, first after `delay` seconds.
  It returns a function that stops it.

Work is plain synchronous code either way. On the Worker it runs on the event loop until it waits on the network
(pyodide.ffi.run_sync), and then other requests and jobs run meanwhile. So no lock may be held across a wait
there: nothing can release it, and the Worker would hang. `blocking` says which runner this is, for code that
would otherwise wait on a lock (AnalysisSlots).

`use(runner)` picks the runner for the whole process; the Worker calls it before it builds the app.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

log = logging.getLogger(__name__)


class Threads:
    """Each lane is a thread pool (a thread per job when it has no limit); each `every` a thread of its own."""

    blocking = True

    def lane(self, name: str, limit: int | None = None) -> "ThreadLane":
        return ThreadLane(name, limit)

    def every(self, name: str, seconds: float, fn: Callable[[], None], delay: float = 0) -> Callable[[], None]:
        stop = threading.Event()

        def loop():
            if delay and stop.wait(delay):
                return
            while True:
                _run(name, fn)
                if stop.wait(seconds):
                    return

        threading.Thread(target=loop, name=f"tastr-{name}", daemon=True).start()
        return stop.set


class ThreadLane:
    def __init__(self, name: str, limit: int | None):
        self.name = name
        self._pool = ThreadPoolExecutor(max_workers=limit, thread_name_prefix=f"tastr-{name}") if limit else None

    def submit(self, fn: Callable, *args) -> None:
        if self._pool is None:
            threading.Thread(target=fn, args=args, name=f"tastr-{self.name}", daemon=True).start()
        else:
            self._pool.submit(fn, *args)


class Loop:
    """The Worker's runner. Jobs are asyncio tasks; repeating work is run by `run_due()`, which the Durable
    Object calls from its alarm, and `next_due()` is when to set the next alarm for."""

    blocking = False

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._every: dict[str, list] = {}  # name -> [seconds, fn, due]

    def lane(self, name: str, limit: int | None = None) -> "LoopLane":
        return LoopLane(name, limit)

    def every(self, name: str, seconds: float, fn: Callable[[], None], delay: float = 0) -> Callable[[], None]:
        entry = [seconds, fn, self.clock() + delay]
        self._every[name] = entry
        return lambda: self._every.pop(name, None) if self._every.get(name) is entry else None

    def next_due(self) -> float | None:
        """When repeating work is next due (a Unix time), or None if there is none."""
        return min((e[2] for e in self._every.values()), default=None)

    def run_due(self) -> int:
        """Run the repeating work that is due; returns how many ran."""
        now, n = self.clock(), 0
        for name, entry in list(self._every.items()):
            if entry[2] <= now:
                entry[2] = now + entry[0]  # before running, so a slow run can't make it due twice
                _run(name, entry[1])
                n += 1
        return n


class LoopLane:
    def __init__(self, name: str, limit: int | None):
        self.name, self.limit = name, limit
        self._sem: asyncio.Semaphore | None = None
        self.tasks: set[asyncio.Task] = set()  # held, so a running task isn't garbage collected

    def submit(self, fn: Callable, *args) -> None:
        task = asyncio.ensure_future(self._go(fn, args))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _go(self, fn, args) -> None:
        if self.limit is None:
            _run(self.name, fn, *args)
            return
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.limit)
        async with self._sem:
            _run(self.name, fn, *args)


def _run(name: str, fn: Callable, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - background work has nobody to raise to; jobs catch their own errors
        log.exception("background work %r failed", name)


_current: Threads | Loop = Threads()


def use(runner: Threads | Loop) -> None:
    global _current
    _current = runner


def current() -> Threads | Loop:
    return _current
