"""Price extraction ladder for competitor product pages. Structured sources
first (Shopify product JSON, schema.org JSON-LD), generic markup after, so
most SMB storefronts never need the LLM fallback."""

import json
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from dispatchkit import netsafe

TIMEOUT = 10
MAX_REDIRECTS = 5


def _get(url: str):
    for _ in range(MAX_REDIRECTS + 1):
        netsafe.assert_public_url(url)
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False)
        if resp.is_redirect or resp.is_permanent_redirect:
            location = resp.headers.get("Location", "")
            if not location:
                return resp
            url = urljoin(url, location)
            continue
        return resp
    raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DispatcherPriceWatch/1.0; +https://dispatcher.build)"}
SHOPIFY_RE = re.compile(r"/products/([\w-]+)")
NUMBER_RE = re.compile(r"\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")
SYMBOL_CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP"}
PAGE_TEXT_CHARS = 6000

RUNGS = ["shopify", "jsonld", "meta", "regex"]


class ExtractError(Exception):
    """The page itself could not be fetched; no rung can succeed."""


def _to_float(raw: str):
    raw = str(raw).strip().replace(" ", "")
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        head, _, tail = raw.rpartition(",")
        raw = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else raw.replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 < value < 10_000_000 else None


def try_shopify(url: str):
    match = SHOPIFY_RE.search(urlsplit(url).path)
    if not match:
        return None
    parts = urlsplit(url)
    endpoint = f"{parts.scheme}://{parts.netloc}/products/{match.group(1)}.json"
    resp = _get(endpoint)
    if resp.status_code != 200:
        return None
    variants = (resp.json().get("product") or {}).get("variants") or []
    prices = [_to_float(v.get("price")) for v in variants]
    prices = [p for p in prices if p is not None]
    if not prices:
        return None
    return {"price": min(prices), "currency": ""}


def _product_nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _product_nodes(item)
        return
    if not isinstance(data, dict):
        return
    node_type = data.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if "Product" in types:
        yield data
    for key in ("@graph", "itemListElement", "item"):
        if key in data:
            yield from _product_nodes(data[key])


def try_jsonld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _product_nodes(data):
            offers = node.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers]
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                price = _to_float(offer.get("price") or offer.get("lowPrice") or "")
                if price is not None:
                    return {"price": price, "currency": offer.get("priceCurrency", "")}
    return None


def try_meta(soup):
    selectors = [
        {"itemprop": "price"},
        {"property": "og:price:amount"},
        {"property": "product:price:amount"},
    ]
    for attrs in selectors:
        tag = soup.find("meta", attrs=attrs)
        if not tag:
            continue
        price = _to_float(tag.get("content", ""))
        if price is None:
            continue
        currency = ""
        for cur_attrs in ({"itemprop": "priceCurrency"}, {"property": "og:price:currency"},
                          {"property": "product:price:currency"}):
            cur = soup.find("meta", attrs=cur_attrs)
            if cur and cur.get("content"):
                currency = cur["content"]
                break
        return {"price": price, "currency": currency}
    return None


def try_regex(soup):
    nodes = soup.find_all(class_=re.compile(r"price", re.I), limit=10)
    for node in nodes:
        text = node.get_text(" ", strip=True)[:120]
        symbol_match = re.search(r"([$€£])\s*(" + NUMBER_RE.pattern + r")", text)
        if not symbol_match:
            continue
        price = _to_float(symbol_match.group(2))
        if price is not None:
            return {"price": price, "currency": SYMBOL_CURRENCIES.get(symbol_match.group(1), "")}
    return None


def extract(url: str, preferred: str = None) -> tuple:
    """Run the ladder. Returns (result_or_None, page_text) where result is
    {"price", "currency", "method"}; page_text feeds the LLM fallback when
    every structured rung failed. Raises ExtractError if the page is
    unreachable."""
    rungs = list(RUNGS)
    if preferred in rungs:
        rungs.remove(preferred)
        rungs.insert(0, preferred)

    soup = None
    fetched = False
    for rung in rungs:
        try:
            if rung == "shopify":
                result = try_shopify(url)
            else:
                if not fetched:
                    try:
                        resp = _get(url)
                    except (requests.RequestException, netsafe.UnsafeUrl) as e:
                        raise ExtractError(str(e))
                    if resp.status_code != 200:
                        raise ExtractError(f"HTTP {resp.status_code}")
                    soup = BeautifulSoup(resp.text, "lxml")
                    fetched = True
                result = {"jsonld": try_jsonld, "meta": try_meta, "regex": try_regex}[rung](soup)
        except ExtractError:
            raise
        except Exception:
            result = None
        if result:
            result["method"] = rung
            return result, ""

    text = soup.get_text(" ", strip=True)[:PAGE_TEXT_CHARS] if soup else ""
    return None, text
