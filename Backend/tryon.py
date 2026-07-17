"""
tryon.py
========
Prepares "try-on" assets for products: a background-removed cutout PNG,
plus which body landmark it should be anchored to (ear / neck / finger / wrist).

Flow:
  1. prepare_asset() is called once per product (cached after that).
  2. It downloads the scraped product photo, runs background removal,
     saves the cutout to disk, and returns its URL + anchor type.
  3. The frontend fetches the cutout once and re-uses it every frame —
     background removal never runs per-frame, only per-product.

Cache layout: tryon_cache/{session_id}/{product_id}.png
"""

import logging
import os
import re
from io import BytesIO

import aiohttp
from PIL import Image
from rembg import remove, new_session

log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "tryon_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# rembg's session holds the ML model in memory — create it once at import time,
# not per-request, since loading it is the slow part.
_rembg_session = new_session("u2net")

# Keyword -> anchor type, checked against product title/type/tags.
# This drives which MediaPipe landmarks the frontend tracks (face vs hand).
_ANCHOR_KEYWORDS = {
    "earring": [r"\bearring", r"\bstud", r"\bjhumk", r"\bhoop\b"],
    "necklace": [r"\bnecklace", r"\bpendant", r"\bchain\b", r"\bchoker", r"\bmangalsutra"],
    "ring": [r"\bring\b"],
    "bangle": [r"\bbangle", r"\bbracelet", r"\bkada\b", r"\bcuff\b"],
}


def classify_anchor(product: dict) -> str:
    """Guess which body part a product is worn on, from its text fields.
    Falls back to 'necklace' (safest default anchor) if nothing matches —
    the frontend can let the user override this if it's guessed wrong."""
    haystack = " ".join([
        product.get("title", ""),
        product.get("product_type", ""),
        " ".join(product.get("tags", []) or []),
    ]).lower()

    for anchor_type, patterns in _ANCHOR_KEYWORDS.items():
        if any(re.search(p, haystack) for p in patterns):
            return anchor_type
    return "necklace"


def _cache_path(session_id: str, product_id: str) -> str:
    # product_id can contain URL characters when scraped from non-Shopify sites —
    # sanitize before using it as a filename.
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", product_id)[:120]
    session_dir = os.path.join(CACHE_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    return os.path.join(session_dir, f"{safe_id}.png")


async def prepare_asset(session_id: str, product: dict) -> dict:
    """Downloads the product photo, removes its background, caches the cutout.
    Returns {"anchor_type": str, "ready": bool}. Idempotent — if the cutout is
    already cached, this just confirms it exists without redoing the work."""
    product_id = str(product.get("id"))
    image_url = product.get("image_url")
    if not image_url:
        return {"anchor_type": classify_anchor(product), "ready": False, "reason": "no_image"}

    path = _cache_path(session_id, product_id)
    if os.path.exists(path):
        return {"anchor_type": classify_anchor(product), "ready": True}

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                raw = await resp.read()
    except Exception:
        log.exception("try-on: failed to download image for product %s", product_id)
        return {"anchor_type": classify_anchor(product), "ready": False, "reason": "download_failed"}

    try:
        cutout_bytes = remove(raw, session=_rembg_session)
        img = Image.open(BytesIO(cutout_bytes)).convert("RGBA")
        # Cap size — scraped photos are often much larger than needed for an
        # overlay that's drawn small on a video frame; keeps the cache light.
        img.thumbnail((600, 600))
        img.save(path, format="PNG")
    except Exception:
        log.exception("try-on: background removal failed for product %s", product_id)
        return {"anchor_type": classify_anchor(product), "ready": False, "reason": "processing_failed"}

    return {"anchor_type": classify_anchor(product), "ready": True}


def get_cached_path(session_id: str, product_id: str) -> str | None:
    path = _cache_path(session_id, product_id)
    return path if os.path.exists(path) else None
