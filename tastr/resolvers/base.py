"""What a URL resolver is and where it registers, apart from tastr.ingest so resolver modules don't import it.

Each resolver says which URLs it handles and returns one or more Sources whose `local_path` points at
downloaded audio (a playlist link returns one Source per track).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..engine.card import Source


class IngestError(Exception):
    pass


class UrlResolver(Protocol):
    name: str

    def handles(self, url: str) -> bool: ...

    def resolve(self, url: str, cache_dir: Path) -> list[Source]: ...


URL_RESOLVERS: list[UrlResolver] = []


def register_url_resolver(resolver: UrlResolver) -> UrlResolver:
    URL_RESOLVERS.append(resolver)
    return resolver
