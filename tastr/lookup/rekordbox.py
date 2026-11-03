"""Rekordbox as a "listed elsewhere" source: its BPM and key for your tracks, from your collection export.

It never looks anything up. `tastr.rekordbox_compare.save_listings` stores Rekordbox's answers for the tracks in
both libraries as lookups under this name, so the card's "Listed elsewhere" line and the review list show where
Tastr disagrees with Rekordbox, the same way they do for the online catalogs (docs/rekordbox.md).
"""

from __future__ import annotations

from .base import LookupResult, Provider, ProviderError, TrackQuery, register


class Rekordbox(Provider):
    name = "rekordbox"
    label = "Rekordbox"
    homepage = "https://rekordbox.com"

    def configured(self) -> bool:
        return False  # nothing to call: listings come from an export, never from TASTR_LOOKUP

    def setup_hint(self) -> str:
        return "it isn't looked up; its values come from your Rekordbox export (docs/rekordbox.md)"

    def lookup(self, q: TrackQuery) -> LookupResult:
        raise ProviderError("Rekordbox values come from your collection export, not a lookup")


register(Rekordbox())
