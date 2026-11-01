"""Command line: `tastr analyze FILES...`, `tastr list`, `tastr show ID`, `tastr query SQL`, `tastr retag`, `tastr set-owner EMAIL`, `tastr pack OUT --owner EMAIL`, `tastr unpack FILE`, `tastr lookup`, `tastr stems ID`, `tastr stages`, `tastr serve`, `tastr bench`, `tastr spec-bench`, `tastr drum-bench`, `tastr key-profiles`,
`tastr vibe-bench`, `tastr vibe-fit`, `tastr fetch`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import time
from pathlib import Path

from . import db
from .engine.pipeline import resolve_order


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
    so = sub.add_parser("set-owner", help="give tracks with no owner to EMAIL, to move a library to a hosted server")
    so.add_argument("email", help="the Access email whose library they join (docs/hosting.md)")
    pk = sub.add_parser("pack", help="write the library, its audio and stems to one file, to move it to a hosted server")
    pk.add_argument("out", help="the archive to write, e.g. library.tar")
    pk.add_argument("--owner", required=True, help="the Access email the tracks belong to on the server (docs/deploy.md)")
    up = sub.add_parser("unpack", help="on a hosted server: load a `tastr pack` archive into its empty library")
    up.add_argument("archive")
    lk = sub.add_parser("lookup", help="look tracks up in online catalogs (TASTR_LOOKUP, see docs/lookup.md)")
    lk.add_argument("track_ids", nargs="*", type=int, help="tracks to look up (default: all)")
    lk.add_argument("--missing", action="store_true", help="skip providers that already answered for a track")
    lk.add_argument("--providers", help="comma-separated provider names (default: TASTR_LOOKUP)")
    lk.add_argument("--usage", action="store_true", help="show requests used and limits per provider, send nothing")
    lb = sub.add_parser("labels", help="your own BPM, key and section labels (docs/bench.md, \"Your own tracks\")")
    lbs = lb.add_subparsers(dest="action", required=True)
    ls_ = lbs.add_parser("set", help="label one track; flags you leave out are cleared")
    ls_.add_argument("track_id", type=int)
    ls_.add_argument("--bpm", help="e.g. 174")
    ls_.add_argument("--key", help='e.g. "A minor", Am or 8A')
    ls_.add_argument("--boundaries", help='section starts, e.g. "0:00 0:32 1:04"')
    ls_.add_argument("--note")
    lbs.add_parser("list", help="every labeled track")
    lc = lbs.add_parser("clear", help="remove a track's labels")
    lc.add_argument("track_id", type=int)
    le = lbs.add_parser("export", help="write the labels as a `tastr bench manifest` CSV")
    le.add_argument("out")
    rv = sub.add_parser("review", help="tracks whose analysis is most likely wrong, to label first")
    rv.add_argument("--limit", type=int, default=20)
    st = sub.add_parser("stems", help="separate one track into vocals, bass, other and drums (needs tastr[stems])")
    st.add_argument("track_id", type=int)
    st.add_argument("--force", action="store_true", help="separate again even if stems exist")
    sub.add_parser("stages", help="list registered pipeline stages in run order")
    w = sub.add_parser("serve", help="run the web UI")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"],
                   help="debug adds stage tracebacks and uvicorn's details (default: info)")

    sb = sub.add_parser("spec-bench", help="spectrogram benches (Beat This!: tempo, downbeats, meter; Harmonix: sections), locally or on Modal")
    sb.add_argument("bench", choices=["tempo", "downbeats", "meter", "sections"])
    sb.add_argument("path", help="folder with <dataset>.npz and beat_this_annotations-1.0/ (a bucket folder with --remote)")
    sb.add_argument("--dataset", help="Beat This! dataset name (default: hjdb for tempo, gtzan for downbeats and meter)")
    sb.add_argument("--limit", type=int, help="first N tracks")
    sb.add_argument("--out", help="write the JSON report here")
    sb.add_argument("--remote", action="store_true", help="run on Modal against the R2 bucket (docs/cloud.md)")
    sb.add_argument("--record", action="store_true",
                    help="save the score to tastr/engine/bench_scores.json, quoted in the UI (full gtzan downbeat and harmonix sections runs only)")
    sb.add_argument("--write-originals", action="store_true",
                    help="save the original recordings' spectrograms and annotations as <dataset>-originals.npz "
                         "and <dataset>-annotations.json, the files to upload")

    kp = sub.add_parser("key-profiles", help="key profiles and labels of a key dataset, for train_key, locally or on Modal")
    kp.add_argument("dataset", choices=["giantsteps-key", "beatport-key", "fma-keys"])
    kp.add_argument("path", help="dataset folder (a bucket folder with --remote)")
    kp.add_argument("--out", required=True, help="the JSON file to write (train_key --profiles)")
    kp.add_argument("--limit", type=int, help="first N tracks")
    kp.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel (locally)")
    kp.add_argument("--remote", action="store_true", help="run on Modal against the R2 bucket (docs/cloud.md)")
    kp.add_argument("--from-cache", metavar="FEATURES",
                    help="read a local key_features cache instead of decoding audio")

    dm = sub.add_parser("drum-model", help="the optional pretrained drum model (ADTOF, CC BY-NC-SA 4.0): status, "
                                           "fetch, or run on one track")
    dm.add_argument("action", choices=["status", "fetch", "run"])
    dm.add_argument("track_id", type=int, nargs="?", help="run: the track to re-analyze with the model")

    db_ = sub.add_parser("drum-bench", help="drum lane benches (synth, idmt, mdb, stemgmd-mix), locally or on Modal")
    db_.add_argument("suite", choices=["synth", "idmt", "mdb", "stemgmd-mix"])
    db_.add_argument("path", nargs="?", help="dataset folder (a bucket folder with --remote); not for synth")
    db_.add_argument("--limit", type=int, help="first N tracks")
    db_.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel (locally)")
    db_.add_argument("--drums-only", action="store_true", help="mdb: score the drum stems instead of the full mixes")
    db_.add_argument("--kits", choices=["all", "train", "test"], default="all",
                     help="stemgmd-mix: all kits, or the half tuned on (train) or held out (test)")
    db_.add_argument("--drum-model", action="store_true",
                     help="score the pretrained drum model's lanes (locally it needs tastr[drums]; with --remote "
                          "it runs where the model is installed)")
    db_.add_argument("--out", help="write the JSON report here")
    db_.add_argument("--remote", action="store_true", help="run on Modal against the R2 bucket (docs/cloud.md)")
    db_.add_argument("--write-bundle", action="store_true",
                     help="save the annotations (idmt, mdb) or trimmed hits (stemgmd-mix) as the one file a remote run reads")

    vb = sub.add_parser("vibe-bench", help="danceability, energy and vibe tags vs. listener labels, locally or on Modal")
    vb.add_argument("dataset", choices=["mtg-moods", "deam"])
    vb.add_argument("path", help="dataset folder from `tastr fetch` (a bucket folder with --remote)")
    vb.add_argument("--limit", type=int, help="first N tracks")
    vb.add_argument("--jobs", type=int, default=1, help="tracks analyzed in parallel (locally)")
    vb.add_argument("--out", help="write the per-track rows and report here (the input of vibe-fit)")
    vb.add_argument("--remote", action="store_true", help="run on Modal against the R2 bucket (docs/cloud.md)")

    vf = sub.add_parser("vibe-fit", help="refit the danceability and energy weights from vibe-bench outputs")
    vf.add_argument("results", nargs="+", help="JSON files written by vibe-bench --out")
    vf.add_argument("--out", help="write the fit results as JSON here")

    fe = sub.add_parser("fetch", help="download a vibe dataset (mtg-moods, deam), FMA Keys (fma-keys), Ballroom (ballroom) or the "
                                      "AcousticBrainz lookup dump (acousticbrainz), locally or on Modal into the bucket")
    fe.add_argument("dataset", choices=["mtg-moods", "deam", "acousticbrainz", "fma-keys", "ballroom"])
    fe.add_argument("path", help="destination folder (a bucket folder with --remote)")
    fe.add_argument("--archives",
                    help="mtg-moods: which of the 100 audio archives, about 1.6 GB each, e.g. 0-5 or 0,7,9 "
                         "(default 0-5); fma-keys: which of its 10 zips, 1.7-5 GB each (default 0)")
    fe.add_argument("--remote", action="store_true", help="run on Modal, writing into the R2 bucket (docs/cloud.md)")

    b = sub.add_parser("bench", help="score the engine against labeled tracks (see docs/bench.md)")
    b.add_argument("dataset", help="synth, giantsteps-key, giantsteps-tempo, beatport-key, beatport-tempo, fma-keys, ballroom, harmonix or manifest")
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
    b.add_argument("--engine", choices=["python", "rust"],
                   help="which engine analyzes (default: TASTR_ENGINE, else python); rust always runs every stage")
    b.add_argument("--record", action="store_true",
                   help="save the headline score to tastr/engine/bench_scores.json, quoted in the UI (full runs only)")

    for name, parser in sub.choices.items():
        parser.set_defaults(func=COMMANDS[name])
    args = p.parse_args(argv)
    return args.func(args)


def _bench(args) -> int:
    if args.record:
        err = _record_error(args)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
    if args.write_index:
        return _bench_write_index(args)
    if args.remote:
        return _bench_remote(args)
    return _bench_local(args)


def _bench_write_index(args) -> int:
    from .cloud import jobs

    try:
        out = jobs.write_index(args.dataset, args.path or "")
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"wrote {out}; upload it with the folder (docs/cloud.md)")
    return 0


def _bench_remote(args) -> int:
    from .cloud import remote
    from .engine.bench import runner

    print(f"running {args.dataset} on Modal...", file=sys.stderr)
    stages = args.stages.split(",") if args.stages else None
    t0 = time.time()
    try:
        report = remote.bench(args.dataset, args.path, stages=stages, limit=args.limit,
                              jobs=args.jobs if args.jobs > 1 else None,
                              **({"engine": args.engine} if args.engine else {}))
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
    return _record(args, "bench", report)


def _bench_local(args) -> int:
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

    if args.engine:
        os.environ["TASTR_ENGINE"] = args.engine
    stages = args.stages.split(",") if args.stages else None
    report = runner.run(items, stages=stages, jobs=args.jobs, limit=args.limit, progress=progress)
    print(file=sys.stderr)
    baseline = runner.load_report(args.baseline) if args.baseline else None
    print(runner.format_summary(report, baseline))
    if args.out:
        runner.save(report, args.out)
    return _record(args, "bench", report)


def _stages(args) -> int:
    for st in resolve_order():
        print(f"{st.name:12} {st.version:14} requires={','.join(st.requires) or '-'}")
    return 0


def _serve(args) -> int:
    from .web.server import run

    run(args.host, args.port, args.db, log_level=args.log_level)
    return 0


def _analyze(args) -> int:
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


def _list(args) -> int:
    for t in db.list_tracks(db.connect(args.db)):
        print(_fmt_row(t))
    return 0


def _show(args) -> int:
    card = db.load_card(db.connect(args.db), args.track_id)
    if card is None:
        print(f"no track {args.track_id}", file=sys.stderr)
        return 1
    if "views" in card.extras:  # hundreds of KB of base64 picture data; the web UI is where it's useful
        card.extras["views"] = f"<waveform + spectrogram data, {len(json.dumps(card.extras['views'])) // 1024} KB>"
    print(card.to_json(indent=2))
    return 0


def _retag(args) -> int:
    print(f"updated {db.refresh_file_meta(db.connect(args.db))} track(s)")
    return 0


def _set_owner(args) -> int:
    email = args.email.strip().lower()
    print(f"gave {db.set_owner(db.connect(args.db), email)} track(s) to {email}")
    return 0


def _pack(args) -> int:
    from . import library

    try:
        r = library.pack(args.db, args.out, args.owner)
    except (ValueError, OSError, tarfile.TarError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"packed {r['tracks']} track(s) and {r['files']} file(s) into {args.out} ({r['bytes'] / 1e6:.0f} MB)")
    if r["missing_audio"]:
        print(f"{r['missing_audio']} track(s) had no audio file on disk; they move without it")
    return 0


def _unpack(args) -> int:
    from . import library

    try:
        r = library.unpack(args.archive, os.environ.get("TASTR_HOME", Path.home() / ".tastr"), args.db)
    except (ValueError, OSError, tarfile.TarError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"unpacked {r['tracks']} track(s) and {r['files']} file(s)")
    return 0


def _query(args) -> int:
    db.connect(args.db).close()  # make sure the file and schema exist
    rows = db.query(db.connect(args.db, readonly=True), args.sql)
    for r in rows:
        print(json.dumps(r, default=str))
    return 0

def _record_error(args) -> str | None:
    """Why `--record` can't be used with these arguments, or None."""
    from .engine.bench import scores

    if args.limit:
        return "--record saves milestone scores, so it needs a full run (drop --limit)"
    if args.cmd == "spec-bench":
        bench, dataset = args.bench, args.dataset or {"tempo": "hjdb", "sections": "harmonix"}.get(args.bench, "gtzan")
    else:
        bench, dataset = "bench", args.dataset
    if not scores.recordable(bench, dataset):
        return f"--record has no score to keep for {dataset}; the UI quotes {', '.join(scores.BENCH_METRICS)}, gtzan downbeats and harmonix sections"
    return None


def _record(args, bench: str, report: dict, dataset: str | None = None) -> int:
    """With --record, save the run's headline score to bench_scores.json (served at /api/bench)."""
    if not args.record:
        return 0
    from .engine.bench import scores

    try:
        key, entry = scores.entry_from_report(bench, dataset or args.dataset, report)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    scores.record(key, entry)
    print(f"recorded {key}: {entry['exact']:.3f} on {entry['tracks']} {entry['what']} in {scores.SCORES_PATH}")
    return 0


def _labels(args) -> int:
    from . import labels

    conn = db.connect(args.db)
    if args.cmd == "review":
        for r in labels.review_queue(conn, args.limit):
            name = " - ".join(x for x in (r["artist"], r["title"]) if x) or f"track {r['track_id']}"
            bpm = f"{r['bpm']:.1f} BPM" if r["bpm"] else "no BPM"
            print(f"{r['track_id']:>5}  {name}  ({bpm}, {r['key'] or 'no key'}): {'; '.join(r['reasons'])}")
        return 0
    if args.action == "list":
        for r in db.list_labels(conn):
            print(json.dumps(r, default=str))
        return 0
    if args.action == "export":
        n = labels.export_manifest(conn, args.out)
        print(f"wrote {n} labeled tracks to {args.out}; score them with `tastr bench manifest {args.out}`")
        return 0
    if db.load_card(conn, args.track_id) is None:
        print(f"no track {args.track_id}", file=sys.stderr)
        return 1
    if args.action == "clear":
        db.set_labels(conn, args.track_id, {})
        return 0
    try:
        clean = labels.clean({"bpm": args.bpm, "key": args.key, "boundaries": args.boundaries, "note": args.note})
    except labels.LabelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(db.set_labels(conn, args.track_id, clean)))
    return 0


def _lookup(args) -> int:
    import os

    from . import lookup

    if args.providers:
        os.environ["TASTR_LOOKUP"] = args.providers
    providers = lookup.enabled()
    if not providers:
        print("no lookup providers enabled: set TASTR_LOOKUP and set each provider up (docs/lookup.md)", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    if args.usage:
        for u in lookup.usage(conn):
            cap = lambda v: "no limit" if v is None else v  # noqa: E731
            print(f"{u['provider']}: {u['today']} today (limit {cap(u['daily'])}), {u['month']} this month "
                  f"(limit {cap(u['monthly'])}), one request per {u['interval']:g} s"
                  + (f"; paused {u['paused_for']} s: {u['pause_reason']}" if u["paused_for"] else ""))
        return 0
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

    if args.record:
        err = _record_error(args)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2

    dataset = args.dataset or {"tempo": "hjdb", "sections": "harmonix"}.get(args.bench, "gtzan")
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
    return _record(args, args.bench, report, dataset)


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


def _drum_model(args) -> int:
    import importlib.util

    from .engine import adt

    print(f"ADTOF drum model (https://github.com/MZehren/ADTOF): weights {adt.LICENSE}, non-commercial use only, "
          "share-alike. Not part of Tastr.")
    if args.action == "run":
        return _drum_model_run(args)
    if importlib.util.find_spec("torch") is None:
        print(f"error: {adt.INSTALL_HINT}", file=sys.stderr)
        return 2
    if args.action == "fetch":
        try:
            print(f"weights in {adt.fetch().parent} (checksums match)")
        except (OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    state = ("off (TASTR_DRUM_MODEL=off)" if adt.switched_off() else "on for every analysis (TASTR_DRUM_MODEL=adtof)"
             if adt.always() else "on request: `tastr drum-model run ID` or \"Detect drums with the model\" in the web UI")
    print(f"drum model: {state}; weights {'downloaded' if adt.cached() else 'download on first use'}")
    return 0


def _drum_model_run(args) -> int:
    from . import drum_jobs, service

    if args.track_id is None:
        print("error: run needs a track id (see `tastr list`)", file=sys.stderr)
        return 2
    if not drum_jobs.available():
        print(f"error: {drum_jobs.INSTALL_HINT} (or TASTR_DRUM_MODEL=off turned it off)", file=sys.stderr)
        return 2
    print(f"Re-analyzing track {args.track_id} with the drum model...", file=sys.stderr)
    try:
        result = service.reanalyze_track(db.connect(args.db), args.track_id, drum_model=True)
    except FileNotFoundError:
        print("error: the original audio file is not on disk", file=sys.stderr)
        return 1
    if result is None:
        print(f"no track {args.track_id}", file=sys.stderr)
        return 1
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
        return 1
    print(_summary_line(result.track_id, result.card))
    return 0


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
            report = remote.drum_bench(args.suite, args.path, args.limit, args.drums_only, args.kits, args.drum_model)
            print(f"wall {time.time() - t0:.0f} s, of which the bench {report.get('timing', {}).get('seconds', 0):.0f} s",
                  file=sys.stderr)
        else:
            if args.drum_model:
                os.environ["TASTR_DRUM_MODEL"] = "adtof"  # every analysis in the bench (and its workers)
            report = jobs.run_drum_bench(args.suite, args.path, args.limit, args.jobs, args.drums_only, args.kits)
    except (remote.RemoteError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(drums.format_report(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


def _vibe_bench(args) -> int:
    from pathlib import Path

    from .cloud import jobs, remote
    from .engine.bench import vibes

    try:
        if args.remote:
            print(f"running {args.dataset} on Modal...", file=sys.stderr)
            t0 = time.time()
            result = remote.vibe_bench(args.dataset, args.path, args.limit)
            print(f"wall {time.time() - t0:.0f} s, of which the analysis {result.get('timing', {}).get('seconds', 0):.0f} s",
                  file=sys.stderr)
        else:
            result = jobs.run_vibe_bench(args.dataset, ".", args.path, limit=args.limit, jobs=args.jobs)
    except (remote.RemoteError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(vibes.format_report(result["report"]))
    if args.out:
        Path(args.out).write_text(json.dumps(result))
    return 0


def _vibe_fit(args) -> int:
    from pathlib import Path

    from .engine.bench import vibes

    rows = [r for f in args.results for r in json.loads(Path(f).read_text())["rows"]]
    results = vibes.fit(rows)
    print(vibes.format_fit(results))
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1))
    return 0


def _fetch(args) -> int:
    from .cloud import fetch, remote

    spec = args.archives or ("0" if args.dataset == "fma-keys" else "0-5")
    archives = sorted({n for part in spec.split(",") for n in
                       (range(int(part.split("-")[0]), int(part.split("-")[1]) + 1) if "-" in part else [int(part)])})
    try:
        if args.remote:
            print(f"fetching {args.dataset} into the bucket at {args.path} on Modal...", file=sys.stderr)
            result = remote.fetch(args.dataset, args.path, archives)
        else:
            result = fetch.fetch(args.dataset, args.path, archives)
    except (remote.RemoteError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1))
    return 1 if result.get("checksum_mismatch") else 0


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


# Each subcommand's handler; main() checks that every subcommand has one.
COMMANDS = {
    "analyze": _analyze, "list": _list, "show": _show, "query": _query, "retag": _retag,
    "set-owner": _set_owner, "pack": _pack, "unpack": _unpack, "lookup": _lookup, "labels": _labels,
    "review": _labels, "stems": _stems, "stages": _stages, "serve": _serve, "spec-bench": _spec_bench,
    "key-profiles": _key_profiles, "drum-model": _drum_model, "drum-bench": _drum_bench,
    "vibe-bench": _vibe_bench, "vibe-fit": _vibe_fit, "fetch": _fetch, "bench": _bench,
}


if __name__ == "__main__":
    sys.exit(main())
