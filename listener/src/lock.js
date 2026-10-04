// One listener per Mac (DESIGN §4.1 main pre 3): O_EXCL lockfile holding the pid; a stale one is replaced.
import { openSync, readFileSync, unlinkSync, writeSync, closeSync } from 'node:fs';

export class LockHeld extends Error {}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === 'EPERM';
  }
}

export function acquireLock(file, pid = process.pid) {
  for (let i = 0; i < 2; i++) {
    try {
      const fd = openSync(file, 'wx', 0o600);
      writeSync(fd, String(pid));
      closeSync(fd);
      return () => {
        try {
          if (readFileSync(file, 'utf8').trim() === String(pid)) unlinkSync(file);
        } catch { /* already gone */ }
      };
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      const other = Number(readFileSync(file, 'utf8').trim());
      if (Number.isInteger(other) && other > 0 && other !== pid && alive(other)) throw new LockHeld(`pid ${other}`);
      unlinkSync(file); // stale
    }
  }
  throw new LockHeld('could not take the lock');
}
