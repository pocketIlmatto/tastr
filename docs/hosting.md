# Sharing Tastr with a few people (hosted mode)

`tastr serve` is a single-person app: it binds to 127.0.0.1 and has no login. To share it with a few
invited people, run it in **hosted mode** behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/),
which sends a one-time code to each invited email address. Cloudflare decides who gets in; Tastr checks
Cloudflare's signed token on every request, so a request that reaches the server without going through
Cloudflare gets nothing.

This page covers what hosted mode changes and how to configure it. Deploying it (Docker image, host,
storage on R2, analysis on Modal) comes in a later change.

## What hosted mode changes

| | Local (`tastr serve`) | Hosted (`TASTR_HOSTED=1`) |
| --- | --- | --- |
| Login | none | Cloudflare Access token required on every request except `/api/health` |
| Link analysis (yt-dlp) | on (with the `links` extra) | off; the link field is hidden |
| SQL box (`/api/query`) | on | off; the box is hidden |
| Uploads | any size | audio types only, size, count, per-person daily and total storage limits |
| Library | one, yours | one per person: each sees and changes only the tracks they uploaded |
| Deleting a track | removes its stems | also removes its uploaded audio |
| Admin view (`/api/admin/usage`) | none | usage per person, for `TASTR_ADMIN_EMAILS` |
| Stem separation | unlimited | per-person daily limit |
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
| `TASTR_MAX_FILES` | 20 | files per upload |
| `TASTR_DAILY_TRACKS` | 30 | tracks each person can upload per UTC day |
| `TASTR_STORAGE_GB` | 10 | uploads are refused once uploaded audio and stems reach this |
| `TASTR_MAX_ANALYSES` | 2 | analyses that run at once |
| `TASTR_DAILY_STEMS` | 10 | stem separations each person can start per UTC day |
| `TASTR_ADMIN_EMAILS` | none | comma-separated emails that may open the admin view |

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

Cloudflare then forwards each signed-in request with a `Cf-Access-Jwt-Assertion` header, which Tastr
verifies against your team's public keys at `https://<team domain>/cdn-cgi/access/certs`.

## Admin view

For the emails in `TASTR_ADMIN_EMAILS`, `GET /api/admin/usage` returns, per person: tracks, uploaded
audio and stem bytes, last upload, last request since the server started, and today's tracks, stem jobs
and visits (page loads), plus 30 days of daily counts, the limits, total storage against the cap and
the stem jobs and analyses running or waiting. Everyone else gets 404. `/api/config` says `"admin": true` for admins.

## Moving your own library to a hosted server

Tracks analyzed on your own machine have no owner, so on a hosted server nobody sees them. Once your
database is on the server (the deploy change adds a script that moves the audio too), give its tracks to
your Access email:

```
tastr set-owner you@example.com
```

## Known gaps until the deploy change

- **Try this in Strudel** can't load the original audio or stems: strudel.cc fetches them without the
  Access login. Short-lived signed links fix this in the deploy change.
- Uploaded audio stays until the track is deleted; the 30-day expiry comes with the deploy change.
