"""The optional drum model (tastr.engine.adt): fallback without PyTorch, mapping onto the lanes, the pieces
written from ADTOF's settings, and the checkpoint conversion. Model weights are never needed here."""

import importlib.util

import numpy as np
import pytest

from tastr.engine import adt, drums
from tastr.engine.bench.drums import _synth_ctx
from tastr.engine.bench.synth import make_drum_track
from tastr.engine.stages import suggestions
from tastr.engine.stages.suggestions import _drum_grid, _step_grid


def no_torch(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(adt.importlib.util, "find_spec", lambda name, *a: None if name == "torch" else real(name, *a))


def test_runs_only_when_asked(monkeypatch, tmp_path):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path))
    monkeypatch.delenv("TASTR_DRUM_MODEL", raising=False)
    monkeypatch.setattr(adt.importlib.util, "find_spec", lambda name, *a: object())  # as if torch were installed
    assert adt.installed() and not adt.wanted(None)  # PyTorch alone (e.g. from tastr[stems]) doesn't turn it on
    for name in adt.FILES:
        (adt.models_dir()).mkdir(parents=True, exist_ok=True)
        (adt.models_dir() / name).write_bytes(b"")
    assert not adt.wanted({})  # nor do downloaded weights: everyday analysis keeps the built-in lanes
    assert adt.wanted({"drum_model": True})  # "Detect drums with the model" on one track
    monkeypatch.setenv("TASTR_DRUM_MODEL", "adtof")
    assert adt.wanted(None)  # every analysis (the drum benches)
    monkeypatch.setenv("TASTR_DRUM_MODEL", "off")
    assert not adt.wanted({"drum_model": True})
    no_torch(monkeypatch)
    monkeypatch.setenv("TASTR_DRUM_MODEL", "adtof")
    assert not adt.installed() and not adt.wanted({"drum_model": True})


def test_drum_model_cli_without_torch(monkeypatch, capsys):
    from tastr.cli import main

    no_torch(monkeypatch)
    assert main(["drum-model", "status"]) == 2
    out = capsys.readouterr()
    assert "CC BY-NC-SA" in out.out and "tastr[drums]" in out.err


def fake_model(monkeypatch, hits):
    monkeypatch.setattr(adt, "wanted", lambda options: True)
    monkeypatch.setattr(adt, "onsets", lambda y, sr: {k: (np.asarray(t, float), np.full(len(t), 1.0)) for k, t in hits.items()})


@pytest.mark.audio
def test_fallback_keeps_the_built_in_lanes(monkeypatch):
    no_torch(monkeypatch)
    y, _ = make_drum_track({"kick": "x...x...x...x...", "hat": "..x...x...x...x."}, bpm=120)
    ctx = _synth_ctx(y, 120)
    assert drums.model_lanes(ctx) is None
    acts = drums.lane_activations(ctx, np.arange(10))
    assert tuple(acts) == drums.LANES  # no cymbal lane
    grid = _drum_grid(ctx, _step_grid(ctx, 1 + len(y) // ctx.hop))
    assert set(grid) == set(drums.LANES)


@pytest.mark.audio
def test_model_onsets_map_onto_the_groove(monkeypatch):
    bpm, step = 120, 0.125
    # the pattern tests/test_drums.py checks the built-in snare on
    y, _ = make_drum_track({"kick": "x.......x.......", "snare": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."}, bpm=bpm)
    bars = 8
    at = lambda steps: [(b * 16 + s) * step for b in range(bars) for s in steps]  # noqa: E731
    fake_model(monkeypatch, {"kick": at([0, 8]), "snare": [], "tom": at([14]), "hat": at(range(0, 16, 2)),
                             "cymbal": at([0])})
    ctx = _synth_ctx(y, bpm)
    lanes = drums.model_lanes(ctx)
    f = int(round(14 * step * ctx.sr / ctx.hop))
    assert lanes["tom"][f] > 0 and lanes["tom"][f - 3:f].max() == 0
    grid = _drum_grid(ctx, _step_grid(ctx, 1 + len(y) // ctx.hop))
    assert "cymbal" in grid
    hits = lambda g: g["pattern"].replace("o", "x") if g["detected"] else "." * 16  # noqa: E731
    assert hits(grid["tom"]) == "..............x."
    assert hits(grid["cymbal"]) == "x..............."
    assert hits(grid["kick"]) == "x.......x......."
    assert hits(grid["hat"]) == "x.x.x.x.x.x.x.x."
    assert hits(grid["snare"]) == "....x.......x..."  # still ours


@pytest.mark.audio
def test_suggestions_pad_and_help_for_the_cymbal(monkeypatch):
    y, _ = make_drum_track({"kick": "x...x...x...x...", "snare": "....x.......x..."}, bpm=120)
    at = lambda steps: [(b * 16 + s) * 0.125 for b in range(8) for s in steps]  # noqa: E731
    fake_model(monkeypatch, {"kick": at([0, 4, 8, 12]), "snare": [], "tom": [], "hat": [], "cymbal": at([0, 8])})
    ctx = _synth_ctx(y, 120)
    grid = _drum_grid(ctx, _step_grid(ctx, 1 + len(y) // ctx.hop))
    sugs = suggestions._suggest(ctx, grid, {}, {})
    groove = next(s for s in sugs if s.title == "Detected groove")
    assert groove.data["pads"]["cymbal"] == "C#2"
    assert "ADTOF" in groove.data["lane_help"]["cymbal"] and "ADTOF" in groove.data["lane_help"]["kick"]


def test_filterbank_and_spectrogram_shape():
    fb = adt.filterbank()
    assert fb.shape == (84, 1024)  # what the checkpoint's first GRU expects (84 bands -> 10 after pooling)
    assert np.allclose(fb.sum(axis=1), 1.0)
    spec = adt.spectrogram(np.zeros(adt.SR, np.float32), fb)
    assert spec.shape == (100, 84) and np.all(spec == 0)


def test_peak_picking_like_madmom():
    act = np.zeros(300)
    act[[50, 52, 150, 250]] = [0.9, 0.5, 0.3, 0.1]  # a peak, a smaller one 20 ms later, one, and noise
    assert list(adt.peaks(act, 0.22)) == [50, 150]


def test_keras_gru_matches_pytorch():
    torch = pytest.importorskip("torch")
    rng = np.random.default_rng(0)
    n_in, h = 5, 4
    cell = {"kernel": rng.normal(size=(n_in, 3 * h)), "recurrent_kernel": rng.normal(size=(h, 3 * h)),
            "bias": rng.normal(size=(2, 3 * h))}
    x = rng.normal(size=(7, n_in))

    def keras(x):  # Keras GRU, reset_after=True, gates z | r | h
        Wz, Wr, Wh = np.split(cell["kernel"], 3, 1)
        Uz, Ur, Uh = np.split(cell["recurrent_kernel"], 3, 1)
        bz, br, bh = np.split(cell["bias"][0], 3)
        cz, cr, ch = np.split(cell["bias"][1], 3)
        s, out = np.zeros(h), []
        sig = lambda v: 1 / (1 + np.exp(-v))  # noqa: E731
        for t in x:
            z, r = sig(t @ Wz + bz + s @ Uz + cz), sig(t @ Wr + br + s @ Ur + cr)
            s = z * s + (1 - z) * np.tanh(t @ Wh + bh + r * (s @ Uh + ch))
            out.append(s)
        return np.array(out)

    gru = torch.nn.GRU(n_in, h, batch_first=True)
    w_ih, w_hh, b_ih, b_hh = adt._keras_gru(cell)
    gru.load_state_dict({k: torch.tensor(v, dtype=torch.float32) for k, v in
                         zip(("weight_ih_l0", "weight_hh_l0", "bias_ih_l0", "bias_hh_l0"), (w_ih, w_hh, b_ih, b_hh))})
    with torch.no_grad():
        ours = gru(torch.tensor(x, dtype=torch.float32)[None])[0][0].numpy()
    assert np.allclose(ours, keras(x), atol=1e-5)


def test_windowed_inference_matches_one_pass(monkeypatch):
    torch = pytest.importorskip("torch")
    torch.manual_seed(0)
    net = adt._network(84).eval()
    monkeypatch.setattr(adt, "_MODEL", net)
    monkeypatch.setattr(adt, "WINDOW", 300)
    monkeypatch.setattr(adt, "WARMUP", 150)
    y = np.random.default_rng(0).normal(scale=0.1, size=adt.SR * 10).astype(np.float32)
    with torch.no_grad():
        whole = net(torch.from_numpy(adt.spectrogram(y))[None])[0].numpy()
    assert np.allclose(adt.activations(y, adt.SR), whole, atol=2e-3)


def test_checkpoint_loads_when_cached():
    """Reads the real weights if a previous run downloaded them (no network in tests)."""
    pytest.importorskip("torch")
    prefix = adt.models_dir() / adt.CHECKPOINT
    if not all((prefix.parent / n).is_file() for n in adt.FILES):
        pytest.skip("ADTOF weights not downloaded")
    model = adt.load_model(prefix)
    import torch

    with torch.no_grad():
        out = model(torch.zeros(1, 50, 84))
    assert out.shape == (1, 50, 5) and float(out.max()) < 0.5  # silence: no drums


def test_fetch_rejects_a_bad_download(monkeypatch, tmp_path):
    monkeypatch.setenv("TASTR_HOME", str(tmp_path))

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"not the weights"

    monkeypatch.setattr(adt.urllib.request, "urlopen", lambda url, timeout: Resp())
    with pytest.raises(ValueError, match="checksum"):
        adt.fetch()
    assert not any(adt.models_dir().glob("*.index"))
