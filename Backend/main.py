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
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scraper import scrape_site
from session_store import store
from groq_client import build_system_prompt, stream_chat_response, tag_image
from matching import filter_products, match_products_by_image_tags, get_available_filter_options


# ---------------------------------------------------------------------------
# Background session cleanup
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    while True:
        await asyncio.sleep(600)  # every 10 minutes
        store.cleanup_expired()


@asynccontextmanager
async def lifespan(app: FastAPI):
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
        raise HTTPException(
            status_code=422,
            detail="Could not find any products on this site. It may block automated access, "
                   "or use a catalog structure this scraper doesn't recognize yet.",
        )

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

    system_prompt = build_system_prompt(session.site_url, session.policies, len(session.products))
    store.append_message(req.session_id, "user", req.message)

    async def event_stream():
        full_response = ""
        try:
            async for chunk in stream_chat_response(
                req.session_id, system_prompt, session.chat_history, req.message
            ):
                full_response += chunk
                yield f"data: {chunk}\n\n"
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


# ---------------------------------------------------------------------------
# Serve built React frontend (static files) — added once frontend is built
# ---------------------------------------------------------------------------

FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


@app.get("/api/health")
async def health():
    return {"status": "ok", "active_sessions": store.active_count()}
