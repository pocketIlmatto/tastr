"""The engine crate as crates.io gets it (engine-rs/Cargo.toml, docs/ci.md "Engine release")."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine-rs"


def test_crate_licence_matches_the_repository():
    # The crate ships its own copy, since crates.io gets only engine-rs/.
    assert (ENGINE / "LICENSE").read_text() == (ROOT / "LICENSE").read_text()


def test_crate_ships_code_and_notices_only():
    package = tomllib.loads((ENGINE / "Cargo.toml").read_text())["package"]
    include = package["include"]
    for name in ("LICENSE", "NOTICE", "README.md", "Cargo.toml"):
        assert name in include
    assert all(p.startswith("src/") or "/" not in p for p in include), include
    assert package["license"] == "MIT"
    assert "rust-version" in package
