"""Resolve exact color references to the publishers' screen previews."""
import asyncio
import io
import re
from html import unescape
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from app.core.cache import TTLCache
from PIL import Image

RAL_PAGE = "https://www.ral-farben.de/en/all-ral-colours"
PANTONE_PAGE = "https://www.pantone.com/na/en-us/color-finder/"
PANTONE_ASSETS = "https://pantone-web-assets-dev.s3.us-east-2.amazonaws.com/magento/media/color-finder/img/chips/"
RAL_NUMBER = r"(?:\d{3} \d{2} \d{2}|\d{3}-[\dM]|\d{4})"
RAL_CODE = re.compile(
    rf"^RAL\s*(?:(?:Classic|Design(?: System Plus)?|Effect)\s+)?(?:colou?r\s*:?\s*)?({RAL_NUMBER})\b",
    re.I,
)
PANTONE_CODE = re.compile(
    r"^Pantone\s*(?:colou?r\s*:?\s*)?([\d]{2,4}(?:-\d{4})?|Warm Red|Rubine Red|Rhodamine Red|"
    r"Process (?:Blue|Magenta|Cyan|Yellow|Black)|Reflex Blue|Purple|Violet|Blue 072|"
    r"(?:Black|Yellow|Orange|Red|Green|Blue)(?: \d{1,3})?)\s*(TCX|TPG|TPX|CP|UP|C|U)\b", re.I,
)
RAL_SWATCH = re.compile(
    rf'<a\s+(?P<attrs>[^>]+)>\s*<span>RAL</span>\s*<span class="number">(?P<code>{RAL_NUMBER})</span>'
    r'(?:\s*<div class="subtext">[^<]*<br\s*/?>(?P<name>[^<]+)</div>)?', re.I,
)
HEX = re.compile(r"background-color\s*:\s*(#[0-9a-f]{6})\b", re.I)
URL_RAL = re.compile(r"(?:^|-)ral-?(\d{4})(?=-|$)", re.I)
URL_PANTONE = re.compile(r"(?:^|-)pantone-?(?:[a-z]+-){0,8}(\d{2,4})(?:-)?(cp|up|c|u)(?=-|$)", re.I)
HREF = re.compile(r'\bhref="([^"]+)"')
_previews = TTLCache()
_ral_page = TTLCache()
_ral_load = asyncio.Lock()


def normalize_reference(value: str) -> str | None:
    if match := RAL_CODE.match(value.strip()):
        return "RAL " + match[1].upper()
    if match := PANTONE_CODE.match(value.strip()):
        return f"Pantone {match[1].title()} {match[2].upper()}"
    return None


def references_in_product_url(url: str) -> list[str]:
    slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]).lower()
    return list(dict.fromkeys([
        *(f"RAL {match[1]}" for match in URL_RAL.finditer(slug)),
        *(f"Pantone {match[1]} {match[2].upper()}" for match in URL_PANTONE.finditer(slug)),
    ]))


def references_from(issues: list[dict], manufacturer_color_name: str | None) -> dict[str, dict]:
    references = {}
    for issue in issues:
        if issue.get("code") == "manufacturer_color_reference" and (code := normalize_reference(issue.get("name") or "")):
            references.setdefault(code, {
                "manufacturer_source": issue.get("url"),
                "from_product_url": bool(issue.get("from_product_url")),
            })
    if code := normalize_reference(manufacturer_color_name or ""):
        references.setdefault(code, {"manufacturer_source": None, "from_product_url": False})
    return references


async def read_asset(url: str) -> bytes:
    # Only fixed publisher URLs and normalized code paths reach this function.
    async with asyncio.timeout(8):
        async with httpx.AsyncClient(timeout=6, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": "FilaBrain"}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 2_000_000:
                        raise ValueError("Color reference asset is too large")
                    body.extend(chunk)
    return bytes(body)


async def _ral_preview(code: str) -> dict | None:
    page = _ral_page.get(RAL_PAGE)
    if page is None:
        async with _ral_load:
            page = _ral_page.get(RAL_PAGE)
            if page is None:
                page = (await read_asset(RAL_PAGE)).decode("utf-8")
                _ral_page.set(RAL_PAGE, page, ttl=3600)
    for match in RAL_SWATCH.finditer(page):
        if match["code"] != code.removeprefix("RAL "):
            continue
        color = HEX.search(match["attrs"])
        href = HREF.search(match["attrs"])
        url = urljoin(RAL_PAGE, href[1]) if href else ""
        if color and url.startswith("https://www.ral-farben.de/en/colour/ral-"):
            return {"name": unescape(match["name"]).strip() if match["name"] else code,
                    "hex_code": color[1].upper(), "source": url}
    return None


async def _pantone_preview(code: str) -> dict | None:
    number = code.removeprefix("Pantone ")
    slug = number.lower().replace(" ", "-")
    url = PANTONE_PAGE + slug
    image_url = PANTONE_ASSETS + f"pantone-color-chip-{slug}.webp"
    page = unquote(unescape((await read_asset(url)).decode("utf-8")))
    exact_asset = re.compile(re.escape(image_url) + r'(?=[?&"\s])')
    alt = next((match for tag in re.findall(r"<img\b[^>]+>", page, re.I)
                if exact_asset.search(tag) and (match := re.search(
                    rf'alt="PANTONE {re.escape(number)}([^"]{{0,100}}) Chip"', tag, re.I
                ))), None)
    if alt is None:
        return None
    with Image.open(io.BytesIO(await read_asset(image_url)), formats=["WEBP"]) as image:
        if max(image.size) > 2048:
            return None
        rgb = image.convert("RGB")
        width, height = rgb.size
        colors = {rgb.getpixel((width * x // 8, height * y // 8)) for x in (3, 4, 5) for y in (2, 3, 4)}
        if len(colors) != 1:
            return None
        red, green, blue = colors.pop()
    return {"name": alt[1].strip() or code, "hex_code": f"#{red:02X}{green:02X}{blue:02X}", "source": url}


async def resolve_preview(code: str) -> dict:
    if preview := _previews.get(code):
        return preview
    try:
        preview = await (_ral_preview(code) if code.startswith("RAL ") else _pantone_preview(code))
    except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError):
        preview = None
    if preview:
        _previews.purge_expired()
        _previews.set(code, preview, ttl=3600)
    return preview or {"name": code, "hex_code": None, "source": None}
