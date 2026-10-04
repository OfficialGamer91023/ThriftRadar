// AlbumBuffer (DESIGN §4.1): groups one seller's photos and text into a post. The join rule is identical to
// backend/app/grouping.py; both are tested against shared/grouping_cases.json. Joining uses WhatsApp message
// timestamps; the flush timer uses arrival time.

export class AlbumBuffer {
  /**
   * @param {object} o
   * @param {object} o.cfg       albumIdleMs, albumMaxSpanMs, albumMaxImages, albumMaxBytes, maxOpenAlbums
   * @param {import('./spool.js').Spool} o.spool
   * @param {(albumId: string) => void} o.onReady   called with each album that has a usable photo
   * @param {(reason: string) => void} [o.onDropped] dropped_text_only / dropped_all_media_missing
   */
  constructor({ cfg, spool, onReady, onDropped = () => {}, log = null, now = Date.now,
    setTimer = setTimeout, clearTimer = clearTimeout }) {
    Object.assign(this, { cfg, spool, onReady, onDropped, log, now, setTimer, clearTimer });
    this.open = new Map(); // `${chat}\n${sender}` -> album
  }

  static bufKey(chat, sender) {
    return `${chat}\n${sender}`;
  }

  joins(a, item) {
    const { albumIdleMs, albumMaxSpanMs, albumMaxImages, albumMaxBytes } = this.cfg;
    return item.ts - a.lastTs <= albumIdleMs / 1000
      && item.ts - a.firstTs <= albumMaxSpanMs / 1000
      && (item.kind !== 'image' || (a.images < albumMaxImages && a.totalBytes + (item.size ?? item.bytes?.length ?? 0) <= albumMaxBytes));
  }

  /**
   * item: {key, kind:'image'|'text', ts, chat, sender, senderAlt, caption, bytes (Buffer|null), missing, reason}
   */
  add(item) {
    const k = AlbumBuffer.bufKey(item.chat, item.sender);
    let a = this.open.get(k);
    if (a && a.items.some((x) => x.key === item.key)) return a.id; // idempotent per key
    if (a && !this.joins(a, item)) {
      this.flush(a);
      a = undefined;
    }
    if (!a) {
      if (this.open.size >= this.cfg.maxOpenAlbums) {
        const oldest = [...this.open.values()].sort((x, y) => x.lastArrivalMs - y.lastArrivalMs)[0];
        this.flush(oldest);
      }
      const nowMs = this.now();
      a = { id: this.spool.newId(nowMs), chat: item.chat, sender: item.sender, senderAlt: item.senderAlt ?? null,
        firstTs: item.ts, lastTs: item.ts, firstArrivalMs: nowMs, lastArrivalMs: nowMs, images: 0, totalBytes: 0,
        items: [] };
      this.open.set(k, a);
    }
    const size = item.size ?? item.bytes?.length ?? 0;
    const file = item.kind === 'image' && !item.missing ? this.spool.writeItem(a.id, item.key, item.bytes) : null;
    a.items.push({ key: item.key, kind: item.kind, ts: item.ts, caption: item.caption ?? null, file,
      missing: Boolean(item.missing), reason: item.reason ?? null });
    if (item.kind === 'image') {
      a.images += 1;
      a.totalBytes += size;
    }
    a.senderAlt ??= item.senderAlt ?? null;
    a.lastTs = Math.max(a.lastTs, item.ts);
    a.lastArrivalMs = this.now();
    this.spool.writeMeta('open', a.id, this.metaOf(a));
    this.arm(a);
    return a.id;
  }

  metaOf(a) {
    const { timer, ...rest } = a;
    return rest;
  }

  arm(a) {
    if (a.timer) this.clearTimer(a.timer);
    const due = Math.min(this.now() + this.cfg.albumIdleMs + 5_000, a.firstArrivalMs + this.cfg.albumMaxSpanMs + 5_000);
    a.timer = this.setTimer(() => this.flush(a), Math.max(0, due - this.now()));
    a.timer?.unref?.();
  }

  flush(a) {
    const k = AlbumBuffer.bufKey(a.chat, a.sender);
    if (this.open.get(k) !== a) return;
    this.open.delete(k);
    if (a.timer) this.clearTimer(a.timer);
    const usable = a.items.some((x) => x.kind === 'image' && !x.missing);
    if (!usable) {
      const reason = a.items.some((x) => x.kind === 'image') ? 'dropped_all_media_missing' : 'dropped_text_only';
      this.spool.discard('open', a.id);
      this.onDropped(reason);
      return;
    }
    this.spool.markReady(a.id);
    this.onReady(a.id);
  }

  flushAll() {
    for (const a of [...this.open.values()]) this.flush(a);
  }

  /** Startup: overdue open albums are flushed, fresh ones re-armed; ready ones go back to the poster. */
  restoreFromSpool() {
    const restored = { flushed: 0, rearmed: 0, rejected: 0, ready: 0 };
    const alreadyReady = this.spool.list('ready'); // before restoring: a flush below adds to ready/ and enqueues itself
    for (const id of this.spool.list('open')) {
      let a;
      try {
        a = this.spool.readMeta('open', id);
        if (!Array.isArray(a.items) || !a.chat || !a.sender) throw new Error('bad meta');
      } catch {
        this.spool.reject('open', id, 'corrupt_meta', null);
        restored.rejected++;
        continue;
      }
      this.open.set(AlbumBuffer.bufKey(a.chat, a.sender), a);
      if (this.now() - a.lastArrivalMs >= this.cfg.albumIdleMs) {
        this.flush(a);
        restored.flushed++;
      } else {
        this.arm(a);
        restored.rearmed++;
      }
    }
    for (const id of alreadyReady) {
      this.onReady(id);
      restored.ready++;
    }
    return restored;
  }
}
