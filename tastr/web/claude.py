""""Ask Claude to change it" in Try this in > Strudel (TAS-67): one Claude API call that rewrites a Strudel sketch.

The page sends the code it shows, the ask and a short summary of the track; Claude sends back the whole new code
and a short explanation, and the page shows the diff. Tastr never runs the code: it only opens in strudel.cc.

Which key: the server's own environment. On a hosted server that's Tastr's ANTHROPIC_API_KEY (a Fly secret, with a
daily limit per person, routes/strudel.py); on your own machine (`tastr serve`) it's yours. The key is never logged,
echoed or sent to the page.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# The latest Sonnet: quick and good at code. The prices are its list prices in dollars per million tokens, for the
# admin view's cost; change them together with the model.
MODEL = "claude-sonnet-5-5"
PRICE_IN, PRICE_OUT = 2.00, 10.00
MAX_TOKENS = 16000

SYSTEM = """You change Strudel live-code (https://strudel.cc) for musicians. Each request has a short summary of the \
track the code was made from, the current code, and what the musician asked for.

Strudel idioms to keep to:
- Mini-notation lives in double quotes: s("bd*4"), note("<c2 g1>"), chord("<Am F C G>").voicing(). Single quotes are \
plain strings (the sample URLs), so keep them single-quoted.
- setcpm(bpm/4) sets one cycle per bar. stack(...) layers patterns; arrange([bars, pattern], ...) plays them in order.
- Effects chain on a pattern: .lpf(), .hpf(), .room(), .size(), .delay(), .gain(), .pan(), .speed(), .slow(), .fast(), \
.swingBy(), .bank("RolandTR909"), .duck(), .orbit(). slider(value, min, max) shows a knob in the editor. Signals such \
as sine.range(a, b).slow(n) sweep a value.
- `name: pattern` lines play; `_name:` mutes that line. all(x => ...) changes every line.

Rules:
- Make only the change asked for, and keep everything else exactly as it is, comments included.
- Never change or remove the samples({...}) lines, the len/bar/first constants, or the orig(...)/origB(...) helper \
definitions: they point at the musician's own audio.
- Keep the code valid Strudel. Add a short // comment next to what you changed when it helps.
- If the ask can't be done in Strudel, return the code unchanged and say why.

Reply with the full new code in `code` and, in `explanation`, one short paragraph in plain words on what you changed \
and how to tweak it further."""

SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string"}, "explanation": {"type": "string"}},
    "required": ["code", "explanation"],
    "additionalProperties": False,
}


class AskError(Exception):
    """An ask that didn't work, with a message for the page (never the key or server details). `status`: HTTP code."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class Reply:
    code: str
    explanation: str
    input_tokens: int
    output_tokens: int


def sdk_installed() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def cost(input_tokens: int, output_tokens: int) -> float:
    """Dollars for one ask at MODEL's list prices."""
    return (input_tokens * PRICE_IN + output_tokens * PRICE_OUT) / 1_000_000


def prompt(code: str, ask: str, summary: str) -> str:
    return f"<track>\n{summary.strip()}\n</track>\n\n<code>\n{code}\n</code>\n\n<ask>\n{ask.strip()}\n</ask>"


def ask_claude(code: str, ask: str, summary: str, client=None) -> Reply:
    """One ask. `client`: an anthropic.Anthropic (tests pass a stub); by default one reading ANTHROPIC_API_KEY."""
    import anthropic

    client = client or anthropic.Anthropic(max_retries=2, timeout=120)
    try:
        # fallbacks="default": if Sonnet's safeguards decline a harmless music ask, the API retries it on another model.
        msg = client.beta.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
            messages=[{"role": "user", "content": prompt(code, ask, summary)}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        log.error("Claude refused the API key (%s)", type(e).__name__)  # no message: it could quote the key
        raise AskError("Claude isn't set up right on this server: its API key was refused.", 503) from None
    except anthropic.RateLimitError:
        raise AskError("Claude is busy right now. Try again in a minute.", 503) from None
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s (request %s)", e.status_code, getattr(e, "request_id", None))
        raise AskError("Claude couldn't answer just now. Try again in a minute.", 502) from None
    except anthropic.APIConnectionError:
        raise AskError("Couldn't reach Claude. Check the server's internet connection.", 502) from None
    usage = getattr(msg, "usage", None)
    tin, tout = (getattr(usage, "input_tokens", 0) or 0), (getattr(usage, "output_tokens", 0) or 0)
    if msg.stop_reason == "refusal":
        raise AskError("Claude declined that ask. Try putting it another way.", 422)
    if msg.stop_reason == "max_tokens":
        raise AskError("The new code came out too long. Try a smaller change.", 422)
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
        new_code, said = data["code"], data["explanation"]
        if not isinstance(new_code, str) or not isinstance(said, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise AskError("Claude's answer didn't come back as code. Try again.", 502) from None
    return Reply(new_code, said.strip(), tin, tout)
