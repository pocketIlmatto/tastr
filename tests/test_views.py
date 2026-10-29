"""Tests for the `views` stage (waveform and spectrogram picture data)."""

import base64
import struct
import zlib

import numpy as np

from tastr.engine.card import Source, TrackCard
from tastr.engine.pipeline import run_pipeline
from tastr.engine.stages import views

SR = 22050


def _png_pixels(data_url: str) -> np.ndarray:
    png = base64.b64decode(data_url.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h, depth, color = struct.unpack(">IIBB", png[16:26])
    assert (depth, color) == (8, 0)
    idat, pos = b"", 8
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        kind = png[pos + 4:pos + 8]
        assert zlib.crc32(png[pos + 4:pos + 8 + n]) == struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0]
        if kind == b"IDAT":
            idat += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    rows = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, w + 1)
    assert (rows[:, 0] == 0).all()  # filter type None
    return rows[:, 1:]


def test_waveform_is_peak_normalized_min_max_per_block():
    y = np.zeros(1000, dtype=np.float32)
    y[300] = 0.5
    y[600] = -0.25
    w = views.waveform(y, block=256)
    assert w.shape == (4, 2) and w.dtype == np.int8
    assert w[1].tolist() == [0, 127]  # sample 300 is in block 1, and it's the peak
    assert w[2].tolist() == [-64, 0]
    assert views.waveform(np.zeros(0, dtype=np.float32)).shape == (0, 2)


def test_spectrogram_puts_a_tone_in_the_right_band():
    t = np.arange(SR * 3) / SR
    y = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    s = views.spectrogram(y, SR)
    assert s.shape[0] == views.SPEC_ROWS and abs(s.shape[1] - 31) <= 1
    row = int(np.log(1000 / views.SPEC_FMIN) / np.log(views.SPEC_FMAX / views.SPEC_FMIN) * views.SPEC_ROWS)
    mid = s[:, s.shape[1] // 2]
    assert abs(int(mid.argmax()) - row) <= 1
    assert mid.max() == 252 and mid[5] < 100  # 50 Hz is far below the tone


def test_stage_writes_decodable_views():
    t = np.arange(SR * 2) / SR
    y = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    card = run_pipeline(y, SR, TrackCard(source=Source(kind="file", uri="/x.wav")), stages=["views"])
    v = card.extras["views"]
    wave = np.frombuffer(base64.b64decode(v["waveform"]["data"]), dtype=np.int8).reshape(-1, 2)
    assert wave.shape[0] == int(np.ceil(y.size / v["waveform"]["block"])) and wave.max() == 127
    spec = v["spectrogram"]
    img = _png_pixels(spec["png"])
    assert img.shape == (spec["rows"], spec["frames"]) and spec["hop"] == 0.1
    assert img[-1].max() < img.max()  # bottom row is 40 Hz, quieter than the 220 Hz tone
