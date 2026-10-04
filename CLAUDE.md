# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ThriftRadar rules
- Update docs/DESIGN.md before implementing or changing any handler; code must match the function spec.
- Guard before spend: dedupe and status checks run before media download, model inference, or any OpenRouter call.
- Every ingest path (live, importer, seed) is idempotent on a stable ID. Concurrent duplicates are stopped by a claim, not just a unique constraint.
- Listener is read-only: no sends, no read receipts, markOnlineOnConnect: false.
- Never log or store real phone numbers in the demo; demo uses a separate DB.
- DEMO_MODE checks are server-side.

## Progress tracking

At the start of a session, read `docs/PROGRESS.md`: it says which build step is current, what's done, what's blocked and why. After every major change (a build step finished, a design decision, a blocker found), update it before moving on.

## Project state

Build phase (DESIGN.md §7 steps; current step in `docs/PROGRESS.md`). `docs/DESIGN.md` is the source of truth: function-level specs (§4), test plan (§6) and build order (§7). Its §0 holds open questions. Check them for answers before building, and don't build past a BLOCKING question that is still open. `architecture.mmd` is an older overview diagram; where it disagrees with DESIGN.md, DESIGN.md wins (its "Corrections" list names the differences).

## Commands

```
docker compose up -d db                 # Postgres 16 + pgvector on 127.0.0.1:5433 (5432 is taken by a Homebrew Postgres); thrift/thrift/thriftradar
cd backend && uv sync                   # Python 3.12 venv in backend/.venv (local python3 is 3.14; don't use it)
cd backend && uv run python -m app.migrate          # apply migrations to the dev DB (--demo adds the demo lockdown)
cd backend && uv run pytest                         # all backend tests; recreates thriftradar_test* DBs on the compose Postgres
cd backend && uv run pytest tests/test_migrations.py::test_demo_rejects_sender_jid   # single test
cd backend && INGEST_TOKEN=… SENDER_HMAC_KEY=… uv run uvicorn app.main:create_app --factory --port 8000   # local API (or put the vars in backend/.env)
cd backend && uv run python -m scripts.import_chat_export data/exports/<export> --dry-run --tz Asia/Karachi   # backfill report; drop --dry-run (and set INGEST_TOKEN) to post
cd backend && uv run --group bake python -m scripts.bake_models    # download + bake model weights into data/models (once)
cd backend && uv run python -m scripts.bench_models                # model latency/RSS on 20 photos
cd backend && uv run pytest -m "not models"                         # skip tests that load real weights
cd backend && uv run python -m app.worker --drain [--vlm] [--limit N]   # process queued posts in the foreground (--vlm spends Featherless credits; caps apply)
cd backend && uv run python -m scripts.requeue --vlm --source chat_export   # dry run: what sending finished posts to the VLM would cost; --yes applies
cd backend && uv run python -m scripts.wishlist add "white sneakers size 42 under 5000"   # wishlists from the terminal (also list / matches ID / remove ID / test-notify)
cd web && npm install && npm run build              # web app -> web/out; the backend serves it at http://127.0.0.1:8000 (restart uvicorn after the first build)
cd web && npm run typecheck
# try demo mode locally on a throwaway DB (AI off, nothing spent); log in at http://127.0.0.1:8001/login/
psql postgresql://thrift:thrift@127.0.0.1:5433/postgres -c 'CREATE DATABASE thriftradar_demo_try'
cd backend && DATABASE_URL=postgresql://thrift:thrift@127.0.0.1:5433/thriftradar_demo_try uv run python -m app.migrate --demo
cd backend && DEMO_MODE=1 DATABASE_URL=…/thriftradar_demo_try SESSION_SECRET=x SENDER_HMAC_KEY=y VLM_PROVIDER=off TRUST_PROXY_HOPS=0 MODELS_DIR=../data/models uv run uvicorn app.main:create_app --factory --port 8001
```

Not set up yet: the listener (Node ≥ 20, `node:test`, `npm test` in `listener/`; `package.json` exists with an exact Baileys pin but deps aren't installed until build step 11). Tests needing real weights are marked `@pytest.mark.models` and skip if `data/models` hasn't been baked. `backend/.env` holds the real `INGEST_TOKEN` and `SENDER_HMAC_KEY`; never change `SENDER_HMAC_KEY` (stored sender/chat refs depend on it).

## Architecture (big picture)

- **listener/** (Node, Baileys): a linked WhatsApp device that only processes one group (`THRIFT_GROUP_JID`). It filters messages before any download, groups photos and captions per (chat, sender) using a sliding 60s window with a 180s cap on message timestamps, spools albums to disk, and POSTs them to the loopback-only `POST /ingest` with an `Idempotency-Key` header.
- **backend/** (FastAPI, single uvicorn worker): the API, an in-process worker and models loaded once, all in one process. `/ingest` validates the request and stores the post with no inference. The **queue is the `posts` table**: a status column plus lease claims (`FOR UPDATE SKIP LOCKED`, `claim_token`). Pipeline order, cheapest first: segment and caption parse → pHash repost check → YOLO-World → SigLIP (+ embedding repost check) → OCR only if size/brand is missing → VLM only if `needs_vlm`. Each VLM attempt is first reserved as a row in the `vlm_calls` ledger, which also enforces the daily cap. Then come matching against wishlists and a macOS notification (osascript via argv, never in demo).
- **Importer** (`backend/scripts/import_chat_export.py`) feeds WhatsApp chat exports through the same `/ingest`. It uses the same grouping rule; both implementations are tested against `shared/grouping_cases.json`.
- **Demo** (`DEMO_MODE=1`, HF Space on port 7860): a separate DB, locked to demo sources by CHECK constraints and a `db_meta.role` check at startup. WhatsApp/ingest routes are not mounted (404), dummy login works only in this mode, and `seed_demo.py` loads licensed seed images with ground-truth attributes (no VLM).

## Privacy and gitignore

`data/`, `listener/auth/`, `.env` hold real group data or session keys and must stay untracked. DESIGN.md §0 correction 9 lists gitignore gaps still to fix (`listener/spool/`, `listener/state/`, `.env.*`, root `auth/`, `data/exports/`). Logs never contain captions, push names, phone numbers or raw JIDs.
