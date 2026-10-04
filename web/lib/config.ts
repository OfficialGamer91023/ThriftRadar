"use client";

// useConfig (DESIGN §4.9): reads /api/config once per page load. Hiding WhatsApp features in demo is cosmetic;
// the server enforces it.

import { useEffect, useState } from "react";
import type { Config } from "./types";

let cached: Promise<Config> | null = null;

export function loadConfig(): Promise<Config> {
  cached ??= fetch("/api/config", { credentials: "same-origin" })
    .then((r) => (r.ok ? (r.json() as Promise<Config>) : Promise.reject(new Error(String(r.status)))))
    .catch((e) => {
      cached = null;
      throw e;
    });
  return cached;
}

export function useConfig(): Config | null {
  const [config, setConfig] = useState<Config | null>(null);
  useEffect(() => {
    let alive = true;
    loadConfig().then((c) => alive && setConfig(c)).catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return config;
}
