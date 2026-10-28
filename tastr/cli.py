"""Command line: `tastr analyze FILES...`, `tastr list`, `tastr show ID`, `tastr query SQL`, `tastr stages`, `tastr serve`."""

from __future__ import annotations

import argparse
import json
import sys

from . import db
from .pipeline import registered_stages, resolve_order


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
    sub.add_parser("stages", help="list registered pipeline stages in run order")
    w = sub.add_parser("serve", help="run the web UI")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)

    args = p.parse_args(argv)

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
        print(card.to_json(indent=2))
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
