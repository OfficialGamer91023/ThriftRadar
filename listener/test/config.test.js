import assert from 'node:assert/strict';
import { test } from 'node:test';
import path from 'node:path';
import { existsSync, statSync } from 'node:fs';
import { ConfigError, loadConfig } from '../src/config.js';
import { GROUP, tmp } from './helpers.js';

const ok = { THRIFT_GROUP_JID: GROUP, INGEST_TOKEN: 'x'.repeat(32) };

test('rejects a non-loopback INGEST_URL', () => {
  for (const url of ['http://192.168.1.5:8000/ingest', 'http://example.com/ingest', 'http://127.0.0.1.nip.io/ingest']) {
    assert.throws(() => loadConfig({ ...ok, INGEST_URL: url }, { mkdirs: false }), ConfigError, url);
  }
  for (const url of ['http://127.0.0.1:8000/ingest', 'http://localhost:8000/ingest', 'http://[::1]:8000/ingest']) {
    assert.equal(loadConfig({ ...ok, INGEST_URL: url }, { mkdirs: false }).ingestUrl, new URL(url).href);
  }
});

test('rejects a bad group JID and a short token', () => {
  assert.throws(() => loadConfig({ ...ok, THRIFT_GROUP_JID: '923001234567@s.whatsapp.net' }, { mkdirs: false }), ConfigError);
  assert.throws(() => loadConfig({ ...ok, INGEST_TOKEN: 'short' }, { mkdirs: false }), ConfigError);
  assert.doesNotThrow(() => loadConfig({}, { listGroups: true, mkdirs: false })); // --list-groups needs neither
});

test('dirs resolve inside the listener package, private, whatever the cwd', () => {
  const pkg = tmp();
  const prev = process.cwd();
  process.chdir('/');
  try {
    const cfg = loadConfig(ok, { pkgDir: pkg });
    assert.equal(cfg.authDir, path.join(pkg, 'auth'));
    assert.equal(statSync(path.join(pkg, 'spool', 'open')).mode & 0o777, 0o700);
    assert.equal(statSync(path.join(pkg, 'state', 'log_salt')).mode & 0o777, 0o600);
    assert.equal(cfg.logSalt.length, 64);
    assert.ok(existsSync(path.join(pkg, 'spool', 'rejected')));
  } finally {
    process.chdir(prev);
  }
});

test('heartbeat URL is on the same loopback origin', () => {
  assert.equal(loadConfig(ok, { mkdirs: false }).heartbeatUrl, 'http://127.0.0.1:8000/listener/heartbeat');
});
