import assert from 'node:assert/strict';
import { test } from 'node:test';
import { classifyMessage } from '../src/filter.js';
import { cfgFor, GROUP, wa } from './helpers.js';

const cfg = cfgFor();
const now = 1_000_100;
const cls = (m, type = 'notify') => classifyMessage(m, type, cfg, now);

test('other chats, DMs and our own messages are dropped first', () => {
  assert.equal(cls(wa({ remoteJid: '120363099999999999@g.us' })).reason, 'other_chat');
  assert.equal(cls(wa({ remoteJid: '923001234567@s.whatsapp.net' })).reason, 'other_chat');
  assert.equal(cls(wa({ fromMe: true })).reason, 'from_me');
});

test('stubs (including CIPHERTEXT placeholders) are dropped without content', () => {
  assert.equal(cls(wa({ stub: 2, message: null })).reason, 'stub');
  assert.equal(cls(wa({ message: null })).reason, 'stub');
});

test('append within 24 h is kept, older is too_old, other types are unknown', () => {
  assert.equal(cls(wa(), 'append').action, 'image');
  assert.equal(cls(wa({ ts: now - 3 * 86_400 }), 'append').reason, 'too_old');
  assert.equal(cls(wa(), 'notify').action, 'image');
  assert.equal(cls(wa(), 'history').reason, 'unknown_type');
});

test('image fields, caption and sender', () => {
  const r = cls(wa({ participantAlt: '923001234567@s.whatsapp.net', participant: '123456789@lid' }));
  assert.equal(r.action, 'image');
  assert.equal(r.caption, 'Nike AF1 size 42 Rs 4500');
  assert.equal(r.fileLength, 200_000);
  assert.equal(r.senderId, '123456789@lid');
  assert.equal(r.senderAlt, '923001234567@s.whatsapp.net');
  assert.equal(r.seenKey.length, 24);
  assert.equal(cls(wa({ participant: '' })).reason, 'no_sender');
});

test('wrappers: view-once dropped; ephemeral and document-with-caption unwrapped', () => {
  const img = { imageMessage: { mimetype: 'image/jpeg', caption: 'x' } };
  assert.equal(cls(wa({ message: { viewOnceMessageV2: { message: img } } })).reason, 'view_once');
  assert.equal(cls(wa({ message: { imageMessage: { ...img.imageMessage, viewOnce: true } } })).reason, 'view_once');
  assert.equal(cls(wa({ message: { ephemeralMessage: { message: img } } })).action, 'image');
  const doc = { documentMessage: { mimetype: 'image/png', caption: 'uncompressed', fileLength: 9 } };
  const r = cls(wa({ message: { documentWithCaptionMessage: { message: doc } } }));
  assert.equal(r.action, 'image');
  assert.equal(r.caption, 'uncompressed');
  assert.equal(cls(wa({ message: { documentMessage: { mimetype: 'application/pdf' } } })).reason, 'unsupported:documentMessage');
});

test('text, protocol and unsupported kinds', () => {
  assert.deepEqual([cls(wa({ message: { conversation: 'price?' } })).action, cls(wa({ message: { conversation: 'price?' } })).text], ['text', 'price?']);
  assert.equal(cls(wa({ message: { extendedTextMessage: { text: 'size 42' } } })).text, 'size 42');
  assert.equal(cls(wa({ message: { protocolMessage: { type: 0 } } })).reason, 'protocol');
  assert.equal(cls(wa({ message: { reactionMessage: { text: 'x' } } })).reason, 'unsupported:reactionMessage');
  assert.equal(cls(wa({ message: { stickerMessage: {} } })).reason, 'unsupported:stickerMessage');
  assert.equal(cls(wa({ message: { videoMessage: {} } })).reason, 'unsupported:videoMessage');
  assert.equal(GROUP, cfg.groupJid);
});
