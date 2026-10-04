// The on-disk album spool (DESIGN §4.1 spool.*): open/ -> ready/ -> posted (deleted) or rejected/. Image bytes
// live here, not in memory, so nothing buffered is lost on a crash or restart.
import { createHash, randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export class Spool {
  constructor(dir) {
    this.dir = dir;
    for (const s of ['open', 'ready', 'rejected']) mkdirSync(path.join(dir, s), { recursive: true, mode: 0o700 });
  }

  newId(now = Date.now()) {
    return `${now}-${randomBytes(4).toString('hex')}`;
  }

  path(where, id, file = '') {
    return path.join(this.dir, where, id, file);
  }

  static fileFor(key) {
    return createHash('sha256').update(key).digest('hex').slice(0, 16) + '.img';
  }

  writeAtomic(file, data) {
    const tmp = file + '.tmp';
    writeFileSync(tmp, data, { mode: 0o600 });
    renameSync(tmp, file);
  }

  writeItem(albumId, key, bytes) {
    const dir = this.path('open', albumId);
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    const file = Spool.fileFor(key);
    if (bytes && !existsSync(path.join(dir, file))) this.writeAtomic(path.join(dir, file), bytes);
    return file;
  }

  writeMeta(where, albumId, meta) {
    const dir = this.path(where, albumId);
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    this.writeAtomic(path.join(dir, 'meta.json'), JSON.stringify(meta));
  }

  readMeta(where, albumId) {
    return JSON.parse(readFileSync(this.path(where, albumId, 'meta.json'), 'utf8'));
  }

  readFile(where, albumId, file) {
    return readFileSync(this.path(where, albumId, file));
  }

  markReady(albumId) {
    renameSync(this.path('open', albumId), this.path('ready', albumId));
  }

  discard(where, albumId) {
    rmSync(this.path(where, albumId), { recursive: true, force: true });
  }

  reject(where, albumId, status, detail) {
    const dest = this.path('rejected', albumId);
    renameSync(this.path(where, albumId), dest);
    writeFileSync(path.join(dest, 'reason.json'), JSON.stringify({ status, detail, at: new Date().toISOString() }));
  }

  list(where) {
    return readdirSync(path.join(this.dir, where)).filter((n) => !n.startsWith('.')).sort();
  }

  pruneRejected(maxAgeDays = 7, now = Date.now()) {
    let n = 0;
    for (const id of this.list('rejected')) {
      if (now - statSync(this.path('rejected', id)).mtimeMs > maxAgeDays * 86_400_000) {
        this.discard('rejected', id);
        n++;
      }
    }
    return n; // rejected albums hold seller photos, so they don't stay forever
  }
}
