// listGroups (DESIGN §4.1): pair if needed, print JID / name / member count to this terminal only, then exit.
// No message handler is registered, so no message is processed.
import { Connection } from './connection.js';

export function listGroups(cfg, auth, logger, { print = console.log } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (code) => {
      if (done) return;
      done = true;
      conn.end();
      resolve(code);
    };
    const conn = new Connection({
      cfg: { ...cfg, groupJid: null }, state: auth.state, saveCreds: auth.saveCreds, logger,
      hooks: {
        exit: (code) => finish(code),
        onSocket: (sock) => sock.ev.on('connection.update', async (u) => {
          if (u.connection !== 'open') return;
          try {
            const groups = Object.values(await sock.groupFetchAllParticipating()); // read-only query
            groups.sort((a, b) => (a.subject ?? '').localeCompare(b.subject ?? ''));
            print('JID\tname\tmembers');
            for (const g of groups) print(`${g.id}\t${g.subject ?? ''}\t${g.participants?.length ?? '?'}`);
            finish(0);
          } catch (e) {
            print(`could not list groups: ${e.message}`);
            finish(1);
          }
        }),
      },
    });
    conn.connect();
  });
}
