"""Command line: `tastr analyze FILES...`, `tastr list`, `tastr show ID`, `tastr query SQL`, `tastr retag`, `tastr lookup`, `tastr stems ID`, `tastr stages`, `tastr serve`, `tastr bench`, `tastr spec-bench`, `tastr drum-bench`, `tastr key-profiles`."""

from __future__ import annotations

import argparse
import json
import sys
import time

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
    lk = sub.add_parser("lookup", help="look tracks up in online catalogs (TASTR_LOOKUP, see docs/lookup.md)")
    lk.add_argument("track_ids", nargs="*", type=int, help="tracks to look up (default: all)")
    lk.add_argument("--missing", action="store_true", help="skip providers that already answered for a track")
    lk.add_argument("--providers", help="comma-separated provider names (default: TASTR_LOOKUP)")
    st = sub.add_parser("stems", help="separate one track into vocals, bass, other and drums (needs tastr[stems])")
    st.add_argument("track_id", type=int)
    st.add_argument("--force", action="store_true", help="separate again even if stems exist")
    sub.add_parser("stages", help="list registered pipeline stages in run order")
    w = sub.add_parser("serve", help="run the web UI")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"],
                   help="debug adds stage tracebacks and uvicorn's details (default: info)")

    sb = sub.add_parser("spec-bench", help="Beat This! spectrogram benches (tempo, downbeats), locally or on Modal")
    sb.add_argument("bench", choices=["tempo", "downbeats"])
    sb.add_argument("path", help="folder with <dataset>.npz and beat_this_annotations-1.0/ (a bucket folder with --remote)")
    sb.add_argument("--dataset", help="Beat This! dataset name (default: hjdb for tempo, gtzan for downbeats)")
    sb.add_argument("--limit", type=int, help="first N tracks")
    sb.add_argument("--out", help="write the JSON report here")
    sb.add_argument("--remote", action="store_true", help="run on Modal against the R2 bucket (docs/cloud.md)")
    sb.add_argument("--write-originals", action="store_true",
                    help="save the original recordings' spectrograms and annotations as <dataset>-originals.npz "
                         "and <dataset>-annotations.json, the files to upload")

    kp = sub.add_parser("key-profiles", help="key profiles and labels of a key dataset, for train_key, locally or on Modal")
    kp.add_argument("dataset", choices=["giantsteps-key", "beatport-key"])
    kp.add_argument("path", help="dataset folder (a bucket folder with --remote)")
    kp.add_argument("--out", required=True, help="the JSON file to write (train_key --profiles)")
    kp.add_argument("--limit", type=int, help="first N tracks")
    kp.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel (locally)")
    kp.add_argument("--remote", action="store_true", help="run on Modal against the R2 bucket (docs/cloud.md)")
    kp.add_argument("--from-cache", metavar="FEATURES",
                    help="read a local key_features cache instead of decoding audio")

    db_ = sub.add_parser("drum-bench", help="drum lane benches (synth, idmt, mdb, stemgmd-mix), locally or on Modal")
    db_.add_argument("suite", choices=["synth", "idmt", "mdb", "stemgmd-mix"])
    db_.add_argument("path", nargs="?", help="dataset folder (a bucket folder with --remote); not for synth")
    db_.add_argument("--limit", type=int, help="first N tracks")
    db_.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel (locally)")
    db_.add_argument("--drums-only", action="store_true", help="mdb: score the drum stems instead of the full mixes")
    db_.add_argument("--out", help="write the JSON report here")
    db_.add_argument("--remote", action="store_true", help="run on Modal against the R2 bucket (docs/cloud.md)")
    db_.add_argument("--write-bundle", action="store_true",
                     help="save the annotations (idmt, mdb) or trimmed hits (stemgmd-mix) as the one file a remote run reads")

    b = sub.add_parser("bench", help="score the engine against labeled tracks (see docs/bench.md)")
    b.add_argument("dataset", help="synth, giantsteps-key, giantsteps-tempo, beatport-key, beatport-tempo, harmonix or manifest")
    b.add_argument("path", nargs="?", help="dataset folder, or the CSV/JSON file for `manifest`")
    b.add_argument("--stages", help="comma-separated stage names (default: core,sections)")
    b.add_argument("--limit", type=int, help="only the first N tracks")
    b.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel")
    b.add_argument("--out", help="write the full JSON report here")
    b.add_argument("--baseline", help="earlier report to compare against")
    b.add_argument("--write-index", action="store_true",
                   help="save the local folder's track list to tastr-index-DATASET.json for fast --remote runs")
    b.add_argument("--remote", action="store_true",
                   help="run on Modal; path is then a folder in the R2 dataset bucket (see docs/cloud.md)")

    args = p.parse_args(argv)

    if args.cmd == "spec-bench":
        return _spec_bench(args)
    if args.cmd == "key-profiles":
        return _key_profiles(args)
    if args.cmd == "drum-bench":
        return _drum_bench(args)

    if args.cmd == "bench" and args.write_index:
        from .cloud import jobs

        try:
            out = jobs.write_index(args.dataset, args.path or "")
        except (ValueError, FileNotFoundError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"wrote {out}; upload it with the folder (docs/cloud.md)")
        return 0

    if args.cmd == "bench" and args.remote:
        from .cloud import remote
        from .engine.bench import runner

        print(f"running {args.dataset} on Modal...", file=sys.stderr)
        stages = args.stages.split(",") if args.stages else None
        t0 = time.time()
        try:
            report = remote.bench(args.dataset, args.path, stages=stages, limit=args.limit,
                                  jobs=args.jobs if args.jobs > 1 else None)
        except (remote.RemoteError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        timing = report.get("timing", {})
        if timing:
            # Where the wall time went: waiting for a container, loading the dataset index, analysis.
            # (container times come from Modal's clock, so they're off by any clock skew)
            ready = timing.get("container_started", t0) - t0
            wait = timing.get("called", t0) - t0
            print(f"wall {time.time() - t0:.0f} s: container ready after {ready:.0f} s, call started after {wait:.0f} s, "
                  f"imports {timing.get('import_seconds', 0):.0f} s, loading the dataset {timing['load_seconds']:.0f} s, analysis {report['seconds']:.0f} s"
                  f" ({timing['jobs']} workers)", file=sys.stderr)
        baseline = runner.load_report(args.baseline) if args.baseline else None
        print(runner.format_summary(report, baseline))
        if args.out:
            runner.save(report, args.out)
        return 0

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

        run(args.host, args.port, args.db, log_level=args.log_level)
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

    if args.cmd == "stems":
        return _stems(args)

    if args.cmd == "retag":
        print(f"updated {db.refresh_file_meta(db.connect(args.db))} track(s)")
        return 0

    if args.cmd == "lookup":
        return _lookup(args)

    if args.cmd == "query":
        db.connect(args.db).close()  # make sure the file and schema exist
        rows = db.query(db.connect(args.db, readonly=True), args.sql)
        for r in rows:
            print(json.dumps(r, default=str))
        return 0
    return 2


def _lookup(args) -> int:
    import os

    from . import lookup

    if args.providers:
        os.environ["TASTR_LOOKUP"] = args.providers
    providers = lookup.enabled()
    if not providers:
        print("no lookup providers enabled: set TASTR_LOOKUP and each provider's API key (docs/lookup.md)", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    ids = args.track_ids or [t["id"] for t in db.list_tracks(conn)]
    for tid in ids:
        card = db.load_card(conn, tid)
        if card is None:
            print(f"{tid}: no such track", file=sys.stderr)
            continue
        for r in lookup.lookup_track(conn, tid, card, providers, only_missing=args.missing):
            what = r.get("error") or " ".join(str(x) for x in (r.get("bpm") and f"{r['bpm']:g} BPM", r.get("key")) if x)
            print(f"{tid}: {r['provider']} {r['status']}" + (f" ({what})" if what else ""))
    return 0


def _spec_bench(args) -> int:
    from pathlib import Path

    from .cloud import jobs, remote

    dataset = args.dataset or ("hjdb" if args.bench == "tempo" else "gtzan")
    mod = jobs.spec_module(args.bench)
    try:
        if args.write_originals:
            from .engine.bench.downbeats import write_originals

            npz, ann = write_originals(args.path, dataset)
            print(f"wrote {npz} and {ann.name}; upload both (docs/cloud.md)")
            return 0
        if args.remote:
            print(f"running {args.bench} on {dataset} on Modal...", file=sys.stderr)
            t0 = time.time()
            report = remote.spec_bench(args.bench, dataset, args.path, args.limit)
            t = report.get("timing", {})
            print(f"wall {time.time() - t0:.0f} s: loading the dataset {t.get('load_seconds', 0):.0f} s, "
                  f"analysis {t.get('seconds', 0):.0f} s", file=sys.stderr)
        else:
            report = jobs.run_spec_bench(args.bench, dataset, args.path, args.limit)
    except (remote.RemoteError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(mod.format_report(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


def _key_profiles(args) -> int:
    from .cloud import jobs, remote
    from .engine.bench import key_features

    try:
        if args.from_cache:
            rows, failed = key_features.profiles_from_cache(args.dataset, args.path, args.from_cache), []
        else:
            if args.remote:
                print(f"computing {args.dataset} key profiles on Modal...", file=sys.stderr)
                t0 = time.time()
                result = remote.key_profiles(args.dataset, args.path, args.limit)
                t = result.get("timing", {})
                print(f"wall {time.time() - t0:.0f} s: loading the dataset {t.get('load_seconds', 0):.0f} s, "
                      f"analysis {t.get('seconds', 0):.0f} s", file=sys.stderr)
            else:
                result = jobs.run_key_profiles(args.dataset, ".", args.path, limit=args.limit, jobs=args.jobs)
            rows, failed = result["tracks"], result["failed"]
    except (remote.RemoteError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    key_features.write_profiles(rows, args.out)
    print(f"wrote {len(rows)} tracks to {args.out}, {len(failed)} failed")
    for sid, err in failed:
        print(f"  {sid}: {err}", file=sys.stderr)
    return 1 if failed else 0


def _drum_bench(args) -> int:
    from pathlib import Path

    from .cloud import jobs, remote
    from .engine.bench import drums

    if args.suite != "synth" and not args.path:
        print(f"error: {args.suite} needs the dataset folder", file=sys.stderr)
        return 2
    try:
        if args.write_bundle:
            print(f"wrote {drums.write_bundle(args.suite, args.path)}; upload it with the folder (docs/cloud.md)")
            return 0
        if args.remote:
            print(f"running {args.suite} on Modal...", file=sys.stderr)
            t0 = time.time()
            report = remote.drum_bench(args.suite, args.path, args.limit, args.drums_only)
            print(f"wall {time.time() - t0:.0f} s, of which the bench {report.get('timing', {}).get('seconds', 0):.0f} s",
                  file=sys.stderr)
        else:
            report = jobs.run_drum_bench(args.suite, args.path, args.limit, args.jobs, args.drums_only)
    except (remote.RemoteError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(drums.format_report(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


def _stems(args) -> int:
    from pathlib import Path

    from . import stems

    card = db.load_card(db.connect(args.db), args.track_id)
    if card is None:
        print(f"no track {args.track_id}", file=sys.stderr)
        return 1
    path = card.source.local_path
    if not path or not Path(path).is_file():
        print("error: the original audio file is not on disk, so there is nothing to separate", file=sys.stderr)
        return 1
    folder = stems.stems_dir(card.source.sha256, args.track_id)
    if stems.is_ready(folder) and not args.force:
        print(f"Stems already in {folder} (use --force to separate again)")
        return 0
    if not stems.available():
        print(f"error: {stems.INSTALL_HINT}", file=sys.stderr)
        return 2
    print(f"Separating {card.source.title or path} (a few minutes on a CPU)...", file=sys.stderr)
    stems.separate_track(path, folder, lambda p: print(f"\r{p:4.0%}", end="", file=sys.stderr, flush=True))
    print(file=sys.stderr)
    print(f"Stems in {folder}: {', '.join(stems.STEM_NAMES)}")
    return 0


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
