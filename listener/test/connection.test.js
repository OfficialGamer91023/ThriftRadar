import assert from 'node:assert/strict';
import { test } from 'node:test';
import { closeAction, Connection, isIgnoredJid } from '../src/connection.js';
import { cfgFor, GROUP } from './helpers.js';

test('every close code maps to the right action and exit code', () => {
  const t = (c) => { const a = closeAction(c); return [a.action, a.exitCode ?? null, a.state]; };
  assert.deepEqual(t(515), ['reconnect', null, 'reconnecting']);
  assert.equal(closeAction(515).delay, 0);
  assert.deepEqual(t(401), ['exit', 2, 'logged_out']);
  assert.deepEqual(t(440), ['exit', 3, 'replaced']);
  assert.deepEqual(t(500), ['exit', 4, 'bad_session']);
  assert.deepEqual(t(411), ['exit', 4, 'bad_session']);
  assert.deepEqual(t(403), ['exit', 6, 'forbidden']);
  for (const c of [428, 408, 503, undefined, 999]) assert.deepEqual(t(c), ['reconnect', null, 'reconnecting']);
});

function conn() {
  let t = 0;
  const timers = [];
  const exits = [];
  const beats = [];
  const sockets = [];
  const c = new Connection({ cfg: cfgFor(), state: { creds: {}, keys: {} }, saveCreds: () => {}, logger: null,
    hooks: { heartbeat: (s) => beats.push(s), exit: (code) => exits.push(code) }, now: () => t, random: () => 1,
    setTimer: (fn, ms) => { const x = { fn, ms }; timers.push(x); return x; }, clearTimer: () => {},
    create: () => { const s = { ev: { on: () => {}, removeAllListeners: () => {} }, end: () => {} }; sockets.push(s); return s; },
    showQr: () => {} });
  return { c, timers, exits, beats, sockets, tick: (ms) => { t += ms; } };
}

test('two closes in a row schedule one reconnect timer', () => {
  const { c, timers } = conn();
  c.connect();
  c.onConnectionUpdate({ connection: 'close', lastDisconnect: { error: { output: { statusCode: 428 } } } });
  c.onConnectionUpdate({ connection: 'close', lastDisconnect: { error: { output: { statusCode: 428 } } } });
  assert.equal(timers.length, 1);
});

test('backoff grows and is capped at 5 minutes', () => {
  const { c, timers } = conn();
  c.connect();
  const delays = [];
  for (let i = 0; i < 9; i++) {
    c.scheduleReconnect();
    delays.push(timers.at(-1).ms);
    timers.at(-1).fn(); // fire: clears the guard and reconnects
  }
  assert.deepEqual(delays.slice(0, 4), [2000, 4000, 8000, 16000]);
  assert.equal(Math.max(...delays), 300_000);
});

test('10 failures within 30 minutes exits 5 after a stopped heartbeat', async () => {
  const { c, timers, exits, beats } = conn();
  c.connect();
  for (let i = 0; i < 10; i++) {
    c.scheduleReconnect();
    if (c.reconnectTimer) timers.at(-1).fn();
  }
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(exits, [5]);
  assert.ok(beats.includes('stopped'));
});

test('logged out exits 2 and never reconnects', async () => {
  const { c, timers, exits } = conn();
  c.connect();
  c.onConnectionUpdate({ connection: 'close', lastDisconnect: { error: { output: { statusCode: 401 } } } });
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(exits, [2]);
  assert.equal(timers.length, 0);
});

test('a QR moves to awaiting_qr; open sends a heartbeat', () => {
  const { c, beats } = conn();
  c.connect();
  c.onConnectionUpdate({ qr: 'xyz' });
  c.onConnectionUpdate({ connection: 'open' });
  assert.ok(beats.includes('awaiting_qr') && beats.includes('open'));
});

test('jid filter: only the target group and our own account pass', () => {
  const me = { id: '923000000000:12@s.whatsapp.net', lid: '555@lid' };
  assert.equal(isIgnoredJid(GROUP, GROUP, me), false);
  assert.equal(isIgnoredJid('120363099999999999@g.us', GROUP, me), true);
  assert.equal(isIgnoredJid('status@broadcast', GROUP, me), true);
  assert.equal(isIgnoredJid('123@newsletter', GROUP, me), true);
  assert.equal(isIgnoredJid('923001234567@s.whatsapp.net', GROUP, me), true);
  assert.equal(isIgnoredJid('923000000000@s.whatsapp.net', GROUP, me), false);
  assert.equal(isIgnoredJid('555@lid', GROUP, me), false);
});
