"""Model wrappers. Pure helpers always run; `models`-marked tests need the baked weights in data/models."""

from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.pipeline.detect import Det, primary_box
from app.pipeline.embed import l2_normalize, segment_embedding, zero_shot_brand
from app.pipeline.ocr import parse_size_tag

MODELS_DIR = Path(__file__).resolve().parents[2] / "data" / "models"
needs_weights = pytest.mark.skipif(not (MODELS_DIR / "yolow-shoe.pt").exists(), reason="run scripts.bake_models first")


# ---------- pure ----------

def test_primary_box_none():
    assert primary_box([], 100, 100) == (None, False)


def test_primary_box_pair_is_union_padded():
    dets = [Det((10, 10, 40, 40), 0.9, "shoe"), Det((50, 20, 90, 60), 0.8, "shoe")]
    box, multi = primary_box(dets, 100, 100)
    assert multi is False
    assert box == [3, 6, 96, 64]  # union (10,10,90,60) padded 8% of 80×50, clipped


def test_primary_box_many_is_largest_and_multi():
    dets = [Det((0, 0, 10, 10), 0.9, "shoe"), Det((20, 20, 80, 80), 0.5, "boot"), Det((85, 85, 95, 95), 0.9, "shoe")]
    box, multi = primary_box(dets, 100, 100)
    assert multi is True and box == [15, 15, 85, 85]


def test_segment_embedding_is_normalized_mean():
    v = l2_normalize(np.array([[1.0, 0, 0], [0, 1.0, 0]], dtype=np.float32))
    out = segment_embedding(v)
    assert np.allclose(out, [2 ** -0.5, 2 ** -0.5, 0]) and np.isclose(np.linalg.norm(out), 1)


def test_zero_shot_brand_needs_both_the_floor_and_the_margin():
    emb = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    names = ["Converse", "Vans", "Nike"]
    bt = lambda *top: np.array([[c, 0, 0] for c in top], dtype=np.float32)
    assert zero_shot_brand(bt(0.12, 0.07, 0.01), names, emb, 0.08, 0.03) == "Converse"
    assert zero_shot_brand(bt(0.12, 0.10, 0.01), names, emb, 0.08, 0.03) is None  # runner-up too close
    assert zero_shot_brand(bt(0.07, 0.01, 0.00), names, emb, 0.08, 0.03) is None  # below the floor
    assert zero_shot_brand(None, [], emb, 0.08, 0.03) is None


@pytest.mark.parametrize("lines,expected", [
    ([("EUR 42 UK 8 US 9", 0.95)], ("EU", Decimal("42"))),
    ([("US 9", 0.9), ("UK 8.5", 0.9)], ("UK", Decimal("8.5"))),
    ([("EUR 42", 0.9), ("EUR 43", 0.9)], None),
    ([("EUR 42", 0.3)], None),
    ([("ART 123456 7 8", 0.99)], None),
    ([("EU 42,5", 0.9)], ("EU", Decimal("42.5"))),
])
def test_parse_size_tag(lines, expected):
    g = parse_size_tag(lines)
    got = (g.size_system, g.size_value) if g and g.size_value is not None else None
    assert got == expected


def test_parse_size_tag_brand_only():
    g = parse_size_tag([("NIKE", 0.9), ("ART 123", 0.9)])
    assert g.brand == "Nike" and g.size_value is None


# ---------- real weights ----------

@pytest.fixture(scope="module")
def registry():
    from app.pipeline.models import ModelRegistry

    r = ModelRegistry(MODELS_DIR, demo=False)
    r.load()
    return r


@pytest.mark.models
@needs_weights
def test_registry_loads_and_brand_text(registry):
    assert registry.ready.is_set()
    assert registry.brand_text.shape == (len(registry.brand_names), 768)


@pytest.mark.models
@needs_weights
def test_embedder_text_image_shapes_and_similarity(registry):
    e = registry.embedder
    t = e.embed_text(["white sneakers", "white sneakers", "a red car"])
    assert t.shape == (3, 768) and np.allclose(np.linalg.norm(t, axis=1), 1, atol=1e-4)
    assert t[0] @ t[1] > 0.99 and t[0] @ t[2] < t[0] @ t[1]
    img = Image.new("RGB", (300, 200), "white")
    assert e.embed_images([img]).shape == (1, 768)


@pytest.mark.models
@needs_weights
def test_detector_returns_original_coordinates(registry):
    img = Image.new("RGB", (1200, 800), "white")
    out = registry.detector.detect([img, img])
    assert len(out) == 2 and all(isinstance(d, list) for d in out)


@pytest.mark.models
@needs_weights
def test_ocr_reads_printed_size_tag(registry):
    img = Image.new("RGB", (600, 160), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 40), "EUR 42  UK 8  US 9", fill="black", font_size=48)
    lines = registry.ocr.read(img)
    g = parse_size_tag(lines)
    assert g is not None and (g.size_system, g.size_value) == ("EU", Decimal("42")), lines
