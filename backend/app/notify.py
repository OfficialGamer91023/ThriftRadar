"""macOS notifications for wishlist matches. Spec: DESIGN.md §4.6 `get_notifier`, `MacNotifier`, `notify_match`."""

import logging
import re
import subprocess
import sys

import psycopg

log = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
OSASCRIPT = "/usr/bin/osascript"


class NullNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> bool:
        self.sent.append((title, body))
        return True


class MacNotifier:
    def __init__(self, settings, platform: str | None = None):
        platform = platform or sys.platform
        # Second check: even direct construction can't reach osascript in demo mode or off macOS.
        if platform != "darwin" or settings.demo_mode or not settings.notify_macos:
            raise RuntimeError("MacNotifier needs macOS, local mode and NOTIFY_MACOS=1")

    def send(self, title: str, body: str, run=subprocess.run) -> bool:
        title = _CONTROL.sub(" ", title)[:80]
        body = _CONTROL.sub(" ", body)[:200]
        # Text goes in as argv, never into the script: listing text comes from sellers and the VLM.
        r = run([OSASCRIPT, "-e", "on run argv",
                 "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                 "-e", "end run", title, body],
                timeout=5, check=False, capture_output=True)
        if r.returncode != 0:
            log.warning("notify osascript rc=%s", r.returncode)
        return r.returncode == 0


def get_notifier(settings):
    if sys.platform == "darwin" and not settings.demo_mode and settings.notify_macos:
        return MacNotifier(settings)
    return NullNotifier()


def describe(row: dict) -> tuple[str, str]:
    """Title and body from listing fields only: never seller data."""
    name = " ".join(x for x in (row.get("brand"), row.get("model")) if x) or "A shoe"
    parts = []
    if row.get("size_label"):
        parts.append(f"Size {row['size_label']}" + (" (read by AI)" if row.get("size_src") == "vlm" else ""))
    else:
        parts.append("size unknown")
    if row.get("price_amount") is not None:
        cur = row.get("currency") or ""
        parts.append(f"{'Rs' if cur in ('PKR', '') else cur} {row['price_amount']:,}")
    if row.get("repost_count"):
        parts.append(f"posted {row['repost_count'] + 1}×")
    return f"ThriftRadar: {name}", " · ".join(parts)


def notify_match(conn: psycopg.Connection, notifier, match_id: int) -> bool:
    """At least once: a crash between send and the UPDATE can repeat one notification; missing a deal is worse."""
    row = conn.execute(
        """SELECT m.notified_at, l.brand, l.model, l.size_label, l.attr_sources->>'size', l.price_amount, l.currency,
                  l.repost_count
           FROM matches m JOIN listings l ON l.id = m.listing_id WHERE m.id = %s""", (match_id,)).fetchone()
    if row is None or row[0] is not None:
        return False
    title, body = describe(dict(zip(("brand", "model", "size_label", "size_src", "price_amount", "currency",
                                     "repost_count"), row[1:])))
    ok = notifier.send(title, body)
    conn.execute(
        """UPDATE matches SET notify_attempts = notify_attempts + 1,
                  notified_at = CASE WHEN %s THEN now() ELSE notified_at END
           WHERE id = %s AND notified_at IS NULL""", (ok, match_id))
    log.info("notify match_id=%s ok=%s", match_id, ok)
    return ok
