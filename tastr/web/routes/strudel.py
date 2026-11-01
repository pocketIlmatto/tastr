"""Try this in > Strudel (TAS-67): each person's own templates, and "Ask Claude to change it"."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ... import db
from .. import claude
from ..context import AppContext
from ..hosted import today

MAX_TEMPLATES, MAX_TEMPLATE_BYTES = 50, 20_000  # templates per person, and the size of one
MAX_CODE_BYTES, MAX_ASK_CHARS, MAX_SUMMARY_CHARS = 60_000, 500, 2_000


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1)


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
