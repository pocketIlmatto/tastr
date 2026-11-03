"""The Worker error check (.github/scripts/worker_errors.py, docs/ci.md#worker-errors), on log events shaped
like the Workers Observability API's."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "worker_errors.py"
spec = importlib.util.spec_from_file_location("worker_errors", SCRIPT)
we = importlib.util.module_from_spec(spec)
spec.loader.exec_module(we)


def ev(rid, msg, trigger="POST /api/analyze/uploaded", t=1_793_000_000_000):
    return {"timestamp": t, "$metadata": {"requestId": rid, "trigger": trigger, "message": msg, "level": "error"}}


NOISE = ["Exception ignored while calling deallocator <function ZipFile.__del__ at 0x148d270>:",
         "Traceback (most recent call last):", '  File "/lib/python314.zip/zipfile/__init__.py", line 2059, in __del__',
         "OSError: [Errno 8] Bad file descriptor"]


def test_a_traceback_split_into_lines_is_one_failed_request():
    events = [ev("a", "Traceback (most recent call last):"), ev("a", '  File "/session/metadata/tastr/db.py", line 9'),
              ev("a", "FileNotFoundError: [Errno 44] No such file: '/tmp/tastr/uploads/3ba0594d/My Song.mp3'")]
    r = we.group(events)
    (sig,) = r["groups"]
    assert len(r["groups"][sig]) == 1 and "FileNotFoundError" in sig
    assert "My Song" not in sig and "uploads/<id>/<file>" in sig


def test_noise_alone_is_not_a_failure_and_doesnt_trip():
    r = we.group([ev(f"n{i}", line) for i in range(10) for line in NOISE])
    assert r["groups"] == {} and r["noise_only"] == 10
    _, failed, tripped = we.report(r, 60, False)
    assert failed == 0 and not tripped


def test_a_reset_trips_at_once_and_many_failures_trip():
    reset = we.group([ev("r", "pyodide.ffi.JsException: Error: Durable Object's isolate exceeded its memory limit and was reset.")])
    assert reset["resets"] == 1 and we.report(reset, 60, False)[2]
    few = we.group([ev(f"x{i}", "ValueError: bad") for i in range(we.FAILED_LIMIT - 1)])
    assert not we.report(few, 60, False)[2]
    many = we.group([ev(f"x{i}", "ValueError: bad") for i in range(we.FAILED_LIMIT)])
    text, failed, tripped = we.report(many, 60, False)
    assert failed == we.FAILED_LIMIT and tripped and "ValueError: bad" in text


def test_the_report_masks_personal_data():
    r = we.group([ev("p", "KeyError: friend@example.com", trigger="GET /pub/abc123.sig/tracks/5?x=secret")])
    text, _, _ = we.report(r, 60, False)
    assert "friend@example.com" not in text and "abc123" not in text and "secret" not in text
