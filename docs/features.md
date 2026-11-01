# What Tastr does

Everything a track card shows and what you can do with it. The [README](../README.md) has the short version.

## Track cards

- **Tempo and key.** BPM with how likely it is to be right (calibrated on the benchmarks), a beat and bar grid, and the
  key from a small learned model trained on GiantSteps and Beatport (shown with
  its Camelot code for mixing). Drum & bass is read at its real tempo, not
  half or two thirds of it.
- **Time signature.** Bars of 4 beats (4/4) or 3 (3/4, waltz time), detected
  from how the beats group. Grids, MIDI and Strudel follow it. When it's wrong,
  change it from the track's ⋯ menu; `TASTR_METER=4/4` turns 3/4 detection off.
  6/8 and songs that change meter are read as 4/4 or 3/4.
- **Energy and sections.** An energy curve with rough section boundaries
  (intro, build, drop, chorus, outro...). Sections that sound alike share a
  letter, so `Drop B` and `Outro B` are the same part.
- **Vibe tags.** Scored tags such as bass-heavy, dark or percussive, grouped by
  kind (energy, mood, rhythm, sound...) and tinted by how strongly they apply.
- **File tags.** Title, artist and album from MP3, FLAC and m4a tags.
- **Listed elsewhere (optional).** The BPM and key that [GetSongBPM](https://getsongbpm.com)
  and FreqBlog (your own API keys) or AcousticBrainz's public-domain dump list for the
  track, shown next to Tastr's figures with a mark when they disagree. Off by default; see
  [lookup.md](lookup.md).

## Drums, bass and chords

<img src="images/drums-bass-chords.png" width="560" alt="Drum grid, bass piano roll and chord cards for one section">

- **Drum lanes** for kick, snare, clap, hat, open hat, tom and perc, as a
  16-step grid per section (12 steps in 3/4). Template patterns that fit the
  tempo (waltz patterns in 3/4) are one click away. Ask for the optional [drum model](usage.md#drum-model-optional) on a track and kick, hat and tom come from
  [ADTOF](https://github.com/MZehren/ADTOF), a network trained on annotated
  real music that finds toms the built-in detector misses, and a cymbal lane
  (crash and ride) is added.
- **Per-section bass and chords:** the bass note and rhythm for each bar, the
  chord loop with Roman numerals, and what changed from the previous section
  (marked Δ).
- **Edit and play it.** Click steps, pick bass notes and chords, and hear it
  in the browser with a playhead on every part. Solo or mute each drum lane and
  part, drag the BPM box up or down to change the tempo, and export the parts
  you pick as one MIDI file: the section's loop, or the whole track with every
  section in order.
- **Saved edits and versions.** Edits save automatically with the track. Name
  a version to come back to later. Re-analyzing keeps your edits for sections
  that didn't move.

## Try this in Ableton or Strudel

- **Ableton Live:** setup (tempo, scale, Camelot), drum rack programming, bass
  and chord moves, scales to play over each chord, an arrangement map and mix
  notes.
- **[Strudel](https://strudel.cc):** a live-code sketch of the track with each
  section's drums, bass and chords at the right tempo, written the short way live
  coders type it (`bd*4`, `<Am F C G>`), one named line per layer to mute with `_`,
  filter sliders and a punchcard under the code. It can use your edits and the
  original audio cut into bars, either the full mix or separated stems, and start
  from any section. "Open in Strudel" opens it in strudel.cc, ready to play and remix.

<img src="images/strudel.png" width="560" alt="A generated Strudel sketch with the original audio">

## Listen and explore

- **Energy, waveform and spectrogram views** with playback. Click a section to
  play from there, drag the playhead to scrub (you hear it as you drag), and
  scroll to zoom. The spectrogram redraws sharp at your screen's resolution as you zoom.
- **Live tab:** the spectrogram streams with playback, as a scrolling 2D view or a
  3D landscape of ridges. **⤢ Expand** shows any chart bigger, or full screen.
- **Stem separation** into vocals, bass, other and drums with Demucs, on your
  machine or on a [Modal](https://modal.com) GPU (see [Stems](usage.md#stems-optional)).
- **Library sidebar** with search, sort (newest or oldest upload, title, BPM, key, energy, length) and filters
  by vibe, key, BPM, upload date and review state. Each row shows when the track was uploaded, a ✓ when you
  labeled it and a hollow dot when it's in your review list. Click a vibe, the BPM or the key on a track card
  to filter the list from there (more vibes narrow it; the key shows the keys that mix with it).
- **Card sections in your order.** Drag a section by its ⠿ grip, or use the ↑/↓ buttons on its header, to put
  e.g. Vibe above Energy. The order is kept in your browser and used for every track; "Reset order" puts it back.
- **Track and bulk actions:** copy a summary, reload metadata, re-analyze,
  separate stems or delete, for one track or a whole selection. Bulk reload, re-analyze and look up run a few
  tracks at a time (`TASTR_BULK_AT_ONCE`; on a hosted server it follows `TASTR_MAX_ANALYSES`, at home about
  half the CPU cores); stems and the drum model run one at a time.
- **Explore tab:** a BPM-energy chart, and **Find tracks** to search your library
  without code (BPM, key and the keys that mix with it, energy, vibe, length,
  sections, text, with AND/OR groups), or in read-only SQL with a table and column
  reference. Save the searches you use as one-click chips, or start from the
  examples. "Edit as SQL" shows the SQL behind a search.
- **Two looks:** 808 (a drum-machine panel) and Technics (70s hi-fi), picked
  in the header.
- **Keyboard shortcuts** for most things; press `?` for the list.

<img src="images/spectrogram.png" width="560" alt="Spectrogram view with sections and playback">

<img src="images/live-3d.png" width="560" alt="The Live tab's 3D view: a landscape of frequency ridges rolling back from the playing moment">

<img src="images/find-tracks.png" width="560" alt="Find tracks: a key that mixes with 8A and a BPM range, and the matching tracks">

## Label and review

Tastr's scores come from public datasets that are mostly EDM previews. Your own labels are the only check on
the music you actually use, and every label also scores future engine changes
([bench.md](bench.md#your-own-tracks)).

- **Review tab** in the sidebar, with a count of tracks to look at. **To review** lists the tracks most likely
  analyzed wrong: a low "likely right" score on BPM or key, or a catalog listing that disagrees, spelled out
  ("freqblog lists 61.1 BPM, half Tastr's"). Tracks you added yourself come first. The × on a row takes a track
  out. **Reviewed** is the log of your labels, newest first, with what changed from Tastr's reading
  ("BPM 61.1 → 122.3", "Kept as is"), a link that shows them in the library, and a CSV download.
- **Label bar** at the bottom of each card's header: "Is this analysis right?", then **Label it**,
  **✓ Keep as is** (stores Tastr's own BPM and key as a confirmation) or **Add to review**. A track can also go
  in or out of review from its ⋯ menu, or for a whole selection from the bulk Actions menu.
- **The label panel:**
  - BPM: Tastr's, half or double in one click, a drag box (a whole BPM per notch, Shift for 0.1), or **Tap**
    along to the beat.
  - Key: Tastr's key, its relative and the fifths above and below (Tastr's most common misses), or any of the
    24. A "catalog" tag marks the one a catalog lists.
  - 🔊 next to each key plays its chord alone for 2 seconds, then brings the track's drop in under it: the right
    key blends in, a wrong one clashes. **🥁 Drums off** plays only the track's harmonic part (HPSS, worked out
    in the browser for the clip), which is what the key detector listens to. A slider sets the chord's level.
  - Sections: start times in m:ss, from the detected ones, or **✎ Mark on chart**: click the chart to add a start
    (it snaps to the nearest bar, Alt for the exact spot), drag a mark to move it, click it to remove it, or press
    M while it plays.
  - A note, and a "Will save" line that always shows what Save stores. **Save & next** opens the next track to
    review.
- **Pattern marks** in **Drums, bass & chords**: under each part, "Detected: ✓ Right · ≈ Close · ✗ Way off" for
  the section shown (click a pressed one to take it off), and **Right everywhere else** to mark the sections you
  haven't. After editing a part, **Save my edit as the right pattern** keeps your edit as the answer for that
  section, which the pattern benchmark scores the engine against. A part marked Way off without a saved right
  pattern puts the track in **To review** ("Bass way off in Drop B"), right after the tracks you added. Marks
  are kept by the section's bars, so a re-analysis keeps them; one whose pattern the re-analysis changed shows
  "marked on an older analysis" and puts the track back in review. **Reviewed** lists them beside your labels.
- Labels, review flags and pattern marks are per person on a hosted server, like the library, and re-analyzing
  never changes them.

## Files

The **Files** tab lists everything Tastr stores for you: the audio you uploaded and the stems made from it,
with their sizes and how much space they take together. On a shared server it also shows how much space the
server has left.

- **Download** any original or any stem (FLAC), or all four stems of a track as one zip. Files are named
  "Artist - Title.mp3" and "Artist - Title (vocals).flac".
- **Search, sort and filter** the same way as the Library's track list (vibe, key, BPM, upload date, review),
  plus With stems / No stems and Largest first.
- **Select** several tracks to download their audio or their stems as one zip (up to 200 tracks or 5 GB),
  separate stems for the ones without, or delete stems to free space. Stems can be made again later.
- Once a track has stems, "Try this in › Strudel" links to each stem, the zip and the Files tab.

## Glossary

A built-in reference for every label, number and pattern on a track card,
and how Tastr works each one out. Open it from the **Glossary** tab, or from
"What the vibes mean" under a track's vibes.

- **Camelot wheel:** click a key to see which keys mix with it. Opening the
  Glossary from a track starts on that track's key.
- **Scales you can hear:** pick a key and scale and play it, or click a row
  of the modes table (Dorian, Phrygian...) to hear that mode.
- **Drum styles:** the pattern ideas for each style, drawn from the engine's
  own templates so they always match what the Pattern picker offers.
- **The rules behind the numbers:** vibe scores, section labels, energy and
  how drums, bass and chords are detected, with the actual thresholds.
- **Accuracy that stays current:** the BPM, key and downbeat scores it quotes
  come from the latest full benchmark run (`tastr bench ... --record`), with
  the run date, so they update without touching the page.

<img src="images/glossary.png" width="560" alt="The Glossary's Camelot wheel, showing which keys mix with the open track's key">

## Admin (hosted)

When Tastr is hosted for friends, the people in `TASTR_ADMIN_EMAILS` get an **Admin** tab
([hosting.md](hosting.md#admin-view)). Nobody else sees it, and your own machine has none.

- **Usage chart:** pick Today (24 UTC hours, with a "now" line), 7 days, 30 days or All time, and show
  Tracks, Stem jobs, Visits, Storage (GB per person at the end of each day) or Cost (estimated from Modal run
  time). Bars stack per person; click a person in the table to pick them out.
- **Daily limits per person:** the ✎ at the end of a person's row changes their tracks and stem jobs per
  day, for today only (back to the default at 00:00 UTC) or until you change it. Their quota shows the raised
  cap with ↑, and someone at their cap gets a **Raise** link. Each change is logged.
- **Recent activity filters:** filter the log by what happened, who, where (Tastr, Modal, Fly.io, GitHub,
  Cloudflare, R2) and when (today, 7 or 30 days, or between two dates). Filters combine; **Problems** shows
  only what went wrong. Each line names its source. GitHub Actions runs on main and the R2 backups show up on
  their own, checked every 15 minutes.
- **Cloudflare links:** with `TASTR_CF_ACCOUNT_ID` set, the Access panel links to who can sign in and to
  the Access log of blocked requests (who and why).
