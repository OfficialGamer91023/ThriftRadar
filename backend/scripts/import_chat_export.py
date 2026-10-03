"""Backfill from a WhatsApp chat export. Spec: DESIGN.md §4.8 `import_chat_export.py`, §5.4.

Run from backend/:  uv run python -m scripts.import_chat_export data/exports/<folder-or-zip> --dry-run

Output never contains names, phone numbers or captions: only counts and sender_ref prefixes.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.grouping import Group, RawMsg, group_messages
from app.ids import idempotency_key
from app.images import MAX_FILE_BYTES, sniff_type
from app.pipeline.caption import parse_caption
from app.pipeline.segment import SegMsg, segment_post

REPO = Path(__file__).resolve().parents[2]
EXPORTS_ROOT = REPO / "data" / "exports"
REQUIRED_FIELDS = ("brand", "size", "price")

# $ per VLM call by model (DESIGN §3.1); Ollama models are free.
PER_CALL_COST = {
    "qwen/qwen2.5-vl-72b-instruct": 0.0020,
    "qwen/qwen3-vl-8b-instruct": 0.00034,
    "qwen/qwen3-vl-32b-instruct": 0.00033,
}
COMPARE_MODELS = ("qwen/qwen2.5-vl-72b-instruct", "qwen/qwen3-vl-8b-instruct")
LOCAL_SECONDS_PER_POST = (0.5, 1.5)  # Mac, DESIGN §3.2


class ImportError_(Exception):
    """Exit 2 with a message."""


class FormatError(ImportError_):
    pass


class NeedsDateOrder(ImportError_):
    pass


# ---------- export access ----------

@dataclass
class Export:
    root: Path
    chat_file: Path
    media_index: dict[str, Path]
    tmp_dir: Path | None = None

    def cleanup(self) -> None:
        if self.tmp_dir is not None:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


def _check_under_root(path: Path, exports_root: Path, override: bool) -> None:
    try:
        path.resolve().relative_to(exports_root.resolve())
    except ValueError:
        if not override:
            raise ImportError_(f"export must be under {exports_root} (gitignored); "
                               "pass --i-know-this-is-not-gitignored to override") from None


def _declared_entries(path: Path) -> int | None:
    """Entry count from the end-of-central-directory record; None if it's a ZIP64 placeholder or not found."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 66_000))
        tail = f.read()
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or i + 12 > len(tail):
        return None
    total = int.from_bytes(tail[i + 10:i + 12], "little")
    return None if total == 0xFFFF else total


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    for info in zf.infolist():
        parts = Path(info.filename).parts
        if info.filename.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
            raise ImportError_("zip contains unsafe paths (zip-slip); refusing")
    zf.extractall(dest)


def resolve_export_path(path: Path) -> Path:
    """A relative path that doesn't exist from the cwd is tried from the repo root (`data/exports/...`)."""
    path = Path(path)
    if not path.is_absolute() and not path.exists() and (REPO / path).exists():
        return REPO / path
    return path


def open_export(path: Path, exports_root: Path = EXPORTS_ROOT, override: bool = False) -> Export:
    path = resolve_export_path(path)
    _check_under_root(path, exports_root, override)
    if not path.exists():
        raise ImportError_(f"not found: {path}")
    tmp_dir = None
    root = path
    if path.is_file() and path.suffix.lower() == ".zip":
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
        tmp_dir = exports_root / ".tmp" / digest
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)
        with zipfile.ZipFile(path) as zf:
            declared = _declared_entries(path)
            if declared is not None and declared != len(zf.infolist()):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise ImportError_(
                    f"this zip's file index is broken ({declared} entries declared, {len(zf.infolist())} readable), "
                    "which happens with large iPhone exports. Unzip it in Finder (double-click) and pass the folder.")
            _safe_extract(zf, tmp_dir)
        root = tmp_dir

    files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
    texts = [p for p in files if p.suffix.lower() == ".txt"]
    chat = next((p for p in texts if p.name == "_chat.txt"), None)
    chat = chat or next((p for p in texts if p.name.lower().startswith("whatsapp")), None)
    if chat is None and len(texts) == 1:
        chat = texts[0]
    if chat is None:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ImportError_("no chat text file (_chat.txt or 'WhatsApp Chat with ….txt') in the export")
    media = {p.name: p for p in files if p != chat}
    return Export(root=root, chat_file=chat, media_index=media, tmp_dir=tmp_dir)


# ---------- lines and format ----------

_STRIP = re.compile("[﻿‎‏‪-‮⁦-⁩]")


def normalize_line(s: str) -> str:
    s = _STRIP.sub("", s)
    return s.replace(" ", " ").replace(" ", " ").replace("\r\n", "\n").rstrip("\r\n")


def read_lines(chat_file: Path) -> list[str]:
    text = chat_file.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    return [normalize_line(line) for line in text.split("\n")]


_DATE = r"(?P<d>\d{1,4}[./-]\d{1,2}[./-]\d{2,4})"
_TIME = r"(?P<t>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[AaPp]\.?\s?[Mm]\.?)?)"
HEADER_RES = {
    "ios": re.compile(r"^\[" + _DATE + r",?\s" + _TIME + r"\]\s(?P<rest>.*)$"),
    "android": re.compile(r"^" + _DATE + r",?\s" + _TIME + r"\s[-–]\s(?P<rest>.*)$"),
}


@dataclass(frozen=True)
class LineFormat:
    kind: str
    regex: re.Pattern
    date_order: str  # dmy | mdy | ymd
    has_seconds: bool
    is_12h: bool


def detect_format(lines: list[str], date_order: str = "auto") -> LineFormat:
    sample = [l for l in lines if l.strip()][:500]
    scores = {k: sum(1 for l in sample if rx.match(l)) for k, rx in HEADER_RES.items()}
    kind = max(scores, key=scores.get)
    if scores[kind] == 0:
        raise FormatError("no WhatsApp message headers recognised in the chat file")
    rx = HEADER_RES[kind]

    dates, times = [], []
    for l in lines:
        if m := rx.match(l):
            dates.append(re.split(r"[./-]", m.group("d")))
            times.append(m.group("t"))
    if date_order == "auto":
        if any(len(d[0]) == 4 for d in dates):
            date_order = "ymd"
        else:
            first = any(int(d[0]) > 12 for d in dates)
            second = any(int(d[1]) > 12 for d in dates)
            if first and second:
                raise FormatError("dates are inconsistent (both day-first and month-first seen)")
            if not first and not second:
                raise NeedsDateOrder("can't tell day/month order from this export; pass --date-order dmy or mdy")
            date_order = "dmy" if first else "mdy"
    has_seconds = any(len(re.split(r"[:.]", re.sub(r"\s?[AaPp].*$", "", t))) == 3 for t in times)
    is_12h = any(re.search(r"[AaPp]\.?\s?[Mm]", t) for t in times)
    return LineFormat(kind, rx, date_order, has_seconds, is_12h)


def _parse_ts(d: str, t: str, fmt: LineFormat, tz: ZoneInfo) -> datetime:
    a, b, c = (int(x) for x in re.split(r"[./-]", d))
    if fmt.date_order == "ymd":
        year, month, day = a, b, c
    elif fmt.date_order == "dmy":
        day, month, year = a, b, c
    else:
        month, day, year = a, b, c
    if year < 100:
        year += 2000
    ampm = re.search(r"([AaPp])\.?\s?[Mm]", t)
    clock = [int(x) for x in re.split(r"[:.]", re.sub(r"\s?[AaPp]\.?\s?[Mm]\.?$", "", t).strip())]
    hour, minute, second = clock[0], clock[1], clock[2] if len(clock) == 3 else 0
    if ampm:
        pm = ampm.group(1).lower() == "p"
        hour = (hour % 12) + (12 if pm else 0)
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


# ---------- messages ----------

@dataclass(frozen=True)
class ParsedMsg:
    header_raw: str
    sender_raw: str
    ts: datetime
    body: str


_SENDER = re.compile(r"^(?P<sender>[^:]{1,80}):\s(?P<body>.*)$")


def parse_messages(lines: list[str], fmt: LineFormat, tz: ZoneInfo) -> list[ParsedMsg]:
    out: list[dict] = []
    current: dict | None = None
    for line in lines:
        if m := fmt.regex.match(line):
            current = None
            if sm := _SENDER.match(m.group("rest")):
                current = {
                    "header_raw": f"{m.group('d')} {m.group('t')}",
                    "sender_raw": sm.group("sender").strip(),
                    "ts": _parse_ts(m.group("d"), m.group("t"), fmt, tz),
                    "body": sm.group("body"),
                }
                out.append(current)
            # else: a system message (joined, left, encryption notice…); skipped with its continuations
        elif current is not None:
            current["body"] += "\n" + line
    return [ParsedMsg(**m) for m in out]


_ATTACH_IOS = re.compile(r"<attached:\s*(?P<f>[^>]+)>(?P<after>.*)$")
_ATTACH_ANDROID = re.compile(r"^(?P<f>[\w-]+\.\w{2,5})\s*\(.*\)\s*$")
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|webp)$", re.IGNORECASE)
_NON_IMAGE_NAME = re.compile(r"(?:^|-)(?:STK|PTT|VID|AUD|STICKER|AUDIO|VIDEO|GIF)-", re.IGNORECASE)
_OMITTED = re.compile(r"^\s*(?:<media omitted>|image omitted|video omitted|sticker omitted|"
                      r"audio omitted|gif omitted|document omitted)\s*$", re.IGNORECASE)
_DELETED = re.compile(r"^\s*(?:this message was deleted\.?|you deleted this message\.?)\s*$", re.IGNORECASE)
_EDITED = re.compile(r"\s*<this message was edited>\s*$", re.IGNORECASE)


def classify_body(body: str, media_index: dict[str, Path]) -> tuple[str, str | None, str | None]:
    """-> (kind, filename, caption). kind: image | image_missing | other | omitted | deleted | text."""
    body = _EDITED.sub("", body)
    first, _, rest = body.partition("\n")
    filename, after = None, ""
    if m := _ATTACH_IOS.search(first):
        filename, after = m.group("f").strip(), m.group("after").strip()
    elif m := _ATTACH_ANDROID.match(first.strip()):
        filename = m.group("f")
    if filename:
        caption = "\n".join(x for x in (after, rest.strip()) if x) or None
        if not _IMAGE_EXT.search(filename) or _NON_IMAGE_NAME.search(filename):
            return "other", filename, None
        if filename in media_index:
            return "image", filename, caption
        return "image_missing", filename, caption
    if _OMITTED.match(first):
        kind = "omitted" if re.search(r"media|image", first, re.IGNORECASE) else "other"
        return kind, None, None
    if _DELETED.match(body):
        return "deleted", None, None
    return "text", None, body.strip() or None


def synthetic_key(chat_id: str, header_raw: str, sender_raw: str, filename_or_body: str, occurrence: int) -> str:
    raw = "\x1f".join((chat_id, header_raw, sender_raw, filename_or_body, str(occurrence)))
    return "exp:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


_PHONE = re.compile(r"^\+?[\d\s()-]{8,}$")


def sender_id_for_export(sender_raw: str) -> str:
    if _PHONE.match(sender_raw.strip()):
        return re.sub(r"\D", "", sender_raw) + "@s.whatsapp.net"
    return "name:" + sender_raw


@dataclass
class BuildCounts:
    images: int = 0
    missing_files: int = 0
    omitted: int = 0
    other_media: int = 0
    deleted: int = 0
    text: int = 0


_SKIPPED_COUNTER = {"omitted": "omitted", "other": "other_media", "deleted": "deleted"}


def build_messages(parsed: list[ParsedMsg], media_index: dict[str, Path], chat_id: str
                   ) -> tuple[list[RawMsg], BuildCounts]:
    counts = BuildCounts()
    seen: dict[tuple[str, str, str], int] = {}
    out: list[RawMsg] = []
    for p in parsed:
        kind, filename, caption = classify_body(p.body, media_index)
        if kind in _SKIPPED_COUNTER:
            name = _SKIPPED_COUNTER[kind]
            setattr(counts, name, getattr(counts, name) + 1)
            continue
        ident = filename if filename else p.body
        occ_key = (p.header_raw, p.sender_raw, ident)
        occurrence = seen.get(occ_key, 0)
        seen[occ_key] = occurrence + 1
        key = synthetic_key(chat_id, p.header_raw, p.sender_raw, ident, occurrence)
        common = dict(key=key, chat=chat_id, sender=sender_id_for_export(p.sender_raw), ts=p.ts.timestamp())
        if kind == "text":
            counts.text += 1
            out.append(RawMsg(kind="text", caption=caption, **common))
        else:
            missing = kind == "image_missing"
            counts.images += 1
            counts.missing_files += missing
            out.append(RawMsg(kind="image", caption=caption, missing=missing, **common,
                              extra={"filename": filename,
                                     "reject_reason": "download_failed" if missing else None}))
    return out, counts


def attach_media(msgs: list[RawMsg], export: Export, lazy: bool) -> list[RawMsg]:
    """Size and type checks. Lazy (dry run): stat and extension only."""
    out = []
    for m in msgs:
        if m.kind != "image" or m.missing:
            out.append(m)
            continue
        path = export.media_index[m.extra["filename"]]
        size = path.stat().st_size
        reason = None
        if size > MAX_FILE_BYTES:
            reason = "too_large"
        elif not lazy:
            with open(path, "rb") as f:
                if sniff_type(f.read(16)) is None:
                    reason = "bad_type"
        extra = m.extra | {"path": path, "reject_reason": reason}
        out.append(RawMsg(m.key, m.chat, m.sender, m.ts, m.kind, m.caption, size, reason is not None, extra))
    return out


# ---------- estimate ----------

@dataclass
class DryRunReport:
    posts: int = 0
    segments: int = 0
    images: int = 0
    omitted: int = 0
    missing_files: int = 0
    dropped_text_only: int = 0
    dropped_all_missing: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    likely_vlm: int = 0
    lower_vlm: int = 0
    field_hits: dict = field(default_factory=lambda: {f: 0 for f in REQUIRED_FIELDS})

    @property
    def est_calls(self) -> tuple[int, int]:
        return round(self.lower_vlm * 1.1), round(self.likely_vlm * 1.1)


def _missing_fields(caption: str | None, extra_text: str) -> list[str]:
    seg = parse_caption(caption)
    post = parse_caption(extra_text)
    have = {
        "brand": bool(seg.brand or post.brand),
        "size": bool(seg.size_value or post.size_value),
        "price": bool(seg.price or seg.price_on_request or post.price or post.price_on_request),
    }
    return [f for f in REQUIRED_FIELDS if not have[f]]


def estimate(groups: list[Group]) -> DryRunReport:
    r = DryRunReport(posts=len(groups))
    for g in groups:
        msgs = [SegMsg(seq=i, kind=m.kind, caption=m.caption,
                       image_id=None if (m.kind != "image" or m.missing) else i)
                for i, m in enumerate(g.messages)]
        segments, extra_text = segment_post(msgs)
        r.segments += len(segments)
        r.images += sum(1 for m in g.messages if m.kind == "image" and not m.missing)
        r.first_ts = g.first_ts if r.first_ts is None else min(r.first_ts, g.first_ts)
        r.last_ts = g.last_ts if r.last_ts is None else max(r.last_ts, g.last_ts)
        for seg in segments:
            missing = _missing_fields(seg.caption, extra_text)
            for f in REQUIRED_FIELDS:
                r.field_hits[f] += f not in missing
            r.likely_vlm += bool(missing)  # multi_item needs the detector; caption-only can't see it
            r.lower_vlm += len(missing) >= 2
    return r


def _cost(model: str) -> float | None:
    if model in PER_CALL_COST:
        return PER_CALL_COST[model]
    if "/" not in model:
        return 0.0  # an Ollama tag such as qwen2.5vl:7b
    return None


def format_report(r: DryRunReport, model: str, tz: ZoneInfo) -> str:
    lo, hi = r.est_calls
    def day(ts):
        return datetime.fromtimestamp(ts, tz).date().isoformat() if ts is not None else "-"
    lines = [
        f"posts:               {r.posts}",
        f"segments (items):    {r.segments}",
        f"images:              {r.images}",
        f"images omitted:      {r.omitted}",
        f"image files missing: {r.missing_files}",
        f"dropped text-only:   {r.dropped_text_only}",
        f"dropped all-missing: {r.dropped_all_missing}",
        f"date range:          {day(r.first_ts)} .. {day(r.last_ts)}",
        "caption coverage:    " + ", ".join(
            f"{f} {100 * n / r.segments:.0f}%" if r.segments else f"{f} -" for f, n in r.field_hits.items()),
        f"est VLM calls:       {lo} .. {hi}  (upper bound: OCR, SigLIP and kNN fill some fields)",
    ]
    for m in dict.fromkeys([model, *COMPARE_MODELS]):
        c = _cost(m)
        lines.append(f"est cost {m}: " + (f"${lo * c:.2f} .. ${hi * c:.2f}" if c is not None else "unknown model"))
    a, b = LOCAL_SECONDS_PER_POST
    lines.append(f"est local CPU time:  {r.posts * a / 60:.0f} .. {r.posts * b / 60:.0f} min")
    return "\n".join(lines)


# ---------- posting (build step 6) ----------

MAX_MESSAGES_PER_POST = 60
MAX_CAPTION = 4096
RETRY_SLEEPS = (2, 8, 30)


def build_payload(group: Group, tz: ZoneInfo, no_vlm: bool) -> tuple[dict, list[tuple[str, tuple]]]:
    """The same multipart the listener sends (DESIGN §4.1 Poster), with source='chat_export'."""
    images = [m for m in group.messages if m.kind == "image"]
    texts = [m for m in group.messages if m.kind == "text"]
    keep = set(id(m) for m in images + texts[: max(0, MAX_MESSAGES_PER_POST - len(images))])
    messages, files = [], []
    for m in group.messages:
        if id(m) not in keep:
            continue
        entry = {
            "key": m.key,
            "kind": m.kind,
            "sent_at": datetime.fromtimestamp(m.ts, tz).isoformat(),
            "caption": m.caption[:MAX_CAPTION] if m.caption else None,
        }
        if m.kind == "image":
            if m.missing:
                entry |= {"missing_media": True, "reject_reason": m.extra.get("reject_reason") or "download_failed"}
            else:
                field_name = f"f{len(files)}"
                entry["file_field"] = field_name
                files.append((field_name, (f"{field_name}.jpg", m.extra["path"].read_bytes(), "application/octet-stream")))
        messages.append(entry)
    meta = {
        "source": "chat_export",
        "chat_id": group.chat,
        "sender_id": group.sender,
        "vlm_policy": "never" if no_vlm else "auto",
        "messages": messages,
    }
    return meta, files


def post_group(client: httpx.Client, group: Group, args, tz: ZoneInfo, token: str,
               sleep=time.sleep) -> str:
    """-> accepted | duplicate | rejected | failed. 3 attempts on 5xx/network, 4xx is final."""
    meta, files = build_payload(group, tz, args.no_vlm)
    key = idempotency_key("chat_export", group.chat, [m["key"] for m in meta["messages"]])
    headers = {"X-Ingest-Token": token, "Idempotency-Key": key}
    for attempt in range(len(RETRY_SLEEPS) + 1):
        try:
            r = client.post(args.ingest_url, headers=headers, data={"meta": json.dumps(meta)}, files=files)
            if r.status_code == 202:
                return "accepted"
            if r.status_code == 200:
                return "duplicate"
            if 400 <= r.status_code < 500:
                print(f"rejected sender={_ref(group.sender)} status={r.status_code} "
                      f"detail={r.json().get('detail', '?') if r.headers.get('content-type', '').startswith('application/json') else '?'}",
                      file=sys.stderr)
                return "rejected"
        except httpx.HTTPError:
            pass
        if attempt < len(RETRY_SLEEPS):
            sleep(RETRY_SLEEPS[attempt])
    return "failed"


def _ref(sender: str) -> str:
    """Short non-reversible tag for warnings; never the name or number."""
    return hashlib.sha256(sender.encode()).hexdigest()[:8]


def wait_for_queue(client: httpx.Client, queue_url: str, token: str, max_received: int,
                   sleep=time.sleep, poll_s: float = 5) -> None:
    while True:
        r = client.get(queue_url, headers={"X-Ingest-Token": token})
        r.raise_for_status()
        if r.json().get("received", 0) <= max_received:
            return
        sleep(poll_s)


def run_import(groups: list[Group], args, client: httpx.Client, tz: ZoneInfo, token: str,
               sleep=time.sleep) -> dict[str, int]:
    queue_url = args.ingest_url.rsplit("/ingest", 1)[0] + "/admin/queue"
    summary = {"accepted": 0, "duplicate": 0, "rejected": 0, "failed": 0}
    for i, g in enumerate(groups):
        wait_for_queue(client, queue_url, token, args.max_queue, sleep)
        summary[post_group(client, g, args, tz, token, sleep)] += 1
        if i + 1 < len(groups) and args.rate > 0:
            sleep(1 / args.rate)
    return summary


# ---------- main ----------

def build_groups(args, export: Export, tz: ZoneInfo) -> tuple[list[Group], DryRunReport]:
    lines = read_lines(export.chat_file)
    fmt = detect_format(lines, args.date_order)
    parsed = parse_messages(lines, fmt, tz)
    chat_id = args.chat_id or "export:" + hashlib.sha256(Path(args.export_path).name.encode()).hexdigest()[:16]
    msgs, counts = build_messages(parsed, export.media_index, chat_id)
    if counts.omitted and counts.images == 0:
        raise ImportError_("this export was made without media; re-export the chat with 'Include media'")
    if counts.omitted:
        with_media = [m.ts for m in msgs if m.kind == "image" and not m.missing] or [m.ts for m in msgs]
        print(f"warning: {counts.omitted} images were omitted from the export; photos are present only "
              f"{_day(min(with_media), tz)} .. {_day(max(with_media), tz)}. Posts outside that range are dropped.",
              file=sys.stderr)
    msgs = attach_media(msgs, export, lazy=args.dry_run)
    stats: dict = {}
    groups = group_messages(msgs, stats=stats)
    if args.since:
        groups = [g for g in groups if g.first_ts >= _date_ts(args.since, tz)]
    if args.before:
        groups = [g for g in groups if g.first_ts < _date_ts(args.before, tz)]
    if args.limit is not None:
        groups = groups[: args.limit]
    report = estimate(groups)
    report.omitted = counts.omitted
    report.missing_files = counts.missing_files
    report.dropped_text_only = stats.get("dropped_text_only", 0)
    report.dropped_all_missing = stats.get("dropped_all_missing", 0)
    return groups, report


def _day(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).date().isoformat()


def _date_ts(value: str, tz: ZoneInfo) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=tz).timestamp()


def _loopback_only(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise argparse.ArgumentTypeError("--ingest-url must be loopback")
    return url


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m scripts.import_chat_export")
    ap.add_argument("export_path", type=Path)
    ap.add_argument("--chat-id")
    ap.add_argument("--date-order", choices=["auto", "dmy", "mdy", "ymd"], default="auto")
    ap.add_argument("--tz", default=None, help="IANA zone of the phone that made the export; default: system zone")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--since")
    ap.add_argument("--before")
    ap.add_argument("--no-vlm", action="store_true")
    ap.add_argument("--rate", type=float, default=2.0)
    ap.add_argument("--max-queue", type=int, default=20)
    ap.add_argument("--ingest-url", type=_loopback_only, default="http://127.0.0.1:8000/ingest")
    ap.add_argument("--model", default=os.environ.get("VLM_MODEL", "qwen2.5vl:7b"))
    ap.add_argument("--keep", action="store_true", help="keep the extracted zip under data/exports/.tmp")
    ap.add_argument("--i-know-this-is-not-gitignored", dest="override_root", action="store_true")
    return ap.parse_args(argv)


def _system_tz() -> ZoneInfo:
    name = os.environ.get("TZ")
    if not name:
        try:
            name = "/".join(Path(os.path.realpath("/etc/localtime")).parts[-2:])
            return ZoneInfo(name)
        except Exception:
            name = "UTC"
    return ZoneInfo(name)


def main(argv: list[str] | None = None, exports_root: Path = EXPORTS_ROOT) -> int:
    args = parse_args(argv)
    tz = ZoneInfo(args.tz) if args.tz else _system_tz()
    try:
        export = open_export(args.export_path, exports_root, args.override_root)
    except ImportError_ as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        groups, report = build_groups(args, export, tz)
        print(format_report(report, args.model, tz))
        if args.dry_run:
            return 0
        token = os.environ.get("INGEST_TOKEN")
        if not token:
            print("error: INGEST_TOKEN must be set (same value as the backend's)", file=sys.stderr)
            return 2
        with httpx.Client(timeout=120) as client:
            summary = run_import(groups, args, client, tz, token)
        print("summary: " + " ".join(f"{k}={v}" for k, v in summary.items()))
        return 0 if summary["failed"] == 0 else 1
    except ImportError_ as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            export.cleanup()


if __name__ == "__main__":
    sys.exit(main())
