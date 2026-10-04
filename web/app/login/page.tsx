"use client";

// Demo login (DESIGN §4.9). The credentials are public and printed here: they work only when the server runs
// with DEMO_MODE=1, which the server checks, not this page.

import { useState } from "react";
import Notice from "@/components/Notice";
import { apiFetch, errorText } from "@/lib/api";
import { useConfig } from "@/lib/config";

const EMAIL = "demo@thriftradar.app";
const PASSWORD = "demo1234";

export default function LoginPage() {
  const config = useConfig();
  const [email, setEmail] = useState(EMAIL);
  const [password, setPassword] = useState(PASSWORD);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiFetch("/api/login", { method: "POST", body: JSON.stringify({ email, password }) });
      window.location.href = "/";
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  if (config && !config.demo) {
    return (
      <div className="stack" style={{ gap: 12, maxWidth: 460 }}>
        <h1>No login here</h1>
        <p>This is your own ThriftRadar on this Mac, so there's nothing to log in to.</p>
        <p><a href="/">Go to the feed</a></p>
      </div>
    );
  }

  return (
    <div className="stack" style={{ gap: 18, maxWidth: 460, marginInline: "auto", paddingTop: 24 }}>
      <div className="stack" style={{ gap: 6 }}>
        <h1>Try the ThriftRadar demo</h1>
        <p className="muted">
          A search engine for shoes posted in a WhatsApp thrift group. The demo has sample listings with licensed
          photos, and you can simulate posting your own. Nothing here comes from a real group.
        </p>
      </div>
      <div className="creds" aria-label="Demo login">
        <span>email: {EMAIL}</span>
        <span>password: {PASSWORD}</span>
      </div>
      <form className="panel" onSubmit={submit}>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" className="input" type="email" autoComplete="username" value={email}
            onChange={(e) => setEmail(e.target.value)} required maxLength={200} />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" className="input" type="password" autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} required maxLength={200} />
        </div>
        <button className="btn primary" type="submit" disabled={busy}>{busy ? "Logging in…" : "Log in"}</button>
        {error && <Notice kind="error">{error}</Notice>}
      </form>
      <p className="muted" style={{ fontSize: "0.85rem" }}>
        Your session lasts 24 hours. Anything you upload is visible only to you and deleted after 24 hours.
      </p>
    </div>
  );
}
