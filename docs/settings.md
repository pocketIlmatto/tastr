# Secrets and settings

Every secret and credential Tastr uses, where it lives, and how to renew it. Values never go in this repository,
a chat or an issue; set them at their source and in the place listed here.
[hosting-map.md](hosting-map.md#where-each-setting-lives) has the plain (non-secret) settings and what each one
does, [hosting.md](hosting.md#settings) their defaults.

When you add, remove or rename a secret, update this page in the same change.

## Renewal calendar

Tokens with a known end date. Add a row whenever you make a token that expires, and renew it a couple of weeks
early: nothing warns before it lapses.

| Token | Expires | What stops working |
| --- | --- | --- |
| Read-only Cloudflare API token in the Claude Code cloud environment (see [below](#claude-code-cloud-environment)) | 2027-01-31 | Claude sessions can no longer read the Worker's logs or settings |
| `CLAUDE_CODE_OAUTH_TOKEN` | about a year after `claude setup-token` made it | slow-tests and worker-errors open their issue but Claude doesn't start |
| `ISSUES_TOKEN` | the date picked when it was made | worker-errors can't open or comment on its issue, so the run goes red |
| `TASTR_GITHUB_TOKEN`, and any other fine-grained GitHub token | the date picked when it was made (GitHub shows it under **Settings > Developer settings > Fine-grained tokens**) | the Admin log's GitHub lines |

The other tokens below don't expire unless one was set when it was made; check at the source if unsure.

## GitHub Actions secrets

Repository **Settings > Secrets and variables > Actions**.

| Secret | Used by | Made at | Renew |
| --- | --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` | `deploy-worker.yml`, `acousticbrainz-d1.yml` | Cloudflare **My Profile > API Tokens**, "Edit Cloudflare Workers" template, plus **D1 Edit** and **Workers R2 Storage Read** on the account (the Worker's D1 binding and the AcousticBrainz load, [lookup.md](lookup.md#on-the-hosted-site)) | Make a new one, replace the secret, run **Actions > deploy-worker**, revoke the old one |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | `deploy.yml`, `bench-smoke.yml` | Modal **Settings > API Tokens** | Same: new token, replace both, run **deploy**, revoke the old |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | `health.yml` | Cloudflare **Zero Trust > Access > Service credentials > Service Tokens**, with a Service Auth policy on the Tastr app ([ci.md](ci.md#live-health-check)) | New service token, replace both, add it to the policy, revoke the old; note its end date in the calendar above |
| `ISSUES_REPO`, `ISSUES_TOKEN` (optional) | `worker-errors.yml` | `ISSUES_REPO` is `owner/name` of the repository that gets the issues (a secret so the public logs mask it). `ISSUES_TOKEN` is a fine-grained token from **GitHub Settings > Developer settings > Fine-grained tokens**: only that repository, **Issues: Read and write** ([ci.md](ci.md#worker-errors)). The variable `ISSUES_LABELS` (Variables tab, optional) adds labels | New token, replace `ISSUES_TOKEN`, delete the old; note its end date in the calendar above. Without both secrets the issues go to this repository |
| `CF_LOGS_TOKEN` | `worker-errors.yml` | Cloudflare custom token, Account > Workers Observability > Read only | New token, replace; a refused token turns the run red ([ci.md](ci.md#worker-errors)) |
| `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | `slow-tests.yml`, `worker-errors.yml`, `dependabot-fix.yml` (also under **Dependabot** secrets if that run says it can't see it) | `claude setup-token` (the owner's Claude plan), or the Anthropic Console (billed per use) | Run `claude setup-token` again and replace the secret |

Repository setting: **Settings > General > Pull Requests > Allow auto-merge** must be on for `automerge.yml`
([ci.md](ci.md#auto-merge-for-docs-and-tests)).

`GH_TOKEN` in the workflows is GitHub's own per-run token (`github.token`); there is nothing to set or renew.

## Worker secrets

Set from `worker/` with `uv run pywrangler secret put NAME`, or in **Workers & Pages > tastr > Settings >
Variables and Secrets** as type **Secret** (a Text variable is wiped by the next deploy). The full list, with
what each does, is in [hosting-map.md](hosting-map.md#worker-secrets). The ones that are real credentials:

| Secret | Made at | Renew |
| --- | --- | --- |
| `TASTR_AUDIO_ACCESS_KEY_ID`, `TASTR_AUDIO_SECRET_ACCESS_KEY` | Cloudflare **R2 > Manage API tokens**, Object Read & Write on `tastr-audio` only | New token, put both, then delete the old token |
| `TASTR_MODAL_KEY`, `TASTR_MODAL_SECRET` | Modal **Settings > Proxy Auth Tokens** | New token, put both, then delete the old one |
| `TASTR_LINK_SECRET` | `openssl rand -hex 32` | Put a new value. Every Strudel and playlist link already shared stops working |
| `TASTR_GITHUB_TOKEN` (optional) | GitHub fine-grained token, read-only Actions on this repository | Before its end date: new token, put it, delete the old |
| `FREQBLOG_API_KEY` (optional) | [FreqBlog](https://api.freqblog.com/docs) account | Only if it leaks |
| `ANTHROPIC_API_KEY` (optional) | Anthropic Console **Settings > API Keys**, with a monthly spend limit on its workspace | Only if it leaks; without it "Ask Claude to change it" is off |

`TASTR_ACCESS_TEAM_DOMAIN`, `TASTR_ACCESS_AUD`, `TASTR_ADMIN_EMAILS`, `FREQBLOG_STORE` (optional, `0` stops storing FreqBlog's answers) and the limits are kept as secrets to keep
them out of the repository, but aren't credentials; nothing to renew.

To see which names are set (never the values): `uv run pywrangler secret list` in `worker/`.

## Modal

| Credential | Used by | Renew |
| --- | --- | --- |
| Secret `tastr-r2` (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) | The benchmark functions, which mount the `tastr-data` bucket ([cloud.md](cloud.md)) | New R2 token scoped to `tastr-data`, then `modal secret create tastr-r2 ... --force` |
| The API token GitHub deploys with, and the Worker's proxy auth token | See above | See above |

## On your own machine

| Credential | Where | Notes |
| --- | --- | --- |
| Modal | `~/.modal.toml` (`modal token new`) | Your own login, for benches and `modal deploy` by hand |
| R2 for datasets | `~/.config/rclone/rclone.conf` | The same kind of `tastr-data` token as `tastr-r2` |
| Catalog lookups | environment variables `GETSONGBPM_API_KEY`, `FREQBLOG_API_KEY` | [lookup.md](lookup.md) |

## Claude Code cloud environment

Claude sessions in the cloud get a read-only Cloudflare Account API token (read access to the account, including
Workers Observability Read and Workers Scripts Read) as a network secret for `api.cloudflare.com`, set in the environment's settings on
claude.ai, not in this repository. It expires on 2027-01-31: make a new read-only token in Cloudflare, replace
the environment's secret, and revoke the old one.
