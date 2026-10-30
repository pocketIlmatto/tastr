"""Catalog lookups (tastr.lookup): providers on canned responses, storage, comparison and the API."""

import pytest
from fastapi.testclient import TestClient

from tastr import db, lookup
from tastr.lookup import freqblog, getsongbpm
from tastr.lookup.base import LookupResult, Provider, ProviderError, TrackQuery, same_artists, same_text
from tastr.web.server import create_app
from test_foundation import full_card

Q = TrackQuery(artist="Daft Punk", title="One More Time")


def canned(monkeypatch, provider, *responses):
    """Make provider.get_json return the given (status, body) pairs in turn, recording the calls."""
    calls = []
    it = iter(responses)

    def fake(url, params=None, headers=None):
        calls.append((url, params or {}, headers or {}))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(provider, "get_json", fake)
    return calls


def tagged_card():
    card = full_card()  # 124 BPM, A minor
    card.source.artist = "Daft Punk"
    card.source.file_tags = {"artist": "Daft Punk", "title": "One More Time"}
    return card


class Fake(Provider):
    name, label, homepage, key_env = "fake", "Fake", "https://fake.example", None

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def lookup(self, q):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or LookupResult("fake", "found", bpm=124.0, key="Am")


@pytest.fixture
def fake(monkeypatch):
    p = Fake()
    monkeypatch.setitem(lookup.PROVIDERS, "fake", p)
    monkeypatch.setenv("TASTR_LOOKUP", "fake")
    return p


def test_getsongbpm_picks_the_matching_hit(monkeypatch):
    p = getsongbpm.GetSongBPM()
    monkeypatch.setenv("GETSONGBPM_API_KEY", "k")
    calls = canned(monkeypatch, p, (200, {"search": [
        {"song_id": "a", "song_title": "One More Time (Live)", "artist": {"name": "Someone Else"}, "tempo": "100", "key_of": "C"},
        {"song_id": "b", "song_title": "One More Time", "artist": {"name": "Daft Punk"}, "tempo": "123", "key_of": "D♭",
         "time_sig": "4/4", "song_uri": "https://getsongbpm.com/song/one-more-time/b"}]}))
    r = p.lookup(Q)
    assert (r.status, r.bpm, r.key, r.url) == ("found", 123.0, "D♭", "https://getsongbpm.com/song/one-more-time/b")
    assert (r.matched_title, r.extra["time_sig"]) == ("One More Time", "4/4")
    assert calls[0][1] == {"type": "both", "lookup": "song:One More Time artist:Daft Punk"}  # key not in the URL
    assert calls[0][2] == {"X-API-KEY": "k"}


def test_getsongbpm_fetches_thin_hits_and_reports_no_result(monkeypatch):
    p = getsongbpm.GetSongBPM()
    canned(monkeypatch, p, (200, {"search": [{"id": "b", "title": "One More Time", "artist": {"name": "Daft Punk"}}]}),
           (200, {"song": {"id": "b", "title": "One More Time", "artist": {"name": "Daft Punk"}, "tempo": "123", "key_of": "Em"}}))
    assert p.lookup(Q).bpm == 123.0
    canned(monkeypatch, p, (200, {"search": {"error": "no result"}}))
    assert p.lookup(Q).status == "not_found"
    canned(monkeypatch, p, (200, {"search": {"error": "invalid api key"}}))
    with pytest.raises(ProviderError):
        p.lookup(Q)


# A /lookup 200 as the live API sends it (2026-10-30), trimmed and with made-up values.
LIVE_SHAPE = {"track_name": "One More Time", "artist_name": "Daft Punk", "album_name": "Discovery", "isrc": "XX0000000001",
              "duration_ms": 320000, "is_remix": False, "bpm": 123.0, "bpm_alt": 61.5, "bpm_confidence": 2.65,
              "key": "D-Major", "key_confidence": 0.89, "mode": 1, "key_int": 2, "camelot": "10B", "open_key": "3d",
              "energy": 0.77, "time_signature": 4, "mood": "happy", "mood_vector": {"happy": 0.77},
              "extended": {"timbre": "dark"}, "feature_source": "essentia_preview", "bpm_snapped": 123}


def test_freqblog_found_pending_and_missing(monkeypatch):
    p = freqblog.FreqBlog()
    monkeypatch.setenv("FREQBLOG_API_KEY", "k")
    calls = canned(monkeypatch, p,
                   (200, LIVE_SHAPE),
                   (202, {"status": "queued"}),
                   ProviderError("FreqBlog answered HTTP 404", 404),
                   (200, {"track_name": "Around the World", "artist_name": "Daft Punk", "bpm": 121, "key": "E minor"}))
    r = p.lookup(Q)
    assert (r.status, r.bpm, r.key, r.camelot, r.extra["bpm_alt"], r.matched_title) == ("found", 123.0, "D-Major", "10B", 61.5, "One More Time")
    assert calls[0][1] == {"track": "One More Time", "artist": "Daft Punk"} and calls[0][2] == {"X-API-Key": "k"}
    assert p.lookup(Q).status == "pending"
    assert p.lookup(Q).status == "not_found"
    wrong = p.lookup(Q)  # its closest match is another song: not stored as this one
    assert wrong.status == "not_found" and wrong.extra["closest"] == "Daft Punk – Around the World"


def test_freqblog_is_stored_only_on_a_paid_plan(db_path, monkeypatch):
    p = freqblog.FreqBlog()
    canned(monkeypatch, p, (200, {"track_name": "One More Time", "artist_name": "Daft Punk", "bpm": 123, "key": "F-Minor"}),
           (200, {"track_name": "One More Time", "artist_name": "Daft Punk", "bpm": 123, "key": "F-Minor"}))
    conn = db.connect(db_path)
    tid = db.save_card(conn, tagged_card())
    monkeypatch.delenv("FREQBLOG_PLAN", raising=False)
    (r,) = lookup.lookup_track(conn, tid, tagged_card(), [p])
    assert r["transient"] and (r["tonic"], r["mode"], r["camelot"]) == (5, "minor", "4A")
    assert db.get_lookups(conn, tid) == []
    assert lookup.lookup_track(conn, tid, tagged_card(), [p], only_missing=True) == []  # analysis skips it
    monkeypatch.setenv("FREQBLOG_PLAN", "paid")
    lookup.lookup_track(conn, tid, tagged_card(), [p])
    assert tuple(conn.execute("SELECT key_name, key_tonic, key_mode, camelot FROM lookups").fetchone()) == ("F-Minor", 5, "minor", "4A")


@pytest.mark.parametrize("texts,want", [
    (("Am",), (9, "minor")), (("F-Minor",), (5, "minor")), (("D♭",), (1, "major")), (("E_major",), (4, "major")),
    ((None, "10B"), (2, "major")), (("??", None, "1m"), (9, "minor")), (("6d",), (11, "major")), (("",), None),
])
def test_parse_listed_key(texts, want):
    assert lookup.parse_listed_key(*texts) == want

def test_enabled_needs_the_name_and_the_key(monkeypatch):
    monkeypatch.setenv("TASTR_LOOKUP", "getsongbpm, freqblog, nope")
    monkeypatch.delenv("GETSONGBPM_API_KEY", raising=False)
    monkeypatch.setenv("FREQBLOG_API_KEY", "k")
    assert [p.name for p in lookup.enabled()] == ["freqblog"]
    monkeypatch.delenv("TASTR_LOOKUP")
    assert lookup.enabled() == []


def test_freqblog_matches_artists_listed_in_another_order(monkeypatch):
    p = freqblog.FreqBlog()
    monkeypatch.setenv("FREQBLOG_API_KEY", "k")
    canned(monkeypatch, p, (200, {**LIVE_SHAPE, "track_name": "Same Song",
                                  "artist_name": "Alpha, Beta, Gamma, Epsilon & Delta"}))
    r = p.lookup(TrackQuery(artist="Alpha, Beta, Gamma, Delta, Epsilon", title="Same Song"))
    assert r.status == "found" and r.matched_artist == "Alpha, Beta, Gamma, Epsilon & Delta"


def test_same_artists():
    assert same_artists("A, B & C", "C, A, B") and same_artists("Fred again.. feat. Future", "Fred again..")
    assert same_artists("Disclosure, Eliza Doolittle", "Disclosure & Eliza Doolittle")
    assert not same_artists("Daft Punk, Justice", "Daft Punk, Kavinsky") and not same_artists("", "A")


def test_same_text():
    assert same_text("The Chemical Brothers", "chemical brothers")
    assert same_text("One More Time", "One More Time (Radio Edit)")
    assert not same_text("", "x") and not same_text("Daft Punk", "Justice")


@pytest.mark.parametrize("bpm,key,want", [
    (124.5, "Am", {"bpm": "agrees", "key": "agrees"}),
    (62, "C", {"bpm": "half", "key": "relative"}),
    (248, "8A", {"bpm": "double", "key": "agrees"}),
    (140, "E minor", {"bpm": "differs", "key": "fifth"}),
    (None, "F#", {"bpm": None, "key": "differs"}),
])
def test_compare(bpm, key, want):
    assert lookup.compare(full_card(), {"bpm": bpm, "key": key}) == want


def test_lookup_track_stores_results_and_errors(db_path, fake):
    conn = db.connect(db_path)
    tid = db.save_card(conn, tagged_card())
    broken = Fake(error=ProviderError("Fake answered HTTP 401"))
    broken.name = "broken"
    lookup.lookup_track(conn, tid, tagged_card(), [fake, broken])
    stored = {r["provider"]: r for r in db.get_lookups(conn, tid)}
    assert stored["fake"]["bpm"] == 124.0 and (stored["broken"]["status"], stored["broken"]["error"]) == ("error", "Fake answered HTTP 401")
    lookup.lookup_track(conn, tid, tagged_card(), [fake, broken], only_missing=True)
    assert (fake.calls, broken.calls) == (1, 2)  # settled answers aren't asked again; errors are
    assert tuple(conn.execute("SELECT bpm, key_name FROM lookups WHERE provider = 'fake'").fetchone()) == (124.0, "Am")


def test_no_tags_means_no_request(db_path, fake):
    conn = db.connect(db_path)
    tid = db.save_card(conn, full_card())  # no artist tag
    assert lookup.lookup_track(conn, tid, full_card(), [fake]) == [{"provider": "fake", "status": "no_tags"}]
    assert fake.calls == 0


def test_lookups_survive_reanalysis_save(db_path, fake):
    conn = db.connect(db_path)
    tid = db.save_card(conn, tagged_card())
    lookup.lookup_track(conn, tid, tagged_card(), [fake])
    assert db.save_card(conn, tagged_card()) == tid
    assert [r["provider"] for r in db.get_lookups(conn, tid)] == ["fake"]


def test_api_lookup_and_card_body(db_path, tmp_path, monkeypatch, fake):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    client = TestClient(create_app(db_path))
    conn = db.connect(db_path)
    tid = db.save_card(conn, tagged_card())
    untagged = db.save_card(conn, _other_card())
    conn.close()
    body = client.get(f"/api/tracks/{tid}").json()
    assert body["lookups"] == [] and body["lookup_providers"] == [{"name": "fake", "label": "Fake"}]
    r = client.post(f"/api/tracks/{tid}/lookup").json()["card"]
    (row,) = r["lookups"]
    assert row["label"] == "Fake" and row["homepage"] == "https://fake.example"
    assert row["compare"] == {"bpm": "agrees", "key": "agrees"}
    assert client.post(f"/api/tracks/{untagged}/lookup").status_code == 422
    assert client.post("/api/tracks/999/lookup").status_code == 404
    monkeypatch.delenv("TASTR_LOOKUP")
    assert client.post(f"/api/tracks/{tid}/lookup").status_code == 400


def test_analysis_runs_enabled_lookups(db_path, synth_wav, fake, monkeypatch):
    from tastr import service
    from tastr.engine.card import Source

    path, _ = synth_wav
    src = Source(kind="file", uri=str(path), local_path=str(path), title="t", artist="Daft Punk",
                 file_tags={"artist": "Daft Punk", "title": "One More Time"})
    conn = db.connect(db_path)
    res = service.analyze_source(conn, src, stages=["core"])
    assert res.error is None and fake.calls == 1
    assert db.get_lookups(conn, res.track_id)[0]["status"] == "found"


def _other_card():
    c = full_card()
    c.source.sha256, c.source.uri = "other", "/other.wav"
    return c


def test_getsongbpm_matches_reordered_artist_credits(monkeypatch):
    p = getsongbpm.GetSongBPM()
    canned(monkeypatch, p, (200, {"search": [
        {"song_id": "c", "song_title": "Song", "artist": {"name": "Beta & Alpha"}, "tempo": "120", "key_of": "Bbm"}]}))
    r = p.lookup(TrackQuery(artist="Alpha, Beta", title="Song"))
    assert (r.status, r.bpm) == ("found", 120.0)
