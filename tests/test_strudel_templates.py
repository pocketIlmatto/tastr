"""Try this in > Strudel templates and Ask Claude (TAS-67): the server side. The page side is in test_strudel.py."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tastr import db
from tastr.web import claude, hosted
from tastr.web.hosted import Settings
from tastr.web.routes import strudel as routes
from tastr.web.server import create_app
from test_hosted import AUD, GOOD, TEAM, stub_verifier


def local_client(db_path):
    return TestClient(create_app(db_path, Settings()))


def hosted_client(db_path, **kw):
    return TestClient(create_app(db_path, Settings(hosted=True, access_team_domain=TEAM, access_aud=AUD, **kw),
                                 verifier=stub_verifier))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_templates_add_update_delete(db_path):
    c = local_client(db_path)
    a = c.post("/api/strudel/templates", json={"name": "Dub", "text": "setcpm({cpm})"}).json()
    b = c.post("/api/strudel/templates", json={"name": "Dub", "text": "x"}).json()  # names needn't be unique
    assert a["id"] != b["id"]
    assert c.put(f"/api/strudel/templates/{a['id']}", json={"name": " Dub 2 ", "text": "y"}).json()["name"] == "Dub 2"
    assert [(t["name"], t["text"]) for t in c.get("/api/strudel/templates").json()] == [("Dub 2", "y"), ("Dub", "x")]
    assert c.post("/api/strudel/templates", json={"name": "  ", "text": "x"}).status_code == 422
    assert c.post("/api/strudel/templates", json={"name": "x" * 41, "text": "x"}).status_code == 422
    assert c.post("/api/strudel/templates", json={"name": "Big", "text": "x" * 20_001}).status_code == 413
    assert c.delete(f"/api/strudel/templates/{a['id']}").status_code == 204
    assert c.delete(f"/api/strudel/templates/{a['id']}").status_code == 404
    assert c.put(f"/api/strudel/templates/{a['id']}", json={"name": "z", "text": "z"}).status_code == 404


def test_templates_are_per_person_and_capped(db_path, monkeypatch):
    conn = db.connect(db_path)
    other = db.add_template(conn, "someone@example.com", "Theirs", "x")
    conn.close()
    c = hosted_client(db_path)
    assert c.get("/api/strudel/templates", headers=GOOD).json() == []
    assert c.put(f"/api/strudel/templates/{other['id']}", json={"name": "Mine", "text": "y"}, headers=GOOD).status_code == 404
    assert c.delete(f"/api/strudel/templates/{other['id']}", headers=GOOD).status_code == 404
    monkeypatch.setattr(routes, "MAX_TEMPLATES", 2)
    for i in range(2):
        assert c.post("/api/strudel/templates", json={"name": f"t{i}", "text": "x"}, headers=GOOD).status_code == 200
    r = c.post("/api/strudel/templates", json={"name": "t3", "text": "x"}, headers=GOOD)
    assert r.status_code == 409 and "Delete one first" in r.json()["detail"]


class FakeClaude:
    """Stands in for anthropic.Anthropic: records each request and answers with `reply`."""

    def __init__(self, reply=None, stop="end_turn"):
        self.calls = []
        text = json.dumps(reply or {"code": "setcpm(128/4)\ns(\"bd*4\").slow(2)", "explanation": "Half-time now."})
        msg = SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                              usage=SimpleNamespace(input_tokens=1200, output_tokens=300))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (self.calls.append(kw), msg)[1]))


def test_ask_claude_sends_the_code_and_parses_the_reply():
    fake = FakeClaude()
    r = claude.ask_claude('s("bd*4")', "make it half-time", "128 BPM, A minor", client=fake)
    assert r.code.endswith(".slow(2)") and r.explanation == "Half-time now." and (r.input_tokens, r.output_tokens) == (1200, 300)
    kw = fake.calls[0]
    assert kw["model"] == claude.MODEL and "keep everything else" in kw["system"]
    assert kw["output_config"]["format"]["schema"]["required"] == ["code", "explanation"]
    content = kw["messages"][0]["content"]
    assert 's("bd*4")' in content and "make it half-time" in content and "128 BPM" in content
    assert claude.cost(1_000_000, 1_000_000) == claude.PRICE_IN + claude.PRICE_OUT


@pytest.mark.parametrize("stop, status", [("refusal", 422), ("max_tokens", 422)])
def test_ask_claude_explains_a_refusal_or_a_cut_off_answer(stop, status):
    with pytest.raises(claude.AskError) as e:
        claude.ask_claude("x", "y", "", client=FakeClaude(stop=stop))
    assert e.value.status == status


def test_ask_needs_a_key_locally(db_path):
    c = local_client(db_path)
    st = c.get("/api/strudel/ask").json()
    assert st["available"] is False and st["reason"] == "no-key" and st["limit"] is None
    r = c.post("/api/strudel/ask", json={"code": "x", "ask": "y"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_ask_locally_has_no_limit(db_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(claude, "ask_claude", lambda code, ask, summary: claude.Reply(code + "\n// changed", "Done.", 10, 5))
    c = local_client(db_path)
    assert c.get("/api/strudel/ask").json()["available"] is True
    for _ in range(3):
        r = c.post("/api/strudel/ask", json={"code": "x", "ask": "y"}).json()
        assert r["code"] == "x\n// changed" and r["left"] is None


def test_hosted_ask_limit_and_usage(db_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(claude, "ask_claude", lambda code, ask, summary: claude.Reply("new", "Done.", 1000, 200))
    c = hosted_client(db_path, daily_asks=2, admins=("friend@example.com",))
    assert c.get("/api/strudel/ask", headers=GOOD).json() == {
        "hosted": True, "model": claude.MODEL, "available": True, "limit": 2, "left": 2, "reason": None}
    assert c.post("/api/strudel/ask", json={"code": "x", "ask": "y"}, headers=GOOD).json()["left"] == 1
    assert c.post("/api/strudel/ask", json={"code": "x", "ask": "y"}, headers=GOOD).json()["left"] == 0
    r = c.post("/api/strudel/ask", json={"code": "x", "ask": "y"}, headers=GOOD)
    assert r.status_code == 429 and r.json()["detail"].startswith("You've used today's 2 asks")
    conn = db.connect(db_path)
    day = hosted.today()
    assert [db.usage_today(conn, "friend@example.com", k, day) for k in ("asks", "ask_in", "ask_out")] == [2, 2000, 400]
    conn.close()
    usage = c.get("/api/admin/usage", headers=GOOD).json()
    me = next(u for u in usage["users"] if u["email"] == "friend@example.com")
    assert me["today"]["asks"] == 2 and me["caps"]["asks"] == 2 and usage["limits"]["daily_asks"] == 2
    assert usage["claude"]["price_in"] == claude.PRICE_IN and sum(usage["series"]["people"]["friend@example.com"]["ask_in"]) == 2000
    assert any("daily Ask Claude limit" in e["detail"] for e in usage["recent"])
    # an admin raises the limit for today
    c.put("/api/admin/limits/friend@example.com", json={"daily_asks": 3}, headers=GOOD)
    assert c.get("/api/config", headers=GOOD).json()["limits"]["daily_asks"] == 3
    assert c.post("/api/strudel/ask", json={"code": "x", "ask": "y"}, headers=GOOD).status_code == 200


def test_hosted_ask_without_a_key_is_off(db_path):
    c = hosted_client(db_path)
    assert c.get("/api/strudel/ask", headers=GOOD).json()["reason"] == "hosted-off"
    assert c.post("/api/strudel/ask", json={"code": "x", "ask": "y"}, headers=GOOD).status_code == 503


def test_ask_refuses_oversized_input(db_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    c = local_client(db_path)
    assert c.post("/api/strudel/ask", json={"code": "x" * 60_001, "ask": "y"}).status_code == 413
    assert c.post("/api/strudel/ask", json={"code": "x", "ask": "y" * 501}).status_code == 422


# ---------- Short share links (TAS-100) ----------

def _opened(page: str) -> str:
    """The code a share page sends strudel.cc."""
    import base64, html as h, re
    from urllib.parse import unquote
    url = h.unescape(re.search(r'url=([^"]+)"', page)[1])
    assert url.startswith("https://strudel.cc/#")
    return base64.b64decode(unquote(url.split("#", 1)[1])).decode()


def test_share_link_opens_the_code_in_strudel(db_path):
    c = local_client(db_path)
    code = "// Mash-up: A <with> B\nsetcpm(32) // ünïcode\n"
    r = c.post("/api/strudel/share", json={"code": code}).json()
    assert r["url"] == f"/pub/s/{r['id']}" and len(r["id"]) == routes.SHARE_ID_LEN and r["id"].isalnum()
    assert c.post("/api/strudel/share", json={"code": code}).json() == r  # same code, same link
    assert c.post("/api/strudel/share", json={"code": code + " "}).json()["id"] != r["id"]
    page = c.get(r["url"])
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "<title>Strudel: Mash-up: A &lt;with&gt; B</title>" in page.text
    assert _opened(page.text) == code
    assert c.get("/pub/s/nothere1234").status_code == 404
    assert c.get("/pub/s/bad..id").status_code == 404
    assert c.post("/api/strudel/share", json={"code": "x" * (routes.MAX_CODE_BYTES + 1)}).status_code == 413
    assert c.post("/api/strudel/share", json={"code": ""}).status_code == 422


def test_share_links_are_capped_per_person(db_path, monkeypatch):
    monkeypatch.setattr(routes, "MAX_SHARES", 1)
    c = local_client(db_path)
    assert c.post("/api/strudel/share", json={"code": "a"}).status_code == 200
    assert c.post("/api/strudel/share", json={"code": "a"}).status_code == 200  # the same one again is fine
    assert c.post("/api/strudel/share", json={"code": "b"}).status_code == 409


def test_share_link_signs_stem_links_again(db_path, monkeypatch):
    from test_hosted import save
    mine, theirs = save(db_path, "friend@example.com"), save(db_path, "other@example.com")
    c = hosted_client(db_path, link_secret="s3cret")
    base = "https://tastr.example"
    a = base + c.get(f"/api/tracks/{mine}/strudel-link", headers=GOOD).json()["audio"]
    stale = f"{base}/pub/1.oldsig/tracks/{theirs}/stems/bass"
    code = f"samples({{ a: '{a.removesuffix('/audio')}/stems/drums', b: '{stale}' }})"
    assert c.post("/api/strudel/share", json={"code": code}).status_code == 401  # making one needs the login
    r = c.post("/api/strudel/share", json={"code": code}, headers=GOOD).json()
    monkeypatch.setattr(hosted.time, "time", lambda: 2_000_000_000.0)  # later: the old token would have expired
    page = c.get(r["url"])  # no login, like whoever gets the link
    assert page.status_code == 200
    opened = _opened(page.text)
    tok = opened.split("/pub/", 1)[1].split("/", 1)[0]
    assert tok.startswith("2000604800.")  # signed again, LINK_DAYS from now
    assert f"/tracks/{mine}/stems/drums'" in opened
    assert stale in opened  # someone else's track is left alone
