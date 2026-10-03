"""Build a local, blind review page of candidate repost pairs. Spec: DESIGN.md §4.8 `repost_review.py`.

Run from backend/:
  uv run python -m scripts.repost_review phash [--per-band 10]   # image pairs by pHash distance (before the worker)
  uv run python -m scripts.repost_review emb [--per-band 10]     # listing pairs by embedding cosine (after a pass)
  uv run python -m scripts.repost_review score phash "3,17,22"   # pair numbers you marked "different"

The page goes to data/review/<kind>/index.html (gitignored, real group photos: never publish it).
Thumbnails are inlined, so the page is self-contained. It shows pairs in random order with no score, so labels
are blind. The answer key is pairs.json next to it.
"""

import argparse
import base64
import io
import json
import random
import sys
from pathlib import Path

import psycopg
from PIL import Image

from app.settings import Settings

REPO = Path(__file__).resolve().parents[2]
REVIEW = REPO / "data" / "review"
MEDIA = REPO / "data" / "media"

PHASH_BANDS = [(2, True), (4, True), (6, True), (8, True), (10, True), (2, False), (4, False)]
EMB_BANDS = [(0.88, 0.91), (0.91, 0.93), (0.93, 0.94), (0.94, 1.01)]


def thumb_uri(sha: bytes, max_side: int = 480) -> str:
    """Inline JPEG thumbnail: browsers block file:// pages from loading files outside their own folder."""
    h = sha.hex()
    with Image.open(MEDIA / h[:2] / f"{h}.jpg") as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def sample_phash(conn, per_band: int, rng: random.Random) -> list[dict]:
    pairs = []
    for dist, same in PHASH_BANDS:
        rows = conn.execute(
            """SELECT a.id, a.sha256, b.id, b.sha256
               FROM images a JOIN images b ON a.post_id < b.post_id
               JOIN posts pa ON pa.id = a.post_id JOIN posts pb ON pb.id = b.post_id
               WHERE bit_count((a.phash # b.phash)::bit(64)) = %s AND (pa.sender_ref = pb.sender_ref) = %s
               ORDER BY md5(a.id::text || ':' || b.id::text) LIMIT %s""",
            (dist, same, per_band if same else max(3, per_band // 2)),
        ).fetchall()
        for a_id, a_sha, b_id, b_sha in rows:
            pairs.append({"band": f"d={dist} {'same' if same else 'cross'}-seller", "score": dist,
                          "same_seller": same, "left": [[a_id, bytes(a_sha)]], "right": [[b_id, bytes(b_sha)]]})
    return pairs


def sample_emb(conn, per_band: int, rng: random.Random) -> list[dict]:
    """Same-seller listing pairs by cosine band, plus segments the embedding guard already merged (cos >= the
    current REPOST_EMB_MIN_COS), shown as the merged post's photos against the listing's photos."""
    rows = conn.execute(
        """SELECT a.id, b.id, 1 - (a.embedding <=> b.embedding) AS cos
           FROM listings a JOIN listings b ON a.sender_ref = b.sender_ref AND a.post_id < b.post_id
           WHERE 1 - (a.embedding <=> b.embedding) >= %s""",
        (EMB_BANDS[0][0],),
    ).fetchall()
    pairs = []
    for lo, hi in EMB_BANDS:
        band = [r for r in rows if lo <= r[2] < hi]
        for a, b, cos in rng.sample(band, min(per_band, len(band))):
            pairs.append({"band": f"cos {lo:.2f}-{min(hi, 1):.2f}", "score": round(cos, 4), "same_seller": True,
                          "left": listing_images(conn, a), "right": listing_images(conn, b)})
    merged = conn.execute(
        """SELECT s.listing_id, s.post_id, s.segment_idx,
                  1 - (l.embedding <=> (SELECT avg(embedding) FROM images i
                                        WHERE i.post_id = s.post_id AND i.segment_idx = s.segment_idx))
           FROM listing_sightings s JOIN listings l ON l.id = s.listing_id
           WHERE s.match_kind = 'embedding'""").fetchall()
    for lid, pid, seg, cos in rng.sample(merged, min(2 * per_band, len(merged))):
        right = conn.execute(
            "SELECT id, sha256 FROM images WHERE post_id = %s AND segment_idx = %s ORDER BY seq LIMIT 3",
            (pid, seg)).fetchall()
        pairs.append({"band": "merged by embedding", "score": round(float(cos or 0), 4), "same_seller": True,
                      "left": listing_images(conn, lid), "right": [[r[0], bytes(r[1])] for r in right]})
    return pairs


def listing_images(conn, listing_id: int, limit: int = 3) -> list:
    rows = conn.execute(
        """SELECT i.id, i.sha256 FROM listings l JOIN images i
             ON i.post_id = l.post_id AND i.segment_idx = l.segment_idx
           WHERE l.id = %s ORDER BY (i.id = l.cover_image_id) DESC, i.seq LIMIT %s""",
        (listing_id, limit),
    ).fetchall()
    return [[r[0], bytes(r[1])] for r in rows]


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Repost review</title><style>
:root{--bg:#fafafa;--fg:#111;--card:#fff;--line:#ddd;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--card:#1c1c1c;--line:#333;--accent:#60a5fa}}
body{background:var(--bg);color:var(--fg);font:15px system-ui;margin:0 auto;max-width:1100px;padding:16px}
.pair{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:14px 0;padding:10px}
.imgs{display:grid;grid-template-columns:1fr 1fr;gap:10px}.side{display:flex;gap:4px;overflow-x:auto}
.side img{max-height:300px;max-width:100%;border-radius:4px}
label{margin-right:14px;cursor:pointer}#out{position:sticky;bottom:0;background:var(--card);border:1px solid var(--line);
padding:10px;border-radius:8px;font-family:ui-monospace,monospace;white-space:pre-wrap}
</style></head><body>
<h1>Same item? __N__ pairs</h1>
<p>__HELP__ Unanswered pairs count as "same". When done, copy the box at the bottom back to Claude.</p>
__PAIRS__
<div id="out"></div>
<script>
const KEY="review-__KIND__";let s={};try{s=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){}
function upd(){const d=[],u=[];for(const[k,v]of Object.entries(s)){if(v==="d")d.push(+k);if(v==="u")u.push(+k)}
d.sort((a,b)=>a-b);u.sort((a,b)=>a-b);document.getElementById("out").textContent=
"__KIND__ different: "+(d.join(",")||"none")+"\\n__KIND__ unsure: "+(u.join(",")||"none")+
"\\nanswered "+Object.keys(s).length+" / __N__";try{localStorage.setItem(KEY,JSON.stringify(s))}catch(e){}}
document.querySelectorAll("input[type=radio]").forEach(r=>{if(s[r.dataset.n]===r.value)r.checked=true;
r.addEventListener("change",()=>{s[r.dataset.n]=r.value;upd()})});upd();
</script></body></html>"""

HELP = {
    "phash": "Each row is two single photos from different posts. Is it the same physical pair of shoes "
             "(the same listing photographed or re-shared), not just the same model?",
    "emb": "Each row is up to 3 photos of two listings by the same seller. Is it the same physical pair of shoes?",
}


def render(kind: str, pairs: list[dict]) -> str:
    blocks = []
    for n, p in enumerate(pairs, 1):
        sides = []
        for side in (p["left"], p["right"]):
            imgs = "".join(f'<img loading="lazy" src="{thumb_uri(sha)}">' for _, sha in side)
            sides.append(f'<div class="side">{imgs}</div>')
        radios = "".join(
            f'<label><input type="radio" name="p{n}" value="{v}" data-n="{n}"> {t}</label>'
            for v, t in (("s", "same"), ("d", "different"), ("u", "unsure")))
        blocks.append(f'<div class="pair"><b>#{n}</b><div class="imgs">{"".join(sides)}</div>{radios}</div>')
    return (PAGE.replace("__PAIRS__", "\n".join(blocks)).replace("__N__", str(len(pairs)))
            .replace("__HELP__", HELP[kind]).replace("__KIND__", kind))


def build(kind: str, per_band: int, settings: Settings) -> Path:
    rng = random.Random(8)
    with psycopg.connect(settings.database_url) as conn:
        pairs = sample_phash(conn, per_band, rng) if kind == "phash" else sample_emb(conn, per_band, rng)
    rng.shuffle(pairs)
    out_dir = REVIEW / kind
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render(kind, pairs))
    key = [{"n": n, "band": p["band"], "score": p["score"], "same_seller": p["same_seller"],
            "left": [i for i, _ in p["left"]], "right": [i for i, _ in p["right"]]} for n, p in enumerate(pairs, 1)]
    (out_dir / "pairs.json").write_text(json.dumps(key, indent=1))
    bands: dict[str, int] = {}
    for p in pairs:
        bands[p["band"]] = bands.get(p["band"], 0) + 1
    print(f"{len(pairs)} pairs -> {out_dir / 'index.html'}")
    for b, c in bands.items():
        print(f"  {b:24} {c}")
    return out_dir


def score(kind: str, different: str) -> None:
    key = json.loads((REVIEW / kind / "pairs.json").read_text())
    diff = {int(x) for x in different.replace(" ", "").split(",") if x}
    bands: dict[str, list[int]] = {}
    for p in key:
        bands.setdefault(p["band"], [0, 0])
        bands[p["band"]][0] += 1
        bands[p["band"]][1] += p["n"] in diff
    print(f"{'band':24} pairs  different  precision(same)")
    for b, (n, d) in bands.items():
        print(f"{b:24} {n:5}  {d:9}  {(n - d) / n:.0%}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.repost_review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for kind in ("phash", "emb"):
        p = sub.add_parser(kind)
        p.add_argument("--per-band", type=int, default=10)
    s = sub.add_parser("score")
    s.add_argument("kind", choices=("phash", "emb"))
    s.add_argument("different", help='comma-separated pair numbers marked "different"')
    args = ap.parse_args(argv)
    if args.cmd == "score":
        score(args.kind, args.different)
    else:
        build(args.cmd, args.per_band, Settings())
    return 0


if __name__ == "__main__":
    sys.exit(main())
