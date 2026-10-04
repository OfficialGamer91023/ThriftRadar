"use client";

import { usePathname } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { useConfig } from "@/lib/config";

const LINKS = [
  { href: "/", label: "Feed" },
  { href: "/wishlists/", label: "Wishlists" },
  { href: "/simulate/", label: "Simulate", demo: true },
  { href: "/status/", label: "Status" },
];

async function logout() {
  try {
    await apiFetch("/api/logout", { method: "POST" });
  } finally {
    window.location.href = "/login/";
  }
}

export default function Nav() {
  const path = usePathname() || "/";
  const config = useConfig();
  const demo = config?.demo ?? false;
  if (demo && path.startsWith("/login")) return null;
  return (
    <nav className="nav" aria-label="Main">
      {LINKS.filter((l) => !l.demo || demo).map((l) => {
        const active = l.href === "/" ? path === "/" || path.startsWith("/listing") : path.startsWith(l.href);
        return (
          <a key={l.href} href={l.href} aria-current={active ? "page" : undefined}>
            {l.label}
          </a>
        );
      })}
      {demo && (
        <button type="button" className="navbtn" onClick={() => void logout()}>
          Log out
        </button>
      )}
    </nav>
  );
}
