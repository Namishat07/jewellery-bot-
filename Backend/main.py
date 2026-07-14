"""
main.py
=======
FastAPI backend for the jewellery chatbot. Serves:
  - POST /api/session          -> submit a site URL, scrape it, get a session_id
  - GET  /api/session/{id}     -> session status + available filter options
  - POST /api/chat             -> SSE streaming chat response
  - POST /api/image-search     -> upload an image, get matched products
  - POST /api/filter           -> MCQ-style guided filter
  - (in production) serves the built React frontend as static files

Run locally:
    uvicorn main:app --reload --port 8000

Colab (for quick endpoint testing via ngrok, NOT for real deployment):
    !pip install fastapi uvicorn python-multipart nest-asyncio pyngrok -q
    import nest_asyncio; nest_asyncio.apply()
    # then run uvicorn.run(app, port=8000) in a cell and expose via pyngrok
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Must run before anything reads GROQ_API_KEY. Anchored to this file's directory so
# it works regardless of the cwd uvicorn was launched from.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

log = logging.getLogger(__name__)

from scraper import scrape_site
from session_store import store
from groq_client import build_system_prompt, stream_chat_response, tag_image
from matching import (
    filter_products,
    match_products_by_image_tags,
    get_available_filter_options,
    search_products,
)


# ---------------------------------------------------------------------------
# Background session cleanup
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)  # was 600 — now check every 1 minute
        store.cleanup_expired()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("GROQ_API_KEY"):
        log.error(
            "GROQ_API_KEY is not set — /api/chat and /api/image-search will fail. "
            "Put it in Backend/.env as GROQ_API_KEY=gsk_..."
        )
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Jewellery Site Chatbot API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this to your actual frontend origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    url: str


class ChatRequest(BaseModel):
    session_id: str
    message: str


class FilterRequest(BaseModel):
    session_id: str
    occasion: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    material: str | None = None
    jewellery_type: str | None = None


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

@app.post("/api/session")
async def create_session(req: CreateSessionRequest):
    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")

    result = await scrape_site(req.url)

    if not result["products"]:
        reason = result["meta"].get("failure_reason", "no_products")
        messages = {
            "blocked": (403, "This site blocks automated access, so its catalogue can't be read."),
            "throttled": (503, "This site is rate-limiting us right now. Wait a minute and try again."),
            "no_products": (422, "Couldn't find any products on this site. It may use a catalogue "
                                 "structure this scraper doesn't recognise yet."),
        }
        status, detail = messages.get(
            reason, (422, f"Couldn't read this site ({reason}).")
        )
        log.warning("scrape failed for %s: %s", req.url, reason)
        raise HTTPException(status_code=status, detail=detail)

    session = store.create(
        site_url=result["site_url"],
        products=result["products"],
        policies=result["policies"],
        meta=result["meta"],
    )

    return {
        "session_id": session.session_id,
        "site_url": session.site_url,
        "product_count": len(session.products),
        "scrape_method": session.meta["method"],
        "elapsed_seconds": session.meta["elapsed_seconds"],
        "filter_options": get_available_filter_options(session.products),
    }


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return {
        "session_id": session.session_id,
        "site_url": session.site_url,
        "product_count": len(session.products),
        "filter_options": get_available_filter_options(session.products),
    }


# ---------------------------------------------------------------------------
# Chat (SSE streaming)
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def chat(req: ChatRequest):
    session = store.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    # Ground the answer in the actual catalogue: retrieve the products relevant to
    # this question and put them in the prompt. The full catalogue can be thousands
    # of items, far too many to send to the model on every turn.
    relevant = search_products(session.products, req.message, limit=25)
    log.info("chat: %d/%d products matched %r", len(relevant), len(session.products), req.message)

    system_prompt = build_system_prompt(
        session.site_url, session.policies, len(session.products), relevant_products=relevant
    )
    store.append_message(req.session_id, "user", req.message)

    async def event_stream():
        full_response = ""
        try:
            async for chunk in stream_chat_response(
                req.session_id, system_prompt, session.chat_history, req.message
            ):
                full_response += chunk
                yield f"data: {chunk}\n\n"
        except Exception:
            # The 200 + headers are already on the wire by now, so we cannot turn this
            # into an HTTP error. Raising here would truncate the chunked body and the
            # browser would report ERR_INCOMPLETE_CHUNKED_ENCODING with no useful detail.
            log.exception("chat stream failed for session %s", req.session_id)
            yield "event: error\ndata: The assistant backend failed. Check the server logs.\n\n"
        finally:
            store.append_message(req.session_id, "assistant", full_response)
            yield "event: done\ndata: \n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Image-based recommendation
# ---------------------------------------------------------------------------

@app.post("/api/image-search")
async def image_search(session_id: str = Form(...), image: UploadFile = File(...)):
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    image_bytes = await image.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 10MB)")

    tags = await tag_image(image_bytes, mime_type=image.content_type or "image/jpeg")
    matches = match_products_by_image_tags(session.products, tags)

    return {"tags": tags, "matches": matches}


# ---------------------------------------------------------------------------
# MCQ-style guided filter
# ---------------------------------------------------------------------------

@app.post("/api/filter")
async def filter_endpoint(req: FilterRequest):
    session = store.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    matches = filter_products(
        session.products,
        occasion=req.occasion,
        min_price=req.min_price,
        max_price=req.max_price,
        material=req.material,
        jewellery_type=req.jewellery_type,
    )
    return {"matches": matches}


@app.get("/api/health")
async def health():
    return {"status": "ok", "active_sessions": store.active_count()}


# ---------------------------------------------------------------------------
# Serve built React frontend (static files) — added once frontend is built
# ---------------------------------------------------------------------------
# This MUST stay last. Starlette matches routes in registration order, and a mount
# at "/" swallows everything beneath it — when this sat above /api/health, that
# endpoint 404'd, which would fail Cloud Run's health check.

FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
