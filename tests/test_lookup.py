"""Catalog lookups (tastr.lookup): providers on canned responses, storage, comparison and the API."""

import time

import pytest
from fastapi.testclient import TestClient

from tastr import db, lookup
from tastr.lookup import acousticbrainz, freqblog, getsongbpm
from tastr.lookup.base import LookupResult, Provider, ProviderError, TrackQuery, same_artists, same_text
from tastr.web.server import create_app
from test_foundation import full_card

Q = TrackQuery(artist="Daft Punk", title="One More Time")


@pytest.fixture(autouse=True)
def fresh_guards(monkeypatch):
    """No refresh window, spacing or usage carried between tests (the guardrail tests set their own)."""
    lookup._asked.clear()
    monkeypatch.setenv("TASTR_LOOKUP_REFRESH_MINUTES", "0")
    for p in lookup.PROVIDERS.values():
        monkeypatch.setattr(p, "_guard", None)
        monkeypatch.setattr(p, "min_interval", 0.0)


def canned(monkeypatch, provider, *responses):
    """Make provider.get_json return the given (status, body) pairs in turn, recording the calls."""
    calls = []
    it = iter(responses)

    def fake(url, params=None, headers=None, source=None):
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
    monkeypatch.setenv("TASTR_LOOKUP_REFRESH_MINUTES", "10")
    assert client.post(f"/api/tracks/{tid}/lookup").json()["notes"] == [{"provider": "fake", "status": "recent", "error": None}]
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


# --- guardrails ---------------------------------------------------------------------------------------

from tastr.lookup import guard as guardmod  # noqa: E402


def _guard(name="g", interval=0.0, daily=None, monthly=None):
    p = Fake()
    p.name, p.label, p.key_env = name, name.upper(), "G_KEY"
    p.min_interval, p.daily_limit, p.monthly_limit = interval, daily, monthly
    return guardmod.Guard(p)


def _req(g, status=200, headers=None):
    g.before_request()
    g.after_response(status, headers)


def test_guard_spaces_requests():
    import time

    g = _guard(interval=0.15)
    t0 = time.time()
    _req(g), _req(g), _req(g)
    assert time.time() - t0 >= 0.3


def test_guard_daily_and_monthly_budgets_persist(db_path, monkeypatch):
    conn = db.connect(db_path)
    token = guardmod.usage_conn.set(conn)
    try:
        g = _guard(daily=2)
        _req(g), _req(g)
        with pytest.raises(guardmod.Limited, match="daily limit of 2"):
            g.before_request()
        assert _guard().used()[0] == 2  # counted in the database, so a restart doesn't reset it
        monkeypatch.setenv("TASTR_LOOKUP_G_DAILY", "5")
        monkeypatch.setenv("TASTR_LOOKUP_G_MONTHLY", "3")
        _req(g)
        with pytest.raises(guardmod.Limited, match="monthly limit of 3"):
            g.before_request()
    finally:
        guardmod.usage_conn.reset(token)
    assert tuple(conn.execute("SELECT provider, requests FROM lookup_usage").fetchone()) == ("g", 3)


@pytest.mark.parametrize("status,headers,reason", [
    (429, {"Retry-After": "120"}, "HTTP 429"),
    (503, {}, "HTTP 503"),
    (401, {}, "G_KEY"),
    (200, {"X-Quota-Used": "990", "X-Quota-Limit": "1000", "X-Quota-Reset": "2099-01-01T00:00:00Z"}, "nearly used up"),
    (200, {"X-RateLimit-Remaining": "3"}, "nearly used up"),
])
def test_guard_pauses_on_pushback(status, headers, reason):
    g = _guard()
    _req(g, status, headers)
    with pytest.raises(guardmod.Limited, match=reason):
        g.before_request()
    assert g.status()["paused_for"] > 0


def test_guard_ignores_healthy_quota_and_frees_the_lock():
    g = _guard()
    _req(g, 200, {"x-quota-used": "5", "x-quota-limit": "1000"})
    _req(g, 404)
    assert g.status()["paused_for"] == 0 and not g.lock.locked()


def test_get_json_goes_through_the_guard(monkeypatch):
    import io
    import urllib.error

    p = getsongbpm.GetSongBPM()

    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "60"}, io.BytesIO(b"slow down"))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ProviderError, match="429"):
        p.get_json("https://example.invalid/x")
    with pytest.raises(guardmod.Limited):  # paused: the second call never reaches urlopen
        p.get_json("https://example.invalid/x")
    assert p.guard.used()[0] == 1


def test_lookup_track_limited_and_recent(db_path, fake, monkeypatch):
    conn = db.connect(db_path)
    tid = db.save_card(conn, tagged_card())
    lookup.lookup_track(conn, tid, tagged_card(), [fake])
    fake.error = guardmod.Limited("FAKE: daily limit of 1 requests reached")
    (r,) = lookup.lookup_track(conn, tid, tagged_card(), [fake])
    assert r["status"] == "limited"
    assert db.get_lookups(conn, tid)[0]["status"] == "found"  # the stored answer survives
    fake.error = ProviderError("Fake answered HTTP 500")
    lookup.lookup_track(conn, tid, tagged_card(), [fake])
    assert db.get_lookups(conn, tid)[0]["status"] == "found"  # so does it after an error
    monkeypatch.setenv("TASTR_LOOKUP_REFRESH_MINUTES", "10")
    calls = fake.calls
    assert lookup.lookup_track(conn, tid, tagged_card(), [fake]) == [{"provider": "fake", "status": "recent"}]
    assert fake.calls == calls


# --- AcousticBrainz: MusicBrainz search, then the dump index ---------------------------------------------

MB_ONE, MB_TWO, MB_LIVE = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", \
    "33333333-3333-3333-3333-333333333333"


def mb_search(*recs):
    return 200, {"count": len(recs), "recordings": list(recs)}


def mb_rec(mbid, title="One More Time", credit=(("Daft Punk", ""),), length=320000):
    return {"id": mbid, "score": 100, "title": title, "length": length,
            "artist-credit": [{"name": n, "joinphrase": j, "artist": {"name": n}} for n, j in credit]}


def write_dump(folder, name, header, rows):
    p = folder / name
    p.write_text("\n".join([",".join(header), *(",".join(map(str, r)) for r in rows)]) + "\n")
    return p


@pytest.fixture
def ab_index(tmp_path, monkeypatch):
    """An index built from rhythm and tonal CSVs, in the dump's column layout; one in a tar archive."""
    import tarfile

    rhythm = write_dump(tmp_path, "rhythm.csv", ["mbid", "submission_offset", "bpm", "danceability"], [
        (MB_ONE, 0, 123.1, 1.2), (MB_ONE, 1, 61.5, 1.0), (MB_LIVE, 0, 118.0, 1.1), ("not-a-uuid", 0, 90, 1)])
    tonal = write_dump(tmp_path, "tonal.csv", ["mbid", "submission_offset", "key_key", "key_scale", "tuning_frequency"], [
        (MB_ONE, 0, "C#", "minor", 440), (MB_ONE, 1, "E", "major", 440), (MB_TWO, 0, "Bb", "major", 441)])
    tar = tmp_path / "tonal.tar"
    with tarfile.open(tar, "w") as t:
        t.add(tonal, arcname="acousticbrainz-lowlevel-features-20220623/tonal.csv")
    out = tmp_path / "home" / acousticbrainz.INDEX_NAME
    result = acousticbrainz.build_index([rhythm, tar], out, progress=lambda *_: None)
    monkeypatch.setenv("ACOUSTICBRAINZ_INDEX", str(out))
    monkeypatch.delenv("ACOUSTICBRAINZ_API_BASE", raising=False)
    return result


def test_acousticbrainz_index_keeps_first_submissions(ab_index):
    assert (ab_index["rows_read"], ab_index["recordings"]) == (7, 3)
    got = acousticbrainz.read_index(acousticbrainz.default_index(), [MB_ONE, MB_TWO, "nope"])
    assert got[MB_ONE] == {"bpm": 123.1, "key_key": "C#", "key_scale": "minor"}
    assert got[MB_TWO] == {"bpm": None, "key_key": "Bb", "key_scale": "major"}


def test_acousticbrainz_found_through_musicbrainz(ab_index, monkeypatch):
    p = acousticbrainz.AcousticBrainz()
    assert p.configured() and p.may_store()
    calls = canned(monkeypatch, p, mb_search(
        mb_rec(MB_LIVE, title="One More Time (live)", length=600000),  # too long to be this file
        mb_rec("44444444-4444-4444-4444-444444444444", credit=(("Someone Else", ""),)),
        mb_rec(MB_TWO, length=330000), mb_rec(MB_ONE, length=321000)))
    r = lookup.normalize(p.lookup(TrackQuery(artist="Daft Punk", title="One More Time", duration=320.4)))
    assert (r.status, r.bpm, r.key, r.tonic, r.mode) == ("found", 123.1, "C# minor", 1, "minor")
    assert r.extra == {"mbid": MB_ONE, "candidates": 2} and r.url == f"https://musicbrainz.org/recording/{MB_ONE}"
    assert (r.matched_artist, r.matched_title) == ("Daft Punk", "One More Time")
    url, params, _ = calls[0]
    assert url == "https://musicbrainz.org/ws/2/recording" and len(calls) == 1  # the index is local
    assert params["query"] == 'recording:"One More Time" AND artist:"Daft Punk"' and params["fmt"] == "json"


def test_acousticbrainz_not_found(ab_index, monkeypatch):
    p = acousticbrainz.AcousticBrainz()
    canned(monkeypatch, p, mb_search(), mb_search(mb_rec("55555555-5555-5555-5555-555555555555")))
    assert p.lookup(Q).status == "not_found"  # not on MusicBrainz
    r = p.lookup(Q)  # on MusicBrainz, never analyzed by AcousticBrainz
    assert r.status == "not_found" and r.extra["mbids"] == ["55555555-5555-5555-5555-555555555555"]
    assert (r.extra["tried"], r.extra["same_isrc"]) == (1, 0)


def test_acousticbrainz_matches_credits_and_escapes_quotes(ab_index, monkeypatch):
    p = acousticbrainz.AcousticBrainz()
    calls = canned(monkeypatch, p, mb_search(mb_rec(MB_ONE, title='Say "Hi"', credit=(("Beta", " & "), ("Alpha", "")))))
    r = p.lookup(TrackQuery(artist="Alpha, Beta", title='Say "Hi"'))
    assert r.status == "found" and r.matched_artist == "Beta & Alpha"
    assert calls[0][1]["query"] == 'recording:"Say \\"Hi\\"" AND artist:"Alpha"'  # first artist; credits matched after


def test_acousticbrainz_api_server_instead_of_an_index(tmp_path, monkeypatch):
    p = acousticbrainz.AcousticBrainz()
    monkeypatch.setenv("ACOUSTICBRAINZ_INDEX", str(tmp_path / "missing.sqlite"))
    monkeypatch.delenv("ACOUSTICBRAINZ_API_BASE", raising=False)
    assert not p.configured() and "tastr fetch acousticbrainz" in p.setup_hint()
    monkeypatch.setenv("ACOUSTICBRAINZ_API_BASE", "https://ab.example/")
    calls = canned(monkeypatch, p, mb_search(mb_rec(MB_TWO), mb_rec(MB_ONE)), (200, {
        MB_ONE: {"0": {"rhythm": {"bpm": 122.9}, "tonal": {"key_key": "F", "key_scale": "minor"}}}, "mbid_mapping": {}}))
    r = p.lookup(Q)
    assert (r.status, r.bpm, r.key, r.extra["mbid"]) == ("found", 122.9, "F minor", MB_ONE)
    assert calls[1][0] == "https://ab.example/api/v1/low-level"
    assert calls[1][1] == {"recording_ids": f"{MB_TWO};{MB_ONE}", "features": "rhythm.bpm;tonal.key_key;tonal.key_scale"}


def test_acousticbrainz_is_enabled_by_its_index_and_stored(ab_index, db_path, monkeypatch):
    monkeypatch.setenv("TASTR_LOOKUP", "acousticbrainz")
    (p,) = lookup.enabled()
    canned(monkeypatch, p, mb_search(mb_rec(MB_ONE, length=None)))
    conn = db.connect(db_path)
    tid = db.save_card(conn, tagged_card())
    (r,) = lookup.lookup_track(conn, tid, tagged_card())
    assert r["status"] == "found" and "transient" not in r
    assert tuple(conn.execute("SELECT provider, bpm, key_name, camelot FROM lookups").fetchone()) == \
        ("acousticbrainz", 123.1, "C# minor", "12A")
    assert acousticbrainz.AcousticBrainz.min_interval >= 1.0 and p.guard.limits()["daily"] == 1000  # MusicBrainz: 1 req/s


def test_fetch_acousticbrainz_downloads_the_dumps_and_builds_the_index(tmp_path, monkeypatch):
    zstandard = pytest.importorskip("zstandard")
    import io
    import tarfile

    from tastr.cloud import fetch

    src = tmp_path / "mirror"
    src.mkdir()
    for name, header, row in [(acousticbrainz.DUMP_FILES[0], "mbid,submission_offset,bpm", f"{MB_ONE},0,128.0"),
                              (acousticbrainz.DUMP_FILES[1], "mbid,submission_offset,key_key,key_scale", f"{MB_ONE},0,A,minor")]:
        data, buf = f"{header}\n{row}\n".encode(), io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            info = tarfile.TarInfo(f"dump/{name}.csv")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        (src / name).write_bytes(zstandard.ZstdCompressor().compress(buf.getvalue()))
    dest = tmp_path / "bucket" / "acousticbrainz"
    monkeypatch.setattr(acousticbrainz, "DUMP_BASE", src.as_uri())
    result = fetch.fetch("acousticbrainz", dest, progress=lambda *_: None)
    assert result["recordings"] == 1 and set(result["bytes"]) == {*acousticbrainz.DUMP_FILES, acousticbrainz.INDEX_NAME}
    assert acousticbrainz.read_index(dest / acousticbrainz.INDEX_NAME, [MB_ONE])[MB_ONE] == \
        {"bpm": 128.0, "key_key": "A", "key_scale": "minor"}


def test_acousticbrainz_tries_recordings_with_the_same_isrc(ab_index, monkeypatch):
    p = acousticbrainz.AcousticBrainz()
    calls = canned(monkeypatch, p,
                   mb_search({**mb_rec("66666666-6666-6666-6666-666666666666"), "isrcs": ["GBDUW0000059"]}),
                   mb_search({**mb_rec(MB_ONE, title="One More Time (single edit)"), "isrcs": ["GBDUW0000059"]},
                             {**mb_rec(MB_LIVE), "isrcs": ["XX0000000000"]}))  # another ISRC: not the same audio
    r = p.lookup(Q)
    assert (r.status, r.bpm, r.extra["mbid"], r.extra["candidates"]) == ("found", 123.1, MB_ONE, 2)
    assert calls[1][1]["query"] == "isrc:(GBDUW0000059)"


def test_acousticbrainz_retries_a_dropped_connection_once(ab_index, monkeypatch):
    p = acousticbrainz.AcousticBrainz()
    canned(monkeypatch, p, ProviderError("MusicBrainz's response was cut off (IncompleteRead)"), mb_search(mb_rec(MB_ONE)))
    assert p.lookup(Q).status == "found"
    canned(monkeypatch, p, ProviderError("MusicBrainz answered HTTP 503", 503), mb_search(mb_rec(MB_ONE)))
    assert p.lookup(Q).status == "found"  # busy: tried once more
    canned(monkeypatch, p, *[ProviderError("MusicBrainz answered HTTP 503", 503)] * 2)
    with pytest.raises(ProviderError, match="MusicBrainz answered HTTP 503"):
        p.lookup(Q)
    canned(monkeypatch, p, ProviderError("MusicBrainz answered HTTP 400", 400))
    with pytest.raises(ProviderError, match="HTTP 400"):  # not retried
        p.lookup(Q)


def test_get_json_names_the_service_that_failed(monkeypatch):
    import http.client
    import urllib.request

    p = acousticbrainz.AcousticBrainz()

    def cut_off(req, timeout):
        raise http.client.IncompleteRead(b"{", 10)

    monkeypatch.setattr(urllib.request, "urlopen", cut_off)
    with pytest.raises(ProviderError, match="^MusicBrainz's response was cut off"):
        p.get_json("https://musicbrainz.example/ws/2/recording", source="MusicBrainz")


def test_errors_can_be_asked_again_right_away(db_path, fake, monkeypatch):
    monkeypatch.setenv("TASTR_LOOKUP_REFRESH_MINUTES", "10")
    conn = db.connect(db_path)
    tid = db.save_card(conn, tagged_card())
    fake.error = ProviderError("Fake answered HTTP 503", 503)
    assert lookup.lookup_track(conn, tid, tagged_card())[0]["status"] == "error"
    fake.error = None
    assert lookup.lookup_track(conn, tid, tagged_card())[0]["status"] == "found"
    assert lookup.lookup_track(conn, tid, tagged_card())[0]["status"] == "recent"


def test_guard_ignores_musicbrainz_global_rate_headers():
    p = acousticbrainz.AcousticBrainz()
    g = p.guard
    g.before_request()
    g.after_response(200, {"x-ratelimit-limit": "15", "x-ratelimit-remaining": "12", "x-ratelimit-zone": "global"})
    assert g.paused_until == 0.0
    g.before_request()
    g.after_response(503, {"retry-after": "0"})
    assert g.paused_until <= time.time()  # Retry-After: 0 means no pause
