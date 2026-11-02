"""Background work (tastr/runner.py): threads on a laptop and on Fly, the event loop on the Cloudflare Worker."""

import asyncio
import threading
import time

import pytest

from tastr import runner
from tastr.web.analysis_jobs import AnalysisJobs
from tastr.web.hosted import AnalysisSlots


def test_a_thread_lane_with_a_limit_runs_its_jobs_one_at_a_time():
    lane, seen, lock, done = runner.Threads().lane("t", 1), [], threading.Lock(), threading.Event()
    running = [0]

    def job(i):
        with lock:
            running[0] += 1
            seen.append(running[0])
        time.sleep(0.01)
        with lock:
            running[0] -= 1
        if i == 4:
            done.set()

    for i in range(5):
        lane.submit(job, i)
    assert done.wait(5)
    assert seen == [1] * 5


def test_threads_repeat_work_until_stopped():
    ticks = threading.Semaphore(0)
    stop = runner.Threads().every("t", 0.01, ticks.release)
    assert ticks.acquire(timeout=5) and ticks.acquire(timeout=5)
    stop()


def test_loop_lanes_run_jobs_as_tasks_and_keep_errors_to_the_log(caplog):
    seen = []

    async def main():
        lane = runner.Loop().lane("t", 1)
        lane.submit(seen.append, 1)
        lane.submit(lambda: 1 / 0)
        lane.submit(seen.append, 2)
        assert seen == []  # nothing runs until the request's handler yields
        await asyncio.gather(*lane.tasks)

    asyncio.run(main())
    assert seen == [1, 2]
    assert "background work 't' failed" in caplog.text


def test_loop_runs_repeating_work_when_due():
    clock, ran = [100.0], []
    loop = runner.Loop(clock=lambda: clock[0])
    assert loop.next_due() is None
    loop.every("a", 10, lambda: ran.append("a"))
    stop_b = loop.every("b", 60, lambda: ran.append("b"), delay=30)
    assert loop.next_due() == 100
    assert loop.run_due() == 1 and ran == ["a"]
    assert loop.next_due() == 110
    clock[0] = 131
    loop.run_due()
    assert ran == ["a", "a", "b"] and loop.next_due() == 141
    stop_b()
    clock[0] = 300
    loop.run_due()
    assert ran == ["a", "a", "b", "a"]


def test_slots_without_waiting_say_busy_at_once():
    slots = AnalysisSlots(1, wait=False)
    with slots:
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            with slots:
                pass
        assert time.monotonic() - start < 0.5
    with slots:
        assert slots.status()["running"] == 1


def test_analysis_jobs_on_the_loop_queue_in_their_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_current", runner.Loop())
    results = {}

    async def main():
        jobs = AnalysisJobs(tmp_path / "t.db", limit=1)
        first = jobs.start("me", ["a.wav"], lambda p: {"results": ["a"]})
        second = jobs.start("me", ["b.wav"], lambda p: {"results": ["b"]})
        assert jobs.status(second["id"], "me")["position"] == 1
        await asyncio.gather(*jobs._lane.tasks)
        results.update(a=jobs.status(first["id"], "me"), b=jobs.status(second["id"], "me"))

    asyncio.run(main())
    assert results["a"]["state"] == results["b"]["state"] == "done"
    assert results["b"]["results"] == ["b"]
