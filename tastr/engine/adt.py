"""Optional pretrained drum transcription: ADTOF's Frame_RNN (Zehren et al.), run with PyTorch.

ADTOF (https://github.com/MZehren/ADTOF) is a convolutional-recurrent network trained on about 360 hours
of real music annotated from rhythm-game charts (its ADTOF-RGW and ADTOF-YT sets). It reports five
drums per 10 ms frame: kick, snare, toms, hi-hat, and cymbals (crash and ride together). Tastr uses it
for the kick, hat, tom and cymbal lanes when `tastr[drums]` (PyTorch) is installed; see docs/drums.md.

The weights are ADTOF's own release, CC BY-NC-SA 4.0 (non-commercial, share-alike), so they are not part
of Tastr: the first use downloads them from a pinned commit of the ADTOF repository into
$TASTR_HOME/models/ and checks their SHA-256. The network, the madmom-style spectrogram and peak picking
below are written here from ADTOF's published architecture and settings, and the TensorFlow checkpoint
is read without TensorFlow (tf_checkpoint.py).

It runs only when asked, because of that license and because it is slower: "Detect drums with the model"
in a track's menu (or `tastr drum-model run ID`) re-analyzes one track with it, downloading the weights
on first use. Re-analyzing that track later keeps using it. TASTR_DRUM_MODEL=adtof runs it on every
analysis instead (the drum benchmarks do this), and TASTR_DRUM_MODEL=off turns it off everywhere.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

LICENSE = "CC BY-NC-SA 4.0"
COMMIT = "b3968fb332f69b65ee07c089fc62f436503755db"  # MZehren/ADTOF, 2025-09-18
CHECKPOINT = "Frame_RNN_adtofAll_0"  # trained on ADTOF-RGW + ADTOF-YT only (the repo's "adtof-all" scenario)
FILES = {
    f"{CHECKPOINT}.index": "3fe35e421c448c395984168cd3aa1f979def902df784b6766291e55bb9aa069f",
    f"{CHECKPOINT}.data-00000-of-00001": "ff611129cb4a4d30722a54251e893341a58a016a40c419ab1e3786fc77a04779",
}
URL = f"https://raw.githubusercontent.com/MZehren/ADTOF/{COMMIT}/adtof/models/{{name}}"

CLASSES = ("kick", "snare", "tom", "hat", "cymbal")  # ADTOF's outputs: MIDI 35, 38, 47, 42, 49
THRESHOLDS = (0.22, 0.24, 0.32, 0.22, 0.30)  # ADTOF's peak-picking thresholds for this model
SR, FPS, FRAME = 44100, 100, 2048
WINDOW, WARMUP = 6000, 400  # frames: 60 s windows read with 4 s of context on each side
INSTALL_HINT = "the drum model needs PyTorch: pip install 'tastr[drums]' (it shares PyTorch with tastr[stems])"


def _choice() -> str:
    return os.environ.get("TASTR_DRUM_MODEL", "").strip().lower()


def switched_off() -> bool:
    """TASTR_DRUM_MODEL=off: never run the model, even when asked."""
    return _choice() in ("off", "0", "false", "none")


def installed() -> bool:
    """True when the model can run here when asked: PyTorch is installed and TASTR_DRUM_MODEL isn't off."""
    return not switched_off() and importlib.util.find_spec("torch") is not None


def always() -> bool:
    """TASTR_DRUM_MODEL=adtof: run the model on every analysis, not only when asked for one track."""
    return _choice() in ("adtof", "on", "1", "true", "always")


def wanted(options: dict | None) -> bool:
    """Whether this analysis should run the model: asked for ({"drum_model": True}) or always on."""
    return installed() and (always() or bool((options or {}).get("drum_model")))


def cached() -> bool:
    return all((models_dir() / name).is_file() for name in FILES)


def models_dir() -> Path:
    return Path(os.environ.get("TASTR_HOME", Path.home() / ".tastr")) / "models" / f"adtof-{COMMIT[:7]}"


def fetch() -> Path:
    """The checkpoint prefix, downloading and verifying the weights the first time."""
    folder = models_dir()
    for name, sha in FILES.items():
        path = folder / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        log.info("downloading the ADTOF drum model weights (%s) to %s", LICENSE, folder)
        with urllib.request.urlopen(URL.format(name=name), timeout=60) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"{name} from the ADTOF repository doesn't match its pinned checksum")
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return folder / CHECKPOINT


# --- Spectrogram: madmom's LogarithmicFilteredSpectrogram as ADTOF configures it --------------------

def filterbank(sr: int = SR, frame: int = FRAME, bands: int = 12, fmin: float = 20.0, fmax: float = 20000.0,
               fref: float = 440.0) -> np.ndarray:
    """Triangular filters on a log frequency scale (bands per octave, aligned to A4), one per unique FFT bin
    triple, each normalised to sum 1: (bands x frame // 2)."""
    lo = np.floor(np.log2(fmin / fref) * bands)
    hi = np.ceil(np.log2(fmax / fref) * bands)
    freqs = fref * 2.0 ** (np.arange(lo, hi) / bands)
    freqs = freqs[np.searchsorted(freqs, fmin):]
    freqs = freqs[:np.searchsorted(freqs, fmax, "right")]
    bin_freqs = np.fft.fftfreq(frame, 1.0 / sr)[: frame // 2]
    idx = np.clip(bin_freqs.searchsorted(freqs), 1, len(bin_freqs) - 1)
    idx = idx - (freqs - bin_freqs[idx - 1] < bin_freqs[idx] - freqs)  # nearest bin
    idx = np.unique(idx)
    fb = np.zeros((len(idx) - 2, frame // 2), dtype=np.float32)
    for k, (start, center, stop) in enumerate(zip(idx[:-2], idx[1:-1], idx[2:])):
        if stop - start < 2:
            center, stop = start, start + 1
        tri = np.concatenate([np.linspace(0, 1, center - start, endpoint=False),
                              np.linspace(1, 0, stop - center, endpoint=False)])
        fb[k, start:stop] = tri / tri.sum()
    return fb


def spectrogram(y: np.ndarray, fb: np.ndarray | None = None) -> np.ndarray:
    """frames x bands: log10(1 + filtered STFT magnitude) of mono audio at SR, frames centred every 10 ms."""
    fb = filterbank() if fb is None else fb
    hop = SR // FPS
    n = int(np.ceil(len(y) / hop))
    padded = np.concatenate([np.zeros(FRAME // 2, np.float32), np.asarray(y, np.float32), np.zeros(FRAME, np.float32)])
    window = np.hanning(FRAME).astype(np.float32)
    out = np.empty((n, fb.shape[0]), np.float32)
    for a in range(0, n, 2048):  # in chunks, to bound memory on long tracks
        idx = np.arange(a, min(n, a + 2048))[:, None] * hop + np.arange(FRAME)[None, :]
        mag = np.abs(np.fft.rfft(padded[idx] * window, axis=1))[:, : FRAME // 2]
        out[a:a + len(idx)] = np.log10(1.0 + mag @ fb.T)
    return out


# --- The network ----------------------------------------------------------------------------------

def _network(n_bands: int):
    """Frame_RNN: two conv blocks (3x3 conv, ReLU, batch norm, twice; max-pool 3 over frequency), three
    bidirectional GRUs of 60 units, one sigmoid output per drum and frame."""
    import torch
    from torch import nn

    class Block(nn.Module):
        def __init__(self, cin: int, cout: int):
            super().__init__()
            self.c1, self.b1 = nn.Conv2d(cin, cout, 3, padding=1), nn.BatchNorm2d(cout, eps=1e-3)
            self.c2, self.b2 = nn.Conv2d(cout, cout, 3, padding=1), nn.BatchNorm2d(cout, eps=1e-3)

        def forward(self, x):
            x = self.b2(torch.relu(self.c2(self.b1(torch.relu(self.c1(x))))))
            # Keras "same" max pooling: pad the frequency axis (more at the end) with values that never win
            w = x.shape[-1]
            pad = max(0, (-(-w // 3) - 1) * 3 + 3 - w)
            x = nn.functional.pad(x, (pad // 2, pad - pad // 2), value=float("-inf"))
            return nn.functional.max_pool2d(x, (1, 3), (1, 3))

    class FrameRNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.blocks = nn.ModuleList([Block(1, 32), Block(32, 64)])
            pooled = -(-n_bands // 3)  # each (1, 3) max-pool rounds the band count up
            width = -(-pooled // 3) * 64
            self.grus = nn.ModuleList([nn.GRU(width if i == 0 else 120, 60, batch_first=True, bidirectional=True)
                                       for i in range(3)])
            self.out = nn.Linear(120, len(CLASSES))

        def forward(self, spec):  # (batch, frames, bands)
            x = spec[:, None]
            for b in self.blocks:
                x = b(x)
            x = x.permute(0, 2, 3, 1).reshape(x.shape[0], x.shape[2], -1)  # per frame: band-major, then channel
            for g in self.grus:
                x, _ = g(x)
            return torch.sigmoid(self.out(x))

    return FrameRNN()


def _keras_gru(cell: dict[str, np.ndarray]) -> tuple[np.ndarray, ...]:
    """A Keras GRU cell (gates z, r, h; reset after the recurrent product) as PyTorch's (gates r, z, n)."""
    def reorder(m):  # columns z|r|h -> rows r|z|n
        z, r, h = np.split(m, 3, axis=-1)
        return np.concatenate([r, z, h], axis=-1).T

    return reorder(cell["kernel"]), reorder(cell["recurrent_kernel"]), reorder(cell["bias"][0]), reorder(cell["bias"][1])


def load_model(prefix: str | Path | None = None):
    """The network with ADTOF's weights (fetched on first use), in eval mode."""
    import torch

    from . import tf_checkpoint

    w = tf_checkpoint.read(prefix or fetch())
    v = lambda name: w[f"{name}/.ATTRIBUTES/VARIABLE_VALUE"]  # noqa: E731
    model = _network(filterbank().shape[0])
    state = {}
    for b in range(2):
        for c in range(2):
            conv, bn = f"layer_with_weights-0/layer_with_weights-{4 * b + 2 * c}", f"layer_with_weights-0/layer_with_weights-{4 * b + 2 * c + 1}"
            state[f"blocks.{b}.c{c + 1}.weight"] = v(f"{conv}/kernel").transpose(3, 2, 0, 1)  # (time, freq, in, out) -> (out, in, time, freq)
            state[f"blocks.{b}.c{c + 1}.bias"] = v(f"{conv}/bias")
            for ours, theirs in (("weight", "gamma"), ("bias", "beta"), ("running_mean", "moving_mean"), ("running_var", "moving_variance")):
                state[f"blocks.{b}.b{c + 1}.{ours}"] = v(f"{bn}/{theirs}")
    for g in range(3):
        for direction, suffix in (("forward_layer", ""), ("backward_layer", "_reverse")):
            cell = {k: v(f"layer_with_weights-{g + 1}/{direction}/cell/{k}") for k in ("kernel", "recurrent_kernel", "bias")}
            for name, arr in zip(("weight_ih_l0", "weight_hh_l0", "bias_ih_l0", "bias_hh_l0"), _keras_gru(cell)):
                state[f"grus.{g}.{name}{suffix}"] = arr
    state["out.weight"] = v("layer_with_weights-4/kernel").T
    state["out.bias"] = v("layer_with_weights-4/bias")
    missing, unexpected = model.load_state_dict(
        {k: torch.from_numpy(np.ascontiguousarray(a, dtype=np.float32)) for k, a in state.items()}, strict=False)
    missing = [k for k in missing if not k.endswith("num_batches_tracked")]
    if missing or unexpected:
        raise ValueError(f"ADTOF checkpoint doesn't fit the network: missing {missing}, unexpected {unexpected}")
    return model.eval()


_MODEL = None


def activations(y: np.ndarray, sr: int) -> np.ndarray:
    """frames (100 per second) x CLASSES: ADTOF's per-frame probability of each drum starting there."""
    global _MODEL
    import torch

    if sr != SR:
        import librosa

        y = librosa.resample(np.asarray(y, np.float32), orig_sr=sr, target_sr=SR)
    if _MODEL is None:
        _MODEL = load_model()
    spec = torch.from_numpy(spectrogram(y))
    # In windows with a warm-up on each side (the GRUs read both ways), so memory stays bounded on long mixes.
    out = np.empty((spec.shape[0], len(CLASSES)), np.float32)
    with torch.no_grad():
        for a in range(0, spec.shape[0], WINDOW):
            lo, hi = max(0, a - WARMUP), min(spec.shape[0], a + WINDOW + WARMUP)
            pred = _MODEL(spec[lo:hi][None])[0].numpy()
            out[a:a + WINDOW] = pred[a - lo:a - lo + min(WINDOW, spec.shape[0] - a)]
    return out


# --- Peak picking: madmom's NotePeakPickingProcessor as ADTOF configures it -------------------------

def peaks(act: np.ndarray, threshold: float) -> np.ndarray:
    """Frames where `act` reaches `threshold`, its 120 ms moving average and the 40 ms running maximum,
    keeping the first of peaks under 20 ms apart."""
    from scipy.ndimage import maximum_filter1d, uniform_filter1d

    pre_avg, post_avg, pre_max, post_max = int(round(0.1 * FPS)), int(round(0.01 * FPS)), int(round(0.02 * FPS)), int(round(0.01 * FPS))
    x = act * (act >= threshold)
    avg = uniform_filter1d(act, pre_avg + post_avg + 1, mode="constant", origin=(pre_avg - post_avg) // 2)
    x = x * (x >= avg)
    mx = maximum_filter1d(act, pre_max + post_max + 1, mode="constant", origin=(pre_max - post_max) // 2)
    frames = np.flatnonzero(x * (x == mx))
    keep: list[int] = []
    for f in frames:
        if not keep or f - keep[-1] > int(round(0.02 * FPS)):
            keep.append(int(f))
    return np.asarray(keep, dtype=int)


def onsets(y: np.ndarray, sr: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """{drum: (onset seconds, confidence 0.5-1)}: confidence maps ADTOF's threshold to 0.5 and 1 to 1."""
    act = activations(y, sr)
    out = {}
    for k, (name, thr) in enumerate(zip(CLASSES, THRESHOLDS)):
        f = peaks(act[:, k], thr)
        out[name] = (f / FPS, 0.5 + 0.5 * (act[f, k] - thr) / (1.0 - thr))
    return out
