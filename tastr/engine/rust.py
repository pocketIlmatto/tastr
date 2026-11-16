"""The Rust engine (engine-rs/, docs/rust-engine.md), which makes every card.

analyze_file hands every analysis to the Rust library, which decodes the file and makes the card itself; for
formats it can't decode (Opus), Python decodes and passes it the audio. The card says which build made it in
extras["engine"]. The benchmarks call it too, for whole cards and, through `bench`, for single steps.

The library is engine-rs's cdylib, loaded with ctypes through its small C interface (the same one the
WebAssembly build exports). It is found at TASTR_ENGINE_LIB, else next to this file, else in engine-rs's
release build folder (`cd engine-rs && cargo build --release`).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
from pathlib import Path

import numpy as np

# The stages every Rust card runs, with their versions (engine-rs/src/stages/mod.rs), as card.stages_run lists
# them. Kept here so a server without the native library (the hosted web server checks browser cards against
# it) knows them; tests/test_rust_switch.py checks them against the library.
STAGES = {"core": "0.6.0", "sections": "0.2.0", "tags": "0.2.0", "suggestions": "0.5.0", "views": "0.2.0"}
# What changed in a stage version, in one sentence, for the card's "Engine 0.5.0 · latest 0.6.0" tooltip. Add a line
# when a stage's version goes up with a change people would notice in their cards.
STAGE_NEWS = {"core": {"0.6.0": "BPM now comes from a line fit through the beats, so it matches the beat grid"}}

SR = 22050  # the engine's sample rate (engine-rs SR): audio handed to it must be at this rate
_LIB_NAMES = {"darwin": "libtastr_engine.dylib", "win32": "tastr_engine.dll"}
_lock = threading.Lock()
_lib: ctypes.CDLL | None = None
_lib_error: str | None = None


def _ver(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))  # "0.6" is 0.6.0


def stale_stages(stages: dict[str, str] | None) -> list[str]:
    """The stages (in STAGES order) a card ran at an older version than this engine's, or not at all. A card with
    any is from an older engine: re-analyzing it brings it up to date."""
    stages = stages or {}
    return [k for k, v in STAGES.items() if k not in stages or _ver(stages[k]) < _ver(v)]


class RustEngineError(RuntimeError):
    pass


class RustDecodeError(RustEngineError):
    """The library is there but can't decode this file (Opus)."""


def lib_name() -> str:
    return _LIB_NAMES.get(sys.platform, "libtastr_engine.so")


def lib_path() -> Path | None:
    """Where the library is, or None."""
    env = os.environ.get("TASTR_ENGINE_LIB")
    if env:
        return Path(env)
    for p in (Path(__file__).parent / lib_name(),
              Path(__file__).resolve().parents[2] / "engine-rs" / "target" / "release" / lib_name()):
        if p.is_file():
            return p
    return None


def _load() -> ctypes.CDLL:
    global _lib, _lib_error
    with _lock:
        if _lib is not None:
            return _lib
        if _lib_error is not None:
            raise RustEngineError(_lib_error)
        path = lib_path()
        try:
            if path is None or not path.is_file():
                raise OSError("the Rust engine library isn't built (cd engine-rs && cargo build --release)")
            lib = ctypes.CDLL(str(path))
        except OSError as e:
            _lib_error = str(e)
            raise RustEngineError(_lib_error) from e
        lib.tastr_alloc.restype = ctypes.c_void_p
        lib.tastr_alloc.argtypes = [ctypes.c_size_t]
        for fn in (lib.tastr_card_hits, lib.tastr_card_f32_hits):
            fn.restype = ctypes.c_int32
            fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int32, ctypes.c_void_p, ctypes.c_size_t]
        lib.tastr_card_ptr.restype = ctypes.c_void_p
        lib.tastr_card_ptr.argtypes = []
        lib.tastr_bench.restype = ctypes.c_int32
        lib.tastr_bench.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        lib.tastr_version.restype = ctypes.c_void_p
        lib.tastr_version_len.restype = ctypes.c_size_t
        _lib = lib
        return lib


def available() -> bool:
    try:
        _load()
        return True
    except RustEngineError:
        return False


def version() -> str:
    lib = _load()
    return ctypes.string_at(lib.tastr_version(), lib.tastr_version_len()).decode()


def _meter(beats_per_bar) -> int:
    return int(beats_per_bar) if beats_per_bar in (3, 4) else 0


def _result(lib: ctypes.CDLL, n: int, what: str) -> dict:
    # The JSON lives in the library's per-thread buffer until this thread's next call; ctypes.string_at copies it.
    if n < 0:  # -(length + 1) of the decoder's message, in the same buffer
        why = ctypes.string_at(lib.tastr_card_ptr(), -n - 1).decode(errors="replace") if n < -1 else ""
        raise RustDecodeError(f"the Rust engine could not decode {what}" + (f": {why}" if why else ""))
    return json.loads(ctypes.string_at(lib.tastr_card_ptr(), n).decode())


MODEL_LANES = ("kick", "hat", "tom", "cymbal", "snare")  # engine-rs drums::MODEL_LANES, the order drum hits go in


def _hits(drum_hits: dict | None) -> np.ndarray:
    """adt.onsets output as the flat float64 buffer tastr_card_hits reads: per MODEL_LANES lane its count, onset
    seconds, then confidences (float32, as the model gives them). Empty for no drum model."""
    if not drum_hits:
        return np.zeros(0)
    parts = []
    for lane in MODEL_LANES:
        t, conf = drum_hits[lane]
        t = np.asarray(t, dtype=float).ravel()
        parts += [[t.size], t, np.asarray(conf, dtype=np.float32).astype(float).ravel()]
    return np.ascontiguousarray(np.concatenate(parts), dtype="<f8")


def card_from_file(path: str | os.PathLike, beats_per_bar=None, drum_hits: dict | None = None) -> dict:
    """The card (TrackCard.to_dict() shape, "source" None) for an audio file, decoded by the Rust engine.
    `drum_hits` (adt.onsets) are the optional drum model's hits, which replace those drum lanes."""
    lib = _load()
    data = Path(path).read_bytes()
    ptr = lib.tastr_alloc(len(data))  # handed over: tastr_card_hits frees it
    ctypes.memmove(ptr, data, len(data))
    h = _hits(drum_hits)
    return _result(lib, lib.tastr_card_hits(ptr, len(data), _meter(beats_per_bar), h.ctypes.data, h.size), str(path))


def card_from_samples(y: np.ndarray, beats_per_bar=None, drum_hits: dict | None = None) -> dict:
    """The same for mono audio already decoded at 22,050 Hz (any float array)."""
    lib = _load()
    y = np.ascontiguousarray(y, dtype="<f4")
    ptr = lib.tastr_alloc(y.nbytes)
    ctypes.memmove(ptr, y.ctypes.data, y.nbytes)
    h = _hits(drum_hits)
    return _result(lib, lib.tastr_card_f32_hits(ptr, y.size, _meter(beats_per_bar), h.ctypes.data, h.size),
                   "the samples")


def bench(op: str, *arrays, **params) -> dict:
    """One benchmark step in the Rust engine (engine-rs/src/bench.rs lists the ops and what each reads): the
    arrays go in as one float64 buffer, in order (matrices row-major); `params` become `key=value` words."""
    lib = _load()
    line = " ".join([op] + [f"{k}={v}" for k, v in params.items()]).encode()
    parts = [np.asarray(a, dtype=float).ravel() for a in arrays]
    data = np.ascontiguousarray(np.concatenate(parts) if parts else np.zeros(0), dtype="<f8")
    n = lib.tastr_bench(line, len(line), data.ctypes.data, data.size)
    if n < 0:
        why = ctypes.string_at(lib.tastr_card_ptr(), -n - 1).decode(errors="replace")
        raise RustEngineError(f"{op}: {why}")
    return json.loads(ctypes.string_at(lib.tastr_card_ptr(), n).decode())


# --- The WebAssembly build, for the browser (TASTR_BROWSER_ENGINE) ---

WASM_NAME = "tastr_engine.wasm"
_wasm_ids: dict[tuple[str, float], str] = {}


def wasm_path() -> Path | None:
    """The WebAssembly build the page loads: TASTR_ENGINE_WASM, else next to this file, else engine-rs's build folder (`cargo build --release --lib --target wasm32-unknown-unknown`)."""
    env = os.environ.get("TASTR_ENGINE_WASM")
    if env:
        return Path(env) if Path(env).is_file() else None
    for p in (Path(__file__).parent / WASM_NAME,
              Path(__file__).resolve().parents[2] / "engine-rs" / "target" / "wasm32-unknown-unknown" / "release" / WASM_NAME):
        if p.is_file():
            return p
    return None


def wasm_id() -> str | None:
    """A content hash of the WebAssembly build: the page names the build it ran, and the server takes a card
    only from the build it serves now. TASTR_ENGINE_WASM_ID gives it where the file isn't on this server's disk
    (the Worker serves it as a static asset; worker/build.py records its hash)."""
    import hashlib

    if os.environ.get("TASTR_ENGINE_WASM_ID"):
        return os.environ["TASTR_ENGINE_WASM_ID"]
    p = wasm_path()
    if p is None:
        return None
    key = (str(p), p.stat().st_mtime)
    if key not in _wasm_ids:
        _wasm_ids[key] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return _wasm_ids[key]
