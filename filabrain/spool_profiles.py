"""Read-only candidate spool designs from public catalogs."""

import asyncio
import http.client
import re
import time
from html.parser import HTMLParser

from app.core.cache import response_cache

from . import knowledge, sources
from .catalog import search_spool_cards
from .weights import AMBIGUOUS_WEIGHT_REPLY, WEIGHT_EXPRESSION, grams_from_match

EMPTY = "https://theemptyspool.cc"
ONLY = "https://www.onlyspoolz.com"
CATALOGS = (EMPTY + "/", ONLY + "/portfolio/")
CATALOG_CACHE_PREFIX = "filabrain.spool_profiles."
PACKAGE_WEIGHT = re.compile(WEIGHT_EXPRESSION, re.I)
SPOOL = re.compile(r"\b(?:spools?|reels?|bobbins?)\b", re.I)
TARE = re.compile(
    r"\btare\b|\bempty (?:weight|spool mass)\b|\b(?:spool|reel|bobbin) (?:weight|mass)\b|"
    r"\bempty\b.*\b(?:weighs?|weight|mass|heavy)\b|"
    r"\b(?:weighs?|weight|mass|heavy)\b.*\bempty\b", re.I,
)
DIMENSIONS = re.compile(
    r"\b(?:dimensions?|width|height|(?:outside|outer|hole|hub|bore|inner|inside|spool) diameter|"
    r"diameter of (?:the )?spool|what size|size of|how (?:wide|tall))\b", re.I,
)
VISUAL = re.compile(
    r"\b(?:look like|photos?|pictures?|visually identify|identify|which design|which style|"
    r"which (?:spool|reel) (?:design|style|profile)|(?:spool|reel) (?:designs?|styles?|profiles?))\b", re.I,
)
LOCAL = re.compile(
    r"\b(?:filaman|inventory|stock|on hand|do (?:i|we) have|how (?:much|many)\b[^?]*\bleft|"
    r"remaining (?:weight|filament)|spool\s+#\d+)\b", re.I,
)
STYLES = {
    "cardboard": r"\b(?:cardboard|paperboard)\b",
    "plastic": r"\bplastic\b",
    "clear": r"\b(?:clear|transparent|translucent)\b",
    "black": r"\bblack\b",
    "white": r"\bwhite\b",
    "no slots": r"\b(?:no slots|without (?:slots|cutouts)|solid flanges?|unslotted)\b",
    "slotted": r"\b(?:slots|slotted|cutouts)\b",
    "reusable": r"\b(?:reusable|refillable)\b",
}
STYLE_CUES = re.compile("|".join(STYLES.values()), re.I)
MATERIALS = {"abs", "asa", "hips", "nylon", "pa", "pc", "pet", "petg", "pla", "pp", "pva", "tpe", "tpu"}
MATERIAL_ALIASES = {"epla": "pla", "rpla": "pla", "pa6": "pa", "pa12": "pa"}
LOOKUP_WORDS = set(
    "a about am an and are at be bobbin bobbins bore can could design designs diameter dimensions do does "
    "center centre empty find for have heavy height help hole how hub i identify in inner inside is it like look looks "
    "me mean mine much my "
    "of on one ones or our outer outside photo photos picture pictures please profile profiles reel reels "
    "s show size spool spools style styles tall tare tell that the these this to visually was we weigh "
    "weighs weight what which wide width with would you".split()
)
REFINEMENT_WORDS = LOOKUP_WORDS | {"brand", "from", "got", "has", "label", "made", "maker", "manufacturer", "printed", "product", "reads", "says"}
CHAT_REQUEST = re.compile(
    r"^\s*(?:(?:please|also|now|(?:can|could|would) you)\s+)*"
    r"(?:tell me|show me|help me|explain|compare|recommend|how|why|what)\b", re.I,
)
LABEL_ANSWER = re.compile(
    r"^\s*(?:(?:the\s+)?(?:label|brand|maker|manufacturer|product)(?:\s*:|\s+(?:says|reads|is)\b)"
    r"|(?:it|mine)\s+(?:says|reads)\b)", re.I,
)
GENERIC_TITLE_PREFIX = {"3d", "add", "filament", "generic", "ice", "petg", "print", "real", "smart", "the"}
STYLE_QUESTION = "Which design matches your spool’s material, color, and cutouts?"
MISSING_QUESTION = "What brand and product are printed on the spool?"
DIMENSION_FIELDS = (
    ("outer_diameter_mm", "outside diameter"),
    ("width_mm", "width"),
    ("hole_diameter_mm", "hole diameter"),
)
SPEC_FIELDS = {
    "empty spool weight": ("empty_weight_g", "g"),
    "advertised filament weight": ("net_weight_g", "g"),
    "outside diameter": ("outer_diameter_mm", "mm"),
    "outer diameter": ("outer_diameter_mm", "mm"),
    "height": ("width_mm", "mm"),
    "hole diameter": ("hole_diameter_mm", "mm"),
}


def _mass_or_length(text: str, unit: str):
    match = re.fullmatch(rf"(\d{{1,6}}(?:\.\d{{1,6}})?)\s*({unit})(?:\s+\([^)]*\))?", text, re.I)
    if match and (value := float(match[1])) > 0:
        return value * (1000 if match[2].lower() == "kg" else 1)
    return None


def _catalog_url(origin: str, href: str, path_pattern: str):
    path = href.removeprefix(origin)
    return origin + path if re.fullmatch(path_pattern, path) else None


class _CatalogPage(HTMLParser):
    def __init__(self, origin: str):
        super().__init__()
        self.origin = origin
        self.entries: list[tuple[dict, str, str]] = []
        self.entry: dict = {}
        self.style = ""
        self.depth = 0
        self.tag = ""
        self.text: list[str] = []
        self.cells = 0

    def handle_starttag(self, tag, attrs):
        fields = {name: value or "" for name, value in attrs}
        if self.origin == ONLY:
            if tag == "div" and "masonry-item" in fields.get("class", "").split():
                self.entry = {"source": "OnlySpoolz"}
                self.style = fields.get("data-spool-type", "").replace("_", " ")
                self.depth = 0
            if self.entry and tag == "div":
                self.depth += 1
        elif tag == "tr" and re.fullmatch(r"[0-9]{1,8}", fields.get("data-spool-id", "")):
            self.entry = {"source": "The Empty Spool", "id": "emptyspool-" + fields["data-spool-id"]}
            self.style = ""
            self.cells = 0
        if not self.entry:
            return
        if self.origin == ONLY:
            if tag in {"h4", "li"}:
                self.tag = tag
                self.text = []
            if tag == "a" and self.tag == "h4":
                url = _catalog_url(ONLY, fields.get("href", ""), r"/portfolio/[a-z0-9][a-z0-9_.-]*/")
                if url:
                    self.entry.update(url=url, id="onlyspoolz-" + url.rstrip("/").rsplit("/", 1)[-1])
            if tag == "img" and "photo" not in self.entry:
                src = fields.get("src", "")
                if "scheme" not in src and (photo := _catalog_url(
                    ONLY, src, r"/img/portfolio/[a-zA-Z0-9_.-]+\.(?:webp|png|jpe?g)",
                )):
                    self.entry["photo"] = photo
        elif tag == "td":
            self.cells += 1
            if self.cells == 4:
                self.tag = "td"
                self.text = []
        elif tag == "a":
            href = fields.get("href", "")
            if url := _catalog_url(EMPTY, href, r"/brand/[a-z0-9-]+"):
                self.entry["url"] = url
            if "spool-photo-trigger" in fields.get("class", "").split() and (photo := _catalog_url(
                EMPTY, href, r"/static/spools/[a-zA-Z0-9_.-]+\.(?:webp|png|jpe?g)(?:\?v=[a-f0-9]+)?",
            )):
                self.entry["photo"] = photo
        elif tag == "button" and fields.get("data-spool-label"):
            self.entry["title"] = fields["data-spool-label"]
            if tare := _mass_or_length(fields.get("data-spool-weight", "") + " g", "g"):
                self.entry["empty_weight_g"] = tare

    def handle_data(self, data):
        if self.tag:
            self.text.append(data)

    def handle_endtag(self, tag):
        if self.entry and tag == self.tag:
            text = " ".join("".join(self.text).split())
            self.tag = ""
            if tag == "h4":
                self.entry["title"] = text
            elif tag == "td":
                if net := _mass_or_length(text, "kg|g"):
                    self.entry["net_weight_g"] = net
            else:
                label, _, value = text.partition(":")
                if field := SPEC_FIELDS.get(label.strip().lower()):
                    name, unit = field
                    if measurement := _mass_or_length(value.strip(), unit):
                        self.entry[name] = measurement
        if not self.entry:
            return
        if self.origin == ONLY and tag == "div":
            self.depth -= 1
        if (self.origin == ONLY and tag == "div" and self.depth == 0) or (
            self.origin == EMPTY and tag == "tr"
        ):
            self.tag = ""
            self.entry["title"] = " ".join(self.entry.get("title", "").split())[:200]
            if all(self.entry.get(name) for name in ("id", "url", "title", "empty_weight_g")):
                style = self.style if self.origin == ONLY else self.entry["title"].partition(" — ")[2]
                self.entries.append((self.entry, "", style))
            self.entry = {}


async def _catalog(url: str):
    key = CATALOG_CACHE_PREFIX + url
    if cached := response_cache.get(key):
        return cached

    def load():
        deadline = time.monotonic() + 5
        parsed, port = sources._supplier_url(url)
        address = sources._public_address(parsed.hostname, port)
        if time.monotonic() >= deadline:
            return []
        status, _, content_type, data = sources._get_pinned(parsed, address, deadline, max_bytes=2 * 1024**2)
        if status != 200 or content_type not in {"text/html", "application/xhtml+xml"}:
            return []
        page = _CatalogPage(f"{parsed.scheme}://{parsed.netloc}")
        page.feed(data.decode("utf-8", "replace"))
        page.close()
        return page.entries

    try:
        async with asyncio.timeout(5):
            entries = await asyncio.to_thread(load)
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return []
    if entries:
        response_cache.set(key, entries, ttl=3600)
    return entries


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", PACKAGE_WEIGHT.sub(" ", text.lower()).replace("+", " plus "))


def _materials(words: set[str]) -> set[str]:
    return {MATERIAL_ALIASES.get(word, word) for word in words} & MATERIALS


def _terms(text: str) -> set[str]:
    return set(_words(STYLE_CUES.sub(" ", text)))


def _styles(text: str) -> set[str]:
    found = {key for key, pattern in STYLES.items() if re.search(pattern, text, re.I)}
    if "no slots" in found:
        found.discard("slotted")
    return found


def spool_intent(question: str):
    """Leave inventory questions and ordinary filament conversations on the existing route."""
    if LOCAL.search(question) or not SPOOL.search(question):
        return None
    if DIMENSIONS.search(question):
        return "dimensions"
    if TARE.search(question):
        return "tare"
    if VISUAL.search(question):
        return "visual"
    return None


def _identifies_catalog_maker(question: str) -> bool:
    if LABEL_ANSWER.search(question):
        return True
    bare = re.sub(r"^\s*(?:it is|it's|mine is)\s+", "", question, flags=re.I)
    words = _words(bare)
    makers = knowledge._data("makers.json")
    if any(knowledge._maker_key(" ".join(words[:end])) in makers for end in range(1, min(len(words), 5) + 1)):
        return True
    for url in CATALOGS:
        for profile, _, _ in response_cache.get(CATALOG_CACHE_PREFIX + url) or ():
            name = _words(profile["title"].partition(" — ")[0])
            if url.startswith(ONLY) and name:
                name = name[:2 if name[0] in GENERIC_TITLE_PREFIX else 1]
            if name and words[:len(name)] == name:
                return True
    return False


def spool_follow_up(question: str, previous_reply: str):
    if (
        not previous_reply.endswith((STYLE_QUESTION, MISSING_QUESTION))
        or LOCAL.search(question)
        or CHAT_REQUEST.search(question)
        or len(question.split()) > 20
    ):
        return False
    remaining = _terms(question) - REFINEMENT_WORDS
    if previous_reply.endswith(MISSING_QUESTION):
        return "?" not in question and bool(remaining) and _identifies_catalog_maker(question)
    return not remaining and bool(STYLE_CUES.search(question) or PACKAGE_WEIGHT.search(question))


def _filamandb_identity(words: list[str], manufacturer: str, product: str) -> tuple[str, str]:
    if manufacturer and product:
        return manufacturer, product
    text = " ".join(words)
    if not manufacturer:
        aliases = (
            (alias, maker_names[0])
            for maker_names in knowledge._data("makers.json").values() for alias in maker_names
        )
        match = max(
            ((len(alias), name, normalized) for alias, name in aliases
             if knowledge._includes(text, normalized := " ".join(_words(alias)))),
            default=None,
        )
        if not match:
            return "", ""
        _, manufacturer, label = match
    else:
        label = " ".join(_words(manufacturer))
    product_words = [word for word in words if word not in LOOKUP_WORDS and word not in label.split()]
    designation = [word for word in product_words if not _materials({word})]
    return manufacturer, " ".join(designation or product_words)[:200].strip()


async def find_spool_profiles(
    question: str, intent: str, refinement: str = "", *,
    style_hint: str = "", package_weight_g: float | None = None, manufacturer_name: str = "",
    product_name: str = "", material_name: str = "", compare_linked: bool = False,
):
    query = question + " " + refinement
    query_words = _words(query)
    words = set(query_words)
    terms = (_terms(question) - LOOKUP_WORDS) | (_terms(refinement) - REFINEMENT_WORDS)
    materials = _materials(terms)
    styles = _styles(style_hint or refinement) or _styles(question)
    size_mentions = list(PACKAGE_WEIGHT.finditer(refinement)) or PACKAGE_WEIGHT.finditer(question)
    sizes = {package_weight_g} if package_weight_g and package_weight_g > 0 else {
        grams_from_match(match) for match in size_mentions
    }
    if None in sizes:
        return {"reply": AMBIGUOUS_WEIGHT_REPLY, "action": None, "spool_profiles": []}
    sizes.discard(0)
    lookups = [_catalog(url) for url in CATALOGS]
    if not compare_linked:
        search = " ".join(dict.fromkeys(word for word in query_words if word in terms))[:200].strip()
        manufacturer, product = _filamandb_identity(
            _words(question + " " + " ".join(
                word for word in _words(refinement) if word not in REFINEMENT_WORDS
            )),
            manufacturer_name.strip(), product_name.strip(),
        )
        families = _materials(set(_words(material_name))) or materials
        material = " ".join(sorted(families)) or material_name
        lookups.append(search_spool_cards(search, manufacturer, product, material, sizes))
    catalogs = await asyncio.gather(*lookups)
    ranked = []
    for entries in catalogs:
        for profile, label, style in entries:
            name = _words(f'{label} {profile["title"]}')
            if not name or name[0] not in words:
                continue
            if name[0] in GENERIC_TITLE_PREFIX and not set(name[:2]) <= words:
                continue
            if name[0] == "the" and "The Filament" not in query:
                continue
            named_materials = _materials(set(name))
            if materials and named_materials and materials.isdisjoint(named_materials):
                continue
            design = _styles(style)
            if compare_linked and not styles <= design:
                continue
            net_weight_g = profile.get("net_weight_g")
            # FilaManDB profiles can describe a reel without specifying its filament capacity.
            size_compatible = bool(sizes) and (
                net_weight_g in sizes or (profile["source"] == "FilaManDB" and net_weight_g is None)
            )
            rank = (
                len(terms.intersection(name)),
                size_compatible,
                len(styles & design),
            )
            ranked.append((rank, profile))
    best_rank = max((rank for rank, _ in ranked), default=(0, False, 0))
    profiles = [profile for rank, profile in ranked if rank == best_rank]

    def completeness(profile):
        dimension_count = sum(key in profile for key, _ in DIMENSION_FIELDS)
        has_photo = "photo" in profile
        return (has_photo, dimension_count) if intent == "visual" else (dimension_count, has_photo)

    profiles = sorted(profiles, key=completeness, reverse=True)[:4]
    term_matches, _, style_matches = best_rank

    if not profiles:
        reply = "I couldn’t find a matching listing in the available spool catalogs. " + MISSING_QUESTION
        if not any(catalogs):
            reply = "The spool catalogs are unavailable right now. Try again later."
    elif term_matches < len(terms):
        reply = "I couldn’t verify that exact product. I found possible spool designs. " + STYLE_QUESTION
    elif sizes and not any(profile.get("net_weight_g") in sizes for profile in profiles):
        reply = "I found possible spool designs, but could not verify that package size. " + STYLE_QUESTION
    elif len(profiles) > 1 or style_matches < len(styles):
        reply = "I found possible spool designs. " + STYLE_QUESTION
    else:
        return spool_profile_result(profiles[0], intent, candidate=True)
    return {"reply": reply, "action": None, "spool_profiles": profiles}


def spool_profile_result(profile: dict, intent: str, *, candidate: bool = False) -> dict:
    dimensions = [f"{profile[key]:g} mm {label}" for key, label in DIMENSION_FIELDS if key in profile]
    tare = [f'{profile["empty_weight_g"]:g} g empty'] if "empty_weight_g" in profile else []
    specs = tare + dimensions
    reply = f'{profile["source"]} lists {profile["title"]}'
    if specs:
        reply += f' at {", ".join(specs)}'
    reply += "."
    if intent == "dimensions" and not dimensions:
        if tare:
            reply += " The listing gives the tare only."
        reply += " The dimensions are unavailable."
    if intent == "tare" and not tare:
        reply += " The empty weight is unavailable."
    if intent == "visual" and "photo" not in profile:
        reply += " A photo of this design is unavailable."
    if candidate:
        reply += " Compare the design with your spool before using these values."
    return {"reply": reply, "action": None, "spool_profiles": [profile]}
