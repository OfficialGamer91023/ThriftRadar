import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

export const GROUP = '120363012345678901@g.us';
export const SELLER = '923001234567@s.whatsapp.net';

export function tmp() {
  return mkdtempSync(path.join(tmpdir(), 'trl-'));
}

export function cfgFor(dir = tmp(), extra = {}) {
  return {
    groupJid: GROUP, ingestUrl: 'http://127.0.0.1:8000/ingest', heartbeatUrl: 'http://127.0.0.1:8000/listener/heartbeat',
    ingestToken: 't'.repeat(32), albumIdleMs: 60_000, albumMaxSpanMs: 180_000, albumMaxImages: 30,
    albumMaxBytes: 62_914_560, maxOpenAlbums: 50, maxBacklogAgeS: 86_400, maxFileBytes: 15 * 1024 * 1024,
    allowMediaReupload: false, filterJids: true, spoolDir: path.join(dir, 'spool'), stateDir: dir, logSalt: 's',
    ...extra,
  };
}

/** A Baileys-shaped group message. */
export function wa({ id = 'M1', remoteJid = GROUP, participant = SELLER, participantAlt, fromMe = false, ts = 1_000_000,
  message, stub } = {}) {
  return { key: { remoteJid, id, participant, participantAlt, fromMe }, messageTimestamp: ts,
    message: message === undefined ? { imageMessage: { caption: 'Nike AF1 size 42 Rs 4500', mimetype: 'image/jpeg', fileLength: 200_000 } } : message,
    messageStubType: stub };
}

export function captureLog() {
  const lines = [];
  const rec = (level) => (obj, msg) => lines.push(JSON.stringify({ level, ...(typeof obj === 'object' ? obj : { msg: obj }), msg }));
  return { lines, log: { info: rec('info'), warn: rec('warn'), error: rec('error'), debug: rec('debug') } };
}
