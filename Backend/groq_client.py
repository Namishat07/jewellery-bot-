"""
groq_client.py
==============
Thin wrapper around the Groq API for two things this project needs:

1. Streaming chat completions — used for the conversational Q&A
   (policies, general questions about the site's catalog).
2. Vision tagging — used for image-based recommendations: user uploads
   a photo, we ask a Groq vision model to describe it in structured tags
   (jewellery type, material, style, color), then match those tags against
   the session's scraped catalog.

Requires env var GROQ_API_KEY (get one free at console.groq.com).

Colab setup:
    !pip install groq -q
    import os
    os.environ["GROQ_API_KEY"] = "your-key-here"
"""

import base64
import json
import os

from groq import AsyncGroq

TEXT_MODEL = "openai/gpt-oss-120b"       # Groq's current general-purpose text model (as of July 2026)
VISION_MODEL = "qwen/qwen3.6-27b"        # Groq's current multimodal/vision model (preview tier — verify at console.groq.com/docs/models before deploying, Groq's lineup changes often)

_client: AsyncGroq | None = None


def get_client() -> AsyncGroq:
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY environment variable is not set")
        _client = AsyncGroq(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Chat (streaming)
# ---------------------------------------------------------------------------

def _format_price(price, currency: str | None) -> str:
    if price is None:
        return "price not listed"
    symbol = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}.get(currency or "", "")
    return f"{symbol}{price:,.0f}" if symbol else f"{price:,.0f} {currency or ''}".strip()


def build_system_prompt(
    site_url: str,
    policies: dict,
    product_count: int,
    relevant_products: list | None = None,
) -> str:
    policy_context = "\n".join(
        f"{name.replace('_', ' ').title()}: {text[:1500]}"
        for name, text in policies.items() if text
    ) or "No policy information was found for this site."

    if relevant_products:
        lines = []
        for p in relevant_products:
            price = _format_price(p.get("price"), p.get("currency"))
            lines.append(f"- {p.get('title')} — {price} — {p.get('product_url')}")
        catalogue_context = (
            "These are the products from this site most relevant to the user's question "
            "(already filtered and ranked for you):\n" + "\n".join(lines)
        )
    else:
        catalogue_context = (
            "No products in this catalogue matched the user's question. Say so honestly "
            "rather than inventing items."
        )

    return f"""You are a helpful shopping assistant for the jewellery website {site_url}.
You ONLY answer questions about this specific website — its products, policies, and shopping experience.
The site has {product_count} products in the catalogue.

{catalogue_context}

Here is the policy information scraped from this site:
{policy_context}

Rules:
- Recommend products ONLY from the list above. Never invent a product, price, or link.
- When recommending, name the product, give its price, and include its link as a markdown link.
- Recommend at most 5 products unless asked for more. Lead with the best fit and say why in a few words.
- If the list above is empty or nothing fits, say plainly that you couldn't find a match on this site, and suggest relaxing the budget or trying a different style.
- Answer policy questions (returns, shipping, etc.) using ONLY the policy information above. If it isn't covered, say you don't have that information and point the user to the site.
- Keep answers concise and warm, like a good store assistant. No preamble.
"""


async def stream_chat_response(session_id: str, system_prompt: str, chat_history: list, user_message: str):
    """
    Async generator yielding text chunks as they arrive from Groq.
    chat_history: list of {"role": "user"/"assistant", "content": str}
    """
    client = get_client()
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history[-10:])  # keep last 10 turns for context, bound token usage
    messages.append({"role": "user", "content": user_message})

    stream = await client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        stream=True,
        temperature=0.5,
        max_tokens=800,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


# ---------------------------------------------------------------------------
# Vision tagging (image-based recommendations)
# ---------------------------------------------------------------------------

VISION_TAG_PROMPT = """Look at this jewellery image and respond with ONLY a JSON object (no markdown, no explanation) in this exact format:
{
  "jewellery_type": "ring" | "necklace" | "earrings" | "bracelet" | "bangle" | "pendant" | "anklet" | "other",
  "material": "gold" | "silver" | "platinum" | "diamond" | "pearl" | "gemstone" | "other",
  "style": "a few descriptive words, e.g. 'minimalist', 'vintage', 'statement', 'floral'",
  "color": "dominant color(s)",
  "keywords": ["list", "of", "5-8", "descriptive", "keywords", "for", "matching", "similar", "products"]
}
Respond with ONLY the JSON object."""


async def tag_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Send an image to Groq's vision model and return structured tags."""
    client = get_client()
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    response = await client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_TAG_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_image}"}},
                ],
            }
        ],
        temperature=0.2,
        max_tokens=600,
        reasoning_effort="none",  # qwen3 models only accept 'none' or 'default' — 'low'/'medium'/'high' are GPT-OSS-only and cause a 400. 'none' disables reasoning entirely, which is fine here since tagging doesn't need chain-of-thought.
    )

    raw = response.choices[0].message.content.strip()
    print(f"[groq_client] raw vision response: {raw[:500]!r}")  # temporary debug log — remove once this is confirmed stable

    tags = _extract_json_object(raw)
    if tags is not None:
        return tags

    return {
        "jewellery_type": "other",
        "material": "other",
        "style": "",
        "color": "",
        "keywords": [],
    }


def _extract_json_object(raw: str) -> dict | None:
    """
    Robustly pull a JSON object out of a model response that may contain
    markdown fences, stray reasoning text, or prose before/after the JSON.
    """
    # 1. try straight parse first (fast path when the model behaves)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. strip markdown code fences
    cleaned = raw
    if "```" in cleaned:
        fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass

    # 3. find the first {...} block anywhere in the text (handles leaked reasoning text)
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return None
