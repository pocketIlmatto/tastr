"""Album art from the Cover Art Archive for tracks without an embedded cover (tastr/coverart.py), and the cover
route's use of it: only for the people whose links may show album art, and only when outside requests are allowed."""

import pytest

from tastr import coverart, covers, http
from tastr.lookup import PROVIDERS, Limited, ProviderError, TrackQuery
from test_hosted import GOOD
from test_share_art import JPEG, client, two_tracks

REC, RG, REL, REL2 = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
                      "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    for k in ("TASTR_COVER_ART", "TASTR_LOOKUP"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def mb(monkeypatch):
    """MusicBrainz, faked: `answers` maps a URL's path to its JSON; `asked` lists what was asked."""
    p = PROVIDERS["acousticbrainz"]
    state = {"answers": {}, "asked": [], "search": []}

    def get_json(url, params=None, headers=None, source=None):
        state["asked"].append((url.split("/ws/2/")[-1], (params or {}).get("inc")))
        got = state["answers"].get(url.split("/ws/2/")[-1])
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise ProviderError("MusicBrainz answered HTTP 404", 404)
        return 200, got

    monkeypatch.setattr(p, "get_json", get_json)
    monkeypatch.setattr(p, "search", lambda query: state["search"])
    return state


@pytest.fixture
def caa(monkeypatch):
    """The archive, faked: `covers` maps a path ("release-group/<id>") to its picture; others are 404."""
    state = {"covers": {}, "asked": [], "status": None}

    def transport(method, url, headers, body, timeout):
        path = url.removeprefix(coverart.CAA_BASE + "/").removesuffix(f"/front-{coverart.SIZE}")
        state["asked"].append(path)
        assert headers["User-Agent"].startswith("tastr/")
        if state["status"]:
            return http.Response(state["status"], {}, iter([b""]))
        data = state["covers"].get(path)
        return http.Response(200 if data else 404, {}, iter([data or b"not found"]))

    monkeypatch.setattr(http, "_transport", transport)
    return state


def q(**kw):
    return TrackQuery(**({"artist": "Artist", "title": "Song", "duration": 200.0} | kw))


def test_enabled_follows_tastr_lookup_unless_set(monkeypatch):
    assert not coverart.enabled()  # nothing leaves the machine by default
    monkeypatch.setenv("TASTR_LOOKUP", "freqblog")
    assert coverart.enabled()
    monkeypatch.setenv("TASTR_COVER_ART", "0")
    assert not coverart.enabled()
    monkeypatch.delenv("TASTR_LOOKUP")
    monkeypatch.setenv("TASTR_COVER_ART", "1")
    assert coverart.enabled()


def test_tagged_recording_gets_its_release_groups_cover(mb, caa):
    mb["answers"][f"recording/{REC}"] = {"releases": [
        {"id": REL2, "title": "Hits", "status": "Bootleg", "date": "1999", "release-group": {"id": "x" * 36}},
        {"id": REL, "title": "Album", "status": "Official", "date": "2001-05-01", "release-group": {"id": RG}}]}
    caa["covers"][f"release-group/{RG}"] = JPEG
    assert coverart.fetch(q(mbid=REC)) == (JPEG, "image/jpeg")
    assert mb["asked"] == [(f"recording/{REC}", "releases+release-groups")]
    assert caa["asked"] == [f"release-group/{RG}"]  # the official release first, so one request


def test_the_tagged_album_wins_and_a_release_without_a_group_cover_tries_its_own(mb, caa):
    mb["answers"][f"recording/{REC}"] = {"releases": [
        {"id": REL, "title": "First", "status": "Official", "date": "1990", "release-group": {"id": RG}},
        {"id": REL2, "title": "The Album", "status": "Official", "date": "2005"}]}
    caa["covers"][f"release/{REL}"] = JPEG
    assert coverart.fetch(q(mbid=REC, album="The Album")) == (JPEG, "image/jpeg")
    assert caa["asked"] == [f"release/{REL2}", f"release-group/{RG}", f"release/{REL}"]


def test_stored_acousticbrainz_matches_are_used_before_a_search(mb, caa):
    mb["answers"][f"recording/{REC}"] = {"releases": [{"id": REL, "release-group": {"id": RG}}]}
    caa["covers"][f"release-group/{RG}"] = JPEG
    stored = [{"provider": "freqblog", "status": "found"},
              {"provider": "acousticbrainz", "status": "found", "extra": {"mbid": REC}}]
    mb["search"] = [{"id": "never"}]
    assert coverart.fetch(q(), stored) == (JPEG, "image/jpeg")
    assert [a for a, _ in mb["asked"]] == [f"recording/{REC}"]


def test_a_search_match_uses_the_releases_the_search_listed(mb, caa):
    mb["search"] = [{"id": REC, "title": "Song", "length": 200_000, "artist-credit": [{"name": "Artist"}],
                     "releases": [{"id": REL, "release-group": {"id": RG}}]},
                    {"id": "other", "title": "Another song entirely", "artist-credit": [{"name": "Artist"}]}]
    caa["covers"][f"release-group/{RG}"] = JPEG
    assert coverart.fetch(q()) == (JPEG, "image/jpeg")
    assert mb["asked"] == []  # no lookup needed


def test_nothing_found_is_none(mb, caa):
    assert coverart.fetch(None) is None  # no artist and title
    assert coverart.fetch(q()) is None  # no match
    mb["answers"][f"recording/{REC}"] = {"releases": [{"id": REL, "release-group": {"id": RG}}]}
    assert coverart.fetch(q(mbid=REC)) is None  # the archive has none
    caa["covers"][f"release-group/{RG}"] = b"<html>not a picture"
    assert coverart.fetch(q(mbid=REC)) is None


def test_busy_or_limited_is_unavailable_not_a_miss(mb, caa):
    mb["answers"][f"recording/{REC}"] = Limited("MusicBrainz is paused")
    with pytest.raises(coverart.Unavailable):
        coverart.fetch(q(mbid=REC))
    mb["answers"][f"recording/{REC}"] = {"releases": [{"id": REL, "release-group": {"id": RG}}]}
    caa["status"] = 503
    with pytest.raises(coverart.Unavailable):
        coverart.fetch(q(mbid=REC))


def test_old_musicbrainz_without_the_groups_include(mb, caa):
    url = f"recording/{REC}"
    answers = iter([ProviderError("bad inc", 400), {"releases": [{"id": REL}]}])
    mb["answers"] = type("A", (dict,), {"get": lambda self, k: next(answers)})()
    caa["covers"][f"release/{REL}"] = JPEG
    assert coverart.fetch(q(mbid=REC)) == (JPEG, "image/jpeg")
    assert mb["asked"] == [(url, "releases+release-groups"), (url, "releases")]


def test_cover_keeps_a_fetched_answer_and_a_miss_but_not_a_failure(tmp_path):
    from test_share_art import _wav
    plain = _wav(tmp_path / "plain.wav")
    assert covers.cover(5, plain, lambda: (JPEG, "image/jpeg")) == (JPEG, "image/jpeg")
    assert covers.cover(5, plain, lambda: pytest.fail("asked again")) == (JPEG, "image/jpeg")
    assert covers.cover(5, plain) is None  # without fetch: only the file's own

    def busy():
        raise coverart.Unavailable("busy")
    assert covers.cover(6, plain, busy) is None
    assert covers.cover(6, plain, lambda: None) is None  # asked again after the failure
    assert covers.cover(6, plain, lambda: pytest.fail("asked again")) is None  # the miss is kept
    covers.remove_cover(5)
    assert covers.cover(5, None, lambda: None) is None


def test_the_file_s_own_cover_wins(tmp_path):
    from test_share_art import wav_with_cover
    p = wav_with_cover(tmp_path / "a.wav")
    assert covers.cover(7, p, lambda: pytest.fail("fetched")) == (JPEG, "image/jpeg")


def test_cover_route_fetches_only_for_admins_and_when_allowed(db_path, tmp_path, monkeypatch):
    a, b = two_tracks(db_path, tmp_path)  # a has an embedded cover, b none
    asked = []
    monkeypatch.setattr(coverart, "fetch", lambda q, stored: asked.append(b) or (JPEG, "image/jpeg"))
    admin = client(db_path, admins=("friend@example.com",))
    assert admin.get(f"/api/tracks/{b}/cover", headers=GOOD).status_code == 404  # outside requests off
    monkeypatch.setenv("TASTR_LOOKUP", "freqblog")
    assert client(db_path).get(f"/api/tracks/{b}/cover", headers=GOOD).status_code == 404  # not an admin
    assert asked == []
    r = admin.get(f"/api/tracks/{b}/cover", headers=GOOD)
    assert r.status_code == 200 and r.content == JPEG and asked == [b]
    assert admin.get(f"/api/tracks/{a}/cover", headers=GOOD).status_code == 200 and asked == [b]  # its own
