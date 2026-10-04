import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { DENIED, readOnlyGuard, ReadOnlyViolation } from '../src/connection.js';

const fake = () => Object.fromEntries([...DENIED, 'end', 'groupFetchAllParticipating', 'updateProfileName']
  .map((k) => [k, () => k]));

test('every user-visible action throws through the guard', () => {
  const sock = readOnlyGuard(fake());
  for (const name of [...DENIED, 'updateProfileName']) {
    assert.throws(() => sock[name], ReadOnlyViolation, name);
  }
  assert.equal(sock.end(), 'end');
  assert.equal(sock.groupFetchAllParticipating(), 'groupFetchAllParticipating');
});

test('updateMediaMessage is allowed only with ALLOW_MEDIA_REUPLOAD', () => {
  assert.throws(() => readOnlyGuard(fake()).updateMediaMessage, ReadOnlyViolation);
  assert.equal(readOnlyGuard(fake(), { allowMediaReupload: true }).updateMediaMessage(), 'updateMediaMessage');
});

test('static scan: no forbidden call anywhere in src/ outside the deny list', () => {
  const dir = fileURLToPath(new URL('../src/', import.meta.url));
  const names = [...DENIED].filter((n) => n !== 'updateMediaMessage');
  for (const f of readdirSync(dir)) {
    let text = readFileSync(path.join(dir, f), 'utf8');
    if (f === 'connection.js') text = text.replace(/export const DENIED = new Set\(\[[\s\S]*?\]\);/, '');
    for (const n of names) {
      assert.ok(!new RegExp(`\\.${n}\\b|\\['${n}'\\]`).test(text), `${f} uses ${n}`);
    }
    assert.ok(!/\.logout\(/.test(text), `${f} calls logout()`);
  }
});
