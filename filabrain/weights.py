import re

WEIGHT_EXPRESSION = (
    r"(?<![\d.,/\-])(?<!\d[ \u00a0\u2009\u202f])(?P<weight>"
    r"\d{1,3}(?:,\d{3}){1,2}(?:\.\d{1,3})?"
    r"|\d{1,3}(?:\.\d{3}){1,2}(?:,\d{1,3})?"
    r"|\d{1,3}(?:[ \u00a0\u2009\u202f]\d{3}){1,2}(?:[.,]\d{1,3})?"
    r"|\d{1,6}(?:[.,]\d{1,3})?)"
    r"\s*(?P<unit>kg|g|grams?|kilograms?)\b"
)

AMBIGUOUS_WEIGHT_REPLY = "Please repeat your question with the weight in grams without a thousands separator."
_AMBIGUOUS_DOT_WEIGHT = re.compile(r"[1-9]\d{0,2}\.\d{3}")


def grams_from_match(match: re.Match[str]) -> float | None:
    # A single dot before three digits can mark either a decimal or a thousands group.
    if _AMBIGUOUS_DOT_WEIGHT.fullmatch(match["weight"]):
        return None
    raw = "".join(match["weight"].split())
    kilos = match["unit"].lower().startswith("k")
    # Keep decimal commas for kilograms and fractional gram values like 0,125 g.
    grouped_comma = re.fullmatch(r"[1-9]\d{0,2},\d{3}", raw)
    if "," in raw and "." in raw:
        grouping = "," if raw.rfind(",") < raw.rfind(".") else "."
        raw = raw.replace(grouping, "")
    elif raw.count(",") > 1 or (grouped_comma and not kilos):
        raw = raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    return float(raw.replace(",", ".")) * (1000 if kilos else 1)
