// ThriftRadar listener (DESIGN §4.1 main): a read-only linked device that watches one WhatsApp group and posts
// each seller's album to the loopback /ingest. Exit codes: 0 normal, 1 config, 2 logged out, 3 another instance
// or connection replaced, 4 bad session/auth, 5 reconnect storm, 6 forbidden.
import { existsSync, renameSync } from 'node:fs';
import path from 'node:path';
import pino from 'pino';
import { useMultiFileAuthState } from 'baileys';
import { AlbumBuffer } from './album.js';
import { ConfigError, loadConfig, PKG_DIR, VERSION } from './config.js';
import { Connection } from './connection.js';
import { classifyMessage } from './filter.js';
import { makeHeartbeat } from './heartbeat.js';
import { acquireLock, LockHeld } from './lock.js';
import { listGroups } from './listGroups.js';
import { downloadImage } from './media.js';
import { Poster } from './poster.js';
import { makeMasker } from './redact.js';
import { SeenStore } from './seen.js';
import { Spool } from './spool.js';

const HELP = `usage: node src/index.js [--list-groups] [--reset-auth] [--help]
  --list-groups  pair if needed, print the groups this account is in (JID, name, members), then exit
  --reset-auth   move auth/ aside (never deleted) and pair again with a new QR code`;

export async function main(argv = process.argv.slice(2)) {
  if (argv.includes('--help')) {
    console.log(HELP);
    return 0;
  }
  const envFile = path.join(PKG_DIR, '.env');
  if (existsSync(envFile)) process.loadEnvFile(envFile);
  const isList = argv.includes('--list-groups');
  let cfg;
  try {
    cfg = loadConfig(process.env, { listGroups: isList });
  } catch (e) {
    if (e instanceof ConfigError) {
      console.error(`config error: ${e.message}`);
      return 1;
    }
    throw e;
  }
  const log = pino({ level: process.env.LOG_LEVEL ?? 'info', base: undefined });
  // Baileys' own log can contain JIDs, so it goes to a file in the gitignored state/ dir, errors only.
  const baileysLogger = pino({ level: 'error' }, pino.destination({ dest: path.join(cfg.stateDir, 'baileys.log'), mkdir: true }));

  let release;
  try {
    release = acquireLock(path.join(cfg.stateDir, 'listener.lock'));
  } catch (e) {
    if (e instanceof LockHeld) {
      console.error(`another listener is already running (${e.message})`);
      return 3;
    }
    throw e;
  }

  try {
    if (argv.includes('--reset-auth') && existsSync(cfg.authDir)) {
      const moved = `${cfg.authDir}.old-${Date.now()}`;
      renameSync(cfg.authDir, moved);
      console.log(`moved the old session to ${moved}; scan the new QR code to pair`);
    }
    let auth;
    try {
      auth = await useMultiFileAuthState(cfg.authDir);
    } catch {
      log.error('auth_unreadable: the session files in auth/ are unreadable; run with --reset-auth to re-pair');
      return 4;
    }
    if (isList) return await listGroups(cfg, auth, baileysLogger);
    return await run(cfg, auth, log, baileysLogger);
  } finally {
    release();
  }
}

async function run(cfg, auth, log, baileysLogger) {
  const mask = makeMasker(cfg.logSalt);
  const spool = new Spool(cfg.spoolDir);
  spool.pruneRejected();
  const seen = new SeenStore(path.join(cfg.stateDir, 'seen.log')).load();
  const inflight = new Set();
  const counts = {};
  const bump = (k) => { counts[k] = (counts[k] ?? 0) + 1; };
  let lastMessageAt = null;
  let accepting = true;

  const poster = new Poster({ cfg, spool, log });
  const album = new AlbumBuffer({ cfg, spool, onReady: (id) => poster.enqueue(id), onDropped: bump });
  const restored = album.restoreFromSpool();
  if (restored.flushed + restored.rearmed + restored.ready + restored.rejected) log.info(restored, 'spool restored');

  const snapshot = () => ({
    last_message_at: lastMessageAt, spool_open: spool.list('open').length, spool_ready: spool.list('ready').length,
    drop_counts: { ...counts, posted: poster.stats.posted, duplicate: poster.stats.duplicate, rejected: poster.stats.rejected },
  });
  const heartbeat = makeHeartbeat({ cfg, version: VERSION, snapshot, log });

  let exitWith;
  const exited = new Promise((r) => { exitWith = r; });
  let conn;

  async function handleAccepted(sock, msg, cls) {
    if (seen.has(cls.seenKey) || inflight.has(cls.seenKey)) return bump('duplicate');
    inflight.add(cls.seenKey);
    try {
      const item = { key: cls.key, kind: cls.action, ts: cls.ts, chat: cfg.groupJid, sender: cls.senderId,
        senderAlt: cls.senderAlt, caption: cls.action === 'text' ? cls.text : cls.caption, bytes: null };
      const t0 = Date.now();
      if (cls.action === 'image') {
        const res = await downloadImage(sock, msg, cls, cfg, { logger: baileysLogger });
        if (res.ok) item.bytes = res.bytes;
        else Object.assign(item, { missing: true, reason: res.reason });
      }
      album.add(item);
      seen.add(cls.seenKey);
      lastMessageAt = new Date(cls.ts * 1000).toISOString();
      bump(`accepted_${cls.action}${item.missing ? '_missing' : ''}`);
      log.info({ kind: cls.action, sender: mask(cls.senderId), bytes: item.bytes?.length ?? 0,
        missing: item.missing ? item.reason : undefined, ms: Date.now() - t0 }, 'accepted');
    } finally {
      inflight.delete(cls.seenKey);
    }
  }

  conn = new Connection({
    cfg, state: auth.state, saveCreds: auth.saveCreds, logger: baileysLogger,
    hooks: {
      log,
      heartbeat,
      exit: (code, reason) => exitWith({ code, reason }),
      onSocket: (sock) => sock.ev.on('messages.upsert', async ({ messages, type }) => {
        const nowSec = Math.floor(Date.now() / 1000);
        for (const msg of messages) { // in order, to keep album order
          if (!accepting) return;
          try {
            const cls = classifyMessage(msg, type, cfg, nowSec);
            if (cls.action === 'drop') bump(cls.reason);
            else await handleAccepted(sock, msg, cls);
          } catch (e) {
            bump('error');
            log.error({ err: e.name }, 'message handling failed'); // no message content in logs
          }
        }
      }),
    },
  });

  log.info({ version: VERSION, group: mask(cfg.groupJid) }, 'listener starting');
  void heartbeat('connecting');
  conn.connect();
  const hb = setInterval(() => void heartbeat(conn.state_), 60_000);
  const summary = setInterval(() => log.info({ counts }, 'summary'), 60_000);

  const onSignal = (sig) => exitWith({ code: 0, reason: sig });
  process.once('SIGINT', onSignal);
  process.once('SIGTERM', onSignal);

  const { code, reason } = await exited;
  accepting = false;
  clearInterval(hb);
  clearInterval(summary);
  const deadline = Date.now() + 5_000;
  while (inflight.size && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
  conn.end(); // never logout(): that would unlink this device from the phone
  poster.stop();
  if (code === 0) await heartbeat('stopped', 'shutdown'); // the backend doesn't notify for a deliberate stop
  log.info({ code, reason }, 'listener exiting'); // open albums stay spooled and resume on restart
  return code;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then((code) => process.exit(code), (e) => {
    console.error(`fatal: ${e.name}: ${e.message}`);
    process.exit(1);
  });
}
