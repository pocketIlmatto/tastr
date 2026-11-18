# Catalog lookups

Tastr can ask online music catalogs what BPM and key they list for a track and show that on the card
next to its own figures ("Listed elsewhere"), marked as matching ours, half or double our tempo, a
related key, or different. The catalogs never replace the engine's numbers.

The line under the card's numbers shows only the sources that found the track. The card's **Metadata**
section ends with a "Listed elsewhere" table that always lists every source: what it found, or why it has
nothing yet (not looked up yet, not found, no artist and title tags, the lookup failed, not set up on this
server, or for Rekordbox, not imported yet or not in your export).

Lookups are **off by default**. When on, they send the track's **artist and title** (from its tags)
to each provider you enabled; no audio and nothing else leaves the machine. Tracks without artist and
title tags are skipped.

## Turning it on

Set these in the shell you run `tastr serve` / `tastr analyze` from, never in the repo:

```bash
export TASTR_LOOKUP=getsongbpm,freqblog,reccobeats,acousticbrainz   # providers to ask, in order
export GETSONGBPM_API_KEY=...               # https://getsongbpm.com/api (free; requires a link back)
export FREQBLOG_API_KEY=...                 # https://api.freqblog.com/docs (free tier 1,000 requests a month)
export FREQBLOG_STORE=0                     # optional: don't store FreqBlog's answers (stored by default; see below)
```

ReccoBeats needs nothing set up. AcousticBrainz needs no key but a local index of its dump; see [AcousticBrainz](#acousticbrainz)
below. A provider named in `TASTR_LOOKUP` that isn't set up (no key, no index) is skipped with a
warning.

Then:

- every newly analyzed track is looked up right after analysis. Re-analyzing doesn't ask again for a
  provider that already answered (found or not found), so it doesn't spend quota;
- "Look up BPM & key online" in a track's ⋯ menu, or in the sidebar's bulk actions, asks again;
- `tastr lookup [IDS...] [--missing] [--providers a,b]` does the same from a terminal (all tracks when
  no ids are given); `tastr lookup --usage` shows requests used against each limit;
  `tastr lookup --forget freqblog` deletes every stored answer from one provider.

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

A provider whose terms don't allow keeping its answers (FreqBlog with `FREQBLOG_STORE=0`) is shown
only for the lookup that fetched it ("Look up BPM & key online", or `tastr lookup`) and isn't stored or
asked during analysis.

A provider can also keep a "not found" only for a while (`miss_days`; FreqBlog 7 days, or the
`retry_after_days` it sends with the answer). Once that passes, the next analysis or `--missing` run
asks again, since the catalog may have added the track.

`status` is `found`, `not_found`, `pending` (FreqBlog queued the track for analysis; asked again next
time), `no_tags` or `error` (the message is in `result_json`).

## Guardrails

Nothing should be able to hammer a provider: not a bulk run over a big library, not a stuck client, and
not a hostile visitor once Tastr is shared. Every request a provider sends goes through
`tastr/lookup/guard.py`, wherever the lookup started:

| Guardrail | Default | Override |
|---|---|---|
| Spacing: one request at a time per provider, at least this far apart | 1 s (AcousticBrainz 1.1 s, for MusicBrainz's one a second) | `TASTR_LOOKUP_<NAME>_INTERVAL` |
| Requests per UTC day | FreqBlog 60, GetSongBPM 1,000 (its own limit is 3,000 an hour), ReccoBeats 1,000 (about 200 tracks), AcousticBrainz 1,000 (MusicBrainz searches) | `TASTR_LOOKUP_<NAME>_DAILY` |
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
| `freqblog` | BPM, half-time BPM, key, Camelot, Open Key | Key in an `X-API-Key` header. Fields per its OpenAPI spec (`track_name`, `artist_name`, ...). It returns its closest match, so a different song is treated as not found. Unknown tracks may come back 202 "queued". Its [terms](https://freqblog.com/terms.html#free-use) (3 November 2026, section 6.2) let a free key store the values it returns for a non-commercial project, used inside that project only: never publish them as a file or dataset (a public repository included) or offer them as an export, API or feed (so they are left out of the library snapshot the hosted search downloads, `in_downloads = False`), and credit "Audio analysis by FreqBlog" where a user can find it. A "not found" may be cached only for a while, so it expires (see above). If the key is revoked or the project turns commercial, delete what is stored within 30 days (`tastr lookup --forget freqblog`) or move to a paid plan. One free key per person. `FREQBLOG_STORE=0` stops storing. Host override: `FREQBLOG_API_BASE`. |
| `reccobeats` | BPM only (energy, danceability and valence kept in `result_json`) | Free, no key; its [terms](https://reccobeats.com/docs/documentation/terms-of-service) allow personal and commercial use and keeping answers, but it may block clients that send too much. No track search, so a lookup is an artist search, that artist's tracks 50 at a time (up to 10 pages) until the title matches, then the track's audio features: about five requests. A remix, live take or DJ mix only matches the same version; extended and radio cuts count as the same track, closest length first. Its key isn't used: on 38 tracks (November 2026) it matched Rekordbox's on 14, against 30 for Tastr's, while its BPM matched on 31 (Tastr's on 28). Host override: `RECCOBEATS_API_BASE`. |
| `acousticbrainz` | BPM, key and scale (Essentia estimates from 2022 and earlier) | Public domain (CC0), so answers are stored. Looked up by MusicBrainz recording id from a local index of the dump; only the MusicBrainz search goes online. See below. |
| `rekordbox` | BPM and key from your own Rekordbox collection export | Never looked up and not in `TASTR_LOOKUP`: stored for the tracks in both libraries by `/api/admin/rekordbox-compare?save=1` ([rekordbox.md](rekordbox.md)), then shown and compared like the others. |

The catalogs' figures are estimates too (theirs or their users'), so a disagreement means "check this
track", not "Tastr is wrong". The provider modules were written from each API's published examples;
check them against a live key after an API change.

## AcousticBrainz

[AcousticBrainz](https://acousticbrainz.org) analyzed millions of recordings with Essentia and keyed
them by MusicBrainz recording id (MBID). It stopped taking submissions in 2022 and its API has shut
down, but its data was released as public-domain dumps. Tastr uses two of them, the low-level
"rhythm" (`bpm`) and "tonal" (`key_key`, `key_scale`) feature CSVs, which hold one row per submission;
the index keeps each recording's first submission. Anything released after mid-2022 isn't there.

A lookup is:

0. when the file is tagged with a MusicBrainz recording id (MusicBrainz Picard writes one; ID3 `UFID`,
   MP4 `MusicBrainz Track Id`, Vorbis `MUSICBRAINZ_TRACKID`), that id read straight from the index, with
   no search. With an ISRC tag (`TSRC` / `ISRC`), one search for the recordings listed under it, taken
   without title matching. If neither gives an answer, the steps below run as usual;
1. a [MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API) recording search by title and the
   first-listed artist (no key; one request a second at most, with a user agent naming Tastr). A hit
   must match the title, the whole artist credit (in any order, as for the other providers) and, when
   both sides know it, the length within 15 s, so a live or extended version isn't taken for the album
   one. Up to 25 results are read;
2. those MBIDs, closest length first, in the local index. The first with a BPM or key is the answer,
   linked to its MusicBrainz page;
3. when none of them is in the index, one more search for the other recordings with the same ISRCs.
   MusicBrainz often lists one song as several recordings and AcousticBrainz analyzed only some.

A song still missing after that is "not found" (`result_json` lists the MBIDs tried and how many came
from the ISRC search). A dropped or cut-off MusicBrainz response, or a 503 (MusicBrainz is busy), is tried
once more; a second failure comes back as an error naming MusicBrainz. MusicBrainz's `x-ratelimit-*`
headers count its global load, not Tastr's requests, so the guard doesn't pause on them.

In a test on a small library of mostly 2019-2022 house releases (October 2026), no track was found:
AcousticBrainz's coverage of recent electronic releases is thin. It suits older catalogs better. A
larger check in November 2026, on 58 tracks of a hosted library, found 8 (14%). By MusicBrainz's
first-release year: 4 of 4 before 2015, 3 of 9 from 2015-2018, 1 of 8 from 2019-2022 and 0 of 20 from
2023 on, which the dump can't hold. Of the 50 misses, 33 were on MusicBrainz but never analyzed and 17
weren't on MusicBrainz at all. So expect it to answer for older tracks and rarely for new releases.

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

### On the hosted site

The Worker has no lasting disk, so there the index lives in a D1 database, `tastr-acousticbrainz`, bound
as `ACOUSTICBRAINZ` (`worker/wrangler.jsonc`, [hosting-map.md](hosting-map.md)). Each lookup reads it with
one query of up to 25 recording ids; the MusicBrainz search is the same as anywhere else. The database is
about 0.4 GB, which Workers Paid's included D1 storage (5 GB) covers.

Set it up once:

1. Give the deploy token (`CLOUDFLARE_API_TOKEN`, [settings.md](settings.md)) **D1 Edit** and **Workers
   R2 Storage Read** on the account, if it doesn't have them. A deploy needs D1 Edit: it finds the database
   by name, or makes an empty one (wrangler's automatic provisioning).
2. Run **Actions > acousticbrainz-d1 > Run workflow**. It reads `acousticbrainz/acousticbrainz-index.sqlite`
   from the `tastr-data` bucket (put there by `tastr fetch acousticbrainz ... --remote`), writes it out as SQL
   (`d1_sql` in `tastr/lookup/acousticbrainz.py`, half a million rows a file), makes the database if a deploy
   hasn't, and imports the files one after another; about 7.6 million rows written, inside the 50 million a
   month Workers Paid includes. Every file can be run again, so if a run stops, run it again with `from_file`
   set to the file it stopped at. It ends by counting the rows (7,564,215).
3. Add `acousticbrainz` to the Worker's `TASTR_LOOKUP` secret, for example
   `freqblog,reccobeats,acousticbrainz`.

With the binding but no rows loaded yet, a lookup comes back as an error naming D1 ("no such table"), so
leave it out of `TASTR_LOOKUP` until the workflow has finished.

The dump URLs and file names are in `tastr/lookup/acousticbrainz.py` (`DUMP_BASE`, `DUMP_FILES`); the
column layout comes from AcousticBrainz's own dump code (`db/dump.py` in metabrainz/acousticbrainz-server).

## Album art for mash-up previews

When `TASTR_LOOKUP` names any provider (or `TASTR_COVER_ART=1`; `0` turns it off), a mash-up preview
(the short link's picture, [features.md](features.md)) gets album art for a track whose file has none:
`tastr/coverart.py` finds the track's MusicBrainz recording (its tags, a stored AcousticBrainz match, else
the same search AcousticBrainz uses), the recording's releases (official ones and the tagged album first),
then the release group's or release's front cover from the [Cover Art Archive](https://coverartarchive.org)
at 500 px. MusicBrainz requests share AcousticBrainz's guardrails (one a second), and the answer is kept per
track, a miss included, so a track is asked about once. Only the people whose links may show album art get it:
admins on a shared server, anyone on their own machine. The images belong to their copyright owners; the
archive grants no licence, so they go only into those previews.

## Lyrics

With `TASTR_LYRICS=lrclib` (off when unset), a track's lyrics come from [LRCLIB](https://lrclib.net), timed line by
line when it has them (`tastr/lyrics.py`). It needs no key. `POST /api/tracks/{id}/lyrics` asks it with the
track's artist, title, album and length from its tags: first its exact match (`/api/get`, length within about
2 s), then its search, keeping a hit whose artist and title match and whose length is within 4 s, timed lyrics
first. The answer is stored in the `lyrics` table (`found` with `synced` lines `[{t, line}]` and `plain` text,
`not_found`, `no_tags` or `error`) and kept across re-analysis; an error never replaces lyrics already found.
`GET /api/tracks/{id}/lyrics` and the card (`lyrics`, `lyrics_available`) read it back. Requests go through the
same guardrails as the catalogs, as provider `lrclib`: one at a time, at least 1 s apart, 1,000 a day
(`TASTR_LOOKUP_LRCLIB_INTERVAL`, `TASTR_LOOKUP_LRCLIB_DAILY`).

Lyrics belong to their writers and publishers, and LRCLIB, whose lyrics come from its users, grants no licence for
them. So they are shown only to the person whose library the track is in: the `lyrics` table isn't in the library
copy the browser downloads for searches, nor in shared links, and the tests use made-up lines.

Karaoke plays the track without its vocals (`tastr/karaoke.py`): once a track has stems, `POST
/api/tracks/{id}/karaoke` mixes its bass, drums and other stems into `stems/<hash>/instrumental.flac` (a few
seconds; on Modal's `karaoke` function on the hosted site), and `GET /api/tracks/{id}/karaoke/audio` plays it.
It goes with the stems and is mixed again after they are separated again.

### Lyrics by listening

For tracks LRCLIB doesn't have, or has without times, `POST /api/tracks/{id}/lyrics/listen` has Whisper listen to
the track's vocal stem (`tastr/listen.py`, `tastr/transcribe.py`: faster-whisper and the large-v3-turbo weights,
both MIT). It runs in the background, separating the stems first when there are none; the card's
`lyrics.listen` says where it is (`queued`, `separating`, `listening`, `error`). Then:

- LRCLIB's plain lyrics keep their words and get line times from what was heard (`timed_by: "whisper"`). A line it
  couldn't place gets a time between its neighbours and `guess: true`.
- Otherwise what was heard is stored as the lyrics, with `source: "whisper"`. They may be wrong, and the page says
  so. A later LRCLIB answer replaces them only when LRCLIB has the words.

On the hosted site it runs in Modal's `transcribe` function on a T4 (about 5-10 s a track once it has stems); each
listen counts toward the daily stem separations. A local server needs `pip install 'tastr[transcribe]'` and runs
Whisper on its CPU, which is slow.

What the trial on 5 tracks taught (2026-11-17, whole tracks, separated first): clear singing and rap were about 3 in
4 words right (word error rate 0.23 and 0.28), with 66-83% of lines starting within 1 s of LRCLIB's. Dense club
tracks with repeated hooks were 0.68-0.83: Whisper goes quiet on repeats instead of making words up. Whisper took
4-8 s a track; separating the stems took 45-80 s and most of the $0.17 the run cost. Line breaks come from pauses,
full stops and capitals; Whisper puts commas inside sung lines, so a comma counts for little.

To try it on a few files on Modal without deploying anything:

    modal run tastr/cloud/modal_app.py::transcribe_files --files "a.flac,b.m4a" --out ~/tastr-whisper-try

It separates each file first (skip with `--no-separate` for vocal stems), writes what it heard and LRCLIB's lyrics to
`<out>/<file>.json` (keep that folder out of the repository), and prints, per track, the word error rate against
LRCLIB's lyrics, how close each timed line starts, and `lines_heard`, the share of lines at least half heard while
they were sung (`score`).

### Your own edits

People can fix a track's lyrics and line times themselves. `PUT /api/tracks/{id}/lyrics/edits` takes their lines,
`{synced: [{t, line}]}` (t in seconds, `null` for a line not timed yet) or `{plain: text}`, and stores them in the
`lyrics_edits` table, apart from the LRCLIB or Whisper answer in `lyrics` (`tastr/lyrics.py`: `edit_lines`). So
looking on LRCLIB again, listening again and re-analyzing never overwrite them. Timed lines are kept in time order;
when some have no time the order is kept and those get `guess: true`. The lyrics the page reads then carry the
person's `synced` and `plain` with `edited: true`, `edited_at` and `base` (`lrclib`, `whisper` or `none`: what they
started from). When LRCLIB or Whisper gives a different answer later, `newer: true` says so;
`POST /api/tracks/{id}/lyrics/edits/keep` keeps the edits and clears it, and `DELETE /api/tracks/{id}/lyrics/edits`
drops them so the latest answer shows again. Listening on edited lyrics that have no times times the person's
words (`timed_by: "whisper"`) and leaves them theirs. Edits are private like the rest of the lyrics.

## Adding a provider

1. Add `tastr/lookup/<name>.py` with a `base.Provider` subclass: set `name`, `label`, `homepage` and
   `key_env` (or override `configured()` and `setup_hint()` when it needs something other than a key),
   and implement `lookup(TrackQuery) -> LookupResult` (status `found` / `not_found` /
   `pending`; raise `ProviderError` for anything else). Return the key as the provider spells it;
   `lookup_track` parses it into tonic / mode / Camelot. Override `may_store()` if the provider's terms
   forbid keeping results, set `miss_days` if they allow caching a "not found" only for a while, and set `min_interval` / `daily_limit` / `monthly_limit` to fit its quota. `Provider.get_json` does the HTTP with a timeout
   and a Tastr user agent; `base.num` and `base.same_text` help with loose fields and fuzzy matches.
2. Call `register(MyProvider())` (from `.base`) at the bottom of the module, and add the module to the
   provider import at the top of `tastr/lookup/__init__.py`.
3. Add tests on canned responses in `tests/test_lookup.py`, a row in the table above, and the
   provider's attribution or usage terms to the README's Licenses section.
