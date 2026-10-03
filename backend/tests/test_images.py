import io

import pytest
from PIL import Image

from app.images import InvalidImage, hamming64, normalize_image, phash64, sniff_type
from tests.imgutil import encode, pattern


@pytest.mark.parametrize("fmt,kind", [("JPEG", "jpeg"), ("PNG", "png"), ("WEBP", "webp")])
def test_accepted_formats(fmt, kind):
    data = encode(pattern(1), fmt)
    assert sniff_type(data[:16]) == kind
    out = normalize_image(data, max_side=2048)
    assert sniff_type(out.jpeg[:16]) == "jpeg"
    assert (out.width, out.height) == (640, 480)
    assert len(out.sha256) == 32


@pytest.mark.parametrize("data", [
    encode(pattern(1), "GIF"),
    b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64,
    b"%PDF-1.4\n" + b"\x00" * 64,
    b"just some text pretending to be a jpg",
])
def test_rejected_types(data):
    assert sniff_type(data[:16]) is None
    with pytest.raises(InvalidImage) as e:
        normalize_image(data, max_side=2048)
    assert e.value.reason == "bad_type"


def test_too_large_checked_first():
    with pytest.raises(InvalidImage) as e:
        normalize_image(b"\xff\xd8\xff" + b"\x00" * 100, max_side=2048, max_bytes=50)
    assert e.value.reason == "too_large"


def test_truncated_jpeg_is_corrupt():
    data = encode(pattern(2))
    with pytest.raises(InvalidImage) as e:
        normalize_image(data[: len(data) // 2], max_side=2048)
    assert e.value.reason == "corrupt"


def test_decompression_bomb():
    data = encode(Image.new("1", (8000, 8000)), "PNG")  # 64M pixels > 40M limit, tiny file
    with pytest.raises(InvalidImage) as e:
        normalize_image(data, max_side=2048)
    assert e.value.reason == "bomb"


def test_exif_gps_removed():
    img = pattern(3)
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    exif[0x8825] = {1: "N", 2: (31.0, 30.0, 0.0)}  # GPSInfo
    data = encode(img, exif=exif)
    assert Image.open(io.BytesIO(data)).getexif()  # input really has EXIF
    out = normalize_image(data, max_side=2048)
    assert not Image.open(io.BytesIO(out.jpeg)).getexif()
    assert b"PhoneMaker" not in out.jpeg


def test_exif_rotation_applied():
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° CW
    data = encode(pattern(4, (200, 100)), exif=exif)
    out = normalize_image(data, max_side=2048)
    assert (out.width, out.height) == (100, 200)


def test_output_bounded_by_max_side():
    out = normalize_image(encode(pattern(5, (3000, 1000))), max_side=1024)
    assert max(out.width, out.height) == 1024


def test_normalize_is_deterministic():
    data = encode(pattern(6))
    assert normalize_image(data, 2048).sha256 == normalize_image(data, 2048).sha256


def test_phash_distances():
    img = pattern(7)
    h = phash64(img)
    assert hamming64(h, h) == 0
    requality = Image.open(io.BytesIO(encode(img, quality=70)))
    assert hamming64(h, phash64(requality)) <= 4
    half = img.resize((img.width // 2, img.height // 2))
    assert hamming64(h, phash64(half)) <= 6
    assert hamming64(h, phash64(pattern(8))) > 12


def test_phash_fits_signed_int64():
    for seed in range(40):
        v = phash64(pattern(seed))
        assert -(1 << 63) <= v < (1 << 63)
