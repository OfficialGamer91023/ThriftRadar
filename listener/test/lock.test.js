import assert from 'node:assert/strict';
import { test } from 'node:test';
import { writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { acquireLock, LockHeld } from '../src/lock.js';
import { makeMasker } from '../src/redact.js';
import { tmp } from './helpers.js';

test('a second instance is refused; a stale lock is replaced', () => {
  const file = path.join(tmp(), 'listener.lock');
  const release = acquireLock(file);
  assert.throws(() => acquireLock(file, process.pid + 1_000_000), LockHeld);
  release();
  assert.ok(!existsSync(file));
  writeFileSync(file, '999999999'); // no such process
  acquireLock(file)();
});

test('maskJid is stable per salt and hides the number', () => {
  const m = makeMasker('salt');
  const out = m('923001234567@s.whatsapp.net');
  assert.match(out, /^u:[0-9a-f]{8}$/);
  assert.equal(out, m('923001234567@s.whatsapp.net'));
  assert.notEqual(out, makeMasker('other')('923001234567@s.whatsapp.net'));
});
