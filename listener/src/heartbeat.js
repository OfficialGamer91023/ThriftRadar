// sendHeartbeat (DESIGN §4.1): state for the status page and the "logged out" notification. Never throws.
export function makeHeartbeat({ cfg, version, snapshot, fetchImpl = fetch, log = null }) {
  return async function sendHeartbeat(state, detail = null) {
    try {
      await fetchImpl(cfg.heartbeatUrl, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ingest-Token': cfg.ingestToken },
        body: JSON.stringify({ state, detail, version, ...snapshot() }), signal: AbortSignal.timeout(5_000),
      });
    } catch (e) {
      log?.debug({ err: e.name }, 'heartbeat failed');
    }
  };
}
