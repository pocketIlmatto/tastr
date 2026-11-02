"""tastr/s3.py, the audio bucket's S3 client: its signatures match botocore's, and listings page."""

import datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from tastr import http, s3

botocore = pytest.importorskip("botocore")
from botocore.auth import S3SigV4Auth  # noqa: E402
from botocore.awsrequest import AWSRequest  # noqa: E402
from botocore.credentials import Credentials  # noqa: E402
from botocore.session import get_session  # noqa: E402

NOW = 1793580000.0  # 2026-11-02 00:40 UTC
ENDPOINT = "https://acct.r2.cloudflarestorage.com"
KEY = "uploads/ab12/My Song (live) é+&.mp3"


def client():
    return s3.Client(ENDPOINT, "auto", "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "tastr-audio")


@pytest.fixture
def frozen(monkeypatch):
    when = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc).replace(tzinfo=None)
    fake = type("dt", (datetime.datetime,), {"utcnow": classmethod(lambda cls: when),
                                              "now": classmethod(lambda cls, tz=None: when.replace(tzinfo=tz) if tz else when)})
    monkeypatch.setattr(botocore.auth.datetime, "datetime", fake)
    monkeypatch.setattr(s3.time, "time", lambda: NOW)


def boto():
    session = get_session()
    session.set_credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    cfg = botocore.config.Config(signature_version="s3v4", s3={"addressing_style": "path"})
    return session.create_client("s3", endpoint_url=ENDPOINT, region_name="auto", config=cfg)


@pytest.mark.parametrize("method,params", [("get_object", {}),
                                           ("get_object", {"ResponseContentDisposition": 'attachment; filename="a b.mp3"'}),
                                           ("put_object", {})])
def test_signed_links_match_botocore(frozen, method, params):
    theirs = boto().generate_presigned_url(method, Params={"Bucket": "tastr-audio", "Key": KEY} | params, ExpiresIn=3600)
    extra = {"response-content-disposition": params["ResponseContentDisposition"]} if params else None
    ours = client().presign("GET" if method == "get_object" else "PUT", KEY, 3600, extra, now=NOW)
    a, b = urlsplit(ours), urlsplit(theirs)
    assert a.path == b.path and parse_qs(a.query) == parse_qs(b.query)


def test_signed_requests_match_botocore(frozen, monkeypatch):
    sent = []

    def transport(method, url, headers, body):
        sent.append((method, url, headers))
        return http.Response(200, {}, iter([b""]))

    monkeypatch.setattr(http, "_transport", transport)
    client().delete(KEY)
    method, url, headers = sent[0]
    req = AWSRequest(method="DELETE", url=url, headers={"x-amz-content-sha256": s3.EMPTY_SHA256})
    S3SigV4Auth(Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"), "s3", "auto").add_auth(req)
    assert headers["Authorization"] == req.headers["Authorization"]


def test_listing_follows_continuation_tokens(monkeypatch):
    pages = {None: ("true", "t2", "a.wav", 3), "t2": ("false", None, "b.wav", 5)}
    asked = []

    def transport(method, url, headers, body):
        token = parse_qs(urlsplit(url).query).get("continuation-token", [None])[0]
        asked.append(token)
        truncated, nxt, key, size = pages[token]
        xml = (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>{truncated}</IsTruncated>'
               + (f"<NextContinuationToken>{nxt}</NextContinuationToken>" if nxt else "")
               + f"<Contents><Key>uploads/{key}</Key><Size>{size}</Size><LastModified>2026-11-02T00:40:00.000Z"
               "</LastModified></Contents></ListBucketResult>")
        return http.Response(200, {}, iter([xml.encode()]))

    monkeypatch.setattr(http, "_transport", transport)
    got = list(client().list("uploads/"))
    assert asked == [None, "t2"]
    assert got == [("uploads/a.wav", 3, NOW), ("uploads/b.wav", 5, NOW)]


def test_errors_say_what_the_bucket_said(monkeypatch):
    def transport(method, url, headers, body):
        if method == "HEAD":
            return http.Response(404, {}, iter([b""]))
        return http.Response(403, {"content-type": "application/xml"},
                             iter([b"<Error><Code>AccessDenied</Code><Message>no</Message></Error>"]))

    monkeypatch.setattr(http, "_transport", transport)
    assert client().head("x") is None
    with pytest.raises(s3.S3Error) as e:
        client().get("x")
    assert e.value.status == 403 and e.value.code == "AccessDenied"


def test_response_reads_like_a_file():
    r = http.Response(200, {}, iter([b"abc", b"defg", b"h"]))
    assert r.read(2) == b"ab" and r.read(4) == b"cdef" and r.read() == b"gh"
