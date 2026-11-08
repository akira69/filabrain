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
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from app.core.cache import TTLCache

OFD = "https://api.openfilamentdatabase.org"
MAX_BODY = 4 * 1024 * 1024  # Storefront HTML includes scripts that the parser discards.
MAX_TEXT = 12_000
SHARED_SPEC = re.compile(r"\b(?:applies to all variants|same for all variants|all variants share)\b", re.IGNORECASE)
_supplier_cooldowns = TTLCache()


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
        self._row: list[str] | None = None
        self._section: list[str] = []
        self._section_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "a" and self._skip is None:
            href = dict(attrs).get("href") or ""
            # Keep only PDF links. The supplier fetcher validates every URL and redirect.
            if re.search(r"\.pdf(?:[?#]|$)", href, re.IGNORECASE):
                self._pdf_link = href
                self._link_text.clear()
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
            label = " ".join(self._link_text) + " " + self._pdf_link
            if re.search(r"technical[ _-]*data[ _-]*sheet|\btds\b", label, re.IGNORECASE):
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
            self.scopes.append(" ".join(" ".join(self._row).split()))
            self._row = None
        elif self._skip is None and self._row is None:
            if tag in ("section", "div", "article", "li", "option") or tag == "p" and not self._section_depth:
                self._finish_section()
            if tag == "section":
                self._section_depth = max(0, self._section_depth - 1)

    def _separates_scope(self, tag):
        return tag in (
            "h1", "h2", "h3", "h4", "section", "div", "article", "li", "option"
        ) or tag == "p" and not self._section_depth

    def _finish_section(self):
        if self._section:
            self.scopes.append(" ".join(" ".join(self._section).split()))
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


def _get_pinned(url, address: str, deadline: float) -> tuple[int, str, str, bytes]:
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
                "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf",
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
        if response.status != 200 or content_type not in (
            "text/html",
            "application/xhtml+xml",
            "text/plain",
            "application/pdf",
        ):
            return response.status, location, content_type, b""
        body = bytearray()
        while len(body) <= MAX_BODY:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Supplier page timed out")
            if sock:
                sock.settimeout(remaining)
            chunk = response.read1(min(32_768, MAX_BODY + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > MAX_BODY:
            raise ValueError("Supplier page is too large")
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


def _fetch_supplier(url: str, *, archive: bool = False) -> Evidence:
    deadline = time.monotonic() + 7
    for _ in range(4):
        parsed = urlsplit(url)
        port = 443 if parsed.scheme == "https" else 80
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, port)
            or len(url) > 2048
            or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in url)
        ):
            raise ValueError("Expected a public supplier HTTP or HTTPS URL")
        if _supplier_cooldowns.get(parsed.hostname):
            raise ValueError("HTTP 429 (rate limited; waiting before retry)")
        address = _public_address(parsed.hostname, port)
        if time.monotonic() >= deadline:
            raise TimeoutError("Supplier page timed out")
        status, location, content_type, body = _get_pinned(parsed, address, deadline)
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
        products = [p for p in parser.products if p.get("designation")]
        product = products[0] if len(products) == 1 else None
        visible = " ".join(" ".join(parser.words).split())
        # Put structured identity first so evidence still contains it on long pages.
        prefix = " ".join(product.values()) + " " if product else ""
        return Evidence(
            url,
            " ".join(parser.title)[:150] or parsed.hostname,
            (prefix + visible)[:MAX_TEXT],
            product,
            scopes=tuple(scope[:MAX_TEXT] for scope in parser.scopes if scope)[:100],
            datasheets=tuple(dict.fromkeys(urljoin(url, link) for link in parser.datasheets))[:4],
            origin="openfilamentdatabase.org" if re.search(r"open filament database", visible, re.IGNORECASE) else "",
        )
    raise ValueError("Too many supplier redirects")


async def fetch_supplier(url: str) -> Evidence:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch_supplier, url), timeout=8)
    except SupplierRateLimited as error:
        _supplier_cooldowns.purge_expired()
        _supplier_cooldowns.set(error.host, True, ttl=error.seconds)
        raise


async def fetch_archived_supplier(url: str) -> Evidence | None:
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
    evidence = await asyncio.wait_for(asyncio.to_thread(_fetch_supplier, f"https://{path}", archive=True), timeout=8)
    date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
    return replace(evidence, archived_at=date, datasheets=())


async def search_web(origin: str, query: str) -> list[str]:
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(
            f"{origin.rstrip('/')}/search", params={"q": query, "format": "json"}
        )
        response.raise_for_status()
        data = response.json()
    if not data.get("results") and data.get("unresponsive_engines"):
        raise ValueError("Upstream search engines failed to return results")
    return [
        row["url"]
        for row in data.get("results", [])[:3]
        if isinstance(row, dict) and isinstance(row.get("url"), str)
    ]


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
                "name": product.get("name"),
                "manufacturer": {"name": brands[0]["name"]},
                "material": {"key": product.get("material")},
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
