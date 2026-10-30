# Security

Tastr is a local app: `tastr serve` binds to 127.0.0.1 and has no login. Don't expose it
to a network you don't trust. To share it with a few invited people, run it in hosted mode behind
Cloudflare Access ([docs/hosting.md](docs/hosting.md)), which requires a login on every request and
turns off link analysis and the SQL box. The one exception is `/pub/`: links signed with
`TASTR_LINK_SECRET` that serve one track's audio and stems to strudel.cc for a week.

## Reporting a vulnerability

Please report security problems privately through GitHub: on this repository's
**Security** tab, choose **Report a vulnerability**. Don't open a public issue for them.

## Secrets

Tastr never needs secrets in the repository. API keys for catalog lookups
(`GETSONGBPM_API_KEY`, `FREQBLOG_API_KEY`) and cloud credentials (the Modal secret
`tastr-r2`, `TASTR_R2_ENDPOINT`) come from environment variables or the provider's own
secret store; see docs/lookup.md and docs/cloud.md. A hosted server's keys (Modal token, R2 keys for the
Litestream backup, `TASTR_LINK_SECRET`) are Fly secrets, and the deploy workflow's are GitHub secrets;
see docs/deploy.md.
