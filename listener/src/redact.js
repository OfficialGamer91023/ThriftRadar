// maskJid (DESIGN §4.1): logs can correlate senders without ever containing a number or JID.
import { createHash } from 'node:crypto';

export function makeMasker(salt) {
  return (jid) => (jid ? 'u:' + createHash('sha256').update(salt + jid).digest('hex').slice(0, 8) : 'u:none');
}
