"""The views on Rust cards (engine-rs/src/stages/views.rs): waveform and spectrogram picture data."""

import base64
import struct
import zlib

import numpy as np
import pytest

from tastr.engine import rust

pytestmark = pytest.mark.skipif(not rust.available(), reason="needs the Rust engine built")

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


def views_for(y: np.ndarray) -> dict:
    return rust.card_from_samples(y.astype(np.float32))["extras"]["views"]


def test_waveform_is_peak_normalized_min_max_per_block():
    y = np.zeros(SR, dtype=np.float32)
    y[300] = 0.5
    y[600] = -0.25
    w = views_for(y)["waveform"]
    wave = np.frombuffer(base64.b64decode(w["data"]), dtype=np.int8).reshape(-1, 2)
    assert w["block"] == 256 and wave.shape[0] == int(np.ceil(y.size / 256))
    assert wave[1].tolist() == [0, 127]  # sample 300 is in block 1, and it's the peak
    assert wave[2].tolist() == [-64, 0]


def test_spectrogram_puts_a_tone_in_the_right_band():
    t = np.arange(SR * 3) / SR
    spec = views_for(0.5 * np.sin(2 * np.pi * 1000 * t))["spectrogram"]
    img = _png_pixels(spec["png"])
    assert img.shape == (spec["rows"], spec["frames"]) and spec["hop"] == 0.1 and abs(spec["frames"] - 31) <= 1
    row = int(np.log(1000 / spec["fmin"]) / np.log(spec["fmax"] / spec["fmin"]) * spec["rows"])
    mid = img[::-1, spec["frames"] // 2]  # the picture has high frequencies on top
    assert abs(int(mid.argmax()) - row) <= 1
    assert mid[5] < 100  # 50 Hz is far below the tone
