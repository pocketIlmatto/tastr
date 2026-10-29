"""Command line: `tastr analyze FILES...`, `tastr list`, `tastr show ID`, `tastr query SQL`, `tastr retag`, `tastr stages`, `tastr serve`, `tastr bench`."""

from __future__ import annotations

import argparse
import json
import sys

from . import db
from .engine.pipeline import registered_stages, resolve_order


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tastr", description=__doc__)
    p.add_argument("--db", help="SQLite file (default: $TASTR_DB or ~/.tastr/tastr.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyze audio files, folders or links")
    a.add_argument("inputs", nargs="+")
    a.add_argument("--stages", help="comma-separated stage names (default: all)")
    a.add_argument("--json", action="store_true", help="print full cards as JSON")

    sub.add_parser("list", help="list analyzed tracks")
    s = sub.add_parser("show", help="print one track card as JSON")
    s.add_argument("track_id", type=int)
    q = sub.add_parser("query", help="run read-only SQL against the database")
    q.add_argument("sql")
    sub.add_parser("retag", help="re-read file tags for analyzed tracks without re-analyzing")
    sub.add_parser("stages", help="list registered pipeline stages in run order")
    w = sub.add_parser("serve", help="run the web UI")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)

    b = sub.add_parser("bench", help="score the engine against labeled tracks (see docs/bench.md)")
    b.add_argument("dataset", help="synth, giantsteps-key, giantsteps-tempo, harmonix or manifest")
    b.add_argument("path", nargs="?", help="dataset folder, or the CSV/JSON file for `manifest`")
    b.add_argument("--stages", help="comma-separated stage names (default: core,sections)")
    b.add_argument("--limit", type=int, help="only the first N tracks")
    b.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel")
    b.add_argument("--out", help="write the full JSON report here")
    b.add_argument("--baseline", help="earlier report to compare against")

    args = p.parse_args(argv)

    if args.cmd == "bench":
        from .engine import bench
        from .engine.bench import runner

        try:
            items = bench.load(args.dataset, args.path)
        except (ValueError, FileNotFoundError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if not items:
            print(f"error: no labeled tracks with audio found for {args.dataset} at {args.path}", file=sys.stderr)
            return 2

        def progress(i, n, row):
            print(f"\r{i}/{n} {row['id'][:50]:50}", end="", file=sys.stderr, flush=True)

        stages = args.stages.split(",") if args.stages else None
        report = runner.run(items, stages=stages, jobs=args.jobs, limit=args.limit, progress=progress)
        print(file=sys.stderr)
        baseline = runner.load_report(args.baseline) if args.baseline else None
        print(runner.format_summary(report, baseline))
        if args.out:
            runner.save(report, args.out)
        return 0

    if args.cmd == "stages":
        for st in resolve_order():
            print(f"{st.name:12} {st.version:14} requires={','.join(st.requires) or '-'}")
        return 0

    if args.cmd == "serve":
        from .web.server import run

        run(args.host, args.port, args.db)
        return 0

    if args.cmd == "analyze":
        from .ingest import IngestError
        from .service import analyze_inputs

        conn = db.connect(args.db)
        stages = args.stages.split(",") if args.stages else None
        try:
            results = analyze_inputs(conn, args.inputs, stages)
        except IngestError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        failed = 0
        for r in results:
            if r.error:
                failed += 1
                print(f"FAIL {r.source.uri}: {r.error}", file=sys.stderr)
            elif args.json:
                print(r.card.to_json(indent=2))
            else:
                print(_summary_line(r.track_id, r.card))
        return 1 if failed else 0

    if args.cmd == "list":
        for t in db.list_tracks(db.connect(args.db)):
            print(_fmt_row(t))
        return 0

    if args.cmd == "show":
        card = db.load_card(db.connect(args.db), args.track_id)
        if card is None:
            print(f"no track {args.track_id}", file=sys.stderr)
            return 1
        if "views" in card.extras:  # hundreds of KB of base64 picture data; the web UI is where it's useful
            card.extras["views"] = f"<waveform + spectrogram data, {len(json.dumps(card.extras['views'])) // 1024} KB>"
        print(card.to_json(indent=2))
        return 0

    if args.cmd == "retag":
        print(f"updated {db.refresh_file_meta(db.connect(args.db))} track(s)")
        return 0

    if args.cmd == "query":
        db.connect(args.db).close()  # make sure the file and schema exist
        rows = db.query(db.connect(args.db, readonly=True), args.sql)
        for r in rows:
            print(json.dumps(r, default=str))
        return 0
    return 2


def _summary_line(track_id, card) -> str:
    c = card.core
    key = c.key.name if c.key else "?"
    bpm = f"{c.bpm:.1f}" if c.bpm else "?"
    energy = f"{c.energy:.2f}" if c.energy is not None else "?"
    errs = f"  errors: {', '.join(card.errors)}" if card.errors else ""
    return f"[{track_id}] {card.source.title}  bpm={bpm} key={key} energy={energy} sections={len(card.sections)}{errs}"


def _fmt_row(t: dict) -> str:
    bpm = f"{t['bpm']:.1f}" if t["bpm"] else "?"
    return f"[{t['id']}] {t['title']}  bpm={bpm} key={t['key_name'] or '?'} camelot={t['camelot'] or '?'}"


if __name__ == "__main__":
    sys.exit(main())
