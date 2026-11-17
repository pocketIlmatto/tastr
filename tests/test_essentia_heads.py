from tastr.engine.bench import essentia_heads as eh
from tastr.engine.bench.datasets import Item, Truth


def item(i, labels):
    return Item(id=i, truth=Truth(), path=f"{i}.mp3", meta={"labels": labels})


def test_download_command_fetches_weights_and_metadata_of_every_model():
    cmd = eh.download_command("/m")
    assert cmd.count("curl ") == 2 * len(eh.model_paths())
    assert f"{eh.BASE_URL}/classification-heads/engagement/engagement_regression-discogs-effnet-1.json" in cmd
    assert "/m/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb" in cmd


def test_extra_labels_reads_voice_and_tonal(tmp_path):
    (tmp_path / "annotations-clean.tsv").write_text(
        "TRACK_ID\tARTIST_ID\tALBUM_ID\tPATH\tDURATION\tANNOTATIONS\n"
        "track_1\ta\tb\t00/1.mp3\t200\tvoice_instrumental---voice,voice,voice\ttonal_atonal---atonal,atonal,atonal\n")
    assert eh.extra_labels(tmp_path) == {"mtg-track_1": {"voice": 1, "tonal": 0}}


def test_held_out_keeps_only_annotated_mtg_tracks_and_pick_is_stable():
    items = [item("a", {"danceable": 1}), item("b", {"mood:dark": 1}), item("c", {"sad": 0})]
    assert [i.id for i in eh.held_out(items, "mtg-moods")] == ["a", "c"]
    assert eh.held_out(items, "deam") == items
    assert [i.id for i in eh.pick(items, 2)] == [i.id for i in eh.pick(list(reversed(items)), 2)]


def test_score_compares_head_and_engine_on_the_same_tracks():
    rows = []
    for k in range(20):
        yes = k % 2
        rows.append({"id": str(k), "labels": {"aggressive": yes}, "energy": 0.5,
                     "heads": {"mood_aggressive": {"aggressive": 0.9 if yes else 0.1}, "genre_discogs400": {"Electronic---House": 0.6}},
                     "head_seconds": {"load": 0.1, "embed": 1.0, "heads": 0.1, "audio_seconds": 60}})
    report = eh.score(rows)
    check = next(c for c in report["checks"] if c["head"] == "mood_aggressive:aggressive" and c["engine"] == "energy")
    assert check["head_score"] == 1.0 and check["n"] == 20 and check["stat"] == "auc"
    assert check["engine_score"] == 0.5  # a constant ranks at chance
    assert report["top_styles"] == {"Electronic---House": 20}
    assert "mood_aggressive:aggressive" in eh.format_report(report)
