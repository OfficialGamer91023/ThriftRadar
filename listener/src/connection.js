// createSocket, readOnlyGuard, onConnectionUpdate, scheduleReconnect (DESIGN §4.1).
// The listener only reads: user-visible actions are blocked at the proxy, Baileys' noisy defaults are turned off.
import makeWASocket, { Browsers, DisconnectReason, jidNormalizedUser, makeCacheableSignalKeyStore } from 'baileys';
import qrcode from 'qrcode-terminal';

export const DENIED = new Set([
  'sendMessage', 'relayMessage', 'readMessages', 'sendReceipt', 'sendReceipts', 'sendPresenceUpdate',
  'presenceSubscribe', 'chatModify', 'star', 'updateMediaMessage', 'groupCreate', 'groupLeave',
  'groupUpdateSubject', 'groupUpdateDescription', 'groupParticipantsUpdate', 'groupSettingUpdate', 'groupInviteCode',
  'groupRevokeInvite', 'groupAcceptInvite', 'groupToggleEphemeral', 'updateBlockStatus', 'logout',
  'sendPeerDataOperationMessage',
]);

export class ReadOnlyViolation extends Error {}

const EVENTS = ['creds.update', 'connection.update', 'messages.upsert'];
function detach(sock) {
  for (const e of EVENTS) {
    try { sock?.ev?.removeAllListeners?.(e); } catch { /* already closed */ }
  }
}

export function readOnlyGuard(sock, { allowMediaReupload = false } = {}) {
  return new Proxy(sock, {
    get(target, prop, receiver) {
      if (typeof prop === 'string'
        && (DENIED.has(prop) || prop.startsWith('updateProfile'))
        && !(allowMediaReupload && prop === 'updateMediaMessage')) {
        throw new ReadOnlyViolation(`the listener is read-only: ${prop} is not allowed`);
      }
      return Reflect.get(target, prop, receiver);
    },
  });
}

/** First-layer drop: every chat except the target group, every broadcast/newsletter, and every user except us. */
export function isIgnoredJid(jid, groupJid, me) {
  if (!jid) return false;
  if (jid.endsWith('@g.us')) return jid !== groupJid;
  if (jid.endsWith('@broadcast') || jid.endsWith('@newsletter')) return true;
  const own = [me?.id, me?.lid].filter(Boolean).map((j) => jidNormalizedUser(j));
  return !own.includes(jidNormalizedUser(jid));
}

export function createSocket(cfg, state, { logger }) {
  const sock = makeWASocket({
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, logger) },
    logger,
    markOnlineOnConnect: false, // default true: the account would show online and the phone would stop notifying
    syncFullHistory: false,
    shouldSyncHistoryMessage: () => false,
    shouldIgnoreJid: cfg.filterJids && cfg.groupJid ? (jid) => isIgnoredJid(jid, cfg.groupJid, state.creds.me) : () => false,
    getMessage: async () => undefined, // we never resend anything
    emitOwnEvents: false,
    generateHighQualityLinkPreview: false,
    browser: Browsers.macOS('Desktop'), // the label in the phone's Linked devices
  });
  return readOnlyGuard(sock, { allowMediaReupload: cfg.allowMediaReupload });
}

/** What to do when the connection closes (DESIGN §4.1 table). -> {action, exitCode?, state, name} */
export function closeAction(code) {
  const name = Object.entries(DisconnectReason).find(([k, v]) => v === code && isNaN(Number(k)))?.[0] ?? 'unknown';
  switch (code) {
    case DisconnectReason.restartRequired: return { action: 'reconnect', delay: 0, state: 'reconnecting', name };
    case DisconnectReason.loggedOut: return { action: 'exit', exitCode: 2, state: 'logged_out', name };
    case DisconnectReason.connectionReplaced: return { action: 'exit', exitCode: 3, state: 'replaced', name };
    case DisconnectReason.badSession:
    case DisconnectReason.multideviceMismatch: return { action: 'exit', exitCode: 4, state: 'bad_session', name };
    case DisconnectReason.forbidden: return { action: 'exit', exitCode: 6, state: 'forbidden', name };
    default: return { action: 'reconnect', state: 'reconnecting', name };
  }
}

/**
 * Owns the socket across reconnects. `hooks`: onSocket(sock) registers message handlers, heartbeat(state, detail),
 * exit(code, reason), log.
 */
export class Connection {
  constructor({ cfg, state, saveCreds, logger, hooks, now = Date.now, random = Math.random,
    setTimer = setTimeout, clearTimer = clearTimeout, create = createSocket, showQr = (qr) => qrcode.generate(qr, { small: true }) }) {
    Object.assign(this, { cfg, state, saveCreds, logger, hooks, now, random, setTimer, clearTimer, create, showQr });
    this.sock = null;
    this.state_ = 'connecting';
    this.attempt = 0;
    this.failures = [];
    this.reconnectTimer = null;
    this.stableTimer = null;
  }

  connect() {
    this.sock = this.create(this.cfg, this.state, { logger: this.logger });
    this.sock.ev.on('creds.update', this.saveCreds);
    this.sock.ev.on('connection.update', (u) => this.onConnectionUpdate(u));
    this.hooks.onSocket?.(this.sock);
    return this.sock;
  }

  setState(s, detail = null) {
    if (s === this.state_) return;
    this.hooks.log?.info({ from: this.state_, to: s }, 'listener state');
    this.state_ = s;
    void this.hooks.heartbeat?.(s, detail);
  }

  onConnectionUpdate(update) {
    if (update.qr) {
      this.showQr(update.qr); // never logged or written to disk
      this.setState('awaiting_qr');
    }
    if (update.connection === 'open') {
      this.setState('open');
      void this.hooks.heartbeat?.('open');
      if (this.stableTimer) this.clearTimer(this.stableTimer);
      this.stableTimer = this.setTimer(() => { this.attempt = 0; }, 60_000);
      this.stableTimer?.unref?.();
    }
    if (update.connection === 'close') {
      const code = update.lastDisconnect?.error?.output?.statusCode;
      const act = closeAction(code);
      this.hooks.log?.warn({ code: code ?? null, reason: act.name }, 'connection closed'); // never the error body
      if (act.action === 'exit') {
        this.setState(act.state);
        void Promise.resolve(this.hooks.heartbeat?.(act.state)).finally(() => this.hooks.exit(act.exitCode, act.name));
        return;
      }
      this.setState('reconnecting');
      this.scheduleReconnect(act.delay);
    }
  }

  scheduleReconnect(delayOverrideMs) {
    if (this.reconnectTimer) return; // one reconnect in flight: no storm
    const t = this.now();
    this.failures = this.failures.filter((f) => t - f < 30 * 60_000);
    this.failures.push(t);
    if (this.failures.length >= 10) {
      this.setState('stopped', 'reconnect storm');
      void Promise.resolve(this.hooks.heartbeat?.('stopped', 'reconnect storm')).finally(() => this.hooks.exit(5, 'reconnect_storm'));
      return;
    }
    const delay = delayOverrideMs ?? Math.min(300_000, 2_000 * 2 ** this.attempt) * (0.5 + this.random() / 2);
    this.attempt++;
    this.reconnectTimer = this.setTimer(() => {
      this.reconnectTimer = null;
      const old = this.sock;
      detach(old);
      try { old?.end?.(undefined); } catch { /* already closed */ }
      this.connect();
    }, delay);
  }

  /** Graceful stop: never logout() (that would unlink the device from the phone). */
  end() {
    if (this.reconnectTimer) this.clearTimer(this.reconnectTimer);
    if (this.stableTimer) this.clearTimer(this.stableTimer);
    detach(this.sock);
    try { this.sock?.end?.(undefined); } catch { /* already closed */ }
  }
}
