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
import logging
import re
import ssl
import time
from urllib.parse import urljoin, urlparse

import aiohttp
import certifi
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 150
MAX_PRODUCTS = 5000
SHOPIFY_PAGE_SIZE = 250               # Shopify's hard max for /products.json
SHOPIFY_PAGES_IN_FLIGHT = 5           # fetch this many catalogue pages concurrently
MAX_CONCURRENT_REQUESTS = 8
MAX_CONCURRENT_BROWSER_PAGES = 4
MIN_PRODUCTS_FOR_SUCCESS = 5          # below this, tier 3 (browser) kicks in
MAX_COLLECTION_PAGES_TO_CRAWL = 6     # tier 2 depth-2 crawl cap
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)

# Storefronts behind Shopify/Cloudflare bot protection throttle bursts of requests
# with a 5xx or 429 rather than a hard block, and recover within seconds. Treating
# those as "this site has no products" is wrong -- back off and retry instead.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 1.0

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Sending a User-Agent and nothing else is itself a bot signal: real Chrome always
# sends Accept, Accept-Language and the Sec-Fetch-* set. Cloudflare/Shopify weight
# that header fingerprint heavily, and they are far stricter with datacentre IPs
# (Render/AWS/GCP) than with home broadband -- which is exactly why giva.co returns
# products locally but 403s from Render.
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # Deliberately NOT setting Accept-Encoding: aiohttp advertises exactly the codecs
    # it can actually decode. Claiming "br" here makes servers send Brotli, which
    # aiohttp cannot decompress without the optional brotli package -- every response
    # then dies with ClientResponseError.
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# Python builds from python.org (and slim Docker images) ship without a CA bundle
# wired into ssl's default context, so every HTTPS handshake fails with
# CERTIFICATE_VERIFY_FAILED. Point at certifi's bundle explicitly.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _new_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=SSL_CONTEXT, limit=MAX_CONCURRENT_REQUESTS * 2)

POLICY_KEYWORDS = {
    "return_policy": ["return", "refund", "exchange"],
    "shipping_policy": ["shipping", "delivery"],
    "faq": ["faq", "help", "support"],
    "terms": ["terms", "condition", "privacy"],
}

PRODUCT_LINK_HINTS = ["/product", "/products", "/item", "/p/"]
COLLECTION_LINK_HINTS = ["/collections", "/collection", "/category", "/categories", "/shop", "/catalog"]

# --- Sitemap tier -----------------------------------------------------------
# Product URLs vary wildly per platform (/products/x on Shopify, /rings/x~433.html
# on BlueStone), so hardcoded path hints will never generalise. Sitemaps are a web
# standard every serious storefront publishes, so we read those instead of guessing.
SITEMAP_FALLBACK_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"]
MAX_SITEMAP_PRODUCTS = 400            # product pages we're willing to fetch one-by-one
MAX_CHILD_SITEMAPS = 8
MAX_SITEMAP_DEPTH = 2
MAX_CONCURRENT_PDP_FETCHES = 24

# Pages that live in a sitemap but are definitely not products.
NON_PRODUCT_URL_HINTS = [
    "/blog", "/about", "/contact", "/policy", "/policies", "/faq", "/help",
    "/careers", "/store", "/stores", "/login", "/cart", "/account", "/terms",
    "/privacy", "/collections", "/collection", "/category", "/categories",
    "/gift-card", "/search", "/pages/",
]

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

    async with aiohttp.ClientSession(
        headers=HEADERS, timeout=REQUEST_TIMEOUT, connector=_new_connector()
    ) as session:
        products: list = []
        policies = _empty_policies()
        method = "shopify_json"

        # ---- Tier 1: Shopify fast-path ----
        try:
            shopify_result = await asyncio.wait_for(
                _try_shopify(url, session), timeout=max(20, timeout * 0.6)
            )
        except Exception:
            log.warning("tier 1 (shopify) failed for %s", url, exc_info=True)
            shopify_result = None

        if shopify_result and shopify_result["products"]:
            products = shopify_result["products"]
            try:
                policies = await asyncio.wait_for(
                    _scrape_policies(url, session), timeout=max(3, timeout * 0.2)
                )
            except Exception:
                log.warning("policy scrape failed for %s", url, exc_info=True)

        # ---- Tier 2: sitemap crawl (the universal path for non-Shopify stores) ----
        if not products:
            method = "sitemap"
            deadline = start + timeout * 0.55
            try:
                # Deadline-aware internally, so it returns partial results rather than
                # being cancelled empty-handed. The wait_for is only a backstop.
                sitemap_result = await asyncio.wait_for(
                    _sitemap_scrape(url, session, deadline),
                    timeout=max(20, deadline - time.time() + 15),
                )
                products = sitemap_result["products"]
                policies = sitemap_result["policies"]
            except Exception:
                log.warning("tier 2 (sitemap) failed for %s", url, exc_info=True)

        # ---- Tier 3: static HTML crawl (if the sitemap gave us nothing) ----
        if not products:
            method = "html_fallback"
            elapsed = time.time() - start
            remaining = max(10, timeout * 0.65 - elapsed)
            try:
                html_result = await asyncio.wait_for(
                    _html_fallback_scrape(url, session), timeout=remaining
                )
                products = html_result["products"]
                policies = html_result["policies"]
            except Exception:
                log.warning("tier 3 (html) failed for %s", url, exc_info=True)

        # ---- Tier 4: headless browser (if still too few products) ----
        # Needs a real floor: this is the last resort for JS-only storefronts, and
        # rendering pages is slow. Handing it 8 leftover seconds guarantees failure.
        if len(products) < MIN_PRODUCTS_FOR_SUCCESS:
            elapsed = time.time() - start
            remaining = max(50, timeout - elapsed)
            try:
                browser_result = await asyncio.wait_for(
                    _browser_fallback_scrape(url), timeout=remaining
                )
                if len(browser_result["products"]) > len(products):
                    products = browser_result["products"]
                    method = "browser_fallback"
                if not any(policies.values()) and any(browser_result["policies"].values()):
                    policies = browser_result["policies"]
            except Exception:
                log.warning("tier 4 (browser) failed for %s", url, exc_info=True)

        meta = {
            "method": method,
            "product_count": len(products[:MAX_PRODUCTS]),
            "elapsed_seconds": round(time.time() - start, 1),
        }
        if not products:
            meta["failure_reason"] = await _diagnose_failure(url, session)

        return {
            "site_url": url,
            "products": products[:MAX_PRODUCTS],
            "policies": policies,
            "meta": meta,
        }


async def _diagnose_failure(base_url: str, session: aiohttp.ClientSession) -> str:
    """
    Called only when every tier came back empty. "No products found" and "the site
    returned 403 to every request" are completely different problems, and reporting
    the first when it was really the second sends people hunting the wrong bug.
    """
    try:
        async with session.get(base_url) as resp:
            status = resp.status
    except Exception as exc:
        return f"unreachable ({type(exc).__name__})"

    if status in (401, 403) or status == 429:
        return "blocked"
    if status in RETRY_STATUSES:
        return "throttled"
    if status != 200:
        return f"http_{status}"
    return "no_products"


# ---------------------------------------------------------------------------
# Tier 1: Shopify fast-path
# ---------------------------------------------------------------------------

async def _try_shopify(base_url: str, session: aiohttp.ClientSession) -> dict | None:
    """
    Walk the whole /products.json catalogue, not just the first page. Pages are
    fetched in small concurrent batches -- a 3000-product store is 12 sequential
    round trips (~25s) but only 3 batched ones (~6s).
    """
    currency = await _shopify_currency(base_url, session)

    products: list = []
    next_page = 1
    exhausted = False

    while not exhausted and len(products) < MAX_PRODUCTS:
        page_numbers = range(next_page, next_page + SHOPIFY_PAGES_IN_FLIGHT)
        batches = await asyncio.gather(*[
            _get(session, f"{base_url}/products.json?limit={SHOPIFY_PAGE_SIZE}&page={n}", as_json=True)
            for n in page_numbers
        ])

        for data in batches:
            batch = data.get("products", []) if isinstance(data, dict) else []
            if not batch:
                exhausted = True
                break

            products.extend(_normalize_shopify_product(p, base_url, currency) for p in batch)

            if len(batch) < SHOPIFY_PAGE_SIZE:   # short page == last page
                exhausted = True
                break

        next_page += SHOPIFY_PAGES_IN_FLIGHT

    if not products:
        return None

    log.info("shopify: %d products from %s", len(products), base_url)
    return {"site_url": base_url, "products": products[:MAX_PRODUCTS]}


def _normalize_shopify_product(p: dict, base_url: str, currency: str | None) -> dict:
    variant = (p.get("variants") or [{}])[0]
    images = p.get("images") or []
    return {
        "id": str(p.get("id")),
        "title": p.get("title", ""),
        "price": _safe_float(variant.get("price")),
        "currency": currency,
        "image_url": images[0].get("src") if images else None,
        "product_url": f"{base_url}/products/{p.get('handle')}",
        "description": _strip_html(p.get("body_html", ""))[:500],
        "product_type": p.get("product_type", ""),
        "tags": p.get("tags", []),
    }


async def _shopify_currency(base_url: str, session: aiohttp.ClientSession) -> str | None:
    """Shopify exposes the storefront currency on /cart.js. Best-effort only."""
    data = await _get(session, f"{base_url}/cart.js", as_json=True)
    return data.get("currency") if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Tier 2: sitemap crawl — the universal path for non-Shopify storefronts
# ---------------------------------------------------------------------------

async def _sitemap_scrape(base_url: str, session: aiohttp.ClientSession, deadline: float) -> dict:
    """
    Fetch product pages in chunks against a wall-clock deadline, keeping whatever
    completed. Some storefronts serve 700KB+ product pages; wrapping the whole thing
    in one wait_for means a slow site times out and we bin hundreds of good products
    to report "no products found". Partial results beat nothing.
    """
    sem = asyncio.Semaphore(MAX_CONCURRENT_PDP_FETCHES)

    sitemaps = await _discover_sitemaps(base_url, session)
    if not sitemaps:
        return {"site_url": base_url, "products": [], "policies": _empty_policies()}

    candidates: list[str] = []
    for sitemap in sitemaps:
        candidates.extend(await _collect_sitemap_locs(session, sitemap, sem))
        if len(candidates) >= MAX_SITEMAP_PRODUCTS * 4 or time.time() > deadline:
            break

    product_urls = _filter_product_urls(candidates, base_url)[:MAX_SITEMAP_PRODUCTS]
    if not product_urls:
        return {"site_url": base_url, "products": [], "policies": _empty_policies()}

    log.info("sitemap: %d candidate product pages on %s", len(product_urls), base_url)

    products: list = []
    chunk_size = MAX_CONCURRENT_PDP_FETCHES * 2
    for i in range(0, len(product_urls), chunk_size):
        if time.time() > deadline:
            log.info("sitemap: deadline reached, keeping %d products", len(products))
            break
        chunk = product_urls[i:i + chunk_size]
        parsed = await asyncio.gather(
            *[_fetch_and_parse_product(session, u, sem, strict=True) for u in chunk],
            return_exceptions=True,
        )
        products.extend(p for p in parsed if isinstance(p, dict) and p)

    log.info("sitemap: %d products from %s", len(products), base_url)

    policies = _empty_policies()
    if time.time() < deadline:
        try:
            policies = await _scrape_policies(base_url, session)
        except Exception:
            log.warning("policy scrape failed for %s", base_url, exc_info=True)

    return {"site_url": base_url, "products": products, "policies": policies}


async def _discover_sitemaps(base_url: str, session: aiohttp.ClientSession) -> list[str]:
    """robots.txt is the authoritative list; fall back to conventional paths."""
    found: list[str] = []

    robots = await _get(session, f"{base_url}/robots.txt")
    if robots:
        found.extend(re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots))

    found.extend(urljoin(base_url, path) for path in SITEMAP_FALLBACK_PATHS)

    # Product-specific sitemaps first — on a big store the generic index may be
    # mostly editorial pages, and we only have budget for a few hundred fetches.
    ordered = sorted(dict.fromkeys(found), key=lambda u: 0 if "product" in u.lower() else 1)
    return ordered


async def _collect_sitemap_locs(
    session: aiohttp.ClientSession, sitemap_url: str, sem: asyncio.Semaphore, depth: int = 0
) -> list[str]:
    """Return <loc> entries, recursing when the sitemap is an index of sitemaps."""
    if depth > MAX_SITEMAP_DEPTH:
        return []

    xml = await _fetch_text_bounded(session, sitemap_url, sem)
    if not xml:
        return []

    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    if not locs:
        return []

    if "<sitemapindex" in xml[:1000].lower():
        children = [l for l in locs if "product" in l.lower()] or locs
        nested = await asyncio.gather(
            *[_collect_sitemap_locs(session, c, sem, depth + 1) for c in children[:MAX_CHILD_SITEMAPS]],
            return_exceptions=True,
        )
        return [u for group in nested if isinstance(group, list) for u in group]

    return locs


def _filter_product_urls(urls: list[str], base_url: str) -> list[str]:
    host = urlparse(base_url).netloc
    root = base_url.rstrip("/").lower()

    keep, seen = [], set()
    for url in urls:
        lowered = url.lower()
        if url in seen or urlparse(url).netloc != host:
            continue
        if lowered.rstrip("/") == root:
            continue
        if any(hint in lowered for hint in NON_PRODUCT_URL_HINTS):
            continue
        seen.add(url)
        keep.append(url)
    return keep


# ---------------------------------------------------------------------------
# Tier 3: static HTML crawl (homepage + one level into collection pages)
# ---------------------------------------------------------------------------

async def _html_fallback_scrape(base_url: str, session: aiohttp.ClientSession) -> dict:
    homepage_html = await _fetch_text(session, base_url)
    if homepage_html is None:
        return {"site_url": base_url, "products": [], "policies": _empty_policies()}

    soup = _soup(homepage_html)
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
            csoup = _soup(html)
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


def _soup(html: str) -> BeautifulSoup:
    """lxml is a C parser and roughly 10x faster than the pure-Python html.parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


async def _fetch_and_parse_product(session, url, sem, strict: bool = False) -> dict | None:
    html = await _fetch_text_bounded(session, url, sem)
    if html is None:
        return None
    # Parsing is synchronous CPU work, and product pages run to hundreds of KB. Doing
    # it inline blocks the event loop -- which stalls every other in-flight fetch AND
    # stops asyncio timeouts from firing, so slow sites overran their deadline badly.
    return await asyncio.to_thread(_parse_product_html, html, url, strict)


def _parse_product_html(html: str, url: str, strict: bool = False) -> dict | None:
    """
    strict=True means "only accept this page if it is really a product page".
    Sitemaps mix in category and editorial pages, and those have an og:title too --
    without this, a /rings category page gets imported as a product called "Rings".
    A real product page carries JSON-LD Product, or at minimum a price.
    """
    soup = _soup(html)

    ld_product = _parse_json_ld_product(soup, url)
    if ld_product:
        return ld_product

    title = _meta_content(soup, "og:title") or (soup.title.string if soup.title else "")
    image = _meta_content(soup, "og:image")
    price = _heuristic_price(soup, html)
    description = _meta_content(soup, "og:description") or ""

    if not title:
        return None
    if strict and price is None:
        return None

    return {
        "id": url,
        "title": title.strip(),
        "price": price,
        "currency": None,
        "image_url": image,
        "product_url": url,
        "description": description[:500],
        "product_type": "",
        "tags": [],
    }


def _parse_json_ld_product(soup: BeautifulSoup, url: str) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        # Yoast/WordPress and many CMSes nest everything under @graph.
        expanded = []
        for entry in candidates:
            if isinstance(entry, dict) and isinstance(entry.get("@graph"), list):
                expanded.extend(entry["@graph"])
            else:
                expanded.append(entry)

        for entry in expanded:
            if not isinstance(entry, dict):
                continue
            types = entry.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
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
            # NOT networkidle: real storefronts run analytics/chat/pixel scripts that keep
            # the network permanently busy, so networkidle never fires and every goto()
            # burns its full timeout. domcontentloaded + a settle delay is what works.
            await home_page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
            await home_page.wait_for_timeout(2500)
            homepage_html = await home_page.content()
            await home_page.close()

            soup = _soup(homepage_html)
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
                    csoup = _soup(html)
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
                    psoup = _soup(html)
                    policies[category] = psoup.get_text(separator=" ", strip=True)[:3000]

        finally:
            await browser.close()

    return {"site_url": base_url, "products": products, "policies": policies}


async def _render_page(context, url: str, sem: asyncio.Semaphore) -> str | None:
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)  # let client-side rendering settle
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
    soup = _soup(homepage_html)
    all_links = _extract_links(soup, base_url)
    policy_links = _classify_policy_links(all_links)
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return await _scrape_policies_from_links(session, policy_links, sem)


async def _scrape_policies_from_links(session, policy_links: dict, sem) -> dict:
    policies = _empty_policies()
    for category, url in policy_links.items():
        html = await _fetch_text_bounded(session, url, sem)
        if html:
            soup = _soup(html)
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

async def _get(session: aiohttp.ClientSession, url: str, as_json: bool = False):
    """GET with backoff on throttling responses. Returns parsed body, or None."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with session.get(url) as resp:
                if resp.status in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    log.info("%s -> HTTP %s, retrying in %.1fs", url, resp.status, backoff)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                if resp.status != 200:
                    log.warning("%s -> HTTP %s, giving up", url, resp.status)
                    return None
                return await (resp.json(content_type=None) if as_json else resp.text())
        except Exception:
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            log.warning("GET %s failed", url, exc_info=True)
            return None
    return None


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str | None:
    return await _get(session, url)


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


# Many storefronts (Magento, and anything React/Vue) render the price client-side, so
# the CSS selectors below match an empty node. The number is almost always still in the
# page -- as a meta tag, a data- attribute, or embedded JSON the JS hydrates from.
_PRICE_ATTR_PATTERNS = [
    re.compile(r'property=["\'](?:product|og):price:amount["\'][^>]*content=["\']([\d.,]+)', re.I),
    re.compile(r'itemprop=["\']price["\'][^>]*content=["\']([\d.,]+)', re.I),
    re.compile(r'data-price-amount=["\']([\d.]+)', re.I),
    re.compile(r'"final_price"\s*:\s*"?([\d.]+)', re.I),
    re.compile(r'"(?:price|priceAmount|salePrice)"\s*:\s*"?([\d.]+)"?', re.I),
]


def _heuristic_price(soup: BeautifulSoup, html: str = "") -> float | None:
    for selector in [".price", "[class*=price]", "[itemprop=price]"]:
        el = soup.select_one(selector)
        if el:
            match = PRICE_REGEX.search(el.get_text())
            if match:
                price = _safe_float(match.group().replace(",", ""))
                if price:
                    return price

    for pattern in _PRICE_ATTR_PATTERNS:
        match = pattern.search(html)
        if match:
            price = _safe_float(match.group(1))
            if price:
                return price
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
