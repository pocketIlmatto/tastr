"""Decoding audio files to the mono float array the pipeline analyzes (or all channels, for stem separation)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import numpy as np

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".aiff", ".aif", ".m4a", ".aac", ".opus"}


class DecodeError(Exception):
    pass


def load_audio(path: str | os.PathLike, sr: int, mono: bool = True) -> np.ndarray:
    """Decode to float32 at `sr`, falling back to ffmpeg for formats libsndfile can't read (m4a, aac, ...).

    Mono gives shape (n,); `mono=False` keeps the file's channels as (channels, n).
    """
    import librosa

    def read(p):
        y, _ = librosa.load(p, sr=sr, mono=mono)
        return y if mono else np.atleast_2d(y)

    try:
        return read(path)
    except Exception as first_error:
        if not shutil.which("ffmpeg"):
            raise DecodeError(f"Could not decode {path}: {first_error}") from first_error
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "decoded.wav")
            proc = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), *(["-ac", "1"] if mono else []), "-ar", str(sr), wav],
                capture_output=True,
            )
            if proc.returncode != 0:
                raise DecodeError(f"ffmpeg could not decode {path}: {proc.stderr.decode(errors='replace')[:300]}") from first_error
            return read(wav)
