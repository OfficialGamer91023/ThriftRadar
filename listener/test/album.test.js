import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { AlbumBuffer } from '../src/album.js';
import { Spool } from '../src/spool.js';
import { cfgFor, tmp } from './helpers.js';

const CASES = JSON.parse(readFileSync(fileURLToPath(new URL('../../shared/grouping_cases.json', import.meta.url)), 'utf8'));

function harness(extra = {}, { now = () => 0 } = {}) {
  const dir = tmp();
  const cfg = cfgFor(dir, extra);
  const spool = new Spool(cfg.spoolDir);
  const ready = [];
  const dropped = [];
  const timers = [];
  const album = new AlbumBuffer({ cfg, spool, onReady: (id) => ready.push(id), onDropped: (r) => dropped.push(r), now,
    setTimer: (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; }, clearTimer: (t) => { t.cleared = true; } });
  return { cfg, spool, album, ready, dropped, timers };
}

for (const c of CASES.cases) {
  test(`shared grouping case: ${c.name}`, () => {
    const p = { ...CASES.defaults, ...(c.params ?? {}) };
    const { album, spool, ready } = harness({ albumIdleMs: p.idle_s * 1000, albumMaxSpanMs: p.max_span_s * 1000,
      albumMaxImages: p.max_images, albumMaxBytes: p.max_bytes });
    const order = new Map();
    c.messages.forEach((m, i) => { if (!order.has(m.key)) order.set(m.key, i); });
    for (const m of c.messages) {
      album.add({ key: m.key, kind: m.kind, ts: m.ts, chat: m.chat, sender: m.sender, caption: m.caption ?? null,
        bytes: m.kind === 'image' && !m.missing ? Buffer.alloc(1) : null, size: m.bytes ?? 0, missing: Boolean(m.missing) });
    }
    album.flushAll();
    const groups = ready.map((id) => spool.readMeta('ready', id).items.map((x) => x.key))
      .sort((a, b) => order.get(a[0]) - order.get(b[0]));
    assert.deepEqual(groups, c.expected);
  });
}

test('the same add twice keeps one item, and image bytes go to the spool', () => {
  const { album, spool, ready } = harness();
  const item = { key: 'a1', kind: 'image', ts: 10, chat: 'g', sender: 's', bytes: Buffer.from('jpeg') };
  album.add(item);
  album.add(item);
  album.flushAll();
  const meta = spool.readMeta('ready', ready[0]);
  assert.equal(meta.items.length, 1);
  assert.equal(spool.readFile('ready', ready[0], meta.items[0].file).toString(), 'jpeg');
});

test('text-only and all-missing albums are dropped, never posted', () => {
  const { album, ready, dropped } = harness();
  album.add({ key: 't1', kind: 'text', ts: 0, chat: 'g', sender: 'a', caption: 'price?' });
  album.add({ key: 'm1', kind: 'image', ts: 0, chat: 'g', sender: 'b', missing: true, reason: 'expired' });
  album.flushAll();
  assert.deepEqual(ready, []);
  assert.deepEqual(dropped.sort(), ['dropped_all_media_missing', 'dropped_text_only']);
});

test('the 51st open album flushes the oldest', () => {
  let t = 0;
  const { album, ready } = harness({}, { now: () => t++ });
  for (let i = 0; i < 51; i++) album.add({ key: `k${i}`, kind: 'image', ts: 0, chat: 'g', sender: `s${i}`, bytes: Buffer.from('x') });
  assert.equal(ready.length, 1);
  assert.equal(album.open.size, 50);
});

test('the timer is armed for idle + grace, capped by the span', () => {
  const { album, timers } = harness();
  album.add({ key: 'a1', kind: 'image', ts: 0, chat: 'g', sender: 's', bytes: Buffer.from('x') });
  assert.equal(timers.at(-1).ms, 65_000);
  timers.at(-1).fn();
  assert.equal(album.open.size, 0);
});

test('restore: an overdue open album is flushed, a fresh one re-armed, a corrupt one rejected', () => {
  const { album, spool, ready } = harness({}, { now: () => 1_000_000 });
  const mk = (id, lastArrivalMs, sender) => spool.writeMeta('open', id, { id, chat: 'g', sender, senderAlt: null, firstTs: 0,
    lastTs: 0, firstArrivalMs: lastArrivalMs, lastArrivalMs, images: 1, totalBytes: 1,
    items: [{ key: `${id}k`, kind: 'image', ts: 0, file: spool.writeItem(id, `${id}k`, Buffer.from('x')), missing: false }] });
  mk('old', 1_000_000 - 120_000, 's1');
  mk('fresh', 1_000_000 - 1_000, 's2');
  mkdirSync(spool.path('open', 'bad'), { recursive: true });
  writeFileSync(spool.path('open', 'bad', 'meta.json'), '{nope');
  const r = album.restoreFromSpool();
  assert.deepEqual(r, { flushed: 1, rearmed: 1, rejected: 1, ready: 0 });
  assert.deepEqual(ready, ['old']);
  assert.ok(album.open.has(AlbumBuffer.bufKey('g', 's2')));
  assert.ok(spool.list('rejected').includes('bad'));
});

test('restore re-enqueues ready albums', () => {
  const { album, spool, ready } = harness();
  spool.writeMeta('ready', 'r1', { items: [] });
  album.restoreFromSpool();
  assert.deepEqual(ready, ['r1']);
  assert.ok(path.isAbsolute(spool.dir));
});
