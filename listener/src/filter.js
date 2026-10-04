// classifyMessage (DESIGN §4.1): pure. Cheapest and most privacy-protective checks first; nothing from another
// chat is even looked at.
import { createHash } from 'node:crypto';
import { getContentType, normalizeMessageContent } from 'baileys';

const IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp']);
const VIEW_ONCE = ['viewOnceMessage', 'viewOnceMessageV2', 'viewOnceMessageV2Extension'];

const drop = (reason) => ({ action: 'drop', reason });

export function seenKeyOf(msg) {
  const k = msg.key;
  return createHash('sha256').update(`${k.remoteJid}|${k.id}|${k.participant ?? ''}`).digest('hex').slice(0, 24);
}

export function classifyMessage(msg, type, cfg, nowSec, normalize = normalizeMessageContent) {
  const key = msg?.key ?? {};
  if (key.remoteJid !== cfg.groupJid) return drop('other_chat');
  if (key.fromMe) return drop('from_me');
  if (!msg.message || msg.messageStubType != null) return drop('stub'); // not marked seen: decrypted copy follows
  if (type !== 'notify' && type !== 'append') return drop('unknown_type');
  const ts = Number(msg.messageTimestamp);
  if (!Number.isFinite(ts) || nowSec - ts > cfg.maxBacklogAgeS) return drop('too_old');
  if (!key.participant) return drop('no_sender');
  if (VIEW_ONCE.some((w) => msg.message[w])) return drop('view_once');

  const content = normalize(msg.message);
  if (!content) return drop('stub');
  if (content.protocolMessage) return drop('protocol');
  const base = {
    key: key.id,
    seenKey: seenKeyOf(msg),
    senderId: key.participant,
    senderAlt: key.participantAlt ?? null,
    ts,
  };
  if (content.imageMessage) {
    const im = content.imageMessage;
    if (im.viewOnce) return drop('view_once');
    return { ...base, action: 'image', caption: im.caption ?? '', fileLength: Number(im.fileLength ?? 0),
      mimetype: im.mimetype ?? 'image/jpeg' };
  }
  if (content.documentMessage && IMAGE_TYPES.has(content.documentMessage.mimetype)) {
    const d = content.documentMessage;
    return { ...base, action: 'image', caption: d.caption ?? '', fileLength: Number(d.fileLength ?? 0),
      mimetype: d.mimetype };
  }
  const text = content.conversation ?? content.extendedTextMessage?.text;
  if (typeof text === 'string' && text.length) return { ...base, action: 'text', text };
  return drop(`unsupported:${getContentType(content) ?? 'unknown'}`);
}
