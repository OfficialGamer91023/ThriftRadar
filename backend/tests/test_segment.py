from app.pipeline.segment import SegMsg, segment_post


def imgs(*captions, missing=()):
    return [SegMsg(seq=i, kind="image", caption=c, image_id=None if i in missing else 100 + i)
            for i, c in enumerate(captions)]


def shape(result):
    segs, extra = result
    return [(s.idx, s.image_ids, s.caption) for s in segs], extra


def test_caption_on_first():
    assert shape(segment_post(imgs("Nike 42", None, None))) == ([(0, [100, 101, 102], "Nike 42")], "")


def test_caption_on_later_photo():
    assert shape(segment_post(imgs(None, None, "Nike 42"))) == ([(0, [100, 101, 102], "Nike 42")], "")


def test_no_caption():
    assert shape(segment_post(imgs(None, None))) == ([(0, [100, 101], None)], "")


def test_distinct_captions_each_photo():
    segs, _ = shape(segment_post(imgs("Nike 42", "Vans 40", "Puma 41")))
    assert segs == [(0, [100], "Nike 42"), (1, [101], "Vans 40"), (2, [102], "Puma 41")]


def test_identical_caption_pasted_on_all():
    assert shape(segment_post(imgs("Nike 42", "nike  42", "NIKE 42")))[0] == [(0, [100, 101, 102], "Nike 42")]


def test_identical_caption_with_uncaptioned_between():
    assert shape(segment_post(imgs("Nike 42", None, "Nike 42")))[0] == [(0, [100, 101, 102], "Nike 42")]


def test_leading_uncaptioned_plus_two_captioned():
    segs, _ = shape(segment_post(imgs(None, "Nike 42", None, "Vans 40", None)))
    assert segs == [(0, [100, 101, 102], "Nike 42"), (1, [103, 104], "Vans 40")]


def test_emoji_only_caption_is_uncaptioned():
    assert shape(segment_post(imgs("🔥🔥", "Nike 42")))[0] == [(0, [100, 101], "Nike 42")]


def test_text_messages_become_extra_text():
    msgs = [SegMsg(0, "text", "Price 4500"), *[SegMsg(i + 1, "image", None, 200 + i) for i in range(2)],
            SegMsg(3, "text", "  dm  ")]
    assert shape(segment_post(msgs)) == ([(0, [200, 201], None)], "Price 4500\ndm")


def test_missing_media_caption_still_starts_segment():
    segs, extra = shape(segment_post(imgs("Nike 42", None, "Vans 40", None, missing=(0,))))
    assert segs == [(0, [101], "Nike 42"), (1, [102, 103], "Vans 40")]


def test_segment_without_images_moves_caption_to_extra():
    segs, extra = shape(segment_post(imgs("Nike 42", "Vans 40", None, missing=(0,))))
    assert segs == [(0, [101, 102], "Vans 40")]
    assert extra == "Nike 42"


def test_seq_order_respected():
    msgs = list(reversed(imgs("Nike 42", None)))
    assert shape(segment_post(msgs))[0] == [(0, [100, 101], "Nike 42")]
