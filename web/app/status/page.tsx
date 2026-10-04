"use client";

import { useEffect, useState } from "react";
import Notice from "@/components/Notice";
import { apiFetch, errorText } from "@/lib/api";
import { useConfig } from "@/lib/config";
import { ago } from "@/lib/format";
import type { ListenerStatus, Status } from "@/lib/types";

const QUEUE_LABEL: Record<string, string> = {
  received: "Waiting",
  processing: "Being processed",
  awaiting_vlm: "Waiting for the AI",
  done: "Done",
  failed: "Failed",
};

const LISTENER_TEXT: Record<ListenerStatus["state"], [string, "ok" | "warn" | "stop"]> = {
  never_seen: ["not started", "warn"],
  connecting: ["connecting", "warn"],
  awaiting_qr: ["waiting for the QR scan", "warn"],
  open: ["connected", "ok"],
  reconnecting: ["reconnecting", "warn"],
  logged_out: ["logged out: re-pair", "stop"],
  replaced: ["replaced by another client", "stop"],
  bad_session: ["session broken: re-pair", "stop"],
  forbidden: ["account restricted", "stop"],
  stopped: ["stopped", "stop"],
};

function listenerPill(l: ListenerStatus | null): [string, "ok" | "warn" | "stop"] {
  if (!l) return ["unknown", "warn"];
  const [text, tone] = LISTENER_TEXT[l.state] ?? ["unknown", "warn"];
  if (l.state !== "never_seen" && l.stale) return [`no heartbeat: ${text}`, "stop"];
  return [text, tone];
}

export default function StatusPage() {
  const config = useConfig();
  const demo = config?.demo ?? false;
  const [listener, setListener] = useState<ListenerStatus | null>(null);
  const [s, setS] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const local = config !== null && !config.demo; // the listener exists only on your Mac
    const tick = () => {
      apiFetch<Status>("/api/status")
        .then((v) => alive && (setS(v), setError(null)))
        .catch((e) => alive && setError(errorText(e)));
      if (local) {
        apiFetch<ListenerStatus>("/api/listener/status").then((v) => alive && setListener(v)).catch(() => {});
      }
    };
    tick();
    const t = setInterval(tick, 10_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [config]);

  if (error && !s) return <Notice kind="error">{error}</Notice>;
  if (!s) return <p className="muted">Loading…</p>;

  const used = s.vlm.daily_cap ? Math.min(100, (100 * s.vlm.calls_today) / s.vlm.daily_cap) : 0;
  const order = ["received", "processing", "awaiting_vlm", "done", "failed"];
  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>Status</h1>
        <span className="muted mono">refreshes every 10 s</span>
      </div>
      {error && <Notice kind="warn">Lost contact with the server: {error}</Notice>}

      <div className="panel">
        <h2>Posts</h2>
        <div className="stats">
          {order.map((k) => (
            <div className="stat" key={k}>
              <b>{(s.queue[k] ?? 0).toLocaleString("en-US")}</b>
              <span>{QUEUE_LABEL[k]}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="two">
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2>AI extraction</h2>
            {s.vlm.provider === "off" ? (
              <span className="pill warn">off</span>
            ) : s.vlm.paused ? (
              <span className="pill stop">paused: key or billing problem</span>
            ) : (
              <span className="pill ok">running</span>
            )}
          </div>
          <p className="mono muted">{s.vlm.provider} · {s.vlm.model}</p>
          <div className="stack" style={{ gap: 6 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span>{s.vlm.calls_today.toLocaleString("en-US")} of {s.vlm.daily_cap.toLocaleString("en-US")} calls today</span>
              <span className="mono">≈ ${s.vlm.est_cost_today_usd.toFixed(2)}</span>
            </div>
            <div className="bar" role="progressbar" aria-valuenow={Math.round(used)} aria-valuemin={0} aria-valuemax={100}
              aria-label="Daily AI budget used"><i style={{ width: `${used}%` }} /></div>
          </div>
          <p className="muted" style={{ fontSize: "0.85rem" }}>The cost is an estimate; the daily call cap is the hard limit.</p>
        </div>

        <div className="panel">
          <h2>{demo ? "Server" : "This Mac"}</h2>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>On-device models</span>
            <span className={`pill ${s.models_ready ? "ok" : "warn"}`}>{s.models_ready ? "ready" : "loading"}</span>
          </div>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>{demo ? "Your active wishlists" : "Active wishlists"}</span>
            <span className="mono">{s.wishlists}</span>
          </div>
          {!demo && (
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span>WhatsApp listener</span>
              <span className={`pill ${listenerPill(listener)[1]}`}>{listenerPill(listener)[0]}</span>
            </div>
          )}
          {!demo && listener?.last_message_at && (
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span>Last group message</span>
              <span className="mono">{ago(listener.last_message_at)}</span>
            </div>
          )}
          {!demo && (listener?.spool_pending ?? 0) > 0 && (
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span>Albums waiting to be sent</span>
              <span className="mono">{listener?.spool_pending}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
