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

1. **Modal**: create an account, then on your machine:
   ```
   pip install -e '.[cloud]'
   modal token new
   ```
2. **R2 bucket**: in the Cloudflare dashboard, create a bucket named `tastr-data` (or set
   `TASTR_R2_BUCKET` when deploying). Note your account id; the endpoint is
   `https://<account id>.r2.cloudflarestorage.com`.
3. **R2 API token**: R2 > Manage API tokens > create a token with Object Read & Write on that bucket.
   Keep the access key id and secret; Cloudflare shows the secret once.
4. **Modal secret** with those keys (the mount reads them under these names):
   ```
   modal secret create tastr-r2 AWS_ACCESS_KEY_ID=<access key id> AWS_SECRET_ACCESS_KEY=<secret>
   ```
5. **Deploy** (again after every engine change):
   ```
   TASTR_R2_ENDPOINT=https://<account id>.r2.cloudflarestorage.com modal deploy tastr/cloud/modal_app.py
   ```

## Uploading datasets

Keep the same folder layout as on disk: each dataset is a top-level folder in the bucket, laid out the way
its loader expects ([bench.md](bench.md#datasets)). With [rclone](https://rclone.org) (`brew install rclone`):

```
rclone config create r2 s3 provider=Cloudflare access_key_id=<id> secret_access_key=<secret> \
  endpoint=https://<account id>.r2.cloudflarestorage.com
rclone copy ~/Development/tastr-data/beatport-key r2:tastr-data/beatport-key --progress
rclone copy ~/Development/tastr-data/giantsteps-key r2:tastr-data/giantsteps-key --progress
```

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
