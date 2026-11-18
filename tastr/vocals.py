"""Does a track have vocals? One answer per track ("yes", "no" or "unsure"), for the library's Vocals filter and the
card's vocal/instrumental chip, so the two always agree.

1. The track has stems that recorded their vocal share (stems.json, `vocal_share`): the vocal stem decides. The share
   is the part of the track where the vocal stem is clearly heard: of the 1-second windows that aren't near-silent
   (mix above SILENT_DB), the ones where the vocal stem is within HEARD_DB of the mix. It is relative to the mix, so
   a quiet master doesn't read as instrumental, and Demucs bleed (usually 25-40 dB under the mix) doesn't count.
   SHARE_YES or more: has vocals; SHARE_NO or less: instrumental.
2. Otherwise Essentia's voice score (engine/ml_tags.py, `voice_instrumental`): VOICE_CUT or more has vocals,
   1 - VOICE_CUT or less is instrumental.
3. Anything in between, or no ML tags and no measured stems yet: unsure.

Stems made before the share was recorded fall back to rule 2 until they are separated again.
"""

from __future__ import annotations

from typing import Any

from .engine.ml_tags import VOICE_CUT

WINDOW_S = 1.0
HEARD_DB = 20.0  # a window counts as "vocals heard" when the vocal stem is no more than this far under the mix
SILENT_DB = -50.0  # windows with the mix under this (dBFS RMS) are skipped
SHARE_YES, SHARE_NO = 0.10, 0.02  # starting values (2026-11-18); to be calibrated against the voice score


def vocal_share(vocals, mix, samplerate: int) -> float | None:
    """The share (0 to 1) of the non-silent 1-second windows where the vocal stem is within HEARD_DB of the mix.
    `vocals` and `mix`: mono or (channels, n) float arrays at full scale 1.0. None when the mix is all silence."""
    import numpy as np

    v, m = np.asarray(vocals, dtype=np.float64), np.asarray(mix, dtype=np.float64)
    if v.ndim > 1:
        v = v.mean(axis=0)
    if m.ndim > 1:
        m = m.mean(axis=0)
    hop = max(1, int(samplerate * WINDOW_S))
    n = min(len(v), len(m)) // hop
    if n == 0:
        return None
    rms = lambda x: np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1))  # noqa: E731
    db = lambda r: 20 * np.log10(np.maximum(r, 1e-10))  # noqa: E731
    vdb, mdb = db(rms(v)), db(rms(m))
    loud = mdb > SILENT_DB
    if not loud.any():
        return None
    return round(float(np.mean(vdb[loud] >= mdb[loud] - HEARD_DB)), 3)


def voice_score(ml_result: dict[str, Any] | None) -> float | None:
    """Essentia's voice score (0 to 1) from a stored ML tags result, or None."""
    if not isinstance(ml_result, dict):
        return None
    v = ((ml_result.get("heads") or {}).get("voice_instrumental") or {}).get("voice")
    return round(float(v), 3) if isinstance(v, (int, float)) else None


def verdict(voice: float | None, share: float | None) -> dict[str, Any]:
    """{"is": "yes" | "no" | "unsure", "by": "stems" | "ml" | None, "share" or "score"}: the rule above."""
    if share is not None:
        return {"is": "yes" if share >= SHARE_YES else "no" if share <= SHARE_NO else "unsure", "by": "stems",
                "share": share}
    if voice is not None:
        return {"is": "yes" if voice >= VOICE_CUT else "no" if voice <= 1 - VOICE_CUT else "unsure", "by": "ml",
                "score": voice}
    return {"is": "unsure", "by": None}


def stems_share(status: dict[str, Any]) -> float | None:
    """The vocal share a ready stem set recorded (a Jobs.status() answer), or None."""
    if status.get("state") != "ready":
        return None
    s = (status.get("made_with") or {}).get("vocal_share")
    return float(s) if isinstance(s, (int, float)) else None
