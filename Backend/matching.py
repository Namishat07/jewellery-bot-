"""
matching.py
===========
Pure in-memory filtering/matching over a session's product list. No DB,
no vector search — just Python over a list of dicts, which is plenty fast
for realistic jewellery-catalog sizes (hundreds to low thousands of items).

Two entry points:
  - filter_products(...)  -> MCQ-style guided recommendation (occasion,
    price range, material, jewellery type)
  - match_products_by_image_tags(...) -> image-based recommendation,
    scores products against the tags Groq's vision model extracted
"""

import re

OCCASION_KEYWORDS = {
    "wedding": ["wedding", "bridal", "bride", "marriage"],
    "engagement": ["engagement", "proposal", "solitaire"],
    "party": ["party", "cocktail", "statement", "evening"],
    "daily wear": ["daily", "everyday", "casual", "minimal"],
    "festive": ["festive", "festival", "traditional", "ethnic"],
    "gift": ["gift", "anniversary", "birthday"],
}


def filter_products(
    products: list,
    occasion: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    material: str | None = None,
    jewellery_type: str | None = None,
    limit: int = 20,
) -> list:
    """MCQ-style guided filter. Any argument left as None is not applied."""
    results = products

    if min_price is not None:
        results = [p for p in results if p.get("price") is not None and p["price"] >= min_price]

    if max_price is not None:
        results = [p for p in results if p.get("price") is not None and p["price"] <= max_price]

    if material:
        material_lower = material.lower()
        results = [
            p for p in results
            if material_lower in (p.get("title", "") + " " + p.get("description", "")).lower()
        ]

    if jewellery_type:
        type_lower = jewellery_type.lower()
        results = [
            p for p in results
            if type_lower in (p.get("title", "") + " " + p.get("description", "")).lower()
        ]

    if occasion:
        keywords = OCCASION_KEYWORDS.get(occasion.lower(), [occasion.lower()])
        results = [
            p for p in results
            if any(kw in (p.get("title", "") + " " + p.get("description", "")).lower() for kw in keywords)
        ]

    return results[:limit]


def match_products_by_image_tags(products: list, tags: dict, limit: int = 12) -> list:
    """
    Score each product against the tags extracted from an uploaded image
    (jewellery_type, material, style, color, keywords) and return the
    best matches, highest score first.
    """
    search_terms = []
    for field in ("jewellery_type", "material", "style", "color"):
        value = tags.get(field, "")
        if value and value != "other":
            search_terms.append(value.lower())
    search_terms.extend(k.lower() for k in tags.get("keywords", []))

    if not search_terms:
        return products[:limit]

    scored = []
    for p in products:
        haystack = (p.get("title", "") + " " + p.get("description", "")).lower()
        score = sum(1 for term in search_terms if term in haystack)
        # bonus weight if jewellery_type matches exactly — this is the strongest signal
        jtype = tags.get("jewellery_type", "").lower()
        if jtype and jtype != "other" and jtype in haystack:
            score += 2
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [p for _, p in scored[:limit]]

    # fallback: if nothing scored, just return a slice so the UI isn't empty
    return results if results else products[:limit]


def get_available_filter_options(products: list) -> dict:
    """
    Inspect the catalog to suggest sensible MCQ options for the frontend
    (e.g. actual price range present, rather than hardcoded guesses).
    """
    prices = [p["price"] for p in products if p.get("price") is not None]
    return {
        "min_price": min(prices) if prices else 0,
        "max_price": max(prices) if prices else 0,
        "occasions": list(OCCASION_KEYWORDS.keys()),
        "jewellery_types": ["ring", "necklace", "earrings", "bracelet", "bangle", "pendant", "anklet"],
        "materials": ["gold", "silver", "platinum", "diamond", "pearl", "gemstone"],
    }
