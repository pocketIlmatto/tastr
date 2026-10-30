import numpy as np
import pytest
from synth import SR, make_track

from tastr.engine.bench import downbeats, tempo_spec


def _log_mel(y):
    """128 x frames spectrogram laid out like the Beat This! release (beat_this.preprocessing.LogMelSpect)."""
    import librosa

    S = np.abs(librosa.stft(y, n_fft=1024, hop_length=441)) / np.sqrt(1024)
    mel = librosa.filters.mel(sr=SR, n_fft=1024, n_mels=128, fmin=30, fmax=11000, norm=None)
    return np.log1p(1000 * mel @ S)


def _release(tmp_path, specs, beats, splits=None):
    np.savez(tmp_path / "set.npz", **specs)
    ann = tmp_path / "beat_this_annotations-1.0" / "set"
    (ann / "annotations" / "beats").mkdir(parents=True)
    for name, b in beats.items():
        np.savetxt(ann / "annotations" / "beats" / f"{name}.beats", b, fmt="%.3f\t%d")
    if splits:
        (ann / "single.split").write_text("".join(f"{n}\t{s}\n" for n, s in splits.items()))


def test_loaders_skip_augmented_copies(tmp_path):
    frames = np.zeros((500, 128), dtype=np.float16)
    specs = {f"song/{k}": frames for k in ("track", "track_ps-3", "track_ps2", "track_ts-12", "track_ts8")}
    beats = np.array([[0.5 * i, i % 4 + 1] for i in range(16)])
    _release(tmp_path, specs, {"song": beats}, {"song": "val"})
    assert downbeats.original_keys(np.load(tmp_path / "set.npz")) == ["song/track"]
    assert [i[0] for i in downbeats.load(tmp_path, "set")] == ["song"]
    ((name, log_mel, ref, split),) = tempo_spec.load(tmp_path, "set")
    assert (name, log_mel.shape, ref, split) == ("song", (128, 500), 120.0, "val")


@pytest.mark.parametrize("bpm", [124, 174])
def test_tempo_from_spectrogram(bpm):
    y, _ = make_track(bpm=bpm, bars_per_section=(4, 8, 4))
    assert abs(tempo_spec.tempo(_log_mel(y)) - bpm) <= 0.04 * bpm
