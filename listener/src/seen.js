// SeenStore (DESIGN §4.1): hashes of handled messages, so redelivery never downloads twice. Holds no JIDs.
import { appendFileSync, existsSync, fsyncSync, openSync, closeSync, readFileSync, renameSync, writeFileSync } from 'node:fs';

export class SeenStore {
  constructor(file, max = 20_000) {
    this.file = file;
    this.max = max;
    this.lines = [];
    this.set = new Set();
  }

  load() {
    if (existsSync(this.file)) {
      this.lines = readFileSync(this.file, 'utf8').split('\n').filter(Boolean).slice(-this.max);
      this.set = new Set(this.lines);
    }
    return this;
  }

  has(k) {
    return this.set.has(k);
  }

  add(k) {
    if (this.set.has(k)) return;
    this.set.add(k);
    this.lines.push(k);
    appendFileSync(this.file, k + '\n', { mode: 0o600 });
    if (this.lines.length > 2 * this.max) this.compact();
  }

  compact() {
    this.lines = this.lines.slice(-this.max);
    this.set = new Set(this.lines);
    const tmp = this.file + '.tmp';
    writeFileSync(tmp, this.lines.join('\n') + '\n', { mode: 0o600 });
    const fd = openSync(tmp, 'r');
    fsyncSync(fd);
    closeSync(fd);
    renameSync(tmp, this.file);
  }
}
