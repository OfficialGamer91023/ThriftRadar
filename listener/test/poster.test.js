import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { buildPayload, idemKey, Poster } from '../src/poster.js';
import { Spool } from '../src/spool.js';
import { captureLog, cfgFor, GROUP, SELLER, tmp } from './helpers.js';

const IDEM = JSON.parse(readFileSync(fileURLToPath(new URL('../../shared/idem_cases.json', import.meta.url)), 'utf8'));

test('Idempotency-Key equals the Python idempotency_key for the shared fixture', () => {
  for (const c of IDEM.cases) assert.equal(idemKey(c.source, c.chat_id, c.keys), c.expected);
});

function readyAlbum(spool, id = 'a1', extra = {}) {
  const file = spool.writeItem(id, 'K1', Buffer.from('jpegbytes'));
  spool.writeMeta('open', id, { id, chat: GROUP, sender: SELLER, senderAlt: null, firstTs: 1_759_000_000,
    lastTs: 1_759_000_010, firstArrivalMs: Date.now(), lastArrivalMs: Date.now(), images: 2, totalBytes: 9,
    items: [{ key: 'K1', kind: 'image', ts: 1_759_000_000, caption: 'Nike 42 Rs 4500', file, missing: false },
      { key: 'K2', kind: 'image', ts: 1_759_000_005, caption: null, file: null, missing: true, reason: 'expired' },
      { key: 'K3', kind: 'text', ts: 1_759_000_010, caption: 'final 4000', file: null, missing: false }], ...extra });
  spool.markReady(id);
  return id;
}

test('payload matches the backend IngestMeta shape', () => {
  const spool = new Spool(tmp());
  const { meta, files, key } = buildPayload(spool.readMeta('ready', readyAlbum(spool)));
  assert.equal(meta.source, 'whatsapp');
  assert.deepEqual(meta.messages.map((m) => [m.kind, m.file_field ?? null, m.missing_media ?? false, m.reject_reason ?? null]),
    [['image', 'f0', false, null], ['image', null, true, 'download_failed'], ['text', null, false, null]]);
  assert.equal(meta.messages[0].sent_at, new Date(1_759_000_000 * 1000).toISOString());
  assert.equal(meta.messages[2].caption, 'final 4000');
  assert.equal(files.length, 1);
  assert.equal(key, idemKey('whatsapp', GROUP, ['K1', 'K2', 'K3']));
});

function poster(spool, responses) {
  const calls = [];
  const { lines, log } = captureLog();
  const p = new Poster({ cfg: cfgFor(), spool, log, sleep: async () => {}, random: () => 0.5,
    fetchImpl: async (url, init) => {
      calls.push(init);
      const r = responses.shift();
      if (r instanceof Error) throw r;
      return new Response(JSON.stringify(r[1] ?? {}), { status: r[0] });
    } });
  return { p, calls, lines };
}

test('202 deletes the spooled album; 200 duplicate too', async () => {
  for (const status of [202, 200]) {
    const spool = new Spool(tmp());
    const id = readyAlbum(spool);
    const { p, calls } = poster(spool, [[status]]);
    assert.equal(await p.postAlbum(id), 'done');
    assert.deepEqual(spool.list('ready'), []);
    assert.equal(calls[0].headers['Idempotency-Key'].length, 64);
    assert.equal(calls[0].headers['X-Ingest-Token'], 't'.repeat(32));
  }
});

test('422 rejects; 401 keeps it for a slow retry; 500 then 202 retries and deletes', async () => {
  let spool = new Spool(tmp());
  let id = readyAlbum(spool);
  assert.equal(await poster(spool, [[422, { detail: 'invalid_meta' }]]).p.postAlbum(id), 'done');
  assert.deepEqual(spool.list('rejected'), [id]);

  spool = new Spool(tmp());
  id = readyAlbum(spool);
  assert.equal(await poster(spool, [[401]]).p.postAlbum(id), 'config');
  assert.deepEqual(spool.list('ready'), [id]);

  spool = new Spool(tmp());
  id = readyAlbum(spool);
  const { p, calls } = poster(spool, [[500], new TypeError('fetch failed'), [202]]);
  p.enqueue(id);
  while (p.running) await new Promise((r) => setTimeout(r, 5));
  assert.equal(calls.length, 3);
  assert.deepEqual(spool.list('ready'), []);
  assert.equal(calls[0].headers['Idempotency-Key'], calls[2].headers['Idempotency-Key']); // same key on retry
});

test('a stale album (over 7 days) is rejected without posting', async () => {
  const spool = new Spool(tmp());
  const id = readyAlbum(spool, 'old', { lastArrivalMs: Date.now() - 8 * 86_400_000 });
  const { p, calls } = poster(spool, []);
  assert.equal(await p.postAlbum(id), 'done');
  assert.equal(calls.length, 0);
});

test('poster logs carry no JID, number or caption', async () => {
  const spool = new Spool(tmp());
  const id = readyAlbum(spool);
  const { p, lines } = poster(spool, [[202]]);
  await p.postAlbum(id);
  const text = lines.join('\n');
  assert.ok(!text.includes('923001234567') && !text.includes('@g.us') && !text.includes('Nike'));
});
