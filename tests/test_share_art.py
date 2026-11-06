"""Album art in mash-up share previews: reading embedded covers (tastr/covers.py), the cover route, and the share's
picture, which only an admin's links get (routes/strudel.py). The page draws the picture (static/app/share-art.js)."""

import re
import wave

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from tastr import covers, db
from tastr.web.hosted import Settings
from tastr.web.server import create_app
from test_hosted import AUD, GOOD, TEAM, save, stub_verifier

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60  # enough for the type check; nothing here decodes it
PNG_1200 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (1200).to_bytes(4, "big") + (630).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))


def _wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000), w.writeframes(b"\x00\x00" * 800)
    return path


def wav_with_cover(path, data=JPEG, kind=3):
    from mutagen.id3 import APIC
    from mutagen.wave import WAVE
    f = WAVE(str(_wav(path)))
    f.add_tags()
    f.tags.add(APIC(encoding=3, mime="image/jpeg", type=kind, desc="", data=data))
    f.save()
    return path


def test_reads_the_front_cover_from_id3(tmp_path):
    p = wav_with_cover(tmp_path / "a.wav")
    assert covers.read_cover(p) == (JPEG, "image/jpeg")
    from mutagen.id3 import APIC
    from mutagen.wave import WAVE
    f = WAVE(str(p))
    f.tags.add(APIC(encoding=3, mime="image/png", type=0, desc="back", data=PNG_1200))  # another picture first
    f.save()
    assert covers.read_cover(p)[1] == "image/jpeg"  # the front cover wins


def test_reads_a_flac_picture(tmp_path):
    from mutagen.flac import FLAC, Picture
    p = tmp_path / "a.flac"
    sf.write(p, np.zeros(800), 8000)
    f = FLAC(str(p))
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/png", PNG_1200
    f.add_picture(pic)
    f.save()
    assert covers.read_cover(p) == (PNG_1200, "image/png")


def test_no_cover_or_not_a_picture(tmp_path):
    assert covers.read_cover(_wav(tmp_path / "plain.wav")) is None
    assert covers.read_cover(wav_with_cover(tmp_path / "junk.wav", data=b"not a picture")) is None
    assert covers.read_cover(tmp_path / "missing.wav") is None


def test_cover_is_read_once_and_kept(tmp_path, monkeypatch):
    p = wav_with_cover(tmp_path / "a.wav")
    assert covers.cover(7, p) == (JPEG, "image/jpeg")
    p.unlink()  # the audio expires: the kept copy still answers
    assert covers.cover(7, p) == (JPEG, "image/jpeg")
    plain = _wav(tmp_path / "plain.wav")
    assert covers.cover(8, plain) is None
    monkeypatch.setattr(covers, "read_cover", lambda _: pytest.fail("read again"))
    assert covers.cover(8, plain) is None  # "none" is kept too
    covers.remove_cover(7)
    assert covers.cover(7, p) is None  # gone, and the audio with it
    assert covers.cover(9, None) is None


def client(db_path, admins=()):
    return TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD, admins=admins,
                                                   link_secret="s3cret"), verifier=stub_verifier))


def two_tracks(db_path, tmp_path, owner="friend@example.com"):
    a = save(db_path, owner, sha="a1", path=str(wav_with_cover(tmp_path / "a.wav")), uri="a.wav")
    b = save(db_path, owner, sha="b1", path=str(_wav(tmp_path / "b.wav")), uri="b.wav")
    return a, b


def og_image(page: str) -> str:
    return dict(re.findall(r'<meta (?:property|name)="([\w:]+)" content="([^"]*)"', page))["og:image"]


def test_cover_route(db_path, tmp_path):
    a, b = two_tracks(db_path, tmp_path)
    theirs = save(db_path, "other@example.com", sha="c1", path=str(wav_with_cover(tmp_path / "c.wav")), uri="c.wav")
    c = client(db_path)
    r = c.get(f"/api/tracks/{a}/cover", headers=GOOD)
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/tracks/{b}/cover", headers=GOOD).status_code == 404  # no art
    assert c.get(f"/api/tracks/{theirs}/cover", headers=GOOD).status_code == 404  # someone else's
    assert c.get(f"/api/tracks/{a}/cover").status_code == 401  # needs the login


def test_an_admins_mash_up_link_shows_the_album_art(db_path, tmp_path):
    a, b = two_tracks(db_path, tmp_path)
    c = client(db_path, admins=("friend@example.com",))
    r = c.post("/api/strudel/share", json={"code": "// A x B", "tracks": [a, b]}, headers=GOOD).json()
    assert r["art"] == "wanted" and r["art_url"] == f"/api/strudel/share/{r['id']}/art"
    assert og_image(c.get(r["url"]).text).endswith("/pub/preview.png")  # the standard one until the page sends it
    assert c.put(r["art_url"], content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, headers=GOOD).status_code == 422  # wrong size
    assert c.put(r["art_url"], content=JPEG, headers=GOOD).status_code == 422
    assert c.put(r["art_url"], content=PNG_1200, headers=GOOD).status_code == 204
    assert c.post("/api/strudel/share", json={"code": "// A x B", "tracks": [a, b]}, headers=GOOD).json()["art"] == "have"
    page = c.get(r["url"]).text  # no login, like a chat app's crawler
    assert og_image(page) == f"https://testserver{r['url']}.png"
    assert "album art of the two tracks" in page
    img = c.get(r["url"] + ".png")
    assert img.status_code == 200 and img.content == PNG_1200 and img.headers["content-type"] == "image/png"
    assert c.get("/pub/s/nothere1234.png").status_code == 404
    # One track, or a track that isn't yours: no art.
    assert c.post("/api/strudel/share", json={"code": "one", "tracks": [a]}, headers=GOOD).json()["art"] is None
    theirs = save(db_path, "other@example.com", sha="c1", uri="c.wav")
    assert c.post("/api/strudel/share", json={"code": "z", "tracks": [a, theirs]}, headers=GOOD).json()["art"] is None


def test_other_peoples_links_never_show_album_art(db_path, tmp_path):
    a, b = two_tracks(db_path, tmp_path)
    c = client(db_path)  # friend@example.com is not an admin here
    r = c.post("/api/strudel/share", json={"code": "// A x B", "tracks": [a, b]}, headers=GOOD).json()
    assert r["art"] is None and "art_url" not in r
    assert c.put(f"/api/strudel/share/{r['id']}/art", content=PNG_1200, headers=GOOD).status_code == 403
    # A picture kept from when they were an admin stops showing once they aren't.
    covers.put_art(r["id"], PNG_1200)
    assert og_image(c.get(r["url"]).text).endswith("/pub/preview.png")
    assert c.get(r["url"] + ".png").status_code == 404


def test_only_the_links_maker_sends_its_picture(db_path, tmp_path):
    a, b = two_tracks(db_path, tmp_path, owner="other@example.com")
    conn = db.connect(db_path)
    db.add_share(conn, "theirs12345", "other@example.com", "x", "code")
    conn.close()
    c = client(db_path, admins=("friend@example.com",))
    assert c.put("/api/strudel/share/theirs12345/art", content=PNG_1200, headers=GOOD).status_code == 404


def test_deleting_a_track_deletes_its_kept_cover(db_path, tmp_path):
    a, _ = two_tracks(db_path, tmp_path)
    covers.cover(a, tmp_path / "a.wav")
    conn = db.connect(db_path)
    db.delete_track(conn, a)
    conn.close()
    assert covers.cover(a, None) is None
