"""Builds the Rust engine (engine-rs/, docs/rust-engine.md) into a Modal image, for TASTR_ENGINE=rust.

The image copies engine-rs's sources, installs a pinned Rust toolchain, builds the library, keeps only the .so
at /opt/tastr/libtastr_engine.so (TASTR_ENGINE_LIB points there) and removes the toolchain again, all in one
build step, so the image grows by about 3 MB. Modal rebuilds the step only when engine-rs changes.
"""

from __future__ import annotations

from pathlib import Path

RUST_TOOLCHAIN = "1.97.0"  # the version CI and the P0-P3 measurements used
ENGINE_DIR = Path(__file__).resolve().parents[2] / "engine-rs"
LIB = "/opt/tastr/libtastr_engine.so"


def with_rust_engine(image):
    """`image` plus the Rust engine's library. Call it before adding local Python sources."""
    return (
        image.apt_install("curl", "build-essential")
        .add_local_dir(ENGINE_DIR, "/opt/tastr/engine-rs", copy=True, ignore=["target", "target/**"])
        .run_commands(
            f"curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain {RUST_TOOLCHAIN}"
            " && cd /opt/tastr/engine-rs && $HOME/.cargo/bin/cargo build --release --lib --locked"
            f" && cp target/release/libtastr_engine.so {LIB}"
            " && cd / && rm -rf /opt/tastr/engine-rs $HOME/.cargo $HOME/.rustup"
        )
        .env({"TASTR_ENGINE_LIB": LIB})
    )
