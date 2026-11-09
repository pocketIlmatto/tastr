# Sharing Tastr with a few people (hosted mode)

`tastr serve` is a single-person app: it binds to 127.0.0.1 and has no login. To share it with a few
invited people, run it in **hosted mode** behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/),
which sends a one-time code to each invited email address. Cloudflare decides who gets in; Tastr checks
Cloudflare's signed token on every request, so a request that reaches the server without going through
Cloudflare gets nothing.

This page covers what hosted mode changes and how to configure it. [deploy.md](deploy.md) sets it up on
a Cloudflare Worker, with analysis on Modal and audio in R2. [hosting-map.md](hosting-map.md) shows how the
pieces fit together.

## What hosted mode changes

| | Local (`tastr serve`) | Hosted (`TASTR_HOSTED=1`) |
| --- | --- | --- |
| Login | none | Cloudflare Access token required on every request except `/api/health` and signed `/pub/` links |
| Link analysis (yt-dlp) | on (with the `links` extra) | off; the link field is hidden |
| Searches (Simple and SQL) | the server runs them (`/api/query`): whole database, 5 s limit | the browser runs them with sql.js on a copy of the person's own library (`/api/query/library`: no owner, path or log columns; admins get every library), so there's no server limit; a slow query can be stopped |
| Uploads | any size | audio types only, size, count, per-person daily and total storage limits |
| Library | one, yours | one per person: each sees and changes only the tracks they uploaded |
| Saved searches (Explore) | one list | one list per person |
| Lists (Explore › Blend) | one set | one set per person |
| SQL history (Explore) | in the browser | in the browser, kept apart per signed-in person |
| Deleting a track | removes its stems | also removes its uploaded audio |
| Uploaded audio and stems | kept | kept, unless `TASTR_AUDIO_DAYS` or an admin sets an expiry for that person (cards stay; admins' audio is always kept) |
| Try this in Strudel | strudel.cc fetches audio from `tastr serve` | strudel.cc fetches it through a signed link that works for a week |
| Admin view (`/api/admin/usage`) | none | usage per person, for `TASTR_ADMIN_EMAILS` |
| Stem separation | unlimited | per-person daily limit |
| Detect drums with the model | unlimited (needs `tastr[drums]`) | per-person daily limit; runs on Modal's `analyze_drums` |
| Analyses at once | unlimited | capped; extra ones wait their turn |
| Analysis errors | full message | "Couldn't analyze this file." (details go to the server log) |
| Security headers | none | CSP, no framing, nosniff, HSTS |
| API docs (`/docs`) | on | off |

In both modes `/api/analyze-url` accepts only `http(s)` links, never a path on the server.

Each track belongs to the email that uploaded it. A request for someone else's track answers 404, the
same as a track that doesn't exist. Two people can upload the same file; each gets their own track,
and they share its stems, which stay until the last copy is deleted.

## Settings

Install the extra that checks tokens: `pip install -e '.[hosted]'`. Then set:

| Variable | Default | What it does |
| --- | --- | --- |
| `TASTR_HOSTED` | off | `1` turns hosted mode on |
| `TASTR_ACCESS_TEAM_DOMAIN` | (required) | your Access team domain, e.g. `yourteam.cloudflareaccess.com` |
| `TASTR_ACCESS_AUD` | (required) | the Access application's Audience (AUD) tag |
| `TASTR_MAX_UPLOAD_MB` | 100 | largest file accepted |
| `TASTR_MAX_MINUTES` | 25 | longest track accepted, read from the file's header before analysis (see [below](#when-an-upload-fails)) |
| `TASTR_MAX_FILES` | 20 | files per upload |
| `TASTR_DAILY_TRACKS` | 30 | tracks each person can upload per UTC day |
| `TASTR_STORAGE_GB` | 10 | uploads are refused once uploaded audio and stems reach this. With the audio bucket on it is a spending cap (R2 bills about $0.015 per GB-month, so 10 GB is about $0.15 a month), not a disk guard |
| `TASTR_MAX_ANALYSES` | 2 | analyses that run at once; more wait in a queue |
| `TASTR_BULK_AT_ONCE` | `TASTR_MAX_ANALYSES` | tracks a bulk action (reload, re-analyze, look up) runs at once from the browser. Stems and the drum model always go one at a time. At home it defaults to about half the CPU cores, 1 to 4 |
| `TASTR_DAILY_STEMS` | 10 | stem separations each person can start per UTC day |
| `TASTR_DAILY_DRUMS` | 10 | drum model runs ("Detect drums with the model") each person can start per UTC day |
| `TASTR_DAILY_ASKS` | 20 | "Ask Claude to change it" requests (Try this in › Strudel) each person can make per UTC day; an admin can change one person's on the Admin tab |
| `ANTHROPIC_API_KEY` | none | Tastr's own Anthropic API key, for "Ask Claude to change it". Without it Ask is off. Keep it secret |
| `TASTR_ADMIN_EMAILS` | none | comma-separated emails that may open the admin view |
| `TASTR_AUDIO_DAYS` | 0 | the default for how long uploaded audio and stems are kept: `0` keeps them, a number deletes them that many days after upload. An admin can set each person's own on the Admin tab, which wins |
| `TASTR_CF_ACCOUNT_ID` | none | your Cloudflare account id (32 hex characters, from the dashboard URL); the Admin tab then links to Access's applications and its log of blocked requests (and, with `TASTR_WORKER_NAME`, to the Worker's logs). Not secret; `worker/wrangler.jsonc` sets it |
| `TASTR_WORKER_NAME` | none | the Cloudflare Worker this server runs as (`worker/wrangler.jsonc` sets `tastr`): the Admin log's source and its link to the Worker's logs |
| `TASTR_GITHUB_REPO` | none | `owner/name` of the repository whose GitHub Actions runs on main the Admin log shows (`worker/wrangler.jsonc` sets it) |
| `TASTR_GITHUB_WORKFLOWS` | none | which workflows' runs the Admin log shows, comma-separated names or file names (`worker/wrangler.jsonc`: `tests,deploy`); none logs no runs |
| `TASTR_GITHUB_TOKEN` | none | a fine-grained token with read-only Actions access, needed while that repository is private, and on the Cloudflare Worker (GitHub's limit without one is per IP address, which Cloudflare shares) |
| `TASTR_WATCH_MINUTES` | 15 | how often the server checks GitHub Actions for the Admin log; `0` turns it off |
| `TASTR_MODAL_WORKSPACE` | none | your Modal workspace name; the Admin tab's Modal link then opens the `tastr-web` app instead of Modal's app list |
| `TASTR_LINK_SECRET` | random | signs the Strudel links; set it (e.g. `openssl rand -hex 32`) so links survive a restart. Keep it secret |
| `TASTR_AUDIO_BUCKET` | none | keep uploads and stems in this S3-compatible bucket (R2), with the disk as a cache; see [Audio in a bucket](#audio-in-a-bucket) |
| `TASTR_AUDIO_ENDPOINT` | none | the bucket's S3 endpoint, for R2 `https://<account id>.r2.cloudflarestorage.com` |
| `TASTR_AUDIO_ACCESS_KEY_ID`, `TASTR_AUDIO_SECRET_ACCESS_KEY` | none | a token that can read and write that bucket only. Keep them secret |
| `TASTR_AUDIO_REGION` | `auto` | the region to sign for; R2 takes `auto` |
| `TASTR_DIRECT_UPLOAD` | off | `1` has the page upload each file straight to the audio bucket on a signed link instead of through the server; see [Audio in a bucket](#audio-in-a-bucket). Needs `TASTR_AUDIO_BUCKET` and a CORS rule that lets your hostname PUT ([deploy.md](deploy.md#2-r2-bucket-for-audio)) |
| `TASTR_BROWSER_ENGINE` | off | `1` has each visitor's browser make the whole card with the Rust engine's WebAssembly build; the server checks it and skips analysis ([ARCHITECTURE.md](ARCHITECTURE.md#browser-engine)). Needs the image's `TASTR_ENGINE_WASM` |
| `TASTR_SQLJS_DIR` | none | where sql.js (`sql-wasm.js`, `sql-wasm.wasm`) is, for searches in the browser; the image sets `/app/sqljs`. Without it a hosted page says search couldn't start |

With `TASTR_HOSTED=1` but no team domain or AUD tag, `tastr serve` refuses to start rather than run
without a login check.

Neither the team domain nor the AUD tag is a secret, but keep them out of the repository like any
deployment setting.

## Setting up Cloudflare Access

You need a domain on Cloudflare (Tastr will live at a hostname on it, such as `tastr.example.com`).

1. In the Cloudflare dashboard, open **Zero Trust**. The first time, pick a team name and the Free plan
   (up to 50 users). Your team domain is `<team name>.cloudflareaccess.com`.
2. **Settings > Authentication**: make sure **One-time PIN** is enabled.
3. **Access > Applications > Add an application > Self-hosted**. Set the application domain to the
   hostname Tastr will use, and a session duration (24 hours to a week).
4. Add a policy: action **Allow**, include **Emails**, and list the people you invite. To remove
   someone later, delete their address here.
5. Save, then open the application's **Overview** and copy the **Application Audience (AUD) Tag** into
   `TASTR_ACCESS_AUD`.

6. Add a second **Self-hosted** application for the same hostname with the path `pub`, and a policy with
   action **Bypass**, include **Everyone**. strudel.cc fetches a track's audio from there without a login;
   Tastr itself checks each of those links (see [Strudel links](#strudel-links)). Access applies the most
   specific path, so the rest of the site still needs the login.

Cloudflare then forwards each signed-in request with a `Cf-Access-Jwt-Assertion` header, which Tastr
verifies against your team's public keys at `https://<team domain>/cdn-cgi/access/certs`.

## Admin view

For the emails in `TASTR_ADMIN_EMAILS`, `GET /api/admin/usage` returns, per person: tracks, uploaded
audio and stem bytes, last upload, last request since the server started, and today's tracks, stem jobs
and visits (page loads), plus 30 days of daily counts, the limits, total storage against the cap and
the stem jobs and analyses running or waiting. Everyone else gets 404. `/api/config` says `"admin": true` for admins.

It also returns `recent`, the last 20 lines of the activity log, `problems`, the last 20 that weren't OK,
`log_links`, where to read the Worker's and Modal's logs, and `blocked_today`, how many requests
were refused today (UTC) for a missing or invalid Access token. The log (`events` table) records uploads,
stem jobs started, re-analyses, first sign-ins, limits hit, failed analyses, deploys and restarts. A failure keeps only the file
name and a short reason, never the error text, which can name server paths. Rows older than 90 days are
dropped as new ones come in. Your own machine keeps no log.

`GET /api/admin/usage?range=1|7|30|all` also returns `series`: per person, tracks, stem jobs, visits and
bytes stored for each UTC hour of today (`range=1`) or each day of the range (`all` goes back at most a year),
and each person's `caps` and `override`. A visit is a person's first request after 30 minutes without one. Usage
per hour (`usage_hours`) is kept two days. Storage per person (`storage_days`) is recorded at the first request
of each UTC day, every 6 hours and whenever the Admin tab opens; the day keeps its last value.

`GET /api/admin/events?type=&who=&source=&from=&to=&problems=&limit=` filters the activity log on the server:
`type` is one of upload, analysis, stems, limit, error, signin, blocked, deploy, backup or admin; `who` an email
or `-` for lines with no person; `source` one of app, modal, github, cloudflare or r2 (or flyio, on lines from before the move off Fly); `from` and `to`
UTC days, both included. Blocked requests come back as one Cloudflare line per day. Deploys are logged by the
server when a new `TASTR_COMMIT` first starts.

Every `TASTR_WATCH_MINUTES` the server also logs (tastr/web/watch.py): each GitHub Actions run that finished on
main ("tests passed on main (3 min): ..."), linked to the run, for the workflows in `TASTR_GITHUB_WORKFLOWS`
when `TASTR_GITHUB_REPO` is set. The first check logs only the last day of runs. A server that was idle
catches up when it wakes.

`PUT /api/admin/limits/{email}` with `{"daily_tracks": 45, "daily_stems": null, "scope": "today"}` changes
one person's daily caps (`null` = the server default; `scope` `today` ends at 00:00 UTC, `standing` lasts until
changed), and `DELETE` puts them back on the defaults. The upload and stem checks use the change, and
`/api/config` returns the caller's own caps. Each change is logged as an admin line.

Sharing some of your tracks with a friend: `GET /api/admin/copy-tracks?to=EMAIL&count=10` shows, without changing
anything, whether they have signed in yet and which of your most recently added tracks (with audio, and not already
theirs) a copy would take (or, with `ids=1,2,3`, exactly those tracks of yours, each marked `they_have` and `has_audio`); `POST /api/admin/copy-tracks` with `{"to": EMAIL, "track_ids": [...]}` copies them
(tastr/library_copy.py), skipping the ones they have and the ones without audio on the server. In the app it's
Library › Select › Actions › **Send copies to…** (admins only). A copy has the same analysis and points at the same audio file and stems; your own edits,
labels, cues and playlists stay yours. The file stays while either track uses it, and the audio expiry keeps it while
an admin's track does.

Admins see an **Admin** tab in the header with all of this: tiles for people, tracks, stem jobs,
storage, analyses running and an estimated 30-day Modal cost, a per-person chart of the last 30 days,
the people table with today's quotas, the activity log, the limits and the blocked count. Nobody else
gets the tab, and on your own machine there is none.

### View as someone else

An admin can see the app exactly as another person sees it: their library, limits and screens.
`POST /api/admin/view-as {"email": ...}` (someone who has used the server, not yourself) sets an
HttpOnly `tastr_view_as` cookie for 2 hours; while it's set, every request from that admin runs as
that person, so `/api/config` reports their email, their limits and `"admin": false`, plus
`"viewing_as": {"email", "by", "read_only": true}` for the page's banner. It is read-only: anything
but a GET (and the read-only POSTs `/api/query`, `/api/files/zip`, `/api/tracks/known`) answers 403
"Read-only while viewing as …". `DELETE /api/admin/view-as` stops. The cookie is ignored for anyone
who isn't in `TASTR_ADMIN_EMAILS`, visits still count for the admin, and each start is an admin line
in the log.

## When an upload fails

The page checks a file's size and length against the limits before it uploads it, and the server checks
again, so a file that is too big or too long fails at once with a message that says which limit. A failed
analysis shows "Couldn't analyze this file", with the step it stopped at and, when Modal stopped it for using
too much memory or time, that reason.

Why there's a length limit: analysis memory grows with the track's length, about 66 MB a minute with the Rust
engine (measured locally: 3 min 235 MB, 10 min 0.7 GB, 20 min 1.4 GB; the Python engine took twice that).
Modal's `analyze` has 2 GB. On 2026-10-31 the deployed function analyzed 12, 20, 25 and 30-minute MP3s
without trouble, in 24 to 54 s, so the limit is 25 minutes to leave headroom. A one-hour mix would need about
4 GB. Raise `TASTR_MAX_MINUTES` past 30 only together with `memory=` in `tastr/cloud/modal_web.py`.

To find out what happened to an upload:

- **Admin tab > Problems**: failed analyses, limits hit and restarts, each naming the file. An upload's
  lines carry a ref, e.g. "Uploaded mix.mp3 (ref 1a2b3c4d)", then "Analysis failed: mix.mp3 at step sections,
  it ran out of memory on Modal (ref 1a2b3c4d)". A restart that cut analyses off logs "The server restarted
  in the middle of an analysis" and "Cut off by the restart: <file>, while at step <step>"; a deploy logs
  "Deployed 49eeecf: <commit message>" with a link to its GitHub Actions run (logged when the new version first
  starts). Each problem line links to the Worker's and Modal's logs.
- **The Worker**: Cloudflare **Workers & Pages > tastr > Observability** keeps the server's log (on in
  `worker/wrangler.jsonc`); failed analyses log `analysis failed for <file> (ref <ref>): <error>` with the full
  error, so search for the ref. `npx wrangler tail tastr` in `worker/` streams it live.
- **Modal**: `modal app logs tastr-web --env friends` streams the functions' logs, and the dashboard
  (modal.com > the `friends` environment > `tastr-web` > `analyze_stream`) lists each call with its
  duration, memory and how it ended (an out-of-memory or timeout stop is shown there).

## Strudel links

"Try this in Strudel" opens strudel.cc with code that loads the track's audio and stems from Tastr, but
strudel.cc can't pass the Access login. So on a hosted server the page asks
`GET /api/tracks/{id}/strudel-link` (owner only) for a link under `/pub/<expiry>.<signature>/tracks/{id}/`
that serves that one track's audio and stems for 7 days. The signature is an HMAC with `TASTR_LINK_SECRET`
over the track id and expiry, so a link can't be moved to another track or extended. Anyone with the link
can play the audio until it expires, the same as a shared Strudel sketch.

## Expiring audio

Audio is kept by default. An admin can set how long each person's audio is kept on the Admin tab (✎ on their row,
**Keep audio for**: forever, a preset or a number of days; stored in the `audio_keep` table), and
`TASTR_AUDIO_DAYS` sets the default for everyone else (`0`, the default, keeps everything). Every 6 hours (and
when the server starts) uploaded files older than their owner's days are deleted, with their stems unless
another track still has the same file. A file several people's tracks share (copied tracks) stays as long as
the owner who keeps it longest would keep it. The track card stays, with everything except
playback, waveform scrubbing, stems and re-analysis. Tracks owned by `TASTR_ADMIN_EMAILS` keep their audio.
With the audio bucket on, the bucket's copy goes too, and the age is counted from when it reached the bucket.

## Audio in a bucket

With `TASTR_AUDIO_BUCKET` set (`tastr/blobs.py`, which talks to the bucket through its own small S3 client, `tastr/s3.py`), uploads and stems
live in an S3-compatible bucket and the server's disk only caches them. On the Cloudflare Worker, whose files
live in memory, the bucket is required:

- Each upload is copied to the bucket, under the same path it has under `TASTR_HOME` (`uploads/ab12cd34/song.mp3`),
  before it is analyzed. If the copy fails the upload is refused (503) and nothing is kept.
- With `TASTR_DIRECT_UPLOAD=1` the page uploads straight to the bucket instead. `POST /api/uploads` checks the
  limits (count, type, size, the daily track limit, the storage cap) before any bytes move and answers with a
  signed PUT link per file (an hour; `POST /api/uploads/{id}/link` renews one). The page PUTs the files, 3 at a
  time, then `POST /api/uploads/{id}/check` checks what arrived (size, and length once read back into the cache)
  and `POST /api/analyze/uploaded` analyzes it like any upload when its turn comes. A PUT the bucket refuses
  (usually a missing CORS rule) is reported with `POST /api/uploads/{id}/failed` and shows in the admin
  activity log and Problems. A refused upload is deleted from the bucket; one that got a link and was never finished is
  deleted by the six-hourly sweep after two hours.
- Modal gets a signed link to the upload instead of its bytes, and uploads stems to signed links of its own.
- Playback (`/api/tracks/{id}/audio`, `/stems/{name}`, and the signed `/pub` links for strudel.cc) answers with
  a redirect to a signed link that lasts an hour; the browser seeks with Range requests against the bucket.
- What needs the file here (re-reading its tags, analysis on this machine) downloads it first. Every 6 hours
  the server deletes local copies the bucket holds that are more than an hour old.
- Deleting a track, or expiring its audio, deletes the bucket's copy; the storage cap and the admin view's
  storage figures count what the bucket holds.
- An upload whose bytes (SHA-256) match a track already in that person's library isn't analyzed again and
  doesn't count toward the daily limit: the answer names the existing track with `"already": true` and the new
  copy is deleted. If that track's audio had expired, the new copy becomes its audio instead, card untouched.
  `POST /api/tracks/known {"sha256": [...]}` answers which files are known, so the page can skip them before
  uploading. The six-hourly sweep also deletes uploads in the bucket that no track points at once they are two
  days old (copies left by re-uploads before this), and stops without deleting if most uploads match no track.
- Each card's waveform and spectrogram data (about a megabyte) goes to the bucket as `views/<track id>.json`,
  gzipped, and the card in the database keeps only a marker; `/api/tracks/{id}/views` redirects to a signed link.
  Cards saved before keep their data inline until they are re-analyzed or backfilled. If the bucket can't take it, the data
  stays in the card. `tastr db-size` shows the database's size by table and how much waveform data is still inline.
- `tastr unpack` copies the archive's audio and stems to the bucket as well.
- The Files view reads sizes from the bucket's listing; single downloads redirect to a signed link that names the
  file, and zips stream each file from the bucket.

On a server you run with `tastr serve`, files uploaded before the bucket was turned on stay on the disk, and keep
working from there, until `tastr bucket-backfill` copies them up (`tastr bucket-backfill --dry-run` first to see
what it would move). It also moves older cards' inline waveform data to the bucket, so the database shrinks without
re-analyzing anything. It skips what the bucket already has, so it is safe to run again; the next sweep deletes
the local copies.

The bucket needs a CORS rule so pages can fetch from it (the app fetches audio to draw and play it, and strudel.cc
follows the redirect): allow `GET` and `HEAD` from any origin (`*`), with the `Range` header. A signed link is
what grants access, and it expires; the bucket stays private. [deploy.md](deploy.md#2-r2-bucket-for-audio)
has the steps.

## Moving your own library to a hosted server

`tastr pack` and `tastr unpack` move a whole library with its audio and stems onto a hosted server you run
yourself with `tastr serve` (`tastr unpack` only goes into an empty library). If only the database is on the
server already, give its tracks with no owner to your Access email:

```
tastr set-owner you@example.com
```

Both need a shell on the server, which the Cloudflare Worker doesn't have ([hosting-map.md](hosting-map.md)).
