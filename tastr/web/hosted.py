"""Hosted mode: what changes when Tastr is shared with invited friends behind Cloudflare Access.

`tastr serve` on your own machine runs as before. With TASTR_HOSTED=1 the server:

- refuses every request without a valid Cloudflare Access token (the `Cf-Access-Jwt-Assertion` header,
  signed by your Access team), so a request that skips Cloudflare gets nothing. Only /api/health answers;
- turns off link analysis (yt-dlp) and the SQL box;
- limits uploads (size, count, audio types, total storage, tracks per person per day), stem jobs per
  person per day, and how many analyses run at once;
- hides error details that could name server paths, and sends security headers (CSP, no framing).

Settings come from environment variables (see `Settings.from_env` and docs/hosting.md).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HEALTH_PATH = "/api/health"
TOKEN_HEADER = "cf-access-jwt-assertion"


class ConfigError(ValueError):
    """Hosted mode is on but not set up well enough to run safely."""


@dataclass(frozen=True)
class Settings:
    hosted: bool = False
    access_team_domain: str = ""  # e.g. "yourteam.cloudflareaccess.com"
    access_aud: str = ""  # the Access application's audience (AUD) tag
    max_upload_mb: float = 100
    max_files: int = 20
    daily_tracks: int = 30
    storage_gb: float = 10
    max_analyses: int = 2
    daily_stems: int = 10

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        hosted = env.get("TASTR_HOSTED", "").strip().lower() in ("1", "true", "yes", "on")
        s = cls(
            hosted=hosted,
            access_team_domain=env.get("TASTR_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/"),
            access_aud=env.get("TASTR_ACCESS_AUD", "").strip(),
            max_upload_mb=float(env.get("TASTR_MAX_UPLOAD_MB", cls.max_upload_mb)),
            max_files=int(env.get("TASTR_MAX_FILES", cls.max_files)),
            daily_tracks=int(env.get("TASTR_DAILY_TRACKS", cls.daily_tracks)),
            storage_gb=float(env.get("TASTR_STORAGE_GB", cls.storage_gb)),
            max_analyses=int(env.get("TASTR_MAX_ANALYSES", cls.max_analyses)),
            daily_stems=int(env.get("TASTR_DAILY_STEMS", cls.daily_stems)),
        )
        if hosted and not (s.access_team_domain and s.access_aud):
            # Fail closed: a hosted server that can't check logins must not start.
            raise ConfigError("TASTR_HOSTED needs TASTR_ACCESS_TEAM_DOMAIN and TASTR_ACCESS_AUD (docs/hosting.md).")
        return s

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    def limits(self) -> dict:
        return {"max_upload_mb": self.max_upload_mb, "max_files": self.max_files, "daily_tracks": self.daily_tracks,
                "daily_stems": self.daily_stems}


class AccessVerifier:
    """Checks a Cloudflare Access JWT against the team's published signing keys; returns the user's email."""

    def __init__(self, team_domain: str, aud: str):
        import jwt  # PyJWT, from the `hosted` extra

        self._jwt = jwt
        self.issuer = f"https://{team_domain}"
        self.aud = aud
        self.keys = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True)

    def __call__(self, token: str) -> str:
        key = self.keys.get_signing_key_from_jwt(token).key
        claims = self._jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
                                  options={"require": ["exp", "iat", "aud", "iss"]})
        email = claims.get("email")
        if not email:
            raise ValueError("token has no email")  # service tokens carry no email; friends always do
        return email.lower()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def stored_bytes(home: Path) -> int:
    """Bytes of uploaded audio and stems under TASTR_HOME, for the storage cap."""
    total = 0
    for sub in ("uploads", "stems"):
        d = home / sub
        if d.is_dir():
            total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return total


class AnalysisSlots:
    """At most `n` analyses at once; later ones wait their turn (up to `timeout` seconds)."""

    def __init__(self, n: int, timeout: float = 600):
        self.sem = threading.BoundedSemaphore(max(1, n))
        self.timeout = timeout

    def __enter__(self):
        if not self.sem.acquire(timeout=self.timeout):
            raise TimeoutError
        return self

    def __exit__(self, *exc):
        self.sem.release()


def inline_script_hashes(html: str) -> list[str]:
    """CSP hashes for the page's inline <script> blocks (the look picker in <head>), so no 'unsafe-inline'."""
    out = []
    for body in re.findall(r"<script>(.*?)</script>", html, flags=re.S):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        out.append(f"'sha256-{digest}'")
    return out


def security_headers(script_hashes: list[str]) -> dict[str, str]:
    csp = "; ".join([
        "default-src 'self'",
        "script-src 'self' " + " ".join(script_hashes),
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",  # the UI sets style attributes
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Strict-Transport-Security": "max-age=31536000",
    }
