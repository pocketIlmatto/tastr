"""The template drum patterns the engine suggests per style (one bar each, "x" a hit, "o" a softer one, "." a
rest), for the Glossary's pattern grids (/api/patterns). The Rust engine picks from the same list
(engine-rs/src/stages/suggestions.rs, `variations`); tests/test_patterns.py checks the two agree.
"""

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
    # 3/4: 12 steps, any tempo.
    "waltz": [
        ("Waltz", {"kick": "x...........", "snare": "....x...x...", "hat": "x...x...x..."}),
        ("Jazz waltz", {"kick": "x.......x...", "snare": "......x.....", "hat": "x..xx..xx..x"}),
    ],
}


STYLE_LABELS = {"four_floor": "four on the floor", "dnb": "drum and bass", "halftime": "halftime",
                "hiphop": "hip-hop", "generic": "generic backbeat", "waltz": "3/4 waltz"}
