// downloadImage (DESIGN §4.1): size and type checks before any network; bounded streaming; expired URLs are
// permanent; no reupload request unless ALLOW_MEDIA_REUPLOAD=1 (Q8), so nothing is ever sent to the seller.
import { downloadMediaMessage } from 'baileys';

const ALLOWED = new Set(['image/jpeg', 'image/png', 'image/webp']);
const BACKOFF_MS = [1_000, 4_000];

export async function downloadImage(sock, msg, cls, cfg, { download = downloadMediaMessage, logger,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)), timeoutMs = 30_000, random = Math.random } = {}) {
  if (cls.fileLength > cfg.maxFileBytes) return { ok: false, reason: 'too_large' };
  if (!ALLOWED.has((cls.mimetype ?? '').split(';')[0])) return { ok: false, reason: 'bad_type' };
  const ctx = cfg.allowMediaReupload ? { logger, reuploadRequest: sock.updateMediaMessage } : undefined;
  for (let attempt = 0; attempt < 3; attempt++) {
    let stream;
    let timer;
    try {
      const work = (async () => {
        stream = await download(msg, 'stream', {}, ctx);
        const chunks = [];
        let n = 0;
        for await (const c of stream) {
          n += c.length;
          if (n > cfg.maxFileBytes) {
            stream.destroy();
            return { ok: false, reason: 'too_large' };
          }
          chunks.push(c);
        }
        return { ok: true, bytes: Buffer.concat(chunks), mimetype: cls.mimetype };
      })();
      const timeout = new Promise((_, rej) => {
        timer = setTimeout(() => {
          stream?.destroy?.();
          rej(Object.assign(new Error('timeout'), { name: 'TimeoutError' }));
        }, timeoutMs);
      });
      return await Promise.race([work, timeout]);
    } catch (e) {
      const status = e?.output?.statusCode ?? e?.status;
      if (status === 404 || status === 410) return { ok: false, reason: 'expired' };
      if (attempt < 2) await sleep(BACKOFF_MS[attempt] * (0.75 + random() / 2));
    } finally {
      clearTimeout(timer);
    }
  }
  return { ok: false, reason: 'download_failed' };
}
