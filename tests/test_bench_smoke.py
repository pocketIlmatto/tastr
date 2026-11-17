"""The monthly benchmark smoke comparison (.github/scripts/bench_smoke.py, docs/ci.md#benchmark-smoke-run)."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "bench_smoke.py"
spec = importlib.util.spec_from_file_location("bench_smoke", SCRIPT)
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

QUOTED = {"scores": {"giantsteps-key": {"exact": 0.591, "tracks": 599, "run": "2026-11-01"}}}


def rep(score, tracks=100, failed=0, metric="key_correct"):
    return {"summary": {"tracks": tracks, "failed": failed, metric: score, f"{metric}_n": tracks - failed}}


def test_a_steady_score_is_ok():
    drop, rows = bs.compare({"giantsteps-key": rep(0.60)}, {"giantsteps-key": rep(0.61)}, QUOTED)
    assert not drop and rows[0].endswith("| ok |")


def test_a_drop_since_the_last_good_run_on_the_same_tracks():
    drop, rows = bs.compare({"giantsteps-key": rep(0.52)}, {"giantsteps-key": rep(0.60)}, QUOTED)
    assert drop and "since the last good run" in rows[0]


def test_a_last_run_on_other_tracks_is_not_compared():
    drop, rows = bs.compare({"giantsteps-key": rep(0.52)}, {"giantsteps-key": rep(0.70, tracks=200)}, QUOTED)
    assert not drop and "none yet" in rows[0]


def test_far_under_the_quoted_score_with_no_earlier_run():
    drop, rows = bs.compare({"giantsteps-key": rep(0.40)}, {}, QUOTED)
    assert drop and "under the quoted full score" in rows[0]


def test_a_missing_report_or_many_failed_tracks_is_a_problem():
    drop, rows = bs.compare({"giantsteps-key": None, "beatport-tempo": rep(0.9, failed=20, metric="acc1")}, {}, QUOTED)
    assert drop and "run failed" in rows[1] and "20 of 100 tracks failed" in rows[0]


def test_main_writes_the_drop_output(tmp_path, monkeypatch, capsys):
    (tmp_path / "now").mkdir()
    (tmp_path / "now" / "giantsteps-key.json").write_text(json.dumps(rep(0.6)))
    (tmp_path / "q.json").write_text(json.dumps(QUOTED))
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    bs.main([str(tmp_path / "now"), str(tmp_path / "missing"), "--quoted", str(tmp_path / "q.json"),
             "--datasets", "giantsteps-key,"])
    assert out.read_text() == "drop=false\n" and "| giantsteps-key |" in capsys.readouterr().out
