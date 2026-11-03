# Deploying a hosted Tastr

This page sets up Tastr for a few invited friends, on infrastructure you don't have to manage:

| Piece | Where | What it does | Cost (list prices, check before relying on them) |
| --- | --- | --- | --- |
| Web server and library | A [Cloudflare Python Worker](https://developers.cloudflare.com/workers/languages/python/) with a SQLite [Durable Object](https://developers.cloudflare.com/durable-objects/) (`worker/`) | Serves the UI and API and holds the library database, with 30 days of point-in-time recovery | Workers Paid, $5 a month, which covers friends-scale use |
| Audio and stems | A private [R2](https://developers.cloudflare.com/r2/) bucket `tastr-audio` | Keeps uploads, stems and each card's waveform data; pages play them from signed links | R2's free tier covers 10 GB, then $0.015 per GB-month |
| Analysis and stems | [Modal](https://modal.com), app `tastr-web` in its own `friends` environment (`tastr/cloud/modal_web.py`) | Analyzes each upload (about $0.001 a track) and separates stems on a GPU (under a cent a track) | inside Modal's $30 monthly free credit at friends scale |
| Login | [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/) | Emails each invited person a one-time code | free up to 50 people; you need a domain on Cloudflare (about $10 a year) |

What hosted mode itself changes (logins, per-person libraries, limits) is in [hosting.md](hosting.md), and
[hosting-map.md](hosting-map.md) shows how the pieces connect and where each setting lives.

You run every command below yourself, so keys never leave your machine and your accounts. Nothing here
puts a key in the repository.

## 1. Accounts and tools

- A Cloudflare account with your domain on it, on **Workers & Pages > Plans > Workers Paid** (the Worker's
  bundle, about 7.7 MiB gzipped, is over the Free plan's 3 MiB). Tastr will live at a hostname such as
  `tastr.example.com`.
- A Modal account, and the `modal` command: `pip install -e '.[cloud]'`, then `modal token new`.
- Node (for wrangler) and [uv](https://docs.astral.sh/uv/) 0.13.4 or newer, to set the Worker's secrets from
  `worker/` ([worker/README.md](../worker/README.md)).
- Your own fork or copy of the repository on GitHub, whose Actions deploy it. Change `account_id` and the
  `vars` in `worker/wrangler.jsonc` (`TASTR_CF_ACCOUNT_ID`, `TASTR_GITHUB_REPO`) to yours.

## 2. R2 bucket for audio

In the Cloudflare dashboard:

1. **R2 Object Storage > Create bucket**, named `tastr-audio`. Leave it private.
2. In the bucket, **Settings > CORS Policy > Add CORS policy**:

   ```json
   [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["Range"],
     "ExposeHeaders": ["Content-Length", "Content-Range", "Accept-Ranges"], "MaxAgeSeconds": 3600},
    {"AllowedOrigins": ["https://<your hostname>"], "AllowedMethods": ["PUT"], "AllowedHeaders": ["Content-Type"],
     "MaxAgeSeconds": 3600}]
   ```

   Signed links are what grant access; this only lets pages read the response. Any origin may GET, because
   strudel.cc follows a redirect from your hostname, after which its requests carry no origin a list could
   match. The PUT rule is for uploads straight to the bucket (`TASTR_DIRECT_UPLOAD`, step 6), which a Worker
   needs for files over 100 MB, its limit for one request.
3. **Manage API tokens > Create API token**: **Object Read & Write**, applied to `tastr-audio` only. Keep the
   **Access Key ID**, the **Secret Access Key** (shown once) and the **S3 endpoint**
   (`https://<account id>.r2.cloudflarestorage.com`).

This is a separate bucket and token from the benchmark datasets' `tastr-data` ([cloud.md](cloud.md)), so the
server's key can't touch the datasets.

## 3. Modal environment and tokens

```
modal environment create friends
```

The server's analysis and stems run there, apart from your benchmarks, so their usage shows on its own in
Modal's usage page. Set a spending limit under Modal's workspace **Settings > Usage** if you want a hard cap.

Make two tokens in the Modal dashboard:

- **Settings > API Tokens**: one for GitHub Actions to deploy with. Keep its **token id** and **token secret**.
- **Settings > Proxy Auth Tokens**: one for the Worker, which calls Modal's `api` endpoint over HTTPS since it
  can't run the Modal client. Keep its id and secret.

## 4. Cloudflare Access

Follow [Setting up Cloudflare Access](hosting.md#setting-up-cloudflare-access) for `tastr.example.com`,
then add the second application it describes for the signed `/pub/` links. Keep the team domain and the
AUD tag.

## 5. First deploy

Add three repository secrets on GitHub (**Settings > Secrets and variables > Actions**):

| Secret | Where it comes from |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Cloudflare **My Profile > API Tokens > Create Token**, from the "Edit Cloudflare Workers" template |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | The Modal API token from step 3 |

Then run **Actions > deploy > Run workflow**. It deploys Modal first, so the Worker finds its functions, then
builds, checks and deploys the Worker (`.github/workflows/deploy-worker.yml`). The Modal deploy takes a few
minutes the first time: it builds the images and analyzes a short test signal, so the compiled code ships in
the image and a cold start doesn't compile it again. Its log prints the `api` endpoint's address (it ends in
`-api.modal.run`); keep it for the next step.

## 6. Worker secrets

From `worker/`, set each one with `uv run pywrangler secret put NAME` (it asks for the value), or in the
dashboard under **Workers & Pages > tastr > Settings > Variables and Secrets** as type **Secret**. A plain Text
variable added in the dashboard is wiped by the next deploy.

| Secret | Value |
| --- | --- |
| `TASTR_ACCESS_TEAM_DOMAIN` | `<team>.cloudflareaccess.com` |
| `TASTR_ACCESS_AUD` | the AUD tag from step 4 |
| `TASTR_ADMIN_EMAILS` | your email |
| `TASTR_LINK_SECRET` | a random value, such as the output of `openssl rand -hex 32` |
| `TASTR_STORAGE_GB` | a cap on what the bucket holds, such as `20` |
| `TASTR_AUDIO_BUCKET`, `TASTR_AUDIO_ENDPOINT`, `TASTR_AUDIO_ACCESS_KEY_ID`, `TASTR_AUDIO_SECRET_ACCESS_KEY` | `tastr-audio`, the S3 endpoint and the bucket's token from step 2 |
| `TASTR_MODAL_URL` | the `api` endpoint's address from step 5 |
| `TASTR_MODAL_KEY`, `TASTR_MODAL_SECRET` | the proxy auth token from step 3 |
| `TASTR_DIRECT_UPLOAD` | `1`, so the page uploads straight to the bucket |
| `TASTR_GITHUB_TOKEN` (optional) | a fine-grained token with read-only Actions access to your repository, for the Admin log ([hosting-map.md](hosting-map.md#worker-secrets)) |

The other settings (daily limits, audio kept for 30 days) have defaults; [hosting.md](hosting.md#settings)
lists them. `worker/wrangler.jsonc` already turns on hosted mode and points analysis and stems at Modal.

## 7. Your hostname

In **Workers & Pages > tastr > Settings > Domains & Routes**, add `tastr.example.com` as a custom domain.
Cloudflare creates its DNS record and certificate. The Worker has no `*.workers.dev` or preview address, so
this hostname, behind the Access application, is the only way in. Open `https://tastr.example.com`: Access asks
for your email, then Tastr opens on an empty library.

Upload one file to check it. A failure that says storage refused the upload or stopped usually means the
bucket's PUT rule is missing or names another hostname.

## 8. Catalog lookups (optional)

"Listed elsewhere" ([lookup.md](lookup.md)) is off until `TASTR_LOOKUP` names providers. Friends' lookups
spend your API keys, within the per-provider limits in lookup.md. For FreqBlog, set the secrets
`TASTR_LOOKUP=freqblog` and `FREQBLOG_API_KEY=<key>`.

AcousticBrainz reads a 322 MB index file, which the Worker can't hold, so it stays off there (TAS-88).

## 9. Ask Claude in Strudel (optional)

"Ask Claude to change it" under Try this in › Strudel sends the code on screen, the ask and the track's BPM,
key and sections to the Claude API (`claude-sonnet-5-5`, `tastr/web/claude.py`), on Tastr's own key. Make a key
in the Anthropic Console (**Settings > API Keys**), give its workspace a monthly spend limit (**Limits**), then
set it as the secret `ANTHROPIC_API_KEY` from your own terminal or the dashboard, never in a chat or a file in
the repository.

Each person gets `TASTR_DAILY_ASKS` asks a day (default 20); the Admin tab shows their asks and what the tokens
cost, and raises one person's limit from the ✎ on their row. Without the secret the panel says Ask is off.

## 10. Invite friends

Add their emails to the Access application's policy. To remove someone, delete their address there; their
tracks stay until you delete them.

## Updating

Merge to main. When the **tests** workflow passes on main, the **deploy** workflow runs on its own and deploys
Modal and then the Worker (`deploy-worker.yml`), so both run the same engine. It skips the deploy when nothing
that ships changed since the last one (a merge of only docs, tests or Markdown; the list of paths is in
`.github/workflows/deploy.yml`). To deploy by hand, run it from **Actions > deploy > Run workflow**; to deploy
just the Worker, **Actions > deploy-worker > Run workflow**.

The page loads `app/main.js`, `strudel.js` and `style.css` with a content hash in the URL (`?v=…`), and the UI's
other modules (`tastr/web/static/app/*.js`, which `main.js` imports) are served with `Cache-Control: no-cache`, so a
deploy shows up on the next page load even though Cloudflare caches static files.

## Backups and restores

The library database is the Durable Object's own storage, and Cloudflare keeps 30 days of point-in-time
recovery for it ([Durable Objects docs](https://developers.cloudflare.com/durable-objects/api/storage-api/#pitr-point-in-time-recovery-api)).
Uploads and stems are in the R2 bucket; they expire after 30 days anyway (except admins').

## Notes

- An idle Durable Object wakes quickly. The first analysis after a quiet spell waits for Modal to start a
  container (about half a minute).
- A queued or running stem separation or drum model run leaves a marker in the database, so if the object
  restarts mid-job the server starts it again.
- Uploads are analyzed without the pretrained drum model, so the analysis image can leave out PyTorch and
  start faster. "Detect drums with the model" in a track's menu re-analyzes that one track through
  Modal's `analyze_drums` function, which has PyTorch and the weights (CC BY-NC-SA 4.0) built into its
  image. It starts a larger container, so expect a minute or two per track. `TASTR_DAILY_DRUMS` caps it.
- `tastr pack`, `tastr unpack` and `tastr bucket-backfill` work on a server's files from a shell, which the
  Worker doesn't have. They're for a hosted `tastr serve` on a machine of your own.
