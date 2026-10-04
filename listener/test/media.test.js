import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Readable } from 'node:stream';
import { downloadImage } from '../src/media.js';
import { cfgFor, wa } from './helpers.js';

const cfg = cfgFor();
const cls = { fileLength: 1000, mimetype: 'image/jpeg' };
const noSleep = { sleep: async () => {} };
const boom = (status) => Object.assign(new Error('http'), { output: { statusCode: status } });

test('over 15 MB declared: no network call', async () => {
  let called = 0;
  const r = await downloadImage({}, wa(), { ...cls, fileLength: 16 * 1024 * 1024 }, cfg, { download: async () => { called++; }, ...noSleep });
  assert.deepEqual(r, { ok: false, reason: 'too_large' });
  assert.equal(called, 0);
});

test('a 410 is expired: no retry, and no reupload request is ever passed', async () => {
  let called = 0;
  let ctxSeen = 'unset';
  const r = await downloadImage({}, wa(), cls, cfg, { download: async (m, t, o, ctx) => { called++; ctxSeen = ctx; throw boom(410); }, ...noSleep });
  assert.deepEqual(r, { ok: false, reason: 'expired' });
  assert.equal(called, 1);
  assert.equal(ctxSeen, undefined);
});

test('two network failures then success', async () => {
  let n = 0;
  const r = await downloadImage({}, wa(), cls, cfg, { ...noSleep,
    download: async () => { if (++n < 3) throw new Error('ECONNRESET'); return Readable.from([Buffer.from('ab'), Buffer.from('cd')]); } });
  assert.equal(r.ok, true);
  assert.equal(r.bytes.toString(), 'abcd');
  assert.equal(n, 3);
});

test('streaming past 15 MB is aborted', async () => {
  const big = Buffer.alloc(8 * 1024 * 1024);
  const r = await downloadImage({}, wa(), cls, cfg, { ...noSleep, download: async () => Readable.from([big, big]) });
  assert.deepEqual(r, { ok: false, reason: 'too_large' });
});

test('a hung download times out and is retried, then gives up', async () => {
  let n = 0;
  const r = await downloadImage({}, wa(), cls, cfg, { ...noSleep, timeoutMs: 20,
    download: async () => { n++; return new Readable({ read() {} }); } });
  assert.deepEqual(r, { ok: false, reason: 'download_failed' });
  assert.equal(n, 3);
});

test('a non-image type is refused before the network', async () => {
  const r = await downloadImage({}, wa(), { fileLength: 10, mimetype: 'video/mp4' }, cfg, { download: async () => { throw new Error('no'); } });
  assert.deepEqual(r, { ok: false, reason: 'bad_type' });
});
