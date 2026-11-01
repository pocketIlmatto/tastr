"""Hosted mode: what changes when Tastr is shared with invited friends behind Cloudflare Access.

`tastr serve` on your own machine runs as before. With TASTR_HOSTED=1 the server:

- refuses every request without a valid Cloudflare Access token (the `Cf-Access-Jwt-Assertion` header,
  signed by your Access team), so a request that skips Cloudflare gets nothing. Only /api/health answers;
- turns off link analysis (yt-dlp) and the SQL box;
- limits uploads (size, length, count, audio types, total storage, tracks per person per day), stem jobs per
  person per day, and how many analyses run at once;
- hides error details that could name server paths, and sends security headers (CSP, no framing);
- gives each person their own library: tracks carry the uploader's email, and every /api/tracks/{id}
  request for someone else's track answers 404. TASTR_ADMIN_EMAILS may open /api/admin/usage;
- hands out signed links that work for a week under /pub/, so strudel.cc (which can't pass the login)
  can fetch a track's audio and stems for "Try this in Strudel" (`LinkSigner`);
- deletes uploaded audio and stems after TASTR_AUDIO_DAYS (cards stay; admins' audio is kept).

Settings come from environment variables (see `Settings.from_env` and docs/hosting.md).
"""

from __future__ import annotations

import logging
import base64
import hashlib
import hmac
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import blobs

log = logging.getLogger(__name__)

HEALTH_PATH = "/api/health"
TOKEN_HEADER = "cf-access-jwt-assertion"
PUBLIC_PREFIX = "/pub/"  # signed links; Cloudflare Access lets these through (docs/hosting.md)
LINK_DAYS = 7


class ConfigError(ValueError):
    """Hosted mode is on but not set up well enough to run safely."""


@dataclass(frozen=True)
class Settings:
    hosted: bool = False
    access_team_domain: str = ""  # e.g. "yourteam.cloudflareaccess.com"
    access_aud: str = ""  # the Access application's audience (AUD) tag
    max_upload_mb: float = 100
    # Analysis memory grows with length (about 66 MB a minute with the Rust engine) and Modal's `analyze` has
    # 2 GB. 30-minute tracks ran there (2026-10-31); 25 leaves headroom. Longer files are refused up front
    # instead of failing after the upload.
    max_minutes: float = 25
    max_files: int = 20
    daily_tracks: int = 30
    storage_gb: float = 10
    max_analyses: int = 2
    # How many tracks a bulk action (reload, re-analyze, look up) runs at once in the browser; 0 = automatic:
    # max_analyses on a hosted server, about half the CPU cores at home. Stems and the drum model always run one at a time.
    bulk_at_once: int = 0
    daily_stems: int = 10
    daily_drums: int = 10  # "Detect drums with the model" runs per person per UTC day
    admins: tuple[str, ...] = ()  # emails that may open the admin view
    audio_days: float = 30  # uploaded audio and stems are deleted after this; 0 keeps them
    link_secret: str = ""  # signs the /pub/ links; without it they stop working when the server restarts
    # For the admin view's links to the logs and to tell a deploy from a restart. Fly sets FLY_APP_NAME and
    # FLY_IMAGE_REF itself; TASTR_MODAL_WORKSPACE (your Modal workspace name) makes the Modal link go to the app.
    fly_app: str = ""
    fly_image: str = ""
    modal_env: str = ""
    modal_workspace: str = ""
    # Which commit is running and the GitHub Actions run that deployed it (the deploy workflow bakes them into
    # the image), for the admin log's deploy lines.
    commit: str = ""
    commit_subject: str = ""
    deploy_run: str = ""
    # Your Cloudflare account id, for the Admin tab's links to the Access dashboard (who can sign in, and the
    # Access log of blocked requests). Not secret, but kept out of the repository.
    cf_account_id: str = ""
    # The admin log's GitHub Actions lines: the repository ("owner/name"; empty turns them off) and a read-only
    # token, needed only while the repository is private. Checked with the R2 backup every `watch_minutes`.
    github_repo: str = ""
    github_token: str = ""
    github_workflows: tuple[str, ...] = ()  # only these workflows' runs are logged (names or file names); none = nothing
    watch_minutes: float = 15

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        hosted = env.get("TASTR_HOSTED", "").strip().lower() in ("1", "true", "yes", "on")
        s = cls(
            hosted=hosted,
            access_team_domain=env.get("TASTR_ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/"),
            access_aud=env.get("TASTR_ACCESS_AUD", "").strip(),
            max_upload_mb=float(env.get("TASTR_MAX_UPLOAD_MB", cls.max_upload_mb)),
            max_minutes=float(env.get("TASTR_MAX_MINUTES", cls.max_minutes)),
            max_files=int(env.get("TASTR_MAX_FILES", cls.max_files)),
            daily_tracks=int(env.get("TASTR_DAILY_TRACKS", cls.daily_tracks)),
            storage_gb=float(env.get("TASTR_STORAGE_GB", cls.storage_gb)),
            max_analyses=int(env.get("TASTR_MAX_ANALYSES", cls.max_analyses)),
            bulk_at_once=int(env.get("TASTR_BULK_AT_ONCE", cls.bulk_at_once)),
            daily_stems=int(env.get("TASTR_DAILY_STEMS", cls.daily_stems)),
            daily_drums=int(env.get("TASTR_DAILY_DRUMS", cls.daily_drums)),
            admins=tuple(e.strip().lower() for e in env.get("TASTR_ADMIN_EMAILS", "").split(",") if e.strip()),
            audio_days=float(env.get("TASTR_AUDIO_DAYS", cls.audio_days)),
            link_secret=env.get("TASTR_LINK_SECRET", "").strip(),
            fly_app=env.get("FLY_APP_NAME", "").strip(),
            fly_image=env.get("FLY_IMAGE_REF", "").strip(),
            modal_env=env.get("MODAL_ENVIRONMENT", "").strip(),
            modal_workspace=env.get("TASTR_MODAL_WORKSPACE", "").strip(),
            commit=env.get("TASTR_COMMIT", "").strip(),
            commit_subject=env.get("TASTR_COMMIT_SUBJECT", "").strip(),
            deploy_run=env.get("TASTR_DEPLOY_RUN", "").strip(),
            cf_account_id=env.get("TASTR_CF_ACCOUNT_ID", "").strip(),
            github_repo=env.get("TASTR_GITHUB_REPO", "").strip(),
            github_token=env.get("TASTR_GITHUB_TOKEN", "").strip(),
            github_workflows=tuple(w.strip() for w in env.get("TASTR_GITHUB_WORKFLOWS", "").split(",") if w.strip()),
            watch_minutes=float(env.get("TASTR_WATCH_MINUTES", cls.watch_minutes)),
        )
        if hosted and not (s.access_team_domain and s.access_aud):
            # Fail closed: a hosted server that can't check logins must not start.
            raise ConfigError("TASTR_HOSTED needs TASTR_ACCESS_TEAM_DOMAIN and TASTR_ACCESS_AUD (docs/hosting.md).")
        return s

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    def bulk_lanes(self) -> int:
        """Tracks a bulk action runs at once: TASTR_BULK_AT_ONCE, else max_analyses hosted (the server's slots queue
        the rest anyway), else about half this machine's cores, 1 to 4."""
        if self.bulk_at_once > 0:
            return self.bulk_at_once
        if self.hosted:
            return max(1, self.max_analyses)
        return max(1, min(4, (os.cpu_count() or 2) // 2))

    def limits(self) -> dict:
        return {"max_upload_mb": self.max_upload_mb, "max_minutes": self.max_minutes, "max_files": self.max_files, "daily_tracks": self.daily_tracks,
                "daily_stems": self.daily_stems, "daily_drums": self.daily_drums, "storage_gb": self.storage_gb, "max_analyses": self.max_analyses,
                "audio_days": self.audio_days}


    def cf_links(self) -> dict[str, str]:
        """The Cloudflare Zero Trust pages for Access applications and the Access log, or {} without an account id."""
        if not re.fullmatch(r"[0-9a-f]{32}", self.cf_account_id):
            return {}
        base = f"https://one.dash.cloudflare.com/{self.cf_account_id}"
        return {"apps": f"{base}/access/apps", "access_logs": f"{base}/logs/access"}

    def log_links(self, modal_app: str) -> dict[str, str]:
        """Where an admin can dig into the logs: Fly's monitoring page for the app, and the Modal app."""
        out = {}
        if self.fly_app:
            out["fly"] = f"https://fly.io/apps/{self.fly_app}/monitoring"
        if self.modal_workspace and self.modal_env:
            out["modal"] = f"https://modal.com/apps/{self.modal_workspace}/{self.modal_env}/deployed/{modal_app}"
        else:
            out["modal"] = "https://modal.com/apps"
        return out


def names(files: list[str], most: int = 3) -> str:
    """'a.mp3, b.mp3 and 2 more', for log lines."""
    shown = ", ".join(files[:most])
    return shown + (f" and {len(files) - most} more" if len(files) > most else "")


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


class LinkSigner:
    """Links to one track's audio and stems that work without the login for `days`, for strudel.cc.

    The token is `<expiry>.<HMAC of track id and expiry>`, so it can't be moved to another track or extended.
    """

    def __init__(self, secret: str | bytes, days: float = LINK_DAYS):
        self.key = secret.encode() if isinstance(secret, str) else secret
        self.ttl = int(days * 86400)

    def _sig(self, track_id: int, exp: int) -> str:
        mac = hmac.new(self.key, f"{track_id}:{exp}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac[:18]).decode()

    def token(self, track_id: int, now: float | None = None) -> str:
        exp = int((now or time.time()) + self.ttl)
        return f"{exp}.{self._sig(track_id, exp)}"

    def check(self, token: str, track_id: int, now: float | None = None) -> bool:
        exp, _, sig = token.partition(".")
        if not exp.isdigit() or int(exp) < (now or time.time()):
            return False
        return hmac.compare_digest(sig, self._sig(track_id, int(exp)))


def expire_audio(conn: sqlite3.Connection, root: Path, days: float, keep: tuple[str, ...] = (),
                 now: float | None = None) -> list[int]:
    """Delete uploaded audio under `root` older than `days`, except tracks owned by `keep` (the admins).
    With the audio bucket on (tastr/blobs.py), its copy goes too, and its upload time decides the age.

    Cards stay, so the track still shows everything but playback. Stems go too, unless another track
    still has the same file. Returns the ids of tracks whose audio went.
    """
    from .. import stems

    cutoff = (now or time.time()) - days * 86400
    root = root.resolve()
    inbucket = blobs.listing(blobs.key(root / "x").rsplit("/", 1)[0] + "/") if blobs.key(root / "x") else {}
    gone = []
    for tid, owner, path, sha in conn.execute("SELECT id, owner, local_path, sha256 FROM tracks WHERE local_path IS NOT NULL").fetchall():
        p = Path(path).resolve()
        if owner in keep or root not in p.parents:
            continue
        stored = inbucket.get(blobs.key(p) or "")
        when = stored[1] if stored else (p.stat().st_mtime if p.is_file() else None)
        if when is None or when >= cutoff:
            continue
        blobs.remove(p)
        if p.parent != root and p.parent.is_dir() and not any(p.parent.iterdir()):
            p.parent.rmdir()
        gone.append((tid, sha))
    for tid, sha in gone:
        paths = conn.execute("SELECT local_path FROM tracks WHERE sha256 = ? AND local_path IS NOT NULL", (sha,)).fetchall()
        if not any(blobs.exists(lp) for (lp,) in paths):
            stems.remove(stems.stems_dir(sha, tid))
    return [tid for tid, _ in gone]


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def file_bytes(path: str | None) -> int:
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0


def audio_minutes(path: str | Path) -> float | None:
    """How long an audio file is, from its header (no decoding), or None if that can't be read."""
    from ..filemeta import read_file_meta

    seconds = read_file_meta(path)[1].get("duration")
    if not seconds:
        try:
            import soundfile

            seconds = soundfile.info(str(path)).duration
        except Exception as e:  # neither reader knows the length; the upload size cap still applies
            log.info("can't read the length of %s from its header: %s", Path(path).name, e)
            return None
    return seconds / 60


def stored_bytes(home: Path) -> int:
    """Bytes of uploaded audio and stems under TASTR_HOME, for the storage cap: what the audio bucket holds
    when that is on (tastr/blobs.py), since the disk is then only a cache."""
    if blobs.enabled():
        return blobs.stored_bytes()
    total = 0
    for sub in ("uploads", "stems"):
        d = home / sub
        if d.is_dir():
            total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    return total


class AnalysisSlots:
    """At most `n` analyses at once; later ones wait their turn (up to `timeout` seconds)."""

    def __init__(self, n: int, timeout: float = 600):
        self.max = max(1, n)
        self.sem = threading.BoundedSemaphore(self.max)
        self.timeout = timeout
        self.lock = threading.Lock()
        self.running = self.waiting = 0

    def __enter__(self):
        with self.lock:
            self.waiting += 1
        got = self.sem.acquire(timeout=self.timeout)
        with self.lock:
            self.waiting -= 1
            self.running += got
        if not got:
            raise TimeoutError
        return self

    def __exit__(self, *exc):
        with self.lock:
            self.running -= 1
        self.sem.release()

    def status(self) -> dict:
        with self.lock:
            return {"running": self.running, "waiting": self.waiting, "max": self.max}


def inline_script_hashes(html: str) -> list[str]:
    """CSP hashes for the page's inline <script> blocks (the look picker in <head>), so no 'unsafe-inline'."""
    out = []
    for body in re.findall(r"<script>(.*?)</script>", html, flags=re.S):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        out.append(f"'sha256-{digest}'")
    return out


def security_headers(script_hashes: list[str], media_origin: str | None = None) -> dict[str, str]:
    """`media_origin`: where else audio comes from (the audio bucket's signed links, tastr/blobs.py)."""
    extra = f" {media_origin}" if media_origin else ""
    csp = "; ".join([
        "default-src 'self'",
        "script-src 'self' " + " ".join(script_hashes),
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",  # the UI sets style attributes
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:" + extra,
        "connect-src 'self'" + extra,
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
