"""Importer parsing and dry-run (DESIGN §6.4). All chat lines here are synthetic."""

import zipfile

import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts import import_chat_export as imp
from tests.imgutil import encode, pattern

TZ = ZoneInfo("Asia/Karachi")
NNBSP = " "
LRM = "‎"


def fmt_of(text, order="auto"):
    lines = [imp.normalize_line(l) for l in text.split("\n")]
    return lines, imp.detect_format(lines, order)


def parsed(text, order="auto"):
    lines, fmt = fmt_of(text, order)
    return imp.parse_messages(lines, fmt, TZ)


# ---------- format detection and timestamps ----------

def test_ios_en_us_12h_with_narrow_nbsp():
    text = f"[3/14/24, 1:05:09{NNBSP}PM] Seller One: {LRM}<attached: 00000012-PHOTO-2024-03-14-13-05-09.jpg>"
    lines, fmt = fmt_of(text)
    assert (fmt.kind, fmt.date_order, fmt.has_seconds, fmt.is_12h) == ("ios", "mdy", True, True)
    assert imp.parse_messages(lines, fmt, TZ)[0].ts == datetime(2024, 3, 14, 13, 5, 9, tzinfo=TZ)


def test_ios_en_gb_24h():
    msgs = parsed("[14/03/2024, 13:05:09] Seller One: hello")
    assert msgs[0].ts == datetime(2024, 3, 14, 13, 5, 9, tzinfo=TZ)
    assert msgs[0].sender_raw == "Seller One" and msgs[0].body == "hello"


def test_android_en_in_lowercase_ampm():
    lines, fmt = fmt_of("14/03/24, 1:05 pm - Seller One: hello\n15/03/24, 12:10 am - Seller One: late")
    assert (fmt.kind, fmt.date_order, fmt.has_seconds, fmt.is_12h) == ("android", "dmy", False, True)
    msgs = imp.parse_messages(lines, fmt, TZ)
    assert msgs[0].ts == datetime(2024, 3, 14, 13, 5, tzinfo=TZ)
    assert msgs[1].ts == datetime(2024, 3, 15, 0, 10, tzinfo=TZ)


def test_android_de_de_dots():
    msgs = parsed("14.03.24, 13:05 - Verkäufer: IMG-20240314-WA0012.jpg (Datei angehängt)")
    assert msgs[0].ts == datetime(2024, 3, 14, 13, 5, tzinfo=TZ)


def test_iso_dates():
    lines, fmt = fmt_of("[2024-03-04, 13:05:09] Seller One: hi")
    assert fmt.date_order == "ymd"
    assert imp.parse_messages(lines, fmt, TZ)[0].ts.day == 4


def test_all_days_le_12_requires_date_order():
    text = "01/02/24, 13:00 - A: x\n03/04/24, 13:00 - A: y"
    with pytest.raises(imp.NeedsDateOrder):
        fmt_of(text)
    _, fmt = fmt_of(text, "mdy")
    assert fmt.date_order == "mdy"


def test_inconsistent_dates_rejected():
    with pytest.raises(imp.FormatError):
        fmt_of("13/01/24, 13:00 - A: x\n01/13/24, 13:00 - A: y")


def test_not_a_chat_file():
    with pytest.raises(imp.FormatError):
        fmt_of("just some notes\nnothing here")


# ---------- message structure ----------

def test_multiline_caption_and_system_messages():
    text = (
        "14/03/24, 13:00 - Messages and calls are end-to-end encrypted. Tap to learn more.\n"
        "14/03/24, 13:01 - Seller One added Seller Two\n"
        "14/03/24, 13:02 - Seller One: IMG-20240314-WA0001.jpg (file attached)\n"
        "Nike AF1\nsize 42\n"
        "14/03/24, 13:03 - Seller One: second"
    )
    msgs = parsed(text)
    assert [m.body for m in msgs] == ["IMG-20240314-WA0001.jpg (file attached)\nNike AF1\nsize 42", "second"]


MEDIA = {"IMG-20240314-WA0001.jpg": None, "00000012-PHOTO-2024-03-14-13-05-09.jpg": None}


@pytest.mark.parametrize("body,expected", [
    ("IMG-20240314-WA0001.jpg (file attached)\nNike size 42", ("image", "IMG-20240314-WA0001.jpg", "Nike size 42")),
    ("IMG-20240314-WA0001.jpg (Datei angehängt)", ("image", "IMG-20240314-WA0001.jpg", None)),
    ("IMG-20240314-WA0001.jpg (fichier joint)", ("image", "IMG-20240314-WA0001.jpg", None)),
    ("<attached: 00000012-PHOTO-2024-03-14-13-05-09.jpg>", ("image", "00000012-PHOTO-2024-03-14-13-05-09.jpg", None)),
    ("<attached: 00000012-PHOTO-2024-03-14-13-05-09.jpg>\nVans 40", ("image", "00000012-PHOTO-2024-03-14-13-05-09.jpg", "Vans 40")),
    ("IMG-20240314-WA0099.jpg (file attached)", ("image_missing", "IMG-20240314-WA0099.jpg", None)),
    ("PTT-20240314-WA0002.opus (file attached)", ("other", "PTT-20240314-WA0002.opus", None)),
    ("VID-20240314-WA0003.mp4 (file attached)", ("other", "VID-20240314-WA0003.mp4", None)),
    ("STK-20240314-WA0004.webp (file attached)", ("other", "STK-20240314-WA0004.webp", None)),
    ("<attached: 00000013-STICKER-2024.webp>", ("other", "00000013-STICKER-2024.webp", None)),
    ("<Media omitted>", ("omitted", None, None)),
    ("image omitted", ("omitted", None, None)),
    ("video omitted", ("other", None, None)),
    ("This message was deleted", ("deleted", None, None)),
    ("You deleted this message", ("deleted", None, None)),
    ("still available? <This message was edited>", ("text", None, "still available?")),
    ("price 4500", ("text", None, "price 4500")),
])
def test_classify_body(body, expected):
    assert imp.classify_body(body, MEDIA) == expected


def test_lrm_before_attached_is_stripped():
    line = imp.normalize_line(f"[14/03/2024, 13:05:09] Seller One: {LRM}<attached: 00000012-PHOTO-2024-03-14-13-05-09.jpg>")
    assert "<attached:" in line and LRM not in line


def test_synthetic_keys_stable_and_occurrence_disambiguates():
    text = "14/03/24, 13:00 - A: same\n14/03/24, 13:00 - A: same\n14/03/24, 13:00 - B: same"
    msgs, _ = imp.build_messages(parsed(text), {}, "chat")
    again, _ = imp.build_messages(parsed(text), {}, "chat")
    assert [m.key for m in msgs] == [m.key for m in again]
    assert len({m.key for m in msgs}) == 3
    assert all(m.key.startswith("exp:") and len(m.key) == 36 for m in msgs)


def test_sender_ids():
    assert imp.sender_id_for_export("+92 300 1234567") == "923001234567@s.whatsapp.net"
    assert imp.sender_id_for_export("Seller One") == "name:Seller One"


# ---------- end to end (dry run) ----------

def _write_export(root, name="Thrift Group", omitted=0):
    folder = root / name
    folder.mkdir(parents=True)
    lines = [
        "14/03/24, 13:00 - Messages and calls are end-to-end encrypted.",
        "14/03/24, 13:00 - Seller One: IMG-20240314-WA0001.jpg (file attached)",
        "Nike AF1 size 42 Rs 4500",
        "14/03/24, 13:00 - Seller One: IMG-20240314-WA0002.jpg (file attached)",
        "14/03/24, 13:01 - Buyer: is this available?",
        "14/03/24, 13:30 - Seller Two: IMG-20240314-WA0003.jpg (file attached)",
        "Vans",
        "14/03/24, 13:31 - Seller Two: IMG-20240314-WA0004.jpg (file attached)",
        "Puma size 41",
        "14/03/24, 14:00 - +92 300 1234567: IMG-20240314-WA0005.jpg (file attached)",
        "15/03/24, 09:00 - Seller One: IMG-20240315-WA0006.jpg (file attached)",
        "15/03/24, 09:00 - Seller One: VID-20240315-WA0007.mp4 (file attached)",
    ] + ["15/03/24, 10:00 - Seller Three: <Media omitted>"] * omitted
    (folder / "WhatsApp Chat with Thrift Group.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for i in range(1, 6):  # WA0006 is deliberately missing from the export
        (folder / f"IMG-2024031{4 if i < 6 else 5}-WA000{i}.jpg").write_bytes(encode(pattern(i, (64, 48))))
    return folder


def test_dry_run_report(tmp_path, capsys):
    folder = _write_export(tmp_path)
    assert imp.main([str(folder), "--dry-run", "--tz", "Asia/Karachi", "--model", "qwen/qwen2.5-vl-72b-instruct"],
                    exports_root=tmp_path) == 0
    out = capsys.readouterr().out
    # S1's album; S2's two items 60 s apart (one post, two segments); the phone-number seller.
    # The buyer's text is dropped as text-only; S1's next-day post has only a missing photo and a video.
    assert "posts:               3" in out
    assert "dropped text-only:   1" in out
    assert "dropped all-missing: 1" in out
    assert "image files missing: 1" in out
    assert "segments (items):    4" in out
    for secret in ("Seller", "Buyer", "923001234567", "1234567", "Nike AF1", "Thrift Group"):
        assert secret not in out


def test_groups_and_segments(tmp_path):
    folder = _write_export(tmp_path)
    args = imp.parse_args([str(folder), "--dry-run"])
    export = imp.open_export(folder, tmp_path)
    groups, report = imp.build_groups(args, export, TZ)
    sizes = [len([m for m in g.messages if m.kind == "image"]) for g in groups]
    assert sizes == [2, 2, 1]  # S2's 13:30 and 13:31 photos are 60 s apart: one post, two captions
    assert [g.sender.startswith("name:") for g in groups] == [True, True, False]
    assert report.segments == 4
    assert report.est_calls[1] >= report.est_calls[0]


def test_limit_and_before(tmp_path, capsys):
    folder = _write_export(tmp_path)
    imp.main([str(folder), "--dry-run", "--tz", "Asia/Karachi", "--limit", "1"], exports_root=tmp_path)
    assert "posts:               1" in capsys.readouterr().out
    imp.main([str(folder), "--dry-run", "--tz", "Asia/Karachi", "--before", "2024-03-14T13:15"], exports_root=tmp_path)
    assert "posts:               1" in capsys.readouterr().out


def test_partial_media_warns_and_continues(tmp_path, capsys):
    folder = _write_export(tmp_path, omitted=10)
    assert imp.main([str(folder), "--dry-run", "--tz", "UTC"], exports_root=tmp_path) == 0
    err = capsys.readouterr().err
    assert "10 images were omitted" in err and "2024-03-14 .. 2024-03-14" in err


def test_no_media_at_all_aborts(tmp_path, capsys):
    folder = tmp_path / "nomedia"
    folder.mkdir()
    (folder / "_chat.txt").write_text("[14/03/2024, 13:05:09] Seller One: image omitted\n"
                                      "[14/03/2024, 13:06:09] Seller One: Nike 42\n")
    assert imp.main([str(folder), "--dry-run", "--tz", "UTC"], exports_root=tmp_path) == 2
    assert "without media" in capsys.readouterr().err


def test_path_outside_exports_refused(tmp_path, capsys):
    folder = _write_export(tmp_path / "elsewhere")
    assert imp.main([str(folder), "--dry-run"], exports_root=tmp_path / "exports") == 2
    assert "gitignored" in capsys.readouterr().err


def test_zip_export_and_cleanup(tmp_path, capsys):
    folder = _write_export(tmp_path / "src")
    exports = tmp_path / "exports"
    exports.mkdir()
    zpath = exports / "chat.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in folder.iterdir():
            zf.write(p, p.name)
    assert imp.main([str(zpath), "--dry-run", "--tz", "UTC"], exports_root=exports) == 0
    assert "posts:               3" in capsys.readouterr().out
    assert not any((exports / ".tmp").iterdir())


def test_zip_slip_rejected(tmp_path, capsys):
    exports = tmp_path / "exports"
    exports.mkdir()
    zpath = exports / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../../escape.txt", "x")
    assert imp.main([str(zpath), "--dry-run"], exports_root=exports) == 2
    assert "zip-slip" in capsys.readouterr().err
    assert not (tmp_path / "escape.txt").exists()


def test_ingest_url_must_be_loopback():
    with pytest.raises(SystemExit):
        imp.parse_args(["x", "--ingest-url", "http://192.168.1.5:8000/ingest"])


# ---------- real run against the app (build step 6) ----------

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402


@pytest.fixture
def ingest_client(db_url, tmp_path):
    with psycopg.connect(db_url) as c:
        c.execute("TRUNCATE posts RESTART IDENTITY CASCADE")
        c.commit()
    app = create_app(Settings(_env_file=None, database_url=db_url, ingest_token="tok", sender_hmac_key="hk",
                              media_dir=str(tmp_path / "media"), vlm_provider="off",
                              load_models=False))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


def _run(folder, root, client, *extra):
    args = imp.parse_args([str(folder), "--tz", "Asia/Karachi", *extra])
    export = imp.open_export(folder, root)
    groups, _ = imp.build_groups(args, export, TZ)
    return imp.run_import(groups, args, client, TZ, "tok", sleep=lambda s: None)


def test_real_run_then_rerun_is_all_duplicates(tmp_path, ingest_client, db_url):
    folder = _write_export(tmp_path / "exports")
    root = tmp_path / "exports"
    assert _run(folder, root, ingest_client) == {"accepted": 3, "duplicate": 0, "rejected": 0, "failed": 0}
    assert _run(folder, root, ingest_client) == {"accepted": 0, "duplicate": 3, "rejected": 0, "failed": 0}
    with psycopg.connect(db_url) as c:
        rows = c.execute("SELECT source, status, priority, vlm_policy FROM posts").fetchall()
        assert rows == [("chat_export", "received", 10, "auto")] * 3
        assert c.execute("SELECT count(*) FROM images").fetchone()[0] == 5
        # the phone-number seller's JID matches the live sender_alt form
        jids = {r[0] for r in c.execute("SELECT sender_jid FROM posts")}
        assert "923001234567@s.whatsapp.net" in jids


def test_no_vlm_sets_policy(tmp_path, ingest_client, db_url):
    folder = _write_export(tmp_path / "exports")
    _run(folder, tmp_path / "exports", ingest_client, "--no-vlm", "--limit", "1")
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT vlm_policy FROM posts").fetchall() == [("never",)]


def test_post_group_retries_5xx_then_gives_up():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"detail": "overlap_retry"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    group = imp.Group("c", "name:x", [imp.RawMsg("k", "c", "name:x", 0, "image", missing=True)], 0, 0)
    args = imp.parse_args(["x"])
    slept = []
    assert imp.post_group(client, group, args, TZ, "tok", sleep=slept.append) == "failed"
    assert len(calls) == 4 and slept == [2, 8, 30]


def test_post_group_4xx_is_final():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(422, json={"detail": "x"})))
    group = imp.Group("c", "name:x", [imp.RawMsg("k", "c", "name:x", 0, "image", missing=True)], 0, 0)
    assert imp.post_group(client, group, imp.parse_args(["x"]), TZ, "tok", sleep=lambda s: None) == "rejected"


def test_wait_for_queue_backpressure():
    depths = iter([25, 21, 20])
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"received": next(depths)})))
    slept = []
    imp.wait_for_queue(client, "http://127.0.0.1:8000/admin/queue", "tok", 20, sleep=slept.append)
    assert slept == [5, 5]


def test_relative_path_falls_back_to_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "REPO", tmp_path)
    _write_export(tmp_path / "data" / "exports")
    monkeypatch.chdir(tmp_path / "data")  # like running from backend/: the path doesn't exist from here
    assert imp.resolve_export_path(imp.Path("data/exports/Thrift Group")) == tmp_path / "data/exports/Thrift Group"


def test_zip_with_broken_index_refused(tmp_path, capsys):
    folder = _write_export(tmp_path / "src")
    exports = tmp_path / "exports"
    exports.mkdir()
    zpath = exports / "chat.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in folder.iterdir():
            zf.write(p, p.name)
    data = bytearray(zpath.read_bytes())
    eocd = data.rfind(b"PK\x05\x06")
    count = int.from_bytes(data[eocd + 10:eocd + 12], "little")
    data[eocd + 10:eocd + 12] = (count + 5).to_bytes(2, "little")  # declare more entries than the index holds
    zpath.write_bytes(bytes(data))
    assert imp.main([str(zpath), "--dry-run", "--tz", "UTC"], exports_root=exports) == 2
    assert "index is broken" in capsys.readouterr().err
