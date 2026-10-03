import pytest

from app.ids import chat_ref, idempotency_key, sender_ref

KEY = "test-key"


def test_idempotency_key_order_insensitive_and_dedupes():
    a = idempotency_key("whatsapp", "123@g.us", ["m2", "m1", "m3"])
    assert a == idempotency_key("whatsapp", "123@g.us", ["m3", "m1", "m2", "m1"])
    assert len(a) == 64 and int(a, 16) >= 0


def test_idempotency_key_differs_by_source_and_chat():
    base = idempotency_key("whatsapp", "123@g.us", ["m1"])
    assert base != idempotency_key("chat_export", "123@g.us", ["m1"])
    assert base != idempotency_key("whatsapp", "456@g.us", ["m1"])


def test_idempotency_key_known_vector():
    # Pinned so the listener's JS twin can assert the same value.
    assert idempotency_key("whatsapp", "c", ["b", "a"]) == __import__("hashlib").sha256(b"whatsapp\nc\na\nb").hexdigest()


def test_refs_normalize_device_suffix_and_case():
    assert sender_ref("923001234567:12@s.whatsapp.net", KEY) == sender_ref("923001234567@S.whatsapp.net", KEY)
    assert len(sender_ref("x@lid", KEY)) == 32


def test_sender_and_chat_refs_are_domain_separated():
    assert sender_ref("same@x", KEY) != chat_ref("same@x", KEY)
    assert sender_ref("same@x", KEY) != sender_ref("same@x", "other-key")


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        sender_ref("x@lid", "")
