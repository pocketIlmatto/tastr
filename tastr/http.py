"""Outgoing HTTP requests from synchronous code, for the audio bucket (tastr/s3.py) and Modal (cloud/remote.py).

`request()` sends with urllib by default. The Cloudflare Worker (worker/, TAS-84) has no sockets, so it swaps in
its own fetch with `use()`. Either way the answer's body can be read as it arrives (`chunks()`), so a download
never has to fit in memory.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import BinaryIO, Callable, Iterator

TIMEOUT = 20 * 60  # a drum model run or a stem separation on Modal can take minutes


class HTTPError(OSError):
    """The request couldn't be sent, or got no answer (`status` 0)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Response:
    """`status`, `headers` (lower-case names) and the body: `chunks()` as it arrives, or `read()` all of it."""

    def __init__(self, status: int, headers: dict[str, str], chunks: Iterator[bytes], close: Callable[[], None] = lambda: None):
        self.status, self.headers = status, headers
        self._chunks, self._close, self._buf = chunks, close, b""

    def chunks(self) -> Iterator[bytes]:
        try:
            if self._buf:
                buf, self._buf = self._buf, b""
                yield buf
            yield from self._chunks
        finally:
            self.close()

    def read(self, n: int = -1) -> bytes:
        """All of the body, or (`n`) up to n bytes of it: a file-like object for zipfile and copies."""
        if n is None or n < 0:
            out = self._buf + b"".join(self._chunks)
            self._buf = b""
            self.close()
            return out
        while len(self._buf) < n:
            part = next(self._chunks, None)
            if part is None:
                break
            self._buf += part
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def text(self) -> str:
        return self.read().decode(errors="replace")

    def close(self) -> None:
        self._close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


Body = bytes | BinaryIO | None
Transport = Callable[[str, str, dict[str, str], Body], Response]


def _urllib(method: str, url: str, headers: dict[str, str], body: Body) -> Response:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310 - our own bucket and Modal endpoint
    except urllib.error.HTTPError as e:  # an answer all the same: the caller decides what a status means
        resp = e
    except OSError as e:
        raise HTTPError(f"couldn't reach {urllib.request.urlparse(url).netloc}: {e}") from e

    def chunks():
        while part := resp.read(65536):
            yield part

    return Response(resp.status if hasattr(resp, "status") else resp.code,
                    {k.lower(): v for k, v in resp.headers.items()}, chunks(), resp.close)


_transport: Transport = _urllib


def use(transport: Transport | None) -> None:
    """`transport(method, url, headers, body)` returns a Response; None goes back to urllib."""
    global _transport
    _transport = transport or _urllib


def request(method: str, url: str, headers: dict[str, str] | None = None, body: Body = None) -> Response:
    return _transport(method, url, dict(headers or {}), body)
