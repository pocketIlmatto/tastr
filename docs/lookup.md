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
export TASTR_LOOKUP=getsongbpm,freqblog     # providers to ask, in order
export GETSONGBPM_API_KEY=...               # https://getsongbpm.com/api (free; requires a link back)
export FREQBLOG_API_KEY=...                 # https://api.freqblog.com/docs (free tier 1,000 requests a month)
export FREQBLOG_PLAN=paid                   # only on a paid FreqBlog plan: lets Tastr store its answers (see below)
```

A provider named in `TASTR_LOOKUP` without its key is skipped with a warning.

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
| Spacing: one request at a time per provider, at least this far apart | 1 s | `TASTR_LOOKUP_<NAME>_INTERVAL` |
| Requests per UTC day | FreqBlog 60, GetSongBPM 300 | `TASTR_LOOKUP_<NAME>_DAILY` |
| Requests per calendar month | FreqBlog 900 (its free tier is 1,000), GetSongBPM none | `TASTR_LOOKUP_<NAME>_MONTHLY` |
| The provider's own quota headers (`x-quota-used` / `x-quota-limit`, `x-ratelimit-remaining`) | pause 20 requests short of its limit until its reset time | |
| HTTP 429 / 5xx | pause for Retry-After, else 1 min doubling to 1 h | |
| HTTP 401 / 403 (bad key) | pause 1 h | |
| The same track asked again (menu clicks, bulk re-runs) | skipped for 10 min | `TASTR_LOOKUP_REFRESH_MINUTES` |

Usage is counted per provider and day in the `lookup_usage` table, so restarting doesn't reset it
(`tastr lookup --usage` shows it). A request a guardrail holds back is never sent; the lookup comes back
"limited" with the reason, and neither that nor an error replaces an answer already stored. Lookups only
ever use a track's own tags, so a visitor can't make Tastr query arbitrary text, only re-ask about
tracks in the library, which the limits above cap.

## Providers

| Name | What it returns | Terms to keep |
|---|---|---|
| `getsongbpm` | tempo, key (`key_of`), time signature, Open Key | Free with a key, sent in the `X-API-KEY` header. A visible link back to getsongbpm.com is "REQUIRED (website or store listing)", or the account can be suspended; the card links each listing to its GetSongBPM page and the README links the site, but whether that satisfies them for a local app is worth confirming with GetSongBPM. Search hits use `song_title` / `song_id` (also read: `title` / `id`). Host override: `GETSONGBPM_API_BASE`. |
| `freqblog` | BPM, half-time BPM, key, Camelot, Open Key | Key in an `X-API-Key` header. Fields per its OpenAPI spec (`track_name`, `artist_name`, ...). It returns its closest match, so a different song is treated as not found. Unknown tracks may come back 202 "queued". Caching results is allowed only on paid plans: answers are stored only with `FREQBLOG_PLAN=paid`. Host override: `FREQBLOG_API_BASE`. |

Both catalogs' figures are estimates too (theirs or their users'), so a disagreement means "check this
track", not "Tastr is wrong". The provider modules were written from each API's published examples;
check them against a live key after an API change.

## Adding a provider

1. Add `tastr/lookup/<name>.py` with a `base.Provider` subclass: set `name`, `label`, `homepage` and
   `key_env`, and implement `lookup(TrackQuery) -> LookupResult` (status `found` / `not_found` /
   `pending`; raise `ProviderError` for anything else). Return the key as the provider spells it;
   `lookup_track` parses it into tonic / mode / Camelot. Override `may_store()` if the provider's terms
   forbid keeping results, and set `min_interval` / `daily_limit` / `monthly_limit` to fit its quota. `Provider.get_json` does the HTTP with a timeout
   and a Tastr user agent; `base.num` and `base.same_text` help with loose fields and fuzzy matches.
2. Call `register(MyProvider())` at the bottom and import the module at the end of
   `tastr/lookup/__init__.py`.
3. Add tests on canned responses in `tests/test_lookup.py`, a row in the table above, and the
   provider's attribution or usage terms to the README's Licenses section.
