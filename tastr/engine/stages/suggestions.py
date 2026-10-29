""""Try this in Ableton" suggestions: drum pattern ideas, bass movement, chord-scale guesses.

Analyses the audio on the bar grid from core's downbeats (16-step drum lanes,
bass notes per bar, chords per bar), stores those by-products in card.extras,
then turns them plus core/sections/tags into Suggestion entries.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .. import drums, music, signals
from ..card import Suggestion
from ..pipeline import AnalysisContext, register_stage

STEPS = 16

# Default Drum Rack pad notes in Live.
DRUM_RACK = {"kick": "C1", "snare": "D1", "clap": "D#1", "hat": "F#1", "open_hat": "A#1", "tom": "A1", "perc": "G#1"}

# Lanes that are reported whenever they have a clear pattern; the rest also need to be loud enough.
CORE_LANES = ("kick", "snare", "hat")

VARIATIONS = {
    "four_floor": [
        ("Classic house/techno", {
            "kick": "x...x...x...x...", "clap": "....x.......x...",
            "hat": "..x...x...x...x.", "open_hat": "......x.......x."}),
        ("Rolling 16ths", {
            "kick": "x...x...x...x...", "clap": "....x.......x...",
            "hat": "xoxoxoxoxoxoxoxo", "perc": "...x..x....x..x."}),
    ],
    "dnb": [
        ("Two-step", {"kick": "x.........x.....", "snare": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."}),
        ("Amen-ish break", {"kick": "x.x.......xx....", "snare": "....x..x.x..x..x", "hat": "x.x.x.x.x.x.x.x."}),
    ],
    "halftime": [
        ("Halftime", {"kick": "x.........x.....", "snare": "........x.......", "hat": "x.x.x.x.x.x.xxx."}),
    ],
    "hiphop": [
        ("Boom bap", {"kick": "x......x..x.....", "snare": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."}),
        ("Trap-style hats", {"kick": "x.....x...x.....", "snare": "........x.......", "hat": "xxx.x.xxx.x.xxxx"}),
    ],
    "generic": [
        ("Backbeat", {"kick": "x.......x.x.....", "snare": "....x.......x...", "hat": "x.x.x.x.x.x.x.x."}),
    ],
}


STYLE_LABELS = {"four_floor": "four on the floor", "dnb": "drum and bass", "halftime": "halftime",
                "hiphop": "hip-hop", "generic": "generic backbeat"}


def _style(bpm: float, perc: float | None) -> str:
    if 160 <= bpm <= 185:
        return "dnb"
    if 136 <= bpm <= 152 and (perc or 0) > 0.25:
        return "halftime"
    if 118 <= bpm < 136:
        return "four_floor"
    if 80 <= bpm < 105:
        return "hiphop"
    return "generic"


def _step_list(pattern: str) -> list[int]:
    return [i + 1 for i, c in enumerate(pattern) if c in "xo"]


def _pattern(profile: np.ndarray) -> str:
    return "".join("x" if v >= 0.72 else "o" if v >= 0.5 else "." for v in profile)


def _lane_help(pattern: dict) -> dict:
    return {k: drums.LANE_HELP[k] for k in pattern if k in drums.LANE_HELP}


def _title(text: str) -> str:
    return text if len(text) <= 80 else text[:79] + "…"


# --- Audio analysis --------------------------------------------------------------


def _step_grid(ctx: AnalysisContext, n_frames: int) -> np.ndarray:
    """Frame index of each 16th note for every complete bar (consecutive downbeats)."""
    downbeats = np.asarray(ctx.card.core.downbeat_times, dtype=float)
    if downbeats.size < 2:
        return np.zeros((0, STEPS), dtype=int)
    beats = np.asarray(ctx.card.core.beat_times, dtype=float)
    rows = []
    for a, b in zip(downbeats[:-1], downbeats[1:]):
        in_bar = beats[(beats >= a - 1e-6) & (beats < b - 1e-6)]
        edges = np.append(in_bar, b) if in_bar.size == 4 else np.linspace(a, b, 5)
        times = [edges[i] + (edges[i + 1] - edges[i]) * s / 4 for i in range(4) for s in range(4)]
        rows.append(times)
    frames = np.round(np.asarray(rows) * ctx.sr / ctx.hop).astype(int)
    return np.clip(frames, 0, max(n_frames - 1, 0))


def _sample(env: np.ndarray, frames: np.ndarray, radius: int = 2) -> np.ndarray:
    """Peak of an envelope in a small window around each frame (tolerates timing slop)."""
    out = np.zeros(frames.shape, dtype=float)
    for idx, f in np.ndenumerate(frames):
        a, b = max(0, f - radius), min(env.size, f + radius + 1)
        out[idx] = env[a:b].max() if b > a else 0.0
    return out


def _band_flux(power: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> np.ndarray:
    import librosa

    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():
        return np.zeros(power.shape[1])
    log_s = librosa.power_to_db(power[mask] + 1e-10)
    return np.maximum(0.0, np.diff(log_s, axis=1, prepend=log_s[:, :1])).mean(axis=0)


def _bar_energy(ctx: AnalysisContext, step_frames: np.ndarray) -> np.ndarray:
    n = step_frames.shape[0]
    series = ctx.card.series.get("energy")
    if series is not None and len(series.values) >= n:
        return np.asarray(series.values[:n], dtype=float)
    rms = ctx.get("rms", signals.rms)
    ends = np.append(step_frames[1:, 0], rms.size)
    return np.array([rms[a:max(b, a + 1)].mean() for a, b in zip(step_frames[:, 0], ends)])


def _drum_grid(ctx: AnalysisContext, step_frames: np.ndarray) -> dict:
    energies = _bar_energy(ctx, step_frames)
    busy = energies >= np.median(energies)
    if busy.sum() < 2:
        busy = np.ones(len(energies), dtype=bool)
    bpm = ctx.card.core.bpm
    step_s = 15.0 / bpm if bpm else None
    acts = drums.lane_activations(ctx, step_frames, step_seconds=step_s)
    # Noise floor: the same measure halfway between 16ths, where a played pattern has no hits.
    between = (step_frames + np.roll(step_frames, -1, axis=1)) // 2
    between[:, -1] = step_frames[:, -1] + (step_frames[:, -1] - step_frames[:, -2]) // 2 if STEPS > 1 else step_frames[:, -1]
    floor_acts = drums.lane_activations(ctx, between, step_seconds=step_s, slop=0)
    grid = {}
    for lane in drums.LANES:
        # sqrt compresses activation toward perceived loudness so a slightly weaker hit still reads as a hit
        steps = np.sqrt(acts[lane])
        profile = steps[busy].mean(axis=0)
        floor = float(np.sqrt(floor_acts[lane])[busy].mean())
        peak = float(profile.max())
        norm = profile / peak if peak > 0 else profile
        grid[lane] = {
            "pattern": _pattern(norm),
            "velocity": [round(float(v), 3) for v in norm],
            "detected": bool(peak > 0 and peak >= 3.0 * floor),
            "_steps": steps,
            "_peak": peak,
        }
    # A lane far quieter than the loudest one is most likely another drum misread.
    top = max(g["_peak"] for g in grid.values())
    for lane, g in grid.items():
        if lane not in CORE_LANES and g["_peak"] < 0.2 * top:
            g["detected"] = False
    # A mid lane that is a faint copy of the kick (same steps, much quieter) is the kick leaking through.
    kick = grid["kick"]
    for lane in ("snare", "clap", "perc"):
        g = grid[lane]
        if g["detected"] and kick["_peak"] > 0 and g["_peak"] < 0.35 * kick["_peak"]:
            a, b = np.asarray(g["velocity"]), np.asarray(kick["velocity"])
            if a.std() > 0 and b.std() > 0 and float(np.corrcoef(a, b)[0, 1]) > 0.9:
                g["detected"] = False
    return grid


def _section_grooves(grid: dict, energies: np.ndarray, sections: list[tuple[int, object]]) -> list[dict]:
    """Per-section 16-step patterns, scaled against the whole-track peak so a quiet section stays sparse."""
    n_bars = energies.size
    out = []
    for idx, sec in sections:
        a = max(0, sec.start_bar - 1)
        b = min(n_bars, sec.end_bar if sec.end_bar is not None else sec.start_bar)
        if b <= a:
            continue
        busy = energies[a:b] >= np.median(energies[a:b])
        pattern = {}
        for lane, g in grid.items():
            if g["_peak"] <= 0:
                pattern[lane] = "." * STEPS
                continue
            profile = g["_steps"][a:b][busy].mean(axis=0)
            pattern[lane] = _pattern(np.minimum(profile / g["_peak"], 1.0))
        out.append({"idx": idx, "label": sec.label, "letter": sec.letter, "start_bar": sec.start_bar,
                    "end_bar": sec.end_bar, "energy": sec.energy, "pattern": pattern})
    return out


def _common_loop(seq: list, length: int) -> tuple | None:
    if len(seq) < length:
        return None
    windows = Counter(tuple(seq[i:i + length]) for i in range(0, len(seq) - length + 1, length))
    return windows.most_common(1)[0][0]


def _bar_slices(step_frames: np.ndarray, n_frames: int):
    ends = np.append(step_frames[1:, 0], n_frames)
    return [(int(a), int(max(b, a + 1))) for a, b in zip(step_frames[:, 0], ends)]


def _bass_summary(ctx: AnalysisContext, step_frames: np.ndarray, tonic: int | None, scale: str) -> dict:
    import librosa

    cqt = ctx.get("bass_cqt", signals.bass_cqt)
    chroma = cqt.reshape(3, 12, -1).sum(axis=0)
    pcs = []
    for a, b in _bar_slices(step_frames, cqt.shape[1]):
        vec = chroma[:, a:b].sum(axis=1)
        pcs.append(int(np.argmax(vec)) if vec.sum() > 0 else None)
    pcs = [p for p in pcs if p is not None]
    if not pcs:
        return {}
    notes = [music.note_name(p) for p in pcs]
    loop = _common_loop(notes, 4) or tuple(notes[:4])
    most, most_count = Counter(pcs).most_common(1)[0]
    intervals = [min((b - a) % 12, (a - b) % 12) for a, b in zip(pcs, pcs[1:]) if a != b]
    if most_count / len(pcs) > 0.7:
        movement = "pedal"
    elif not intervals:
        movement = "static"
    elif np.mean([iv <= 2 for iv in intervals]) > 0.6:
        movement = "stepwise"
    elif np.mean([iv in (5, 7) for iv in intervals]) > 0.5:
        movement = "fourths/fifths"
    else:
        movement = "leaps"
    degrees = []
    if tonic is not None:
        for n in loop:
            d = music.degree_of(music.NOTE_NAMES.index(n), tonic, scale)
            degrees.append(str(d + 1) if d is not None else f"({n})")
    log_s = librosa.power_to_db(cqt ** 2 + 1e-10)
    flux = np.maximum(0.0, np.diff(log_s, axis=1, prepend=log_s[:, :1])).mean(axis=0)
    prof = _sample(flux, step_frames).mean(axis=0)
    rhythm = _pattern(prof / prof.max()) if prof.max() > 0 else None
    return {"notes": notes, "loop": list(loop), "degrees": degrees, "movement": movement, "rhythm": rhythm}


def _harmony_summary(ctx: AnalysisContext, step_frames: np.ndarray, tonic: int, scale: str) -> dict:
    chroma = ctx.get("chroma", signals.chroma)
    bars = [music.match_chord(chroma[:, a:b].mean(axis=1)) for a, b in _bar_slices(step_frames, chroma.shape[1])]
    confident = [b for b in bars if b["score"] >= 0.6 and b["root"] is not None]
    if not confident:
        return {}
    names = [b["name"] for b in bars]
    loop = _common_loop(names, 4) or tuple(names[:4])
    lookup = {b["name"]: b for b in confident}
    chords = []
    for name, count in Counter(b["name"] for b in confident).most_common(6):
        b = lookup[name]
        chords.append({
            "name": name,
            "bars": count,
            "roman": music.roman_numeral(b["root"], b["quality"], tonic, scale),
            "scale": music.chord_scale(b["root"], b["quality"], tonic, scale),
        })
    roman = [music.roman_numeral(lookup[n]["root"], lookup[n]["quality"], tonic, scale) if n in lookup else "?" for n in loop]
    return {"progression": list(loop), "roman": roman, "chords": chords}


# --- Suggestion rules ------------------------------------------------------------


def _suggest(ctx: AnalysisContext, grid: dict, bass: dict, harm: dict, by_section: list[dict] | None = None) -> list[Suggestion]:
    card = ctx.card
    core, f = card.core, card.features
    bpm, key = core.bpm, core.key
    out: list[Suggestion] = []

    def add(category: str, title: str, body: str, data: dict | None = None) -> None:
        out.append(Suggestion(category, _title(title), body, data or {}))

    root = music.note_name(key.tonic) if key else None
    scale = (key.scale or ("Minor" if key.mode == "minor" else "Major")) if key else None
    perc = f.get("percussiveness")
    style = _style(bpm, perc) if bpm else None

    # --- Setup ---
    if bpm:
        warp = "Beats" if (perc or 0) > 0.3 else "Complex Pro"
        title = f"Set the project to {bpm:.0f} BPM" + (f", {root} {scale}" if key else "")
        body = f"Set the tempo to {bpm:.1f} (half/double: {bpm / 2:.0f}/{bpm * 2:.0f}). "
        data = {"bpm": bpm, "warp_mode": warp, "template": "{setup}"}
        if key:
            body += (f"Turn on Scale in the Control Bar with root {root} and scale {scale} so MIDI devices and "
                     f"the piano roll follow it.{f' Camelot {key.camelot} if you are mixing it.' if key.camelot else ''} ")
            data.update(root=root, scale=scale, camelot=key.camelot)
        body += f"If you drop the audio in, warp it in {warp} mode."
        add("setup", title, body, data)
    elif key:
        add("setup", f"Set Scale to {root} {scale}",
            f"Turn on Scale in the Control Bar with root {root} and scale {scale}.", {"root": root, "scale": scale, "template": "{setup}"})

    # --- Drums ---
    swing_conf, swing_pct = f.get("swing_confidence"), f.get("swing_percent")
    if swing_conf is not None and swing_pct is not None and swing_conf > 0.3 and swing_pct >= 54:
        amount = int(min(75, max(50, round(swing_pct / 2) * 2)))
        add("drums", f"Swing your 16ths to about {amount}%",
            "The off-beat 16ths sit late. Drag 'Swing 16ths' from the Groove Pool (Core Library > Swing) onto "
            f"your drum clip and start around {amount}%, or use a Groove with Timing at about {int((amount - 50) * 4)}%.",
            {"swing_percent": amount, "template": "{swing}"})
    detected = {k: v["pattern"] for k, v in grid.items() if v["detected"]}
    if detected:
        rows = "; ".join(f"{k} on steps {', '.join(map(str, _step_list(p))) or 'none'}" for k, p in detected.items())
        pads = ", ".join(f"{k.replace('_', ' ')} {DRUM_RACK[k]}" for k in detected if k in DRUM_RACK)
        data = {"pattern": detected, "pads": DRUM_RACK, "origin": "detected", "bpm": round(bpm) if bpm else None,
                "lane_help": _lane_help(detected),
                "template": "Measured per section, since drums change between sections. Averaged over the busiest "
                            f"bars the whole track reads as {rows}. Program it on {{drumrack}}{{pads}} and vary the "
                            "last bar of every 4."}
        if by_section:
            data["by_section"] = [{**sec, "pattern": {k: v for k, v in sec["pattern"].items() if k in detected}}
                                  for sec in by_section]
        add("drums", "Detected groove",
            f"Measured per section, since drums change between sections. Averaged over the busiest bars the whole "
            f"track reads as {rows}. Program it on a Drum Rack ({pads}) and vary the last bar of every 4.", data)
    for name, pattern in VARIATIONS[style] if style else []:
        add("drums", f"Pattern idea: {name}",
            f"A {name.lower()} pattern that sits at this tempo. Lanes map to default Drum Rack pads.",
            {"pattern": pattern, "pads": DRUM_RACK, "origin": "template", "style": STYLE_LABELS[style],
             "style_key": style, "bpm": round(bpm),
             "lane_help": _lane_help(pattern),
             "template": f"A {name.lower()} pattern that sits at this tempo. Lanes map to the default {{drumrack}} pads."})

    # --- Bass ---
    if bass.get("loop"):
        tips = {
            "pedal": "It mostly sits on one note, so the interest is in rhythm and filter movement. Try an Auto "
                     "Filter with an LFO synced to 1/8 or a Wavetable filter envelope with short decay.",
            "stepwise": "It moves stepwise, which suggests a melodic bassline. Try Glide in Operator or Drift with legato notes.",
            "fourths/fifths": "It moves in fourths and fifths, classic root motion. Keep the bass on chord roots and "
                              "add an octave jump on the last 16th of the bar.",
            "leaps": "It leaps around, so keep the sound tight (short release) so the notes don't smear.",
            "static": "It barely moves; rhythm does the work here.",
        }
        rhythm = bass.get("rhythm") or ""
        offbeat = len(rhythm) == STEPS and rhythm[0] == "." and all(rhythm[i] in "xo" for i in (2, 6, 10, 14))
        degrees = f" (scale degrees {' - '.join(bass['degrees'])})" if bass.get("degrees") else ""
        add("bass", f"Bass loop: {' - '.join(bass['loop'])} ({bass['movement']})",
            f"Per-bar bass notes loop as {' - '.join(bass['loop'])}{degrees}. {tips[bass['movement']]}"
            + (" The bass hits the off-beats (house-style), so leave the downbeats to the kick." if offbeat else ""),
            {"loop": bass["loop"], "degrees": bass["degrees"], "rhythm": rhythm})
    if bpm and ((f.get("low_end") or 0) > 0.2 or style in ("four_floor", "dnb", "halftime")):
        eighth = 60000 / bpm / 2
        add("bass", "Sidechain the bass to the kick",
            "Put a Compressor on the bass, enable Sidechain from the kick, ratio 4:1, attack 0.1 ms, release "
            f"about {eighth * 0.6:.0f}-{eighth:.0f} ms (an 8th note at this tempo is {eighth:.0f} ms). Or use "
            "Live 12's Shaper/Envelope Follower for a drawn ducking curve.",
            {"release_ms": round(eighth), "template": "{sidechain}"})

    # --- Harmony ---
    if harm.get("progression"):
        prog, roman = " - ".join(harm["progression"]), " - ".join(harm["roman"])
        add("harmony", f"Chord loop guess: {prog}",
            f"The most common 4-bar loop reads as {prog} ({roman} in {root} {scale}). Sketch it with the Chord "
            "MIDI effect or Live 12's chord generators, then thin the voicing so it doesn't fight the bass.",
            {"progression": harm["progression"], "roman": harm["roman"]})
        for c in harm["chords"][:4]:
            add("harmony", f"Over {c['name']} ({c['roman']}): play {c['scale']}",
                f"{c['name']} shows up in {c['bars']} bars. For leads or arps over it, {c['scale']} fits.", dict(c))
    if key:
        pent = "Minor Pentatonic" if key.mode == "minor" else "Major Pentatonic"
        add("harmony", f"Safe lead scale: {root} {pent}",
            f"For hooks that never clash, set Scale to {root} {pent} and run an Arpeggiator (Up/Down, 1/16) into a short pluck.",
            {"root": root, "scale": pent})

    # --- Arrangement ---
    secs = [s for s in card.sections if s.start_bar is not None]
    if len(secs) > 1:
        names = [f"{s.label.capitalize()} {s.letter}" if s.letter else s.label.capitalize() for s in secs]
        layout = ", ".join(f"{n} (bars {s.start_bar}-{s.end_bar})" for n, s in zip(names, secs))
        add("arrangement", f"Arrangement map: {len(secs)} sections",
            f"Rough layout: {layout}. Drop locators at those bars in Arrangement View to rebuild the structure, "
            "then fill in your own parts.",
            {"locators": [{"bar": s.start_bar, "name": n} for n, s in zip(names, secs)]})
        builds = [s for s in secs if s.label == "build"]
        if builds:
            s = builds[0]
            bars = (s.end_bar or s.start_bar) - s.start_bar + 1
            add("arrangement", f"Recreate the build at bar {s.start_bar}",
                f"A {bars}-bar riser. Automate an Auto Filter high-pass up over it, add a snare roll that doubles "
                "every 2 bars, and cut the kick for the last beat before the drop.",
                {"start_bar": s.start_bar, "bars": bars})

    # --- Sound ---
    brightness = f.get("brightness")
    if brightness is not None and brightness < 0.35:
        add("sound", "It's a dark mix",
            "Most energy sits low-mid. Roll off highs on pads (EQ Eight shelf around 6 kHz) and let only hats "
            "and one lead live up top.")
    elif brightness is not None and brightness > 0.6:
        add("sound", "It's a bright mix",
            "Lots of top end. Saturator on the drum bus and a touch of Hybrid Reverb with a high low-cut will match it.")
    return out


class SuggestionsStage:
    name = "suggestions"
    version = "0.1.0"
    requires = ("core", "sections", "tags")

    def run(self, ctx: AnalysisContext) -> None:
        card = ctx.card
        key = card.core.key
        tonic = key.tonic if key else None
        scale = (key.scale or ("Minor" if key.mode == "minor" else "Major")) if key else "Major"
        grid, bass, harm = {}, {}, {}
        n_frames = 1 + len(ctx.y) // ctx.hop
        step_frames = _step_grid(ctx, n_frames)
        if step_frames.shape[0]:
            grid = _drum_grid(ctx, step_frames)
            bass = _bass_summary(ctx, step_frames, tonic, scale)
            if tonic is not None:
                harm = _harmony_summary(ctx, step_frames, tonic, scale)
        by_section = []
        if grid:
            secs = [(i, x) for i, x in enumerate(card.sections) if x.start_bar is not None]
            if len(secs) > 1:
                by_section = _section_grooves(grid, _bar_energy(ctx, step_frames), secs)
            card.extras["suggestions.drum_grid"] = {k: {f: v for f, v in g.items() if not f.startswith("_")} for k, g in grid.items()}
        if bass:
            card.extras["suggestions.bass"] = bass
        if harm:
            card.extras["suggestions.harmony"] = harm
        card.suggestions = _suggest(ctx, grid, bass, harm, by_section)


register_stage(SuggestionsStage())
