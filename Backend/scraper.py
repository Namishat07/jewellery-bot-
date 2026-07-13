"""
scraper.py
==========
Site ingestion module for the per-site jewellery chatbot.

Given a store URL, produces a normalized dict:
{
    "site_url": str,
    "products": [
        {
            "id": str,
            "title": str,
            "price": float | None,
            "currency": str | None,
            "image_url": str | None,
            "product_url": str,
            "description": str,
        },
        ...
    ],
    "policies": {
        "return_policy": str, "shipping_policy": str, "faq": str,
        "terms": str, "other": str,
    },
    "meta": {
        "method": "shopify_json" | "html_fallback" | "browser_fallback",
        "product_count": int,
        "elapsed_seconds": float,
    }
}

Three-tier strategy
--------------------
1. Shopify JSON fast-path — /products.json, works for any Shopify storefront
   regardless of theme. Fast, structured, no HTML parsing needed.
2. Static HTML crawl — for server-rendered non-Shopify sites. Fetches the
   homepage, discovers both direct product links AND collection/category
   links, then crawls one level into those collection pages too (so it
   doesn't stop at whatever happens to be linked from the homepage).
3. Headless-browser fallback (Playwright) — only triggered when tiers 1+2
   together find fewer than MIN_PRODUCTS_FOR_SUCCESS products. This catches
   JavaScript-rendered SPA sites (React/Angular/Vue storefronts) where the
   raw HTML has no real content until JS executes. Runs a single headless
   Chromium instance, bounded concurrency via a semaphore over browser
   pages/contexts. This is safe on Hugging Face Spaces free tier (2 vCPU /
   16GB RAM) — the RAM problems in earlier iterations of this project came
   from combining Playwright with CLIP/torch/FAISS on a 512MB Render
   instance; neither of those is used anymore.

Designed to run:
  - Standalone in a Colab cell (call `scrape_site_sync(url)`)
  - Inside a FastAPI async endpoint (call `await scrape_site(url)`)

Colab setup:
    !pip install aiohttp beautifulsoup4 lxml playwright -q
    !playwright install --with-deps chromium
"""

import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 35
MAX_PRODUCTS = 200
MAX_CONCURRENT_REQUESTS = 8
MAX_CONCURRENT_BROWSER_PAGES = 4
MIN_PRODUCTS_FOR_SUCCESS = 5          # below this, tier 3 (browser) kicks in
MAX_COLLECTION_PAGES_TO_CRAWL = 6     # tier 2 depth-2 crawl cap
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}

POLICY_KEYWORDS = {
    "return_policy": ["return", "refund", "exchange"],
    "shipping_policy": ["shipping", "delivery"],
    "faq": ["faq", "help", "support"],
    "terms": ["terms", "condition", "privacy"],
}

PRODUCT_LINK_HINTS = ["/product", "/products", "/item", "/p/"]
COLLECTION_LINK_HINTS = ["/collections", "/collection", "/category", "/categories", "/shop", "/catalog"]

PRICE_REGEX = re.compile(r"[\d,]+\.?\d*")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def scrape_site_sync(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Synchronous wrapper for Colab cells / quick testing."""
    return asyncio.run(scrape_site(url, timeout=timeout))


async def scrape_site(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Main entry point. Tries Shopify -> static HTML crawl -> headless browser."""
    start = time.time()
    url = _normalize_base_url(url)

    async with aiohttp.ClientSession(headers=HEADERS, timeout=REQUEST_TIMEOUT) as session:
        products: list = []
        policies = _empty_policies()
        method = "shopify_json"

        # ---- Tier 1: Shopify fast-path ----
        try:
            shopify_result = await asyncio.wait_for(
                _try_shopify(url, session), timeout=max(3, timeout * 0.25)
            )
        except (asyncio.TimeoutError, Exception):
            shopify_result = None

        if shopify_result and shopify_result["products"]:
            products = shopify_result["products"]
            try:
                policies = await asyncio.wait_for(
                    _scrape_policies(url, session), timeout=max(3, timeout * 0.2)
                )
            except (asyncio.TimeoutError, Exception):
                pass

        # ---- Tier 2: static HTML crawl (if Shopify found nothing) ----
        if not products:
            method = "html_fallback"
            elapsed = time.time() - start
            remaining = max(5, timeout * 0.55 - elapsed)
            try:
                html_result = await asyncio.wait_for(
                    _html_fallback_scrape(url, session), timeout=remaining
                )
                products = html_result["products"]
                policies = html_result["policies"]
            except (asyncio.TimeoutError, Exception):
                pass

        # ---- Tier 3: headless browser (if still too few products) ----
        if len(products) < MIN_PRODUCTS_FOR_SUCCESS:
            elapsed = time.time() - start
            remaining = max(8, timeout - elapsed)
            try:
                browser_result = await asyncio.wait_for(
                    _browser_fallback_scrape(url), timeout=remaining
                )
                if len(browser_result["products"]) > len(products):
                    products = browser_result["products"]
                    method = "browser_fallback"
                if not any(policies.values()) and any(browser_result["policies"].values()):
                    policies = browser_result["policies"]
            except (asyncio.TimeoutError, Exception):
                pass

        return {
            "site_url": url,
            "products": products[:MAX_PRODUCTS],
            "policies": policies,
            "meta": {
                "method": method,
                "product_count": len(products[:MAX_PRODUCTS]),
                "elapsed_seconds": round(time.time() - start, 1),
            },
        }


# ---------------------------------------------------------------------------
# Tier 1: Shopify fast-path
# ---------------------------------------------------------------------------

async def _try_shopify(base_url: str, session: aiohttp.ClientSession) -> dict | None:
    products = []
    page = 1
    while len(products) < MAX_PRODUCTS:
        endpoint = f"{base_url}/products.json?limit=250&page={page}"
        try:
            async with session.get(endpoint) as resp:
                if resp.status != 200:
                    break
                data = await resp.json(content_type=None)
        except Exception:
            break

        batch = data.get("products", [])
        if not batch:
            break

        for p in batch:
            variant = (p.get("variants") or [{}])[0]
            image = (p.get("images") or [{}])[0].get("src") if p.get("images") else None
            products.append({
                "id": str(p.get("id")),
                "title": p.get("title", ""),
                "price": _safe_float(variant.get("price")),
                "currency": None,
                "image_url": image,
                "product_url": f"{base_url}/products/{p.get('handle')}",
                "description": _strip_html(p.get("body_html", ""))[:500],
            })

        if len(batch) < 250:
            break
        page += 1

    if not products:
        return None
    return {"site_url": base_url, "products": products[:MAX_PRODUCTS]}


# ---------------------------------------------------------------------------
# Tier 2: static HTML crawl (homepage + one level into collection pages)
# ---------------------------------------------------------------------------

async def _html_fallback_scrape(base_url: str, session: aiohttp.ClientSession) -> dict:
    homepage_html = await _fetch_text(session, base_url)
    if homepage_html is None:
        return {"site_url": base_url, "products": [], "policies": _empty_policies()}

    soup = BeautifulSoup(homepage_html, "html.parser")
    all_links = _extract_links(soup, base_url)

    direct_product_links = {l for l in all_links if any(h in l.lower() for h in PRODUCT_LINK_HINTS)}
    collection_links = list({l for l in all_links if any(h in l.lower() for h in COLLECTION_LINK_HINTS)})[:MAX_COLLECTION_PAGES_TO_CRAWL]

    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    # crawl one level into collection pages to discover more product links
    collection_htmls = await asyncio.gather(
        *[_fetch_text_bounded(session, link, sem) for link in collection_links],
        return_exceptions=True,
    )
    for html in collection_htmls:
        if isinstance(html, str):
            csoup = BeautifulSoup(html, "html.parser")
            clinks = _extract_links(csoup, base_url)
            direct_product_links |= {l for l in clinks if any(h in l.lower() for h in PRODUCT_LINK_HINTS)}

    product_links = list(direct_product_links)[:MAX_PRODUCTS]
    policy_links = _classify_policy_links(all_links)

    products_results = await asyncio.gather(
        *[_fetch_and_parse_product(session, link, sem) for link in product_links],
        return_exceptions=True,
    )
    policies = await _scrape_policies_from_links(session, policy_links, sem)
    products = [p for p in products_results if isinstance(p, dict) and p]

    return {"site_url": base_url, "products": products, "policies": policies}


async def _fetch_and_parse_product(session, url, sem) -> dict | None:
    html = await _fetch_text_bounded(session, url, sem)
    if html is None:
        return None
    return _parse_product_html(html, url)


def _parse_product_html(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    ld_product = _parse_json_ld_product(soup, url)
    if ld_product:
        return ld_product

    title = _meta_content(soup, "og:title") or (soup.title.string if soup.title else "")
    image = _meta_content(soup, "og:image")
    price = _heuristic_price(soup)
    description = _meta_content(soup, "og:description") or ""

    if not title:
        return None

    return {
        "id": url,
        "title": title.strip(),
        "price": price,
        "currency": None,
        "image_url": image,
        "product_url": url,
        "description": description[:500],
    }


def _parse_json_ld_product(soup: BeautifulSoup, url: str) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            if entry.get("@type") not in ("Product", ["Product"]):
                continue
            offers = entry.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = _safe_float(offers.get("price")) if isinstance(offers, dict) else None
            currency = offers.get("priceCurrency") if isinstance(offers, dict) else None
            image = entry.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            return {
                "id": entry.get("sku") or url,
                "title": entry.get("name", ""),
                "price": price,
                "currency": currency,
                "image_url": image,
                "product_url": url,
                "description": (entry.get("description") or "")[:500],
            }
    return None


# ---------------------------------------------------------------------------
# Tier 3: headless-browser fallback (Playwright) for JS-rendered sites
# ---------------------------------------------------------------------------

async def _browser_fallback_scrape(base_url: str) -> dict:
    from playwright.async_api import async_playwright

    products: list = []
    policies = _empty_policies()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        try:
            context = await browser.new_context(user_agent=USER_AGENT)
            home_page = await context.new_page()
            await home_page.goto(base_url, wait_until="networkidle", timeout=15000)
            homepage_html = await home_page.content()
            await home_page.close()

            soup = BeautifulSoup(homepage_html, "html.parser")
            all_links = _extract_links(soup, base_url)
            direct_product_links = {l for l in all_links if any(h in l.lower() for h in PRODUCT_LINK_HINTS)}
            collection_links = list({l for l in all_links if any(h in l.lower() for h in COLLECTION_LINK_HINTS)})[:MAX_COLLECTION_PAGES_TO_CRAWL]
            policy_links = _classify_policy_links(all_links)

            sem = asyncio.Semaphore(MAX_CONCURRENT_BROWSER_PAGES)

            # render collection pages to discover more product links
            collection_htmls = await asyncio.gather(
                *[_render_page(context, link, sem) for link in collection_links],
                return_exceptions=True,
            )
            for html in collection_htmls:
                if isinstance(html, str):
                    csoup = BeautifulSoup(html, "html.parser")
                    clinks = _extract_links(csoup, base_url)
                    direct_product_links |= {l for l in clinks if any(h in l.lower() for h in PRODUCT_LINK_HINTS)}

            product_links = list(direct_product_links)[:MAX_PRODUCTS]

            product_htmls = await asyncio.gather(
                *[_render_page(context, link, sem) for link in product_links],
                return_exceptions=True,
            )
            for html, link in zip(product_htmls, product_links):
                if isinstance(html, str):
                    parsed = _parse_product_html(html, link)
                    if parsed:
                        products.append(parsed)

            # render policy pages (bounded)
            for category, link in policy_links.items():
                html = await _render_page(context, link, sem)
                if html:
                    psoup = BeautifulSoup(html, "html.parser")
                    policies[category] = psoup.get_text(separator=" ", strip=True)[:3000]

        finally:
            await browser.close()

    return {"site_url": base_url, "products": products, "policies": policies}


async def _render_page(context, url: str, sem: asyncio.Semaphore) -> str | None:
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=12000)
            return await page.content()
        except Exception:
            return None
        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Policy scraping (static tier)
# ---------------------------------------------------------------------------

async def _scrape_policies(base_url: str, session: aiohttp.ClientSession) -> dict:
    homepage_html = await _fetch_text(session, base_url)
    if homepage_html is None:
        return _empty_policies()
    soup = BeautifulSoup(homepage_html, "html.parser")
    all_links = _extract_links(soup, base_url)
    policy_links = _classify_policy_links(all_links)
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return await _scrape_policies_from_links(session, policy_links, sem)


async def _scrape_policies_from_links(session, policy_links: dict, sem) -> dict:
    policies = _empty_policies()
    for category, url in policy_links.items():
        html = await _fetch_text_bounded(session, url, sem)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            policies[category] = soup.get_text(separator=" ", strip=True)[:3000]
    return policies


def _classify_policy_links(all_links: set) -> dict:
    result = {}
    for link in all_links:
        lower = link.lower()
        for category, keywords in POLICY_KEYWORDS.items():
            if category in result:
                continue
            if any(kw in lower for kw in keywords):
                result[category] = link
    return result


def _empty_policies() -> dict:
    return {"return_policy": "", "shipping_policy": "", "faq": "", "terms": "", "other": ""}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.text()
    except Exception:
        return None


async def _fetch_text_bounded(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> str | None:
    async with sem:
        return await _fetch_text(session, url)


def _extract_links(soup: BeautifulSoup, base_url: str) -> set:
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        if urlparse(full).netloc == urlparse(base_url).netloc:
            links.add(full.split("#")[0])
    return links


def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return tag["content"] if tag and tag.has_attr("content") else None


def _heuristic_price(soup: BeautifulSoup) -> float | None:
    for selector in [".price", "[class*=price]", "[itemprop=price]"]:
        el = soup.select_one(selector)
        if el:
            match = PRICE_REGEX.search(el.get_text())
            if match:
                return _safe_float(match.group().replace(",", ""))
    return None


def _safe_float(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator=" ", strip=True)


def _normalize_base_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


# ---------------------------------------------------------------------------
# Quick manual test (Colab)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_url = "https://www.tanishq.co.in"
    data = scrape_site_sync(test_url)
    print(f"Method: {data['meta']['method']}, Products found: {data['meta']['product_count']}")
    print(json.dumps(data["products"][:2], indent=2))
