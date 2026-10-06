# ThriftRadar

Turns a WhatsApp thrift group's shoe posts into a searchable catalogue, and tells you when your size turns up under your budget.

Sellers in thrift groups post photo albums with loose captions ("AF1 43 Rs 4500 final"), repost the same pairs for weeks, and bury the one you want under hundreds of others. ThriftRadar listens to one group as a read-only linked device, finds the shoe in each photo, reads the caption and the size tag, fills the gaps with a vision-language model only when it has to, spots reposts, and matches every new listing against your wishlists. A match pops up as a macOS notification.

It runs on your own Mac. A separate demo mode, with licensed sample photos and no WhatsApp at all, can be deployed publicly.

## Features

- **Search by text or by photo.** "white sneakers size 42 under 5000" becomes brand, size and budget filters plus a semantic ranking; a photo search finds visually similar listings.
- **Wishlists** by text or reference photo, with a notification when a new post matches.
- **Repost detection** (perceptual hash, then embedding similarity), so a pair reposted ten times is one listing with a repost and price history.
- **Attributes from every source:** caption parsing, OCR on size tags, zero-shot brand recognition and a VLM. Each attribute shows where it came from.
- **Spend controls:** the VLM is the last resort, every call is reserved in a ledger first, and daily and per-post caps apply.
- **Backfill** from a WhatsApp chat export through the same ingest path as live messages.

## How it works

```mermaid
flowchart LR
  subgraph WA["WhatsApp thrift group"]
    S["Sellers post photo albums + captions"]
  end

  subgraph L["Listener (Node.js, Baileys): read-only linked device"]
    F["Filter: one group, images/text only, before any download"]
    A["Album grouping: per seller, 60 s sliding window, 180 s cap"]
    SP["Disk spool (crash-safe)"]
    F --> A --> SP
  end

  S --> F
  SP -- "POST /ingest + Idempotency-Key" --> I

  subgraph B["Backend (FastAPI, single process)"]
    I["/ingest: validate + store, no inference"]
    Q[("posts table = job queue<br/>FOR UPDATE SKIP LOCKED leases")]
    I --> Q

    subgraph P["AI pipeline: cheapest step first"]
      P1["Caption parser: price, size, brand"]
      P2["pHash repost check"]
      P3["YOLO-World: find & crop the shoe"]
      P4["SigLIP embedding + vector repost check"]
      P5["RapidOCR: size/brand from tags, only if missing"]
      P6["Vision-language model: only if still unsure<br/>daily cap + cost ledger"]
      P1 --> P2 --> P3 --> P4 --> P5 --> P6
    end

    Q --> P1
    M["Wishlist matching<br/>(filters + image similarity)"]
    P6 --> M
    P4 --> M
  end

  DB[("PostgreSQL 16 + pgvector")]
  P4 <--> DB
  M <--> DB
  M --> N["macOS notification"]

  W["Web app (Next.js static export)"] -- "REST API" --> B
```

- **`listener/`** (Node ≥ 20, [Baileys](https://github.com/WhiskeySockets/Baileys)): a linked device that processes exactly one group. It never sends messages or read receipts and doesn't appear online. Messages are filtered before any media download; photos and captions from the same sender are grouped into albums, spooled to disk, and posted to the loopback-only `/ingest` with an idempotency key.
- **`backend/`** (Python 3.12, FastAPI): the API, an in-process worker and the models, all in one process. The queue is the `posts` table itself, claimed with leases. The pipeline runs cheapest first and stops early: caption → pHash repost check → YOLO-World shoe detection → SigLIP embedding (and embedding repost check, zero-shot brand) → OCR only if size or brand is still missing → VLM only if required fields are still missing. Then wishlist matching and the notification.
- **`web/`** (Next.js 16, React 19): a static export served by the backend. Pages: feed and search, listing detail, wishlists, system status, and in demo mode login, simulate-a-post and photo credits.
- **`shared/`**: fixtures both languages are tested against (album grouping rule, idempotency keys), so the listener and the chat-export importer always agree.

### Models

| Stage | Model | Runs |
|---|---|---|
| Detection | YOLO-World v2 (s), shoe vocabulary baked in | local CPU / Apple GPU |
| Embeddings, brand | SigLIP base patch16-224 | local |
| OCR | RapidOCR (ONNX Runtime) | local |
| Attributes | Qwen VL models via Ollama (local, default), Featherless or OpenRouter | local or API, capped |

Weights are downloaded once, pinned by revision and hash, and baked into `data/models`.

## Privacy

The real group's data never leaves the machine except what you explicitly send to a VLM provider, and the code is built so that mistakes are hard to make:

- `data/`, `listener/auth/`, spools and all `.env` files are gitignored.
- Senders are stored as HMAC references, not numbers. Logs never contain captions, names, phone numbers or raw WhatsApp IDs (there's a test for it).
- The demo uses a separate database that CHECK constraints lock to demo sources, verified at startup. In demo mode the WhatsApp and ingest routes aren't mounted at all.
- The demo bundle builder refuses to finish if it finds env files, auth or data directories, phone-like numbers, WhatsApp IDs or key-like strings.

## Getting started

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Node ≥ 20, macOS for notifications.

```bash
# Postgres 16 + pgvector on 127.0.0.1:5433
docker compose up -d db

# Backend
cd backend
uv sync
cp .env.example .env            # set INGEST_TOKEN and SENDER_HMAC_KEY to random strings
uv run python -m app.migrate
uv run --group bake python -m scripts.bake_models   # download and bake model weights (once)

# Web app (served by the backend)
cd ../web && npm install && npm run build

# Run
cd ../backend && uv run uvicorn app.main:create_app --factory --port 8000
# → http://127.0.0.1:8000
```

> **Keep `SENDER_HMAC_KEY` fixed** once you've ingested data: stored sender references depend on it.

### Listen to a group

```bash
cd listener
npm install
node src/index.js --list-groups    # scan the QR code with WhatsApp, then copy the group's JID
cp .env.example .env               # set THRIFT_GROUP_JID and the same INGEST_TOKEN as the backend
npm start
```

`deploy/macos/install.sh` installs launchd agents that keep the backend and listener running (logs in `~/Library/Logs/ThriftRadar/`; `--uninstall` removes them).

### Backfill from a chat export

Export the chat from WhatsApp *with media*, put it under `data/exports/`, then, with the backend running:

```bash
cd backend
uv run python -m scripts.import_chat_export data/exports/<export> --dry-run --tz <your/timezone>   # counts and cost estimate
INGEST_TOKEN=<same as backend> uv run python -m scripts.import_chat_export data/exports/<export> --tz <your/timezone>   # ingest
uv run python -m app.worker --drain            # process the queue in the foreground
```

### VLM provider

Set `VLM_PROVIDER` in `backend/.env`: `ollama` (default, local, free), `featherless`, `openrouter`, or `off`. API providers need their key (`FEATHERLESS_API_KEY` / `OPENROUTER_API_KEY`) and are bounded by `VLM_DAILY_CAP`. `uv run python -m scripts.requeue --vlm --source chat_export` prints what sending finished posts to the VLM would cost before you commit to it (`--yes` applies).

### Wishlists from the terminal

```bash
uv run python -m scripts.wishlist add "white sneakers size 42 under 5000"
uv run python -m scripts.wishlist list
```

## Demo mode

`DEMO_MODE=1` runs the same code against a separate database seeded with 47 sample listings built from CC-licensed photos (`backend/seed/`, credits in `ATTRIBUTION.csv` and on the `/credits` page) with made-up sizes and prices. It adds a dummy login, per-visitor simulated posts that are purged after 24 hours, rate limits and a small VLM cap.

Try it locally on a throwaway database:

```bash
psql postgresql://thrift:thrift@127.0.0.1:5433/postgres -c 'CREATE DATABASE thriftradar_demo_try'
cd backend
DATABASE_URL=postgresql://thrift:thrift@127.0.0.1:5433/thriftradar_demo_try uv run python -m app.migrate --demo
DEMO_MODE=1 DATABASE_URL=postgresql://thrift:thrift@127.0.0.1:5433/thriftradar_demo_try \
  SESSION_SECRET=x SENDER_HMAC_KEY=y VLM_PROVIDER=off TRUST_PROXY_HOPS=0 MODELS_DIR=../data/models \
  uv run uvicorn app.main:create_app --factory --port 8001
# → http://127.0.0.1:8001/login/ (credentials are shown on the page)
```

### Deploy

`backend/scripts/build_space.py` assembles a minimal Docker bundle in `dist/space/` (Postgres inside the container, models baked in, the seed database built at image build and restored on every boot). `deploy/oracle/` runs it on an Oracle Cloud Always Free VM behind Caddy with automatic HTTPS on an `sslip.io` name: copy the bundle and `deploy/oracle/*` to the VM and run `setup.sh <public-ip>`. `deploy/space/` holds the Hugging Face Space variant.

## Tests

```bash
cd backend && uv run pytest              # recreates thriftradar_test* databases on the compose Postgres
cd backend && uv run pytest -m "not models"   # skip tests that load real weights
cd listener && npm test
cd web && npm run typecheck
```

Tests never read `backend/.env`, so they can't spend API credits.

## Repository layout

```
backend/     FastAPI app, worker, AI pipeline, migrations, scripts, tests, demo seed
listener/    WhatsApp listener (Baileys)
web/         Next.js web app
shared/      cross-language test fixtures
deploy/      launchd agents (macOS), demo Docker image, Oracle VM setup
```

## Photo credits

The demo seed photos are by their respective authors on Flickr and rawpixel, used under CC BY 2.0, CC BY-SA 2.0 and CC0. Each file's author, source, license and checksum are listed in [`backend/seed/ATTRIBUTION.csv`](backend/seed/ATTRIBUTION.csv).
