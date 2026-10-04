// loadConfig (DESIGN §4.1). Every check fails with a specific message; paths resolve relative to the listener
// package, never the cwd, so auth/, spool/ and state/ always land in the gitignored listener/ dirs.
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

export const PKG_DIR = fileURLToPath(new URL('..', import.meta.url));
export const VERSION = '0.1.0';
const LOOPBACK = new Set(['127.0.0.1', '[::1]', 'localhost']);
const GROUP_JID = /^\d+(-\d+)?@g\.us$/;

export class ConfigError extends Error {}

function intIn(env, name, def, min, max) {
  const raw = env[name];
  if (raw === undefined || raw === '') return def;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < min || n > max) throw new ConfigError(`${name} must be an integer in ${min}..${max}`);
  return n;
}

export function loadConfig(env = process.env, { listGroups = false, pkgDir = PKG_DIR, mkdirs = true } = {}) {
  const groupJid = (env.THRIFT_GROUP_JID ?? '').trim();
  if (!listGroups && !GROUP_JID.test(groupJid)) {
    throw new ConfigError('THRIFT_GROUP_JID must look like 1203630xxxxxxxxx@g.us (find it with --list-groups)');
  }
  let ingestUrl;
  try {
    ingestUrl = new URL(env.INGEST_URL || 'http://127.0.0.1:8000/ingest');
  } catch {
    throw new ConfigError('INGEST_URL is not a valid URL');
  }
  if (!LOOPBACK.has(ingestUrl.hostname) || !['http:', 'https:'].includes(ingestUrl.protocol)) {
    throw new ConfigError('INGEST_URL must point at this Mac (127.0.0.1, ::1 or localhost): payloads never leave it');
  }
  const ingestToken = env.INGEST_TOKEN ?? '';
  if (!listGroups && ingestToken.length < 32) throw new ConfigError('INGEST_TOKEN must be at least 32 characters');

  const cfg = {
    groupJid: groupJid || null,
    ingestUrl: ingestUrl.href,
    heartbeatUrl: new URL('/listener/heartbeat', ingestUrl).href,
    ingestToken,
    albumIdleMs: intIn(env, 'ALBUM_IDLE_MS', 60_000, 5_000, 600_000),
    albumMaxSpanMs: intIn(env, 'ALBUM_MAX_SPAN_MS', 180_000, 10_000, 1_800_000),
    albumMaxImages: intIn(env, 'ALBUM_MAX_IMAGES', 30, 1, 60),
    albumMaxBytes: intIn(env, 'ALBUM_MAX_BYTES', 62_914_560, 1_000_000, 200_000_000),
    maxOpenAlbums: intIn(env, 'MAX_OPEN_ALBUMS', 50, 1, 500),
    maxBacklogAgeS: intIn(env, 'MAX_BACKLOG_AGE_S', 86_400, 60, 7 * 86_400),
    maxFileBytes: 15 * 1024 * 1024,
    allowMediaReupload: env.ALLOW_MEDIA_REUPLOAD === '1', // Q8: off, so no media-retry receipt reaches sellers
    filterJids: env.FILTER_JIDS !== '0', // shouldIgnoreJid first layer; '0' disables it if decryption suffers
    authDir: path.join(pkgDir, 'auth'),
    spoolDir: path.join(pkgDir, 'spool'),
    stateDir: path.join(pkgDir, 'state'),
  };
  if (mkdirs) {
    for (const d of [cfg.stateDir, ...['open', 'ready', 'rejected'].map((s) => path.join(cfg.spoolDir, s))]) {
      mkdirSync(d, { recursive: true, mode: 0o700 });
    }
    const saltFile = path.join(cfg.stateDir, 'log_salt');
    if (!existsSync(saltFile)) writeFileSync(saltFile, randomBytes(32).toString('hex'), { mode: 0o600 });
    chmodSync(saltFile, 0o600);
    cfg.logSalt = readFileSync(saltFile, 'utf8').trim();
  } else {
    cfg.logSalt = 'test-salt';
  }
  return cfg;
}
