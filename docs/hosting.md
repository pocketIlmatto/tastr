# Sharing Tastr with a few people (hosted mode)

`tastr serve` is a single-person app: it binds to 127.0.0.1 and has no login. To share it with a few
invited people, run it in **hosted mode** behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/),
which sends a one-time code to each invited email address. Cloudflare decides who gets in; Tastr checks
Cloudflare's signed token on every request, so a request that reaches the server without going through
Cloudflare gets nothing.

This page covers what hosted mode changes and how to configure it. [deploy.md](deploy.md) sets it up on
Fly.io, with analysis on Modal and the library backed up to R2. [hosting-map.md](hosting-map.md) shows how the
pieces fit together.

## What hosted mode changes

| | Local (`tastr serve`) | Hosted (`TASTR_HOSTED=1`) |
| --- | --- | --- |
| Login | none | Cloudflare Access token required on every request except `/api/health` and signed `/pub/` links |
| Link analysis (yt-dlp) | on (with the `links` extra) | off; the link field is hidden |
| SQL box (`/api/query`) | whole database, 5 s limit | admins: whole database; everyone else: an in-memory copy of their own library (no owner, path or log columns); 2 s limit, one query at a time |
| Uploads | any size | audio types only, size, count, per-person daily and total storage limits |
| Library | one, yours | one per person: each sees and changes only the tracks they uploaded |
| Saved searches (Explore) | one list | one list per person |
| Deleting a track | removes its stems | also removes its uploaded audio |
| Uploaded audio and stems | kept | deleted after `TASTR_AUDIO_DAYS` (cards stay; admins' audio is kept) |
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
| `TASTR_STORAGE_GB` | 10 | uploads are refused once uploaded audio and stems reach this |
| `TASTR_MAX_ANALYSES` | 2 | analyses that run at once |
| `TASTR_DAILY_STEMS` | 10 | stem separations each person can start per UTC day |
| `TASTR_DAILY_DRUMS` | 10 | drum model runs ("Detect drums with the model") each person can start per UTC day |
| `TASTR_ADMIN_EMAILS` | none | comma-separated emails that may open the admin view |
| `TASTR_AUDIO_DAYS` | 30 | uploaded audio and stems are deleted after this many days; `0` keeps them |
| `TASTR_CF_ACCOUNT_ID` | none | your Cloudflare account id (32 hex characters, from the dashboard URL); the Admin tab then links to Access's applications and its log of blocked requests. Not secret, but keep it out of the repository |
| `TASTR_GITHUB_REPO` | none | `owner/name` of the repository whose GitHub Actions runs on main the Admin log shows (fly.toml sets it) |
| `TASTR_GITHUB_WORKFLOWS` | none | which workflows' runs the Admin log shows, comma-separated names or file names (fly.toml: `tests,deploy`); none logs no runs |
| `TASTR_GITHUB_TOKEN` | none | a fine-grained token with read-only Actions access, needed only while that repository is private |
| `TASTR_WATCH_MINUTES` | 15 | how often the server checks GitHub Actions and the R2 backup for the Admin log; `0` turns it off |
| `TASTR_MODAL_WORKSPACE` | none | your Modal workspace name; the Admin tab's Modal link then opens the `tastr-web` app instead of Modal's app list |
| `TASTR_LINK_SECRET` | random | signs the Strudel links; set it (e.g. `openssl rand -hex 32`) so links survive a restart. Keep it secret |
| `TASTR_BROWSER_ENGINE` | off | `1` has each visitor's browser make the whole card with the Rust engine's WebAssembly build; the server checks it and skips analysis ([ARCHITECTURE.md](ARCHITECTURE.md#browser-engine)). Needs the image's `TASTR_ENGINE_WASM` |
| `TASTR_ENGINE` | `rust` | which engine makes cards, on the server and on Modal ([rust-engine.md](rust-engine.md)). `python` picks the Python reference engine; the drum model and `tastr analyze --stages` always use it |

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
`log_links`, where to read the Fly and Modal logs, and `blocked_today`, how many requests
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
or `-` for lines with no person; `source` one of app, modal, flyio, github, cloudflare or r2; `from` and `to`
UTC days, both included. Blocked requests come back as one Cloudflare line per day. Deploys are logged by the
server when it starts on Fly.

Every `TASTR_WATCH_MINUTES` the server also logs (tastr/web/watch.py): each GitHub Actions run that finished on
main ("tests passed on main (3 min): ..."), linked to the run, for the workflows in `TASTR_GITHUB_WORKFLOWS`
when `TASTR_GITHUB_REPO` is set; and each new
Litestream snapshot in R2, when `LITESTREAM_BUCKET` is set. A failed check of the backup, or no snapshot for 30
hours, logs one problem line a day. The first check logs only the last day of runs and the newest snapshot. A
machine Fly stopped for being idle catches up when it wakes.

`PUT /api/admin/limits/{email}` with `{"daily_tracks": 45, "daily_stems": null, "scope": "today"}` changes
one person's daily caps (`null` = the server default; `scope` `today` ends at 00:00 UTC, `standing` lasts until
changed), and `DELETE` puts them back on the defaults. The upload and stem checks use the change, and
`/api/config` returns the caller's own caps. Each change is logged as an admin line.

Admins see an **Admin** tab in the header with all of this: tiles for people, tracks, stem jobs,
storage, analyses running and an estimated 30-day Modal cost, a per-person chart of the last 30 days,
the people table with today's quotas, the activity log, the limits and the blocked count. Nobody else
gets the tab, and on your own machine there is none.

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
  starts, so after a deploy to a stopped machine, at its next wake-up). Each problem line links to the Fly and
  Modal logs.
- **Fly**: `fly logs -a <app>` streams the server's log live; failed analyses log
  `analysis failed for <file> (ref <ref>): <error>` with the full error, so search for the ref. Older lines are in the Fly dashboard's
  **Monitoring** page for the app. `fly status -a <app>` shows restarts.
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

Every 6 hours (and when the server starts) uploaded files older than `TASTR_AUDIO_DAYS` are deleted, with
their stems unless another track still has the same file. The track card stays, with everything except
playback, waveform scrubbing, stems and re-analysis. Tracks owned by `TASTR_ADMIN_EMAILS` keep their audio.

## Moving your own library to a hosted server

`tastr pack` and `tastr unpack` move a whole library with its audio and stems; [deploy.md](deploy.md#9-move-your-own-library-optional)
has the steps. If only the database is on the server already, give its tracks with no owner to your Access
email:

```
tastr set-owner you@example.com
```
