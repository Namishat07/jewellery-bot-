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


# ---------------------------------------------------------------------------
# Free-text retrieval — used to ground chat answers in the real catalogue
# ---------------------------------------------------------------------------

# Words that carry no signal for matching a product title. Without this, "me" in
# "give me a ring" matches every title containing "me" (Gemstone, Charm...).
STOPWORDS = {
    "a", "an", "the", "me", "my", "i", "you", "show", "give", "want", "need", "find",
    "get", "some", "any", "for", "of", "in", "on", "with", "and", "or", "to", "under",
    "below", "above", "over", "less", "than", "cheap", "cheaper", "price", "priced",
    "rs", "inr", "rupees", "please", "can", "do", "have", "is", "are", "what", "which",
    "recommend", "suggest", "looking", "buy", "best", "good", "nice", "something",
}

# "under 3000", "below rs. 5,000", "less than 2000", "upto 1500"
_MAX_PRICE_RE = re.compile(
    r"(?:under|below|less than|cheaper than|within|upto|up to|max|maximum|<=?)\s*"
    r"(?:rs\.?|inr|₹|\$)?\s*([\d,]+)", re.I)
# "above 2000", "over rs 5000", "at least 1000"
_MIN_PRICE_RE = re.compile(
    r"(?:above|over|more than|at least|starting|minimum|min|>=?)\s*"
    r"(?:rs\.?|inr|₹|\$)?\s*([\d,]+)", re.I)
# "between 1000 and 3000"
_RANGE_RE = re.compile(
    r"between\s*(?:rs\.?|inr|₹|\$)?\s*([\d,]+)\s*(?:and|-|to)\s*(?:rs\.?|inr|₹|\$)?\s*([\d,]+)", re.I)


def parse_price_constraints(query: str) -> tuple[float | None, float | None]:
    """Pull (min_price, max_price) out of a natural-language question."""
    def num(s: str) -> float:
        return float(s.replace(",", ""))

    match = _RANGE_RE.search(query)
    if match:
        low, high = sorted((num(match.group(1)), num(match.group(2))))
        return low, high

    min_price = max_price = None
    match = _MAX_PRICE_RE.search(query)
    if match:
        max_price = num(match.group(1))
    match = _MIN_PRICE_RE.search(query)
    if match:
        min_price = num(match.group(1))
    return min_price, max_price


def search_products(products: list, query: str, limit: int = 25) -> list:
    """
    Rank the catalogue against a free-text question. Keyword scoring over
    title/type/tags, with any price constraint in the question applied as a
    hard filter. Cheap enough to run per-message over a few thousand products.
    """
    min_price, max_price = parse_price_constraints(query)

    candidates = products
    if min_price is not None:
        candidates = [p for p in candidates if (p.get("price") or 0) >= min_price]
    if max_price is not None:
        candidates = [p for p in candidates if p.get("price") is not None and p["price"] <= max_price]

    terms = {
        w for w in re.findall(r"[a-z]+", query.lower())
        if len(w) > 2 and w not in STOPWORDS
    }

    if not terms:
        # Pure price question ("anything under 3000?") — no keywords to rank by.
        return sorted(candidates, key=lambda p: p.get("price") or 0)[:limit]

    scored = []
    for p in candidates:
        title = (p.get("title") or "").lower()
        ptype = (p.get("product_type") or "").lower()
        tags = " ".join(p.get("tags") or []).lower()
        body = (p.get("description") or "").lower()

        score = 0
        for term in terms:
            singular = term[:-1] if term.endswith("s") and len(term) > 3 else term
            if singular in title:
                score += 5          # title is the strongest signal
            if singular in ptype:
                score += 3
            if singular in tags:
                score += 2
            if singular in body:
                score += 1
        if score:
            scored.append((score, p.get("price") or 0, p))

    # best match first; ties broken by cheapest, which is what shoppers expect
    scored.sort(key=lambda t: (-t[0], t[1]))
    results = [p for _, _, p in scored[:limit]]

    # If nothing matched by keyword but a price filter did apply, still show those.
    if not results and (min_price is not None or max_price is not None):
        return sorted(candidates, key=lambda p: p.get("price") or 0)[:limit]
    return results


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
