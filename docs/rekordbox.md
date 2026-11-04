# Tastr vs Rekordbox

How Tastr's analysis compares with [Rekordbox](https://rekordbox.com)'s (AlphaTheta / Pioneer DJ), first by
feature and then by accuracy on the same tracks. Rekordbox is a desktop app, so the benchmark doesn't run it: it
reads what Rekordbox exported (its collection XML) and scores that with the same metrics `tastr bench` uses for
Tastr ([bench.md](bench.md#what-gets-scored)).

## Features side by side

What each one works out from the audio by itself. Things you set by hand in either app (hot cues, comments,
ratings, My Tag) are left out. Rekordbox's column describes versions 6 and 7.

| Feature | Rekordbox | Tastr | Compared by the bench |
|---|---|---|---|
| BPM | Yes. "Normal" analysis within a BPM range you set, or "Dynamic" for tracks whose tempo drifts | Yes, with how likely it is to be right; drum & bass is read at its full tempo | Yes: BPM within 4%, and with x2 / half |
| Beat grid | Yes. One steady grid, or several tempo anchors with Dynamic analysis | Yes: beat and bar times | Yes: beat F and downbeat (bar 1) F at 70 ms |
| Time signature | The grid is 4/4 | 4/4 and 3/4 | Yes, where the dataset labels it (Ballroom waltzes) |
| Key | Yes, shown as classic ("Am") or alphanumeric ("8A") | Yes, from a small learned model, with its Camelot code | Yes: exact key and MIREX weighted score, with the error kind (fifth, relative, parallel) |
| Song structure | Phrase analysis: Intro, Up, Down, Chorus, Verse, Bridge, Outro, with a Low / Mid / High mood per track | Sections (intro, build, drop, chorus, outro...) on an energy curve; parts that sound alike share a letter | Not yet: phrases aren't in the XML export ([below](#not-compared-yet)) |
| Energy | Only the phrase mood | Energy curve, loudness and an energy score | No: Rekordbox has nothing comparable |
| Vocals | Marks where vocals are (AI vocal detection, shown above the waveform) | Vocal stem from separation, but no vocal markers on the card | Not yet |
| Cue points | Auto cue at the first beat or after the silence at the start; memory and hot cues by hand | Cue points by hand | No |
| Waveform | Blue, RGB or 3-band colored waveforms | Frequency-colored waveform, energy chart, spectrogram, live 3D view | No (display only) |
| Stems | Real-time stem parts in the decks (Rekordbox 7) | Four Demucs stems (vocals, bass, other, drums), saved with the track | No |
| Mood and style | My Tag, set by hand | Scored vibe tags (dark, percussive, bass-heavy...) and danceability | No: Rekordbox has nothing automatic |
| Drums, bass and chords | No | Per-section drum lanes, bass line and chord loop, editable, playable, MIDI and Strudel export | No: Tastr only |
| Mix suggestions | Related tracks / track suggestions by BPM, key and genre | Keys that mix, Blend map, "between two songs" paths | No |
| Cloud analysis | Can fetch BPM, grid, waveform and vocal analysis of the same file from AlphaTheta's server | Runs its own analysis, locally or on Modal | Turn it off for the bench (below) |

## Three ways to benchmark

**1. Head-to-head on labeled tracks (the real comparison).** Analyze a labeled dataset's audio in Rekordbox,
export the XML, and run the usual bench with `--rekordbox`. Rekordbox's answers and Tastr's are both scored
against the dataset's labels, on exactly the tracks both analyzed:

```
tastr bench giantsteps-key ~/data/giantsteps-key --limit 100 --rekordbox ~/data/rekordbox/giantsteps.xml
tastr bench giantsteps-key giantsteps-key --remote --rekordbox rekordbox/giantsteps.xml   # on Modal
```

```
Rekordbox vs Tastr on the 100 tracks both analyzed (0 of the dataset's tracks aren't in the XML)
                          Rekordbox    Tastr
  Key (MIREX weighted)        0.xxx    0.xxx  n=100
  Key exactly right           0.xxx    0.xxx  n=100
  ...
```

Tracks are matched by file name (the whole name, then the part before the first dot, so `3315.LOFI.mp3`
matches `3315.mp3`), because the folders differ between the Mac that ran Rekordbox and the bench. The report
(`--out`) keeps both sets of rows under `rekordbox`.

Good datasets for this, all already in the R2 bucket: `giantsteps-key` and `beatport-key` (key), `giantsteps-tempo`
and `beatport-tempo` (BPM), `ballroom` (BPM, beats, bars, 3/4) and `fma-keys` (key outside dance music).

**2. Agreement on your own library.** With no labels, the `rekordbox` dataset treats Rekordbox's answers as the
reference, so the scores say how often Tastr agrees with it, and the review list shows the tracks where they
disagree most, which are the ones worth labeling ([bench.md](bench.md#your-own-tracks)):

```
tastr bench rekordbox ~/Music/rekordbox.xml --limit 30
tastr bench rekordbox rekordbox/pam --remote --limit 30   # a bucket folder with rekordbox.xml and the audio
```

Each track's audio is its path in the XML when that file exists (the same Mac); otherwise a file with the same
name in the XML's folder or below, which is how a copy in the bucket is found. Tracks Rekordbox hasn't analyzed
are skipped. Agreement is not accuracy: where they disagree, either could be wrong.

**3. Your library: Tastr's stored analysis vs Rekordbox's.** For tracks that are in both libraries, compare
what each app already worked out, with no re-analysis:

```
tastr rekordbox-compare ~/Music/rekordbox.xml                 # your library at ~/.tastr (or --db FILE)
tastr --db hosted.db rekordbox-compare rekordbox.xml --owner you@example.com
```

```
212 tracks in both (480 in Tastr, 1,950 analyzed in Rekordbox; matched by file name 150, artist and title 62)
How often Tastr agrees with Rekordbox:
  Same BPM (within 4%)         0.xxx  n=212
  Same key                     0.xxx  n=205
  Beats line up (70 ms)        0.xxx  n=212
  key differences (Tastr vs Rekordbox): correct .., fifth .., relative ..
Against your labels (14 tracks):  Rekordbox   Tastr
  Same BPM (within 4%)             0.xxx   0.xxx  n=14
Biggest disagreements (31 in all; label these to see who's right):
  #42 Artist - Title: Tastr 87.0 F minor, Rekordbox 174.0 F minor
```

On the hosted server, open **Admin › Rekordbox export** and drop the export there (or choose the file; up to
50 MB). Tastr stores it in the audio bucket as `rekordbox/collection.xml`, replacing the last one, matches it to
your own tracks and shows a preview: how many tracks matched, how often the two apps agree, and the matches
themselves. Nothing changes on your tracks until you press **Load Rekordbox values**. That stores Rekordbox's BPM
and key on each matched track. Each card then shows Rekordbox under "Listed elsewhere", marked where it
disagrees (half, double, a fifth...), and the review list gains "rekordbox lists 103 BPM, not Tastr's 136"
reasons, the same as for the online catalogs ([lookup.md](lookup.md)). Loading a newer export replaces them, and
tracks that aren't in it lose theirs. The load is logged in the activity log. Rekordbox is never looked up:
naming it in `TASTR_LOOKUP` does nothing. The export lists your library's paths, so delete it from the bucket
when you no longer need it.

The routes behind the panel are `POST /api/admin/rekordbox/upload` (the XML as the body: stores it and answers the
comparison), `POST /api/admin/rekordbox/load` and `GET /api/admin/rekordbox/status`. For scripts,
`/api/admin/rekordbox-compare?format=text` still reads the stored export (drop `format=text` for JSON,
`key=rekordbox/other.xml` for another file, `show=N` for more disagreements, `save=1` to load as the panel does).
All are admins only and use your own tracks.

Tracks are matched by file name first, then by artist and title (with lengths within 3 s), each track once.
Where you labeled a track in Tastr ([bench.md](bench.md#your-own-tracks)), both apps are scored against your
label. This reads the analysis as it was stored, so a track analyzed by an older Tastr shows the older answer;
re-analyze first to compare the current engine. `--out` writes every matched track as JSON.

## Making the export

1. In Rekordbox's Preferences › Analysis, note the BPM range and whether analysis is Normal or Dynamic, and turn
   off cloud analysis (it can fetch results from AlphaTheta's server, which may have been corrected by hand).
   Leave Key, BPM and beat grid analysis on.
2. Import the folder of audio and analyze it. Don't adjust any grid or key by hand.
3. File › Export Collection in xml format. Exporting your whole collection is fine for a dataset run: only
   the dataset's files are matched and the rest are ignored. Keep the dataset's file names
   (`<id>.LOFI.mp3` for GiantSteps) so they match.
4. For `--remote`, upload the XML (and for the `rekordbox` dataset, the audio) to the dataset bucket, e.g.
   `tastr-data/rekordbox/giantsteps.xml`.

Record the Rekordbox version (it's in the XML's `PRODUCT` line) and the analysis settings with any score you
quote, since both change the results.

Never commit an export: it lists your file paths and library. The datasets' licenses apply to Rekordbox's
results for their tracks too, so quote scores, not per-track answers.

## Not compared yet

- **Phrases.** Rekordbox keeps them in its ANLZ analysis files (the `PSSI` tag of each track's `.EXT` file),
  not the XML. Reading them (for example with the MIT-licensed `pyrekordbox`) would let the Harmonix sections
  bench score Rekordbox's phrase starts next to Tastr's sections.
- **Vocal positions**, also only in the analysis files.

## Results

Add each run here with the date, dataset, track count, Rekordbox version and settings.

### 2026-11-03: GiantSteps, Rekordbox 7.2.19

Rekordbox 7.2.19 on macOS: Normal analysis, BPM range 70-180, high precision beat grid on, cloud analysis off,
no hand edits. Tastr: Rust engine 0.1.0, `core,sections` stages, on Modal. Both scored against the datasets' labels
on the tracks both analyzed.

| Set | Score | Rekordbox | Tastr | Tracks |
|---|---|---|---|---|
| GiantSteps Key, first 100 | key exactly right | 0.515 | 0.484 | 97 |
| | MIREX weighted | 0.581 | 0.573 | 97 |
| GiantSteps Tempo, the 43 with audio | BPM within 4% | 0.825 | 0.750 | 40 |
| | BPM incl. x2 / half | 0.950 | 0.900 | 40 |

Key errors: Rekordbox correct 50, fifth 9, relative 3, parallel 5, other 30; Tastr correct 47, fifth 13, relative 3,
parallel 6, other 28. BPM octave errors: Rekordbox half 2, double 3; Tastr half 3, double 3.

- **About even.** Rekordbox is ahead by 3 tracks on key and 3 on tempo. On samples this small that's within
  noise: a share right on 97 tracks is known to about ±10 points, on 40 to about ±13.
- **Different misses.** The two disagree on 31 of the 97 key tracks: Rekordbox is right on 8 of those, Tastr on 5,
  neither on 18. Where they agree (66), both are right on 42. So a vote of the two wouldn't fix much, but the 24
  tracks both get wrong in the same way are worth a look at their labels.
- **Fifths.** Tastr's extra misses are mostly a fifth off (13 vs 9), the error the engine report already lists
  as open ([engine-report.md](engine-report.md#key)).
- **Drum & bass tempo.** Both read some 174 BPM tracks at 86 and some 85 BPM tracks at 172; Rekordbox's 70-180
  range doesn't stop either error.
- 3 tracks are in both GiantSteps sets and were imported twice, so they went unmatched in this run. The matcher now
  counts two copies with the same answers as one (next runs).

A full run (all 600 key tracks, which Rekordbox would have to analyze too) would narrow the margins; GiantSteps
Tempo has no more tracks with audio.

### 2026-11-03: Pam's library, Tastr's stored analysis vs Rekordbox 7.2.19

58 tracks in both libraries (of 88 in the hosted Tastr library), house, afro house, pop and hip hop. No labels, so
this is agreement, not accuracy.

| | Agreement |
|---|---|
| Same BPM (within 4%) | 0.810 |
| Same BPM incl. x2 / half | 0.845 |
| Same key | 0.845 (49 same, 3 a fifth apart, 2 relative, 4 other) |
| Beats line up (70 ms) | 0.590 |
| Bar starts line up (70 ms) | 0.358 |

What the 16 disagreements showed:

- **Tastr's BPM is coarse.** Every Tastr BPM among them sits exactly on 60 x 22050 / 512 / n (117.45, 123.05, 129.2,
  136.0, 143.55, 152.0, 161.5): the tempo comes from the median gap between beats counted in whole analysis frames
  (23 ms), so near 128 BPM it can only say 123.05 or 129.2. That passes "within 4%" (ARTBAT's 128 reads 129.2) but
  drifts a beat every few bars against a real grid, which is likely most of the low beat and bar scores.
- **4:3 tempo errors.** Six afro house and pop tracks read about 4/3 of Rekordbox's tempo (143.55 for 107, 161.5
  for 120, 136 for 103): the tracker locked onto the dotted-eighth pulse of a 3-3-2 rhythm. The existing 3:2 check
  doesn't cover it.
- One each of half and double tempo (86 vs 171, 161.5 vs 82).
