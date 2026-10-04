// Poster (DESIGN §4.1): sends ready albums to the loopback /ingest one at a time, FIFO. The Idempotency-Key is the
// same formula as backend/app/ids.py, so a retry after a slow success is answered "duplicate" before any parsing.
import { createHash } from 'node:crypto';

const STALE_MS = 7 * 86_400_000;
const REJECT_REASONS = new Set(['corrupt', 'too_large', 'bad_type', 'download_failed']);

export function idemKey(source, chatId, keys) {
  const body = `${source}\n${chatId}\n` + [...new Set(keys)].sort().join('\n');
  return createHash('sha256').update(body).digest('hex');
}

/** The multipart `meta` part (backend IngestMeta) and the file parts, from an album's spool meta. */
export function buildPayload(album) {
  const files = [];
  const messages = album.items.map((it) => {
    const m = { key: it.key, kind: it.kind, sent_at: new Date(it.ts * 1000).toISOString() };
    if (it.caption) m.caption = it.caption;
    if (it.kind === 'image') {
      if (it.missing) {
        m.missing_media = true;
        m.reject_reason = REJECT_REASONS.has(it.reason) ? it.reason : 'download_failed';
      } else {
        m.file_field = `f${files.length}`;
        files.push({ field: m.file_field, file: it.file });
      }
    }
    return m;
  });
  const meta = { source: 'whatsapp', chat_id: album.chat, sender_id: album.sender, sender_alt: album.senderAlt ?? null,
    messages };
  return { meta, files, key: idemKey('whatsapp', album.chat, album.items.map((i) => i.key)) };
}

export class Poster {
  constructor({ cfg, spool, log, fetchImpl = fetch, now = Date.now, sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
    random = Math.random }) {
    Object.assign(this, { cfg, spool, log, fetchImpl, now, sleep, random });
    this.queue = [];
    this.queued = new Set();
    this.running = false;
    this.stopped = false;
    this.stats = { posted: 0, duplicate: 0, rejected: 0 };
  }

  enqueue(albumId) {
    if (this.queued.has(albumId)) return;
    this.queued.add(albumId);
    this.queue.push(albumId);
    if (!this.running) void this.run();
  }

  async run() {
    this.running = true;
    try {
      while (this.queue.length && !this.stopped) {
        const id = this.queue[0];
        let attempt = 0;
        for (;;) {
          const r = await this.postAlbum(id);
          if (r === 'done' || this.stopped) break;
          const wait = r === 'config' ? 300_000 : Math.min(300_000, 2_000 * 2 ** attempt) * (0.5 + this.random() / 2);
          attempt++;
          await this.sleep(wait);
        }
        this.queue.shift();
        this.queued.delete(id);
      }
    } finally {
      this.running = false;
    }
  }

  /** -> 'done' (posted, duplicate or rejected), 'retry' (5xx/network) or 'config' (401/403/404). */
  async postAlbum(albumId) {
    let album;
    try {
      album = this.spool.readMeta('ready', albumId);
    } catch {
      this.spool.reject('ready', albumId, 'corrupt_meta', null);
      this.stats.rejected++;
      return 'done';
    }
    if (this.now() - album.lastArrivalMs > STALE_MS) {
      this.spool.reject('ready', albumId, 'stale', null);
      this.stats.rejected++;
      return 'done';
    }
    const { meta, files, key } = buildPayload(album);
    const form = new FormData();
    form.append('meta', JSON.stringify(meta));
    for (const f of files) {
      form.append(f.field, new Blob([this.spool.readFile('ready', albumId, f.file)]), `${f.field}.img`);
    }
    const t0 = this.now();
    let res;
    try {
      res = await this.fetchImpl(this.cfg.ingestUrl, {
        method: 'POST', body: form, headers: { 'X-Ingest-Token': this.cfg.ingestToken, 'Idempotency-Key': key },
        signal: AbortSignal.timeout(120_000),
      });
    } catch (e) {
      this.log?.warn({ album: albumId, err: e.name }, 'ingest unreachable; will retry');
      return 'retry';
    }
    const ms = this.now() - t0;
    let detail = null;
    try {
      detail = (await res.json())?.detail ?? null;
    } catch { /* not JSON */ }
    const base = { album: albumId, images: files.length, status: res.status, ms };
    if (res.status === 202 || res.status === 200) {
      this.spool.discard('ready', albumId);
      this.stats[res.status === 200 ? 'duplicate' : 'posted']++;
      this.log?.info(base, res.status === 200 ? 'album already ingested' : 'album posted');
      return 'done';
    }
    if ([400, 413, 422].includes(res.status)) {
      this.spool.reject('ready', albumId, res.status, typeof detail === 'string' ? detail : 'invalid');
      this.stats.rejected++;
      this.log?.error({ ...base, detail: typeof detail === 'string' ? detail : 'invalid' }, 'album rejected by ingest');
      return 'done';
    }
    if ([401, 403, 404].includes(res.status)) {
      this.log?.error(base, 'ingest refused the token or is in demo mode; check INGEST_TOKEN/INGEST_URL');
      return 'config';
    }
    this.log?.warn(base, 'ingest error; will retry');
    return 'retry';
  }

  stop() {
    this.stopped = true;
  }
}
