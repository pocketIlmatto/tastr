"""The Rust engine (engine-rs/, docs/rust-engine.md) as a drop-in for the whole pipeline.

analyze_file hands every analysis to the Rust library, which decodes the file and makes the card itself; for
formats it can't decode (Opus), Python decodes and passes it the audio. The Python engine makes the card only
with TASTR_ENGINE=python (or options {"engine": "python"}), for a subset of stages, with the drum model, or
when the library isn't there. The card says which engine made it in extras["engine"].

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

ENGINES = ("python", "rust")
_LIB_NAMES = {"darwin": "libtastr_engine.dylib", "win32": "tastr_engine.dll"}
_lock = threading.Lock()
_lib: ctypes.CDLL | None = None
_lib_error: str | None = None


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
        for fn in (lib.tastr_card, lib.tastr_card_f32):
            fn.restype = ctypes.c_int32
            fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int32]
        lib.tastr_card_ptr.restype = ctypes.c_void_p
        lib.tastr_card_ptr.argtypes = []
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


def chosen(options: dict | None = None) -> str:
    """Which engine this analysis asks for: options["engine"], else TASTR_ENGINE, else rust. "python" picks
    the reference engine (tastr/engine/stages/), which the benchmarks compare against."""
    name = str((options or {}).get("engine") or os.environ.get("TASTR_ENGINE") or "rust").strip().lower()
    return name if name in ENGINES else "rust"


def _meter(beats_per_bar) -> int:
    return int(beats_per_bar) if beats_per_bar in (3, 4) else 0


def _result(lib: ctypes.CDLL, n: int, what: str) -> dict:
    # The JSON lives in the library's per-thread buffer until this thread's next call; ctypes.string_at copies it.
    if n < 0:  # -(length + 1) of the decoder's message, in the same buffer
        why = ctypes.string_at(lib.tastr_card_ptr(), -n - 1).decode(errors="replace") if n < -1 else ""
        raise RustDecodeError(f"the Rust engine could not decode {what}" + (f": {why}" if why else ""))
    return json.loads(ctypes.string_at(lib.tastr_card_ptr(), n).decode())


def card_from_file(path: str | os.PathLike, beats_per_bar=None) -> dict:
    """The card (TrackCard.to_dict() shape, "source" None) for an audio file, decoded by the Rust engine."""
    lib = _load()
    data = Path(path).read_bytes()
    ptr = lib.tastr_alloc(len(data))  # handed over: tastr_card frees it
    ctypes.memmove(ptr, data, len(data))
    return _result(lib, lib.tastr_card(ptr, len(data), _meter(beats_per_bar)), str(path))


def card_from_samples(y: np.ndarray, beats_per_bar=None) -> dict:
    """The same for mono audio already decoded at 22,050 Hz (any float array)."""
    lib = _load()
    y = np.ascontiguousarray(y, dtype="<f4")
    ptr = lib.tastr_alloc(y.nbytes)
    ctypes.memmove(ptr, y.ctypes.data, y.nbytes)
    return _result(lib, lib.tastr_card_f32(ptr, y.size, _meter(beats_per_bar)), "the samples")


# --- The WebAssembly build, for the browser (TASTR_BROWSER_ENGINE) ---

WASM_NAME = "tastr_engine.wasm"
_wasm_ids: dict[tuple[str, float], str] = {}


def wasm_path() -> Path | None:
    """The WebAssembly build the page loads: TASTR_ENGINE_WASM, else next to this file (the hosted image puts
    it there), else engine-rs's build folder (`cargo build --release --lib --target wasm32-unknown-unknown`)."""
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
    only from the build it serves now."""
    import hashlib

    p = wasm_path()
    if p is None:
        return None
    key = (str(p), p.stat().st_mtime)
    if key not in _wasm_ids:
        _wasm_ids[key] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return _wasm_ids[key]
