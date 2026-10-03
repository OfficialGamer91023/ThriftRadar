from decimal import Decimal

import pytest

from app.pipeline.caption import parse_caption, to_size_eu

# (caption, expected subset of fields). Written by us in the style of the group; no real captions.
CASES = [
    # price
    ("Rs 4,500", {"price": 4500, "currency": "PKR"}),
    ("Rs. 4500", {"price": 4500, "currency": "PKR"}),
    ("rs4500", {"price": 4500, "currency": "PKR"}),
    ("PKR4500", {"price": 4500, "currency": "PKR"}),
    ("pkr 12,000", {"price": 12000, "currency": "PKR"}),
    ("₨ 3500", {"price": 3500, "currency": "PKR"}),
    ("4500/-", {"price": 4500, "currency": None}),
    ("4,500/-", {"price": 4500}),
    ("Rs 4500/-", {"price": 4500, "currency": "PKR"}),
    ("4500 rs", {"price": 4500, "currency": "PKR"}),
    ("4.5k", {"price": 4500}),
    ("12k only", {"price": 12000}),
    ("price 4500", {"price": 4500, "currency": None}),
    ("Price: 4500", {"price": 4500}),
    ("price: rs 4500", {"price": 4500, "currency": "PKR"}),
    ("demand 6000", {"price": 6000}),
    ("price 4500 final 4000", {"price": 4000}),
    ("was 5000 now 4200", {"price": 4200}),
    ("Rs 4500 Rs 6000", {"price": None, "price_ambiguous": True}),
    ("dm for price", {"price": None, "price_on_request": True}),
    ("Price in DM", {"price_on_request": True}),
    ("inbox for details", {"price_on_request": True}),
    ("Rs ۴۵۰۰", {"price": 4500}),
    ("Rs ٤٥٠٠", {"price": 4500}),
    ("Rs 4​500", {"price": 4500}),
    ("Rs 4,‎500", {"price": 4500}),
    ("size 42", {"price": None}),
    ("call 0300 1234567", {"price": None}),
    # size
    ("size 42", {"size_label": "EU 42", "size_value": Decimal("42"), "size_system": "EU"}),
    ("Size: 43", {"size_label": "EU 43"}),
    ("size-44", {"size_label": "EU 44"}),
    ("eu 42.5", {"size_label": "EU 42.5", "size_value": Decimal("42.5")}),
    ("EUR 41", {"size_label": "EU 41"}),
    ("42 eu", {"size_label": "EU 42"}),
    ("42.5 EUR", {"size_label": "EU 42.5"}),
    ("uk 8", {"size_label": "UK 8", "size_system": "UK"}),
    ("UK8.5", {"size_label": "UK 8.5"}),
    ("us 9", {"size_label": "US 9", "size_system": "US"}),
    ("sz:9 us", {"size_label": "US 9", "size_system": "US"}),
    ("size 9 uk", {"size_label": "UK 9"}),
    ("size 9", {"size_label": "9", "size_system": None}),
    ("cm 27", {"size_label": "CM 27", "size_system": "CM"}),
    ("27 cm insole", {"size_label": "CM 27"}),
    ("size 40-42", {"size_value": None, "size_ambiguous": True}),
    ("size 40/41", {"size_ambiguous": True}),
    ("eu 42 eu 43", {"size_ambiguous": True}),
    ("EUR 42 UK 8 US 9", {"size_label": "EU 42", "size_ambiguous": False}),
    ("size 42 uk 8", {"size_label": "EU 42"}),
    ("Rs 4500", {"size_label": None}),
    ("contact 03001234567", {"size_label": None}),
    # brand
    ("Nike Air Max 90", {"brand": "Nike"}),
    ("aj1 mocha", {"brand": "Jordan"}),
    ("AJ 4 military black", {"brand": "Jordan"}),
    ("air jordan 1 low", {"brand": "Jordan"}),
    ("nb 550 white green", {"brand": "New Balance"}),
    ("NB550", {"brand": "New Balance"}),
    ("new balance 2002r", {"brand": "New Balance"}),
    ("adidas samba og", {"brand": "Adidas"}),
    ("samba og cream", {"brand": "Adidas"}),
    ("af1 triple white", {"brand": "Nike"}),
    ("vans old skool", {"brand": "Vans"}),
    ("old skool black", {"brand": "Vans"}),
    ("chuck taylor 70", {"brand": "Converse"}),
    ("Dr. Martens 1460", {"brand": "Dr. Martens"}),
    ("dr martens boots", {"brand": "Dr. Martens"}),
    ("onitsuka tiger mexico 66", {"brand": "Onitsuka Tiger"}),
    ("Under Armour curry", {"brand": "Under Armour"}),
    ("off-white x nike", {"brand": "Off-White"}),
    ("bata shoes", {"brand": "Bata"}),
    ("batao size", {"brand": None}),
    ("caravans", {"brand": None}),
    ("sneakers for sale", {"brand": None}),
    # condition
    ("BNIB", {"condition": "new"}),
    ("brand new with box", {"condition": "new"}),
    ("DS", {"condition": "new"}),
    ("deadstock", {"condition": "new"}),
    ("vnds", {"condition": "like_new"}),
    ("like new", {"condition": "like_new"}),
    ("used once", {"condition": "used"}),
    ("worn twice 9/10", {"condition": "used", "condition_score": 9.0}),
    ("condition 8.5/10", {"condition_score": 8.5}),
    ("10/10", {"condition_score": 10.0}),
    ("12/10 date", {"condition_score": None}),
    # sold
    ("SOLD", {"is_sold": True}),
    ("sold out", {"is_sold": True}),
    ("not soldier", {"is_sold": False}),
    # combined
    ("Nike AF1 size 42 Rs 4500 bnib", {"brand": "Nike", "size_label": "EU 42", "price": 4500, "condition": "new"}),
    ("Jordan 4 | UK 8 | 9/10 | final 15k", {"brand": "Jordan", "size_label": "UK 8", "price": 15000,
                                            "condition_score": 9.0}),
]


@pytest.mark.parametrize("caption,expected", CASES, ids=[c for c, _ in CASES])
def test_parse_caption(caption, expected):
    got = parse_caption(caption).as_dict()
    assert {k: got[k] for k in expected} == expected


def test_table_size():
    assert len(CASES) >= 60


def test_empty_caption():
    assert parse_caption(None).as_dict() == parse_caption("").as_dict()
    assert parse_caption("").price is None


@pytest.mark.parametrize("value,system,expected", [
    (Decimal("42"), "EU", (Decimal("42"), False)),
    (Decimal("42.5"), "EU", (Decimal("42.5"), False)),
    (Decimal("8"), "UK", (Decimal("42"), True)),
    (Decimal("8.5"), "US", (Decimal("42"), True)),
    (Decimal("27"), "CM", (None, False)),
    (Decimal("9"), None, (None, False)),
    (Decimal("30"), "UK", (None, False)),
    (None, "EU", (None, False)),
])
def test_to_size_eu(value, system, expected):
    assert to_size_eu(value, system) == expected
