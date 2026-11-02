"""A small S3 client for the audio bucket (Cloudflare R2): the calls tastr/blobs.py makes, signed with AWS
Signature Version 4, sent through tastr/http.py.

It replaces boto3, which can't run on the Cloudflare Worker (worker/, TAS-84) and is a large dependency for a
handful of calls. Requests use path-style addresses (`<endpoint>/<bucket>/<key>`), as R2 expects, and don't
sign the payload (UNSIGNED-PAYLOAD, which R2 and S3 accept over HTTPS), so a file is sent as it is read.
`presign()` makes the links browsers, strudel.cc and Modal use; tests/test_s3.py checks it against botocore.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape

from . import http

UNSIGNED = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Error(OSError):
    """The bucket said no: `status` (0 if it couldn't be reached) and S3's error `code` (NoSuchKey, ...)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}" if status else message)
        self.status, self.code = status, code


def _enc(text: str, safe: str = "") -> str:
    return quote(text, safe="-_.~" + safe)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class Client:
    def __init__(self, endpoint: str | None, region: str, access_key: str, secret_key: str, bucket: str):
        self.region = region or "auto"
        self.endpoint = (endpoint or f"https://s3.{self.region}.amazonaws.com").rstrip("/")
        self.access_key, self.secret_key, self.bucket = access_key or "", secret_key or "", bucket
        self.host = urlsplit(self.endpoint).netloc

    # ---- signing ------------------------------------------------------------------------------------------

    def _path(self, key: str | None) -> str:
        return "/" + _enc(self.bucket) + ("/" + _enc(key, "/") if key else "")

    def _signing_key(self, day: str) -> bytes:
        k = _hmac(("AWS4" + self.secret_key).encode(), day)
        for part in (self.region, "s3", "aws4_request"):
            k = _hmac(k, part)
        return k

    def _signature(self, method: str, path: str, query: dict[str, str], headers: dict[str, str], payload: str,
                   stamp: str) -> tuple[str, str]:
        """(the signature, the signed header names) for a request; `headers` include host."""
        names = sorted(h.lower() for h in headers)
        lower = {k.lower(): str(v).strip() for k, v in headers.items()}
        canonical = "\n".join([
            method, path, "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items())),
            "".join(f"{n}:{lower[n]}\n" for n in names), ";".join(names), payload])
        scope = f"{stamp[:8]}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        return hmac.new(self._signing_key(stamp[:8]), to_sign.encode(), hashlib.sha256).hexdigest(), ";".join(names)

    @staticmethod
    def _stamp(now: float | None = None) -> str:
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))

    def presign(self, method: str, key: str, seconds: int, params: dict[str, str] | None = None,
                now: float | None = None) -> str:
        """A link that lets anyone `method` (GET or PUT) the object for `seconds`. `params`: extra query
        parameters the bucket honours, such as response-content-disposition."""
        stamp, path = self._stamp(now), self._path(key)
        query = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                 "X-Amz-Credential": f"{self.access_key}/{stamp[:8]}/{self.region}/s3/aws4_request",
                 "X-Amz-Date": stamp, "X-Amz-Expires": str(int(seconds)), "X-Amz-SignedHeaders": "host"} | (params or {})
        sig, _ = self._signature(method, path, query, {"host": self.host}, UNSIGNED, stamp)
        q = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items()))
        return f"{self.endpoint}{path}?{q}&X-Amz-Signature={sig}"

    def _send(self, method: str, key: str | None = None, query: dict[str, str] | None = None,
              headers: dict[str, str] | None = None, body: http.Body = None, ok=(200, 204)) -> http.Response:
        query, path = query or {}, self._path(key)
        headers = dict(headers or {})
        payload = UNSIGNED if body is not None else EMPTY_SHA256
        for attempt in range(2):  # once more after a dropped connection or a 5xx
            stamp = self._stamp()
            signed = headers | {"host": self.host, "x-amz-date": stamp, "x-amz-content-sha256": payload}
            sig, names = self._signature(method, path, query, signed, payload, stamp)
            signed["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.access_key}/{stamp[:8]}/{self.region}"
                                       f"/s3/aws4_request, SignedHeaders={names}, Signature={sig}")
            del signed["host"]
            url = self.endpoint + path + ("?" + "&".join(f"{_enc(k)}={_enc(v)}" if v != "" else _enc(k)
                                                         for k, v in sorted(query.items())) if query else "")
            data = body
            if isinstance(body, Path):
                data = open(body, "rb")  # noqa: SIM115 - closed below, after the request
            try:
                resp = http.request(method, url, signed, data)
            except http.HTTPError as e:
                if attempt:
                    raise S3Error(0, "Unreachable", str(e)) from e
                continue
            finally:
                if data is not body:
                    data.close()
            if resp.status in ok:
                return resp
            if resp.status >= 500 and not attempt and not hasattr(body, "read"):
                resp.close()
                continue
            raise self._error(resp)
        raise S3Error(0, "Unreachable", f"{method} {key or self.bucket} failed")

    @staticmethod
    def _error(resp: http.Response) -> S3Error:
        text = resp.text() if resp.status != 404 or "xml" in resp.headers.get("content-type", "") else ""
        code, message = "", text[:200]
        try:
            root = ET.fromstring(text)
            code, message = root.findtext("Code") or "", root.findtext("Message") or ""
        except ET.ParseError:
            pass
        return S3Error(resp.status, code or ("NoSuchKey" if resp.status == 404 else "Error"), message)

    # ---- objects --------------------------------------------------------------------------------------------

    def put(self, key: str, body: bytes | Path, content_type: str, content_encoding: str | None = None) -> None:
        """Store bytes, or a file read as it is sent."""
        size = body.stat().st_size if isinstance(body, Path) else len(body)
        headers = {"Content-Type": content_type, "Content-Length": str(size)}
        if content_encoding:
            headers["Content-Encoding"] = content_encoding
        self._send("PUT", key, headers=headers, body=body).close()

    def head(self, key: str) -> int | None:
        """The object's size, or None if there's no such object."""
        try:
            resp = self._send("HEAD", key)
        except S3Error as e:
            if e.status in (403, 404):  # 403: a token without list rights says that for a missing key
                return None
            raise
        resp.close()
        return int(resp.headers.get("content-length", 0))

    def get(self, key: str) -> http.Response:
        """The object, its body read as it arrives (`chunks()`, or `read(n)` like a file)."""
        return self._send("GET", key)

    def download(self, key: str, path: Path) -> None:
        with self.get(key) as resp, open(path, "wb") as f:
            for chunk in resp.chunks():
                f.write(chunk)

    def delete(self, key: str) -> None:
        self._send("DELETE", key).close()

    def delete_many(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            body = ("<Delete><Quiet>true</Quiet>" + "".join(f"<Object><Key>{escape(k)}</Key></Object>"
                                                            for k in keys[i:i + 1000]) + "</Delete>").encode()
            md5 = base64.b64encode(hashlib.md5(body).digest()).decode()  # noqa: S324 - S3 asks for Content-MD5
            self._send("POST", None, {"delete": ""}, {"Content-MD5": md5, "Content-Type": "application/xml",
                                                      "Content-Length": str(len(body))}, body).close()

    def list(self, prefix: str) -> Iterator[tuple[str, int, float]]:
        """(key, bytes, last modified as a Unix time) for every object under `prefix`."""
        token = None
        while True:
            query = {"list-type": "2", "prefix": prefix} | ({"continuation-token": token} if token else {})
            root = ET.fromstring(self._send("GET", None, query).read())
            for obj in root.iter(NS + "Contents"):
                when = datetime.fromisoformat(obj.findtext(NS + "LastModified").replace("Z", "+00:00"))
                yield obj.findtext(NS + "Key"), int(obj.findtext(NS + "Size")), when.astimezone(timezone.utc).timestamp()
            token = root.findtext(NS + "NextContinuationToken")
            if root.findtext(NS + "IsTruncated") != "true" or not token:
                return

    def create_bucket(self) -> None:
        """For tests and a first setup; R2 buckets are usually made in the dashboard."""
        self._send("PUT", None).close()
