"""Try this in > Strudel (TAS-67): each person's own templates, "Ask Claude to change it", and short share links
(TAS-100)."""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
import string
from contextlib import closing
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ... import db
from .. import claude
from ..context import STATIC_DIR, AppContext
from ..hosted import PUBLIC_PREFIX, today

MAX_TEMPLATES, MAX_TEMPLATE_BYTES = 50, 20_000  # templates per person, and the size of one
MAX_CODE_BYTES, MAX_ASK_CHARS, MAX_SUMMARY_CHARS = 60_000, 500, 2_000
MAX_SHARES, SHARE_ID_LEN = 1000, 10  # share links per person; 10 letters and digits is about 59 bits, too many to guess
SHARE_PREFIX = PUBLIC_PREFIX + "s/"  # under /pub/, so Cloudflare Access lets it through like the audio links
PREVIEW_PATH = PUBLIC_PREFIX + "preview.png"  # the link-preview image; crawlers can't get past Access to /static/
SIGNED = re.compile(r"/pub/(\d+\.[A-Za-z0-9_-]+)/tracks/(\d+)/")  # a /pub/ audio or stem link in the code


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1)


class ShareBody(BaseModel):
    code: str = Field(min_length=1)


def strudel_url(code: str) -> str:
    """The strudel.cc link that opens `code`: the same encoding as strudelLink in static/strudel.js."""
    return "https://strudel.cc/#" + quote(base64.b64encode(code.encode()).decode(), safe="")


def share_page(code: str, origin: str = "") -> str:
    """A page that goes straight on to strudel.cc (a meta refresh: the link is too long for a safe redirect header,
    and the page's CSP allows no script). Its title is the code's first comment, and its OpenGraph and Twitter tags
    give chat apps a preview card: their crawlers read the tags and don't follow the refresh. `origin` (like
    https://host) makes the image's address absolute, which the tags need."""
    url = html.escape(strudel_url(code))
    first = next((ln.strip()[2:].strip() for ln in code.splitlines() if ln.strip().startswith("//")), "")
    title = html.escape(f"Strudel: {first}" if first else "Strudel code from Tastr")
    desc = "Live code from Tastr. Opens in strudel.cc, ready to play."
    image = html.escape(origin + PREVIEW_PATH)
    meta = "".join(f'<meta property="{k}" content="{v}">' for k, v in [
        ("og:type", "website"), ("og:site_name", "Tastr"), ("og:title", title), ("og:description", desc),
        ("og:image", image), ("og:image:width", "1200"), ("og:image:height", "630")])
    meta += "".join(f'<meta name="{k}" content="{v}">' for k, v in [
        ("description", desc), ("twitter:card", "summary_large_image"), ("twitter:title", title),
        ("twitter:description", desc), ("twitter:image", image)])
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
            f'<meta name="robots" content="noindex"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{meta}<link rel="icon" href="{PUBLIC_PREFIX}favicon.svg" type="image/svg+xml">'
            f'<meta http-equiv="refresh" content="0; url={url}"></head>'
            f'<body style="font-family: system-ui, sans-serif; padding: 2rem"><p>Opening strudel.cc&hellip; '
            f'<a href="{url}">Open it yourself</a> if nothing happens.</p></body></html>')


class AskBody(BaseModel):
    code: str = Field(min_length=1)
    ask: str = Field(min_length=1, max_length=MAX_ASK_CHARS)
    summary: str = Field("", max_length=MAX_SUMMARY_CHARS)  # BPM, key and sections, written by the page


def router(ctx: AppContext) -> APIRouter:
    api = APIRouter()

    def checked(body: TemplateBody) -> tuple[str, str]:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Give the template a name.")
        if len(body.text.encode()) > MAX_TEMPLATE_BYTES:
            raise HTTPException(413, f"That template is too big to save (the most is {MAX_TEMPLATE_BYTES // 1000} KB).")
        return name, body.text

    # Templates: the signed-in email's own when hosted, else everyone's (like saved searches).
    @api.get("/api/strudel/templates")
    def templates(request: Request):
        with closing(ctx.conn()) as c:
            return db.list_templates(c, ctx.owner_of(request))

    @api.post("/api/strudel/templates")
    def add_template(body: TemplateBody, request: Request):
        name, text = checked(body)
        owner = ctx.owner_of(request)
        with closing(ctx.conn()) as c:
            if len(db.list_templates(c, owner)) >= MAX_TEMPLATES:
                raise HTTPException(409, f"You have {MAX_TEMPLATES} templates. Delete one first.")
            return db.add_template(c, owner, name, text)

    @api.put("/api/strudel/templates/{template_id}")
    def update_template(template_id: int, body: TemplateBody, request: Request):
        name, text = checked(body)
        with closing(ctx.conn()) as c:
            t = db.update_template(c, ctx.owner_of(request), template_id, name, text)
        if t is None:
            raise HTTPException(404, "no such template")
        return t

    @api.delete("/api/strudel/templates/{template_id}", status_code=204)
    def delete_template(template_id: int, request: Request):
        with closing(ctx.conn()) as c:
            if not db.delete_template(c, ctx.owner_of(request), template_id):
                raise HTTPException(404, "no such template")
        return Response(status_code=204)

    # Short share links (TAS-100).
    @api.post("/api/strudel/share")
    def share(body: ShareBody, request: Request):
        """Save this code and return a short link to it ({id, url}). The same code from the same person gets the
        same link."""
        if len(body.code.encode()) > MAX_CODE_BYTES:
            raise HTTPException(413, f"That code is too long to share (the most is {MAX_CODE_BYTES // 1000} KB).")
        owner, sha = ctx.owner_of(request), hashlib.sha256(body.code.encode()).hexdigest()
        with closing(ctx.conn()) as c:
            sid = db.find_share(c, owner, sha)
            if sid is None:
                if db.count_shares(c, owner) >= MAX_SHARES:
                    raise HTTPException(409, f"You have {MAX_SHARES} short links, the most there can be.")
                while sid is None or db.get_share(c, sid) is not None:
                    sid = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(SHARE_ID_LEN))
                db.add_share(c, sid, owner, sha, body.code)
        return {"id": sid, "url": SHARE_PREFIX + sid}

    def fresh_links(c, owner: str | None, code: str) -> str:
        """The code with its signed audio and stem links signed again, so they work for another LINK_DAYS days.
        Only for tracks the person who made the link still has; the rest are left as they were (and expire)."""
        if ctx.signer is None:
            return code  # on your own machine the links aren't signed

        def sign(m: re.Match) -> str:
            tid = int(m[2])
            if db.track_owner(c, tid) != (True, owner):
                return m[0]
            return f"/pub/{ctx.signer.token(tid)}/tracks/{tid}/"
        return SIGNED.sub(sign, code)

    @api.get(PREVIEW_PATH, include_in_schema=False)
    @api.get(PUBLIC_PREFIX + "favicon.svg", include_in_schema=False)
    def public_image(request: Request):
        """The share page's preview image and icon, under /pub/ so a crawler or a signed-out browser can get them
        (on the Worker, worker/src/entry.py serves these two from Static Assets before they reach here)."""
        name = request.url.path.removeprefix(PUBLIC_PREFIX)
        kind = "image/png" if name.endswith(".png") else "image/svg+xml"
        return Response((STATIC_DIR / name).read_bytes(), media_type=kind,
                        headers={"Cache-Control": "public, max-age=86400"})

    @api.get(SHARE_PREFIX + "{share_id}", include_in_schema=False)
    def open_share(share_id: str, request: Request):
        """Anyone with the link: on to strudel.cc with the code. No login (strudel.cc links get passed around)."""
        with closing(ctx.conn()) as c:
            got = db.get_share(c, share_id) if share_id.isalnum() and len(share_id) <= 32 else None
            if got is None:
                raise HTTPException(404, "There's no Strudel code at this link.")
            code = fresh_links(c, *got)
        origin = f"{'https' if ctx.hosted else request.url.scheme}://{request.url.netloc}"
        return HTMLResponse(share_page(code, origin), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    # Ask Claude.
    def status(request: Request) -> dict:
        """Whether Ask works here, and on a hosted server how many asks this person has left today."""
        out = {"hosted": ctx.hosted, "model": claude.MODEL, "available": claude.sdk_installed() and claude.has_key(),
               "limit": None, "left": None, "reason": None}
        if not claude.sdk_installed():
            out["reason"] = "hosted-off" if ctx.hosted else "no-sdk"
        elif not claude.has_key():
            out["reason"] = "hosted-off" if ctx.hosted else "no-key"
        if ctx.hosted:
            user = request.state.user
            with closing(ctx.conn()) as c:
                out["limit"] = ctx.cap(c, user, "asks")
                out["left"] = max(0, out["limit"] - db.usage_today(c, user, "asks", today()))
        return out

    @api.get("/api/strudel/ask")
    def ask_status(request: Request):
        return status(request)

    @api.post("/api/strudel/ask")
    def ask(body: AskBody, request: Request):
        """Send the code, the ask and the track summary to Claude; returns {code, explanation} plus what's left today.
        429 once a hosted person has used today's asks."""
        if len(body.code.encode()) > MAX_CODE_BYTES:
            raise HTTPException(413, "That code is too long to send to Claude.")
        st = status(request)
        if not st["available"]:
            raise HTTPException(503, "Ask Claude isn't turned on here." if ctx.hosted
                                else "Set ANTHROPIC_API_KEY before `tastr serve` to ask Claude.")
        user = request.state.user if ctx.hosted else None
        if ctx.hosted and st["left"] <= 0:
            raise ctx.refuse(user, 429, f"You've used today's {st['limit']} asks. They come back at 00:00 UTC.",
                             f"daily Ask Claude limit ({st['limit']})")
        try:
            reply = claude.ask_claude(body.code, body.ask, body.summary)
        except claude.AskError as e:
            raise HTTPException(e.status, str(e)) from None
        left = None
        if ctx.hosted:
            day = today()
            with closing(ctx.conn()) as c:
                db.add_usage(c, user, "asks", day)
                db.add_usage(c, user, "ask_in", day, reply.input_tokens)
                db.add_usage(c, user, "ask_out", day, reply.output_tokens)
            left = st["left"] - 1
        return {"code": reply.code, "explanation": reply.explanation, "left": left, "limit": st["limit"],
                "tokens": {"input": reply.input_tokens, "output": reply.output_tokens}}

    return api
