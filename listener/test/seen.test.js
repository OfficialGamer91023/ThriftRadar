import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { SeenStore } from '../src/seen.js';
import { seenKeyOf } from '../src/filter.js';
import { tmp, wa } from './helpers.js';

test('round trip and compaction keep the last N', () => {
  const file = path.join(tmp(), 'seen.log');
  const s = new SeenStore(file, 3).load();
  for (let i = 0; i < 7; i++) s.add(`k${i}`);
  assert.ok(s.has('k6'));
  const again = new SeenStore(file, 3).load();
  assert.ok(again.has('k6') && again.has('k4') && !again.has('k0'));
});

test('the file holds hashes only, no JIDs', () => {
  const file = path.join(tmp(), 'seen.log');
  const s = new SeenStore(file).load();
  s.add(seenKeyOf(wa()));
  const text = readFileSync(file, 'utf8');
  assert.ok(!text.includes('whatsapp.net') && !text.includes('923001234567') && !text.includes('@g.us'));
});
