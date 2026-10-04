"use client";

import { useConfig } from "@/lib/config";

export default function Footer() {
  const demo = useConfig()?.demo ?? false;
  if (!demo) return null;
  return (
    <footer className="wrap footer">
      <span>ThriftRadar demo: sample listings with made-up sizes and prices.</span>
      <a href="/credits/">Photo credits</a>
    </footer>
  );
}
