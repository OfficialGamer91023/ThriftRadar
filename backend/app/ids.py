"""Stable ids and HMAC refs. Spec: DESIGN.md §4.2."""

import hashlib
import hmac
import re

_DEVICE_SUFFIX = re.compile(r":\d+@")


def idempotency_key(source: str, chat_id: str, msg_keys: list[str]) -> str:
    """Same formula as the listener's idemKey (cross-language fixture test)."""
    body = f"{source}\n{chat_id}\n" + "\n".join(sorted(set(msg_keys)))
    return hashlib.sha256(body.encode()).hexdigest()


def _normalize_jid(jid: str) -> str:
    return _DEVICE_SUFFIX.sub("@", jid.strip().lower())


def _ref(prefix: str, value: str, key: str) -> str:
    if not key:
        raise ValueError("HMAC key is empty")
    return hmac.new(key.encode(), (prefix + _normalize_jid(value)).encode(), hashlib.sha256).hexdigest()[:32]


def sender_ref(sender_id: str, key: str) -> str:
    return _ref("s:", sender_id, key)


def chat_ref(chat_id: str, key: str) -> str:
    return _ref("c:", chat_id, key)
