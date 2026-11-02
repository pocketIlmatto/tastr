# How hosting fits together

A map of the hosted Tastr: which service does what, what talks to what, how logins are checked and where
each setting lives. [deploy.md](deploy.md) has the setup steps and [hosting.md](hosting.md) what hosted mode
changes in the app.

Six services, each with one job:

| Service | Job |
| --- | --- |
| Cloudflare | DNS for your domain, HTTPS at the edge, and the login (Zero Trust Access) |
| Fly.io | Runs the web server (app `tastr-frienz` in `fly.toml`) and keeps the database on its volume, with uploads and stems too unless the audio bucket is on |
| Modal | Analysis, the on-request drum model and stem separation (app `tastr-web`, environment `friends`, `tastr/cloud/modal_web.py`) |
| Cloudflare R2 | Litestream's backup of the library database (bucket `tastr-app`) and, once `TASTR_AUDIO_BUCKET` is set, the uploads and stems (bucket `tastr-audio`) |
| GitHub Actions | The `deploy` workflow, which ships new code to Modal and Fly after the tests pass on main (or by hand) |
| Anthropic (Claude API) | "Ask Claude to change it" under Try this in › Strudel, once `ANTHROPIC_API_KEY` is set (`tastr/web/claude.py`) |

`tests/test_hosting_map.py` fails when a setting, secret or Modal function in the deploy files is missing
from this page. Update the page in the same change.

## The map

Everything a visitor does goes through Cloudflare first, except playing audio from the audio bucket: with
`TASTR_AUDIO_BUCKET` set, the server answers a request for a track's audio or a stem with a redirect to a
signed link that expires after an hour, and the browser (or strudel.cc) fetches the bytes from R2 directly.
Modal reads uploads from the same kind of link and writes stems to signed upload links. Without the setting,
audio stays on the Fly volume and passes through the server as before. The server is the only thing that talks
to the catalog lookups when they're turned on, and to the Claude API for "Ask Claude to change it" in Strudel.

```mermaid
flowchart TB
  F([Friend's browser])
  S([strudel.cc])
  PL([Music players: exported M3U8, XSPF, PLS playlists])
  subgraph CF[Cloudflare]
    DNS[DNS, proxied CNAME to the fly.dev name]
    ACC[Access app: one-time PIN login]
    BYP[Access app on /pub: Bypass]
  end
  subgraph FLY[Fly.io app tastr-frienz]
    WEB[Tastr web server, 512 MB, no DSP libraries, serves the Rust engine's and sql.js's WebAssembly builds]
    VOL[(Volume tastr_data at /data: tastr.db with unfinished jobs, optional AcousticBrainz index, and uploads and stems, only a cache once the audio bucket is on)]
    LS[Litestream]
  end
  subgraph MOD[Modal app tastr-web, environment friends]
    AN[analyze_stream, 2 CPUs, Rust engine library]
    AD[analyze_drums, 2 CPUs + PyTorch + drum model + Rust engine library]
    ST[stems, T4 GPU]
  end
  R2[(R2 bucket tastr-app: tastr.db)]
  R2A[(R2 bucket tastr-audio, if TASTR_AUDIO_BUCKET is set: uploads, stems, each card's waveform data)]
  GHT[GitHub Actions: tests workflow, on each push to main]
  GH[GitHub Actions: deploy workflow, after tests pass if app code changed]
  CAT([Catalogs, if TASTR_LOOKUP is set: FreqBlog, MusicBrainz])
  ANT([Claude API, if ANTHROPIC_API_KEY is set: claude-sonnet-5-5])

  F -->|your hostname| DNS --> ACC -->|request + signed token| WEB
  WEB -.->|a copy of your own library, for searches in the browser| F
  S -->|signed /pub link| DNS --> BYP --> WEB
  PL -->|signed /pub link| DNS
  WEB --- VOL
  VOL -.- LS
  LS -->|every database change| R2
  WEB -->|audio bytes, or a signed link to them| AN -->|each step, then the track card| WEB
  WEB -->|audio bytes or a signed link, on request| AD -->|track card| WEB
  WEB -->|audio bytes or a signed link| ST -->|stem files, or a note that they're in the bucket| WEB
  WEB -->|each upload, as it arrives| R2A
  F -.->|with TASTR_DIRECT_UPLOAD: the upload itself, on a signed link| R2A
  R2A -.->|signed links: audio, stems, waveform data| F
  WEB -->|each card's waveform data| R2A
  R2A -.->|signed links| S
  R2A -.->|signed links| PL
  R2A -->|the upload| AN
  ST -->|stems, to signed upload links| R2A
  WEB -.->|artist + title| CAT
  WEB -.->|Ask Claude: the Strudel code, the ask, BPM, key and sections| ANT
  GHT -->|passed| GH
  GH -->|modal deploy| MOD
  GH -->|fly deploy, with the commit and run link| FLY
  WEB -.->|every 15 min: runs on main, for the Admin log| GH
  WEB -.->|every 15 min: litestream snapshots, for the Admin log| R2
```

Without the audio bucket, uploads and stems live only on the Fly volume and expire after `TASTR_AUDIO_DAYS`,
and R2 backs up only the database (cards, edits, who owns what). With `TASTR_AUDIO_BUCKET` set
(`tastr/blobs.py`), every upload and stem is kept in that bucket under the same path it has under `/data`
(`uploads/ab12cd34/song.mp3`, `stems/<hash>/drums.flac`), the volume holds only copies that the six-hourly sweep
deletes once they're an hour old, and expiry deletes the bucket's copy. Modal keeps nothing after a job.
With `TASTR_DIRECT_UPLOAD` on as well, the page uploads each file straight to the bucket on a signed link, so the
file's bytes never pass through the server on the way in; the same sweep deletes uploads that got a link and were
never finished (after two hours).

## An upload, step by step

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser
  participant C as Cloudflare Access
  participant W as Fly: Tastr server
  participant M as Modal: analyze_stream
  participant R as R2
  participant A as R2 audio bucket (optional)
  B->>C: open your hostname
  C-->>B: asks for an email, sends a PIN
  B->>C: PIN
  C->>C: is the email in the Allow policy?
  C->>W: request + Cf-Access-Jwt-Assertion
  W->>W: verify the token, read the email
  Note over W: a stopped machine wakes here (a few seconds)
  B->>B: file size and length within the limits?
  opt TASTR_BROWSER_ENGINE is on
    B->>W: GET /engine/tastr_engine.wasm (once, then cached)
    B->>B: decode and analyze the whole track in a worker (engine.js)
  end
  alt TASTR_DIRECT_UPLOAD is on (needs the audio bucket)
    B->>W: POST /api/uploads: name and size
    W->>W: check limits (count, type, size, daily tracks, storage cap)
    W-->>B: a signed upload link (one hour), with an id
    B->>A: PUT the file to the link (3 at a time; a run-out link is renewed once)
    opt the bucket refuses the PUT
      B->>W: POST /api/uploads/{id}/failed (logged for the admin, source r2)
    end
    B->>W: POST /api/uploads/{id}/check
    W->>A: is it there, and how big?
    W->>W: read it back into the cache, check its length from the header
    B->>W: POST /api/analyze/uploaded, on its turn: the id (+ its card)
  else the upload goes through the server
    B->>C: upload a track (+ its card)
    C->>W: upload + token
    W->>W: check limits (size, length from the header), save the file on the volume
    opt TASTR_AUDIO_BUCKET is set
      W->>A: copy the upload up (refused with 503 if that fails)
    end
  end
  W-->>B: a job id (the upload is done)
  opt a card came and checks out
    W->>W: save the card, skip Modal
  end
  W->>M: audio bytes, or with the audio bucket a signed link, and options (meter, drum model)
  opt a signed link
    M->>A: download the upload
  end
  Note over M: cold start about 30 s after a quiet spell
  loop every 1-2 s until done
    M--)W: the step it's on (decode, core, sections...)
    B->>W: GET /api/analyze/jobs/{id}
    W-->>B: the step, for the progress bar
  end
  M-->>W: track card
  opt TASTR_AUDIO_BUCKET is set
    W->>A: the card's waveform data (the card keeps a marker)
  end
  W->>W: save the card in tastr.db, owner = the email
  W-)R: Litestream copies the change within seconds
  B->>W: GET /api/analyze/jobs/{id}
  W-->>B: done, with the track id
  opt the audio bucket is on
    W->>W: delete the volume's copy of the upload
    B->>W: GET /api/tracks/{id}/audio (playback)
    W-->>B: 302 to a signed link (one hour)
    B->>A: GET the audio, with Range for seeking
  end
```

Modal analyzes with the Rust engine (`tastr/cloud/rust_image.py` builds its library into the image); Python only
decodes the formats Rust can't (Opus) and runs the optional drum model.

With `TASTR_BROWSER_ENGINE` on, the page the Rust engine's WebAssembly build from the
server (`/engine/tastr_engine.wasm`, about 2 MB, cached by its hash), makes the whole card in a worker and uploads
it with the audio. The server stores that card without calling Modal when it checks out: made by the build the
server serves now, every stage at the engine's versions, as long as the file (`tastr/engine/browser_card.py`).
Otherwise, and for Opus files, tracks over 20 minutes or any failure in the browser, Modal analyzes the file as
usual. Both run the same Rust engine, so a card is the same wherever it was made. Off by default; see
[rust-engine.md](rust-engine.md).

The job's state is also kept in the database (`job_markers`, `tastr/markers.py`), so if the machine stops
mid-analysis, the page hears that it failed and can upload again, even on a fresh machine with a restored database. An app deployed before `analyze_stream` existed still
works: the server falls back to Modal's `analyze`, without steps.

Stems work the same way through Modal's `stems` function; the server saves the files it gets back on the
volume. "Detect drums with the model" in a track's menu re-analyzes it through `analyze_drums`, which is
`analyze` plus PyTorch and the pretrained drum model's weights, kept in its own image so everyday uploads
don't wait for PyTorch to load. A queued or running separation or drum model run leaves a marker in the
database (`job_markers`), so it resumes if the machine stops and starts again. Servers before that kept these as
files (`analysis-jobs/`, `stems/*.job`, `drum-jobs/*.job`); the first start after an upgrade reads them into the
database and deletes them.

## Which service handles which step

| Step | Service | Detail |
| --- | --- | --- |
| Name to address | Cloudflare DNS | A proxied CNAME for your hostname points at `<app>.fly.dev`. The registrar only points the domain's nameservers at Cloudflare. |
| HTTPS | Cloudflare and Fly | Cloudflare's certificate faces browsers; Fly's (`fly certs add`, checked with the `_acme-challenge` record) covers the Cloudflare-to-Fly hop, hence SSL/TLS **Full (strict)**. |
| Login | Cloudflare Access | Emails a one-time PIN, checks the Allow policy, adds a signed token to each request. |
| Checking the login | Tastr on Fly | `AccessVerifier` in `tastr/web/hosted.py` checks the token on every request and takes the email from it. |
| UI, API, per-person libraries, limits | Tastr on Fly | `tastr serve` in hosted mode (`TASTR_HOSTED`). |
| Searches (Simple and SQL) | The visitor's browser | `GET /api/query/library` sends a SQLite copy of the person's own library (admins: every library), once per page; sql.js (MIT, in the image at `TASTR_SQLJS_DIR`, served from `/sqljs/`) runs each search in a worker (`static/sql-worker.js`, `app/query-lib.js`). The server has no SQL endpoint in hosted mode. |
| Files | Fly volume `tastr_data` | Mounted at `/data` (`TASTR_HOME`): `tastr.db`, `uploads/`, `stems/`, and the optional AcousticBrainz lookup index `acousticbrainz-index.sqlite` (not backed up). |
| Audio and stems (optional) | R2 bucket `tastr-audio` | With `TASTR_AUDIO_BUCKET`, `TASTR_AUDIO_ENDPOINT` and the bucket's own token (`TASTR_AUDIO_ACCESS_KEY_ID`, `TASTR_AUDIO_SECRET_ACCESS_KEY`), `tastr/blobs.py` copies each upload up before analysis (each request signed by its own small S3 client, `tastr/s3.py`, not boto3), plays audio and stems from signed links (`TASTR_AUDIO_REGION` defaults to `auto`), and the page's Content-Security-Policy allows that host. The bucket's CORS rule lets any origin GET, since strudel.cc follows the redirect, and (for `TASTR_DIRECT_UPLOAD`) lets your hostname PUT. Each card's waveform and spectrogram data goes there too (`views/<track id>.json`, gzipped), so the database keeps a marker instead of about a megabyte per card; `/api/tracks/{id}/views` redirects to it. `tastr db-size` shows how big the database is and what's still inline. |
| Whole analysis (optional) | The visitor's browser | With `TASTR_BROWSER_ENGINE` on, `tastr/web/static/engine.js` runs the Rust engine's WebAssembly build in a worker and the upload carries the card. The Docker image builds the `.wasm` in a Rust stage and the server serves it from `TASTR_ENGINE_WASM`. |
| Analysis | Modal `analyze_stream` (or `analyze`) | Picked by `TASTR_ANALYSIS_BACKEND`, found by `TASTR_MODAL_APP` and `MODAL_ENVIRONMENT`. Yields each step as it starts, then the card. Stays warm 1 minute after a request. Cards always come from the Rust engine, whose library the image builds (`tastr/cloud/rust_image.py`). |
| Drum model (on request) | Modal `analyze_drums` | Used for the tracks someone asks for, through the same `TASTR_ANALYSIS_BACKEND`. CPU-only PyTorch; the weights (CC BY-NC-SA 4.0) are downloaded into the image at deploy time. The model runs in Python and the Rust library, built into this image too, makes the card. |
| Stems | Modal `stems` | Picked by `TASTR_STEMS_BACKEND`. Demucs on a T4 GPU. With the audio bucket on, it reads the upload from a signed link and uploads each stem to a signed link of its own, so stems never pass through the server. |
| HTTPS front for Modal (not used by Fly yet) | Modal `api` | A small web endpoint in front of the functions above, for the Cloudflare Worker, which can't run the Modal client (TAS-84). Only callers with a Modal proxy auth token get in. Every answer streams lines of JSON, with a blank line every 20 seconds so a long call stays open. A server uses it when `TASTR_MODAL_URL` is set, with the token as `TASTR_MODAL_KEY` and `TASTR_MODAL_SECRET` (`tastr/cloud/remote.py`). |
| Database backup | Litestream to R2 | `deploy/start.sh` restores onto an empty volume, then runs the server under `litestream replicate` (`deploy/litestream.yml`). |
| Shipping code | GitHub Actions | `.github/workflows/deploy.yml`: Modal first, then Fly, from main. Runs after `tests` passes on a push to main when something that ships changed since the last deploy, or by hand. |

## How security works

Four layers, each assuming the one before it might be bypassed.

1. **Cloudflare Access at the door.** Only emails in the Allow policy get a PIN. Deleting an address
   locks that person out at their next sign-in.
2. **Tastr checks Cloudflare's signature.** `<app>.fly.dev` skips Cloudflare, so the server trusts only a
   token signed by your team (`TASTR_ACCESS_TEAM_DOMAIN`) for this application (`TASTR_ACCESS_AUD`). No valid
   token means 401, except `/api/health` and signed `/pub/` links. Without both settings the server won't
   start.
3. **Per-person libraries and limits.** Each track belongs to the email that uploaded it; anyone else gets
   404. Uploads (size and length), stems, drum model runs, Claude asks, storage and concurrent analyses are capped; link downloads and API docs are off; searches
   run in the browser on a copy of the person's own library (admins: every library), with no owner or path columns, so the server runs no one's SQL; errors don't name server paths; responses carry CSP (`'wasm-unsafe-eval'` lets workers compile WebAssembly, nothing else) and HSTS headers.
4. **Signed Strudel and playlist links.** strudel.cc can't sign in, and neither can a music player opening a
   playlist exported from Library › List (`/api/lists/{id}/playlist.m3u8`, `.xspf`, `.pls`), so the `/pub`
   Bypass application lets them through Cloudflare. Tastr checks each link itself: an HMAC with
   `TASTR_LINK_SECRET` over one track id and an expiry 7 days out. Changing the secret ends every outstanding
   link. The playlist zip (`playlist.zip`) holds the audio itself, so it needs no link.

Keys are split so a leak stays small. The R2 token is scoped to `tastr-app`, not the benchmark bucket
`tastr-data`. Fly and GitHub each use their own Modal token, so either can be revoked alone. The `friends`
Modal environment keeps the app's spending apart from benchmarks.

## Where each setting lives

### In the repository

| File | What it sets |
| --- | --- |
| `fly.toml` | App name (`tastr-frienz`), region (`sjc`), the volume (`tastr_data` at `/data`), auto-stop, the `/api/health` check, a 512 MB VM, and the non-secret environment: `TASTR_HOSTED`, `TASTR_HOME`, `TASTR_ANALYSIS_BACKEND`, `TASTR_STEMS_BACKEND`, `TASTR_MODAL_APP`, `MODAL_ENVIRONMENT`, `TASTR_GITHUB_REPO` and `TASTR_GITHUB_WORKFLOWS` (whose GitHub Actions runs the Admin log shows, and which workflows) |
| `Dockerfile`, `deploy/start.sh`, `deploy/litestream.yml` | The server image (`.[hosted,cloud]`: no librosa, scipy or numba, since the server analyzes nothing itself; sql.js from npm, checksum-pinned, in `/app/sqljs`), the restore-then-replicate start, and the backup target |
| `tastr/cloud/modal_web.py` | Modal functions `analyze`, `analyze_stream`, `analyze_drums` and `stems`, their images, CPU, GPU, memory and timeouts |
| `.github/workflows/deploy.yml` | When it deploys (after `tests` on main, which files count), the deploy order, which repository secrets it uses, and the commit and run link it builds into the Fly image |

### Fly secrets

Set with `fly secrets set` (each change restarts the app); `fly secrets list` shows names only.

| Secret | Used for |
| --- | --- |
| `TASTR_ACCESS_TEAM_DOMAIN`, `TASTR_ACCESS_AUD` | Checking Cloudflare's token. Not secret, but kept out of the repository. |
| `TASTR_ADMIN_EMAILS` | Who gets the Admin tab and keeps audio forever |
| `TASTR_LINK_SECRET` | Signing Strudel links |
| `TASTR_STORAGE_GB` | The upload cap, kept below the volume size (with the audio bucket on, a cap on what the bucket holds) |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | The server's Modal token |
| `LITESTREAM_BUCKET`, `LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY` | The R2 backup |
| `TASTR_AUDIO_BUCKET`, `TASTR_AUDIO_ENDPOINT`, `TASTR_AUDIO_ACCESS_KEY_ID`, `TASTR_AUDIO_SECRET_ACCESS_KEY` (optional) | Keeping uploads and stems in R2 bucket `tastr-audio` ([deploy.md](deploy.md#2b-r2-bucket-for-audio-optional)). A token for that bucket only, not the backup's. |
| `TASTR_GITHUB_TOKEN` (optional) | Reading GitHub Actions runs for the Admin log while the repository is private: a fine-grained token with read-only Actions access to the repository |
| `TASTR_LOOKUP`, `FREQBLOG_API_KEY` (optional) | Catalog lookups ([lookup.md](lookup.md), [deploy.md](deploy.md#9b-catalog-lookups-optional)) |
| `ANTHROPIC_API_KEY` (optional) | "Ask Claude to change it" in Try this in › Strudel ([deploy.md](deploy.md#9c-ask-claude-in-strudel-optional)). Tastr's own Anthropic key; each ask's tokens are counted per person and priced on the Admin tab. Without it the panel says Ask is off. |

These have defaults and change only if you set a Fly secret of the same name ([hosting.md](hosting.md#settings)):
`TASTR_MAX_UPLOAD_MB`, `TASTR_MAX_MINUTES`, `TASTR_MAX_FILES`, `TASTR_DAILY_TRACKS`, `TASTR_MAX_ANALYSES`, `TASTR_BULK_AT_ONCE` (tracks a bulk action runs at once; defaults to `TASTR_MAX_ANALYSES`), `TASTR_DAILY_STEMS`,
`TASTR_DAILY_DRUMS`, `TASTR_DAILY_ASKS` (Claude asks per person per day, default 20), `TASTR_AUDIO_DAYS`, `TASTR_MODAL_WORKSPACE` (your Modal workspace name, so the Admin
tab's Modal link opens the app) and `TASTR_CF_ACCOUNT_ID` (your Cloudflare account id, so the Admin tab links to
Access's applications and its log of blocked requests; without it those links are hidden) and `TASTR_WATCH_MINUTES`
(default 15: how often the server checks GitHub Actions and the R2 backup for the Admin log; `0` turns it off). `TASTR_BROWSER_ENGINE=1` (off by default) has the browser make the whole card
with the Rust engine. `TASTR_DIRECT_UPLOAD=1` (off by default; needs the audio bucket and its PUT rule) has the
page upload straight to the bucket. Fly sets `FLY_APP_NAME` (the Admin tab's Fly logs link) and `FLY_IMAGE_REF`
(a change since the last start is logged as a deploy) on its own. The deploy workflow builds `TASTR_COMMIT`,
`TASTR_COMMIT_SUBJECT` and `TASTR_DEPLOY_RUN` into the image (Docker build arguments, not secrets), so the
Admin tab's deploy line names the commit and links to its GitHub Actions run.

### Dashboards

| Setting | Where |
| --- | --- |
| DNS records, SSL/TLS mode | Cloudflare > your domain > **DNS** and **SSL/TLS** |
| Who's invited | Cloudflare **Zero Trust > Access > Applications** > the Tastr application > its Allow policy (the Admin tab links here when `TASTR_CF_ACCOUNT_ID` is set) |
| Who was blocked, and why | **Zero Trust > Logs > Access** (linked from the Admin tab's blocked count) |
| Login method | **Zero Trust > Settings > Authentication** (One-time PIN) |
| AUD tag | The Tastr Access application's **Overview** |
| `/pub` bypass | A second Access application, same hostname, path `pub`, Bypass policy |
| Backup bucket and its token | Cloudflare **R2 Object Storage** > `tastr-app`; **Manage API tokens** |
| Audio bucket, its CORS rule and token | Cloudflare **R2 Object Storage** > `tastr-audio` > **Settings** > CORS Policy; **Manage API tokens** |
| Volume, logs, certificates | Fly dashboard, or `fly volumes list`, `fly logs`, `fly certs show <hostname>` |
| Modal usage and spend cap | Modal dashboard, environment `friends`; **Settings > Usage** |
| Modal tokens | Modal **Settings > API Tokens** (one for Fly, one for GitHub) |
| Anthropic key and spend limit | Anthropic Console **Settings > API Keys** and **Limits** (set a monthly spend limit for the key's workspace) |
| Deploy secrets | GitHub **Settings > Secrets and variables > Actions**: `FLY_API_TOKEN`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` |

## Coming next: Cloudflare Workers instead of Fly (TAS-84)

The Fly exit plan (option D) moves the web server onto a Cloudflare Python Worker. Nothing live uses it yet:
Fly serves everything until the move (TAS-51). What exists so far, in `worker/` (setup in `worker/README.md`):

```mermaid
flowchart LR
    B[Browser] -->|HTTPS, Access login| CF[Cloudflare edge]
    CF --> W["Worker tastr<br/>(worker/src/entry.py)"]
    W -->|/static/*| SA[Workers Static Assets<br/>worker/public/static]
    W -->|everything else| DO["Durable Object Library<br/>FastAPI app + the library database<br/>in its SQLite storage"]
    DO -.->|signing keys, hourly| AK[Access certs<br/>/cdn-cgi/access/certs]
    AL[Durable Object alarm] -->|expiry sweep, watch| DO
    DO -->|HTTPS, proxy auth token| MA["Modal api<br/>(analyze_stream, analyze_drums, stems)"]
```

- The Durable Object runs the same FastAPI app as Fly (`create_app(threads=False)`) on one event loop. The
  database is the object's own SQL storage through `tastr/do_sqlite.py` (`db.use_storage`), with 30 days of
  point-in-time recovery from Cloudflare instead of Litestream.
- Settings are the same names as on Fly: `vars` in `worker/wrangler.jsonc` for the plain ones (`TASTR_HOSTED`),
  `pywrangler secret put` for secrets.
- Logins: the Worker checks the same Access token, with the signing keys fetched by the Worker itself
  (`AccessKeys`) since PyJWT's own client can't open sockets there.
- Background work goes through `tastr/runner.py` on both hosts. On Fly it is threads, as before. On the Worker,
  jobs (analyses, stems, drum model runs) are asyncio tasks in lanes that queue them, and the repeating work (the
  expiry sweep every 6 hours, the watch) runs from the Durable Object's alarm, which wakes the object when it is
  due. Background analyses queue for an analysis slot in their lane there, since there are no threads to wait.
- Modal: the Worker calls the `api` web endpoint of `tastr-web` (`TASTR_MODAL_URL`, with a Modal proxy auth
  token as `TASTR_MODAL_KEY` and `TASTR_MODAL_SECRET`) through its own fetch, instead of the Modal client.
- The audio bucket works the same as on Fly: `tastr/s3.py` signs each request itself (no boto3), and
  `tastr/http.py` sends it with the Worker's fetch. Uploads, stems, signed links and zips (streamed from the
  bucket) all go through it. The Worker's own files are in memory and gone after a restart, so on the Worker
  the audio bucket is required, not optional.
- CI builds the Worker whenever the app or `worker/` changes (`worker/check.sh`, the last steps of
  `.github/workflows/test.yml`): it bundles it with its Pyodide packages, fails above 10 MiB gzipped (the Workers
  Paid limit; it is about 7.7 MiB), and runs it under `pywrangler dev` to check a few pages answer.
- Not on the Worker: Litestream (the Durable Object's own recovery replaces it), and the "Deployed ..." line
  in the Admin log, which needs a file that outlives a restart.

### Before the switch (TAS-51)

What has to happen before the Worker takes over from Fly, in order. Steps 1 to 4 are setup; the Worker is
reachable only at a custom domain (`workers_dev` and `preview_urls` are off in `worker/wrangler.jsonc`), so
nothing is public until step 5.

| Step | Where | What |
| --- | --- | --- |
| 1 | Cloudflare **Workers & Pages > Plans** | Workers Paid ($5 a month): the bundle (about 7.7 MiB gzipped) is over the Free plan's 3 MiB limit. |
| 2 | Modal **Settings > Proxy Auth Tokens** | A token for the Worker. Its id and secret become `TASTR_MODAL_KEY` and `TASTR_MODAL_SECRET`. `TASTR_MODAL_URL` is the `api` endpoint's address, which `modal deploy` prints (in the deploy workflow's log). |
| 3 | Cloudflare **My Profile > API Tokens**, then GitHub **Settings > Secrets and variables > Actions** | A token from the "Edit Cloudflare Workers" template, saved as the GitHub secret `CLOUDFLARE_API_TOKEN`. Then **Actions > deploy-worker > Run workflow** (`.github/workflows/deploy-worker.yml`) builds, checks and deploys the Worker. |
| 4 | `pywrangler secret put NAME` in `worker/`, or the Worker's **Settings > Variables and Secrets** | The same secrets as on Fly, without the Fly, Modal-token and Litestream ones: `TASTR_ACCESS_TEAM_DOMAIN`, `TASTR_ACCESS_AUD`, `TASTR_ADMIN_EMAILS`, `TASTR_LINK_SECRET`, `TASTR_STORAGE_GB`, the four `TASTR_AUDIO_*` ones (required on the Worker), the three `TASTR_MODAL_*` ones above, and the optional ones you use. |
| 5 | Cloudflare **Workers > tastr > Settings > Domains & Routes**, and Access | Try it first at a second hostname (say `tastr-next.pocketknows.com`) added to the same Access application. |
| 6 | Fly, then the Worker | `fly ssh console -C "tastr library-to-bucket"` copies the database to the audio bucket as `import/library.db` (`tastr/library_move.py`). The Worker imports it on its next start if its library is still empty, once (it notes the import in `meta`); to make it start again, redeploy. Audio and stems are in the bucket already. |
| 7 | Cloudflare **Workers > tastr > Settings > Domains & Routes** | Right after step 6, point `tastr.pocketknows.com` at the Worker, keeping the Access application in front of it. Anything changed on Fly between steps 6 and 7 stays on Fly, so do them together. Then stop Fly. |

## Common tasks

| To | Do this |
| --- | --- |
| Invite someone | Add their email to the Access Allow policy. Nothing on Fly changes. |
| Remove someone | Delete their email from the policy. Their tracks stay until you delete them. |
| Ship new code | Merge to main. Once `tests` passes, `deploy` runs on its own (skipped when the merge only touched docs, tests or Markdown). To deploy anyway: **Actions > deploy > Run workflow**. |
| See usage | The Admin tab (today by hour, 7 or 30 days, or all time); Modal's usage page for actual cost. |
| Let someone upload more today | Admin tab > ✎ on their row > Today only. The same row raises their Claude asks. |
| Recover a lost volume | Create a volume named `tastr_data` and deploy. The database comes back from R2; audio and stems come back too when the audio bucket is on, and are lost otherwise. |
| Move old files into the audio bucket | `fly ssh console -C "tastr bucket-backfill --dry-run"`, then without `--dry-run`. Copies uploads and stems from before the bucket was on, and moves older cards' waveform data out of the database. Safe to run again. |
| Rotate a key | Make a new one at its source, `fly secrets set` it (or update the GitHub secret), then revoke the old one. |

## Why it's sometimes slow

To stay cheap, the Fly machine stops after a few idle minutes and the next request wakes it (a few seconds).
Modal stops its container 1 minute after the last analysis, so the first upload after a quiet spell waits
about 30 seconds more. A drum model run usually starts its own, larger container, so it takes a minute
or two.
