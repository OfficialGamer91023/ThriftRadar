"use client";

import { useEffect, useState } from "react";
import Notice from "@/components/Notice";
import { apiFetch, errorText } from "@/lib/api";
import { useConfig } from "@/lib/config";
import type { Status } from "@/lib/types";

const QUEUE_LABEL: Record<string, string> = {
  received: "Waiting",
  processing: "Being processed",
  awaiting_vlm: "Waiting for the AI",
  done: "Done",
  failed: "Failed",
};

export default function StatusPage() {
  const demo = useConfig()?.demo ?? false;
  const [s, setS] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = () =>
      apiFetch<Status>("/api/status")
        .then((v) => alive && (setS(v), setError(null)))
        .catch((e) => alive && setError(errorText(e)));
    void tick();
    const t = setInterval(tick, 10_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

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
              <span className="pill warn">not set up yet</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
