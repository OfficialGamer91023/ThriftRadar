"""Caption parsing: price, size, brand, condition. Spec: DESIGN.md §4.2 `parse_caption`, `to_size_eu`. Pure."""

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import Decimal

# ---------- normalization ----------

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_DIGITS)
    text = _INVISIBLE.sub("", text).lower()
    return re.sub(r"\s+", " ", text).strip()


# ---------- price ----------

_NUM = r"(\d{1,3}(?:[,\s]\d{3})+|\d{3,7})"
_PRICE_PATTERNS = [
    # (regex, group holding the number, multiplier)
    (re.compile(r"(rs\.?|pkr|price|demand|final|now)\s*[:=\-]?\s*" + _NUM + r"(?:\s*/-)?"), 2, 1),
    (re.compile(r"(?<![\d.])(\d+(?:\.\d)?)\s*k\b"), 1, 1000),
    (re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{3,7})\s*/-"), 1, 1),
    (re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{3,7})\s*(?:rs|pkr)\b"), 1, 1),
]
_RANK2 = re.compile(r"\b(final|now)\b")
_RANK1 = re.compile(r"\b(price|demand)\b")
_CURRENCY_BEFORE = re.compile(r"(?:\brs\.?|\bpkr)\s*[:=\-]?\s*$")
_CURRENCY_AFTER = re.compile(r"^\s*(?:rs|pkr)\b")
_ON_REQUEST = re.compile(r"dm for price|price in dm|\binbox\b")


def _parse_price(t: str) -> tuple[int | None, str | None, bool, bool]:
    """(price, currency, price_on_request, price_ambiguous)."""
    found: list[tuple[int, int, int]] = []  # (number start, number end, value)
    for rx, grp, mult in _PRICE_PATTERNS:
        for m in rx.finditer(t):
            start, end = m.span(grp)
            if any(a < end and start < b for a, b, _ in found):
                continue  # same number already matched by an earlier pattern
            value = int(round(float(re.sub(r"[,\s]", "", m.group(grp))) * mult))
            if value > 0:
                found.append((start, end, value))
    found.sort()

    candidates: list[tuple[int, int, str | None]] = []  # (rank, value, currency)
    prev_end = 0
    for start, end, value in found:
        # Keywords count only between the previous number and this one, at most 16 chars back.
        context = t[max(prev_end, start - 16): start]
        rank = 2 if _RANK2.search(context) else 1 if _RANK1.search(context) else 0
        currency = "PKR" if _CURRENCY_BEFORE.search(context) or _CURRENCY_AFTER.search(t[end:end + 6]) else None
        candidates.append((rank, value, currency))
        prev_end = end

    on_request = bool(_ON_REQUEST.search(t))
    if not candidates:
        return None, None, on_request, False
    top = max(c[0] for c in candidates)
    values = {c[1] for c in candidates if c[0] == top}
    if len(values) != 1:
        return None, None, on_request, True
    value = values.pop()
    currency = next((c[2] for c in candidates if c[1] == value and c[2]), None) or next(
        (c[2] for c in candidates if c[2]), None)
    return value, currency, on_request, False


# ---------- size ----------

_SYS = {"size": None, "sz": None, "eu": "EU", "eur": "EU", "uk": "UK", "us": "US", "cm": "CM"}
_SIZE_NUM = r"(\d{1,2}(?:\.5)?)"
_SIZE_PREFIX = re.compile(
    r"\b(size|sz|eur|eu|uk|us|cm)\s*[:\-]?\s*" + _SIZE_NUM + r"(?![\d.])"
    r"(?:\s*[-/–]\s*" + _SIZE_NUM + r"(?![\d.]))?"
    r"(?:\s*(eur|eu|uk|us|cm)\b(?!\s*[:\-]?\s*\d))?"  # a system word followed by a number is the next label
)
_SIZE_SUFFIX = re.compile(r"(?<![\d.])(\d{2}(?:\.5)?)\s*(eur|eu|cm)\b")


def _fmt(v: Decimal) -> str:
    return str(v.normalize()) if v != v.to_integral() else str(int(v))


def _parse_size(t: str) -> tuple[str | None, Decimal | None, str | None, bool]:
    """(size_label, size_value, size_system, size_ambiguous)."""
    cands: set[tuple[Decimal, str | None]] = set()
    ranged = False
    spans: list[tuple[int, int]] = []
    for m in _SIZE_PREFIX.finditer(t):
        label, a, b, trailing = m.group(1), m.group(2), m.group(3), m.group(4)
        system = _SYS[label] or (_SYS[trailing] if trailing else None)
        for raw in filter(None, (a, b)):
            cands.add((Decimal(raw), system))
        ranged = ranged or b is not None
        spans.append(m.span())
    for m in _SIZE_SUFFIX.finditer(t):
        if any(a <= m.start() < b for a, b in spans):
            continue
        cands.add((Decimal(m.group(1)), _SYS[m.group(2)]))

    # An unlabelled number in the EU range is EU.
    resolved = {(v, s if s else ("EU" if 34 <= v <= 50 else None)) for v, s in cands}
    if not resolved:
        return None, None, None, False
    by_system: dict[str | None, set[Decimal]] = {}
    for v, s in resolved:
        by_system.setdefault(s, set()).add(v)
    if ranged or any(len(vs) > 1 for vs in by_system.values()):
        return None, None, None, True
    if None in by_system and len(by_system) > 1:
        return None, None, None, True  # an unknown-system number next to a labelled one
    system = "EU" if "EU" in by_system else next(iter(by_system))
    value = next(iter(by_system[system]))
    label = f"{system} {_fmt(value)}" if system else _fmt(value)
    return label, value, system, False


# ---------- brand ----------

BRAND_ALIASES: list[tuple[str, str]] = [
    (r"air jordan|jordan|aj ?\d{1,2}", "Jordan"),
    (r"nike", "Nike"),
    (r"adidas", "Adidas"),
    (r"yeezy", "Yeezy"),
    (r"new balance|nb ?\d{3,4}", "New Balance"),
    (r"puma", "Puma"),
    (r"vans", "Vans"),
    (r"converse", "Converse"),
    (r"asics", "Asics"),
    (r"onitsuka(?: tiger)?", "Onitsuka Tiger"),
    (r"reebok", "Reebok"),
    (r"skechers", "Skechers"),
    (r"fila", "Fila"),
    (r"under armou?r", "Under Armour"),
    (r"salomon", "Salomon"),
    (r"hoka", "Hoka"),
    (r"timberland", "Timberland"),
    (r"dr\.? ?martens|doc martens", "Dr. Martens"),
    (r"clarks", "Clarks"),
    (r"crocs", "Crocs"),
    (r"birkenstock", "Birkenstock"),
    (r"balenciaga", "Balenciaga"),
    (r"gucci", "Gucci"),
    (r"off[- ]white", "Off-White"),
    (r"golden goose", "Golden Goose"),
    (r"veja", "Veja"),
    (r"mizuno", "Mizuno"),
    (r"saucony", "Saucony"),
    (r"lacoste", "Lacoste"),
    (r"hush puppies", "Hush Puppies"),
    (r"bata", "Bata"),
    (r"servis", "Servis"),
    (r"ndure", "Ndure"),
    (r"borjan", "Borjan"),
]
MODEL_BRANDS: list[tuple[str, str]] = [
    (r"af ?1|air force|air max|dunk", "Nike"),
    (r"samba|gazelle|superstar|stan smith", "Adidas"),
    (r"chuck taylor|all star", "Converse"),
    (r"old skool|sk8", "Vans"),
]
_BRAND_RX = [(re.compile(rf"\b(?:{p})\b"), name) for p, name in BRAND_ALIASES]
_MODEL_RX = [(re.compile(rf"\b(?:{p})\b"), name) for p, name in MODEL_BRANDS]


def _first_match(t: str, table) -> str | None:
    hits = [(m.start(), name) for rx, name in table for m in [rx.search(t)] if m]
    return min(hits)[1] if hits else None


# ---------- condition ----------

_NEW = re.compile(r"\bbnib\b|brand new|new with box|\bds\b|deadstock")
_LIKE_NEW = re.compile(r"\bvnds\b|like new")
_USED = re.compile(r"\bused\b|\bworn\b")
_SCORE = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*/\s*10\b")
_SOLD = re.compile(r"\bsold\b")


@dataclass
class CaptionFields:
    price: int | None = None
    currency: str | None = None
    price_on_request: bool = False
    price_ambiguous: bool = False
    size_label: str | None = None
    size_value: Decimal | None = None
    size_system: str | None = None
    size_ambiguous: bool = False
    brand: str | None = None
    condition: str | None = None
    condition_score: float | None = None
    is_sold: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra")
        return d


def parse_caption(text: str | None) -> CaptionFields:
    if not text:
        return CaptionFields()
    t = normalize(text)
    price, currency, on_request, price_amb = _parse_price(t)
    size_label, size_value, size_system, size_amb = _parse_size(t)
    brand = _first_match(t, _BRAND_RX) or _first_match(t, _MODEL_RX)

    condition = "new" if _NEW.search(t) else "like_new" if _LIKE_NEW.search(t) else "used" if _USED.search(t) else None
    score = None
    if m := _SCORE.search(t):
        v = float(m.group(1))
        score = v if 0 <= v <= 10 else None

    return CaptionFields(
        price=price, currency=currency, price_on_request=on_request, price_ambiguous=price_amb,
        size_label=size_label, size_value=size_value, size_system=size_system, size_ambiguous=size_amb,
        brand=brand, condition=condition, condition_score=score, is_sold=bool(_SOLD.search(t)),
    )


# ---------- size conversion ----------

# Men's, adidas-style chart. UK→EU; US men's = UK + 0.5.
_UK_TO_EU = {
    3: 35.5, 3.5: 36, 4: 36.5, 4.5: 37, 5: 38, 5.5: 38.5, 6: 39, 6.5: 40, 7: 40.5, 7.5: 41,
    8: 42, 8.5: 42.5, 9: 43, 9.5: 44, 10: 44.5, 10.5: 45, 11: 46, 11.5: 46.5, 12: 47, 12.5: 47.5, 13: 48,
}


def to_size_eu(value: Decimal | None, system: str | None) -> tuple[Decimal | None, bool]:
    if value is None:
        return None, False
    if system == "EU":
        return Decimal(value), False
    if system in ("UK", "US"):
        uk = float(value) - (0.5 if system == "US" else 0)
        eu = _UK_TO_EU.get(uk)
        return (Decimal(str(eu)), True) if eu is not None else (None, False)
    return None, False
