"""Decoding audio files to the mono float array the pipeline analyzes."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import numpy as np

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".aiff", ".aif", ".m4a", ".aac", ".opus"}


class DecodeError(Exception):
    pass


def load_audio(path: str | os.PathLike, sr: int) -> np.ndarray:
    """Decode to mono float32 at `sr`, falling back to ffmpeg for formats libsndfile can't read."""
    import librosa

    try:
        y, _ = librosa.load(path, sr=sr, mono=True)
        return y
    except Exception as first_error:
        if not shutil.which("ffmpeg"):
            raise DecodeError(f"Could not decode {path}: {first_error}") from first_error
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "decoded.wav")
            proc = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), wav],
                capture_output=True,
            )
            if proc.returncode != 0:
                raise DecodeError(f"ffmpeg could not decode {path}: {proc.stderr.decode(errors='replace')[:300]}")
            y, _ = librosa.load(wav, sr=sr, mono=True)
            return y
