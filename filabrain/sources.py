import asyncio
import http.client
import ipaddress
import json
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from app.core.cache import TTLCache

OFD = "https://api.openfilamentdatabase.org"
DEFAULT_SUPPLIER_DOWNLOAD_MIB = 8
DEFAULT_SUPPLIER_DOWNLOAD_BYTES = DEFAULT_SUPPLIER_DOWNLOAD_MIB * 1024 * 1024
SUPPLIER_TOO_LARGE = "Supplier page is too large"
MAX_TEXT = 12_000
MAX_SEARCH_RESPONSE_BYTES = 512 * 1024
SHARED_SPEC = re.compile(r"\b(?:applies to all variants|same for all variants|all variants share)\b", re.IGNORECASE)
DATASHEET_LABEL = re.compile(
    r"(?<![^\W_])(?:technical[ _-]*data[ _-]*sheet|tds)(?![^\W_])", re.IGNORECASE
)
_supplier_cooldowns = TTLCache()
_product_sitemaps = TTLCache()


class SearchEnginesUnavailable(ValueError):
    pass


class SupplierRateLimited(ValueError):
    def __init__(self, host: str, retry_after: str = ""):
        super().__init__("HTTP 429 (rate limited)")
        self.host = host
        try:
            seconds = int(retry_after) if retry_after.isdecimal() else (
                parsedate_to_datetime(retry_after).timestamp() - time.time()
            )
        except (ValueError, OverflowError, OSError):
            seconds = 60
        self.seconds = max(1, min(31_536_000, round(seconds)))


def _supplier_url(value: str):
    if len(value) > 2048 or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in value):
        raise ValueError("Expected a public supplier HTTP or HTTPS URL")
    parsed = urlsplit(value)
    port = {"https": 443, "http": 80}.get(parsed.scheme)
    if (
        port is None or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.port not in (None, port)
    ):
        raise ValueError("Expected a public supplier HTTP or HTTPS URL")
    return parsed, port


def public_link(value) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 500
        or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in value)
    ):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme in ("https", "http")
            and url.hostname
            and url.username is None
            and url.password is None
            and url.port != 0
        ):
            return value
    except ValueError:
        pass
    return None


def supplier_page_url(url: str) -> str:
    parsed = urlsplit(url)
    query = "&".join(
        part for part in parsed.query.split("&")
        if not part.lower().startswith(("utm_", "_pos=", "_sid=", "_ss="))
    )
    return urlunsplit(parsed._replace(query=query, fragment=""))


@dataclass(frozen=True)
class Evidence:
    url: str
    title: str
    text: str
    product: dict | None = None
    manual: bool = False
    scopes: tuple[str, ...] | None = None
    shared: bool = False
    manufacturer_page: bool = False
    datasheet: bool = False
    datasheets: tuple[str, ...] = ()
    origin: str = ""
    archived_at: str = ""


class _ProductPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.words: list[str] = []
        self.title: list[str] = []
        self.products: list[dict] = []
        self.datasheets: list[str] = []
        self._pdf_link: str | None = None
        self._link_text: list[str] = []
        self._skip: str | None = None
        self._in_title = False
        self._structured: list[str] | None = None
        self.scopes: list[str] = []
        self.main_scopes: list[str] = []
        self._main_tag: str | None = None
        self._main_depth = 0
        self._row: list[str] | None = None
        self._section: list[str] = []
        self._section_depth = 0

    def handle_starttag(self, tag, attrs):
        if self._skip is None:
            if tag == self._main_tag:
                self._main_depth += 1
            elif self._main_tag is None and (tag == "main" or dict(attrs).get("role") == "main"):
                self._finish_section()
                self._main_tag = tag
                self._main_depth = 1
        if tag == "a" and self._skip is None:
            attributes = dict(attrs)
            href = (attributes.get("href") or "").strip()
            filename = attributes.get("download") or ""
            decoded_href = unquote(href)
            # Keep only PDF links. The supplier fetcher validates every URL and redirect.
            if href and (re.search(r"\.pdf(?:[?#]|$)", decoded_href, re.IGNORECASE) or filename.lower().endswith(".pdf")):
                self._pdf_link = href
                self._link_text = [attributes.get("aria-label") or "", attributes.get("title") or "", filename, decoded_href]
        if tag in ("script", "style", "noscript", "template"):
            self._skip = tag
            if tag == "script" and dict(attrs).get("type") == "application/ld+json":
                self._structured = []
        elif tag == "title" and not self.title:
            self._in_title = True
        elif self._skip is None and tag == "tr":
            self._row = []
        elif self._skip is None and self._row is None:
            if tag == "section":
                self._section_depth += 1
            if self._separates_scope(tag):
                self._finish_section()

    def handle_endtag(self, tag):
        if tag == "a" and self._pdf_link:
            if DATASHEET_LABEL.search(" ".join(self._link_text)):
                self.datasheets.append(self._pdf_link)
            self._pdf_link = None
        if tag == self._skip:
            self._skip = None
            if self._structured is not None:
                try:
                    self._add_products(json.loads("".join(self._structured)))
                except (ValueError, RecursionError):
                    pass
                self._structured = None
        elif tag == "title":
            self._in_title = False
        elif tag == "tr" and self._row is not None:
            self._save_scope(self._row)
            self._row = None
        elif self._skip is None and self._row is None:
            if tag in ("section", "div", "article", "li", "option") or tag == "p" and not self._section_depth:
                self._finish_section()
            if tag == "section":
                self._section_depth = max(0, self._section_depth - 1)
        if self._skip is None and tag == self._main_tag:
            if self._main_depth == 1:
                self._finish_section()
                self._main_tag = None
            self._main_depth -= 1

    def _separates_scope(self, tag):
        return tag in (
            "h1", "h2", "h3", "h4", "section", "div", "article", "li", "option"
        ) or tag == "p" and not self._section_depth

    def _save_scope(self, parts):
        text = " ".join(" ".join(parts).split())
        self.scopes.append(text)
        if self._main_tag is not None:
            self.main_scopes.append(text)

    def _finish_section(self):
        if self._section:
            self._save_scope(self._section)
            self._section.clear()

    def close(self):
        super().close()
        self._finish_section()

    def _add_products(self, value):
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("@type") == "Product":
                brand = row.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name")
                image = row.get("image")
                if isinstance(image, list):
                    image = next((item for item in image if isinstance(item, str)), None)
                product = {
                    "manufacturer": brand,
                    "designation": row.get("name"),
                    "sku": row.get("sku"),
                    "color": row.get("color"),
                    "material": row.get("material"),
                    "finish_type": row.get("finish"),
                }
                self.products.append(
                    {
                        k: v.strip()[:255]
                        for k, v in product.items()
                        if isinstance(v, str) and v.strip()
                    }
                )
                if isinstance(image, str) and len(image) <= 500:
                    self.products[-1]["image_url"] = image
            if isinstance(row.get("@graph"), list):
                self._add_products(row["@graph"])
            if isinstance(row.get("hasVariant"), list):
                self._add_products(row["hasVariant"])

    def handle_data(self, data):
        if self._structured is not None:
            self._structured.append(data)
        elif self._skip is None and data.strip():
            if self._pdf_link:
                self._link_text.append(data.strip())
            self.words.append(data.strip())
            if self._in_title:
                self.title.append(data.strip())
            elif self._row is not None:
                self._row.append(data.strip())
            else:
                self._section.append(data.strip())


def _public_address(host: str, port: int) -> str:
    addresses = {
        row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    }
    if not addresses or any(
        not ipaddress.ip_address(address).is_global
        or ipaddress.ip_address(address).is_multicast
        for address in addresses
    ):
        raise ValueError("Supplier URL must resolve to a public address")
    return min(addresses, key=lambda address: (":" in address, address))


def _get_pinned(
    url, address: str, deadline: float, *, max_bytes: int, index: bool = False,
) -> tuple[int, str, str, bytes]:
    allowed = ("application/xml", "text/xml", "text/plain") if index else (
        "text/html", "application/xhtml+xml", "text/plain", "application/pdf",
    )
    connection_type = (
        http.client.HTTPSConnection
        if url.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(
        url.hostname, url.port, timeout=max(0.001, deadline - time.monotonic())
    )
    # Keep the original TLS hostname and HTTP Host header. Only the socket uses the checked IP.
    def connect(target, *args, **kwargs):
        sock = socket.create_connection((address, target[1]), *args, **kwargs)
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        return sock

    connection._create_connection = connect
    timer = None
    try:
        connection.connect()
        sock = connection.sock

        def stop_reading():
            try:
                # Do not clear an SSLSocket's TLS state while its reader is running.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

        # Socket timeouts alone reset on each byte, including inside HTTP header reads.
        timer = threading.Timer(max(0, deadline - time.monotonic()), stop_reading)
        timer.daemon = True
        timer.start()
        target = url.path or "/"
        if url.query:
            target += f"?{url.query}"
        connection.request(
            "GET",
            target,
            headers={
                "Accept": ",".join(allowed),
                "Accept-Encoding": "identity",
                "User-Agent": "FilaBrain",
            },
        )
        response = connection.getresponse()
        if time.monotonic() >= deadline:
            raise TimeoutError("Supplier page timed out")
        if response.status == 429:
            raise SupplierRateLimited(url.hostname, response.getheader("Retry-After", ""))
        location = response.getheader("Location", "")
        content_type = (
            response.getheader("Content-Type", "").split(";")[0].strip().lower()
        )
        if response.status != 200 or content_type not in allowed:
            return response.status, location, content_type, b""
        body = bytearray()
        while len(body) <= max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Supplier page timed out")
            if sock:
                sock.settimeout(remaining)
            chunk = response.read1(min(32_768, max_bytes + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError(SUPPLIER_TOO_LARGE)
        if time.monotonic() >= deadline:
            raise TimeoutError("Supplier page timed out")
        return response.status, location, content_type, bytes(body)
    except (OSError, http.client.HTTPException) as exc:
        if time.monotonic() >= deadline:
            raise TimeoutError("Supplier page timed out") from exc
        raise
    finally:
        if timer:
            timer.cancel()
            timer.join()
        connection.close()


def _fetch_supplier(
    url: str, *, max_bytes: int = DEFAULT_SUPPLIER_DOWNLOAD_BYTES, archive: bool = False,
) -> Evidence:
    deadline = time.monotonic() + 7
    for _ in range(4):
        parsed, port = _supplier_url(url)
        if _supplier_cooldowns.get(parsed.hostname):
            raise ValueError("HTTP 429 (rate limited; waiting before retry)")
        address = _public_address(parsed.hostname, port)
        if time.monotonic() >= deadline:
            raise TimeoutError("Supplier page timed out")
        status, location, content_type, body = _get_pinned(parsed, address, deadline, max_bytes=max_bytes)
        if status in (301, 302, 303, 307, 308) and location:
            if archive:
                raise ValueError("Archive snapshot redirected")
            url = urljoin(url, location)
            continue
        if status != 200:
            detail = {403: "access denied", 404: "page not found", 429: "rate limited"}.get(status)
            raise ValueError(f"HTTP {status}" + (f" ({detail})" if detail else ""))
        if content_type == "application/pdf":
            try:
                result = subprocess.run(
                    ["pdftotext", "-f", "1", "-l", "3", "-layout", "-", "-"],
                    input=body, capture_output=True, check=True,
                    timeout=max(0.1, min(3, deadline - time.monotonic())),
                )
                text = result.stdout.decode("utf-8", errors="replace")[:MAX_TEXT].strip()
            except (OSError, subprocess.SubprocessError):
                text = ""
            return Evidence(url, "Technical data sheet", text, manual=not text, datasheet=True)
        if not body:
            raise ValueError("Supplier did not return readable HTML, text, or a PDF")
        text = body.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            lines = tuple(" ".join(line.split()) for line in text[:MAX_TEXT].splitlines())
            return Evidence(url, parsed.hostname, " ".join(text.split())[:MAX_TEXT], scopes=lines)
        parser = _ProductPage()
        parser.feed(text)
        parser.close()
        title = " ".join(" ".join(parser.title).split())
        if title.casefold().rstrip(".") in {"just a moment", "access denied", "robot check", "attention required! | cloudflare"}:
            raise ValueError("Automated access blocked")
        products = [p for p in parser.products if p.get("designation")]
        product = products[0] if len(products) == 1 else None
        scopes = list(dict.fromkeys([*parser.main_scopes, *parser.scopes])) if parser.main_scopes else parser.scopes
        visible = " ".join(" ".join(scopes if parser.main_scopes else parser.words).split())
        # Put structured identity first so evidence still contains it on long pages.
        prefix = " ".join(product.values()) + " " if product else ""
        return Evidence(
            url,
            title[:150] or parsed.hostname,
            (prefix + visible)[:MAX_TEXT],
            product,
            scopes=tuple(scope[:MAX_TEXT] for scope in scopes if scope)[:100],
            datasheets=tuple(dict.fromkeys(urljoin(url, link) for link in parser.datasheets))[:4],
            origin="openfilamentdatabase.org" if re.search(r"open filament database", visible, re.IGNORECASE) else "",
        )
    raise ValueError("Too many supplier redirects")


async def fetch_supplier(url: str, *, max_bytes: int = DEFAULT_SUPPLIER_DOWNLOAD_BYTES) -> Evidence:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch_supplier, url, max_bytes=max_bytes), timeout=8)
    except SupplierRateLimited as error:
        _supplier_cooldowns.purge_expired()
        _supplier_cooldowns.set(error.host, True, ttl=error.seconds)
        raise


async def fetch_archived_supplier(url: str, *, max_bytes: int = DEFAULT_SUPPLIER_DOWNLOAD_BYTES) -> Evidence | None:
    url = supplier_page_url(url)
    async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
        response = await client.get("https://archive.org/wayback/available", params={"url": url})
        response.raise_for_status()
        snapshot = response.json().get("archived_snapshots", {}).get("closest") or {}
    timestamp = snapshot.get("timestamp", "")
    if (
        snapshot.get("available") is not True or snapshot.get("status") != "200"
        or not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{14}", timestamp)
    ):
        return None
    path = f"web.archive.org/web/{timestamp}/{url}"
    if snapshot.get("url") not in (f"http://{path}", f"https://{path}"):
        return None
    evidence = await asyncio.wait_for(
        asyncio.to_thread(_fetch_supplier, f"https://{path}", max_bytes=max_bytes, archive=True), timeout=8,
    )
    date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
    return replace(evidence, archived_at=date, datasheets=())


async def supplier_sitemap_urls(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    port = 443 if parsed.scheme == "https" else 80
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.port not in (None, port):
        return ()
    origin = f"{parsed.scheme}://{parsed.hostname}"
    cached = _product_sitemaps.get(origin)
    if cached is not None:
        return cached
    if _supplier_cooldowns.get(parsed.hostname):
        return ()

    def read_index():
        deadline = time.monotonic() + 4
        address = _public_address(parsed.hostname, port)

        def read(link):
            # Sitemap declarations may be absolute. Keep index requests on the original host.
            if len(link) > 2048 or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in link):
                return b""
            try:
                target = urlsplit(link)
                if (
                    (target.scheme, target.netloc) == (parsed.scheme, parsed.hostname)
                    and time.monotonic() < deadline
                ):
                    return _get_pinned(target, address, deadline, max_bytes=512 * 1024, index=True)[3]
            except SupplierRateLimited:
                raise
            except (OSError, ValueError, http.client.HTTPException):
                pass
            return b""

        def entries(link):
            try:
                xml = read(link).decode("utf-8-sig")
                if not xml or "<!DOCTYPE" in xml.upper():
                    return (), ()
                root = ElementTree.fromstring(xml)
            except (UnicodeError, ElementTree.ParseError):
                return (), ()
            products = tuple(node.text.strip() for node in root.findall("./{*}url/{*}loc") if node.text)
            indexes = tuple(node.text.strip() for node in root.findall("./{*}sitemap/{*}loc") if node.text)
            return products, indexes

        robots = read(f"{origin}/robots.txt").decode("utf-8", errors="replace")
        declared = re.findall(r"(?im)^[ \t]*sitemap:[ \t]*(https?://[^\s#]+)", robots)
        children = []
        products = []
        for sitemap in dict.fromkeys([*declared[:1], f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]):
            links, indexes = entries(sitemap)
            products.extend(links)
            children.extend(indexes)
            if products or children:
                break
        # Prefer product indexes and read at most two children.
        for child in sorted(dict.fromkeys(children), key=lambda link: "product" not in link.lower())[:2]:
            products.extend(entries(child)[0])
        return tuple(dict.fromkeys(products))

    try:
        urls = await asyncio.wait_for(asyncio.to_thread(read_index), timeout=5)
    except SupplierRateLimited as error:
        _supplier_cooldowns.set(error.host, True, ttl=error.seconds)
        return ()
    except (OSError, ValueError, http.client.HTTPException):
        urls = ()
    _product_sitemaps.purge_expired()
    _product_sitemaps.set(origin, urls, ttl=3600 if urls else 300)
    return urls


async def paramount_product_urls() -> tuple[str, ...]:
    origin = "https://www.paramount-3d.com"
    sitemap = f"{origin}/store-products-sitemap.xml"
    cached = _product_sitemaps.get(sitemap)
    if cached is not None:
        return cached
    body = bytearray()
    try:
        async with (
            asyncio.timeout(5),
            httpx.AsyncClient(timeout=5, trust_env=False) as client,
            client.stream("GET", sitemap) as response,
        ):
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=32_768):
                body.extend(chunk)
                if len(body) > 512 * 1024:
                    return ()
        root = ElementTree.fromstring(body)
    except (httpx.HTTPError, TimeoutError, ElementTree.ParseError):
        return ()
    urls = tuple(dict.fromkeys(
        loc.text for loc in root.findall("./{*}url/{*}loc")
        if loc.text and re.fullmatch(rf"{re.escape(origin)}/product-page/[a-zA-Z0-9_-]+", loc.text)
    ))
    _product_sitemaps.set(sitemap, urls, ttl=3600)
    return urls


async def search_web(origin: str, query: str) -> list[str]:
    async with (
        asyncio.timeout(8),
        httpx.AsyncClient(timeout=5, trust_env=False) as client,
        client.stream("GET", f"{origin.rstrip('/')}/search", params={"q": query, "format": "json"}) as response,
    ):
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > MAX_SEARCH_RESPONSE_BYTES:
                raise ValueError("SearXNG response is too large")
            raw.extend(chunk)
        data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Expected SearXNG JSON search results")
    if not data["results"] and data.get("unresponsive_engines"):
        raise SearchEnginesUnavailable("Upstream search engines failed to return results")
    urls = []
    for row in data["results"]:
        if not isinstance(row, dict) or not (url := public_link(row.get("url"))):
            continue
        try:
            _supplier_url(url)
        except ValueError:
            continue
        urls.append(url)
        if len(urls) == 3:
            break
    return urls


async def ofd_candidates(manufacturer: str) -> tuple[list[dict], str]:
    if not manufacturer:
        return [], ""
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(f"{OFD}/api/v1/brands/index.json")
        response.raise_for_status()
        brands = response.json().get("brands", [])
        brands = [
            row
            for row in brands
            if isinstance(row, dict)
            and str(row.get("name", "")).casefold() == manufacturer.strip().casefold()
        ]
        if len(brands) != 1 or not re.fullmatch(
            r"[a-zA-Z0-9_-]+", str(brands[0].get("slug", ""))
        ):
            return [], ""
        url = f"{OFD}/json/brands/{quote(brands[0]['slug'])}.json"
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()

    products = {row["id"]: row for row in payload.get("filaments", []) if "id" in row}
    variants = {row["id"]: row for row in payload.get("variants", []) if "id" in row}
    items = []
    for size in payload.get("sizes", []):
        variant = variants.get(size.get("variant_id"), {})
        product = products.get(variant.get("filament_id"), {})
        if not product:
            continue
        name = product.get("name")
        material = product.get("material")
        plus = re.fullmatch(r"PLA\+(?:\s+(\d+(?:\.\d+)+))?", name.strip(), re.IGNORECASE) if (
            isinstance(name, str) and isinstance(material, str) and material.casefold() == "pla"
        ) else None
        colors = variant.get("color_hex")
        traits = variant.get("traits")
        traits = traits if isinstance(traits, dict) else {}
        finishes = [
            key for key in ("matte", "silk", "glossy", "satin", "translucent", "transparent", "glow", "glitter", "iridescent")
            if traits.get(key) is True
        ]
        items.append(
            {
                "id": size.get("id"),
                "name": name,
                "manufacturer": {"name": brands[0]["name"]},
                "material": {"key": "PLA+" if plus else material},
                "material_subgroup": plus[1] if plus else None,
                "manufacturer_color_name": variant.get("name"),
                "color_hex": colors,
                "color_mode": "multi" if isinstance(colors, list) and len(colors) > 1 else "single",
                "finish_type": " / ".join(finishes) or None,
                "diameter_mm": size.get("diameter"),
                "nominal_weight_g": size.get("filament_weight"),
                "density_g_cm3": product.get("density"),
                "sku": size.get("article_number"),
                "datasheet_url": product.get("data_sheet_url"),
                "dry_temp": product.get("dry_temperature"),
                "dry_time_hours": product.get("dry_time"),
                "temp_nozzle_min": product.get("min_print_temperature"),
                "temp_nozzle_max": product.get("max_print_temperature"),
                "temp_bed_min": product.get("min_bed_temperature"),
                "temp_bed_max": product.get("max_bed_temperature"),
            }
        )
    return items, url
