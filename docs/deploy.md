# Deploying a hosted Tastr

This page sets up Tastr for a few invited friends, on infrastructure you don't have to manage:

| Piece | Where | What it does | Cost (list prices, check before relying on them) |
| --- | --- | --- | --- |
| Web server | [Fly.io](https://fly.io), one 512 MB machine (`fly.toml`, `Dockerfile`) | Serves the UI and API, stores uploads and stems on a volume, stops when nobody uses it | about $3-5 a month, plus $0.15 per GB-month of volume |
| Analysis and stems | [Modal](https://modal.com), app `tastr-web` in its own `friends` environment (`tastr/cloud/modal_web.py`) | Analyzes each upload (about $0.001 a track) and separates stems on a GPU (under a cent a track) | inside Modal's $30 monthly free credit at friends scale |
| Library backup | [Litestream](https://litestream.io) to a private R2 bucket `tastr-app` | Copies every database change to R2 within seconds; a new volume restores from it | well under $0.10 a month |
| Login | [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/) | Emails each invited person a one-time code | free up to 50 people; you need a domain on Cloudflare (about $10 a year) |

What hosted mode itself changes (logins, per-person libraries, limits) is in [hosting.md](hosting.md), and
[hosting-map.md](hosting-map.md) shows how the pieces connect and where each setting lives.

You run every command below yourself, so keys never leave your machine and your accounts. Nothing here
puts a key in the repository.

## 1. Accounts and tools

- A Cloudflare account with your domain on it. Tastr will live at a hostname such as `tastr.example.com`.
- A Fly.io account with a card on file, and `flyctl`: `brew install flyctl`, then `fly auth login`.
- A Modal account, and the `modal` command: `pip install -e '.[cloud]'`, then `modal token new`.

## 2. R2 bucket for the library backup

In the Cloudflare dashboard:

1. **R2 Object Storage > Create bucket**, named `tastr-app`. Leave it private.
2. **Manage API tokens > Create API token**: **Object Read & Write**, applied to `tastr-app` only. Keep the
   **Access Key ID**, the **Secret Access Key** (shown once) and the **S3 endpoint**
   (`https://<account id>.r2.cloudflarestorage.com`).

This is a separate bucket and token from the benchmark datasets' `tastr-data` ([cloud.md](cloud.md)), so the
server's key can't touch the datasets.

## 2b. R2 bucket for audio (optional)

Without this, uploads and stems live on the Fly volume only and a lost volume loses them. With it, they live
in R2 and the volume only caches them ([hosting.md](hosting.md#audio-in-a-bucket)).

1. **R2 Object Storage > Create bucket**, named `tastr-audio`. Leave it private.
2. In the bucket, **Settings > CORS Policy > Add CORS policy**:

   ```json
   [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["Range"],
     "ExposeHeaders": ["Content-Length", "Content-Range", "Accept-Ranges"], "MaxAgeSeconds": 3600}]
   ```

   Signed links are what grant access; this only lets pages read the response. Any origin, because strudel.cc
   follows a redirect from your hostname, after which its requests carry no origin a list could match.
3. **Manage API tokens > Create API token**: **Object Read & Write**, applied to `tastr-audio` only. Keep the
   Access Key ID and Secret Access Key.
4. After the first deploy (step 7), turn it on:

   ```
   fly secrets set TASTR_AUDIO_BUCKET=tastr-audio \
     TASTR_AUDIO_ENDPOINT=https://<account id>.r2.cloudflarestorage.com \
     TASTR_AUDIO_ACCESS_KEY_ID=<access key id> TASTR_AUDIO_SECRET_ACCESS_KEY=<secret access key>
   ```

   Deploy Modal first if it is older than this setting: the server then sends Modal signed links instead of
   audio bytes. The deploy workflow always deploys Modal first.

5. Move what the volume already holds into the bucket: `fly ssh console -C "tastr bucket-backfill --dry-run"` shows
   what would move, then run it without `--dry-run`. `tastr db-size` should then show no waveform data inline.

## 3. Modal environment

```
modal environment create friends
```

The server's analysis and stems run there, apart from your benchmarks, so their usage shows on its own in
Modal's usage page. Set a spending limit under Modal's workspace **Settings > Usage** if you want a hard cap.

For the server, create a separate token under **Settings > API Tokens** in the Modal dashboard. Keep its
**token id** and **token secret**.

## 4. Fly app and volume

Pick an app name (it shows up in `<name>.fly.dev`) and a region near you and your friends, and put both
in `fly.toml` (`app`, `primary_region`). Then:

```
fly apps create <name>
fly volumes create tastr_data --size 5 --region <region>
```

The volume holds the database, uploads and stems. Keep `TASTR_STORAGE_GB` (step 6) below its size; a volume
can be extended later with `fly volumes extend`.

## 5. Cloudflare Access

Follow [Setting up Cloudflare Access](hosting.md#setting-up-cloudflare-access) for `tastr.example.com`,
then add the second application it describes for the signed `/pub/` links. Keep the team domain and the
AUD tag.

## 6. Fly secrets

```
fly secrets set \
  TASTR_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com \
  TASTR_ACCESS_AUD=<AUD tag> \
  TASTR_ADMIN_EMAILS=<your email> \
  TASTR_LINK_SECRET=$(openssl rand -hex 32) \
  TASTR_STORAGE_GB=4 \
  MODAL_TOKEN_ID=<token id> MODAL_TOKEN_SECRET=<token secret> \
  LITESTREAM_BUCKET=tastr-app \
  LITESTREAM_ENDPOINT=https://<account id>.r2.cloudflarestorage.com \
  LITESTREAM_ACCESS_KEY_ID=<access key id> LITESTREAM_SECRET_ACCESS_KEY=<secret access key>
```

The other settings (daily limits, audio kept for 30 days) have defaults; [hosting.md](hosting.md#settings)
lists them. `fly.toml` already turns on hosted mode and points analysis and stems at Modal.

## 7. First deploy

Modal first, so the server finds its functions:

```
modal deploy --env friends tastr/cloud/modal_web.py
fly deploy
```

The Modal deploy takes a few minutes the first time: it builds the images and analyzes a short test signal,
so the compiled code ships in the image and a cold start doesn't compile it again.

## 8. Your hostname

```
fly certs add tastr.example.com
```

In Cloudflare DNS, add the records it prints (a CNAME `tastr` to `<name>.fly.dev`, proxied, plus the
`_acme-challenge` record for the certificate), and set **SSL/TLS** to **Full (strict)**. Open
`https://tastr.example.com`: Access asks for your email, then Tastr opens on an empty library.

`<name>.fly.dev` reaches the server without Cloudflare, so it answers 401 to everything except
`/api/health` and the signed links.

## 9. Move your own library (optional)

Do this before inviting anyone: `tastr unpack` only goes into an empty library. On your machine, in the
clone you run Tastr from:

```
tastr pack ~/tastr-library.tar --owner <your Access email>
```

This copies your database, gives its tracks to your email, and adds each track's audio (wherever it is on
disk) and its stems. Your own library is left as it was. Then:

```
fly ssh sftp shell
» put /Users/<you>/tastr-library.tar /data/library.tar
fly ssh console -C "tastr unpack /data/library.tar"
fly ssh console -C "rm /data/library.tar"
```

Admins' audio is never expired, so your library keeps its audio. It counts toward `TASTR_STORAGE_GB`.

## 9b. Catalog lookups (optional)

"Listed elsewhere" ([lookup.md](lookup.md)) is off until `TASTR_LOOKUP` names providers. Friends' lookups
spend your API keys, within the per-provider limits in lookup.md. For FreqBlog:

```
fly secrets set TASTR_LOOKUP=freqblog FREQBLOG_API_KEY=<key>
```

AcousticBrainz needs no key but reads a 322 MB index (`tastr fetch acousticbrainz`, lookup.md) from the
volume. Copy it up under a temporary name, so a cut-off upload is never read, then turn it on:

```
fly ssh sftp shell
» put /path/to/acousticbrainz-index.sqlite /data/acousticbrainz-index.sqlite.part
fly ssh console -C "mv /data/acousticbrainz-index.sqlite.part /data/acousticbrainz-index.sqlite"
fly secrets set TASTR_LOOKUP=freqblog,acousticbrainz
```

`TASTR_HOME` is `/data`, so the provider finds it there without `ACOUSTICBRAINZ_INDEX`. It isn't backed up
(the database is the only file Litestream copies); copy it again if the volume is replaced. It counts against
the volume, not `TASTR_STORAGE_GB`, so leave room for it.

## 10. Invite friends

Add their emails to the Access application's policy. To remove someone, delete their address there; their
tracks stay until you delete them.

## Updating

Merge to main. When the **tests** workflow passes on main, the **deploy** workflow runs on its own and deploys
Modal and then Fly, so both run the same engine. It skips the deploy when nothing that ships changed since the
last one (a merge of only docs, tests or Markdown; the list of paths is in `.github/workflows/deploy.yml`). To
deploy by hand, run it from **Actions > deploy > Run workflow**. It needs three repository secrets (**Settings > Secrets and variables >
Actions**):

| Secret | Where it comes from |
| --- | --- |
| `FLY_API_TOKEN` | `fly tokens create deploy` |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | another Modal API token, as in step 3 |

Or run the two commands from step 7 yourself.

The page loads `app/main.js`, `strudel.js` and `style.css` with a content hash in the URL (`?v=…`), and the UI's
other modules (`tastr/web/static/app/*.js`, which `main.js` imports) are served with `Cache-Control: no-cache`, so a
deploy shows up on the next page load even though Cloudflare caches static files. Deploys before this change could
stay stale for a few hours; **Caching > Configuration > Purge Everything** in Cloudflare clears that.

## Backups and restores

Litestream copies each change to `tastr-app/tastr.db` in R2. If the volume is lost, create a new one with
the same name and deploy: on a volume with no database, the server restores it from R2 before starting.
Uploaded audio and stems are not backed up; they are on the volume only, and expire after 30 days anyway
(except admins').

## Notes

- The machine stops after a few idle minutes and starts on the next request, which takes a few seconds.
  The first analysis after a quiet spell also waits for Modal to start a container (about half a minute).
- A queued or running stem separation or drum model run leaves a marker on the volume, so if the machine
  stops mid-job the server starts it again on its next start.
- Uploads are analyzed without the pretrained drum model, so the analysis image can leave out PyTorch and
  start faster. "Detect drums with the model" in a track's menu re-analyzes that one track through
  Modal's `analyze_drums` function, which has PyTorch and the weights (CC BY-NC-SA 4.0) built into its
  image. It starts a larger container, so expect a minute or two per track. `TASTR_DAILY_DRUMS` caps it.
