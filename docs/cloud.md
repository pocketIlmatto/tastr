# Running heavy jobs in the cloud

Benchmarks and stem separation can run on [Modal](https://modal.com) instead of your machine; benchmarks
read their datasets from a Cloudflare [R2](https://developers.cloudflare.com/r2/) bucket. The web app and everyday analysis still run
locally. This is option C of the cloud plan; moving the app itself (option B) is tracked separately.

```
tastr bench beatport-key beatport-key --remote --limit 200 --out bp-200.json
```

With `--remote`, the dataset path is a folder in the bucket, not on disk. `--limit`, `--stages`,
`--baseline` and `--out` work as usual (see [bench.md](bench.md)); `--jobs` defaults to the container's
8 CPUs. The report comes back to your machine; nothing is written to the bucket.

**Modal runs the code that was last deployed, not your working tree.** Redeploy after changing the engine
(step 5 below) before a remote run, or the numbers describe the old code.

## Stems on a Modal GPU

Start the server with `TASTR_STEMS_BACKEND=modal tastr serve` (or set it for `tastr stems ID`). "Separate
stems" then uploads the track to the `stems` function, which runs Demucs on a T4 GPU and sends the four
FLAC stems back; they are stored and served locally as before. You need `tastr[cloud]`, not
`tastr[stems]`, so PyTorch stays off your machine. The progress bar jumps from start to done, because
Modal reports no progress. The first job after a quiet spell waits for a cold start (tens of seconds).

## Cost

Modal bills per second. The `bench` function asks for 8 CPUs and 16 GB: roughly $0.40 for a 46-minute
full GiantSteps run at list prices. A stem job is well under a cent (T4 at about $0.60 an hour). Modal's free tier includes $30 of credit a month. R2 storage is
about $0.015 per GB-month with no egress fees; today's datasets (about 7 GB) cost around $0.10 a month.
Check both pricing pages before relying on these.

## One-time setup

You need a Modal account and a Cloudflare account with R2 enabled. Run the commands below in the clone
you run the app from, with its virtualenv active.

### 1. Install

```
pip install -e '.[cloud]'   # the Modal client and its `modal` command
brew install rclone         # only for uploading datasets
```

You don't need `tastr[stems]` (Demucs and PyTorch) locally; the Modal image installs them.

### 2. Log in to Modal

```
modal token new
```

This opens the browser and saves a token to `~/.modal.toml`. There is nothing to copy by hand.

### 3. Create the bucket and an R2 token

In the Cloudflare dashboard:

1. R2 Object Storage > Create bucket, named `tastr-data` (another name works if you set
   `TASTR_R2_BUCKET` to it when deploying). Leave it private: no public access, no custom domain.
2. R2 Object Storage > Manage API tokens > Create API token. Pick **Object Read & Write**, apply it to
   the `tastr-data` bucket only, and create it.
3. The next page shows several values. Keep three of them:
   - **Access Key ID**
   - **Secret Access Key** (shown only once)
   - The **S3 endpoint**, `https://<account id>.r2.cloudflarestorage.com` (the account id is also in
     the dashboard URL). Use the default endpoint, not a jurisdiction-specific one.

   The "Token value" on that page is for Cloudflare's own API and isn't needed.

### 4. Put the keys where they're used

None of these go in the repo.

| Where | What | Why |
| --- | --- | --- |
| A Modal secret named `tastr-r2` | `modal secret create tastr-r2 AWS_ACCESS_KEY_ID=<access key id> AWS_SECRET_ACCESS_KEY=<secret>` | The `bench` function mounts the bucket with these. The names must be exactly `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, even though it's R2. |
| Your shell profile (`~/.zshrc`) | `export TASTR_R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com` | `modal deploy` needs it. Open a new terminal afterwards. |
| An rclone remote named `r2` | see [Uploading datasets](#uploading-datasets) | Copying datasets from your machine to the bucket. |

The secret can also be made in the Modal dashboard: Secrets > Create new secret > Custom.

### 5. Deploy

```
modal deploy tastr/cloud/modal_app.py
```

Run it again after every engine change. The first deploy builds two images and takes several minutes;
the stems image downloads PyTorch and the Demucs weights. The app then shows up as `tastr` in the
Modal dashboard, with the functions `bench` and `stems`.

### 6. Check it

```
tastr bench synth --remote --limit 4      # synthetic tracks, needs no datasets
```

## Uploading datasets

Keep the same folder layout as on disk: each dataset is a top-level folder in the bucket, laid out the way
its loader expects ([bench.md](bench.md#datasets)). With [rclone](https://rclone.org), once:

```
rclone config create r2 s3 provider=Cloudflare access_key_id=<id> secret_access_key=<secret> \
  endpoint=https://<account id>.r2.cloudflarestorage.com
tastr bench beatport-key ~/Development/tastr-data/beatport-key --write-index
rclone copy ~/Development/tastr-data/beatport-key r2:tastr-data/beatport-key --progress
tastr bench giantsteps-key ~/Development/tastr-data/giantsteps-key --write-index
rclone copy -L ~/Development/tastr-data/giantsteps-key r2:tastr-data/giantsteps-key --progress
```

`--write-index` saves the loader's track list as `tastr-index.json` in the folder. A remote run reads
that one file instead of walking the bucket: on a cold container the walk took about 5 minutes for
Beatport (3,000 files), the same for 5 tracks as for 200. Without an index the remote run still works,
just slowly. Rewrite and re-upload the index whenever the folder's contents change. `-L` follows
symlinks, which the GiantSteps staging uses.

Each remote run prints where its time went: container start, imports, loading the dataset, analysis.

Upload only what the loaders read (audio and annotations), not result reports or feature caches. The
bucket must stay private: Beatport EDM Key and GiantSteps are CC BY-SA and not ours to republish.

## How it fits together

- `tastr/cloud/modal_app.py`: the Modal app. Its image installs the engine's dependencies (kept in step
  with `pyproject.toml` by `tests/test_cloud.py`) and the local `tastr` package. The bucket is mounted
  read-only at `/data`.
- `tastr/cloud/jobs.py`: the job bodies, plain Python so tests run them without Modal.
- `tastr/cloud/remote.py`: what the CLI and `tastr/stems.py` call; looks up the deployed function by name.

## Not here yet

- A GitHub workflow to start runs. If one is added, keep it a thin client: `workflow_dispatch` or a
  schedule only, a `timeout-minutes`, and a concurrency group, so no heavy work lands on GitHub's runners.

## Status

The Modal function and mount were written against Modal 1.6 and tested locally with a stubbed Modal
client; the first real run needs the setup above.
