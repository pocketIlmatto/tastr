# Catalog lookups

Tastr can ask online music catalogs what BPM and key they list for a track and show that on the card
next to its own figures ("Listed elsewhere"), marked as matching ours, half or double our tempo, a
related key, or different. The catalogs never replace the engine's numbers.

Lookups are **off by default**. When on, they send the track's **artist and title** (from its tags)
to each provider you enabled; no audio and nothing else leaves the machine. Tracks without artist and
title tags are skipped.

## Turning it on

Set these in the shell you run `tastr serve` / `tastr analyze` from, never in the repo:

```bash
export TASTR_LOOKUP=getsongbpm,freqblog,acousticbrainz   # providers to ask, in order
export GETSONGBPM_API_KEY=...               # https://getsongbpm.com/api (free; requires a link back)
export FREQBLOG_API_KEY=...                 # https://api.freqblog.com/docs (free tier 1,000 requests a month)
export FREQBLOG_PLAN=paid                   # only on a paid FreqBlog plan: lets Tastr store its answers (see below)
```

AcousticBrainz needs no key but a local index of its dump; see [AcousticBrainz](#acousticbrainz)
below. A provider named in `TASTR_LOOKUP` that isn't set up (no key, no index) is skipped with a
warning.

Then:

- every newly analyzed track is looked up right after analysis. Re-analyzing doesn't ask again for a
  provider that already answered (found or not found), so it doesn't spend quota;
- "Look up BPM & key online" in a track's ⋯ menu, or in the sidebar's bulk actions, asks again;
- `tastr lookup [IDS...] [--missing] [--providers a,b]` does the same from a terminal (all tracks when
  no ids are given); `tastr lookup --usage` shows requests used against each limit.

Results are stored per track and provider in the `lookups` table (`status`, `bpm`, `key_name` as
listed, `key_tonic` / `key_mode` / `camelot` parsed from it, `fetched_at`, `result_json`) and survive
re-analysis, so they can be queried:

```sql
SELECT t.title, t.bpm, l.provider, l.bpm AS listed_bpm FROM tracks t JOIN lookups l ON l.track_id = t.id
WHERE l.status = 'found' AND abs(t.bpm - l.bpm) > 0.04 * l.bpm;
-- Tracks whose listed key isn't ours
SELECT t.title, t.camelot, l.provider, l.camelot AS listed FROM tracks t JOIN lookups l ON l.track_id = t.id
WHERE l.status = 'found' AND l.camelot IS NOT NULL AND l.camelot <> t.camelot;
```

A provider whose terms don't allow keeping its answers (FreqBlog on its free plan) is shown only for
the lookup that fetched it ("Look up BPM & key online", or `tastr lookup`) and isn't stored or asked
during analysis.

`status` is `found`, `not_found`, `pending` (FreqBlog queued the track for analysis; asked again next
time), `no_tags` or `error` (the message is in `result_json`).

## Guardrails

Nothing should be able to hammer a provider: not a bulk run over a big library, not a stuck client, and
not a hostile visitor once Tastr is shared. Every request a provider sends goes through
`tastr/lookup/guard.py`, wherever the lookup started:

| Guardrail | Default | Override |
|---|---|---|
| Spacing: one request at a time per provider, at least this far apart | 1 s (AcousticBrainz 1.1 s, for MusicBrainz's one a second) | `TASTR_LOOKUP_<NAME>_INTERVAL` |
| Requests per UTC day | FreqBlog 60, GetSongBPM 1,000 (its own limit is 3,000 an hour), AcousticBrainz 1,000 (MusicBrainz searches) | `TASTR_LOOKUP_<NAME>_DAILY` |
| Requests per calendar month | FreqBlog 900 (its free tier is 1,000), GetSongBPM none | `TASTR_LOOKUP_<NAME>_MONTHLY` |
| The provider's own quota headers (`x-quota-used` / `x-quota-limit`, `x-ratelimit-remaining`) | pause 20 requests short of its limit until its reset time | |
| HTTP 429 / 5xx | pause for Retry-After, else 1 min doubling to 1 h | |
| HTTP 401 / 403 (bad key) | pause 1 h | |
| The same track asked again (menu clicks, bulk re-runs) | skipped for 10 min after an answer (not after an error) | `TASTR_LOOKUP_REFRESH_MINUTES` |

Usage is counted per provider and day in the `lookup_usage` table, so restarting doesn't reset it
(`tastr lookup --usage` shows it). A request a guardrail holds back is never sent; the lookup comes back
"limited" with the reason, and neither that nor an error replaces an answer already stored. Lookups only
ever use a track's own tags, so a visitor can't make Tastr query arbitrary text, only re-ask about
tracks in the library, which the limits above cap.

The web server also refuses POST / PUT / DELETE requests that a browser sends from another site, so a
page open in another tab can't trigger lookups (or analysis, deletes, stems). That is not a login:
scripts and curl still get through, so a server other people can reach needs real authentication first.

## Providers

| Name | What it returns | Terms to keep |
|---|---|---|
| `getsongbpm` | tempo, key (`key_of`), time signature, Open Key | Free with a key, sent in the `X-API-KEY` header. A visible link back to getsongbpm.com is "REQUIRED (website or store listing)", or the account can be suspended; the card links each listing to its GetSongBPM page and the README links the site, but whether that satisfies them for a local app is worth confirming with GetSongBPM. Search hits use `song_title` / `song_id` (also read: `title` / `id`). Host override: `GETSONGBPM_API_BASE`. |
| `freqblog` | BPM, half-time BPM, key, Camelot, Open Key | Key in an `X-API-Key` header. Fields per its OpenAPI spec (`track_name`, `artist_name`, ...). It returns its closest match, so a different song is treated as not found. Unknown tracks may come back 202 "queued". Caching results is allowed only on paid plans: answers are stored only with `FREQBLOG_PLAN=paid`. Host override: `FREQBLOG_API_BASE`. |
| `acousticbrainz` | BPM, key and scale (Essentia estimates from 2022 and earlier) | Public domain (CC0), so answers are stored. Looked up by MusicBrainz recording id from a local index of the dump; only the MusicBrainz search goes online. See below. |

Both catalogs' figures are estimates too (theirs or their users'), so a disagreement means "check this
track", not "Tastr is wrong". The provider modules were written from each API's published examples;
check them against a live key after an API change.

## AcousticBrainz

[AcousticBrainz](https://acousticbrainz.org) analyzed millions of recordings with Essentia and keyed
them by MusicBrainz recording id (MBID). It stopped taking submissions in 2022 and its API has shut
down, but its data was released as public-domain dumps. Tastr uses two of them, the low-level
"rhythm" (`bpm`) and "tonal" (`key_key`, `key_scale`) feature CSVs, which hold one row per submission;
the index keeps each recording's first submission. Anything released after mid-2022 isn't there.

A lookup is:

1. a [MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API) recording search by title and the
   first-listed artist (no key; one request a second at most, with a user agent naming Tastr). A hit
   must match the title, the whole artist credit (in any order, as for the other providers) and, when
   both sides know it, the length within 15 s, so a live or extended version isn't taken for the album
   one. Up to 25 results are read;
2. those MBIDs, closest length first, in the local index. The first with a BPM or key is the answer,
   linked to its MusicBrainz page;
3. when none of them is in the index, one more search for the other recordings with the same ISRCs.
   MusicBrainz often lists one song as several recordings and AcousticBrainz analyzed only some.

A song still missing after that is "not found". A dropped or cut-off MusicBrainz response is tried once
more; an HTTP error (MusicBrainz answers 503 when it's busy) comes back as an error naming MusicBrainz,
and the guardrails pause it before the next ask.

In a test on a small library of mostly 2019-2022 house releases (October 2026), no track was found:
AcousticBrainz's coverage of recent electronic releases is thin. It suits older catalogs better.

Build the index once:

```bash
tastr fetch acousticbrainz acousticbrainz --remote      # on Modal, into the R2 bucket (docs/cloud.md)
# or the same without redeploying the shared app, from a checkout of main:
modal run tastr/cloud/modal_app.py::fetch --dataset acousticbrainz --path acousticbrainz
# or on this machine (needs `pip install zstandard` or the zstd tool):
tastr fetch acousticbrainz $TASTR_DATA/acousticbrainz
```

`--remote` calls the deployed app, so it needs a `modal deploy` from a checkout that has this provider.
All of them download the two `.tar.zst` archives (rhythm 1.26 GB, tonal 0.88 GB) from data.metabrainz.org (kept whole, skipped when already
there) and write `acousticbrainz-index.sqlite` beside them. Put the index at
`~/.tastr/acousticbrainz-index.sqlite` (from the bucket, download it in the Cloudflare dashboard or
with any S3 client), or point `ACOUSTICBRAINZ_INDEX` at it. The provider counts as set up when the
index exists.

| Setting | Default |
|---|---|
| `ACOUSTICBRAINZ_INDEX` | `$TASTR_HOME/acousticbrainz-index.sqlite` |
| `ACOUSTICBRAINZ_API_BASE` | unset. Instead of an index: a server that still speaks the AcousticBrainz API (its bulk `/api/v1/low-level` endpoint), asked for up to 25 MBIDs at once |
| `MUSICBRAINZ_API_BASE` | `https://musicbrainz.org/ws/2` (a mirror also works) |

The dump URLs and file names are in `tastr/lookup/acousticbrainz.py` (`DUMP_BASE`, `DUMP_FILES`); the
column layout comes from AcousticBrainz's own dump code (`db/dump.py` in metabrainz/acousticbrainz-server).

## Adding a provider

1. Add `tastr/lookup/<name>.py` with a `base.Provider` subclass: set `name`, `label`, `homepage` and
   `key_env` (or override `configured()` and `setup_hint()` when it needs something other than a key),
   and implement `lookup(TrackQuery) -> LookupResult` (status `found` / `not_found` /
   `pending`; raise `ProviderError` for anything else). Return the key as the provider spells it;
   `lookup_track` parses it into tonic / mode / Camelot. Override `may_store()` if the provider's terms
   forbid keeping results, and set `min_interval` / `daily_limit` / `monthly_limit` to fit its quota. `Provider.get_json` does the HTTP with a timeout
   and a Tastr user agent; `base.num` and `base.same_text` help with loose fields and fuzzy matches.
2. Call `register(MyProvider())` at the bottom and import the module at the end of
   `tastr/lookup/__init__.py`.
3. Add tests on canned responses in `tests/test_lookup.py`, a row in the table above, and the
   provider's attribution or usage terms to the README's Licenses section.
